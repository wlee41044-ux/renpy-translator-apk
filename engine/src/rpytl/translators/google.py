"""Google 번역 (비공식 translate.googleapis.com gtx 엔드포인트).

속도:
- 병렬: 한 배치를 여러 요청(청크)으로 나눠 workers 개까지 동시에 보낸다.
- 큰 요청은 URL 대신 본문(POST)으로 보낸다. URL 에 넣으면 한글/일본어가 글자당 9바이트로 늘어나
  한 요청에 몇 줄밖에 못 담는다 (요청 수가 늘어 병렬의 이득이 사라짐). POST 가 거절되면 자동으로 GET 으로 돌아간다.
- 한도(413)를 만나면 그 크기의 85% 로 한도를 낮춰 기억한다.

안전:
- 모든 요청 시작 사이에 전체 공통 최소 간격(delay). 429/차단 페이지가 오면 모든 워커가 같이 지수 백오프로 쉬고
  동시 요청 수를 하나 줄인다. 성공이 이어지면 천천히 되돌린다.
- 여러 줄을 줄바꿈으로 이어 한 요청으로 보낸다. 결과 줄 수가 다르면 한 줄씩 다시 보낸다.
  (원문 안의 줄바꿈은 protect 가 이미 플레이스홀더로 바꿔 두었으므로 줄 경계 = 항목 경계)
"""

import json
import re
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
    """HTTP 413/414: 요청이 너무 크다. 호출자가 청크를 쪼개서 다시 보낸다."""


GROW_AFTER = 40       # 연속 성공 요청 수: 이만큼 지나면 동시 요청 수를 하나 되돌린다
GET_ABOVE = 1200      # URL 인코딩 후 이 길이를 넘으면 POST 를 시도한다


def _short(body):
    """오류 응답이 HTML 통째로 로그에 쏟아지지 않게 제목만 뽑는다."""
    m = re.search(r"<title>(.*?)</title>", body, re.S | re.I)
    return (m.group(1) if m else body)[:120].strip()


class GoogleTranslator(Translator):
    name = "google"
    OPTIONS = ("delay", "max_retries", "timeout", "max_chars", "max_batch", "backoff_base",
               "backoff_max", "workers", "max_encoded", "use_post")

    def __init__(self, delay=0.5, max_retries=6, timeout=20, max_chars=3000, max_batch=40,
                 backoff_base=30.0, backoff_max=600.0, workers=3, max_encoded=6000, use_post=True,
                 opener=None, post_opener=None, clock=time.monotonic, sleeper=None):
        self.delay = float(delay)          # 요청 시작 사이 전체 공통 최소 간격(초)
        self.max_retries = int(max_retries)
        self.timeout = float(timeout)
        self.max_chars = int(max_chars)    # POST 한 요청의 원문 글자 수 (413 을 만나면 자동으로 낮춤)
        self.max_batch = int(max_batch)    # 한 요청의 줄 수
        self.max_encoded = int(max_encoded)  # GET 한 요청의 URL 인코딩 후 길이 (413 을 만나면 자동으로 낮춤)
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        self.max_workers = max(1, int(workers))
        self._workers = self.max_workers   # 현재 허용 동시 요청 수 (차단되면 줄어듦)
        self._open = opener or self._urlopen
        # 테스트 등에서 GET 용 opener 만 주면 POST 는 쓰지 않는다
        self._open_post = post_opener or (self._urlopen_post if opener is None else None)
        self._post_ok = None if (use_post and self._open_post) else False   # None=아직 모름
        self._clock = clock
        self._sleep = sleeper or sleep_interruptible
        self._lock = threading.Lock()
        self._next_start = 0.0
        self._pause_until = 0.0
        self._streak = 0
        self._interval = self.delay        # 현재 요청 시작 간격: 제한을 맞으면 늘리고, 안정되면 기본값까지 줄인다
        self._level = 0                    # 연속 제한 사건 수준 (대기 시간 = backoff_base * 2^(level-1))
        self._abort = False
        self._rep = {"req": 0, "lat": 0.0, "gate": 0.0, "post": 0}
        self.log = lambda msg: None

    @property
    def parallel(self):
        return self._workers

    @property
    def batch_lines(self):
        # 여러 요청 라운드를 한 배치에 담아 배치 끝의 꼬리 대기와 DB 왕복 비중을 줄인다
        return self.max_batch * self.max_workers * 3

    def take_report(self):
        with self._lock:
            r, self._rep = self._rep, {"req": 0, "lat": 0.0, "gate": 0.0, "post": 0}
            w = self._workers
            mode = "POST" if self._post_ok else "GET"
        if not r["req"]:
            return ""
        return "요청 %d회(평균 %.1fs, 간격대기 합 %.1fs) %s 동시 %d" % (
            r["req"], r["lat"] / r["req"], r["gate"], mode, w)

    # ------------------------------------------------------------------ 공통 제한

    def _gate(self, should_stop):
        """요청을 보내기 전에 호출: 전체 공통 간격과 공통 백오프를 지킨다."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next_start, self._pause_until)
            self._next_start = start + self._interval
            wait = start - now
            self._rep["gate"] += max(0.0, wait)
        if wait > 0:
            self._sleep(wait, should_stop)

    def _on_limited(self):
        """429/차단을 만났다. 같은 사건(이미 쉬는 중)은 한 번만 센다 — 워커 4개가 각자 대기를 늘리고
        쉬는 시간이 끝나면 동시에 몰려서 또 제한에 걸리는 것을 막는다. 반환: 쉴 시간(초)."""
        with self._lock:
            now = self._clock()
            self._streak = 0
            if now >= self._pause_until:      # 새로운 제한 사건
                self._level += 1
                wait = min(self.backoff_max, self.backoff_base * (2 ** (self._level - 1)))
                self._pause_until = now + wait
                # 앞으로는 더 천천히: 요청 시작 간격을 두 배로 (최대 5초), 동시 요청도 하나 줄임
                self._interval = min(5.0, max(self._interval * 2, 0.5))
                if self._workers > 1:
                    self._workers -= 1
                self.log("요청 제한 감지 — %d초 쉬고, 이후 간격 %.1fs · 동시 %d 로 늦춤"
                         % (wait, self._interval, self._workers))
                return wait
            return self._pause_until - now

    def _on_success(self):
        with self._lock:
            self._streak += 1
            if self._streak >= GROW_AFTER:
                self._streak = 0
                self._level = max(0, self._level - 1)
                if self._interval > self.delay:
                    self._interval = max(self.delay, self._interval * 0.8)
                elif self._workers < self.max_workers:
                    self._workers += 1
                    self.log("안정적 — 동시 요청 수를 %d 로 복구" % self._workers)

    # ------------------------------------------------------------------ HTTP

    @staticmethod
    def _read(resp_or_err):
        try:
            return resp_or_err.read().decode("utf-8", "replace")
        except Exception:
            return ""

    def _urlopen(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, self._read(r)
        except urllib.error.HTTPError as e:
            return e.code, self._read(e)

    def _urlopen_post(self, url, data):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"User-Agent": UA, "Accept": "*/*",
                     "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, self._read(r)
        except urllib.error.HTTPError as e:
            return e.code, self._read(e)

    def _params(self, sl, tl):
        return {"client": "gtx", "sl": sl, "tl": tl, "dt": "t"}

    def _send(self, text, sl, tl):
        """(status, body, used_post). 큰 요청은 POST 를 시도하고, POST 가 안 되면 GET 으로 돌아간다."""
        big = len(urllib.parse.quote(text)) > GET_ABOVE
        if big and self._post_ok is not False:
            url = URL + "?" + urllib.parse.urlencode(self._params(sl, tl))
            data = urllib.parse.urlencode({"q": text}).encode("ascii")
            status, body = self._open_post(url, data)
            if status == 200 and body.lstrip().startswith("["):
                if self._post_ok is None:
                    self.log("큰 요청은 POST 로 전송 (한 요청에 더 많은 줄)")
                self._post_ok = True
                return status, body, True
            if status in (429, 503) or "sorry" in body[:2000].lower() or status in (413, 414):
                return status, body, True            # 제한/크기 문제는 POST 탓이 아님
            if self._post_ok is None:
                self._post_ok = False
                self.log("POST 가 거절됨(HTTP %s) — GET 방식으로 전환" % status)
                # 이 요청은 GET 한도(max_encoded)를 넘을 수 있으므로 호출자가 쪼개서 다시 보낸다
                return 414, "", False
            return status, body, True
        params = dict(self._params(sl, tl), q=text)
        status, body = self._open(URL + "?" + urllib.parse.urlencode(params))
        return status, body, False

    def _request(self, text, sl, tl, should_stop):
        attempt = 0
        while True:
            self._gate(should_stop)
            if should_stop():
                raise Stopped()
            t0 = time.monotonic()
            try:
                status, body, used_post = self._send(text, sl, tl)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                status, body, used_post = None, str(e), False
            lat = time.monotonic() - t0
            with self._lock:
                self._rep["req"] += 1
                self._rep["lat"] += lat
                self._rep["post"] += 1 if used_post else 0

            if status == 200 and body.lstrip().startswith("["):
                result = self._parse(body)
                self._on_success()
                return result

            attempt += 1
            limited = status in (429, 503) or (status is not None and "sorry" in body[:2000].lower())
            if attempt > self.max_retries and not limited and status not in (413, 414):
                raise TranslatorError("google failed: HTTP %s %s" % (status, _short(body)))
            if attempt > self.max_retries and limited:
                raise RateLimited("google rate limited (HTTP %s)" % status)
            if limited:
                self._on_limited()   # 다음 _gate 에서 모든 워커가 같이 쉰다 (대기 시간은 공통)
            elif status in (413, 414):
                raise PayloadTooLarge("google HTTP %s (요청이 너무 김, %d자)" % (status, len(text)))
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

    def _shrink_limit(self, size_chars, joined):
        """413 을 만난 크기의 85% 로 한도를 낮춰 다음 청크부터 처음부터 작게 나눈다."""
        with self._lock:
            if self._post_ok:
                new = max(300, int(size_chars * 0.85))
                if new < self.max_chars:
                    self.max_chars = new
                    self.log("POST 한도 조정: 한 요청 최대 %d자" % new)
            else:
                enc = len(urllib.parse.quote(joined))
                new = max(1000, int(enc * 0.85))
                if new < self.max_encoded:
                    self.max_encoded = new
                    self.log("GET 한도 조정: 한 요청 최대 %d바이트" % new)

    def _translate_long(self, text, sl, tl, should_stop):
        """한 줄 자체가 너무 길 때: 문장 부호/공백 단위로 쪼개 따로 번역해서 이어 붙인다."""
        limit = self.max_encoded // 2
        pieces, cur = [], ""
        for part in re.split(r"(?<=[.!?。！？…、,\s])", text):
            if cur and len(urllib.parse.quote(cur + part)) > limit:
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
        joined = "\n".join(chunk)
        try:
            out = self._request(joined, sl, tl, should_stop)
        except PayloadTooLarge:
            self._shrink_limit(len(joined), joined)
            mid = len(chunk) // 2
            self.log("요청이 너무 커서 %d줄을 반으로 나눠 재시도" % len(chunk))
            return (self._translate_chunk(chunk[:mid], sl, tl, should_stop)
                    + self._translate_chunk(chunk[mid:], sl, tl, should_stop))
        parts = out.split("\n")
        if len(parts) == len(chunk):
            return [p.strip() for p in parts]
        self.log("줄 수 불일치(%d→%d) — 반으로 나눠 다시 번역" % (len(chunk), len(parts)))
        mid = len(chunk) // 2
        return (self._translate_chunk(chunk[:mid], sl, tl, should_stop)
                + self._translate_chunk(chunk[mid:], sl, tl, should_stop))

    def translate_batch(self, texts, source_lang, target_lang, should_stop=lambda: False):
        """실패하면 예외에 .partial (texts 와 같은 길이, 못 한 곳은 None) 을 붙여 던진다.
        이미 성공한 청크의 번역을 버리지 않고 runner 가 저장하게 하기 위함."""
        chunks = list(self._chunks(texts))
        workers = min(self._workers, len(chunks))
        results = [None] * len(chunks)
        errors = []

        def attach(exc):
            partial = []
            for c, r in zip(chunks, results):
                partial.extend(r if r is not None else [None] * len(c))
            exc.partial = partial
            return exc

        if workers <= 1:
            for i, c in enumerate(chunks):
                try:
                    results[i] = self._translate_chunk(c, source_lang, target_lang, should_stop)
                except Exception as e:
                    raise attach(e)
            return [x for r in results for x in r]

        self._abort = False

        def stop():
            return self._abort or should_stop()

        def task(i, c):
            try:
                results[i] = self._translate_chunk(c, source_lang, target_lang, stop)
            except Exception as e:
                self._abort = True   # 다른 워커도 빨리 멈춘다
                errors.append(e)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, c in enumerate(chunks):
                ex.submit(task, i, c)
        if errors:
            # 다른 워커가 abort 때문에 낸 Stopped 보다 진짜 원인을 우선
            real = [e for e in errors if not isinstance(e, Stopped)]
            raise attach((real or errors)[0])
        return [x for r in results for x in r]

    def _chunks(self, texts):
        """POST 가 가능하면 글자 수 기준, 아니면 URL 인코딩 길이 기준으로 청크를 자른다."""
        use_post = self._post_ok is not False
        chunk, chars, enc = [], 0, 0
        for t in texts:
            t = t.replace("\n", " ")
            e = len(urllib.parse.quote(t)) + 3   # 줄 구분 %0A
            over = (chars + len(t) + 1 > self.max_chars) if use_post else (enc + e > self.max_encoded)
            if chunk and (over or len(chunk) >= self.max_batch):
                yield chunk
                chunk, chars, enc = [], 0, 0
            chunk.append(t)
            chars += len(t) + 1
            enc += e
        if chunk:
            yield chunk
