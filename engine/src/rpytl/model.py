"""엔진 공통 자료형과 Ren'Py 문자열 인코딩."""

import hashlib
import re
from dataclasses import dataclass, field

DIALOGUE = "dialogue"
STRING = "string"


@dataclass
class Item:
    """번역 대상 한 줄.

    dialogue: ident = translate 식별자, tmpl = tl 블록 재구성 정보
              {"pre": [번역 블록 안의 say 앞 문장들], "who": "e happy", "post": "with dissolve"}
    string:   ident = 원문 문자열 그대로 (translate strings 의 old)
    """

    kind: str
    ident: str
    source: str
    file: str
    line: int
    speaker: str = ""
    label: str = ""
    tmpl: dict = field(default_factory=dict)
    seq: int = 0     # 파일 내 순서 (scan 에서 채움)
    hash: str = ""   # src_hash(source) (scan 에서 채움)


def src_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def encode_say_string(s):
    """renpy.translation.encode_say_string 과 같은 규칙으로 문자열 리터럴을 만든다."""
    s = s.replace("\\", "\\\\")
    s = s.replace("\n", "\\n")
    s = s.replace('"', '\\"')
    s = re.sub(r"(?<= ) ", "\\ ", s)
    return '"' + s + '"'


def say_code(who, what, attributes=None, temporary_attributes=None,
             interact=True, arguments=None, with_=None, identifier=None):
    """renpy.ast.Say.get_code() 재현 (식별자 md5 계산과 tl 출력에 사용)."""
    rv = []
    if who:
        rv.append(who)
    if attributes:
        rv.extend(attributes)
    if temporary_attributes:
        rv.append("@")
        rv.extend(temporary_attributes)
    rv.append(encode_say_string(what))
    if not interact:
        rv.append("nointeract")
    if identifier:
        rv.append("id")
        rv.append(identifier)
    if arguments:
        rv.append(arguments)
    if with_:
        rv.append("with")
        rv.append(with_)
    return " ".join(rv)


def translate_digest(codes):
    md5 = hashlib.md5()
    for code in codes:
        md5.update((code + "\r\n").encode("utf-8"))
    return md5.hexdigest()[:8]


class IdentifierAllocator:
    """renpy.translation.Restructurer 의 label/alternate/unique_identifier 규칙."""

    def __init__(self, reserved=(), global_ids=None):
        self.label = None
        self.alternate = None
        self.used = set()
        self.reserved = set(reserved)  # 파일 안의 명시적 id 들 (preexisting_identifiers)
        # 먼저 로드된 파일들의 식별자 (Ren'Py 의 translator.default_translates)
        self.global_ids = global_ids if global_ids is not None else set()

    def on_label(self, name, hide=False):
        if hide:
            return
        if name.startswith("_"):
            self.alternate = name
        else:
            self.label = name
            self.alternate = None

    def _unique(self, label, digest):
        base = digest if label is None else label.replace(".", "_") + "_" + digest
        ident, i = base, 0
        while ident in self.used or ident in self.reserved or ident in self.global_ids:
            i += 1
            ident = "%s_%d" % (base, i)
        return ident

    def allocate(self, codes, explicit_id=None):
        """codes: 다이제스트에 들어갈 문장 코드들. explicit_id: say 의 id 절."""
        digest = translate_digest(codes)
        ident = self._unique(self.label, digest)
        if self.alternate is not None:
            alternate = self._unique(self.alternate, digest)
            ident = explicit_id or ident
        elif explicit_id is not None:
            alternate = ident
            ident = explicit_id
        else:
            alternate = None
        self.used.add(ident)
        self.global_ids.add(ident)
        if alternate is not None:
            self.used.add(alternate)
        return ident
