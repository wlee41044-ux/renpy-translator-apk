"""번역 큐: DB 의 대기(pending) 원문을 배치로 번역하고 배치마다 저장한다.

언제 끊겨도(일시정지, 앱 종료, 프로세스 kill) 다음 실행은 DB 의 pending 부터 이어간다.

control (Kotlin 객체 또는 테스트용 파이썬 객체):
    isStopped() -> bool       일시정지 요청 여부
    log(msg: str)             진행 로그
    progress(done, total)     완료 줄 수 / 전체 줄 수 (화면 진행률)
"""

import json
import time

from . import db
from .protect import RestoreError, protect, restore, structure_problem
from .translators import RateLimited, TranslatorError, create
from .translators.base import Stopped

# Ren'Py 언어 이름 → Google 언어 코드
LANG_CODES = {
    "korean": "ko", "japanese": "ja", "english": "en", "schinese": "zh-CN",
    "chinese": "zh-CN", "tchinese": "zh-TW", "spanish": "es", "french": "fr", "german": "de",
    "russian": "ru", "portuguese": "pt", "italian": "it", "vietnamese": "vi", "thai": "th",
    "indonesian": "id", "polish": "pl", "turkish": "tr", "ukrainian": "uk", "arabic": "ar",
}


def lang_code(renpy_language):
    return LANG_CODES.get(renpy_language.lower(), renpy_language)


class _Ctl:
    def __init__(self, control):
        self.c = control

    def stopped(self):
        try:
            return bool(self.c.isStopped()) if self.c is not None else False
        except Exception:
            return False

    def log(self, msg):
        if self.c is not None:
            try:
                self.c.log(str(msg))
            except Exception:
                pass

    def progress(self, done, total):
        if self.c is not None:
            try:
                self.c.progress(int(done), int(total))
            except Exception:
                pass


def run(db_path, game_id, engine="google", opts_json="{}", control=None, local_bridge=None):
    """반환: JSON {status: finished|stopped|error, translated, review, copied, message}"""
    opts = json.loads(opts_json or "{}")
    ctl = _Ctl(control)
    con = db.connect(db_path)
    game = db.get_game(con, game_id)
    if game is None:
        return json.dumps({"status": "error", "message": "no such game"})
    source_lang = opts.get("source_lang") or "auto"
    target_lang = opts.get("target_code") or lang_code(game["language"])
    batch_size = int(opts.get("batch_size") or 0)
    fallback = opts.get("fallback")  # 예: "local"

    engines = [engine] + ([fallback] if fallback and fallback != engine else [])
    result = {"status": "finished", "translated": 0, "review": 0, "copied": 0, "message": ""}
    try:
        tr = create(engines[0], opts.get(engines[0], {}), local_bridge)
    except Exception as e:
        con.close()
        return json.dumps({"status": "error", "message": "엔진 생성 실패: %s" % e})
    tr.log = ctl.log
    ctx_index = {}

    def prepare(t):
        """LLM 번역기에는 캐릭터 분석 결과를 붙이고, 앞 대사를 보여 주기 위한 색인을 만든다."""
        if not getattr(t, "uses_meta", False):
            return
        profiles = db.get_profiles(con, game_id) if opts.get("use_profiles", True) else {}
        t.story = profiles.pop(db.STORY_KEY, "")
        t.profiles = profiles
        if not ctx_index:
            ctx_index.update(db.dialogue_index(con, game_id))
        if profiles or t.story:
            ctl.log("캐릭터 분석 %d명%s 을 번역에 사용" % (len(profiles), " + 작품 요약" if t.story else ""))

    def make_meta(rows):
        metas = []
        for r in rows:
            ctx = []
            if r.get("kind") == "dialogue":
                for back in (2, 1):
                    got = ctx_index.get((r.get("file"), (r.get("seq") or 0) - back))
                    if got:
                        ctx.append(got)
            metas.append({"speaker": r.get("speaker") or "", "context": ctx})
        return metas

    prepare(tr)
    if opts.get("retry_review"):
        db.reset_status(con, game_id, db.REVIEW)

    ctl.log("번역 시작: 엔진=%s, %s → %s" % (tr.name, source_lang, target_lang))

    def save_results(todo, masks, outs):
        """outs[i] 가 None 이면(못 한 줄) 대기 상태로 남긴다. 반환: 완료로 저장된 줄 수"""
        ok = 0
        with db.tx(con):
            for row, p, out in zip(todo, masks, outs):
                if out is None:
                    continue
                try:
                    text = restore(out, p.tokens)
                    if not text.strip():
                        raise RestoreError("empty translation")
                    problem = structure_problem(row["source"], text)
                    if problem:
                        raise RestoreError(problem)
                    db.set_translation(con, game_id, row["src_hash"], text, tr.name)
                    result["translated"] += 1
                    ok += 1
                except RestoreError as e:
                    db.set_translation(con, game_id, row["src_hash"], out, tr.name,
                                       status=db.REVIEW, note=str(e))
                    result["review"] += 1
        return ok

    try:
        st = db.stats(con, game_id)
        done, total = st[db.DONE], st["total"]
        ctl.progress(done, total)
        rows = db.pending_all(con, game_id)      # 배치마다 DB 를 다시 훑지 않는다
        pos = 0
        while pos < len(rows):
            if ctl.stopped():
                result["status"] = "stopped"
                break
            n = batch_size or tr.batch_lines
            batch = rows[pos:pos + n]

            t_db = time.monotonic()
            todo, masks = [], []
            copied = 0
            with db.tx(con):
                for row in batch:
                    p = protect(row["source"])
                    if not p.translatable:
                        db.set_translation(con, game_id, row["src_hash"], row["source"], "copy")
                        copied += 1
                    else:
                        todo.append(row)
                        masks.append(p)
            db_time = time.monotonic() - t_db
            result["copied"] += copied
            done += copied
            if not todo:
                pos += len(batch)
                ctl.progress(done, total)
                continue

            t0 = time.monotonic()
            try:
                extra = {"meta": make_meta(todo)} if getattr(tr, "uses_meta", False) else {}
                outs = tr.translate_batch([p.masked for p in masks], source_lang, target_lang,
                                          should_stop=ctl.stopped, **extra)
            except Stopped as e:
                part = getattr(e, "partial", None)
                if part:
                    done += save_results(todo, masks, part)
                    ctl.progress(done, total)
                result["status"] = "stopped"
                break
            except (RateLimited, TranslatorError) as e:
                part = getattr(e, "partial", None)
                if part:
                    saved = save_results(todo, masks, part)      # 이미 성공한 청크는 버리지 않는다
                    done += saved
                    ctl.progress(done, total)
                    if saved:
                        ctl.log("실패 전에 끝난 %d줄은 저장" % saved)
                ctl.log("엔진 실패: %s" % e)
                if len(engines) > 1 and tr.name == engines[0]:
                    try:
                        tr = create(engines[1], opts.get(engines[1], {}), local_bridge)
                        tr.log = ctl.log
                        prepare(tr)
                        ctl.log("대체 엔진으로 전환: %s" % tr.name)
                        continue
                    except Exception as e2:
                        ctl.log("대체 엔진 생성 실패: %s" % e2)
                result["status"] = "error"
                result["message"] = str(e)
                break
            net_time = time.monotonic() - t0
            if len(outs) != len(todo):
                result["status"] = "error"
                result["message"] = "engine returned %d results for %d" % (len(outs), len(todo))
                break

            t_db = time.monotonic()
            ok = save_results(todo, masks, outs)
            db_time += time.monotonic() - t_db
            pos += len(batch)
            done += ok
            ctl.progress(done, total)
            rep = tr.take_report()
            ctl.log("%d줄 %.1fs (%.0f줄/초)%s · DB %.1fs · 검수필요 누적 %d"
                    % (len(todo), net_time + db_time, len(todo) / max(0.001, net_time + db_time),
                       (" · " + rep) if rep else "", db_time, result["review"]))
    finally:
        st = db.stats(con, game_id)
        ctl.progress(st[db.DONE], st["total"])
        try:
            tr.close()
        except Exception:
            pass
        con.close()
    ctl.log("번역 %s: 완료 %d, 검수필요 %d, 그대로 %d"
            % ({"finished": "끝", "stopped": "일시정지", "error": "중단(오류)"}[result["status"]],
               result["translated"], result["review"], result["copied"]))
    return json.dumps(result, ensure_ascii=False)
