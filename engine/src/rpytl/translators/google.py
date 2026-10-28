"""Google 번역 (비공식 translate.googleapis.com gtx 엔드포인트).

- 요청 간 최소 간격(delay), 일시적 오류 재시도, 429/차단 페이지 지수 백오프.
- 여러 줄을 줄바꿈으로 이어 한 번에 보낸다. 번역 결과의 줄 수가 다르면 한 줄씩 다시 보낸다.
  (원문 안의 줄바꿈은 protect 가 이미 플레이스홀더로 바꿔 두었으므로 줄 경계 = 항목 경계)
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import RateLimited, Translator, TranslatorError, sleep_interruptible

URL = "https://translate.googleapis.com/translate_a/single"
UA = ("Mozilla/5.0 (Linux; Android 14; SM-S921N) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36")


class GoogleTranslator(Translator):
    name = "google"
    OPTIONS = ("delay", "max_retries", "timeout", "max_chars", "max_batch", "backoff_base",
               "backoff_max")

    def __init__(self, delay=1.2, max_retries=5, timeout=20, max_chars=1800, max_batch=25,
                 backoff_base=30.0, backoff_max=600.0, opener=None, clock=time.monotonic,
                 sleeper=None):
        self.delay = float(delay)
        self.max_retries = int(max_retries)
        self.timeout = float(timeout)
        self.max_chars = int(max_chars)   # 한 요청의 원문 글자 수 (URL 길이 제한 대비)
        self.max_batch = int(max_batch)
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        self._open = opener or self._urlopen
        self._clock = clock
        self._sleep = sleeper or sleep_interruptible
        self._last = None
        self.log = lambda msg: None

    # ------------------------------------------------------------------ HTTP

    def _urlopen(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            return e.code, body

    def _request(self, text, sl, tl, should_stop):
        params = urllib.parse.urlencode({"client": "gtx", "sl": sl, "tl": tl, "dt": "t", "q": text})
        url = URL + "?" + params
        attempt = 0
        while True:
            if self._last is not None:
                wait = self.delay - (self._clock() - self._last)
                if wait > 0:
                    self._sleep(wait, should_stop)
            self._last = self._clock()
            try:
                status, body = self._open(url)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                status, body = None, str(e)

            if status == 200 and body.lstrip().startswith("["):
                return self._parse(body)

            attempt += 1
            limited = status in (429, 503) or (status is not None and "sorry" in body[:2000].lower())
            if attempt > self.max_retries:
                if limited:
                    raise RateLimited("google rate limited (HTTP %s)" % status)
                raise TranslatorError("google failed: HTTP %s %s" % (status, body[:200]))
            if limited:
                wait = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
                self.log("Google 요청 제한(HTTP %s). %d초 대기 후 재시도 (%d/%d)"
                         % (status, wait, attempt, self.max_retries))
            elif status is not None and 400 <= status < 500:
                raise TranslatorError("google HTTP %s: %s" % (status, body[:200]))
            else:
                wait = min(60.0, 2.0 * (2 ** (attempt - 1)))
                self.log("Google 오류(%s). %d초 후 재시도 (%d/%d)"
                         % (status if status is not None else body[:80], wait, attempt,
                            self.max_retries))
            self._sleep(wait, should_stop)

    @staticmethod
    def _parse(body):
        try:
            data = json.loads(body)
            return "".join(seg[0] for seg in data[0] if seg and seg[0])
        except (ValueError, TypeError, IndexError) as e:
            raise TranslatorError("google: unexpected response: %s" % e)

    # ------------------------------------------------------------------ API

    def translate_one(self, text, sl, tl, should_stop=lambda: False):
        return self._request(text, sl, tl, should_stop)

    def translate_batch(self, texts, source_lang, target_lang, should_stop=lambda: False):
        out = []
        for chunk in self._chunks(texts):
            if len(chunk) == 1:
                out.append(self._request(chunk[0], source_lang, target_lang, should_stop))
                continue
            joined = self._request("\n".join(chunk), source_lang, target_lang, should_stop)
            parts = joined.split("\n")
            if len(parts) == len(chunk):
                out.extend(p.strip() for p in parts)
            else:
                self.log("줄 수 불일치(%d→%d) — 한 줄씩 다시 번역" % (len(chunk), len(parts)))
                for t in chunk:
                    out.append(self._request(t, source_lang, target_lang, should_stop))
        return out

    def _chunks(self, texts):
        chunk, size = [], 0
        for t in texts:
            t = t.replace("\n", " ")
            if chunk and (size + len(t) + 1 > self.max_chars or len(chunk) >= self.max_batch):
                yield chunk
                chunk, size = [], 0
            chunk.append(t)
            size += len(t) + 1
        if chunk:
            yield chunk
