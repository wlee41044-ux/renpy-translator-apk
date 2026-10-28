"""Ren'Py .rpy 텍스트 파서 (번역 대상만).

완전한 Ren'Py 파서는 아니다. 논리 줄(괄호/문자열이 열려 있으면 다음 줄까지 이어짐)과
들여쓰기 블록을 만든 뒤, 대사·메뉴·_() 문자열·screen 텍스트만 해석한다.
대사 식별자는 Ren'Py 의 Restructurer 규칙(라벨 + md5(code)[:8], 중복시 _1..)으로 계산한다.
"""

import re
from dataclasses import dataclass, field

from .model import DIALOGUE, STRING, IdentifierAllocator, Item, say_code
from .rpyc import _literal_string
from .strscan import scan_marked_strings


@dataclass
class Node:
    line: int
    indent: int
    text: str
    children: list = field(default_factory=list)


# ---------------------------------------------------------------- 렉서

def logical_lines(src):
    """[(줄번호, 들여쓰기, 텍스트)] — 주석 제거, 괄호/문자열이 열려 있으면 줄을 잇는다."""
    if src.startswith("﻿"):
        src = src[1:]
    src = src.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    i, n, line_no = 0, len(src), 1
    while i < n:
        start = line_no
        buf = []
        depth = 0
        while i < n:
            c = src[i]
            if c == "\n":
                line_no += 1
                i += 1
                if depth == 0:
                    break
                buf.append("\n")
                continue
            if c == "#":
                while i < n and src[i] != "\n":
                    i += 1
                continue
            if c in "\"'`":
                q3 = src[i:i + 3]
                if q3 == c * 3:
                    end = src.find(q3, i + 3)
                    while end != -1 and _escaped(src, end):
                        end = src.find(q3, end + 1)
                    end = n if end == -1 else end + 3
                else:
                    j = i + 1
                    while j < n and src[j] != c:
                        j += 2 if src[j] == "\\" else 1
                    end = min(j + 1, n)
                chunk = src[i:end]
                line_no += chunk.count("\n")
                buf.append(chunk)
                i = end
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth = max(0, depth - 1)
            buf.append(c)
            i += 1
        text = "".join(buf)
        if text.strip():
            stripped = text.lstrip(" \t")
            indent = len(text) - len(stripped)
            out.append((start, indent, stripped.rstrip()))
    return out


def _escaped(s, pos):
    k = 0
    while pos - 1 - k >= 0 and s[pos - 1 - k] == "\\":
        k += 1
    return k % 2 == 1


def build_tree(lines):
    root = Node(0, -1, "")
    stack = [root]
    for ln, ind, text in lines:
        while stack[-1].indent >= ind:
            stack.pop()
        node = Node(ln, ind, text)
        stack[-1].children.append(node)
        stack.append(node)
    return root.children


# ---------------------------------------------------------------- Ren'Py 문자열

_STR = r'''(?:[rR]?(?:"""(?:[^\\]|\\.)*?"""|\'\'\'(?:[^\\]|\\.)*?\'\'\'|```(?:[^\\]|\\.)*?```|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`))'''


def _dequote(m):
    c = m.group(1)
    if c == "{":
        return "{{"
    if c == "[":
        return "[["
    if c == "%":
        return "%%"
    if c == "n":
        return "\n"
    if c[0] == "u" and m.group(2):
        return chr(int(m.group(2), 16))
    return c


def renpy_triple_string(lit, delimiter="\n\n"):
    """lexer.triple_string(): 모놀로그 모드. [(줄 오프셋, 문단)] 또는 raw 면 문자열."""
    raw = lit[:1] in "rR"
    if raw:
        lit = lit[1:]
    s = lit[3:-3]
    if raw:
        return s
    s = re.sub(r" *\n *", "\n", s)
    sl = s.split(delimiter) if delimiter else [s]
    rv = []
    pos = 0
    for part in sl:
        leading_ws = len(part) - len(part.lstrip())
        line_offset = s.count("\n", 0, pos) + part.count("\n", 0, leading_ws)
        pos += len(part) + len(delimiter)
        part = part.strip()
        if not part:
            continue
        if delimiter:
            part = re.sub(r"[ \n]+", " ", part)
        else:
            part = re.sub(r" +", " ", part)
        part = re.sub(r"\\(u([0-9a-fA-F]{1,4})|.)", _dequote, part)
        rv.append((line_offset, part))
    return rv


def renpy_string(lit):
    """Ren'Py lexer.string() 과 같은 처리: 공백/줄바꿈 축약 후 역슬래시 해석."""
    raw = lit[:1] in "rR"
    if raw:
        lit = lit[1:]
    if lit[:3] in ('"""', "'''", "```"):
        s = lit[3:-3]
    else:
        s = lit[1:-1]
    if raw:
        return s
    s = re.sub(r"[ \n]+", " ", s)
    s = re.sub(r"\\(u([0-9a-fA-F]{1,4})|.)", _dequote, s)
    return s


# ---------------------------------------------------------------- 문장 해석

_KEYWORDS = {
    "label", "menu", "if", "elif", "else", "while", "for", "pass", "return", "jump", "call",
    "show", "scene", "hide", "with", "play", "queue", "stop", "pause", "window", "nvl",
    "voice", "python", "init", "define", "default", "image", "style", "transform", "screen",
    "translate", "camera", "layeredimage", "testcase", "rpy", "early", "on", "use", "key",
    "text", "textbutton", "add", "vbox", "hbox", "frame", "button", "imagebutton", "viewport",
    "fixed", "grid", "side", "null", "timer", "bar", "vbar", "input", "imagemap", "hotspot",
    "action", "at", "zorder", "modal", "tag", "variant", "style_prefix", "onlayer",
    "testsuite",
}

# 자식 블록이 Ren'Py 스크립트가 아닌 문장 (ATL, testcase 등)
_NON_SCRIPT_BLOCKS = {"show", "scene", "camera", "hide", "testcase", "testsuite", "layeredimage", "with",
                      "window", "pause", "play", "queue", "stop", "voice", "nvl", "return",
                      "jump", "call", "pass", "rpy", "translate", "on", "at"}

_WHO = r'(?P<who>[A-Za-z_]\w*(?:\.\w+)*|' + _STR + r')'
_SAY_RE = re.compile(
    r'^(?:' + _WHO + r'(?P<attrs>(?:\s+-?[A-Za-z_]\w*)*)(?P<tattrs>\s+@(?:\s+-?[A-Za-z_]\w*)+)?\s+)?'
    r'(?P<what>' + _STR + r')(?P<rest>.*)$', re.S)
_LABEL_RE = re.compile(r'^label\s+([\w.]+)(.*)$')
_CHOICE_RE = re.compile(r'^(?P<s>' + _STR + r')(?P<rest>.*):$', re.S)
_SCREEN_TEXT_RE = re.compile(r'^(?:text|textbutton|label)\s+(' + _STR + r')')
_CHAR_DEFINE_RE = re.compile(
    r'^define\s+([\w.]+)\s*=\s*(?:[\w.]+\.)?Character\(\s*(?:_\(\s*)?(' + _STR + r')')


def _parse_say(text):
    """say 문장이면 dict, 아니면 None."""
    first = re.match(r"[\w$]+", text)
    if first and first.group(0) in _KEYWORDS:
        return None
    if text.startswith("$"):
        return None
    m = _SAY_RE.match(text)
    if not m:
        return None
    who = m.group("who")
    attrs = m.group("attrs").split() if m.group("attrs") else []
    tattrs = m.group("tattrs").split()[1:] if m.group("tattrs") else []
    rest = m.group("rest").strip()
    if rest.endswith(":"):
        return None  # 메뉴 선택지 등
    # who 가 문자열이고 속성이 없으면 그 자체가 say 의 what 일 수도: "a" "b" 형식은 Ren'Py 도 who/what
    interact, ident, args, with_ = True, None, None, None
    while rest:
        if rest.startswith("("):
            depth, j = 0, 0
            for j, ch in enumerate(rest):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
            args = rest[:j + 1]
            rest = rest[j + 1:].strip()
            continue
        m2 = re.match(r"nointeract\b", rest)
        if m2:
            interact = False
            rest = rest[m2.end():].strip()
            continue
        m2 = re.match(r"id\s+(\w+)", rest)
        if m2:
            ident = m2.group(1)
            rest = rest[m2.end():].strip()
            continue
        m2 = re.match(r"with\s+(.+)$", rest, re.S)
        if m2:
            with_ = m2.group(1).strip()
            rest = ""
            continue
        return None  # 해석 못 하는 꼬리 = say 가 아닐 가능성 (사용자 정의 문장 등)
    lit = m.group("what")
    body = lit[1:] if lit[:1] in "rR" else lit
    if body[:3] in ('"""', "'''", "```"):
        what = renpy_triple_string(lit)
    else:
        what = renpy_string(lit)
    return {
        "who": who, "attrs": attrs, "tattrs": tattrs,
        "what": what,
        "interact": interact, "ident": ident, "args": args, "with_": with_,
    }


class RpyExtractor:
    def __init__(self, relpath, tlid_only_say=False, reserved=(), global_ids=None):
        self.file = relpath
        self.tlid_only_say = tlid_only_say
        self.items = []
        self.alloc = IdentifierAllocator(reserved, global_ids)
        self.global_label = None
        self.label = ""
        self.group = []  # say 앞의 번역 가능 문장 (voice 등)
        self.characters = {}

    def walk(self, nodes, in_screen=False):
        for node in nodes:
            self.stmt(node, in_screen)

    def _strings_in(self, node, in_screen):
        for s in scan_marked_strings(node.text):
            self._string(s, node.line)
        if in_screen:
            m = _SCREEN_TEXT_RE.match(node.text)
            if m:
                lit = _literal_string(m.group(1))
                if lit:
                    self._string(lit, node.line)

    def _deep_strings(self, node, in_screen):
        self._strings_in(node, in_screen)
        for c in node.children:
            self._deep_strings(c, in_screen)

    def _string(self, s, line):
        if s and s.strip():
            self.items.append(Item(kind=STRING, ident=s, source=s, file=self.file,
                                   line=line, label=self.label))

    def _say(self, node, say, interact=None):
        if interact is not None:
            say = dict(say, interact=interact)
        what = say["what"]
        if isinstance(what, str):
            self._one_say(node.line, say, what)
            return
        for off, part in what:  # 모놀로그 모드: 문단마다 별도 say
            if part == "{clear}":
                self.group.append("nvl clear")
                continue
            self._one_say(node.line + off, say, part)

    def _one_say(self, line, say, what):
        code = say_code(say["who"], what, say["attrs"], say["tattrs"], say["interact"],
                        say["args"], say["with_"], say["ident"])
        pre = list(self.group)
        self.group = []
        ident = self.alloc.allocate([code] if self.tlid_only_say else pre + [code],
                                    explicit_id=say["ident"])
        prefix = " ".join(([say["who"]] if say["who"] else []) + say["attrs"]
                          + (["@"] + say["tattrs"] if say["tattrs"] else []))
        post = []
        if not say["interact"]:
            post.append("nointeract")
        if say["args"]:
            post.append(say["args"])
        if say["with_"]:
            post += ["with", say["with_"]]
        speaker = say["who"] or ""
        self.items.append(Item(
            kind=DIALOGUE, ident=ident, source=what, file=self.file, line=line,
            speaker=self.characters.get(speaker, speaker), label=self.label,
            tmpl={"pre": pre, "who": prefix, "post": " ".join(post)}))

    def stmt(self, node, in_screen=False):
        t = node.text
        word = re.match(r"[\w$]*", t).group(0)

        if in_screen:
            self._strings_in(node, True)
            self.walk(node.children, True)
            return

        if word == "translate":
            return
        if word == "label":
            self.group = []
            m = _LABEL_RE.match(t)
            if m:
                name = m.group(1)
                hide = bool(re.search(r"\bhide\b", m.group(2)))
                if name.startswith("."):
                    name = (self.global_label or "") + name
                elif "." not in name:
                    self.global_label = name
                self.alloc.on_label(name, hide)
                if not name.startswith("_"):
                    self.label = name
            self.walk(node.children)
            return
        if word == "screen":
            self.group = []
            self.walk(node.children, True)
            return
        if word in ("python", "init", "define", "default", "style", "transform", "image") \
                or t.startswith("$"):
            self.group = []
            m = _CHAR_DEFINE_RE.match(t)
            if m:
                self.characters[m.group(1)] = renpy_string(m.group(2))
            if word == "init" and t.rstrip().endswith(":") and not re.search(r"\bpython\b", t):
                self.walk(node.children)
            else:
                self._deep_strings(node, False)
            return
        if word == "menu":
            self.group = []
            m = re.match(r"menu\s+([\w.]+)", t)
            if m:  # menu 이름: → 라벨로 취급 (Ren'Py 는 Label 노드를 만든다)
                name = m.group(1)
                if name.startswith("."):
                    name = (self.global_label or "") + name
                self.alloc.on_label(name)
                if not name.startswith("_"):
                    self.label = name
            self._menu(node)
            return
        if word in ("voice",) or t.startswith("nvl clear"):
            self.group.append(t)
            return
        if word in ("if", "elif", "else", "while", "for"):
            self.group = []
            self._strings_in(node, False)
            self.walk(node.children)
            return

        say = _parse_say(t) if not node.children else None
        if say is not None:
            self._say(node, say)
            return
        self.group = []
        if word in _NON_SCRIPT_BLOCKS:
            # ATL/테스트 블록: 안의 문자열은 대사가 아님 (displayable 이름 등)
            self._deep_strings(node, False)
            return
        self._strings_in(node, False)
        self.walk(node.children)  # 사용자 정의 문장(CDS)의 블록은 스크립트로 취급

    def _menu(self, node):
        for child in node.children:
            t = child.text
            if t.startswith("set "):
                continue
            m = _CHOICE_RE.match(t)
            if m and child.text.endswith(":"):
                self._string(renpy_string(m.group("s")), child.line)
                self.walk(child.children)
                continue
            say = _parse_say(t)
            if say is not None and say["who"]:
                self._say(child, say, interact=False)
                continue
            m = re.match(r"^(" + _STR + r")\s*$", t)
            if m:
                self._string(renpy_string(m.group(1)), child.line)  # 캡션
                continue
            self._strings_in(child, False)


def _explicit_ids(nodes, out):
    for n in nodes:
        if not n.children:
            say = _parse_say(n.text)
            if say and say["ident"]:
                out.add(say["ident"])
        _explicit_ids(n.children, out)
    return out


def extract_from_rpy(src, relpath, tlid_only_say=False, global_ids=None):
    """tlid_only_say: Ren'Py 8.6+ 기본값(config.tlid_only_considers_say=True).
    True 면 voice 등 say 이외 문장이 식별자 계산에서 빠진다."""
    tree = build_tree(logical_lines(src))
    ex = RpyExtractor(relpath, tlid_only_say, _explicit_ids(tree, set()), global_ids)
    # 화자 이름 표시용: define 을 먼저 훑는다
    for node in tree:
        m = _CHAR_DEFINE_RE.match(node.text)
        if m:
            ex.characters[m.group(1)] = renpy_string(m.group(2))
    ex.walk(tree)
    return ex.items


_TLID_CONFIG_RE = re.compile(r"^\s*(?:define\s+)?config\.tlid_only_considers_say\s*=\s*(True|False)", re.M)


def tlid_config_override(src):
    """스크립트가 config.tlid_only_considers_say 를 직접 설정했으면 그 값."""
    m = _TLID_CONFIG_RE.search(src)
    return None if m is None else m.group(1) == "True"
