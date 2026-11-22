"""The pure half of the context decision: repairing a rebuilt message list.

`consistent` is the one function here whose failure is not a wrong answer but a
turn no provider will accept, so its rules are stated as cases.  `instruction`
renders the discriminator's prompt, and its bound grammar is checked here too:
the prompt is the other place a model reads it, and a copy in the template is a
copy that drifts.
"""

from __future__ import annotations

import pytest

from slife2.helpers.timeutil import BOUND_GRAMMAR
from slife2.libs.turn.context import (
    consistent,
    instruction,
    live_instruction,
    parse_live,
    with_message_id,
)
from slife2.libs.turn.tools import INTERRUPTED

pytestmark = pytest.mark.unit


def test_the_instruction_takes_the_bound_grammar_from_its_one_spelling() -> None:
    """**The template must not carry a second copy of the grammar.**

    `BOUND_GRAMMAR` is the sentence `turn_list`/`turn_search` print in their
    schemas and `normalize_bound` raises with; the recall template's own list
    had fallen behind it and told the discriminator less than `since` and
    `until` accept — no `tomorrow`, `now`, `quarter` or datetime.
    """
    text = instruction()

    assert BOUND_GRAMMAR in text


def call(call_id: str, name: str = "search") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
    }


def answer(call_id: str, text: str = "done") -> dict:
    return {"role": "tool", "content": text, "tool_call_id": call_id}


def repair(call_id: str) -> dict:
    return {"role": "tool", "content": INTERRUPTED, "tool_call_id": call_id}


def test_a_call_answered_later_in_the_same_turn_is_left_alone() -> None:
    messages = [call("a"), answer("a")]

    assert consistent(messages) == messages


def test_an_unanswered_call_is_completed_after_its_own_message() -> None:
    messages = [call("a")]

    assert consistent(messages) == [call("a"), repair("a")]


def test_a_result_in_an_earlier_turn_does_not_answer_a_later_call() -> None:
    """**An id is one response's, and the answer has to follow its call.**

    Two stored turns can carry the same call id — a local provider that numbers
    per response, and nothing checks uniqueness across turns.  The old repair
    collected every `tool_call_id` in the list before walking it, so the first
    turn's result answered the second turn's call: the later turn kept an
    assistant message whose call was never answered, the one history state
    every provider rejects, and the repair that exists to prevent it was
    skipped.
    """
    messages = [call("call_0"), answer("call_0"), call("call_0")]

    repaired = consistent(messages)

    assert repaired == [
        call("call_0"),
        answer("call_0"),
        call("call_0"),
        repair("call_0"),
    ]


def test_each_duplicate_id_gets_the_result_that_follows_it() -> None:
    """A result answers the nearest call before it, not any call with the id."""
    messages = [call("x"), answer("x", "first"), call("x"), answer("x", "second")]

    assert consistent(messages) == messages


def test_a_damaged_call_entry_is_skipped_rather_than_raised() -> None:
    """A call that is not a mapping is not a call this can repair.

    The store refuses the message body that carries one, but a live list is
    built by this build; skipping keeps this function total even if a future
    writer forgets.
    """
    messages = [{"role": "assistant", "content": "hi", "tool_calls": ["oops"]}]

    assert consistent(messages) == messages


# --- the live selector's copy and reply --------------------------------------


def test_a_live_id_is_prefixed_into_a_copy_only() -> None:
    """The record keeps the list it had; the id lives in the one request."""
    original = {"role": "user", "content": "hi"}
    prefixed = with_message_id(original, 4)

    assert prefixed["content"] == '[MESSAGE:{"id": 4}] hi'
    assert original["content"] == "hi", "the copy is a copy"

    parts = {"role": "user", "content": [{"type": "text", "text": "look"}]}
    got = with_message_id(parts, 2)

    assert got["content"][0] == {"type": "text", "text": '[MESSAGE:{"id": 2}] '}
    assert got["content"][1] == {"type": "text", "text": "look"}


def test_the_live_instruction_names_the_candidates() -> None:
    text = live_instruction([1, 4], usage="12,345 (1.2%)")

    assert "1, 4" in text
    assert "12,345 (1.2%)" in text
    assert '"compact"' in text
    assert "goes as a whole" in text, "the legality rule is in the prompt"


def test_a_live_reply_is_a_drop_list() -> None:
    """Empty and absent are both "drop nothing"; a bad shape is no decision."""
    assert parse_live('{"compact": [3, 1, 3]}') == ([3, 1], "")
    assert parse_live("{}") == ([], "")
    assert parse_live('{"compact": []}') == ([], "")
    assert parse_live('{"compact": "all"}')[0] is None
    assert parse_live("not json")[0] is None
    assert parse_live('{"drop": [1]}')[0] is None
    assert parse_live('{"compact": [true]}')[0] is None
