"""The message model's tolerances: what is degraded, and what is refused.

The vocabulary itself is round-tripped in `tests/libs/model/test_wire.py`, which
is where `to_wire` and `from_wire` are held against each other.  What is here is
the other half of that contract — the input that did not come from this build:
a model that emitted arguments that are not a dict, and the loop that reads them
next.  `tests/libs/model/` cannot hold these because they are about what a
*damaged* call does, and the damage has one reader that must not see it at all.
"""

from __future__ import annotations

import pytest

from slife2.libs.turn.messages import Message, ToolCall
from slife2.libs.turn.tools import split_meta

pytestmark = pytest.mark.unit


def call_with(arguments: object) -> ToolCall:
    """The wire object a model's stream reassembles into, with these arguments."""
    return ToolCall.from_wire(
        {"id": "c1", "function": {"name": "calc", "arguments": arguments}}
    )


@pytest.mark.parametrize("junk", ["[]", "null", "5", '"a string"', "[{}]"])
def test_arguments_that_parse_to_something_that_is_not_an_object_degrade(
    junk: str,
) -> None:
    """**Valid JSON is not the same as valid arguments.**

    `arguments` is a dict — the class says so, and everything downstream relies
    on it — so `[]`, `null` and `5` are damage however well-formed they are, and
    all three used to be assigned as they came.  The first reader is
    `split_meta`, inside the loop, which calls `.items()` on them: the loop has
    no handler for that `AttributeError`, so one bad call ended the turn with a
    traceback about a list where the model should have been told its own call
    was malformed and given the chance to correct it.

    Degrading rather than raising is what this path already does for JSON that
    will not parse, and for the same reason: the error belongs to the tool call,
    which is the loop's normal feedback path.
    """
    call = call_with(junk)

    assert call.arguments == {}
    # The reader that used to be handed a list: it has to be able to take the
    # answer, or the degradation has bought nothing.
    assert split_meta(call.arguments)[0] == {}


def test_arguments_that_are_already_a_mapping_are_kept() -> None:
    """Read back off our own wire, or off a provider that sent one already."""
    assert call_with({"e": "2+2"}).arguments == {"e": "2+2"}


def test_a_tool_call_with_arguments_that_are_not_a_dict_round_trips_as_empty() -> None:
    """The degraded call is still a call, with its name and its id intact.

    Losing the id would be worse than losing the arguments: the tool result has
    to answer *this* call, and a message whose `tool_call_id` answers nothing is
    the one history state every provider rejects.
    """
    message = Message.from_wire(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "function": {"name": "calc", "arguments": "[]"}}
            ],
        }
    )

    assert message.tool_calls == [ToolCall(id="c1", name="calc", arguments={})]
