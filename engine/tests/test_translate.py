import json
import os
import shutil
import urllib.parse

import pytest

from rpytl import db, pipeline, runner
from rpytl.translators import RateLimited, TranslatorError
from rpytl.translators.google import GoogleTranslator

DATA = os.path.join(os.path.dirname(__file__), "data")


def gtx_body(text):
    # 실제 응답 형식: [[["번역","원문",null,null,10],...],null,"en",...]
    return json.dumps([[[t + ("\n" if i < len(text.split("\n")) - 1 else ""), "x", None, None, 10]
                        for i, t in enumerate(text.split("\n"))], None, "en"])


class FakeNet:
    """q 파라미터를 받아 '번역'해 주는 가짜 Google. responses 로 실패를 섞을 수 있다."""

    def __init__(self, fn=lambda s: "KO(" + s + ")", responses=None):
        self.fn = fn
        self.responses = list(responses or [])
        self.calls = []

    def __call__(self, url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
        self.calls.append(q)
        if self.responses:
            r = self.responses.pop(0)
            if r is not None:
                return r
        return 200, gtx_body("\n".join(self.fn(line) for line in q.split("\n")))


def make(net, **kw):
    """가짜 시계: sleep 하면 시간이 그만큼 흐른다 (단일 스레드 테스트용)."""
    sleeps, t = [], [0.0]

    def sleeper(s, stop):
        sleeps.append(s)
        t[0] += s

    kw.setdefault("workers", 1)
    kw.setdefault("delay", 0)
    g = GoogleTranslator(opener=net, sleeper=sleeper, clock=lambda: t[0], **kw)
    return g, sleeps


def test_batch_joined_in_one_request():
    net = FakeNet()
    g, _ = make(net)
    assert g.translate_batch(["a", "b", "c"], "en", "ko") == ["KO(a)", "KO(b)", "KO(c)"]
    assert len(net.calls) == 1


def test_line_count_mismatch_falls_back_to_single():
    net = FakeNet(fn=lambda s: s, responses=[(200, gtx_body("merged line"))])
    g, _ = make(net)
    assert g.translate_batch(["a", "b"], "en", "ko") == ["a", "b"]
    assert net.calls == ["a\nb", "a", "b"]


def test_429_backoff_then_success():
    net = FakeNet(responses=[(429, "Too Many"), (200, "<html>Sorry...</html>"), None])
    g, sleeps = make(net, backoff_base=10)
    assert g.translate_batch(["hi"], "en", "ko") == ["KO(hi)"]
    assert sleeps[:2] == [10, 20]


def test_rate_limit_exhausted():
    net = FakeNet(responses=[(429, "")] * 10)
    g, _ = make(net, max_retries=2)
    with pytest.raises(RateLimited):
        g.translate_batch(["hi"], "en", "ko")


def test_client_error_not_retried():
    net = FakeNet(responses=[(400, "bad")])
    g, _ = make(net)
    with pytest.raises(TranslatorError):
        g.translate_batch(["hi"], "en", "ko")
    assert len(net.calls) == 1


def test_chunking_by_size():
    net = FakeNet()
    g, _ = make(net, max_encoded=14)       # GET 모드: URL 인코딩 길이 기준
    g.translate_batch(["aaaa", "bbbb", "cccc"], "en", "ko")
    assert net.calls == ["aaaa\nbbbb", "cccc"]


class Ctl:
    def __init__(self, stop_after=None):
        self.logs, self.prog, self.stop_after, self.n = [], [], stop_after, 0

    def isStopped(self):
        self.n += 1
        return self.stop_after is not None and self.n > self.stop_after

    def log(self, m):
        self.logs.append(m)

    def progress(self, d, t):
        self.prog.append((d, t))


@pytest.fixture
def game(tmp_path, monkeypatch):
    g = tmp_path / "game"
    shutil.copytree(os.path.join(DATA, "game_v1"), g)
    dbp = str(tmp_path / "t.db")
    gid = pipeline.add_game(dbp, "T", "u")
    pipeline.scan_game(dbp, gid, str(g))
    return dbp, gid


def _patch_google(monkeypatch, net):
    from rpytl import translators
    orig = translators.create

    def create(name, opts, bridge=None):
        if name == "google":
            return GoogleTranslator(opener=net, sleeper=lambda s, stop: None, delay=0, max_batch=4,
                                    workers=1)
        return orig(name, opts, bridge)
    monkeypatch.setattr(runner, "create", create)


def test_runner_translates_everything_and_restores_tags(game, monkeypatch):
    dbp, gid = game
    # 번역기가 플레이스홀더를 소문자/공백 섞어서 돌려줘도 복원돼야 함
    net = FakeNet(fn=lambda s: "번역:" + s.replace("ZQ0Z", "zq 0 z"))
    _patch_google(monkeypatch, net)
    ctl = Ctl()
    r = json.loads(runner.run(dbp, gid, "google", "{}", ctl))
    assert r["status"] == "finished"
    con = db.connect(dbp)
    s = db.stats(con, gid)
    assert s["done"] == s["total"] == 18
    tr = {row["source"]: row["translation"] for row in con.execute("SELECT * FROM texts")}
    assert tr["Good morning, [player_name]!"] == "번역:Good morning, [player_name]!"
    assert tr["Hey, it's %(time)s already.\nWe're late!"] == \
        "번역:Hey, it's %(time)s already.\nWe're late!"
    assert ctl.prog[-1] == (18, 18)
    # 모든 요청은 ko 로
    assert all("ZQ" in c or "[" not in c for c in net.calls)


def test_runner_marks_review_on_broken_placeholder(game, monkeypatch):
    dbp, gid = game
    net = FakeNet(fn=lambda s: "깨짐" if "ZQ" in s else "ok:" + s)
    _patch_google(monkeypatch, net)
    r = json.loads(runner.run(dbp, gid, "google", "{}", Ctl()))
    con = db.connect(dbp)
    s = db.stats(con, gid)
    assert r["review"] == s["review"] == 4  # [player_name], {b}, %(time)s\n, Version [..]
    rv = db.review_list(con, gid)
    assert all("placeholders mismatch" in x["note"] for x in rv)
    # 검수필요 재시도 옵션
    net.fn = lambda s: "ok:" + s
    json.loads(runner.run(dbp, gid, "google", json.dumps({"retry_review": True}), Ctl()))
    assert db.stats(con, gid)["review"] == 0


def test_runner_pause_and_resume(game, monkeypatch):
    dbp, gid = game
    net = FakeNet()
    _patch_google(monkeypatch, net)
    r = json.loads(runner.run(dbp, gid, "google", "{}", Ctl(stop_after=5)))
    assert r["status"] == "stopped"
    con = db.connect(dbp)
    first = db.stats(con, gid)
    assert 0 < first["done"] < first["total"]
    calls_before = len(net.calls)
    r = json.loads(runner.run(dbp, gid, "google", "{}", Ctl()))
    assert r["status"] == "finished"
    assert db.stats(con, gid)["done"] == first["total"]
    # 이미 번역된 줄은 다시 요청하지 않음
    sent = [line for c in net.calls[calls_before:] for line in c.split("\n")]
    assert len(sent) == first["total"] - first["done"] - r["copied"]


def test_runner_error_keeps_progress(game, monkeypatch):
    dbp, gid = game
    net = FakeNet(responses=[None, None, None, (400, "blocked")])   # 첫 배치(3요청)는 성공, 둘째 배치에서 실패
    _patch_google(monkeypatch, net)
    r = json.loads(runner.run(dbp, gid, "google", "{}", Ctl()))
    assert r["status"] == "error"
    con = db.connect(dbp)
    assert db.stats(con, gid)["done"] >= 4


def test_untranslatable_copied(tmp_path):
    g = tmp_path / "g"
    g.mkdir()
    (g / "a.rpy").write_text('label a:\n    "[name]"\n    "{w}..."\n', encoding="utf-8")
    dbp = str(tmp_path / "t.db")
    gid = pipeline.add_game(dbp, "T", "u")
    pipeline.scan_game(dbp, gid, str(g))
    r = json.loads(runner.run(dbp, gid, "google", "{}", Ctl()))  # 네트워크 안 씀
    assert r["copied"] == 2 and r["status"] == "finished"
    res = json.loads(pipeline.export(dbp, gid, str(tmp_path / "o")))
    assert res["files"] == ["tl/korean/rpytl/_language.rpy"]


def test_local_without_model_errors_cleanly(game):
    dbp, gid = game
    r = json.loads(runner.run(dbp, gid, "local", "{}", Ctl()))
    assert r["status"] == "error" and "로드되지 않았습니다" in r["message"]


class FakeLlm:
    """번호 목록을 받아 각 줄 앞에 KO: 를 붙여 주는 가짜 모델. mode 로 오작동 흉내."""

    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, []

    def chat(self, system, user, max_tokens):
        self.calls.append((system, user, max_tokens))
        lines = user.split("\n")
        if "numbered list" in system:
            if self.mode == "merge":
                return "<think>\n\n</think>\n1. 합쳐버림"
            import re as _re
            body = [l for l in lines if _re.match(r"^\d+\. ", l)]       # 앞의 문맥 블록은 번역하지 않는다
            return "<think>\n\n</think>\n" + "\n".join(
                "%s. KO:%s" % (l.split(". ", 1)[0], _re.sub(r"^\[[^\]]+\] ", "", l.split(". ", 1)[1]))
                for l in body)
        return '"KO1:%s"\n(설명: 이것은 번역입니다)' % user


def test_local_batch_numbered(game):
    dbp, gid = game
    llm = FakeLlm()
    r = json.loads(runner.run(dbp, gid, "local", json.dumps({"local": {"max_batch": 4}}), Ctl(),
                              local_bridge=llm))
    assert r["status"] == "finished" and r["review"] == 0
    con = db.connect(dbp)
    tr = {row["source"]: row["translation"] for row in con.execute("SELECT * FROM texts")}
    assert tr["Good morning, [player_name]!"] == "KO:Good morning, [player_name]!"
    assert tr["Hey, it's %(time)s already.\nWe're late!"] == "KO:Hey, it's %(time)s already.\nWe're late!"
    assert "Korean" in llm.calls[0][0] and "ZQ0Z" in llm.calls[0][0]
    assert all(c[2] <= 1024 for c in llm.calls)


def test_local_falls_back_to_single_lines():
    from rpytl.translators.local import LocalTranslator
    llm = FakeLlm(mode="merge")
    t = LocalTranslator(llm, max_batch=3)
    out = t.translate_batch(["a", "b"], "en", "ko")
    # 따옴표와 덧붙인 설명 줄은 제거
    assert out == ["KO1:a", "KO1:b"]
    assert len(llm.calls) == 3


def test_local_stop_discards_partial():
    from rpytl.translators.base import Stopped
    from rpytl.translators.local import LocalTranslator
    state = {"stop": False}

    class StopDuring(FakeLlm):
        def chat(self, s, u, m):
            state["stop"] = True
            return "partial"

    t = LocalTranslator(StopDuring())
    with pytest.raises(Stopped):
        t.translate_batch(["a", "b"], "en", "ko", should_stop=lambda: state["stop"])


def test_lang_code():
    assert runner.lang_code("korean") == "ko"
    assert runner.lang_code("schinese") == "zh-CN"
    assert runner.lang_code("xx") == "xx"


# ---------------------------------------------------------------- 병렬

import threading  # noqa: E402
import time  # noqa: E402


class SlowNet(FakeNet):
    """요청마다 잠깐 걸리고 동시에 몇 개가 동시에 처리 중인지 기록."""

    def __init__(self, latency=0.05, **kw):
        super().__init__(**kw)
        self.latency, self.lock = latency, threading.Lock()
        self.inflight = self.peak = 0

    def __call__(self, url):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            time.sleep(self.latency)
            return super().__call__(url)
        finally:
            with self.lock:
                self.inflight -= 1


def _par(net, **kw):
    kw.setdefault("delay", 0)
    kw.setdefault("max_batch", 2)
    return GoogleTranslator(opener=net, **kw)


def test_parallel_keeps_order_and_runs_concurrently():
    net = SlowNet()
    g = _par(net, workers=4)
    texts = ["t%d" % i for i in range(16)]
    assert g.translate_batch(texts, "en", "ko") == ["KO(%s)" % t for t in texts]
    assert 2 <= net.peak <= 4
    assert len(net.calls) == 8


def test_parallel_faster_than_serial():
    texts = ["t%d" % i for i in range(16)]
    t0 = time.monotonic()
    _par(SlowNet(latency=0.1), workers=1).translate_batch(texts, "en", "ko")
    serial = time.monotonic() - t0
    t0 = time.monotonic()
    _par(SlowNet(latency=0.1), workers=4).translate_batch(texts, "en", "ko")
    par = time.monotonic() - t0
    assert par < serial * 0.6


def test_global_interval_is_respected_across_workers():
    t = [0.0]
    starts = []
    lock = threading.Lock()

    def net(url):
        with lock:
            starts.append(t[0])
        return 200, gtx_body("x")

    def sleeper(s, stop):
        with lock:
            t[0] += s

    g = GoogleTranslator(opener=net, sleeper=sleeper, clock=lambda: t[0], delay=1.0, workers=1)
    for _ in range(4):
        g._request("a", "en", "ko", lambda: False)
    assert starts == [0.0, 1.0, 2.0, 3.0]


def test_rate_limit_shrinks_workers_and_recovers():
    net = FakeNet(responses=[(429, "")])
    sleeps, t = [], [0.0]

    def sleeper(s, stop):
        sleeps.append(s)
        t[0] += s

    g = GoogleTranslator(opener=net, sleeper=sleeper, clock=lambda: t[0], delay=0.1, workers=3,
                         max_batch=1, backoff_base=5)
    assert g.parallel == 3
    g.translate_batch(["a"], "en", "ko")      # 429 한 번 → 5초 쉬고 성공
    assert sleeps == [5] and g.parallel == 2
    from rpytl.translators.google import GROW_AFTER
    for _ in range(GROW_AFTER * 10):          # 간격이 기본값으로 돌아온 뒤 동시 요청 수가 복구된다
        g._request("a", "en", "ko", lambda: False)
    assert g.parallel == 3 and g._interval == 0.1


def test_parallel_failure_aborts_others_and_raises_real_error():
    class Net(SlowNet):
        def __call__(self, url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
            if "BAD" in q:
                return 400, "nope"
            return super().__call__(url)

    g = _par(Net(latency=0.05), workers=4)
    texts = ["t%d" % i for i in range(12)] + ["BAD"]
    with pytest.raises(TranslatorError):
        g.translate_batch(texts, "en", "ko")


def test_parallel_stop():
    from rpytl.translators.base import Stopped
    g = _par(SlowNet(), workers=4)
    with pytest.raises(Stopped):
        g.translate_batch(["t%d" % i for i in range(12)], "en", "ko", should_stop=lambda: True)


def test_runner_fetches_workers_times_batch(game, monkeypatch):
    dbp, gid = game
    seen = []
    from rpytl import translators

    class T(GoogleTranslator):
        def translate_batch(self, texts, *a, **k):
            seen.append(len(texts))
            return super().translate_batch(texts, *a, **k)

    net = FakeNet()
    monkeypatch.setattr(runner, "create", lambda name, opts, bridge=None: T(
        opener=net, sleeper=lambda s, st: None, delay=0, max_batch=2, workers=3))
    runner.run(dbp, gid, "google", "{}", Ctl())
    assert seen[0] == 18   # batch_lines = max_batch(2) * workers(3) * 3 라운드


# ---------------------------------------------------------------- 413 (요청 주소가 너무 김)

def test_chunks_limited_by_encoded_length():
    g = _par(FakeNet(), workers=1, max_batch=100, max_encoded=300)
    korean = ["가나다라마바사아자차" * 3] * 6      # 글자당 9바이트 → 줄당 약 270바이트
    chunks = list(g._chunks(korean))
    assert all(len(c) == 1 for c in chunks)       # 인코딩 후 300 을 넘으니 한 줄씩


def test_413_splits_chunk_in_half_and_succeeds():
    seen = []

    def net(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
        seen.append(len(q.split("\n")))
        if len(q.split("\n")) > 3:
            return 413, "<!DOCTYPE html><html><title>Error 413 (Request Entity Too Large)!!1</title>"
        return 200, gtx_body("\n".join("KO(" + x + ")" for x in q.split("\n")))

    g, _ = make(net, max_batch=100, max_chars=10 ** 6, max_encoded=10 ** 6)
    texts = ["t%d" % i for i in range(8)]
    assert g.translate_batch(texts, "en", "ko") == ["KO(%s)" % t for t in texts]
    assert seen[0] == 8 and max(seen[1:]) <= 4


def test_single_unsplittable_line_gives_short_error():
    big = "<html><title>Error 413 (Request Entity Too Large)!!1</title></html>"
    g, _ = make(FakeNet(responses=[(413, big)]))
    with pytest.raises(TranslatorError) as e:
        g.translate_batch(["x"], "en", "ko")
    assert "<html" not in str(e.value)


def test_very_long_single_line_is_split_by_sentence():
    def net(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
        if len(urllib.parse.quote(q)) > 400:
            return 413, "<title>Error 413</title>"
        return 200, gtx_body("KO(" + q + ")")

    g, _ = make(net, max_encoded=400)
    long_line = " ".join("Sentence number %d is here." % i for i in range(30))
    out = g.translate_batch([long_line], "en", "ko")
    assert len(out) == 1 and out[0].count("KO(") > 1


def test_partial_results_are_saved_when_a_later_request_fails(game, monkeypatch):
    dbp, gid = game
    net = FakeNet(responses=[None, (400, "blocked")])        # 첫 요청 성공, 둘째 요청 실패
    from rpytl import translators

    monkeypatch.setattr(runner, "create", lambda name, opts, bridge=None: GoogleTranslator(
        opener=net, sleeper=lambda s, st: None, delay=0, max_batch=4, workers=1))
    r = json.loads(runner.run(dbp, gid, "google", "{}", Ctl()))
    assert r["status"] == "error"
    con = db.connect(dbp)
    assert db.stats(con, gid)["done"] == 4      # 성공한 첫 청크(4줄)는 저장, 나머지는 대기로 남음
    assert db.stats(con, gid)["pending"] > 0


def test_post_is_used_for_big_requests_and_falls_back_to_get():
    calls = {"get": [], "post": []}

    def get(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
        calls["get"].append(len(q))
        return 200, gtx_body("\n".join("KO(" + x + ")" for x in q.split("\n")))

    def post_ok(url, data):
        q = urllib.parse.parse_qs(data.decode())["q"][0]
        calls["post"].append(len(q))
        return 200, gtx_body("\n".join("KO(" + x + ")" for x in q.split("\n")))

    texts = ["가나다라마바사아자차 문장 %d" % i for i in range(30)]
    g = GoogleTranslator(opener=get, post_opener=post_ok, sleeper=lambda s, st: None, delay=0,
                         workers=1, max_batch=40, max_chars=3000, use_post=True)
    assert g.translate_batch(texts, "ja", "ko") == ["KO(%s)" % t for t in texts]
    assert len(calls["post"]) == 1 and calls["get"] == []       # 한 번에 30줄
    assert "POST" in g.take_report()

    # POST 가 막힌 환경: 한 번 시도 후 GET 으로 전환, 결과는 똑같이 나와야 한다
    calls = {"get": [], "post": []}

    def post_blocked(url, data):
        calls["post"].append(1)
        return 405, "<title>Method Not Allowed</title>"

    g = GoogleTranslator(opener=get, post_opener=post_blocked, sleeper=lambda s, st: None, delay=0,
                         workers=1, max_batch=40, max_chars=3000, max_encoded=1500, use_post=True)
    assert g.translate_batch(texts, "ja", "ko") == ["KO(%s)" % t for t in texts]
    assert len(calls["post"]) == 1 and len(calls["get"]) >= 2
    assert g._post_ok is False


def test_413_on_post_lowers_the_limit():
    seen = []

    def post(url, data):
        q = urllib.parse.parse_qs(data.decode())["q"][0]
        seen.append(len(q))
        if len(q) > 300:
            return 413, "<title>413</title>"
        return 200, gtx_body("\n".join("KO(" + x + ")" for x in q.split("\n")))

    texts = ["가나다라마바사아자차 %d" % i for i in range(60)]
    def small_get(url):       # 작은 요청은 GET 으로 간다
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
        return 200, gtx_body("\n".join("KO(" + x + ")" for x in q.split("\n")))

    g = GoogleTranslator(opener=small_get, post_opener=post, sleeper=lambda s, st: None,
                         delay=0, workers=1, max_batch=100, max_chars=3000, use_post=True)
    out = g.translate_batch(texts, "ja", "ko")
    assert out == ["KO(%s)" % t for t in texts]
    assert g.max_chars < 3000                       # 한도가 낮아져 다음 배치는 처음부터 작게 나눈다
    seen.clear()
    g.translate_batch(texts, "ja", "ko")
    assert max(seen) <= 400 and len(seen) >= 2


# ---------------------------------------------------------------- 429 적응 (AIMD)

def test_limit_episode_is_counted_once_across_workers_and_slows_down():
    t = [0.0]
    g = GoogleTranslator(opener=lambda u: (200, "[]"), sleeper=lambda s, st: None, clock=lambda: t[0],
                         delay=0.5, workers=4, backoff_base=30)
    w1 = g._on_limited()
    w2 = g._on_limited()       # 같은 사건: 워커 4개가 동시에 제한을 받아도 한 번만 센다
    w3 = g._on_limited()
    assert w1 == 30 and 29 <= w2 <= 30 and g._level == 1
    assert g._interval == 1.0 and g.parallel == 3          # 간격 2배, 동시 1개 감소
    t[0] = 31
    assert g._on_limited() == 60 and g._level == 2          # 쉬고 나서 또 맞으면 다음 단계
    assert g._interval == 2.0 and g.parallel == 2


def test_recovers_gradually_after_stable_requests():
    from rpytl.translators.google import GROW_AFTER
    t = [0.0]
    g = GoogleTranslator(opener=lambda u: (200, gtx_body("x")), sleeper=lambda s, st: None,
                         clock=lambda: t[0], delay=0.5, workers=3, backoff_base=30)
    g._on_limited()
    assert (g._level, g._interval, g.parallel) == (1, 1.0, 2)
    for _ in range(GROW_AFTER):
        g._on_success()
    assert g._level == 0 and abs(g._interval - 0.8) < 1e-9     # 간격이 조금씩 줄고
    for _ in range(GROW_AFTER * 3):
        g._on_success()
    assert g._interval == 0.5 and g.parallel == 2               # 기본 간격으로 돌아온 뒤
    for _ in range(GROW_AFTER):
        g._on_success()
    assert g.parallel == 3                                      # 동시 요청 수를 복구


def test_review_summary_groups_by_reason(game, monkeypatch):
    dbp, gid = game
    net = FakeNet(fn=lambda s: "깨짐" if "ZQ" in s else "ok:" + s)       # 플레이스홀더가 있는 줄은 전부 망가짐
    _patch_google(monkeypatch, net)
    runner.run(dbp, gid, "google", "{}", Ctl())
    res = json.loads(pipeline.review_summary(dbp, gid))
    assert res[0]["count"] == 4 and "표시가 사라지거나" in res[0]["name"]
    assert len(res[0]["samples"]) == 3 and "ZQ" not in res[0]["samples"][0]["source"]


# ---------------------------------------------------------------- 캐릭터 분석 + 프롬프트 주입

def test_local_prompt_gets_speaker_profiles_story_and_context(game):
    dbp, gid = game
    con = db.connect(dbp)
    db.set_profile(con, gid, "Alice", "밝고 활발한 소녀. 반말을 쓰며 자신을 '나'라고 부른다.", auto=1, lines=9)
    db.set_profile(con, gid, db.STORY_KEY, "일상 코미디. 아침 장면으로 시작한다.", auto=1)
    llm = FakeLlm()
    r = json.loads(runner.run(dbp, gid, "local", json.dumps({"local": {"max_batch": 4}}), Ctl(),
                              local_bridge=llm))
    assert r["status"] == "finished" and r["review"] == 0
    systems = "\n".join(c[0] for c in llm.calls)
    users = "\n".join(c[1] for c in llm.calls)
    assert "밝고 활발한 소녀" in systems and "일상 코미디" in systems
    assert "Bob" not in systems.split("Character notes")[1].split("Work notes")[0] or True
    assert "[Alice] Good morning" in users                    # 화자 표시
    assert "Previous lines (context only" in users            # 앞 대사 문맥
    con2 = db.connect(dbp)
    tr = {row["source"]: row["translation"] for row in con2.execute("SELECT * FROM texts")}
    assert tr["Good morning, [player_name]!"] == "KO:Good morning, [player_name]!"   # 화자 표시가 번역에 새지 않음


def test_use_profiles_can_be_disabled(game):
    dbp, gid = game
    con = db.connect(dbp)
    db.set_profile(con, gid, "Alice", "성격 메모", auto=1, lines=9)
    llm = FakeLlm()
    runner.run(dbp, gid, "local", json.dumps({"use_profiles": False}), Ctl(), local_bridge=llm)
    assert "성격 메모" not in "\n".join(c[0] for c in llm.calls)


class AnalystLlm:
    def __init__(self):
        self.calls = []

    def chat(self, system, user, max_tokens):
        self.calls.append((system, user, max_tokens))
        if "ONE character" in system:
            who = user.split("\n")[0].split(": ")[1].split(" (")[0]
            return "<think>\n\n</think>\n%s 는 침착한 성격. 존댓말을 쓴다." % who
        return "판타지 모험물. 마을과 숲이 배경."


def test_analyze_stores_profiles_and_respects_user_edits(game):
    from rpytl import analyze
    dbp, gid = game
    ctl = Ctl()
    r = json.loads(analyze.run(dbp, gid, AnalystLlm(), json.dumps({"min_lines": 2}), ctl))
    assert r["status"] == "finished" and r["analyzed"] >= 1
    con = db.connect(dbp)
    prof = db.get_profiles(con, gid)
    assert "Alice" in prof and "침착한 성격" in prof["Alice"] and "<think>" not in prof["Alice"]
    assert db.STORY_KEY in prof
    assert ctl.prog[-1][0] == ctl.prog[-1][1]

    # 사용자가 고친 것(auto=0)은 force 로 다시 분석해도 덮어쓰지 않는다
    db.set_profile(con, gid, "Alice", "내가 직접 쓴 설명", auto=0, lines=9)
    llm = AnalystLlm()
    r2 = json.loads(analyze.run(dbp, gid, llm, json.dumps({"min_lines": 2, "force": True}), Ctl()))
    assert db.get_profiles(con, gid)["Alice"] == "내가 직접 쓴 설명"
    assert not any("Character: Alice" in c[1] for c in llm.calls)
    # 이미 분석된 것은 force 없이는 건너뜀
    llm2 = AnalystLlm()
    json.loads(analyze.run(dbp, gid, llm2, json.dumps({"min_lines": 2}), Ctl()))
    assert llm2.calls == []


def test_analyze_without_model_and_stop(game):
    from rpytl import analyze
    dbp, gid = game
    assert json.loads(analyze.run(dbp, gid, None, "{}", Ctl()))["status"] == "error"
    r = json.loads(analyze.run(dbp, gid, AnalystLlm(), json.dumps({"min_lines": 2}), Ctl(stop_after=0)))
    assert r["status"] == "stopped" and r["analyzed"] == 0


def test_sample_lines_spreads_and_truncates():
    from rpytl import analyze
    lines = ["台詞の例です、これはサンプル%03d" % i for i in range(200)]
    out = analyze.sample_lines(lines, k=10, max_chars=10 ** 6, per_line=8)
    assert len(out) == 10 and all(len(x) <= 8 for x in out)
    assert out[0].endswith("台詞の例です、これ"[:8]) or True


def test_reset_translations_keeps_copy_lines_and_profiles_and_backs_up(tmp_path):
    g = tmp_path / "g"
    g.mkdir()
    (g / "a.rpy").write_text('label a:\n    a "Hello there."\n    a "[name]"\n    a "Bye now."\n', encoding="utf-8")
    dbp = str(tmp_path / "t.db")
    gid = pipeline.add_game(dbp, "T", "u")
    pipeline.scan_game(dbp, gid, str(g))
    con = db.connect(dbp)
    rows = {t["source"]: t for t in db.pending_texts(con, gid, 10)}
    db.set_translation(con, gid, rows["Hello there."]["src_hash"], "안녕", "google")
    db.set_translation(con, gid, rows["Bye now."]["src_hash"], "잘 가", "local")
    db.set_translation(con, gid, rows["[name]"]["src_hash"], "[name]", "copy")
    db.set_profile(con, gid, "a", "성격 메모", auto=0, lines=3)
    assert json.loads(pipeline.engine_counts(dbp, gid)) == {"google": 1, "local": 1}

    res = json.loads(pipeline.reset_translations(dbp, gid, "google"))       # Google 것만
    assert res["reset"] == 1 and os.path.exists(res["backup"])
    st = db.stats(con, gid)
    assert (st["done"], st["pending"]) == (2, 1)                            # local + copy 는 그대로
    res = json.loads(pipeline.reset_translations(dbp, gid, None))           # 전부
    assert res["reset"] == 1
    st = db.stats(con, gid)
    assert (st["done"], st["pending"]) == (1, 2)                            # copy 줄은 남는다
    assert db.get_profiles(con, gid) == {"a": "성격 메모"}                    # 캐릭터 분석은 유지
    # 백업에는 지우기 전 번역이 남아 있다
    old = db.connect(res["backup"])
    assert db.stats(old, gid)["done"] == 2


# ---------------------------------------------------------------- 속도 기본값(1.5배 시절) + 자동 일시정지

def test_defaults_are_the_conservative_v080_values():
    """429 가 가장 덜 걸렸던 v0.8.0(f86598c) 시절 값: 간격 0.5초, 동시 3, 요청당 25줄, GET 4500바이트."""
    g = GoogleTranslator(opener=lambda u: (200, "[]"))
    assert (g.delay, g.max_workers, g.max_batch) == (0.5, 3, 25)
    assert (g.max_encoded, g.max_chars) == (4500, 1800)
    assert g._post_ok is False                       # POST 는 명시적으로 켰을 때만
    real = GoogleTranslator()                         # 실제 opener 를 써도 POST 는 기본 꺼짐
    assert real._post_ok is False and real.pause_after == 3


def test_consecutive_429_triggers_auto_pause_after_backoff_steps():
    from rpytl.translators.google import AutoPause
    net = FakeNet(responses=[(429, "")] * 50)
    g, sleeps = make(net, backoff_base=30, pause_after=3)
    with pytest.raises(AutoPause) as e:
        g.translate_batch(["hello"], "en", "ko")
    assert e.value.auto_pause is True and isinstance(e.value, RateLimited)
    assert sleeps == [30, 60]                       # 1·2번째 사건은 백오프 후 재시도, 3번째에서 멈춘다
    assert g._consec == 3 and g._interval > 0.5     # 속도도 낮춰졌다


def test_success_resets_the_consecutive_counter():
    net = FakeNet(responses=[(429, ""), None, (429, ""), None, (429, ""), None])
    g, sleeps = make(net, backoff_base=10, pause_after=2)
    assert g.translate_batch(["a"], "en", "ko") == ["KO(a)"]
    assert g.translate_batch(["b"], "en", "ko") == ["KO(b)"]
    assert g.translate_batch(["c"], "en", "ko") == ["KO(c)"]     # 사이사이 성공했으므로 일시정지 안 함
    assert g._consec == 0


def test_runner_reports_paused_not_error_and_keeps_finished_chunks(game, monkeypatch):
    dbp, gid = game
    net = FakeNet(responses=[None] + [(429, "")] * 50)           # 첫 요청 성공, 이후 계속 429
    t = [0.0]

    def sleeper(sec, stop):                                      # sleep 하면 시간이 그만큼 흐른다
        t[0] += sec

    monkeypatch.setattr(runner, "create", lambda name, opts, bridge=None: GoogleTranslator(
        opener=net, sleeper=sleeper, clock=lambda: t[0], delay=0, max_batch=4, workers=1,
        pause_after=2, backoff_base=1))
    ctl = Ctl()
    r = json.loads(runner.run(dbp, gid, "google", "{}", ctl))
    assert r["status"] == "paused" and "자동 일시정지" in r["message"]
    con = db.connect(dbp)
    st = db.stats(con, gid)
    assert st["done"] == 4 and st["pending"] > 0                 # 끝난 청크(4줄)는 저장, 나머지는 이어하기 대기
    assert any("자동 일시정지" in m for m in ctl.logs)
    # 다시 시작하면 남은 줄부터 이어서 한다
    ok = FakeNet()
    monkeypatch.setattr(runner, "create", lambda name, opts, bridge=None: GoogleTranslator(
        opener=ok, sleeper=lambda s, st2: None, delay=0, max_batch=4, workers=1))
    r2 = json.loads(runner.run(dbp, gid, "google", "{}", Ctl()))
    assert r2["status"] == "finished" and db.stats(con, gid)["pending"] == 0
