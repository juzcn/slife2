"""Progress notifications: a display line that cannot be read is dropped.

`decode` is the one reader of another server's progress stream.  Its contract is
the registered fallback: a line this build has no word for is not shown, not
raised, because a progress stream is display and a turn that dies over a note is
worse than a note nobody saw (SDD Appendix C).  The type checks are the half
that keeps a *readable* JSON line with the wrong field types inside that
contract instead of raising out of the callback.
"""

from __future__ import annotations

import json

import pytest

from slife2.libs.turn.events import ContextChosen, TurnFinished, decode
from slife2.libs.turn.messages import Usage

pytestmark = pytest.mark.unit


def test_a_known_line_is_decoded() -> None:
    assert decode(json.dumps({"t": "context", "kept": 2, "recalled": 1})) == (
        ContextChosen(kept=2, recalled=1)
    )
    assert decode(
        json.dumps({"t": "context", "kept": 2, "recalled": 1, "r": "unavailable"})
    ) == ContextChosen(kept=2, recalled=1, rerank="unavailable")

    # A status of another type is a line this build does not know, not one
    # whose status is empty.
    assert (
        decode(json.dumps({"t": "context", "kept": 2, "recalled": 1, "r": 3})) is None
    )


def test_a_count_with_the_wrong_type_drops_the_line_instead_of_raising() -> None:
    """**A readable line with the wrong type is still unreadable.**  `int("many")`
    used to raise out of `decode` and into the progress callback, where the
    registered answer is to drop the line."""
    assert decode(json.dumps({"t": "context", "kept": "many"})) is None
    assert decode(json.dumps({"t": "context", "kept": True})) is None
    assert (
        decode(json.dumps({"t": "tool_start", "name": "x", "iteration": "1"})) is None
    )
    assert decode(json.dumps({"t": "tool_end", "name": "x", "ms": "slow"})) is None


def test_a_usage_breakdown_with_a_string_is_dropped() -> None:
    """`Usage.from_wire` would raise on the string, and a missing key would be a
    number nobody measured."""
    assert decode(json.dumps({"t": "done", "usage": {"prompt_tokens": "ten"}})) is None
    assert decode(json.dumps({"t": "done", "usage": "none"})) is None
    assert decode(json.dumps({"t": "done", "iterations": 3})) is None


def test_a_line_with_a_hole_is_not_read_as_a_zero() -> None:
    """`encode` writes every count it has, so a missing one is not this build's
    line — and reading the hole as a number would show a figure nobody measured."""
    assert decode(json.dumps({"t": "tools", "n": 3})) is None
    assert decode(json.dumps({"t": "context", "kept": 2})) is None
    full = {
        "t": "done",
        "d": "ok",
        "iterations": 1,
        "stop": "stop",
        "usage": Usage(10, 20).to_wire(),
        "last": Usage(7, 3).to_wire(),
    }
    assert decode(json.dumps(full)) == TurnFinished(
        text="ok",
        usage=Usage(10, 20),
        last_usage=Usage(7, 3),
        iterations=1,
        stop_reason="stop",
    )
