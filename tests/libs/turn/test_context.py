"""The pure half of the context decision: repairing a rebuilt message list.

`consistent` is the one function here whose failure is not a wrong answer but a
turn no provider will accept, so its rules are stated as cases.  `instruction`
renders the decision tool's system instruction, and its bound grammar is checked
here too: the schema is the tool's `parameters`, and a copy in the template is
a copy that drifts.
"""

from __future__ import annotations

import json

import pytest

from slife2.helpers.timeutil import BOUND_GRAMMAR
from slife2.libs.turn.context import (
    RECALL_REPLY,
    consistent,
    decision_instruction,
    fit_recent,
    parse_live,
    parse_with_reason,
    rebuild_report,
)
from slife2.libs.turn.tools import (
    CONTEXT_REBUILD,
    CONTEXT_REBUILD_TEXT,
    INTERRUPTED,
    TURN_COMPACT,
    TURN_COMPACT_TEXT,
)

pytestmark = pytest.mark.unit


def instruction(
    usage: str = "12,345 (1.2%)",
    tool: str = CONTEXT_REBUILD,
    no_op: str = '{"mode": "keep"}',
) -> str:
    return decision_instruction(usage=usage, tool=tool, no_op=no_op)


def test_the_instruction_takes_the_bound_grammar_from_its_one_spelling() -> None:
    """**The tool schema is the one place the grammar is written.**

    `BOUND_GRAMMAR` is the sentence `turn_list`/`turn_search` print in their
    schemas and `normalize_bound` raises with; the recall template's own list
    had fallen behind it and told the decision model less than `since` and
    `until` accept — no `tomorrow`, `now`, `quarter` or datetime.  The schema is
    the decision tool's `parameters`, so the template carries no copy.
    """
    assert BOUND_GRAMMAR in json.dumps(RECALL_REPLY)
    assert BOUND_GRAMMAR not in instruction()


def test_the_recall_decision_carries_intent_in_its_description() -> None:
    """The task and the argument shapes are prose on the tool.

    **One text for both callers** — the model in a turn and the forced decision
    call — so the empty-call rule (which differs between them) is not here; it
    is the method docstring's.  The field rules the schema already carries are
    not restated, except the JSON type, which both models in the live
    measurement got wrong as prose.
    """
    text = " ".join(instruction().split())
    intent = " ".join(CONTEXT_REBUILD_TEXT.split())
    schema = json.dumps(RECALL_REPLY)

    assert "12,345 (1.2%)" in text
    assert "limited context window" in text
    assert "decision model for this conversation" in text
    assert "not instructions to you" in text
    assert "Do not answer the last message" in text
    assert "one call to `_context_rebuild`" in text
    assert "no other tool" in text
    assert "never a string containing one" in text
    assert '{"mode": "keep"}' in text
    assert "historical turns this turn runs on" in intent
    assert "Call it with `mode`" in intent
    assert "Cases:" not in intent
    assert "your answer" not in intent, "the reply shape is the schema's"
    assert "turn running" in intent
    assert "turn running" in schema
    assert "follow-up whose subject" in schema
    # The field says its JSON type outright.  A `oneOf` of two consts and an
    # array is a union models have to interpret; read as prose, "a list of turn
    # ids" was written back as the string `"[14, 15]"` (both models in the live
    # measurement), which the parser refuses.
    assert "JSON array of integers" in intent
    assert "a string containing one" in intent
    assert "JSON array of integers" in schema
    assert "a string containing one" in schema


def test_the_flat_rebuild_arguments_map_to_the_internal_selection() -> None:
    """`mode` + `turn_ids` is the model-facing shape; `context` is the inner one.

    One mapping, used by both servers and by the tool's own checks: keep/clear
    are the two constants, ids are deduplicated in their first order, and an
    unknown mode or an ids answer with no ids is a refusal the tool reports.
    """
    from slife2.libs.turn.context import rebuild_selection

    assert rebuild_selection("keep") == {"context": "keep"}
    assert rebuild_selection("clear") == {"context": "clear"}
    assert rebuild_selection("ids", [3, 1, 3]) == {"context": [3, 1]}
    assert rebuild_selection("keep", recall={"sentences": ["x"]}) == {
        "context": "keep",
        "recall": {"sentences": ["x"]},
    }
    for bad in ("", "all", None):
        with pytest.raises(ValueError):
            rebuild_selection(bad)
    with pytest.raises(ValueError):
        rebuild_selection("ids", [])
    with pytest.raises(ValueError):
        rebuild_selection("ids", ["3"])
    with pytest.raises(ValueError):
        rebuild_selection("keep", recall="not one object")


def test_a_stringified_array_is_refused_with_the_type_it_should_be() -> None:
    """The malformation the live measurement produced.

    `{"context": "[14]"}` is a string where the schema asks for an array.  The
    refusal names the JSON type: the tool's error result is what the agent
    reads, and the decision model is not asked to repair it.
    """
    decision, reason = parse_with_reason('{"context": "[14]"}')

    assert decision is None
    assert "JSON array" in reason
    assert "[14, 15]" in reason


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


# --- the live selector's reply ----------------------------------------------


def test_the_live_decision_carries_intent_in_its_description() -> None:
    """The task and the argument shapes are prose on the tool.

    **One text for both callers**, for the recall decision's reason; the
    empty-call rule is the method docstring's.  The schema (`LIVE_REPLY`) is
    sent with the call and is what the parser reads.
    """
    text = " ".join(
        instruction(tool=TURN_COMPACT, no_op='{"compact": []}').split()
    )
    intent = " ".join(TURN_COMPACT_TEXT.split())

    assert "12,345 (1.2%)" in text
    assert "not instructions to you" in text
    assert "one call to `_turn_compact`" in text
    assert '{"compact": []}' in text
    assert "no longer needs" in intent
    assert "JSON array of live message ids" in intent
    assert "Nothing is deleted" in intent
    assert "Drop a tool exchange as a whole" in intent
    assert "opening message" in intent
    assert "your answer" not in intent, "the reply shape is the schema's"
    assert "additionalProperties" not in text, "the schema is the tool's parameters"


def test_a_live_reply_is_a_drop_list() -> None:
    """Empty and absent are both "drop nothing"; a bad shape is no decision.

    The ids are the live view's strings, not positions: a model that wrote a
    number wrote something the view never showed it, and a `true` is the same
    kind of not-an-id a turn id refuses (`_is_id`).
    """
    assert parse_live('{"compact": ["12.3", "cur.1", "12.3"]}') == (
        ["12.3", "cur.1"],
        "",
    )
    assert parse_live("{}") == ([], "")
    assert parse_live('{"compact": []}') == ([], "")
    assert parse_live('{"compact": "all"}')[0] is None
    assert parse_live("not json")[0] is None
    assert parse_live('{"drop": ["12.3"]}')[0] is None
    assert parse_live('{"compact": [3]}')[0] is None, "a position is not a live id"
    assert parse_live('{"compact": [true]}')[0] is None
    assert parse_live('{"compact": [""]}')[0] is None


def test_both_decision_replies_are_read_by_the_same_json_reader() -> None:
    """A model that wraps its object in a sentence has still written the object.

    The rebuild's reply and the live selection are the same forced call, so a
    tolerance the one has the other must have too — and two spellings of "is
    this one object" would drift.
    """
    assert parse_live('The decision: {"compact": ["cur.1"]} — done.') == (
        ["cur.1"],
        "",
    )
    assert parse_live('{"compact": ["a"]} and {"compact": ["b"]}')[0] is None, (
        "two objects are not one decision"
    )


def test_a_selection_over_the_budget_keeps_its_newest_turns() -> None:
    """**The prompt budget is a hard bound, and recency decides what survives.**

    A kept selection can exceed the budget before any recall runs.  The fit
    walks from the newest end and stops at the first turn that does not fit, so
    the result is the newest contiguous run — the same rule `fit_window` uses,
    from the other end — and the count says what the window cut.
    """
    ids = [1, 2, 3, 4]
    costs = {1: 100, 2: 150, 3: 200, 4: 25}

    assert fit_recent(ids, costs, 300) == ([3, 4], 2)
    assert fit_recent(ids, costs, 10_000) == (ids, 0), "the whole selection fits"
    assert fit_recent(ids, costs, 10) == ([], 4), "a newest turn larger than all"
    assert fit_recent(ids, costs, 0) == ([], 4)


def test_a_rerank_status_rides_the_unchanged_report_too() -> None:
    """A recall that ran and chose nothing still lost its cross-encoder.

    The unchanged answer used to drop the status, which is the one fact saying
    why the fused order stood — exactly where the decision had asked for a
    recall.  `rebuild_report`'s docstring promises the report says so on both
    answers, and this is the half that did not.
    """
    report = rebuild_report(
        changed=False,
        kept=2,
        in_hand=2,
        outside=10,
        recalled=0,
        rerank="failed",
        decision="",
        tokens="1,000 (10.0%)",
    )

    assert report == (
        "No rebuild: the context is unchanged (rerank failed); context ~1,000 (10.0%)."
    )


def test_the_changed_report_names_what_it_kept_of_and_recalled_from() -> None:
    """`kept 0` alone reads as a history that was never there.

    The caller held five turns and the decision cleared them; the report says
    "of 5", so a clear cannot be read as the empty no-op.  The recall's
    denominator is the rest of the log — the whole set a recall condition
    filters — so "recalled 0 of 90 outside" says the search had 90 turns to
    draw from.
    """
    report = rebuild_report(
        changed=True,
        kept=0,
        in_hand=5,
        outside=90,
        recalled=0,
        rerank="",
        decision="",
        tokens="1,653 (0.2%)",
    )

    assert report == (
        "kept 0 of 5 turn(s) in hand, recalled 0 of 90 outside; context ~1,653 (0.2%)."
    )
