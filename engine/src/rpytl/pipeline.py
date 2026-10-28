"""Kotlin(Chaquopy) 에서 부르는 진입점. 인자/반환은 단순 타입과 JSON 문자열."""

import json
import os
import shutil

from . import db, rpa, scan, tlgen
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


def scan_game(db_path, game_id, work_game_dir, renpy_version_text=None):
    """renpy_version_text: 게임의 renpy/vc_version.py 또는 renpy/__init__.py 내용 (없으면 None)."""
    ver = parse_renpy_version(renpy_version_text)
    items, files, errors = scan.collect_items(work_game_dir, tlid_only_considers_say(ver))
    con = open_db(db_path)
    try:
        counts = scan.apply_scan(con, game_id, items)
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


def export(db_path, game_id, out_dir, font=None, force_language=True, existing_tl_dir=None):
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    con = open_db(db_path)
    try:
        written, info = tlgen.generate(con, game_id, out_dir, font=font or None,
                                       force_language=bool(force_language),
                                       existing_tl_dir=existing_tl_dir or None)
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
