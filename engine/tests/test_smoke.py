import rpytl


def test_engine_info():
    assert rpytl.__version__ in rpytl.engine_info()
