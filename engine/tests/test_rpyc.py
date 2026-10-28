import os
import pickle
import zlib

import pytest

import fakerenpy as f
from rpytl import rpyc
from rpytl.model import DIALOGUE, STRING, say_code, translate_digest


def _game_ast():
    L = ("game/script.rpy", 0)
    voice = f.UserStatement(("game/script.rpy", 4), line='voice "v1.ogg"', translatable=True)
    say1 = f.Say(("game/script.rpy", 5), who="e", what="Hello, {b}world{/b}!", with_=None,
                 attributes=("happy",), interact=True, arguments=None, temporary_attributes=None)
    say2 = f.TranslateSay(("game/script.rpy", 8), who=None, what="Narration [name].",
                          identifier="start_deadbeef", language=None, with_="dissolve")
    menu = f.Menu(("game/script.rpy", 10), items=[
        ("Choice A", "True", [f.TranslateSay(("game/script.rpy", 12), who="e", what="You chose A.",
                                             identifier="start_aaaa0001", language=None)]),
        ("Caption only", "True", None),
    ])
    define = f.Define(("game/script.rpy", 1), varname="e",
                      code=f.PyCode('Character(_("Eileen"), color="#fff")'))
    screen_disp = f.SLDisplayable(displayable=f._textbutton,
                                  positional=[f.PyExpr('"Start Game"')], children=[])
    screen_text = f.SLDisplayable(displayable=f.Text, positional=[f.PyExpr('variable_text')],
                                  children=[])
    screen = f.Screen(("game/screens.rpy", 3),
                      screen=f.SLScreen(children=[screen_disp, screen_text]))
    old_tl = f.Translate(("game/tl/x.rpy", 1), identifier="start_other", language="korean",
                         block=[f.Say(("x", 1), who=None, what="IGNORED")])
    label = f.Label(L, _name="start", block=[], parameters=None, hide=False)
    return [define, label,
            f.Translate(("game/script.rpy", 4), identifier="start_12345678", language=None,
                        block=[voice, say1]),
            say2, menu, screen, old_tl]


def test_extract_translate_nodes():
    data = f.make_rpyc(_game_ast())
    items = rpyc.extract_from_rpyc(data, "script.rpy")
    dl = [i for i in items if i.kind == DIALOGUE]
    st = {i.ident for i in items if i.kind == STRING}

    assert [d.ident for d in dl] == ["start_12345678", "start_deadbeef", "start_aaaa0001"]
    assert dl[0].source == "Hello, {b}world{/b}!"
    assert dl[0].tmpl == {"pre": ['voice "v1.ogg"'], "who": "e happy", "post": ""}
    assert dl[0].label == "start"
    assert dl[1].tmpl["post"] == "with dissolve"
    assert dl[1].line == 8
    assert "IGNORED" not in [d.source for d in dl]
    assert st == {"Choice A", "Caption only", "Eileen", "Start Game"}


def test_old_rpyc_without_translate_computes_identifier():
    say = f.Say(("s.rpy", 3), who="e", what="Hi.", with_=None, attributes=None,
                interact=True, arguments=None, temporary_attributes=None)
    stmts = [f.Label(("s.rpy", 2), name="start", block=[], hide=False), say]
    items = rpyc.extract_from_rpyc(f.make_rpyc(stmts, rpc2=False), "s.rpy")
    expected = "start_" + translate_digest([say_code("e", "Hi.")])
    assert [i.ident for i in items] == [expected]


def test_malicious_pickle_does_not_execute(tmp_path):
    marker = tmp_path / "pwned"

    class Evil:
        def __reduce__(self):
            return (os.system, ("touch %s" % marker,))

    blob = zlib.compress(pickle.dumps(({}, [Evil()]), protocol=2))
    try:
        rpyc.extract_from_rpyc(blob, "x.rpy")
    except rpyc.RpycError:
        pass
    assert not marker.exists()


def test_garbage_raises_rpyc_error():
    with pytest.raises(rpyc.RpycError):
        rpyc.load_rpyc_bytes(b"RENPY RPC2" + b"\x00" * 12)
    with pytest.raises(rpyc.RpycError):
        rpyc.load_rpyc_bytes(b"not zlib at all")


def test_defaultdict_and_containers_in_pickle():
    """실제 Ren'Py 파일에는 collections.defaultdict(list) 같은 것이 들어 있다 ('type' object is not iterable)."""
    import collections
    node = f.Label(("s.rpy", 1), name="start", block=[], hide=False)
    node.extra = collections.defaultdict(list)
    node.extra["a"].append(1)
    node.ordered = collections.OrderedDict([("k", 1)])
    node.s = {1, 2}
    say = f.TranslateSay(("s.rpy", 2), who="e", what="Hi.", identifier="start_00000001", language=None)
    items = rpyc.extract_from_rpyc(f.make_rpyc([node, say]), "s.rpy")
    assert [i.source for i in items] == ["Hi."]


def test_speaker_names_resolved_from_character_definitions():
    """rpyc 에서는 화자가 변수명(e)이라 Character("Eileen") 정의에서 이름을 찾아 바꿔야 한다. 정의가 다른 파일에 있어도."""
    import os
    import tempfile
    from rpytl import scan
    define_e = f.Define(("a.rpy", 1), varname="e", code=f.PyCode('Character(_("Eileen"), color="#fff")'))
    py_block = f.Init(("a.rpy", 2), block=[f.Define(("a.rpy", 3), varname="x", code=f.PyCode(
        'm = Character("{color=#f00}Mia{/color}", who_color="#f00")\nzzz = 1'))])
    defs = f.make_rpyc([f.Init(("defs.rpy", 1), block=[define_e]), py_block])
    say1 = f.TranslateSay(("s.rpy", 3), who="e", what="Hello there.", identifier="start_00000001", language=None)
    say2 = f.TranslateSay(("s.rpy", 4), who="m", what="Hi, Eileen.", identifier="start_00000002", language=None)
    say3 = f.TranslateSay(("s.rpy", 5), who="narr", what="Silence.", identifier="start_00000003", language=None)
    script = f.make_rpyc([f.Label(("s.rpy", 1), name="start", block=[], hide=False), say1, say2, say3])
    d = tempfile.mkdtemp()
    open(os.path.join(d, "defs.rpyc"), "wb").write(defs)       # 파일 이름순으로는 defs 가 script 보다 먼저지만
    open(os.path.join(d, "a_script.rpyc"), "wb").write(script)  # 일부러 정의가 뒤에 읽히는 경우도 확인
    items, _, errors = scan.collect_items(d)
    assert errors == []
    spk = {i.source: i.speaker for i in items if i.kind == DIALOGUE}
    assert spk == {"Hello there.": "Eileen", "Hi, Eileen.": "Mia", "Silence.": "narr"}
