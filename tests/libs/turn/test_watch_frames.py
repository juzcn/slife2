"""The frames a watcher is handed around a turn: one round trip, one contract.

The turn events' own encoding is `test_events.py`'s subject.  What is here is
the watcher-only vocabulary — the snapshot, the two opening frames and the
closing one — and the rule it shares with the events: a frame this build cannot
read is a line not drawn, never a value read as an empty one.
"""

from __future__ import annotations

import json

import pytest

from slife2.libs.turn.watch import (
    Closed,
    Snapshot,
    TaskBrief,
    TurnStarted,
    decode,
    encode,
)

pytestmark = pytest.mark.unit


def test_a_task_brief_round_trips() -> None:
    item = TaskBrief("research", "Find [the] retry policy.", "running")
    assert decode(encode(item)) == item


def test_a_task_brief_with_a_missing_field_is_dropped() -> None:
    """The status is what the screen shows; a hole is not empty."""
    payload = json.loads(encode(TaskBrief("research", "find it", "running")))
    del payload["s"]
    assert decode(json.dumps(payload)) is None


def test_the_other_frames_keep_their_round_trip() -> None:
    assert decode(encode(Snapshot([], []))) == Snapshot([], [])
    assert decode(encode(TurnStarted("hello", "agent:jack"))) == TurnStarted(
        "hello", "agent:jack"
    )
    assert decode(encode(Closed("gone"))) == Closed("gone")
