"""로컬 LLM 번역 (llama.cpp GGUF, 폰 안에서 실행).

실제 추론은 Kotlin/JNI 브리지(LlamaLlm)가 한다.
bridge: chat(system: str, user: str, max_tokens: int) -> str

여러 줄을 번호 목록으로 한 번에 보내고, 번호가 전부 맞게 돌아오지 않으면 한 줄씩 다시 보낸다.
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
    "{multi}"
)
MULTI = ("- The input is a numbered list. Answer with the same numbers, one line per item, "
         "in the format \"N. translation\". Never merge or split items.\n")

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_NUM_RE = re.compile(r"^\s*(\d+)\s*[.)\]:：]\s?(.*)$")


def _clean(out):
    out = _THINK_RE.sub("", out)
    out = out.replace("<think>", "").replace("</think>", "")
    return out.strip()


class LocalTranslator(Translator):
    name = "local"
    OPTIONS = ("max_batch", "max_tokens", "cooldown")

    def __init__(self, bridge=None, max_batch=4, max_tokens=1024, cooldown=0.0):
        self.bridge = bridge
        self.max_batch = max(1, int(max_batch))
        self.max_tokens = int(max_tokens)
        self.cooldown = float(cooldown)
        self.log = lambda msg: None

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

    def _system(self, src, tgt, multi):
        return SYSTEM.format(src=LANG_NAMES.get(src, "the source language" if src == "auto" else src),
                             tgt=LANG_NAMES.get(tgt, tgt), multi=MULTI if multi else "")

    def translate_one(self, text, source_lang, target_lang, should_stop=lambda: False):
        out = self._chat(self._system(source_lang, target_lang, False), text, len(text), should_stop)
        # 한 줄 원문이면 첫 줄만 (모델이 덧붙인 설명 제거)
        if "\n" not in text:
            out = out.split("\n")[0].strip()
        if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'「『" and text[:1] not in "\"'":
            out = out[1:-1]
        return out

    def translate_batch(self, texts, source_lang, target_lang, should_stop=lambda: False):
        if self.bridge is None:
            raise TranslatorError("로컬 모델이 로드되지 않았습니다 (설정에서 GGUF 모델 파일을 지정하세요)")
        texts = [t.replace("\n", " ") for t in texts]
        if len(texts) == 1:
            return [self.translate_one(texts[0], source_lang, target_lang, should_stop)]
        t0 = time.monotonic()
        user = "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(texts))
        out = self._chat(self._system(source_lang, target_lang, True), user, len(user), should_stop)
        got = {}
        for line in out.split("\n"):
            m = _NUM_RE.match(line)
            if m:
                got.setdefault(int(m.group(1)), m.group(2).strip())
        if sorted(got) == list(range(1, len(texts) + 1)) and all(got.values()):
            self.log("로컬: %d줄 %.1fs" % (len(texts), time.monotonic() - t0))
            return [got[i + 1] for i in range(len(texts))]
        self.log("로컬: 번호 목록 응답이 맞지 않음 — 한 줄씩 다시 번역")
        return [self.translate_one(t, source_lang, target_lang, should_stop) for t in texts]
