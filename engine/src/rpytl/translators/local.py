"""로컬 LLM 번역 (llama.cpp GGUF). 실제 추론은 Kotlin/JNI 브리지가 담당한다 (4단계).

bridge: generate(prompt: str, max_tokens: int) -> str 를 가진 객체
        (Android 에서는 Kotlin 객체가 Chaquopy 로 넘어온다)
"""

from .base import Translator, TranslatorError


class LocalTranslator(Translator):
    name = "local"
    OPTIONS = ("max_batch", "max_tokens")

    def __init__(self, bridge=None, max_batch=4, max_tokens=512):
        self.bridge = bridge
        self.max_batch = int(max_batch)
        self.max_tokens = int(max_tokens)

    def translate_batch(self, texts, source_lang, target_lang, should_stop=lambda: False):
        if self.bridge is None:
            raise TranslatorError("로컬 모델이 아직 연결되지 않았습니다 (4단계에서 구현)")
        raise TranslatorError("로컬 모델 번역은 4단계에서 구현")
