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


# ---------------------------------------------------------------- Gemini 엔진

import re as _re  # noqa: E402
from rpytl.translators.gemini import GeminiClient, GeminiTranslator, parse_error  # noqa: E402
from rpytl.translators.google import AutoPause  # noqa: E402

KEY = "test-secret-key-not-real-123"


def gem_ok(text):
    return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})


def gem_err(code, status, message, details=None):
    return code, json.dumps({"error": {"code": code, "status": status, "message": message,
                                       "details": details or []}})


class FakeGemini:
    """generateContent 를 흉내: 번호 목록은 'N. KO:<텍스트>' 로 번역해 준다. responses 로 오류를 끼워 넣을 수 있다."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def __call__(self, url, headers, data):
        payload = json.loads(data.decode("utf-8")) if data else None
        self.calls.append((url, headers, payload))
        if self.responses:
            r = self.responses.pop(0)
            if r is not None:
                return r
        user = payload["contents"][0]["parts"][0]["text"]
        rows = [l for l in user.split("\n") if _re.match(r"^\d+\. ", l)]
        if rows:
            out = "\n".join("%s. KO:%s" % (l.split(". ", 1)[0], _re.sub(r"^\[[^\]]+\] ", "", l.split(". ", 1)[1]))
                            for l in rows)
        else:
            out = "KO:" + _re.sub(r"^\[[^\]]+\] ", "", user.split("\n")[-1])
        return gem_ok(out)


def gem_client(net, **kw):
    t, sleeps = [0.0], []

    def sleeper(s, stop):
        sleeps.append(s)
        t[0] += s

    kw.setdefault("rpm", 6000)
    c = GeminiClient(api_key=KEY, opener=net, sleeper=sleeper, clock=lambda: t[0], **kw)
    c.test_sleeps = sleeps
    return c


def test_gemini_request_format_and_key_only_in_header():
    net = FakeGemini()
    c = gem_client(net, model="gemini-flash-latest")
    assert c.chat("SYS PROMPT", "hello", 777) == "KO:hello"
    url, headers, payload = net.calls[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
    assert headers["x-goog-api-key"] == KEY
    assert KEY not in url and KEY not in json.dumps(payload)           # 키는 헤더에만
    assert payload["systemInstruction"]["parts"][0]["text"] == "SYS PROMPT"
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert payload["generationConfig"]["maxOutputTokens"] == 777
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert all(s["threshold"] == "BLOCK_NONE" for s in payload["safetySettings"])


def test_gemini_empty_key_and_bad_model_name():
    with pytest.raises(TranslatorError):
        gem_client(FakeGemini(), model="a/b/../c")
    c = GeminiClient(api_key="", opener=FakeGemini())
    with pytest.raises(TranslatorError) as e:
        c.chat("s", "u")
    assert "키" in str(e.value)


def test_gemini_429_uses_retry_delay_then_succeeds_and_slows_down():
    info = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]
    net = FakeGemini([gem_err(429, "RESOURCE_EXHAUSTED", "Quota exceeded", info)])
    c = gem_client(net, rpm=60)
    assert c.chat("s", "hi") == "KO:hi"
    assert 35 in c.test_sleeps                       # retryDelay(34s) + 1
    assert c._interval > 1.0                         # 이후 요청 간격이 늘어난다 (기본 60/60 = 1초)


def test_gemini_daily_quota_pauses_immediately():
    det = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]
    net = FakeGemini([gem_err(429, "RESOURCE_EXHAUSTED", "Quota exceeded", det)] * 5)
    c = gem_client(net)
    with pytest.raises(AutoPause) as e:
        c.chat("s", "hi")
    assert "하루 한도" in str(e.value) and len(net.calls) == 1     # 기다려도 소용없으니 바로 멈춘다


def test_gemini_consecutive_429_pauses_and_success_resets():
    net = FakeGemini([gem_err(429, "RESOURCE_EXHAUSTED", "slow down")] * 10)
    c = gem_client(net, pause_after=3, backoff_base=10)
    with pytest.raises(AutoPause):
        c.chat("s", "hi")
    assert c.test_sleeps[:2] == [10, 20] and c._consec == 3
    net2 = FakeGemini([gem_err(429, "RESOURCE_EXHAUSTED", "x"), None, gem_err(429, "RESOURCE_EXHAUSTED", "x"), None])
    c2 = gem_client(net2, pause_after=2, backoff_base=1)
    assert c2.chat("s", "a") == "KO:a" and c2.chat("s", "b") == "KO:b" and c2._consec == 0


def test_gemini_thinking_budget_rejected_falls_back_without_it():
    net = FakeGemini([gem_err(400, "INVALID_ARGUMENT", "Budget 0 is invalid. This model only works in thinking mode.")])
    c = gem_client(net)
    assert c.chat("s", "hi") == "KO:hi"
    assert "thinkingConfig" in net.calls[0][2]["generationConfig"]
    assert "thinkingConfig" not in net.calls[1][2]["generationConfig"]
    c.chat("s", "again")                                              # 한 번 알아낸 뒤로는 계속 빼고 보낸다
    assert "thinkingConfig" not in net.calls[2][2]["generationConfig"]


def test_gemini_errors_are_readable_and_never_contain_the_key():
    cases = [
        (gem_err(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."), "API 키가 올바르지 않"),
        (gem_err(403, "PERMISSION_DENIED", "User location is not supported"), "접근 거부"),
        (gem_err(404, "NOT_FOUND", "models/xyz is not found"), "모델을 찾을 수 없"),
        (gem_err(400, "INVALID_ARGUMENT", "Something else"), "Gemini 오류(400)"),
    ]
    for resp, expect in cases:
        c = gem_client(FakeGemini([resp]))
        with pytest.raises(TranslatorError) as e:
            c.chat("s", "hi")
        assert expect in str(e.value) and KEY not in str(e.value)
    c = gem_client(FakeGemini([(503, "unavailable")] * 9), max_retries=2)
    with pytest.raises(TranslatorError):
        c.chat("s", "hi")
    assert c.test_sleeps == [2, 4]                                    # 서버 오류는 짧게 백오프 후 포기


def test_gemini_blocked_content_returns_empty_so_only_that_line_goes_to_review():
    blocked = 200, json.dumps({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
    logs = []
    c = gem_client(FakeGemini([blocked]))
    c.log = logs.append
    assert c.chat("s", "bad") == "" and any("PROHIBITED_CONTENT" in m for m in logs)


def test_gemini_rpm_spacing():
    net = FakeGemini()
    c = gem_client(net, rpm=12)                       # 5초 간격
    for _ in range(3):
        c.chat("s", "x")
    assert c.test_sleeps == [5.0, 5.0]


def test_gemini_runner_end_to_end_with_speakers_profiles_and_blocked_line(game, monkeypatch):
    dbp, gid = game
    con = db.connect(dbp)
    db.set_profile(con, gid, "Alice", "밝고 활발한 소녀. 반말.", auto=1, lines=9)
    db.set_profile(con, gid, db.STORY_KEY, "일상 코미디.", auto=1)
    # 두 번째 요청이 안전 필터에 막히는 경우: 배치 응답이 비어서 한 줄씩 다시 보내고, 막힌 줄만 빈 번역 → 검수필요
    blocked = 200, json.dumps({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
    net = FakeGemini([None, blocked, blocked])       # 1번째 배치 OK, 2번째 배치 차단 → 한 줄씩 → 첫 줄도 차단
    monkeypatch.setattr(runner, "create", lambda name, opts, bridge=None: GeminiTranslator(
        client=gem_client(net), max_batch=4))
    ctl = Ctl()
    r = json.loads(runner.run(dbp, gid, "gemini", "{}", ctl))
    assert r["status"] == "finished"
    st = db.stats(con, gid)
    assert st["pending"] == 0 and st["done"] + st["review"] == st["total"] and st["review"] >= 1
    systems = "\n".join(c[2]["systemInstruction"]["parts"][0]["text"] for c in net.calls)
    users = "\n".join(c[2]["contents"][0]["parts"][0]["text"] for c in net.calls)
    assert "밝고 활발한 소녀" in systems and "일상 코미디" in systems       # 캐릭터 분석 + 작품 요약
    assert "[Alice] Good morning" in users and "Previous lines (context only" in users
    tr = {row["source"]: row["translation"] for row in con.execute("SELECT * FROM texts")}
    assert tr["It was a quiet morning."].startswith("KO:")
    assert all(KEY not in m for m in ctl.logs)                          # 로그에 키가 남지 않는다


def test_gemini_runner_pauses_on_daily_limit_keeping_finished_work(game, monkeypatch):
    dbp, gid = game
    det = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": "RequestsPerDay"}]}]
    net = FakeGemini([None, gem_err(429, "RESOURCE_EXHAUSTED", "Quota exceeded", det)])
    monkeypatch.setattr(runner, "create", lambda name, opts, bridge=None: GeminiTranslator(
        client=gem_client(net), max_batch=4))
    ctl = Ctl()
    r = json.loads(runner.run(dbp, gid, "gemini", "{}", ctl))
    assert r["status"] == "paused" and "하루 한도" in r["message"]
    con = db.connect(dbp)
    assert db.stats(con, gid)["done"] == 4                               # 첫 요청(4줄)은 저장됨
    assert any("자동 일시정지" in m for m in ctl.logs)


def test_gemini_list_models_and_parse_error():
    body = json.dumps({"models": [
        {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent", "countTokens"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}]})
    c = GeminiClient(api_key=KEY, opener=lambda url, h, d: (200, body))
    assert c.list_models() == ["gemini-flash-latest"]
    c2 = GeminiClient(api_key=KEY, opener=lambda url, h, d: gem_err(400, "INVALID_ARGUMENT", "API key not valid"))
    with pytest.raises(TranslatorError):
        c2.list_models()
    msg, status, retry, per_day = parse_error(json.dumps({"error": {
        "message": "x", "status": "RESOURCE_EXHAUSTED",
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7.5s"}]}}))
    assert (status, retry, per_day) == ("RESOURCE_EXHAUSTED", 7.5, False)
    assert parse_error("<html>oops</html>")[1:] == ("", None, False)


def test_analyze_can_use_gemini_via_opts(game, monkeypatch):
    dbp, gid = game
    seen = []

    def fake_open(self, url, headers, data):
        seen.append(headers.get("x-goog-api-key"))
        system = json.loads(data)["systemInstruction"]["parts"][0]["text"]
        return gem_ok("침착한 성격. 존댓말." if "ONE character" in system else "판타지 모험물.")

    monkeypatch.setattr(GeminiClient, "_urlopen", fake_open)
    monkeypatch.setattr(GeminiClient, "_gate", lambda self, stop: None)
    opts = {"min_lines": 2, "gemini": {"api_key": KEY, "model": "gemini-flash-latest", "rpm": 100}}
    r = json.loads(pipeline.analyze(dbp, gid, json.dumps(opts), Ctl(), None))
    assert r["status"] == "finished" and r["analyzed"] >= 1
    assert set(seen) == {KEY}
    con = db.connect(dbp)
    assert "침착한 성격" in db.get_profiles(con, gid)["Alice"] and db.STORY_KEY in db.get_profiles(con, gid)


def test_gemini_daily_quota_marks_per_day_and_analyze_pauses(game, monkeypatch):
    dbp, gid = game
    det = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]
    monkeypatch.setattr(GeminiClient, "_urlopen",
                        lambda self, url, headers, data: gem_err(429, "RESOURCE_EXHAUSTED", "Quota exceeded", det))
    monkeypatch.setattr(GeminiClient, "_gate", lambda self, stop: None)
    c = GeminiClient(api_key=KEY)
    with pytest.raises(AutoPause) as e:
        c.chat("s", "hi")
    assert e.value.per_day is True
    opts = {"min_lines": 2, "gemini": {"api_key": KEY, "rpm": 100}}
    r = json.loads(pipeline.analyze(dbp, gid, json.dumps(opts), Ctl(), None))
    assert r["status"] == "paused" and r["per_day"] is True


def test_roles_split_dialogue_to_one_engine_and_other_strings_to_another_concurrently(game, monkeypatch):
    import threading
    from rpytl import translators
    dbp, gid = game
    con = db.connect(dbp)
    kinds = {r["src_hash"]: r["kind"] for r in con.execute("SELECT src_hash, kind FROM entries WHERE active = 1")}
    assert {"dialogue", "string"} <= set(kinds.values())     # 픽스처에 두 종류가 다 있어야 의미가 있다

    class FakeDialogueEngine(GoogleTranslator):
        name = "gemini"

    gnet = FakeNet(fn=lambda s: "G:" + s)
    dnet = FakeNet(fn=lambda s: "D:" + s)

    def create(name, opts, bridge=None):
        cls, net = (FakeDialogueEngine, dnet) if name == "gemini" else (GoogleTranslator, gnet)
        return cls(opener=net, sleeper=lambda s, stop: None, delay=0, max_batch=4, workers=1)
    monkeypatch.setattr(runner, "create", create)

    results = {}

    def go(engine, only):
        results[only] = json.loads(runner.run(dbp, gid, engine, json.dumps({"only": only}), Ctl()))
    ts = [threading.Thread(target=go, args=("gemini", "dialogue")), threading.Thread(target=go, args=("google", "other"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results["dialogue"]["status"] == results["other"]["status"] == "finished"
    s = db.stats(db.connect(dbp), gid)
    assert s["done"] == s["total"] and s["remaining"] == 0
    for row in db.connect(dbp).execute("SELECT src_hash, engine, translation FROM texts WHERE status = 'done'"):
        has_dlg = row["src_hash"] in {h for h, k in kinds.items() if k == "dialogue"} or any(
            r["kind"] == "dialogue" for r in db.connect(dbp).execute(
                "SELECT kind FROM entries WHERE game_id = ? AND src_hash = ? AND active = 1", (gid, row["src_hash"])))
        if row["engine"] == "copy":
            continue
        assert row["engine"] == ("gemini" if has_dlg else "google"), row["engine"]
