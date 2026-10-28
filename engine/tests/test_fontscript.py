"""생성되는 _language.rpy 의 한글 폰트 대체 코드를 가짜 renpy 모듈 위에서 실제로 실행해 본다."""

import sys
import textwrap
import types

from rpytl import tlgen

FONT = "tl/korean/rpytl/font.ttf"


class FontGroup:
    """renpy.text.font.FontGroup 의 add/segment 동작을 그대로 옮긴 것 (Ren'Py 소스 기준)."""

    def __init__(self):
        self.map = {}
        self.char_map = {}

    def add(self, font, start, end=None):
        if start is None:
            if None not in self.map:
                self.map[None] = font
            return self
        for i in range(start, end + 1):
            if i not in self.map:
                self.map[i] = font
        return self

    def segment(self, s):
        mark = pos = 0
        font = old = None
        for c in s:
            n = ord(c)
            if n == 32 and font:
                pos += 1
                continue
            font = self.map.get(n)
            if font is None:
                font = self.map.get(None)
                if font is None:
                    raise Exception("Character U+%04x not found in FontGroup" % n)
            if font != old:
                if pos:
                    yield old, s[mark:pos]
                old = font
                mark = pos
            pos += 1
        if font is not None:
            yield font, s[mark:]


class TextSegment:
    def __init__(self, source=None):
        self.font = source.font if source is not None else None

    def subsegment(self, s):
        tf = self.font
        if not isinstance(tf, FontGroup):
            yield (self, s)
            return
        segs = {}
        for f, ss in tf.segment(s):
            seg = segs.get(f)
            if seg is None:
                seg = TextSegment(self)
                seg.font = f
                segs[f] = seg
            yield seg, ss


def _install(monkeypatch, script):
    renpy = types.ModuleType("renpy")
    text_pkg = types.ModuleType("renpy.text")
    text_mod = types.ModuleType("renpy.text.text")
    font_mod = types.ModuleType("renpy.text.font")
    text_mod.TextSegment = TextSegment
    font_mod.FontGroup = FontGroup
    renpy.text, text_pkg.text, text_pkg.font = text_pkg, text_mod, font_mod
    for name, mod in (("renpy", renpy), ("renpy.text", text_pkg), ("renpy.text.text", text_mod),
                      ("renpy.text.font", font_mod)):
        monkeypatch.setitem(sys.modules, name, mod)
    # init 1000 python: 블록의 본문을 꺼내 실행
    body = script.split("init 1000 python:\n", 1)[1]
    ns = {}
    exec(textwrap.dedent(body), ns)
    return ns


def _fallback_script():
    return tlgen.language_script("korean", FONT, True, "fallback")


def test_hangul_uses_korean_font_and_latin_keeps_original(monkeypatch):
    _install(monkeypatch, _fallback_script())
    seg = TextSegment()
    seg.font = "fancy.ttf"
    parts = [(s.font, t) for s, t in seg.subsegment("Hello 안녕하세요 World!")]
    assert parts == [("fancy.ttf", "Hello "), (FONT, "안녕하세요 "), ("fancy.ttf", "World!")] or \
        parts == [("fancy.ttf", "Hello "), (FONT, "안녕하세요"), ("fancy.ttf", " World!")]
    # 글자가 빠지거나 중복되지 않는다
    assert "".join(t for _, t in parts) == "Hello 안녕하세요 World!"


def test_text_without_hangul_is_untouched(monkeypatch):
    _install(monkeypatch, _fallback_script())
    seg = TextSegment()
    seg.font = "fancy.ttf"
    assert [(s.font, t) for s, t in seg.subsegment("Start Game")] == [("fancy.ttf", "Start Game")]


def test_already_korean_font_and_groups_are_not_wrapped(monkeypatch):
    _install(monkeypatch, _fallback_script())
    seg = TextSegment()
    seg.font = FONT
    assert [(s.font, t) for s, t in seg.subsegment("안녕")] == [(FONT, "안녕")]
    g = FontGroup().add("a.ttf", None)
    seg2 = TextSegment()
    seg2.font = g
    assert [t for _, t in seg2.subsegment("안녕")] == ["안녕"]


def test_failure_falls_back_to_original(monkeypatch):
    _install(monkeypatch, _fallback_script())

    def boom(self, s):
        raise RuntimeError("renpy internals changed")

    monkeypatch.setattr(FontGroup, "segment", boom)
    seg = TextSegment()
    seg.font = "fancy.ttf"
    # 대체 로직이 실패해도 원래 동작(글자 그대로, 폰트 그대로)으로 돌아간다
    assert [(s.font, t) for s, t in seg.subsegment("안녕")] == [("fancy.ttf", "안녕")]


def test_missing_renpy_modules_do_not_break_game_start(monkeypatch):
    """Ren'Py 내부 구조가 다른 버전이어도 임포트 실패는 조용히 무시되어야 한다."""
    monkeypatch.setitem(sys.modules, "renpy", None)   # import 하면 ImportError
    body = _fallback_script().split("init 1000 python:\n", 1)[1]
    exec(textwrap.dedent(body), {})                   # 예외 없이 끝나야 함


def test_script_is_valid_python_for_both_modes():
    for mode in ("fallback", "replace"):
        script = tlgen.language_script("korean", FONT, True, mode)
        assert FONT in script
        body = script.split("init 1000 python:\n", 1)[1] if "init 1000" in script else "pass"
        compile(textwrap.dedent(body), "x", "exec")
    assert "translate korean style default" in tlgen.language_script("korean", FONT, True, "replace")
    assert "translate korean style" not in tlgen.language_script("korean", FONT, True, "fallback")
    assert "init 1000" not in tlgen.language_script("korean", None, True, "fallback")
