"""Kotlin(Chaquopy) 에서 부르는 진입점. 인자/반환은 단순 타입과 JSON 문자열."""

import json
import os
import shutil

from . import db, rpa, scan, tlgen
from .protect import structure_problem
from .version import parse_renpy_version, tlid_only_considers_say


def _j(o):
    return json.dumps(o, ensure_ascii=False)


def open_db(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return db.connect(path)


def add_game(db_path, name, folder_uri, language="korean"):
    con = open_db(db_path)
    try:
        return db.add_game(con, name, folder_uri, language)
    finally:
        con.close()


def list_games(db_path):
    con = open_db(db_path)
    try:
        games = db.list_games(con)
        for g in games:
            g["stats"] = db.stats(con, g["id"])
        return _j(games)
    finally:
        con.close()


def reset_workdir(work_game_dir):
    if os.path.isdir(work_game_dir):
        shutil.rmtree(work_game_dir)
    os.makedirs(work_game_dir, exist_ok=True)


def extract_archive_fd(fd, dest):
    """SAF 로 연 .rpa 의 파일 디스크립터. 호출자의 fd 는 닫지 않는다(dup 사용)."""
    with os.fdopen(os.dup(fd), "rb") as f:
        return _j(rpa.extract_scripts(f, dest))


def extract_archive_path(path, dest):
    with open(path, "rb") as f:
        return _j(rpa.extract_scripts(f, dest))


def extract_apks(apk_paths_json, work_game_dir, language=None, control=None):
    """설치된 앱의 APK 경로 목록(JSON 배열)에서 스크립트를 작업 폴더로 추출."""
    from . import apk
    log = (lambda m: control.log(m)) if control is not None else None
    return _j(apk.extract_scripts(json.loads(apk_paths_json), work_game_dir, language, log))


def is_renpy_apk(path):
    from . import apk
    return apk.is_renpy_apk(path)


def scan_game(db_path, game_id, work_game_dir, renpy_version_text=None, py_strings=True,
              skip_existing=True):
    """renpy_version_text: 게임의 renpy/vc_version.py 또는 renpy/__init__.py 내용 (없으면 None)."""
    ver = parse_renpy_version(renpy_version_text)
    items, files, errors = scan.collect_items(work_game_dir, tlid_only_considers_say(ver), bool(py_strings))
    con = open_db(db_path)
    try:
        counts = scan.apply_scan(con, game_id, items)
        existing = 0
        if skip_existing:
            # 게임에 이미 들어 있는 다른 번역(tl/<언어>)이 덮는 줄은 번역하지 않는다
            lang = db.get_game(con, game_id)["language"]
            ids, strs = tlgen.scan_existing_tl(os.path.join(work_game_dir, "tl", lang), lang)
            if ids or strs:
                existing = db.mark_existing_covered(
                    con, game_id, lambda kind, ident: ident in (ids if kind == "dialogue" else strs))
        counts["existing_translation"] = existing
        return _j({"counts": counts, "files": len(files), "items": len(items),
                   "renpy_version": ".".join(map(str, ver)) if ver else None,
                   "errors": errors, "stats": db.stats(con, game_id)})
    finally:
        con.close()


def stats(db_path, game_id):
    con = open_db(db_path)
    try:
        return _j(db.stats(con, game_id))
    finally:
        con.close()


def export(db_path, game_id, out_dir, font=None, force_language=True, existing_tl_dir=None,
           font_mode="fallback"):
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    con = open_db(db_path)
    try:
        # 이미 저장된 번역 중 태그/변수 구조가 깨진 것은 게임을 멈추게 하므로 출력에서 뺀다 (검수필요로 전환)
        moved = db.revalidate(con, game_id, structure_problem)
        written, info = tlgen.generate(con, game_id, out_dir, font=font or None,
                                       force_language=bool(force_language),
                                       existing_tl_dir=existing_tl_dir or None,
                                       font_mode=font_mode or "fallback")
        info["unsafe_moved_to_review"] = moved
        return _j({"files": written, "info": info})
    finally:
        con.close()


def set_language(db_path, game_id, language):
    import re
    if not re.match(r"^\w+$", language or ""):
        raise ValueError("언어 이름은 영문/숫자/_ 만 가능: %r" % language)
    con = open_db(db_path)
    try:
        db.set_language(con, game_id, language)
    finally:
        con.close()


def delete_game(db_path, game_id):
    con = open_db(db_path)
    try:
        db.delete_game(con, game_id)
    finally:
        con.close()


def review_list(db_path, game_id, limit=200):
    con = open_db(db_path)
    try:
        return _j(db.review_list(con, game_id, limit))
    finally:
        con.close()


def translate(db_path, game_id, engine, opts_json, control, local_bridge=None):
    from . import runner
    return runner.run(db_path, game_id, engine, opts_json, control, local_bridge)


def backup_db(db_path, keep=3):
    """작업 시작 전 번역 기록 DB 를 backup/ 에 복사 (sqlite backup API: WAL 이 있어도 일관됨).
    최근 keep 개만 남긴다. 반환: 백업 파일 경로 또는 빈 문자열."""
    import sqlite3
    import time
    if not os.path.exists(db_path):
        return ""
    bdir = os.path.join(os.path.dirname(db_path), "backup")
    os.makedirs(bdir, exist_ok=True)
    dest = os.path.join(bdir, "rpytl-%s.db" % time.strftime("%Y%m%d-%H%M%S"))
    src = sqlite3.connect(db_path, timeout=30)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    olds = sorted(f for f in os.listdir(bdir) if f.startswith("rpytl-") and f.endswith(".db"))
    for f in olds[:-keep]:
        os.remove(os.path.join(bdir, f))
    return dest


def review_summary(db_path, game_id):
    con = open_db(db_path)
    try:
        return _j(db.review_summary(con, game_id))
    finally:
        con.close()


def analyze(db_path, game_id, opts_json, control, bridge=None):
    """캐릭터 분석 (로컬 모델 또는 Gemini). 결과는 DB profiles 에 저장.
    opts 에 "gemini": {api_key, model, rpm} 가 있고 bridge 가 없으면 Gemini 로 분석한다."""
    from . import analyze as analyzer
    if bridge is None:
        g = (json.loads(opts_json or "{}")).get("gemini")
        if g:
            from .translators.gemini import GeminiClient
            bridge = GeminiClient(**{k: v for k, v in g.items() if k in ("api_key", "model", "rpm")})
            if control is not None:
                bridge.log = lambda m: control.log(m)
    return analyzer.run(db_path, game_id, bridge, opts_json, control)


def gemini_models(api_key):
    """키 확인 겸 쓸 수 있는 모델 이름 목록 (JSON 배열). 실패하면 예외 메시지에 사유가 있다."""
    from .translators.gemini import GeminiClient
    return _j(GeminiClient(api_key=api_key).list_models())


def characters(db_path, game_id, min_lines=3):
    """화면에 보여 줄 캐릭터 목록: 화자별 줄 수 + 분석 결과(없으면 빈 문자열). 작품 요약은 맨 앞."""
    con = open_db(db_path)
    try:
        prof = {p["name"]: p for p in db.list_profiles(con, game_id)}
        rows = []
        story = prof.pop(db.STORY_KEY, None)
        if story:
            rows.append({"name": db.STORY_KEY, "lines": story["lines"] or 0, "profile": story["profile"],
                         "auto": story["auto"]})
        for name, n in db.speaker_stats(con, game_id):
            if n >= min_lines or name in prof:
                p = prof.pop(name, None)
                rows.append({"name": name, "lines": n, "profile": p["profile"] if p else "",
                             "auto": p["auto"] if p else 1})
        return _j(rows[:80])
    finally:
        con.close()


def set_profile(db_path, game_id, name, profile):
    """사용자가 직접 고친 분석 (auto=0: 다시 분석해도 덮어쓰지 않음). 빈 문자열이면 삭제."""
    con = open_db(db_path)
    try:
        with db.tx(con):
            db.set_profile(con, game_id, name, profile, auto=0)
    finally:
        con.close()


def engine_counts(db_path, game_id):
    con = open_db(db_path)
    try:
        return _j(db.engine_counts(con, game_id))
    finally:
        con.close()


def reset_translations(db_path, game_id, engine=None):
    """번역 지우기. 지우기 전에 DB 를 백업한다. 반환: JSON {reset: 줄 수, backup: 경로}"""
    backup = backup_db(db_path)
    con = open_db(db_path)
    try:
        n = db.reset_translations(con, game_id, engine or None)
        return _j({"reset": n, "backup": backup})
    finally:
        con.close()
