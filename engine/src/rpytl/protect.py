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
    r"|%\([^()]*\)[-#0 +]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]" # %(name)s
    r"|%[-#0 +]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]"            # %s %d %.2f
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


def check_translation(source, translation):
    """번역문이 원문의 보호 토큰을 같은 개수로 가지고 있는지 (수동 번역 검증 등)."""
    a = sorted(m.group(0) for m in _TOKEN_RE.finditer(source))
    b = sorted(m.group(0) for m in _TOKEN_RE.finditer(translation))
    return a == b
