"""번역 엔진 (교체형). 모든 엔진은 base.Translator 를 구현한다."""

from .base import RateLimited, Translator, TranslatorError  # noqa: F401


def create(name, opts, local_bridge=None):
    if name == "google":
        from .google import GoogleTranslator
        return GoogleTranslator(**{k: v for k, v in opts.items() if k in GoogleTranslator.OPTIONS})
    if name == "local":
        from .local import LocalTranslator
        return LocalTranslator(local_bridge, **{k: v for k, v in opts.items()
                                                if k in LocalTranslator.OPTIONS})
    raise ValueError("unknown engine: %r" % name)
