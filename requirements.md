# Requirements

## E1-S1 slugify

- E1-S1-AC1: "Hello World" becomes "hello-world".
- E1-S1-AC2: "  A  b " becomes "a-b".
- E1-S1-AC3: initials("Ada Lovelace") gives "AL".

## E1-S2 more helpers

- E1-S2-AC4: shout("hi") gives "HI!".
- E1-S2-AC5: whisper("HI") gives "hi...".
- E1-S2-AC6: reverse_words("a b c") gives "c b a".
- E1-S2-AC7: first_word("one two") gives "one".
- E1-S2-AC8: last_word("one two") gives "two".

## Protected paths

Changes to these paths always wait for the owner:

- `.github/**`
- `pytest.ini`
- `**/conftest.py`
- `requirements.md`
