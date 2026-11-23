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
    opening,
    report,
    selectable,
    split_exchange,
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


def test_the_view_annotates_every_addressable_message_with_its_live_id() -> None:
    """The id is in the view, like the `[TURN:]` footnote — not in a copy a
    selector adds for itself, and never in the record the list came from."""
    first = Message(role="user", content="a", mid="7.0")
    second = Message(role="assistant", content="b", mid="7.1")
    third = user("c")

    got = view([first, second, third], [])

    assert [m.mid for m in got] == ["7.0", "7.1", "cur.0"]
    assert got[0].content == 'a [MESSAGE:{"id": "7.0"}]'
    assert got[2].content == 'c [MESSAGE:{"id": "cur.0"}]'
    assert first.content == "a", "the view is a copy"


def test_the_view_drops_hidden_ids_and_keeps_order() -> None:
    first = Message(role="user", content="a", mid="7.0")
    second = Message(role="assistant", content="b", mid="7.1")
    third = user("c")

    got = view([first, second, third], ["7.1"])

    assert [m.mid for m in got] == ["7.0", "cur.0"]
    assert [m.content for m in got] == [
        'a [MESSAGE:{"id": "7.0"}]',
        'c [MESSAGE:{"id": "cur.0"}]',
    ]


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


def test_a_selection_cannot_split_a_tool_exchange() -> None:
    """The provider's rule, refused before the next request rather than by it."""
    exchange = Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="c1", name="fake__echo")],
        mid="cur.1",
    )
    result = Message(role="tool", content="ok", tool_call_id="c1", mid="cur.2")
    later = Message(role="assistant", content="done", mid="cur.3")
    got = [system(), user("now"), exchange, result, later]

    assert split_exchange(got, ["cur.1", "cur.2"]) is None, "the whole exchange"
    assert "split" in (split_exchange(got, ["cur.1"]) or ""), "the call alone"
    assert "split" in (split_exchange(got, ["cur.2"]) or ""), "the result alone"
    assert split_exchange(got, ["cur.3"]) is None, "a plain message"


def test_the_report_names_the_record() -> None:
    assert "turn log keeps every one" in report(Compacted(seen=5, dropped=2))
    assert "did not answer" in report(Compacted(seen=4, dropped=0, decision="failed"))
    assert "split" in report(
        Compacted(seen=4, dropped=0, refused="the selection would split")
    )
    assert "Nothing dropped" in report(Compacted(seen=4, dropped=0))


def test_a_task_report_claims_no_record() -> None:
    """A task records no turn; a report that named a log would be a false fact."""
    text = report(Compacted(seen=5, dropped=2), recorded=False)

    assert "Dropped 2 message(s)" in text
    assert "turn log" not in text
