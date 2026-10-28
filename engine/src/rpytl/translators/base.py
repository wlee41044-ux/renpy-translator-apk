"""번역 엔진 인터페이스."""

import time


class TranslatorError(Exception):
    """재시도해도 안 되는 실패 (또는 재시도 소진)."""


class RateLimited(TranslatorError):
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


class Stopped(Exception):
    """사용자가 일시정지를 눌렀다."""


class Translator:
    name = "base"
    # 한 번에 넘길 최대 항목 수 (runner 가 참고)
    max_batch = 20

    def translate_batch(self, texts, source_lang, target_lang, should_stop=lambda: False):
        """texts(플레이스홀더 처리된 문자열 목록) → 같은 길이의 번역 목록."""
        raise NotImplementedError

    def close(self):
        pass


def sleep_interruptible(seconds, should_stop, step=0.25):
    end = time.monotonic() + seconds
    while True:
        if should_stop():
            raise Stopped()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(step, left))
