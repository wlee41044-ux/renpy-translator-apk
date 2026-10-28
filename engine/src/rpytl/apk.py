"""설치된 Ren'Py 안드로이드 게임(APK)에서 스크립트 추출.

Ren'Py 안드로이드 빌드(rapt)는 game/ 을 APK 의 assets/x-game/ 에 넣고, 경로의 모든 요소 앞에
"x-" 를 붙인다 (aapt 가 일부 이름을 빼먹지 않게). Play 애셋 팩(split APK)은 assets/game/ 에
접두어 없이 들어간다 (renpy/loader.py find_apks / scandirfiles_from_apk 참고).

APK 파일(/data/app/.../base.apk)은 다른 앱도 권한 없이 읽을 수 있다.
"""

import io
import os
import struct
import zipfile

from . import rpa

SCRIPT_EXTS = (".rpy", ".rpyc", ".rpym", ".rpymc")
PREFIXED = "assets/x-game/"
PLAIN = "assets/game/"


def _game_rel(name):
    """APK 엔트리 이름 → game/ 기준 상대경로. 게임 파일이 아니면 None."""
    if name.startswith(PREFIXED):
        parts = name[len(PREFIXED):].split("/")
        if any(not p.startswith("x-") for p in parts if p):
            return None
        return "/".join(p[2:] for p in parts if p)
    if name.startswith(PLAIN):
        return name[len(PLAIN):]
    return None


def is_renpy_apk(path):
    try:
        with zipfile.ZipFile(path) as zf:
            for n in zf.namelist():
                if n.startswith(PREFIXED) or n.startswith("assets/x-renpy/") or n.startswith(PLAIN):
                    return True
    except (OSError, zipfile.BadZipFile):
        pass
    return False


class _StoredMember(io.RawIOBase):
    """압축 안 된(STORED) 엔트리를 APK 파일 위에서 바로 임의 접근 (큰 .rpa 용)."""

    def __init__(self, path, info):
        self.f = open(path, "rb")
        self.f.seek(info.header_offset)
        hdr = self.f.read(30)
        if hdr[:4] != b"PK\x03\x04":
            raise zipfile.BadZipFile("bad local header")
        nlen, xlen = struct.unpack("<HH", hdr[26:30])
        self.start = info.header_offset + 30 + nlen + xlen
        self.size = info.file_size
        self.pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, off, whence=0):
        if whence == 0:
            self.pos = off
        elif whence == 1:
            self.pos += off
        else:
            self.pos = self.size + off
        self.pos = max(0, min(self.pos, self.size))
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        self.f.seek(self.start + self.pos)
        data = self.f.read(n)
        b[:len(data)] = data
        self.pos += len(data)
        return len(data)

    def close(self):
        self.f.close()
        super().close()


def _open_member(path, zf, info):
    if info.compress_type == zipfile.ZIP_STORED:
        return io.BufferedReader(_StoredMember(path, info), buffer_size=1 << 16)
    return zf.open(info)  # ZipExtFile 도 seek 가능 (뒤로 가면 처음부터 다시 풀어서 느림)


def extract_scripts(apk_paths, dest, language=None, log=None):
    """APK 들에서 스크립트를 dest(=game/ 사본)로 추출.

    - 느슨한 .rpy/.rpyc (tl/ 은 지정한 언어만, 우리 rpytl/ 제외)
    - game/ 바로 아래의 .rpa 에서 스크립트 멤버
    순서: 아카이브 먼저, 그다음 느슨한 파일이 덮어씀 (Ren'Py 우선순위와 같음).
    반환: {"archives": n, "files": n, "names": [...]}
    """
    log = log or (lambda m: None)
    zfs = []
    try:
        return _extract(apk_paths, dest, language, log, zfs)
    finally:
        for zf in zfs:
            zf.close()


def _extract(apk_paths, dest, language, log, zfs):
    archives, loose = [], []
    for path in apk_paths:
        try:
            zf = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as e:
            log("APK 열기 실패: %s (%s)" % (path, e))
            continue
        zfs.append(zf)
        for info in zf.infolist():
            rel = _game_rel(info.filename)
            if not rel or info.is_dir():
                continue
            low = rel.lower()
            if low.endswith(".rpa") and "/" not in rel:
                archives.append((path, zf, info, rel))
            elif low.endswith(SCRIPT_EXTS):
                parts = low.split("/")
                if parts[0] == "tl":
                    if len(parts) < 3 or language is None or parts[1] != language.lower() \
                            or parts[2] == "rpytl":
                        continue
                loose.append((zf, info, rel))
    names = []
    for path, zf, info, rel in sorted(archives, key=lambda a: a[3]):
        log("아카이브 추출: %s (%dMB)" % (rel, info.file_size // (1 << 20)))
        with _open_member(path, zf, info) as f:
            got = rpa.extract_scripts(f, dest)
        log("  스크립트 %d개" % len(got))
        names.extend(got)
    for zf, info, rel in loose:
        out = rpa.safe_join(dest, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with zf.open(info) as src, open(out, "wb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        names.append(rel)
    return {"archives": len(archives), "files": len(loose), "names": names}
