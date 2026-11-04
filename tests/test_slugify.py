import textkit


def test_e1_s1_ac1_hello_world():
    assert textkit.slugify("Hello World") == "hello-world"


def test_e1_s1_ac2_extra_spaces():
    assert textkit.slugify("  A  b ") == "a-b"
