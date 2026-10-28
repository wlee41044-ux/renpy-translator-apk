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
from .protect import RestoreError, protect, restore
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
    if opts.get("retry_review"):
        db.reset_status(con, game_id, db.REVIEW)

    ctl.log("번역 시작: 엔진=%s, %s → %s" % (tr.name, source_lang, target_lang))
    try:
        while True:
            if ctl.stopped():
                result["status"] = "stopped"
                break
            st = db.stats(con, game_id)
            ctl.progress(st[db.DONE], st["total"])
            n = batch_size or tr.max_batch
            batch = db.pending_texts(con, game_id, n)
            if not batch:
                break

            todo, masks = [], []
            with db.tx(con):
                for row in batch:
                    p = protect(row["source"])
                    if not p.translatable:
                        db.set_translation(con, game_id, row["src_hash"], row["source"], "copy")
                        result["copied"] += 1
                    else:
                        todo.append(row)
                        masks.append(p)
            if not todo:
                continue

            t0 = time.monotonic()
            try:
                outs = tr.translate_batch([p.masked for p in masks], source_lang, target_lang,
                                          should_stop=ctl.stopped)
            except Stopped:
                result["status"] = "stopped"
                break
            except (RateLimited, TranslatorError) as e:
                ctl.log("엔진 실패: %s" % e)
                if len(engines) > 1 and tr.name == engines[0]:
                    try:
                        tr = create(engines[1], opts.get(engines[1], {}), local_bridge)
                        tr.log = ctl.log
                        ctl.log("대체 엔진으로 전환: %s" % tr.name)
                        continue
                    except Exception as e2:
                        ctl.log("대체 엔진 생성 실패: %s" % e2)
                result["status"] = "error"
                result["message"] = str(e)
                break
            if len(outs) != len(todo):
                result["status"] = "error"
                result["message"] = "engine returned %d results for %d" % (len(outs), len(todo))
                break

            with db.tx(con):
                for row, p, out in zip(todo, masks, outs):
                    try:
                        text = restore(out, p.tokens)
                        if not text.strip():
                            raise RestoreError("empty translation")
                        db.set_translation(con, game_id, row["src_hash"], text, tr.name)
                        result["translated"] += 1
                    except RestoreError as e:
                        db.set_translation(con, game_id, row["src_hash"], out, tr.name,
                                           status=db.REVIEW, note=str(e))
                        result["review"] += 1
            ctl.log("%d줄 번역 (%.1fs), 검수필요 누적 %d"
                    % (len(todo), time.monotonic() - t0, result["review"]))
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
