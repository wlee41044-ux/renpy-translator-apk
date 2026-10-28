import pytest

from rpytl.protect import RestoreError, check_translation, protect, restore


def test_mask_and_restore_all_kinds():
    src = "Hi [name], {i}really{/i}? {color=#f00}%s{/color} %(n)d%%\nBye {{x}} [[y]"
    p = protect(src)
    assert "[name]" not in p.masked and "{i}" not in p.masked and "%s" not in p.masked
    assert p.translatable
    # 번역기가 순서를 바꾸고 공백을 넣었다고 가정
    fake = p.masked.replace("Hi", "안녕").replace("really", "정말").replace("Bye", "잘가")
    fake = fake.replace("ZQ0Z", "zq 0 z")
    assert restore(fake, p.tokens) == src.replace("Hi", "안녕").replace("really", "정말") \
        .replace("Bye", "잘가")


def test_adjacent_tokens_merged():
    p = protect("{b}{i}Hello{/i}{/b}")
    assert p.tokens == ["{b}{i}", "{/i}{/b}"]
    assert p.masked == "ZQ0ZHelloZQ1Z"


def test_fullwidth_placeholder_restored():
    p = protect("[a] and [b]")
    assert restore("ＺＱ１Ｚ 그리고 ZQ0Z", p.tokens) == "[b] 그리고 [a]"


def test_missing_or_duplicated_placeholder_fails():
    p = protect("Hello [name]!")
    with pytest.raises(RestoreError):
        restore("안녕!", p.tokens)
    with pytest.raises(RestoreError):
        restore("ZQ0Z ZQ0Z 안녕", p.tokens)
    with pytest.raises(RestoreError):
        restore("ZQ5Z 안녕", p.tokens)


def test_newline_spaces_trimmed():
    p = protect("Line one.\nLine two.")
    assert restore("첫 줄. ZQ0Z 둘째 줄.", p.tokens) == "첫 줄.\n둘째 줄."


def test_untranslatable():
    assert not protect("[name]").translatable
    assert not protect("{w=0.5}...{nw}").translatable
    assert protect("Hi {w} there").translatable


def test_check_translation():
    assert check_translation("Hi [a] {b}x{/b}", "{b}x{/b} [a] 안녕")
    assert not check_translation("Hi [a]", "안녕")


# ---------------------------------------------------------------- 구조 검사 (게임이 죽는 번역 막기)

from rpytl.protect import structure_problem  # noqa: E402


def test_bracket_escape_lost_is_caught():
    """실제 사고: 원문 '[[Battle Victory] ...' 가 번역에서 '[전투 승리] ...' 가 되어 Ren'Py 가
    '전투 승리' 를 코드로 실행하다 SyntaxError 로 멈췄다."""
    src = "[[Battle Victory] You completely defeated [current_enemy.name]!"
    bad = "[전투 승리] [current_enemy.name]를 완전히 물리쳤습니다!"
    good = "[[전투 승리] [current_enemy.name]를 완전히 물리쳤습니다!"
    assert structure_problem(src, bad) is not None
    assert structure_problem(src, good) is None


def test_translator_added_brackets_or_braces_are_caught():
    assert structure_problem("Hello", "[안녕]") is not None
    assert structure_problem("Hello", "안녕 {") is not None
    assert structure_problem("100% done", "100% 완료") is None
    assert structure_problem("Hi [name]", "안녕 [name]") is None
    assert structure_problem("Hi [name]", "안녕 [이름]") is not None
    assert structure_problem("{b}Hi{/b}", "{b}안녕{/b}") is None
    assert structure_problem("{b}Hi{/b}", "{b}안녕") is not None


def test_runner_sends_broken_structure_to_review(tmp_path, monkeypatch):
    import json
    from rpytl import db, pipeline, runner
    g = tmp_path / "g"
    g.mkdir()
    (g / "a.rpy").write_text('label a:\n    "[[Battle Victory] Defeated [enemy]!"\n', encoding="utf-8")
    dbp = str(tmp_path / "t.db")
    gid = pipeline.add_game(dbp, "T", "u")
    pipeline.scan_game(dbp, gid, str(g))

    class FakeGoogle:
        name, max_batch, parallel, batch_lines = "google", 10, 1, 10
        log = lambda self, m: None  # noqa: E731

        def translate_batch(self, texts, *a, **k):
            # 플레이스홀더는 모두 지켰지만 번역기가 자기 멋대로 [..] 를 끼워 넣은 번역
            return ["ZQ0Z전투 승리] ZQ1Z를 물리쳤다! [추가]" for _ in texts]

        def take_report(self):
            return ""

        def close(self):
            pass

    monkeypatch.setattr(runner, "create", lambda *a, **k: FakeGoogle())
    r = json.loads(runner.run(dbp, gid, "google", "{}", None))
    con = db.connect(dbp)
    assert r["status"] == "finished" and r["review"] == 1
    rv = db.review_list(con, gid)
    assert "구조 불일치" in rv[0]["note"]


def test_export_moves_already_saved_unsafe_translations_to_review(tmp_path):
    import json
    from rpytl import db, pipeline
    g = tmp_path / "g"
    g.mkdir()
    (g / "a.rpy").write_text('label a:\n    "[[Battle Victory] Defeated [enemy]!"\n    "Safe line."\n',
                             encoding="utf-8")
    dbp = str(tmp_path / "t.db")
    gid = pipeline.add_game(dbp, "T", "u")
    pipeline.scan_game(dbp, gid, str(g))
    con = db.connect(dbp)
    for t in db.pending_texts(con, gid, 10):
        tr = "[전투 승리] [enemy]를 물리쳤다!" if "Battle" in t["source"] else "안전한 줄."
        db.set_translation(con, gid, t["src_hash"], tr, "google")
    res = json.loads(pipeline.export(dbp, gid, str(tmp_path / "out")))
    assert res["info"]["unsafe_moved_to_review"] == 1
    out = (tmp_path / "out/tl/korean/rpytl/a.rpy").read_text(encoding="utf-8")
    assert "안전한 줄." in out and "전투 승리" not in out
    assert db.stats(con, gid)["review"] == 1


def test_percent_followed_by_space_is_not_a_format_spec():
    p = protect("50% damage and 100% done")
    assert p.tokens == []
    p = protect("HP: %d / %(max)s, 5%s")
    assert p.tokens == ["%d", "%(max)s", "%s"]
