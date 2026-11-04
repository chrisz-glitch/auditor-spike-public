"""Small text helpers."""


def initials(s):
    return "".join(w[0].upper() for w in s.split())


def word_count(s):
    return len(s.split())


def slugify(s):
    return "-".join(s.lower().split())
