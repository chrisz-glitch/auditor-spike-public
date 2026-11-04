import importlib


def test_e1_s2_ac5_whisper():
    m = importlib.import_module("whisper")
    assert m.whisper("HI") == "hi..."
