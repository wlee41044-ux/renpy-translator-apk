import io
import os
import pickle
import zlib

import pytest

from rpytl import rpa


def test_roundtrip_extracts_only_scripts(tmp_path):
    arc = tmp_path / "scripts.rpa"
    rpa.write_rpa3(str(arc), {
        "script.rpyc": b"RPYC-DATA",
        "sub/dir/ch1.rpy": b'label a:\n    "hi"\n',
        "images/bg.png": b"\x89PNG....",
    })
    out = tmp_path / "out"
    with open(arc, "rb") as f:
        names = rpa.extract_scripts(f, str(out))
    assert sorted(names) == ["script.rpyc", "sub/dir/ch1.rpy"]
    assert (out / "script.rpyc").read_bytes() == b"RPYC-DATA"
    assert not (out / "images").exists()


def test_rpa2_and_prefix(tmp_path):
    p = tmp_path / "a.rpa"
    body = b"HEADER-PLACEHOLDER-XXXXXXXXXXXXXX\nWORLD"
    idx_off = len(body)
    index = {"a.rpy": [(len(body) - 5, 5 + 5, b"HELLO")]}  # prefix + 5 bytes from file
    data = b"RPA-2.0 %016x\n" % idx_off
    data = data + body[len(data):]
    data += zlib.compress(pickle.dumps(index, protocol=2))
    p.write_bytes(data)
    with open(p, "rb") as f:
        a = rpa.RpaArchive(f)
        assert a.version == "RPA-2.0"
        assert a.read("a.rpy") == b"HELLOWORLD"


def test_rejects_path_traversal(tmp_path):
    arc = tmp_path / "evil.rpa"
    rpa.write_rpa3(str(arc), {"../../evil.rpy": b"x"})
    with open(arc, "rb") as f, pytest.raises(rpa.RpaError):
        rpa.extract_scripts(f, str(tmp_path / "out"))


def test_rejects_class_in_index(tmp_path):
    class Evil:
        def __reduce__(self):
            return (os.system, ("echo pwned",))
    p = tmp_path / "e.rpa"
    blob = zlib.compress(pickle.dumps({"a.rpy": [Evil()]}, protocol=2))
    p.write_bytes(b"RPA-3.0 %016x %08x\n" % (34, 0) + blob)
    with open(p, "rb") as f, pytest.raises(rpa.RpaError):
        rpa.RpaArchive(f)


def test_unknown_format():
    with pytest.raises(rpa.RpaError):
        rpa.RpaArchive(io.BytesIO(b"ZiX-12A 0000\n"))
