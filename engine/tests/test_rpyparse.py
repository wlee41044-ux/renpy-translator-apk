import os

from rpytl.model import DIALOGUE, STRING, say_code, translate_digest
from rpytl.rpyparse import extract_from_rpy, logical_lines, renpy_string, tlid_config_override
from rpytl.version import parse_renpy_version, tlid_only_considers_say

DATA = os.path.join(os.path.dirname(__file__), "data")


def _dl(items):
    return [i for i in items if i.kind == DIALOGUE]


def test_known_renpy_identifiers():
    # Ren'Py 가 실제로 생성한 식별자 (Ren'Py 샘플 게임 the_question / tutorial 의 tl 파일에서 확인한 값)
    src = (
        'label start:\n'
        '    "It\'s only when I hear the sounds of shuffling feet and supplies being put away '
        'that I realize that the lecture\'s over."\n'
        '    "Professor Eileen\'s lectures are usually interesting, but today I just couldn\'t '
        'concentrate on it."\n'
        'label tutorial_dialogue:\n'
        '    "Wow, it\'s really really dark in here."\n'
        '    "Wow, it\'s really really dark in here."\n'
    )
    ids = [i.ident for i in _dl(extract_from_rpy(src, "script.rpy"))]
    assert ids == ["start_915cb944", "start_d48fb984",
                   "tutorial_dialogue_8989713f", "tutorial_dialogue_8989713f_1"]


def test_tlid_only_considers_say():
    src = ('label tutorial_nvlmode:\n'
           '    nvl clear\n'
           '    nvle "You use \'nvl clear\' to clear the screen when that becomes necessary."\n')
    # Ren'Py 8.6+ (piglatin tl 에서 확인한 값)
    assert _dl(extract_from_rpy(src, "a.rpy", tlid_only_say=True))[0].ident == \
        "tutorial_nvlmode_ad2b8b67"
    # 8.6 미만: nvl clear 도 다이제스트에 포함
    old = _dl(extract_from_rpy(src, "a.rpy", tlid_only_say=False))[0]
    say = 'nvle "You use \'nvl clear\' to clear the screen when that becomes necessary."'
    assert old.ident == "tutorial_nvlmode_" + translate_digest(["nvl clear", say])
    assert old.tmpl["pre"] == ["nvl clear"]


def test_sample_game_items():
    with open(os.path.join(DATA, "game_v1", "script.rpy"), encoding="utf-8") as f:
        items = extract_from_rpy(f.read(), "script.rpy")
    dl = _dl(items)
    sources = [d.source for d in dl]
    assert sources == [
        "It was a quiet morning.",
        "Good morning, [player_name]!",
        "I made {b}coffee{/b} for you.",
        "Do you want some?",
        "Here you go.",
        "Suit yourself.",
        "Hey, it's %(time)s already.\nWe're late!",
        "Let's go.",
        "The end of the demo.",
        "Thanks for playing!",
    ]
    by_src = {d.source: d for d in dl}
    assert by_src["Good morning, [player_name]!"].speaker == "Alice"
    assert by_src["Good morning, [player_name]!"].line == 12
    assert by_src["I made {b}coffee{/b} for you."].tmpl["who"] == "a happy"
    assert by_src["Do you want some?"].tmpl["pre"] == ['voice "a_003.ogg"']
    assert by_src["Let's go."].tmpl["who"] == '"Bob"'
    assert by_src["Thanks for playing!"].tmpl["post"] == "with dissolve"
    assert by_src["Thanks for playing!"].label == "chapter2"
    assert by_src["Here you go."].ident.startswith("start_")
    strings = {i.ident for i in items if i.kind == STRING}
    assert strings == {"Alice", "Bob", "Yes, please.", "No, thanks."}   # Character("Bob") 이름도 표시되는 글


def test_identifier_uses_label_and_encoded_code():
    src = 'label ch1:\n    e "Say \\"hi\\"  twice\\nok"\n'
    d = _dl(extract_from_rpy(src, "x.rpy"))[0]
    assert d.source == 'Say "hi" twice\nok'  # 공백 축약 + \n 해석
    assert d.ident == "ch1_" + translate_digest([say_code("e", 'Say "hi" twice\nok')])


def test_local_label_hidden_label_and_menu_label():
    src = ('label main:\n'
           '    "a"\n'
           'label .sub:\n'
           '    "b"\n'
           'label _hidden_like:\n'
           '    "c"\n'
           'label skipped hide:\n'
           '    "d"\n'
           'menu choose_one:\n'
           '    "Pick":\n'
           '        "e"\n')
    ids = [i.ident for i in _dl(extract_from_rpy(src, "x.rpy"))]
    dg = lambda w: translate_digest([say_code(None, w)])  # noqa: E731
    assert ids == ["main_" + dg("a"), "main_sub_" + dg("b"), "main_sub_" + dg("c"),
                   "main_sub_" + dg("d"), "choose_one_" + dg("e")]


def test_explicit_id_and_menu_say():
    src = ('label start:\n'
           '    e "Hello" id my_line\n'
           '    menu:\n'
           '        e "Which one?"\n'
           '        "One":\n'
           '            pass\n'
           '        "Caption"\n')
    items = extract_from_rpy(src, "x.rpy")
    dl = _dl(items)
    assert dl[0].ident == "my_line"
    assert dl[1].source == "Which one?" and dl[1].tmpl["post"] == "nointeract"
    assert dl[1].ident == "start_" + translate_digest([say_code("e", "Which one?", interact=False)])
    assert {i.ident for i in items if i.kind == STRING} == {"One", "Caption"}


def test_monologue_triple_quoted():
    src = 'label m:\n    e """\n    First para.\n\n    Second\n    para.\n    """\n'
    dl = _dl(extract_from_rpy(src, "x.rpy"))
    assert [d.source for d in dl] == ["First para.", "Second para."]
    assert dl[1].line == 5


def test_screens_and_marked_strings():
    with open(os.path.join(DATA, "game_v1", "screens.rpy"), encoding="utf-8") as f:
        items = extract_from_rpy(f.read(), "screens.rpy")
    assert {i.ident for i in items} == {"Start", "Quit", "Version [config.version]", "Game saved."}
    assert not _dl(items)


def test_logical_lines_join_parens_and_strings():
    lines = logical_lines('define x = Foo(\n    1,\n    2)  # c\n"a # not comment"\n')
    assert [(n, t) for n, _, t in lines] == [(1, "define x = Foo(\n    1,\n    2)"),
                                              (4, '"a # not comment"')]


def test_renpy_string_escapes():
    assert renpy_string(r'"a\{b\[c\%d"') == "a{{b[[c%%d"
    assert renpy_string('r"raw\\n  x"') == "raw\\n  x"


def test_non_say_lines_ignored():
    src = ('label s:\n    show eileen happy at left\n    play music "a.ogg"\n'
           '    $ x = "not dialogue"\n    window hide\n    pause 1.0\n')
    assert _dl(extract_from_rpy(src, "x.rpy")) == []


def test_version_detection():
    assert parse_renpy_version("version = '8.3.7.25031702'\n") == (8, 3, 7)
    assert parse_renpy_version("version_tuple = (7, 4, 11, vc_version)") == (7, 4, 11)
    assert tlid_only_considers_say((8, 6, 0)) is True
    assert tlid_only_considers_say((8, 5, 2)) is False
    assert tlid_only_considers_say(None) is False
    assert tlid_config_override("define config.tlid_only_considers_say = True") is True


def test_atl_and_testcase_blocks_are_not_dialogue():
    src = ('label s:\n'
           '    show eileen:\n'
           '        "eileen happy"\n'
           '        pause 0.5\n'
           '        "eileen sad"\n'
           '    "Real line."\n'
           'testcase t:\n'
           '    "Start"\n'
           'testsuite ts:\n'
           '    description "Suite"\n')
    assert [d.source for d in _dl(extract_from_rpy(src, "x.rpy"))] == ["Real line."]


PY_DATA = '''
init python:
    class Item(object):
        def __init__(self, name, desc, icon, kind):
            self.name, self.desc, self.icon, self.kind = name, desc, icon, kind

    items = {
        "sword": Item("Iron Sword", "A sturdy blade.\\nDeals {b}10{/b} damage.", "images/sword.png", "weapon"),
        "potion": Item("Potion", "Restores [hp] HP", "images/potion.png", "consumable"),
    }
    stat_labels = ["HP", "MP", "attack", "Status Window"]   # don't confuse the scanner

define skill_names = ["Fire Ball", "heal"]
default quest_text = "Find the lost sword."

screen status():
    text "Level" 
    textbutton "Close" action Hide("status")
    text "[player.name]" style "status_text"

label start:
    $ gold_msg = "Not enough gold!"
    $ flag = "met_bob"
'''


def _strs(items):
    return {i.ident for i in items if i.kind == STRING}


def test_python_strings_are_extracted_for_items_skills_status():
    got = _strs(extract_from_rpy(PY_DATA, "items.rpy"))
    assert {"Iron Sword", "Potion", "A sturdy blade.\nDeals {b}10{/b} damage.", "Restores [hp] HP",
            "HP", "MP", "Status Window", "Fire Ball", "Find the lost sword.", "Level", "Close",
            "Not enough gold!"} <= got
    # 내부용 키/경로/식별자는 제외
    assert not ({"sword", "potion", "weapon", "consumable", "images/sword.png", "attack", "heal",
                 "met_bob", "status_text", "status"} & got)


def test_python_strings_can_be_turned_off():
    got = _strs(extract_from_rpy(PY_DATA, "items.rpy", py_strings=False))
    assert "Iron Sword" not in got and "Fire Ball" not in got and "Not enough gold!" not in got
    assert {"Level", "Close"} <= got          # 화면 text/textbutton 리터럴은 원래부터 대상


def test_looks_translatable_rules():
    from rpytl.strscan import looks_translatable as t
    assert t("Iron Sword") and t("Sword") and t("体力") and t("HP") and t("Status Window")
    assert not t("images/a.png") and not t("bgm.ogg") and not t("#ff00ff") and not t("ITEM_ID")
    assert not t("sword") and not t("camelCase") and not t("123") and not t("x") and not t("a.b")


def test_rpyc_python_code_strings_and_old_tl_are_handled():
    import fakerenpy as f
    from rpytl import rpyc
    code = f.PyCode('items = {"sword": ("Iron Sword", "A sturdy blade.", "images/s.png")}\n'
                    'labels = ["Status Window", "attack"]')
    init = f.Init(("a.rpy", 1), block=[f.Define(("a.rpy", 2), varname="x", code=code)])
    old_tl = f.Translate(("tl/x.rpy", 1), identifier="s_1", language="korean",
                         block=[f.Say(("x", 1), who=None, what="OLD")])
    old_py = f.Init(("tl/x.rpy", 5), block=[f.Define(("tl/x.rpy", 6), varname="y",
                                                    code=f.PyCode('z = "Korean Old Text"'))])
    old_tl.block.append(old_py)
    got = _strs(rpyc.extract_from_rpyc(f.make_rpyc([init, old_tl]), "a.rpy"))
    assert {"Iron Sword", "A sturdy blade.", "Status Window"} <= got
    assert "sword" not in got and "attack" not in got
    assert "Korean Old Text" not in got       # 기존 번역(language 있는 블록) 안은 원문이 아님
    off = _strs(rpyc.extract_from_rpyc(f.make_rpyc([init]), "a.rpy", py_strings=False))
    assert "Iron Sword" not in off
