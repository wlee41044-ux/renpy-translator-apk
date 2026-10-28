"""Ren'Py .rpa 아카이브 읽기 (unrpa 방식).

지원: RPA-2.0, RPA-3.0, RPA-3.2. 인덱스 피클은 클래스 로드를 전부 막은 언피클러로 읽는다.
스크립트 멤버(.rpy/.rpyc/.rpym/.rpymc)만 꺼내는 용도라 이미지 등은 읽지 않는다.
"""

import io
import os
import pickle
import zlib

SCRIPT_EXTS = (".rpy", ".rpyc", ".rpym", ".rpymc")


class RpaError(Exception):
    pass


def safe_codecs_encode(obj, encoding="utf-8", errors="strict"):
    """Python 3 의 protocol 2 피클은 bytes 를 _codecs.encode(str, 'latin1') 로 저장한다."""
    if not isinstance(obj, str) or encoding.lower().replace("-", "") not in ("latin1", "utf8", "ascii"):
        raise RpaError("unexpected _codecs.encode call")
    return obj.encode(encoding, errors)


def safe_bytes(*args):
    """b"" 는 protocol 2 에서 __builtin__.bytes() 호출로 저장된다. 정수 인자(거대 할당)는 거부."""
    if not args:
        return b""
    if len(args) == 1 and isinstance(args[0], (bytes, list, tuple)) and len(args[0]) < (1 << 24):
        return bytes(args[0])
    raise RpaError("unexpected bytes() call")


_ALLOWED = {
    ("_codecs", "encode"): safe_codecs_encode,
    ("__builtin__", "bytes"): safe_bytes,
    ("builtins", "bytes"): safe_bytes,
}


class _NoClassUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) in _ALLOWED:
            return _ALLOWED[(module, name)]
        raise RpaError("rpa index references a class (%s.%s); refusing" % (module, name))


def _fix_name(name):
    if isinstance(name, bytes):
        try:
            return name.decode("utf-8")
        except UnicodeDecodeError:
            return name.decode("latin-1")
    # Python 2 피클의 str 은 latin-1 로 풀었으므로 utf-8 로 재해석을 시도.
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _to_bytes(v):
    if v is None:
        return b""
    if isinstance(v, bytes):
        return v
    return v.encode("latin-1")


class RpaArchive:
    """f: seek 가능한 바이너리 파일 객체. 닫는 책임은 호출자."""

    def __init__(self, f):
        self.f = f
        self.version, self.index = self._read_index()

    def _read_index(self):
        f = self.f
        f.seek(0)
        header = f.readline(256)
        try:
            text = header.decode("ascii").strip()
        except UnicodeDecodeError:
            raise RpaError("not an rpa archive")
        parts = text.split()
        if not parts:
            raise RpaError("empty header")
        magic = parts[0]
        key = 0
        try:
            if magic == "RPA-3.0":
                offset = int(parts[1], 16)
                for sub in parts[2:]:
                    key ^= int(sub, 16)
            elif magic == "RPA-3.2":
                offset = int(parts[1], 16)
                for sub in parts[3:]:
                    key ^= int(sub, 16)
            elif magic == "RPA-2.0":
                offset = int(parts[1], 16)
            else:
                raise RpaError("unsupported archive format: %r" % magic[:16])
        except (IndexError, ValueError):
            raise RpaError("bad header: %r" % text[:80])

        f.seek(offset)
        try:
            raw = zlib.decompress(f.read())
            data = _NoClassUnpickler(io.BytesIO(raw), encoding="latin-1").load()
        except RpaError:
            raise
        except Exception as e:
            raise RpaError("cannot read index: %s" % e)
        if not isinstance(data, dict):
            raise RpaError("index is not a dict")

        index = {}
        for name, chunks in data.items():
            fixed = []
            for c in chunks:
                if len(c) == 2:
                    off, ln = c
                    prefix = b""
                else:
                    off, ln, prefix = c[0], c[1], c[2]
                fixed.append((off ^ key, ln ^ key, _to_bytes(prefix)))
            index[_fix_name(name)] = fixed
        return magic, index

    def names(self):
        return sorted(self.index)

    def read(self, name):
        out = []
        for off, ln, prefix in self.index[name]:
            self.f.seek(off)
            out.append(prefix)
            out.append(self.f.read(ln - len(prefix)))
        return b"".join(out)


def safe_join(root, name):
    """아카이브 멤버 이름을 root 아래 경로로. ../ 탈출, 절대경로 차단."""
    name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise RpaError("unsafe member name: %r" % name)
    return os.path.join(root, *parts)


def extract_scripts(f, dest, exts=SCRIPT_EXTS):
    """아카이브에서 스크립트만 dest 에 풀고 상대경로 목록을 반환."""
    arc = RpaArchive(f)
    done = []
    for name in arc.names():
        if not name.lower().endswith(exts):
            continue
        path = safe_join(dest, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as out:
            out.write(arc.read(name))
        done.append(name.replace("\\", "/"))
    return done


def write_rpa3(path, files, key=0x42424242):
    """테스트용 RPA-3.0 작성기. files: {name: bytes}"""
    with open(path, "wb") as f:
        f.write(b"RPA-3.0 %016x %08x\n" % (0, 0))
        index = {}
        for name, data in files.items():
            off = f.tell()
            f.write(data)
            index[name] = [(off ^ key, len(data) ^ key, b"")]
        idx_off = f.tell()
        f.write(zlib.compress(pickle.dumps(index, protocol=2)))
        f.seek(0)
        f.write(b"RPA-3.0 %016x %08x\n" % (idx_off, key))
