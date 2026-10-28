"""코드 안의 번역 표시 문자열 _("..."), __("...") 찾기."""

import ast
import re

# _( "..." ) / __( '...' ), 접두어 r/u 허용. 앞이 식별자 문자면 다른 함수 (예: foo_("x")) 이므로 제외.
_MARKED_RE = re.compile(
    r'(?<![\w.])__?\(\s*'
    r'((?:[rRuU])?(?:"""(?:[^\\]|\\.)*?"""|\'\'\'(?:[^\\]|\\.)*?\'\'\'|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'))'
    r'\s*\)',
    re.S)


def scan_marked_strings(src):
    if not src or "_(" not in src:
        return []
    out = []
    for m in _MARKED_RE.finditer(src):
        try:
            v = ast.literal_eval(m.group(1))
        except Exception:
            continue
        if isinstance(v, str) and v.strip():
            out.append(v)
    return out


# ---------------------------------------------------------------- 파이썬 코드 속 문자열 (아이템/스킬 설명 등)

import io  # noqa: E402
import tokenize  # noqa: E402
import unicodedata  # noqa: E402

_SKIP_EXT_RE = re.compile(
    r"\.(png|jpe?g|webp|gif|avif|svg|ogg|oga|mp3|wav|opus|flac|webm|mp4|ogv|avi|mkv|ttf|otf|ttc|"
    r"rpy|rpyc|rpa|py|txt|json|csv|xml|yaml|yml|ini|cfg|dat|bin|zip|ico|bmp)$", re.I)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATHISH_RE = re.compile(r"^[\w.\-]+(?:[/\\][\w.\- ]+)+$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def looks_translatable(s):
    """코드 속 문자열이 화면에 표시될 글(아이템 이름, 설명 등)처럼 보이는지.

    Ren'Py 는 표시되는 모든 글자를 old/new 와 정확히 비교하므로, 후보가 조금 넓어도 해롭지 않다
    (표시되지 않는 문자열은 그냥 안 쓰인다). 다만 번역할 양과 시간을 줄이기 위해 확실히 내부용인 것은 뺀다.
    """
    s = s.strip()
    if len(s) < 2 or len(s) > 2000:
        return False
    if not any(unicodedata.category(c)[0] == "L" for c in s):
        return False
    if _COLOR_RE.match(s) or _SKIP_EXT_RE.search(s) or _PATHISH_RE.match(s):
        return False
    if " " not in s and "\n" not in s and s.isascii():
        # 한 단어 ASCII: 변수명/키처럼 보이면 제외, 대문자로 시작하는 이름/라벨("Sword", "HP")은 포함
        if "_" in s or "." in s or not s[0].isupper():
            return False
        if _IDENT_RE.match(s) and s != s.capitalize() and not s.isupper():
            return False   # camelCase 식별자
    return True


def _literals_by_tokenize(code):
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(code).readline):
        if tok.type == tokenize.STRING:
            out.append(tok.string)
    return out


def _literals_by_regex(code):
    return [m.group(0) for m in re.finditer(
        r'(?:[rRuU]{0,2})(?:"""(?:[^\\]|\\.)*?"""|\'\'\'(?:[^\\]|\\.)*?\'\'\'|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\')',
        code, re.S)]


def python_literals(code):
    """파이썬 코드 한 조각에서 번역 후보 문자열(디코딩된 값)을 뽑는다. f-string/bytes 는 제외."""
    if not code or ('"' not in code and "'" not in code):
        return []
    try:
        raw = _literals_by_tokenize(code)
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        raw = _literals_by_regex(code)
    out = []
    for lit in raw:
        prefix = re.match(r"[A-Za-z]*", lit).group(0).lower()
        if "f" in prefix or "b" in prefix:
            continue
        try:
            v = ast.literal_eval(lit)
        except Exception:
            continue
        if isinstance(v, str) and looks_translatable(v):
            out.append(v)
    return out
