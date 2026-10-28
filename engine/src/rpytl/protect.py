"""번역 전 태그/변수 보호와 번역 후 복원.

보호 대상: Ren'Py 텍스트 태그 {i} {/i} {color=#f00} {w=0.5} ..., 이스케이프 {{ [[,
보간 [name] [obj.attr!t], 포맷 %s %d %(x)s %%, 줄바꿈.
붙어 있는 보호 토큰은 하나의 플레이스홀더로 묶는다 ({i}{b} → 하나).

플레이스홀더는 ZQ<n>Z 형태(번역기가 그대로 두기 쉬운 무의미한 라틴 문자+숫자).
복원 시 번역기가 넣은 공백/대소문자/전각 변형을 허용하고, 개수가 정확히 맞지 않으면 실패.
"""

import re
import unicodedata

_TOKEN_RE = re.compile(
    r"\{\{|\[\["                                          # 이스케이프된 괄호
    r"|\{[^{}]*\}"                                         # 텍스트 태그
    r"|\[[^\[\]]*\]"                                       # 보간
    r"|%\([^()]*\)[-#0+]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]" # %(name)s
    r"|%[-#0+]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]"            # %s %d %.2f
    r"|%%"
    r"|\n"
)

_PH_FMT = "ZQ%dZ"
# 복원용: 공백 끼어듦, 소문자, 전각 문자 허용
_PH_RE = re.compile(r"[ZzＺｚ]\s*[QqＱｑ]\s*([0-9０-９]+)\s*[ZzＺｚ]")


class Protected:
    __slots__ = ("masked", "tokens")

    def __init__(self, masked, tokens):
        self.masked = masked
        self.tokens = tokens

    @property
    def translatable(self):
        """보호 토큰/공백/기호만 남으면 번역할 필요 없음."""
        rest = _PH_RE.sub("", self.masked)
        return any(unicodedata.category(c)[0] in "LN" for c in rest)


def protect(text):
    tokens = []
    out = []
    pos = 0
    last_end = None
    for m in _TOKEN_RE.finditer(text):
        if m.start() > pos:
            out.append(text[pos:m.start()])
        if last_end == m.start() and tokens:
            tokens[-1] += m.group(0)  # 바로 붙은 토큰은 합친다
        else:
            tokens.append(m.group(0))
            out.append(_PH_FMT % (len(tokens) - 1))
        pos = last_end = m.end()
    out.append(text[pos:])
    return Protected("".join(out), tokens)


class RestoreError(ValueError):
    pass


def restore(translated, tokens):
    """플레이스홀더를 원래 토큰으로. 개수/번호가 안 맞으면 RestoreError."""
    found = []

    def sub(m):
        idx = int(unicodedata.normalize("NFKC", m.group(1)))
        found.append(idx)
        if idx >= len(tokens):
            raise RestoreError("unknown placeholder %d" % idx)
        return "\x00%d\x00" % idx

    tmp = _PH_RE.sub(sub, translated)
    if sorted(found) != list(range(len(tokens))):
        raise RestoreError("placeholders mismatch: expected %d, got %r" % (len(tokens), found))
    # 줄바꿈 토큰 주변에 번역기가 붙인 공백 제거
    out = re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], tmp)
    out = re.sub(r"[ \t]*\n[ \t]*", "\n", out)
    return out


_BRACKETS = "[]{}%"


def structure_problem(source, translation):
    """번역문이 원문과 같은 Ren'Py 태그/변수 구조인지 검사. 문제가 있으면 사유 문자열, 없으면 None.

    Ren'Py 는 번역문 안의 [..] 를 코드로 실행하고 {..} 를 텍스트 태그로 해석하므로, 구조가 달라진 번역은
    번역이 안 되는 정도가 아니라 게임을 멈추게 한다 (예: 원문 "[[Victory] ..." → 번역 "[승리] ...").
    1) 보호 토큰(태그, [변수], %s, [[ 등)의 종류와 개수가 같아야 하고
    2) 토큰을 뺀 나머지의 괄호 글자 개수도 같아야 한다.
    """
    a = sorted(m.group(0) for m in _TOKEN_RE.finditer(source))
    b = sorted(m.group(0) for m in _TOKEN_RE.finditer(translation))
    if a != b:
        only_src = [t for t in a if t not in b][:3]
        only_tr = [t for t in b if t not in a][:3]
        return "태그/변수 구조 불일치 (원문에만 %r, 번역에만 %r)" % (only_src, only_tr)
    rs = _TOKEN_RE.sub("", source)
    rt = _TOKEN_RE.sub("", translation)
    for ch in _BRACKETS:
        if rs.count(ch) != rt.count(ch):
            return "괄호 %r 개수 불일치 (원문 %d, 번역 %d)" % (ch, rs.count(ch), rt.count(ch))
    return None


def check_translation(source, translation):
    """번역문이 원문과 같은 태그/변수 구조인지 (수동 번역 검증 등)."""
    return structure_problem(source, translation) is None
