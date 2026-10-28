"""테스트용 가짜 renpy 모듈. 실제 Ren'Py 가 만드는 피클 구조를 흉내 내서 .rpyc 를 만든다."""

import copyreg
import pickle
import struct
import sys
import types
import zlib


def _module(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


ast = _module("fake_renpy_ast")
astsupport = _module("fake_renpy_astsupport")
slast = _module("fake_renpy_sl2_slast")
ui = _module("fake_renpy_ui")


class Node:
    def __init__(self, loc, **kw):
        self.filename, self.linenumber = loc
        self.__dict__.update(kw)


def _cls(mod, name, base=Node, **extra):
    c = type(name, (base,), dict(extra))
    c.__module__ = mod.__name__
    c.__qualname__ = name
    setattr(mod, name, c)
    return c


Say = _cls(ast, "Say")
Translate = _cls(ast, "Translate")
Label = _cls(ast, "Label")
Menu = _cls(ast, "Menu")
UserStatement = _cls(ast, "UserStatement")
Init = _cls(ast, "Init")
Define = _cls(ast, "Define")
Screen = _cls(ast, "Screen")
If = _cls(ast, "If")


class _CSlotNode(Node):
    """Ren'Py 8.4+ cslots 처럼 (copyreg.__newobj__, (cls,), (None, slots)) 로 피클된다."""

    def __reduce_ex__(self, protocol):
        return (copyreg.__newobj__, (type(self),), (None, dict(self.__dict__)))


TranslateSay = _cls(ast, "TranslateSay", _CSlotNode)


class PyExpr(str):
    def __new__(cls, s, filename="x.rpy", linenumber=1):
        o = str.__new__(cls, s)
        o.filename, o.linenumber = filename, linenumber
        return o

    def __reduce__(self):
        return (PyExpr, (str(self), self.filename, self.linenumber, 3, 0, 0))


PyExpr.__module__ = astsupport.__name__
astsupport.PyExpr = PyExpr


class PyCode:
    def __init__(self, source):
        self.source = source

    def __getstate__(self):
        return (1, self.source, ("x.rpy", 1), "exec", 3, 0, 0)

    def __setstate__(self, st):
        self.source = st[1]


PyCode.__module__ = ast.__name__
ast.PyCode = PyCode

class _Plain:
    def __init__(self, **kw):
        self.__dict__.update(kw)


SLScreen = _cls(slast, "SLScreen", _Plain)
SLDisplayable = _cls(slast, "SLDisplayable", _Plain)


def _fn(mod, name):
    f = lambda *a, **k: None  # noqa: E731
    f.__module__ = mod.__name__
    f.__name__ = f.__qualname__ = name
    setattr(mod, name, f)
    return f


Text = _fn(ui, "Text")
_textbutton = _fn(ui, "_textbutton")


def rename_modules(data):
    """피클 바이트 안의 fake 모듈 이름을 진짜 renpy 모듈 이름으로 바꾼다."""
    for a, b in ((b"fake_renpy_sl2_slast", b"renpy.sl2.slast"),
                 (b"fake_renpy_astsupport", b"renpy.astsupport"),
                 (b"fake_renpy_ast", b"renpy.ast"),
                 (b"fake_renpy_ui", b"renpy.ui")):
        data = _replace_name(data, a, b)
    return data


def _replace_name(data, a, b):
    # 프로토콜 2 GLOBAL opcode 는 "c<module>\n<name>\n" 텍스트라서 길이가 달라도 안전하게 치환 가능
    return data.replace(b"c" + a + b"\n", b"c" + b + b"\n")


def make_rpyc(stmts, rpc2=True):
    data = pickle.dumps(({"version": 5003000, "key": "x"}, stmts), protocol=2)
    data = rename_modules(data)
    blob = zlib.compress(data)
    if not rpc2:
        return blob
    header = b"RENPY RPC2"
    start = len(header) + 12 * 2
    return header + struct.pack("<III", 1, start, len(blob)) + struct.pack("<III", 0, 0, 0) + blob
