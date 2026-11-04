"""Small text helpers."""


def word_count(s):
    return len(s.split())


def slugify(s):
    return "-".join(s.lower().split())
