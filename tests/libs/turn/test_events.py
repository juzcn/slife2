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

pytestmark = pytest.mark.unit


def test_a_known_line_is_decoded() -> None:
    assert decode(json.dumps({"t": "context", "kept": 2, "recalled": 1})) == (
        ContextChosen(kept=2, recalled=1)
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
    """`Usage.from_wire` would raise on the string, and `or 0` on a missing key
    would be a number nobody measured."""
    assert decode(json.dumps({"t": "done", "usage": {"prompt_tokens": "ten"}})) is None
    assert decode(json.dumps({"t": "done", "usage": "none"})) is None
    finished = decode(json.dumps({"t": "done", "iterations": 3}))
    assert isinstance(finished, TurnFinished)
    assert finished.iterations == 3
