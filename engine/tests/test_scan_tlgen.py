import json
import os
import shutil

from rpytl import db, pipeline, rpa, scan, tlgen

DATA = os.path.join(os.path.dirname(__file__), "data")


def _setup(tmp_path, version="game_v1"):
    game = tmp_path / "work" / "game"
    shutil.copytree(os.path.join(DATA, version), game)
    dbp = str(tmp_path / "t.db")
    gid = pipeline.add_game(dbp, "Test", "content://x/tree/game")
    return dbp, gid, str(game)


def _translate_all(con, gid, prefix="KO:"):
    for t in db.pending_texts(con, gid, 10000):
        db.set_translation(con, gid, t["src_hash"], prefix + t["source"], "manual")


def test_scan_counts_and_resume(tmp_path):
    dbp, gid, game = _setup(tmp_path)
    r = json.loads(pipeline.scan_game(dbp, gid, game))
    assert r["errors"] == []
    assert r["counts"]["added"] == r["items"] == 10 + 3 + 5
    assert r["stats"]["total"] == 18 and r["stats"]["pending"] == 18
    # 다시 스캔해도 변화 없음
    r2 = json.loads(pipeline.scan_game(dbp, gid, game))
    assert r2["counts"] == {"added": 0, "changed": 0, "removed": 0, "unchanged": 18,
                            "reactivated": 0}


def test_update_diff_keeps_translations_and_history(tmp_path):
    dbp, gid, game = _setup(tmp_path)
    pipeline.scan_game(dbp, gid, game)
    con = db.connect(dbp)
    _translate_all(con, gid)
    assert db.stats(con, gid)["done"] == 18

    shutil.rmtree(game)
    shutil.copytree(os.path.join(DATA, "game_v2"), game)
    r = json.loads(pipeline.scan_game(dbp, gid, game))
    c = r["counts"]
    # coffee 줄 변경, "Let's go." 삭제, 새 줄 1개 추가
    assert c["changed"] == 1
    assert c["removed"] == 1
    assert c["added"] == 1
    assert c["unchanged"] == 16

    s = db.stats(con, gid)
    assert s["unused"] == 1
    assert s["pending"] == 2          # 변경 1 + 신규 1 만 다시 번역
    assert s["done"] == 16

    h = db.history(con, gid)
    assert len(h) == 1
    assert h[0]["old_source"] == "I made {b}coffee{/b} for you."
    assert h[0]["new_source"] == "I made {b}fresh coffee{/b} for you."
    assert h[0]["old_translation"] == "KO:I made {b}coffee{/b} for you."

    # 삭제된 줄의 번역은 보존: v1 으로 되돌리면 재활성화되고 다시 번역 안 함
    shutil.rmtree(game)
    shutil.copytree(os.path.join(DATA, "game_v1"), game)
    r = json.loads(pipeline.scan_game(dbp, gid, game))
    assert r["counts"]["reactivated"] == 1
    s = db.stats(con, gid)
    assert s["unused"] == 1  # v2 의 새 줄
    assert s["pending"] == 0  # coffee 원문 번역은 원문 해시로 남아 있음


def test_same_source_translated_once(tmp_path):
    game = tmp_path / "g"
    game.mkdir()
    (game / "a.rpy").write_text('label a:\n    "Same."\n    "Same."\n', encoding="utf-8")
    dbp = str(tmp_path / "t.db")
    gid = pipeline.add_game(dbp, "T", "u")
    pipeline.scan_game(dbp, gid, str(game))
    con = db.connect(dbp)
    assert len(db.pending_texts(con, gid, 100)) == 1
    assert db.stats(con, gid)["total"] == 2


def test_generate_tl_files(tmp_path):
    dbp, gid, game = _setup(tmp_path)
    pipeline.scan_game(dbp, gid, game)
    con = db.connect(dbp)
    _translate_all(con, gid)
    # 하나는 검수 필요로
    row = con.execute("SELECT src_hash FROM texts WHERE source = 'Suit yourself.'").fetchone()
    db.set_translation(con, gid, row[0], "???", "google", status=db.REVIEW)
    # 기존 tl 파일(게임 동봉)에 이미 있는 문자열/식별자는 제외해야 함
    ex = tmp_path / "existing" / "korean"
    ex.mkdir(parents=True)
    ident = con.execute("SELECT ident FROM entries WHERE kind='dialogue' AND line = 11").fetchone()[0]
    (ex / "common.rpy").write_text(
        'translate korean strings:\n    old "Alice"\n    new "앨리스"\n\n'
        'translate korean %s:\n    "이미 있음"\n' % ident, encoding="utf-8")

    out = tmp_path / "out"
    res = json.loads(pipeline.export(dbp, gid, str(out), font="tl/korean/rpytl/font.ttf",
                                     existing_tl_dir=str(ex)))
    assert sorted(res["files"]) == ["tl/korean/rpytl/_language.rpy",
                                    "tl/korean/rpytl/_strings.rpy",
                                    "tl/korean/rpytl/script.rpy"]
    assert res["info"]["skipped_existing"] == 2

    script = (out / "tl/korean/rpytl/script.rpy").read_text(encoding="utf-8")
    assert "translate korean %s:" % ident not in script
    assert '    voice "a_003.ogg"\n    # a "Do you want some?"\n    a "KO:Do you want some?"' in script
    assert '    a happy "KO:I made {b}coffee{/b} for you."' in script
    assert '"KO:Hey, it\'s %(time)s already.\\nWe\'re late!"' in script
    assert '    a "KO:Thanks for playing!" with dissolve' in script
    assert '    "Bob" "KO:Let\'s go."' in script
    assert "Suit yourself" not in script.split("# a \"Suit yourself.\"")[-1].split("\n")[1:2][0] \
        if "# a \"Suit yourself.\"" in script else True
    assert "KO:Suit yourself." not in script and '"???"' not in script

    strings = (out / "tl/korean/rpytl/_strings.rpy").read_text(encoding="utf-8")
    assert 'old "Alice"' not in strings
    assert 'old "Yes, please."\n    new "KO:Yes, please."' in strings
    assert strings.count("translate korean strings:") == 1

    lang = (out / "tl/korean/rpytl/_language.rpy").read_text(encoding="utf-8")
    assert "config.default_language = 'korean'" in lang
    assert "_rpytl_font_path = 'tl/korean/rpytl/font.ttf'" in lang      # 기본: 한글만 한글 폰트
    assert "translate korean style" not in lang
    res2 = json.loads(pipeline.export(dbp, gid, str(tmp_path / "out2"), font="tl/korean/rpytl/font.ttf",
                                      font_mode="replace"))
    lang2 = (tmp_path / "out2" / "tl/korean/rpytl/_language.rpy").read_text(encoding="utf-8")
    assert 'translate korean style default:\n    font "tl/korean/rpytl/font.ttf"' in lang2

    # 생성 파일은 실제 Ren'Py 문법으로 다시 읽혀야 한다: 우리 파서로 들여쓰기/문자열이 깨지지 않았는지
    from rpytl.rpyparse import logical_lines
    for p in res["files"]:
        logical_lines((out / p).read_text(encoding="utf-8"))


def test_language_setting(tmp_path):
    dbp, gid, game = _setup(tmp_path)
    pipeline.scan_game(dbp, gid, game)
    con = db.connect(dbp)
    _translate_all(con, gid)
    db.set_language(con, gid, "japanese")
    res = json.loads(pipeline.export(dbp, gid, str(tmp_path / "o")))
    assert all(p.startswith("tl/japanese/rpytl/") for p in res["files"])


def test_pipeline_with_archive_and_rpyc(tmp_path):
    import fakerenpy as f
    say = f.TranslateSay(("game/s.rpy", 3), who="e", what="From archive.",
                         identifier="start_0badc0de", language=None)
    rpyc_bytes = f.make_rpyc([f.Label(("game/s.rpy", 2), name="start", block=[], hide=False), say])
    arc = tmp_path / "archive.rpa"
    rpa.write_rpa3(str(arc), {"s.rpyc": rpyc_bytes, "loose.rpyc": b"broken", "bg.jpg": b"x"})
    work = tmp_path / "w"
    pipeline.reset_workdir(str(work))
    fd = os.open(str(arc), os.O_RDONLY)
    try:
        names = json.loads(pipeline.extract_archive_fd(fd, str(work)))
        os.fstat(fd)  # 호출자 fd 는 열린 채로 남아야 함
    finally:
        os.close(fd)
    assert sorted(names) == ["loose.rpyc", "s.rpyc"]
    # 느슨한 .rpy 가 아카이브의 .rpyc 를 이긴다
    (work / "loose.rpy").write_text('label l:\n    "Loose wins."\n', encoding="utf-8")
    dbp = str(tmp_path / "t.db")
    gid = pipeline.add_game(dbp, "T", "u")
    r = json.loads(pipeline.scan_game(dbp, gid, str(work)))
    assert r["errors"] == []
    con = db.connect(dbp)
    srcs = {t["source"] for t in db.pending_texts(con, gid, 100)}
    assert srcs == {"From archive.", "Loose wins."}


def test_broken_file_reported_not_fatal(tmp_path):
    game = tmp_path / "g"
    game.mkdir()
    (game / "bad.rpyc").write_bytes(b"RENPY RPC2garbage")
    (game / "ok.rpy").write_text('label a:\n    "ok"\n', encoding="utf-8")
    items, files, errors = scan.collect_items(str(game))
    assert len(errors) == 1 and errors[0].startswith("bad.rpyc")
    assert [i.source for i in items] == ["ok"]


def test_tl_dir_skipped(tmp_path):
    game = tmp_path / "g"
    (game / "tl" / "korean").mkdir(parents=True)
    (game / "tl" / "korean" / "x.rpy").write_text('label q:\n    "should not appear"\n')
    (game / "a.rpy").write_text('label a:\n    "ok"\n')
    items, _, _ = scan.collect_items(str(game))
    assert [i.source for i in items] == ["ok"]


def test_existing_tl_from_rpyc(tmp_path):
    import fakerenpy as f
    TS = f._cls(f.ast, "TranslateString")
    stmts = [f.Translate(("tl/korean/a.rpy", 1), identifier="start_11111111", language="korean",
                         block=[]),
             TS(("tl/korean/a.rpy", 5), language="korean", old="Start", new="시작"),
             TS(("tl/korean/a.rpy", 6), language="japanese", old="Quit", new="x")]
    d = tmp_path / "korean"
    d.mkdir()
    (d / "a.rpyc").write_bytes(f.make_rpyc(stmts))
    ids, strs = tlgen.scan_existing_tl(str(d), "korean")
    assert ids == {"start_11111111"} and strs == {"Start"}


def test_backup_db_keeps_records_and_rotates(tmp_path):
    dbp, gid, game = _setup(tmp_path)
    pipeline.scan_game(dbp, gid, game)
    con = db.connect(dbp)
    _translate_all(con, gid)
    import time
    paths = []
    for _ in range(5):
        paths.append(pipeline.backup_db(dbp, keep=3))
        time.sleep(1.1)
    left = sorted(os.listdir(os.path.join(os.path.dirname(dbp), "backup")))
    assert len(left) == 3
    con2 = db.connect(os.path.join(os.path.dirname(dbp), "backup", left[-1]))
    assert db.stats(con2, gid)["done"] == 18
    assert pipeline.backup_db(str(tmp_path / "none.db")) == ""
