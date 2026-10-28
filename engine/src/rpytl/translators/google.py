"""Google 번역 (비공식 translate.googleapis.com gtx 엔드포인트).

- 병렬: 한 배치를 여러 요청(청크)으로 나눠 workers 개까지 동시에 보낸다.
- 안전장치: 모든 요청 시작 사이에 전체 공통 최소 간격(delay), 429/차단 페이지가 오면
  모든 워커가 같이 지수 백오프로 쉬고 동시 요청 수를 하나 줄인다. 성공이 이어지면 천천히 되돌린다.
- 여러 줄을 줄바꿈으로 이어 한 요청으로 보낸다. 번역 결과의 줄 수가 다르면 한 줄씩 다시 보낸다.
  (원문 안의 줄바꿈은 protect 가 이미 플레이스홀더로 바꿔 두었으므로 줄 경계 = 항목 경계)
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .base import RateLimited, Stopped, Translator, TranslatorError, sleep_interruptible

URL = "https://translate.googleapis.com/translate_a/single"
UA = ("Mozilla/5.0 (Linux; Android 14; SM-S921N) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36")

class PayloadTooLarge(TranslatorError):
    """HTTP 413/414: 요청 주소가 너무 길다. 호출자가 청크를 쪼개서 다시 보낸다."""


GROW_AFTER = 40  # 연속 성공 요청 수: 이만큼 지나면 동시 요청 수를 하나 되돌린다


def _short(body):
    """오류 응답이 HTML 통째로 로그에 쏟아지지 않게 제목만 뽑는다."""
    import re
    m = re.search(r"<title>(.*?)</title>", body, re.S | re.I)
    return (m.group(1) if m else body)[:120].strip()


class GoogleTranslator(Translator):
    name = "google"
    OPTIONS = ("delay", "max_retries", "timeout", "max_chars", "max_batch", "backoff_base",
               "backoff_max", "workers", "max_encoded")

    def __init__(self, delay=0.5, max_retries=5, timeout=20, max_chars=1800, max_batch=25,
                 backoff_base=30.0, backoff_max=600.0, workers=3, max_encoded=4500, opener=None,
                 clock=time.monotonic, sleeper=None):
        self.delay = float(delay)          # 요청 시작 사이 전체 공통 최소 간격(초)
        self.max_retries = int(max_retries)
        self.timeout = float(timeout)
        self.max_chars = int(max_chars)    # 한 요청의 원문 글자 수
        # 한 요청의 URL 인코딩 후 길이. 한글/일본어는 글자당 9바이트로 늘어나 글자 수만으로는 부족하다.
        self.max_encoded = int(max_encoded)
        self.max_batch = int(max_batch)    # 한 요청의 줄 수
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        self.max_workers = max(1, int(workers))
        self._workers = self.max_workers   # 현재 허용 동시 요청 수 (차단되면 줄어듦)
        self._open = opener or self._urlopen
        self._clock = clock
        self._sleep = sleeper or sleep_interruptible
        self._lock = threading.Lock()
        self._next_start = 0.0
        self._pause_until = 0.0
        self._streak = 0
        self._abort = False
        self.log = lambda msg: None

    @property
    def parallel(self):
        return self._workers

    # ------------------------------------------------------------------ 공통 제한

    def _gate(self, should_stop):
        """요청을 보내기 전에 호출: 전체 공통 간격과 공통 백오프를 지킨다."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next_start, self._pause_until)
            self._next_start = start + self.delay
            wait = start - now
        if wait > 0:
            self._sleep(wait, should_stop)

    def _on_limited(self, wait):
        with self._lock:
            self._pause_until = max(self._pause_until, self._clock() + wait)
            self._streak = 0
            if self._workers > 1:
                self._workers -= 1
                self.log("요청 제한 감지 — 동시 요청 수를 %d 로 줄임" % self._workers)

    def _on_success(self):
        with self._lock:
            self._streak += 1
            if self._streak >= GROW_AFTER and self._workers < self.max_workers:
                self._workers += 1
                self._streak = 0
                self.log("안정적 — 동시 요청 수를 %d 로 복구" % self._workers)

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
            self._gate(should_stop)
            if should_stop():
                raise Stopped()
            try:
                status, body = self._open(url)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                status, body = None, str(e)

            if status == 200 and body.lstrip().startswith("["):
                result = self._parse(body)
                self._on_success()
                return result

            attempt += 1
            limited = status in (429, 503) or (status is not None and "sorry" in body[:2000].lower())
            if attempt > self.max_retries:
                if limited:
                    raise RateLimited("google rate limited (HTTP %s)" % status)
                raise TranslatorError("google failed: HTTP %s %s" % (status, _short(body)))
            if limited:
                wait = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
                self.log("Google 요청 제한(HTTP %s). %d초 쉬고 재시도 (%d/%d)"
                         % (status, wait, attempt, self.max_retries))
                self._on_limited(wait)   # 다음 _gate 에서 모든 워커가 같이 쉰다
            elif status in (413, 414):
                raise PayloadTooLarge("google HTTP %s (요청이 너무 김)" % status)
            elif status is not None and 400 <= status < 500:
                raise TranslatorError("google HTTP %s: %s" % (status, _short(body)))
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

    def _translate_long(self, text, sl, tl, should_stop):
        """한 줄 자체가 너무 길 때: 문장 부호/공백 단위로 쪼개 따로 번역해서 이어 붙인다."""
        import re
        pieces, cur = [], ""
        for part in re.split(r"(?<=[.!?。！？…、,\s])", text):
            if cur and len(urllib.parse.quote(cur + part)) > self.max_encoded // 2:
                pieces.append(cur)
                cur = ""
            cur += part
        if cur:
            pieces.append(cur)
        if len(pieces) <= 1:
            raise TranslatorError("한 줄이 너무 길어 보낼 수 없음 (%d자)" % len(text))
        self.log("한 줄이 너무 길어 %d조각으로 나눠 번역" % len(pieces))
        return "".join(self._request(p, sl, tl, should_stop) for p in pieces)

    def _translate_chunk(self, chunk, sl, tl, should_stop):
        if len(chunk) == 1:
            try:
                return [self._request(chunk[0], sl, tl, should_stop)]
            except PayloadTooLarge:
                return [self._translate_long(chunk[0], sl, tl, should_stop)]
        try:
            joined = self._request("\n".join(chunk), sl, tl, should_stop)
        except PayloadTooLarge:
            mid = len(chunk) // 2
            self.log("요청이 너무 커서 %d줄을 반으로 나눠 재시도" % len(chunk))
            return (self._translate_chunk(chunk[:mid], sl, tl, should_stop)
                    + self._translate_chunk(chunk[mid:], sl, tl, should_stop))
        parts = joined.split("\n")
        if len(parts) == len(chunk):
            return [p.strip() for p in parts]
        self.log("줄 수 불일치(%d→%d) — 한 줄씩 다시 번역" % (len(chunk), len(parts)))
        return [self._request(t, sl, tl, should_stop) for t in chunk]

    def translate_batch(self, texts, source_lang, target_lang, should_stop=lambda: False):
        chunks = list(self._chunks(texts))
        workers = min(self._workers, len(chunks))
        if workers <= 1:
            out = []
            for c in chunks:
                out.extend(self._translate_chunk(c, source_lang, target_lang, should_stop))
            return out

        self._abort = False

        def stop():
            return self._abort or should_stop()

        def task(c):
            try:
                return self._translate_chunk(c, source_lang, target_lang, stop)
            except Exception:
                self._abort = True   # 다른 워커도 빨리 멈춘다
                raise

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(task, c) for c in chunks]
        errors = [f.exception() for f in futures if f.exception() is not None]
        if errors:
            # 다른 워커가 abort 때문에 낸 Stopped 보다 진짜 원인을 우선
            real = [e for e in errors if not isinstance(e, Stopped)]
            raise (real or errors)[0]
        out = []
        for f in futures:
            out.extend(f.result())
        return out

    def _chunks(self, texts):
        chunk, size, enc = [], 0, 0
        for t in texts:
            t = t.replace("\n", " ")
            e = len(urllib.parse.quote(t)) + 3   # 줄 구분 %0A
            if chunk and (size + len(t) + 1 > self.max_chars or enc + e > self.max_encoded
                          or len(chunk) >= self.max_batch):
                yield chunk
                chunk, size, enc = [], 0, 0
            chunk.append(t)
            size += len(t) + 1
            enc += e
        if chunk:
            yield chunk
