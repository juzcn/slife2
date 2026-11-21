"""The harness's lines as words: the trim's five lists become one sentence.

The trim answers the model with an object — one verdict per name — and the
transcript's note is the same answer for a person.  The one rule worth testing
is that an answer this does not recognise is shown as it came rather than read
as "nothing moved".
"""

from __future__ import annotations

import json

import pytest

from slife2.libs.harness import notes

pytestmark = pytest.mark.unit


def answer(**lists: list[str]) -> str:
    found = {
        "unloaded": [],
        "refused": [],
        "no_load_state": [],
        "unknown": [],
        "not_loaded": [],
    }
    found.update(lists)
    return json.dumps(found)


def test_the_trim_becomes_the_names_that_left() -> None:
    text = notes.unloaded(answer(unloaded=["b", "a"], refused=["tool_search"]))

    assert text == ("unloaded: b, a; refused (the system needs them): tool_search")


def test_an_empty_trim_says_nothing_moved() -> None:
    assert notes.unloaded(answer()) == "nothing to unload"


def test_an_answer_that_is_not_the_shape_is_shown_as_it_came() -> None:
    assert notes.unloaded("not json") == "not json"
    foreign = json.dumps({"other": []})
    assert notes.unloaded(foreign) == foreign
    partial = json.dumps({"unloaded": ["a"]})
    assert notes.unloaded(partial) == partial
