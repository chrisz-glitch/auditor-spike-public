import textkit


def test_word_count():
    assert textkit.word_count("one two  three") == 3
