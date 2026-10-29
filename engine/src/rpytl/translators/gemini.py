"""Gemini API 번역 (generativelanguage.googleapis.com, REST).

- 엔드포인트: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
  인증은 x-goog-api-key 헤더 (키를 URL 에 넣지 않는다 → 로그/오류 메시지에 키가 남지 않는다).
- 프롬프트는 LocalTranslator 와 같다: 번호 목록 배치, 화자 [Speaker], 앞 대사, 캐릭터 분석(profiles)과 작품 요약.
  그래서 말투/호칭 지시가 먹는다 (Google 무료 번역 엔드포인트에는 없는 장점).
- 한도: 분당 요청 수(rpm)로 간격을 지키고, 429 는 서버가 알려 주는 retryDelay 만큼 쉰다.
  하루 한도(PerDay)를 다 쓰면 기다려도 소용없으니 바로 자동 일시정지한다. 연속 실패도 자동 일시정지.
- 게임 대사는 안전 필터에 걸릴 수 있어 safetySettings 를 BLOCK_NONE 으로 보낸다. 그래도 막힌 줄은 빈 번역으로
  돌려줘서 그 줄만 검수필요로 빠지게 한다 (전체가 멈추지 않는다).
- 주의: 입력(게임 대사)이 Google 서버로 전송된다. 무료 등급은 입력이 서비스 개선에 쓰일 수 있다.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

from .base import RateLimited, Stopped, TranslatorError, sleep_interruptible
from .google import AutoPause
from .local import LocalTranslator, _clean

BASE = "https://generativelanguage.googleapis.com/v1beta/"
DEFAULT_MODEL = "gemini-flash-latest"

SAFETY = [{"category": c, "threshold": "BLOCK_NONE"} for c in (
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]


def _model_path(model):
    model = (model or DEFAULT_MODEL).strip()
    if "/" in model or ".." in model or "?" in model or "&" in model or " " in model:
        if not re.match(r"^(models|tunedModels)/[\w.\-]+$", model):
            raise TranslatorError("모델 이름이 올바르지 않습니다: %r" % model[:60])
        return model
    return "models/" + model


def parse_error(body):
    """Google API 오류 JSON → (message, status, retry_delay_sec|None, per_day:bool)"""
    try:
        err = json.loads(body).get("error", {})
    except (ValueError, AttributeError):
        return (body[:160], "", None, False)
    msg = str(err.get("message", ""))[:300]
    status = str(err.get("status", ""))
    retry = None
    per_day = False
    for d in err.get("details", []) or []:
        if not isinstance(d, dict):
            continue
        if str(d.get("@type", "")).endswith("RetryInfo"):
            m = re.match(r"^([\d.]+)s$", str(d.get("retryDelay", "")))
            if m:
                retry = float(m.group(1))
        for v in d.get("violations", []) or []:
            blob = json.dumps(v)
            if "PerDay" in blob or "per day" in blob.lower():
                per_day = True
    if "per day" in msg.lower() or "perday" in msg.lower():
        per_day = True
    return (msg, status, retry, per_day)


class GeminiClient:
    """한 번의 chat() = generateContent 요청 하나. 한도 지키기, 재시도, 오류 분류를 맡는다."""

    def __init__(self, api_key="", model=DEFAULT_MODEL, rpm=10, temperature=0.2, max_tokens=4096,
                 timeout=90, max_retries=5, pause_after=3, backoff_base=30.0, backoff_max=300.0,
                 thinking=False, opener=None, clock=time.monotonic, sleeper=None):
        self.api_key = (api_key or "").strip()
        self.model = _model_path(model)
        self.rpm = max(1.0, float(rpm))
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.pause_after = max(1, int(pause_after))
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        self._thinking_cfg = None if thinking else {"thinkingBudget": 0}   # 번역에는 생각이 필요 없다 (느리고 토큰 낭비)
        self._open = opener or self._urlopen
        self._clock = clock
        self._sleep = sleeper or sleep_interruptible
        self._lock = threading.Lock()
        self._next_start = 0.0
        self._interval = 60.0 / self.rpm
        self._consec = 0
        self.log = lambda msg: None
        self.requests = 0

    # ------------------------------------------------------------------ HTTP

    def _urlopen(self, url, headers, data):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            return e.code, body

    def _headers(self):
        if not self.api_key:
            raise TranslatorError("Gemini API 키가 비어 있습니다 (설정에서 입력하세요)")
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def _gate(self, should_stop):
        with self._lock:
            now = self._clock()
            start = max(now, self._next_start)
            self._next_start = start + self._interval
            wait = start - now
        if wait > 0:
            self._sleep(wait, should_stop)

    def _payload(self, system, user, max_tokens):
        gen = {"temperature": self.temperature, "maxOutputTokens": int(max_tokens or self.max_tokens)}
        if self._thinking_cfg:
            gen["thinkingConfig"] = dict(self._thinking_cfg)
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
            "safetySettings": SAFETY,
        }

    def _text_of(self, data):
        """응답 JSON → 번역 텍스트. 안전 필터 등으로 막히면 ''."""
        cands = data.get("candidates") or []
        if not cands:
            why = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            self.log("Gemini 응답 없음/차단 (%s) — 이 줄은 건너뜀" % why)
            return ""
        c = cands[0]
        parts = ((c.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        if not text.strip():
            self.log("Gemini 가 빈 응답 (%s) — 이 줄은 건너뜀" % c.get("finishReason", "?"))
        return text

    def chat(self, system, user, max_tokens=None, should_stop=lambda: False):
        url = BASE + self.model + ":generateContent"
        attempt = 0
        while True:
            self._gate(should_stop)
            if should_stop():
                raise Stopped()
            data = json.dumps(self._payload(system, user, max_tokens), ensure_ascii=False).encode("utf-8")
            try:
                status, body = self._open(url, self._headers(), data)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                status, body = None, str(e)
            self.requests += 1

            if status == 200:
                try:
                    out = self._text_of(json.loads(body))
                except ValueError:
                    raise TranslatorError("Gemini 응답을 해석할 수 없습니다")
                self._consec = 0
                return out

            msg, st, retry, per_day = parse_error(body or "")
            if status == 429:
                if per_day:
                    raise AutoPause("Gemini 하루 한도를 다 썼습니다 (%s). 내일 '번역 이어하기'를 누르세요" % (msg or st))
                self._consec += 1
                if self._consec >= self.pause_after:
                    raise AutoPause("Gemini 요청 제한(429)이 성공 없이 %d회 연속 — 자동 일시정지" % self._consec)
                wait = (retry + 1.0) if retry else min(self.backoff_max, self.backoff_base * 2 ** (self._consec - 1))
                with self._lock:   # 이후 요청도 조금 더 천천히
                    self._interval = min(60.0, self._interval * 1.5)
                self.log("Gemini 요청 제한(429). %d초 쉬고 재시도, 이후 간격 %.1f초 (%d/%d)"
                         % (wait, self._interval, self._consec, self.pause_after))
                self._sleep(wait, should_stop)
                continue
            if status == 400 and self._thinking_cfg and "thinking" in (msg or "").lower():
                self._thinking_cfg = None     # 이 모델은 생각을 끌 수 없다: 설정 없이 다시
                self.log("이 모델은 thinkingBudget=0 을 받지 않아 기본 설정으로 다시 시도")
                continue
            if status == 400 and ("API key not valid" in (msg or "") or "API_KEY_INVALID" in (body or "")):
                raise TranslatorError("Gemini API 키가 올바르지 않습니다")
            if status in (401, 403):
                raise TranslatorError("Gemini 접근 거부(%s): %s" % (status, msg or st))
            if status == 404:
                raise TranslatorError("Gemini 모델을 찾을 수 없습니다: %s (설정의 '모델 목록 보기'로 확인)" % self.model)
            if status is not None and 400 <= status < 500:
                raise TranslatorError("Gemini 오류(%s): %s" % (status, msg or st))
            attempt += 1
            if attempt > self.max_retries:
                raise TranslatorError("Gemini 서버 오류가 계속됨 (%s %s)" % (status, msg or (body or "")[:80]))
            wait = min(60.0, 2.0 * 2 ** (attempt - 1))
            self.log("Gemini 오류(%s). %d초 후 재시도 (%d/%d)" % (status if status else (body or "")[:60], wait,
                                                         attempt, self.max_retries))
            self._sleep(wait, should_stop)

    def list_models(self):
        """generateContent 를 지원하는 모델 이름 목록 (키/모델 이름 확인용)."""
        status, body = self._open(BASE + "models?pageSize=200", self._headers(), None)
        if status != 200:
            msg, st, _, _ = parse_error(body or "")
            raise TranslatorError("모델 목록을 못 가져왔습니다 (%s): %s" % (status, msg or st))
        out = []
        for m in json.loads(body).get("models", []):
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                out.append(str(m.get("name", "")).replace("models/", "", 1))
        return out


class GeminiTranslator(LocalTranslator):
    """LocalTranslator 의 프롬프트/파싱을 그대로 쓰고 모델 호출만 Gemini API 로 한다."""

    name = "gemini"
    OPTIONS = ("api_key", "model", "rpm", "max_batch", "temperature", "max_tokens", "pause_after")

    def __init__(self, api_key="", model=DEFAULT_MODEL, rpm=10, max_batch=20, temperature=0.2,
                 max_tokens=4096, pause_after=3, client=None):
        self.client = client or GeminiClient(api_key=api_key, model=model, rpm=rpm, temperature=temperature,
                                             max_tokens=max_tokens, pause_after=pause_after)
        super().__init__(bridge=self.client, max_batch=max_batch, max_tokens=max_tokens, cooldown=0.0)
        self.parallel = 1

    @property
    def log(self):
        return self.client.log

    @log.setter
    def log(self, fn):
        self.client.log = fn

    def _chat(self, system, user, n_chars, should_stop):
        if should_stop():
            raise Stopped()
        out = self.client.chat(system, user, self.max_tokens, should_stop)
        if should_stop():
            raise Stopped()
        return _clean(str(out))

    def take_report(self):
        n, self.client.requests = self.client.requests, 0
        return "Gemini 요청 %d회 (분당 약 %.0f회 이하)" % (n, 60.0 / self.client._interval) if n else ""
