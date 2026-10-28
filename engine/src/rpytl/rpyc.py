"""Ren'Py .rpyc 로드 + AST 에서 번역 항목 추출 (unrpyc 방식).

Ren'Py 를 import 하지 않는다. 피클 안의 renpy.* 클래스는 전부 동작 없는 가짜 클래스로
만들어지므로 .rpyc 를 읽는 것만으로는 어떤 코드도 실행되지 않는다.
전체 소스 복원(완전한 디컴파일) 대신 번역에 필요한 노드만 해석한다:
  Translate / TranslateSay  → 대사 (Ren'Py 가 계산해 둔 식별자를 그대로 사용)
  Say (Translate 밖)        → 옛 버전 rpyc, 식별자를 직접 계산
  Menu                       → 선택지 문자열
  PyExpr/PyCode 소스의 _()   → UI 문자열
  screen 의 text/textbutton/label 리터럴 → UI 문자열
"""

import ast as pyast
import pickle
import struct
import zlib

from .rpa import safe_bytes, safe_codecs_encode
from .model import DIALOGUE, STRING, IdentifierAllocator, Item, say_code
from .strscan import scan_marked_strings


class RpycError(Exception):
    pass


# ---------------------------------------------------------------- 가짜 클래스

class FakeBase:
    _fake_module = ""
    _fake_name = ""

    def __new__(cls, *args, **kwargs):
        obj = object.__new__(cls)
        obj.__dict__["_args"] = args
        return obj

    def __init__(self, *args, **kwargs):
        pass

    def __setstate__(self, state):
        _apply_state(self, state)

    # 리스트/딕트처럼 피클된 객체(APPENDS/SETITEMS)도 받아준다.
    def append(self, v):
        self.__dict__.setdefault("_items", []).append(v)

    def extend(self, vs):
        self.__dict__.setdefault("_items", []).extend(vs)

    def __setitem__(self, k, v):
        self.__dict__.setdefault("_map", {})[k] = v

    def __repr__(self):
        return "<fake %s.%s>" % (self._fake_module, self._fake_name)


def _apply_state(obj, state):
    if isinstance(state, tuple) and len(state) == 2 and \
            (state[0] is None or isinstance(state[0], dict)) and \
            (state[1] is None or isinstance(state[1], dict)):
        for part in state:
            if part:
                for k, v in part.items():
                    obj.__dict__[_key(k)] = v
    elif isinstance(state, dict):
        for k, v in state.items():
            obj.__dict__[_key(k)] = v
    else:
        obj.__dict__["_state"] = state


def _key(k):
    return k.decode("latin-1") if isinstance(k, bytes) else str(k)


class FakeStr(str):
    """PyExpr 같은 str 서브클래스."""

    def __new__(cls, s="", *args, **kwargs):
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        return str.__new__(cls, s)

    def __init__(self, *args, **kwargs):
        pass

    def __setstate__(self, state):
        _apply_state(self, state)


class FakeList(list):
    def __setstate__(self, state):
        _apply_state(self, state)


class FakeDict(dict):
    def __setstate__(self, state):
        _apply_state(self, state)


class FakeSet(set):
    def __setstate__(self, state):
        _apply_state(self, state)


_fake_cache = {}


def _fake_class(module, name):
    key = (module, name)
    cls = _fake_cache.get(key)
    if cls is None:
        if name in ("PyExpr",):
            base = FakeStr
        elif name.endswith("List"):
            base = FakeList
        elif name.endswith("Dict"):
            base = FakeDict
        elif name.endswith("Set"):
            base = FakeSet
        else:
            base = FakeBase
        cls = type(str(name), (base,), {"_fake_module": module, "_fake_name": name})
        _fake_cache[key] = cls
    return cls


def _safe_newobj(cls, *args):
    return cls.__new__(cls, *args)


def _safe_newobj_ex(cls, args, kwargs):
    return cls.__new__(cls, *args, **kwargs)


def _safe_reconstructor(cls, base, state):
    if base is object or not isinstance(base, type):
        obj = cls.__new__(cls)
    else:
        obj = base.__new__(cls, state)
    return obj


_SAFE = {
    ("builtins", "set"): set, ("__builtin__", "set"): set,
    ("builtins", "frozenset"): frozenset, ("__builtin__", "frozenset"): frozenset,
    ("builtins", "list"): list, ("__builtin__", "list"): list,
    ("builtins", "dict"): dict, ("__builtin__", "dict"): dict,
    ("builtins", "tuple"): tuple, ("__builtin__", "tuple"): tuple,
    ("builtins", "object"): object, ("__builtin__", "object"): object,
    ("builtins", "str"): str, ("__builtin__", "unicode"): str, ("__builtin__", "str"): str,
    ("builtins", "bytes"): safe_bytes, ("__builtin__", "bytes"): safe_bytes,
    ("builtins", "int"): int, ("__builtin__", "int"): int, ("__builtin__", "long"): int,
    ("builtins", "float"): float, ("__builtin__", "float"): float,
    ("builtins", "bool"): bool, ("__builtin__", "bool"): bool,
    ("collections", "OrderedDict"): dict,
    ("collections", "defaultdict"): FakeDict,
    ("copy_reg", "_reconstructor"): _safe_reconstructor,
    ("copyreg", "_reconstructor"): _safe_reconstructor,
    # Ren'Py 8.4+ cslots 객체는 (copyreg.__newobj__, (cls,), (None, slots)) 로 피클된다
    ("copyreg", "__newobj__"): _safe_newobj, ("copy_reg", "__newobj__"): _safe_newobj,
    ("copyreg", "__newobj_ex__"): _safe_newobj_ex,
    ("_codecs", "encode"): safe_codecs_encode,
}


class SafeUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        real = _SAFE.get((module, name))
        if real is not None:
            return real
        return _fake_class(module, name)


# ---------------------------------------------------------------- 파일 로드

def load_rpyc_bytes(data):
    """(data, stmts) 반환. RPC2(슬롯 형식)와 옛 zlib 단일 형식 지원."""
    if data[:10] == b"RENPY RPC2":
        pos = 10
        chunks = {}
        while pos + 12 <= len(data):
            slot, start, length = struct.unpack("<III", data[pos:pos + 12])
            if slot == 0:
                break
            chunks[slot] = data[start:start + length]
            pos += 12
        if 1 not in chunks:
            raise RpycError("no slot 1 in rpyc")
        payload = chunks[1]
    else:
        payload = data
    try:
        raw = zlib.decompress(payload)
    except zlib.error as e:
        raise RpycError("zlib: %s (obfuscated or unsupported rpyc?)" % e)
    try:
        obj = SafeUnpickler(_BytesIO(raw), encoding="latin-1").load()
    except Exception as e:
        raise RpycError("unpickle: %s" % e)
    if isinstance(obj, tuple) and len(obj) == 2:
        return obj
    raise RpycError("unexpected rpyc payload")


def _BytesIO(b):
    import io
    return io.BytesIO(b)


# ---------------------------------------------------------------- AST 해석

def _cls(node):
    return getattr(type(node), "_fake_name", type(node).__name__)


def _get(node, name, default=None):
    d = getattr(node, "__dict__", None)
    if d is None:
        return default
    return d.get(name, default)


def _text(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    s = str(v)
    # Python 2 피클의 bytes str 을 latin-1 로 풀었을 수 있으므로 utf-8 재해석 시도
    if any(0x80 <= ord(c) <= 0xff for c in s):
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def _args_code(arginfo):
    """renpy.ast.ArgumentInfo.get_code() 근사."""
    if arginfo is None:
        return None
    args = _get(arginfo, "arguments") or []
    starred = set(_get(arginfo, "starred_indexes") or ())
    dstarred = set(_get(arginfo, "doublestarred_indexes") or ())
    parts = []
    for i, a in enumerate(args):
        name, expr = a[0], _text(a[1])
        if i in starred:
            parts.append("*" + expr)
        elif i in dstarred:
            parts.append("**" + expr)
        elif name is None:
            parts.append(expr)
        else:
            parts.append("%s=%s" % (_text(name), expr))
    # 옛 형식 (extrapos/extrakw)
    ep, ek = _get(arginfo, "extrapos"), _get(arginfo, "extrakw")
    if ep:
        parts.append("*" + _text(ep))
    if ek:
        parts.append("**" + _text(ek))
    return "(" + ", ".join(parts) + ")"


def _say_parts(node):
    who = _text(_get(node, "who"))
    what = _text(_get(node, "what")) or ""
    attrs = [_text(a) for a in (_get(node, "attributes") or ())]
    tattrs = [_text(a) for a in (_get(node, "temporary_attributes") or ())]
    interact = _get(node, "interact", True)
    args = _args_code(_get(node, "arguments"))
    with_ = _text(_get(node, "with_"))
    return who, what, attrs, tattrs, interact, args, with_


def _say_prefix(who, attrs, tattrs):
    rv = []
    if who:
        rv.append(who)
    rv.extend(attrs)
    if tattrs:
        rv.append("@")
        rv.extend(tattrs)
    return " ".join(rv)


def _say_suffix(interact, args, with_):
    rv = []
    if not interact:
        rv.append("nointeract")
    if args:
        rv.append(args)
    if with_:
        rv.append("with")
        rv.append(with_)
    return " ".join(rv)


def _stmt_code(node):
    """번역 블록 안의 say 이외 문장 (예: voice "a.ogg")."""
    c = _cls(node)
    if c == "UserStatement":
        return _text(_get(node, "line"))
    if c in ("Say", "TranslateSay"):
        who, what, attrs, tattrs, interact, args, with_ = _say_parts(node)
        return say_code(who, what, attrs, tattrs, interact, args, with_)
    return None


_TEXT_DISPLAYABLES = ("Text", "_text", "_textbutton", "_label", "textbutton", "text", "label")


class Extractor:
    def __init__(self, relpath, global_ids=None):
        self.file = relpath
        self.items = []
        self.global_ids = global_ids if global_ids is not None else set()
        self.alloc = IdentifierAllocator(global_ids=self.global_ids)
        self.label = ""
        self.seen_nodes = set()

    # -- 대사
    def _dialogue(self, ident, block, node_for_loc):
        self.global_ids.add(ident)
        says = [n for n in block if _cls(n) in ("Say", "TranslateSay")]
        if len(says) != 1:
            return  # say 가 없거나 여러 개인 블록은 드묾: 건너뜀
        say = says[0]
        pre = []
        for n in block:
            if n is say:
                break
            code = _stmt_code(n)
            if code is None:
                return
            pre.append(code)
        who, what, attrs, tattrs, interact, args, with_ = _say_parts(say)
        self.items.append(Item(
            kind=DIALOGUE, ident=ident, source=what, file=self.file,
            line=int(_get(node_for_loc, "linenumber", 0) or 0),
            speaker=who or "", label=self.label,
            tmpl={"pre": pre, "who": _say_prefix(who, attrs, tattrs),
                  "post": _say_suffix(interact, args, with_)}))

    def _string(self, s, node):
        s = _text(s)
        if s is None or not s.strip():
            return
        self.items.append(Item(kind=STRING, ident=s, source=s, file=self.file,
                               line=int(_get(node, "linenumber", 0) or 0), label=self.label))

    # -- 순회
    def walk_block(self, block):
        if not isinstance(block, (list, tuple)):
            return
        for node in block:
            self.walk_node(node)

    def walk_node(self, node):
        if id(node) in self.seen_nodes:
            return
        self.seen_nodes.add(id(node))
        c = _cls(node)

        if c == "Label":
            # Ren'Py 8.4+ 는 name 을 _name 으로 저장하기도 한다 (unrpyc renpycompat 참고)
            name = _text(_get(node, "name") or _get(node, "_name")) or ""
            self.alloc.on_label(name, bool(_get(node, "hide", False)))
            if not name.startswith("_"):
                self.label = name
            self.walk_block(_get(node, "block"))
        elif c == "TranslateSay":
            if _get(node, "language") is None:
                self._dialogue(_text(_get(node, "identifier")), [node], node)
        elif c == "Translate":
            if _get(node, "language") is None:
                self._dialogue(_text(_get(node, "identifier")), _get(node, "block") or [], node)
                # 블록 안 메뉴 등은 없지만 혹시 모를 중첩 대비
                for n in _get(node, "block") or []:
                    if _cls(n) not in ("Say", "TranslateSay", "UserStatement"):
                        self.walk_node(n)
        elif c == "Say":
            # Translate 로 감싸지지 않은 say = 옛 Ren'Py. 식별자 직접 계산.
            code = _stmt_code(node)
            ident = self.alloc.allocate([code])
            self._dialogue(ident, [node], node)
        elif c == "Menu":
            for item in _get(node, "items") or []:
                if isinstance(item, (list, tuple)) and item:
                    self._string(item[0], node)
                    if len(item) > 2:
                        self.walk_block(item[2])
        elif c in ("If",):
            for entry in _get(node, "entries") or []:
                if isinstance(entry, (list, tuple)) and len(entry) > 1:
                    self.walk_block(entry[1])
        elif c in ("TranslateString", "TranslateBlock", "TranslateEarlyBlock",
                   "TranslatePython"):
            return  # 기존 번역 파일 내용은 원문이 아님
        else:
            for attr in ("block", "children"):
                self.walk_block(_get(node, attr))
            entries = _get(node, "entries")
            if isinstance(entries, list):
                for e in entries:
                    if isinstance(e, (list, tuple)) and len(e) > 1:
                        self.walk_block(e[1])


def _scan_code_strings(root, extractor):
    """그래프 전체에서 코드 문자열(PyExpr, PyCode 소스)의 _() 와 screen 텍스트 리터럴을 찾는다."""
    stack = [root]
    seen = set()
    found = []
    while stack:
        o = stack.pop()
        if id(o) in seen:
            continue
        seen.add(id(o))
        if isinstance(o, FakeStr):
            found.extend(scan_marked_strings(_text(o)))
            continue
        if isinstance(o, str):
            continue
        if isinstance(o, (list, tuple, set, frozenset)):
            stack.extend(o)
            continue
        if isinstance(o, dict):
            stack.extend(o.values())
            continue
        d = getattr(o, "__dict__", None)
        if d is None:
            continue
        c = _cls(o)
        if c == "PyCode":
            st = d.get("_state")
            if isinstance(st, tuple) and len(st) > 1 and isinstance(st[1], str):
                found.extend(scan_marked_strings(_text(st[1])))
        elif c == "SLDisplayable":
            disp = d.get("displayable")
            dname = getattr(disp, "_fake_name", getattr(disp, "__name__", ""))
            pos = d.get("positional") or []
            if dname in _TEXT_DISPLAYABLES and pos:
                lit = _literal_string(_text(pos[0]))
                if lit:
                    found.append(lit)
        stack.extend(v for k, v in d.items() if k not in ("what",))
    for s in found:
        extractor._string(s, None)


def _literal_string(src):
    if not src:
        return None
    try:
        v = pyast.literal_eval(src.strip())
    except Exception:
        return None
    return v if isinstance(v, str) else None


def extract_from_rpyc(data, relpath, global_ids=None):
    """rpyc 바이트 → [Item]"""
    _, stmts = load_rpyc_bytes(data)
    ex = Extractor(relpath, global_ids)
    ex.walk_block(stmts)
    _scan_code_strings(stmts, ex)
    return ex.items
