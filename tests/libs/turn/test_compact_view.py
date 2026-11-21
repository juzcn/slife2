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


def test_the_view_drops_hidden_messages_and_keeps_order() -> None:
    first, second, third = user("a"), answer("b"), user("c")

    assert view([first, second, third], [second]) == [first, third]
    assert view([first, second, third], []) == [first, second, third]


def test_the_system_and_the_opening_are_not_selectable() -> None:
    """Everything else — historical and current — is the model's to drop."""
    head, past, now, tool = system(), user("old"), user("now"), result("c1")

    assert selectable([head, past, now, tool], now) == [1, 3]


def test_the_report_names_the_record() -> None:
    assert "turn log keeps every one" in report(Compacted(seen=5, dropped=2))
    assert "did not answer" in report(Compacted(seen=4, dropped=0, decision="failed"))
    assert "Nothing dropped" in report(Compacted(seen=4, dropped=0))


def test_a_task_report_claims_no_record() -> None:
    """A task records no turn; a report that named a log would be a false fact."""
    text = report(Compacted(seen=5, dropped=2), recorded=False)

    assert "Dropped 2 message(s)" in text
    assert "turn log" not in text
