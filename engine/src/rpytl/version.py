"""게임에 들어 있는 Ren'Py 버전 감지 (renpy/vc_version.py 또는 renpy/__init__.py)."""

import re


def parse_renpy_version(text):
    """(major, minor, patch) 또는 None."""
    if not text:
        return None
    m = re.search(r"""^\s*version\s*=\s*['"](\d+)\.(\d+)\.(\d+)""", text, re.M)
    if not m:
        m = re.search(r"version_tuple\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", text)
    if not m:
        m = re.search(r"""(?:version|vc_version)\s*=\s*['"]Ren'Py\s+(\d+)\.(\d+)\.(\d+)""", text)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def tlid_only_considers_say(version):
    """config.tlid_only_considers_say 기본값: Ren'Py 8.6 부터 True."""
    if version is None:
        return False  # 대부분의 기존 게임은 8.6 미만
    return tuple(version[:2]) >= (8, 6)
