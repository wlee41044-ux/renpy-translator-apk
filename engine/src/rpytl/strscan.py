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
