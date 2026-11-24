"""The live view: what a turn's model is sent, and what it may drop.

The projection is the whole mechanism: the list the loop advances, the turn's
saved row and the store's `covered` are the record, and a compaction only ever
changes the list a model call is handed.  These cases are about the three
questions that decide it — where the turn opens, what is fixed, and whether a
compaction already happened.
"""

from __future__ import annotations

import pytest

from slife2.libs.turn.compact import (
    Compacted,
    due,
    opening,
    projection,
    report,
    selectable,
    split_exchanges,
    unknown_ids,
    view,
)
from slife2.libs.turn.messages import Message, ToolCall

pytestmark = pytest.mark.unit


def system() -> Message:
    return Message(role="system", content="you are jack")


def user(text: str) -> Message:
    return Message(role="user", content=text)


def answer(text: str = "") -> Message:
    return Message(role="assistant", content=text)


def call(call_id: str, name: str = "fake__echo") -> Message:
    return Message(
        role="assistant", content=None, tool_calls=[ToolCall(id=call_id, name=name)]
    )


def result(call_id: str, text: str = "ok") -> Message:
    return Message(role="tool", content=text, tool_call_id=call_id)


def test_the_turn_opens_at_the_last_user_message() -> None:
    """**The last one, because a turn starts with one.**  A message that arrives
    mid-turn is a harness pair, never a second user message."""
    messages = [system(), user("old"), answer("a"), user("now"), call("c1")]

    assert opening(messages) == 3


def test_a_conversation_with_no_user_message_has_no_opening() -> None:
    assert opening([system()]) is None


def test_the_view_annotates_the_messages_the_model_did_not_write() -> None:
    """The id is in the view, like the `[TURN:]` footnote — and only on what the
    model is reading.

    **An assistant message never gets one.**  Its content is what the model
    said, and a marker written there is a sentence it never wrote: the model
    copies it into the next answer, once per call (the `cur.N` chain a live
    session showed).  The id itself is still kept on the view's copy, for the
    exchange rule; the opening is fixed and carries neither text nor id.
    """
    first = Message(role="user", content="a", mid="7.0")
    second = Message(role="assistant", content="b", mid="7.1")
    third = user("c")

    got = view([first, second, third], [])

    assert [m.mid for m in got] == ["7.0", "7.1", "cur.0"]
    assert got[0].content == 'a [MESSAGE:{"id": "7.0"}]'
    assert got[1].content == "b", "the model's own words, untouched"
    assert got[2].content == "c", "the opening is fixed and needs no id"
    assert first.content == "a", "the view is a copy"


def test_a_message_with_no_text_is_not_given_one() -> None:
    """A tool-call-only assistant message has nothing to write an annotation in.

    Writing one there made the model believe it had said it, and its next
    answer repeated it — the reported `slife2> [MESSAGE:…]` line.
    """
    tool_call = Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="c1", name="fake__echo")],
        mid="cur.1",
    )
    got = view([system(), user("now"), tool_call, result("c1")], [])

    assert got[2].content is None
    assert got[3].content == 'ok [MESSAGE:{"id": "cur.2"}]'


def test_the_view_drops_hidden_ids_and_keeps_order() -> None:
    first = Message(role="user", content="a", mid="7.0")
    second = Message(role="assistant", content="b", mid="7.1")
    third = user("c")

    got = view([first, second, third], ["7.1"])

    assert [m.mid for m in got] == ["7.0", "cur.0"]
    assert [m.content for m in got] == ['a [MESSAGE:{"id": "7.0"}]', "c"]


def test_the_system_and_the_opening_are_not_selectable() -> None:
    """Everything else — historical and current — is the model's to drop."""
    head = system()
    past = Message(role="user", content="old", mid="7.0")
    now, tool = user("now"), result("c1")

    got = view([head, past, now, tool], [])
    at = opening(got)
    assert at is not None
    opening_mid = got[at].mid

    assert selectable(got, opening_mid) == ["7.0", "cur.1"]


def test_a_half_named_exchange_is_split_and_a_whole_one_is_not() -> None:
    """**A pair moves whole or not at all.**

    Naming one member is a split: the caller keeps the pair in the view and
    applies the rest of the selection.  Naming both is the only way to drop the
    exchange.
    """
    exchange = Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="c1", name="fake__echo")],
        mid="cur.1",
    )
    result = Message(role="tool", content="ok", tool_call_id="c1", mid="cur.2")
    later = Message(role="assistant", content="done", mid="cur.3")
    got = [system(), user("now"), exchange, result, later]

    assert split_exchanges(got, ["cur.2"]) == [["cur.1", "cur.2"]], "the result alone"
    assert split_exchanges(got, ["cur.1"]) == [["cur.1", "cur.2"]], "the call alone"
    assert split_exchanges(got, ["cur.1", "cur.2"]) == [], "the pair is whole"
    assert split_exchanges(got, ["cur.3"]) == [], "a plain message is alone"
    assert split_exchanges(got, []) == []


def test_the_selector_view_annotates_the_assistant_of_an_exchange() -> None:
    """The selector has to read both ids to drop a pair.

    The turn's own view leaves the assistant message unannotated; the selector's
    copy (`projection`) writes its id, because an exchange is dropped by naming
    both members.
    """
    exchange = Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="c1", name="fake__echo")],
        mid="7.1",
    )
    result = Message(role="tool", content="ok", tool_call_id="c1", mid="7.2")

    projected, candidates = projection([system(), user("now"), exchange, result], set())

    assert projected[2].content == '[MESSAGE:{"id": "7.1"}]'
    assert candidates == ["7.1", "7.2"]


def test_an_id_the_view_did_not_offer_is_named() -> None:
    """The selection's membership rule, beside the shape rule `parse_live` holds.

    An already-hidden id is not unknown — a model that repeats a cumulative
    list is asking for the same view — but a fixed message or an id no build
    wrote leaves the whole selection refused by the caller.
    """
    assert unknown_ids(["cur.1", "7.0"], ["7.0", "cur.1"]) == []
    assert unknown_ids(["cur.1"], ["7.0", "cur.1"]) == ["7.0"]
    assert unknown_ids(["cur.1"], ["7.0"], {"7.0"}) == []


def test_a_due_compaction_answers_the_view_and_its_candidates() -> None:
    """**The threshold check and the ask are one projection.**

    The caller that finds a compaction due is handed the view the selector reads
    and the ids it may name, so the two servers build no second copy of either.
    """
    messages = [
        system(),
        Message(role="user", content="old", mid="7.0"),
        user("now"),
        result("c1"),
    ]

    asked = due(
        messages,
        set(),
        last_tokens=900,
        prompt_window=1000,
        compact_at=0.9,
        held=False,
    )

    assert asked is not None
    got, candidates = asked
    assert [m.mid for m in got] == ["", "7.0", "cur.0", "cur.1"]
    assert candidates == ["7.0", "cur.1"]


def test_a_compaction_is_not_due_below_the_fraction_or_while_held() -> None:
    messages = [system(), user("now"), answer("x")]

    for facts in (
        {"last_tokens": 899, "compact_at": 0.9, "held": False},
        {"last_tokens": 900, "compact_at": 0.0, "held": False},
        {"last_tokens": 900, "compact_at": 0.9, "held": True},
    ):
        assert due(messages, set(), prompt_window=1000, **facts) is None, facts


def test_a_turn_with_nothing_to_drop_is_not_due() -> None:
    """The system prompt and the opening are the fixed two; a turn made of them
    has no selection to ask for."""
    assert (
        due(
            [system(), user("now")],
            set(),
            last_tokens=900,
            prompt_window=1000,
            compact_at=0.9,
            held=False,
        )
        is None
    )


def test_the_report_names_the_record() -> None:
    assert "turn log keeps every one" in report(Compacted(seen=5, dropped=2))
    assert "did not answer" in report(Compacted(seen=4, dropped=0, decision="failed"))
    assert "does not offer" in report(
        Compacted(seen=4, dropped=0, refused="the view does not offer '9.9'")
    )
    assert "Nothing dropped" in report(Compacted(seen=4, dropped=0))


def test_the_report_names_the_pair_a_partial_selection_kept() -> None:
    """A drop beside a half-named exchange reports both, so the pair's
    survival is not read as a failed drop."""
    text = report(Compacted(seen=6, dropped=2, held=[["cur.3", "cur.4"]]))

    assert text.startswith("Dropped 2 message(s)")
    assert "named only in part" in text
    assert "stayed in the view" in text

    nothing = report(Compacted(seen=6, dropped=0, held=[["cur.3", "cur.4"]]))
    assert "Nothing dropped" in nothing
    assert "named only in part" in nothing

    two = report(
        Compacted(seen=7, dropped=2, held=[["cur.3", "cur.4"], ["cur.5", "cur.6"]])
    )
    assert "2 tool exchanges were named only in part" in two


def test_a_task_report_claims_no_record() -> None:
    """A task records no turn; a report that named a log would be a false fact."""
    text = report(Compacted(seen=5, dropped=2), recorded=False)

    assert "Dropped 2 message(s)" in text
    assert "turn log" not in text
