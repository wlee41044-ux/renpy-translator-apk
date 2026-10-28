"""game/tl/<언어>/ 번역 파일 생성.

출력은 전부 tl/<언어>/rpytl/ 아래에 둔다 (우리 파일만 모아서 통째로 교체/삭제 가능).
Ren'Py 는 같은 식별자/같은 old 문자열 번역이 두 번 있으면 시작 시 오류를 내므로
- 우리 출력 안에서 중복 제거
- 게임에 이미 있는 tl/<언어>/ 파일(우리 것 제외)의 식별자/문자열은 제외한다.
"""

import json
import os
import re

from . import db
from .model import DIALOGUE, STRING, encode_say_string

OUR_DIR = "rpytl"
HEADER = "# rpytl 이 생성한 파일입니다. 직접 수정하면 다음 생성 때 덮어써집니다.\n"

_TL_ID_RE = re.compile(r"^\s*translate\s+(\w+)\s+(\w+)\s*:", re.M)
_TL_OLD_RE = re.compile(r'^\s*old\s+("(?:[^"\\]|\\.)*")', re.M)


def scan_existing_tl(tl_lang_dir, language):
    """기존 tl/<언어> 파일(우리 rpytl/ 제외)에 이미 있는 식별자와 old 문자열."""
    from .rpyparse import renpy_string
    idents, strings = set(), set()
    if not tl_lang_dir or not os.path.isdir(tl_lang_dir):
        return idents, strings
    for root, dirs, files in os.walk(tl_lang_dir):
        dirs[:] = [d for d in dirs if d != OUR_DIR]
        names = set(files)
        for name in files:
            path = os.path.join(root, name)
            low = name.lower()
            if low.endswith(".rpy"):
                with open(path, "rb") as f:
                    src = f.read().decode("utf-8", "replace")
                for m in _TL_ID_RE.finditer(src):
                    if m.group(1) == language and m.group(2) not in ("strings", "python", "style"):
                        idents.add(m.group(2))
                for m in _TL_OLD_RE.finditer(src):
                    strings.add(renpy_string(m.group(1)))
            elif low.endswith(".rpyc") and name[:-1] not in names:
                # 아카이브 안에 컴파일본만 있는 기존 번역
                try:
                    with open(path, "rb") as f:
                        i2, s2 = _existing_from_rpyc(f.read(), language)
                    idents |= i2
                    strings |= s2
                except Exception:
                    pass
    return idents, strings


def _existing_from_rpyc(data, language):
    from .rpyc import _cls, _get, _text, load_rpyc_bytes
    _, stmts = load_rpyc_bytes(data)
    idents, strings = set(), set()
    stack, seen = list(stmts), set()
    while stack:
        n = stack.pop()
        if id(n) in seen or not hasattr(n, "__dict__"):
            continue
        seen.add(id(n))
        c = _cls(n)
        if c in ("Translate", "TranslateSay") and _text(_get(n, "language")) == language:
            idents.add(_text(_get(n, "identifier")))
        elif c == "TranslateString" and _text(_get(n, "language")) == language:
            strings.add(_text(_get(n, "old")))
        for attr in ("block", "children"):
            v = _get(n, attr)
            if isinstance(v, list):
                stack.extend(v)
    return idents, strings


def _out_name(relfile):
    base = relfile
    for ext in (".rpymc", ".rpyc", ".rpym", ".rpy"):
        if base.lower().endswith(ext):
            base = base[:-len(ext)]
            break
    return base + ".rpy"


def _say_line(tmpl, text):
    parts = []
    if tmpl.get("who"):
        parts.append(tmpl["who"])
    parts.append(encode_say_string(text))
    if tmpl.get("post"):
        parts.append(tmpl["post"])
    return " ".join(parts)


def generate(con, game_id, out_dir, language=None, font=None, force_language=True,
             existing_tl_dir=None, font_mode="fallback"):
    """out_dir 에 tl/<lang>/rpytl/*.rpy 생성. 반환: (game/ 기준 상대경로 목록, 통계)"""
    game = db.get_game(con, game_id)
    language = language or game["language"]
    if not re.match(r"^\w+$", language):
        raise ValueError("language must be an identifier: %r" % language)
    ex_ids, ex_strs = scan_existing_tl(existing_tl_dir, language)

    by_file = {}
    strings = []
    seen_str = set(ex_strs)
    skipped = 0
    for e in db.entries_for_output(con, game_id):
        if e["status"] != db.DONE or e["translation"] is None:
            continue
        if e["translation"] == e["source"]:
            continue  # 번역할 게 없던 줄 (태그/변수만 있음): 원문 그대로 두면 됨
        if e["kind"] == DIALOGUE:
            if e["ident"] in ex_ids:
                skipped += 1
                continue
            by_file.setdefault(e["file"], []).append(e)
        elif e["kind"] == STRING:
            if e["source"] in seen_str:
                skipped += 1
                continue
            seen_str.add(e["source"])
            strings.append(e)

    base = os.path.join(out_dir, "tl", language, OUR_DIR)
    written = []

    def write(rel, text):
        path = os.path.join(base, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
        written.append("tl/%s/%s/%s" % (language, OUR_DIR, rel))

    for relfile in sorted(by_file):
        out = [HEADER]
        for e in by_file[relfile]:
            tmpl = json.loads(e["tmpl"] or "{}")
            out.append("# game/%s:%d\n" % (relfile, e["line"] or 0))
            out.append("translate %s %s:\n\n" % (language, e["ident"]))
            for pre in tmpl.get("pre", []):
                out.append("    %s\n" % pre)
            out.append("    # %s\n" % _say_line(tmpl, e["source"]).replace("\n", " "))
            out.append("    %s\n\n" % _say_line(tmpl, e["translation"]))
        write(_out_name(relfile), "".join(out))

    if strings:
        out = [HEADER, "translate %s strings:\n\n" % language]
        for e in strings:
            out.append("    # game/%s:%d\n" % (e["file"], e["line"] or 0))
            out.append("    old %s\n" % encode_say_string(e["source"]))
            out.append("    new %s\n\n" % encode_say_string(e["translation"]))
        write("_strings.rpy", "".join(out))

    write("_language.rpy", language_script(language, font, force_language, font_mode))
    return written, {"dialogue_files": len(by_file), "strings": len(strings),
                     "skipped_existing": skipped}


FONT_STYLES = ("default", "say_dialogue", "say_label", "say_thought", "button_text",
               "choice_button_text", "label_text", "prompt_text", "input", "hyperlink_text")


# 글자 범위: 한글 자모, 호환 자모, 자모 확장-A, 한글 음절, 자모 확장-B
HANGUL_RANGES = ((0x1100, 0x11FF), (0x3130, 0x318F), (0xA960, 0xA97F), (0xAC00, 0xD7FF))

FALLBACK_TEMPLATE = """
# 한글 글자만 한글 폰트로 그리고, 영문/숫자/기호는 게임 원래 폰트를 그대로 쓴다.
# (화려한 폰트나 타이틀 화면 폰트에 한글 글리프가 없어서 글자가 안 보이는 문제 해결)
# Ren'Py 내부(TextSegment.subsegment)를 감싸므로 실패하면 조용히 원래 동작으로 돌아간다.
init 1000 python:
    try:
        import renpy.text.text as _rpytl_text
        import renpy.text.font as _rpytl_font

        _rpytl_font_path = %(font)r
        _rpytl_hangul = %(ranges)r
        _rpytl_groups = {}
        _rpytl_orig_sub = _rpytl_text.TextSegment.subsegment

        def _rpytl_has_hangul(s):
            for c in s:
                n = ord(c)
                for a, b in _rpytl_hangul:
                    if a <= n <= b:
                        return True
            return False

        def _rpytl_subsegment(self, s):
            try:
                tf = self.font
                if (isinstance(tf, _rpytl_font.FontGroup) or tf == _rpytl_font_path
                        or not _rpytl_has_hangul(s)):
                    return list(_rpytl_orig_sub(self, s))
                g = _rpytl_groups.get(tf)
                if g is None:
                    g = _rpytl_font.FontGroup()
                    for a, b in _rpytl_hangul:
                        g.add(_rpytl_font_path, a, b)
                    g.add(tf, None)
                    _rpytl_groups[tf] = g
                segs = {}
                out = []
                for f, ss in g.segment(s):
                    seg = segs.get(f)
                    if seg is None:
                        seg = _rpytl_text.TextSegment(self)
                        seg.font = f
                        segs[f] = seg
                    out.append((seg, ss))
                return out
            except Exception:
                return list(_rpytl_orig_sub(self, s))

        _rpytl_text.TextSegment.subsegment = _rpytl_subsegment
    except Exception:
        pass
"""


def language_script(language, font=None, force_language=True, font_mode="fallback"):
    """font_mode: fallback = 한글만 한글 폰트 (기본), replace = 주요 스타일의 폰트를 통째로 교체"""
    lines = [HEADER, "\n"]
    lines.append("init 999 python:\n")
    lines.append("    try:\n")
    lines.append("        config.default_language = %r\n" % language)
    if force_language:
        lines.append("        config.language = %r\n" % language)
    lines.append("    except Exception:\n        pass\n\n")
    if font and font_mode == "fallback":
        lines.append(FALLBACK_TEMPLATE % {"font": font, "ranges": HANGUL_RANGES})
    elif font:
        # font: game/ 기준 경로 (예: tl/korean/rpytl/font.ttf)
        lines.append("translate %s python:\n" % language)
        lines.append("    try:\n")
        for var in ("text_font", "name_text_font", "interface_text_font",
                    "button_text_font", "choice_button_text_font"):
            lines.append("        if hasattr(gui, %r): gui.%s = %r\n" % (var, var, font))
        lines.append("    except Exception:\n        pass\n\n")
        for st in FONT_STYLES:
            lines.append("translate %s style %s:\n    font %s\n\n"
                         % (language, st, encode_say_string(font)))
    return "".join(lines)
