"""로컬 LLM 번역 (llama.cpp GGUF, 폰 안에서 실행).

실제 추론은 Kotlin/JNI 브리지(LlamaLlm)가 한다.
bridge: chat(system: str, user: str, max_tokens: int) -> str

- 여러 줄을 번호 목록으로 한 번에 보내고, 번호가 전부 맞게 돌아오지 않으면 한 줄씩 다시 보낸다.
- runner 가 meta 를 넘기면 화자 이름, 앞 대사(문맥), 캐릭터 분석 결과(self.profiles), 작품 요약(self.story)을
  프롬프트에 붙여 말투와 호칭이 캐릭터마다 일관되게 한다.
"""

import re
import time

from .base import Stopped, Translator, TranslatorError, sleep_interruptible

LANG_NAMES = {
    "ko": "Korean", "ja": "Japanese", "en": "English", "zh-CN": "Simplified Chinese",
    "zh-TW": "Traditional Chinese", "es": "Spanish", "fr": "French", "de": "German",
    "ru": "Russian", "pt": "Portuguese", "it": "Italian", "vi": "Vietnamese", "th": "Thai",
    "id": "Indonesian", "pl": "Polish", "tr": "Turkish", "uk": "Ukrainian", "ar": "Arabic",
}

SYSTEM = (
    "You are a professional translator of visual novel (video game) dialogue. "
    "Translate from {src} to {tgt}. Rules:\n"
    "- Output only the translation, no explanations, notes or romanization.\n"
    "- Tokens like ZQ0Z, ZQ1Z are placeholders: copy each one exactly once, unchanged, "
    "at the natural position in the translated sentence.\n"
    "- Keep the tone and speech style of the original (casual/polite) natural for a game.\n"
    "{multi}{notes}"
)
MULTI = ("- The input is a numbered list. Answer with the same numbers, one line per item, "
         "in the format \"N. translation\". Never merge or split items.\n")
SPEAKER_RULE = ("- A line may start with [Speaker]. That tells you who is talking; do NOT include the "
                "[Speaker] tag in your output.\n")

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_NUM_RE = re.compile(r"^\s*(\d+)\s*[.)\]:：]\s?(.*)$")
_TAG_RE = re.compile(r"\{[^{}]*\}")

# 프롬프트가 컨텍스트(4096 토큰 안팎)를 넘지 않게 하는 글자 수 상한
MAX_NOTES_CHARS = 1500
MAX_PROFILE_CHARS = 380
MAX_STORY_CHARS = 420
MAX_CTX_LINE = 70


def _clean(out):
    out = _THINK_RE.sub("", out)
    out = out.replace("<think>", "").replace("</think>", "")
    return out.strip()


def _plain(text, limit):
    text = _TAG_RE.sub("", text).replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


class LocalTranslator(Translator):
    name = "local"
    uses_meta = True
    OPTIONS = ("max_batch", "max_tokens", "cooldown")

    def __init__(self, bridge=None, max_batch=4, max_tokens=1024, cooldown=0.0):
        self.bridge = bridge
        self.max_batch = max(1, int(max_batch))
        self.max_tokens = int(max_tokens)
        self.cooldown = float(cooldown)
        self.profiles = {}
        self.story = ""
        self.log = lambda msg: None

    # ------------------------------------------------------------------ 프롬프트

    def _notes(self, speakers):
        parts = []
        if self.story:
            parts.append("Work notes: " + self.story[:MAX_STORY_CHARS])
        chars = []
        for n in speakers:
            p = (self.profiles or {}).get(n)
            if p:
                chars.append("- %s: %s" % (n, p.replace("\n", " ")[:MAX_PROFILE_CHARS]))
        if chars:
            parts.append("Character notes (keep each character's voice and forms of address consistent):\n"
                         + "\n".join(chars))
        if not parts:
            return ""
        text = "\n" + "\n".join(parts) + "\n"
        return text[:MAX_NOTES_CHARS]

    def _system(self, src, tgt, multi, speakers=(), with_speaker=False):
        return SYSTEM.format(
            src=LANG_NAMES.get(src, "the source language" if src == "auto" else src),
            tgt=LANG_NAMES.get(tgt, tgt),
            multi=(MULTI if multi else "") + (SPEAKER_RULE if with_speaker else ""),
            notes=self._notes(speakers))

    @staticmethod
    def _context_block(meta):
        seen, lines = set(), []
        for m in meta or []:
            for spk, text in (m or {}).get("context", []):
                key = (spk, text)
                if key in seen:
                    continue
                seen.add(key)
                lines.append("%s%s" % ("[%s] " % spk if spk else "", _plain(text, MAX_CTX_LINE)))
        lines = lines[:6]
        if not lines:
            return ""
        return "Previous lines (context only, do NOT translate them):\n" + "\n".join(lines) + "\n---\n"

    @staticmethod
    def _strip_speaker(text, speaker):
        if speaker:
            tag = "[%s]" % speaker
            if text.startswith(tag):
                return text[len(tag):].strip()
        return text

    def _chat(self, system, user, n_chars, should_stop):
        if should_stop():
            raise Stopped()
        budget = min(self.max_tokens, 48 + 3 * n_chars)
        try:
            out = self.bridge.chat(system, user, budget)
        except Exception as e:  # JNI 쪽 IllegalStateException 등
            raise TranslatorError("로컬 모델 오류: %s" % e)
        if should_stop():
            raise Stopped()  # 일시정지로 생성이 잘렸을 수 있으니 결과를 쓰지 않는다
        if self.cooldown > 0:
            sleep_interruptible(self.cooldown, should_stop)
        return _clean(str(out))

    # ------------------------------------------------------------------ API

    def translate_one(self, text, source_lang, target_lang, should_stop=lambda: False, meta=None):
        m = (meta or [None])[0] or {}
        spk = m.get("speaker", "")
        system = self._system(source_lang, target_lang, False, [spk] if spk else [], bool(spk))
        user = self._context_block([m]) + ("[%s] %s" % (spk, text) if spk else text)
        out = self._chat(system, user, len(text), should_stop)
        # 한 줄 원문이면 첫 줄만 (모델이 덧붙인 설명 제거)
        if "\n" not in text:
            out = out.split("\n")[0].strip()
        out = self._strip_speaker(out, spk)
        if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'「『" and text[:1] not in "\"'":
            out = out[1:-1]
        return out

    def translate_batch(self, texts, source_lang, target_lang, should_stop=lambda: False, meta=None):
        if self.bridge is None:
            raise TranslatorError("로컬 모델이 로드되지 않았습니다 (설정에서 GGUF 모델 파일을 지정하세요)")
        texts = [t.replace("\n", " ") for t in texts]
        meta = meta or [None] * len(texts)
        if len(texts) == 1:
            return [self.translate_one(texts[0], source_lang, target_lang, should_stop, meta)]
        t0 = time.monotonic()
        speakers = []
        for m in meta:
            n = (m or {}).get("speaker", "")
            if n and n not in speakers:
                speakers.append(n)
        has_spk = any((m or {}).get("speaker") for m in meta)
        rows = []
        for i, (t, m) in enumerate(zip(texts, meta)):
            spk = (m or {}).get("speaker", "")
            rows.append("%d. %s%s" % (i + 1, "[%s] " % spk if spk else "", t))
        user = self._context_block(meta) + "\n".join(rows)
        system = self._system(source_lang, target_lang, True, speakers, has_spk)
        out = self._chat(system, user, len(user), should_stop)
        got = {}
        for line in out.split("\n"):
            m = _NUM_RE.match(line)
            if m:
                k = int(m.group(1))
                got.setdefault(k, self._strip_speaker(m.group(2).strip(), (meta[k - 1] or {}).get("speaker", "")
                                                      if 1 <= k <= len(meta) else ""))
        if sorted(got) == list(range(1, len(texts) + 1)) and all(got.values()):
            self.log("로컬: %d줄 %.1fs" % (len(texts), time.monotonic() - t0))
            return [got[i + 1] for i in range(len(texts))]
        self.log("로컬: 번호 목록 응답이 맞지 않음 — 한 줄씩 다시 번역")
        return [self.translate_one(t, source_lang, target_lang, should_stop, [m])
                for t, m in zip(texts, meta)]
