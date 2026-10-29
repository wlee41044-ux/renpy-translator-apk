"""캐릭터 분석: 번역 전에 대사를 모아 캐릭터별 성격/말투를 요약해 DB 에 저장한다.

번역 단계(LocalTranslator)가 이 요약을 프롬프트에 붙여서 말투와 호칭을 일관되게 만든다.
사용자가 결과를 고칠 수 있고(auto=0), 고친 것은 다시 분석해도 덮어쓰지 않는다.

bridge: chat(system, user, max_tokens) -> str  (로컬 LLM)
control: isStopped() / log(msg) / progress(done, total)
"""

import json
import re
import time

from . import db

SYSTEM_CHAR = (
    "You are a literary analyst who prepares notes for translators of visual novels and games.\n"
    "You are given dialogue lines spoken by ONE character (original language). Write short notes in Korean, "
    "at most 5 lines, covering:\n"
    "- gender and age impression, only if the lines show it (otherwise omit)\n"
    "- personality and role\n"
    "- speech style: polite or casual, rough or gentle, sentence endings, verbal tics\n"
    "- how the character refers to themself (first-person words such as 俺/僕/私/I) and addresses others\n"
    "Rules: use only what the lines show, never invent facts. Output only the notes, no preface."
)

SYSTEM_STORY = (
    "You prepare notes for translators of a game. You are given sample dialogue lines from the whole game "
    "(original language). Write at most 5 lines in Korean: genre, setting/worldview, overall tone, and any "
    "recurring special terms. Use only what the lines show. Output only the notes."
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def _clean(text):
    text = _THINK_RE.sub("", text or "").replace("<think>", "").replace("</think>", "").strip()
    text = re.sub(r"^\s*(notes?|분석|요약)\s*[:：]\s*", "", text, flags=re.I)
    return text[:900].strip()


def sample_lines(lines, k=40, max_chars=1800, per_line=110):
    """대사가 많으면 전체에서 고르게 뽑고, 한 줄은 per_line 글자로 자른다. 모델 컨텍스트에 맞추기 위함."""
    lines = [l.strip().replace("\n", " ") for l in lines if l and l.strip()]
    # 너무 짧은 감탄사만 있는 줄보다 어느 정도 길이가 있는 줄을 우선
    rich = [l for l in lines if len(l) >= 6] or lines
    if len(rich) > k:
        step = len(rich) / float(k)
        rich = [rich[int(i * step)] for i in range(k)]
    out, total = [], 0
    for l in rich:
        l = l[:per_line]
        if total + len(l) > max_chars:
            break
        out.append(l)
        total += len(l)
    return out


class _Ctl:
    def __init__(self, c):
        self.c = c

    def stopped(self):
        try:
            return bool(self.c.isStopped()) if self.c is not None else False
        except Exception:
            return False

    def log(self, m):
        if self.c is not None:
            try:
                self.c.log(str(m))
            except Exception:
                pass

    def progress(self, d, t):
        if self.c is not None:
            try:
                self.c.progress(int(d), int(t))
            except Exception:
                pass


def run(db_path, game_id, bridge, opts_json="{}", control=None):
    """반환: JSON {status: finished|stopped|error, analyzed, skipped, message}"""
    opts = json.loads(opts_json or "{}")
    ctl = _Ctl(control)
    if bridge is None:
        return json.dumps({"status": "error", "message": "로컬 모델이 로드되지 않았습니다 (설정에서 GGUF 모델을 지정하세요)"},
                          ensure_ascii=False)
    min_lines = int(opts.get("min_lines", 5))
    max_chars = int(opts.get("max_characters", 40))
    force = bool(opts.get("force", False))
    with_story = bool(opts.get("story", True))

    con = db.connect(db_path)
    result = {"status": "finished", "analyzed": 0, "skipped": 0, "message": ""}
    try:
        existing = {r["name"]: r for r in db.list_profiles(con, game_id)}
        stats = [(n, c) for n, c in db.speaker_stats(con, game_id) if c >= min_lines][:max_chars]
        todo = []
        for name, count in stats:
            old = existing.get(name)
            if old is not None and (not force or not old["auto"]):
                result["skipped"] += 1          # 이미 있거나 사용자가 고친 것은 건드리지 않는다
                continue
            todo.append((name, count))
        need_story = with_story and (force or db.STORY_KEY not in existing)
        total = len(todo) + (1 if need_story else 0)
        ctl.log("캐릭터 분석 시작: 대상 %d명 (건너뜀 %d)%s" % (len(todo), result["skipped"],
                                                         ", 작품 요약 포함" if need_story else ""))
        done = 0
        ctl.progress(0, total)

        def ask(system, user, max_tokens):
            return _clean(bridge.chat(system, user, max_tokens))

        for name, count in todo:
            if ctl.stopped():
                result["status"] = "stopped"
                break
            sample = sample_lines([s for _, s in db.speaker_lines(con, game_id, name)])
            if not sample:
                continue
            user = "Character: %s (total %d lines)\nLines:\n%s" % (name, count, "\n".join("- " + l for l in sample))
            t0 = time.monotonic()
            try:
                text = ask(SYSTEM_CHAR, user, 320)
            except Exception as e:
                ctl.log("분석 실패(%s): %s" % (name, e))
                if getattr(e, "auto_pause", False):
                    result["status"] = "paused"
                    result["per_day"] = bool(getattr(e, "per_day", False))
                else:
                    result["status"] = "error"
                result["message"] = str(e)
                break
            if ctl.stopped():
                result["status"] = "stopped"
                break
            if text:
                with db.tx(con):
                    db.set_profile(con, game_id, name, text, auto=1, lines=count)
                result["analyzed"] += 1
                ctl.log("  [%s] (%d줄, %.0fs) %s" % (name, count, time.monotonic() - t0, text.replace("\n", " / ")[:160]))
            done += 1
            ctl.progress(done, total)

        if need_story and result["status"] == "finished":
            lines = [s for _, s in db.speaker_lines(con, game_id)]
            sample = sample_lines(lines, k=60, max_chars=2200)
            if sample:
                try:
                    text = ask(SYSTEM_STORY, "Sample lines:\n" + "\n".join("- " + l for l in sample), 320)
                    if text:
                        with db.tx(con):
                            db.set_profile(con, game_id, db.STORY_KEY, text, auto=1, lines=len(lines))
                        ctl.log("  [작품 요약] " + text.replace("\n", " / ")[:200])
                except Exception as e:
                    ctl.log("작품 요약 실패: %s" % e)
            ctl.progress(total, total)
    finally:
        con.close()
    ctl.log("캐릭터 분석 %s: %d명 분석" % ({"finished": "끝", "stopped": "일시정지", "paused": "자동 일시정지(한도)", "error": "중단(오류)"}[result["status"]],
                                      result["analyzed"]))
    return json.dumps(result, ensure_ascii=False)
