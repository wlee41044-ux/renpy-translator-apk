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
