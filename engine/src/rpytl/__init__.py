"""Ren'Py 번역 엔진 (순수 Python, Android/Chaquopy 와 PC 양쪽에서 동작)."""

import sys

__version__ = "0.1.0"


def engine_info():
    return "rpytl %s / Python %s" % (__version__, sys.version.split()[0])
