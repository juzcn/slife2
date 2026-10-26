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


@pytest.mark.parametrize("junk", ["[]", "null", "5", '"a string"', "[{}]", "{oops"])
def test_arguments_that_are_not_an_object_are_kept_and_marked(junk: str) -> None:
    """**Valid JSON is not the same as valid arguments, and neither is run.**

    `arguments` is a dict — everything downstream relies on it — and `[]`,
    `null` and `5` are not one however well-formed they are.  Reading them as
    `{}` used to dispatch the tool on defaults the model never chose, and its
    validation error then blamed a missing required argument instead of the
    call the model got wrong; assigning them as they came was worse, an
    `AttributeError` out of the loop.

    So the call keeps its id and name, its `arguments` stay empty, and `error`
    carries the sentence `ToolRegistry.execute` answers with — the tool is
    never reached.
    """
    call = call_with(junk)

    assert call.arguments == {}
    assert call.error is not None
    assert "was not run" in call.error
    assert call.raw_arguments == junk
    # The reader that used to be handed a list: it still has a dict.
    assert split_meta(call.arguments)[0] == {}


def test_arguments_that_are_already_a_mapping_are_kept() -> None:
    """Read back off our own wire, or off a provider that sent one already."""
    call = call_with({"e": "2+2"})
    assert call.arguments == {"e": "2+2"}
    assert call.error is None


def test_a_damaged_call_round_trips_with_what_the_model_wrote() -> None:
    """The call is still a call, and the record keeps the text that arrived.

    The id has to survive — a tool result answers it, and a message whose
    `tool_call_id` answers nothing is the one history state every provider
    rejects — and the name has to survive, because it is what tells the model
    which call it got wrong.  The arguments survive as the wire's own string:
    an empty object in their place is the lie this refuses.
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

    (call,) = message.tool_calls
    assert (call.id, call.name) == ("c1", "calc")
    assert call.error is not None
    wire = message.to_wire()
    assert wire["tool_calls"][0]["function"]["arguments"] == "[]"
    assert Message.from_wire(wire) == message
