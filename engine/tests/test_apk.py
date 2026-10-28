import json
import os
import zipfile

import fakerenpy as f
from rpytl import apk, db, pipeline, rpa


def _rpa_bytes(tmp_path, files):
    p = tmp_path / "tmp.rpa"
    rpa.write_rpa3(str(p), files)
    return p.read_bytes()


def _make_apk(tmp_path, compress_rpa):
    rpyc = f.make_rpyc([
        f.Label(("game/script.rpy", 1), name="start", block=[], hide=False),
        f.TranslateSay(("game/script.rpy", 2), who="e", what="From the APK archive.",
                       identifier="start_a1b2c3d4", language=None),
    ])
    path = tmp_path / "base.apk"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", b"\x00")
        z.writestr("lib/arm64-v8a/librenpython.so", b"\x7fELF")
        z.writestr("assets/x-renpy/x-common/x-00start.rpy", "# common")
        z.writestr(zipfile.ZipInfo("assets/x-game/x-scripts.rpa"),
                   _rpa_bytes(tmp_path, {"script.rpyc": rpyc, "images/x.png": b"png"}),
                   compress_type=zipfile.ZIP_DEFLATED if compress_rpa else zipfile.ZIP_STORED)
        z.writestr("assets/x-game/x-extra/x-side.rpy", 'label side:\n    "Loose line."\n')
        z.writestr("assets/x-game/x-tl/x-korean/x-old.rpy",
                   'translate korean strings:\n    old "Start"\n    new "시작"\n')
        z.writestr("assets/x-game/x-tl/x-japanese/x-old.rpy", '# other language')
        z.writestr("assets/x-game/x-images/x-bg.png", b"png")
    split = tmp_path / "split_pack.apk"
    with zipfile.ZipFile(split, "w") as z:
        z.writestr("assets/game/pack.rpy", 'label pack:\n    "From asset pack."\n')
    return [str(path), str(split)]


def _check(tmp_path, compress):
    apks = _make_apk(tmp_path, compress)
    assert apk.is_renpy_apk(apks[0])
    work = tmp_path / "work"
    r = apk.extract_scripts(apks, str(work), language="korean")
    assert r["archives"] == 1
    assert sorted(r["names"]) == ["extra/side.rpy", "pack.rpy", "script.rpyc", "tl/korean/old.rpy"]
    assert not (work / "tl" / "japanese").exists()
    assert not (work / "images").exists()

    dbp = str(tmp_path / "t.db")
    gid = pipeline.add_game(dbp, "G", "apk:com.example.game")
    res = json.loads(pipeline.scan_game(dbp, gid, str(work)))
    assert res["errors"] == []
    con = db.connect(dbp)
    srcs = {t["source"] for t in db.pending_texts(con, gid, 100)}
    assert srcs == {"From the APK archive.", "Loose line.", "From asset pack."}


def test_extract_stored_rpa(tmp_path):
    _check(tmp_path, compress=False)


def test_extract_deflated_rpa(tmp_path):
    _check(tmp_path, compress=True)


def test_not_renpy(tmp_path):
    p = tmp_path / "other.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("classes.dex", b"dex")
    assert not apk.is_renpy_apk(str(p))
    assert not apk.is_renpy_apk(str(tmp_path / "missing.apk"))


def test_prefix_rules():
    assert apk._game_rel("assets/x-game/x-a/x-b.rpy") == "a/b.rpy"
    assert apk._game_rel("assets/x-game/a.rpy") is None
    assert apk._game_rel("assets/game/a/b.rpyc") == "a/b.rpyc"
    assert apk._game_rel("assets/x-renpy/x-common/x-a.rpy") is None
