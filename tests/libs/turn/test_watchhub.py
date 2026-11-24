"""The shared half of watching: one tap, many readers, and the race between them.

The hub is one implementation because both servers that run turns face the same
two facts: a turn's events must be kept from its start for a watcher that joins
mid-turn, and a watcher's queue must never be able to slow the turn.  These
tests pin the table's own contract — tap replacement, replay copies, readers
leaving — so a change there is a change both servers feel on purpose.
"""

from __future__ import annotations

import pytest

from slife2.libs.turn.events import TextDelta
from slife2.libs.turn.watch import Closed, TurnStarted
from slife2.libs.turn.watchhub import WatchHub

pytestmark = pytest.mark.unit


def test_a_published_event_reaches_the_tap_and_every_watcher() -> None:
    hub: WatchHub[str] = WatchHub()
    tap = hub.start_tap("c", TurnStarted("hello", ""))
    first = hub.watch("c")
    second = hub.watch("c")

    hub.publish("c", TextDelta("a"))

    assert tap.events == [TextDelta("a")]
    assert first.queue.get_nowait() == TextDelta("a")
    assert second.queue.get_nowait() == TextDelta("a"), "two readers are two readers"


def test_a_new_turn_replaces_the_tap_and_announces_where_it_began() -> None:
    hub: WatchHub[str] = WatchHub()
    hub.start_tap("c", TurnStarted("first", ""))
    watcher = hub.watch("c")

    later = hub.start_tap("c", TurnStarted("second", "agent:x"))

    assert watcher.queue.get_nowait() == TurnStarted("second", "agent:x")
    assert hub.tap("c") is later
    replay = hub.replay("c")
    assert replay is not None
    assert replay.started == TurnStarted("second", "agent:x")
    assert replay.events == []


def test_a_replay_is_a_copy_taken_at_the_joining_step() -> None:
    """The watcher's list is what stood when it registered; later events are
    only ever in the queue, so a replay and a live frame cannot be drawn twice."""
    hub: WatchHub[str] = WatchHub()
    tap = hub.start_tap("c", TurnStarted("hello", ""))
    hub.publish("c", TextDelta("a"))
    tap.recorded_turn = 7

    replay = hub.replay("c")
    assert replay is not None
    assert replay.recorded_turn == 7
    assert replay.events == [TextDelta("a")]

    hub.publish("c", TextDelta("b"))
    assert replay.events == [TextDelta("a")], "a copy, not the tap's list"


def test_publish_without_a_tap_still_serves_the_readers() -> None:
    hub: WatchHub[str] = WatchHub()
    watcher = hub.watch("c")

    hub.publish("c", TextDelta("a"))

    assert watcher.queue.get_nowait() == TextDelta("a")


def test_dropping_a_tap_leaves_the_readers_on_the_live_queue() -> None:
    hub: WatchHub[str] = WatchHub()
    hub.start_tap("c", TurnStarted("hello", ""))
    watcher = hub.watch("c")

    hub.drop("c")

    assert hub.replay("c") is None
    hub.publish("c", TextDelta("a"))
    assert watcher.queue.get_nowait() == TextDelta("a")


def test_drop_if_only_drops_the_tap_it_was_given() -> None:
    """A failed record can land after the next turn has started; dropping by key
    alone would take the live turn's tap with it."""
    hub: WatchHub[str] = WatchHub()
    first = hub.start_tap("c", TurnStarted("first", ""))
    second = hub.start_tap("c", TurnStarted("second", ""))

    hub.drop_if("c", first)
    assert hub.tap("c") is second

    hub.drop_if("c", second)
    assert hub.tap("c") is None


def test_unwatching_one_reader_leaves_the_other() -> None:
    hub: WatchHub[str] = WatchHub()
    first = hub.watch("c")
    second = hub.watch("c")

    hub.unwatch("c", first)
    hub.publish("c", TextDelta("a"))

    assert first.queue.empty()
    assert second.queue.get_nowait() == TextDelta("a")


def test_a_close_frame_ends_every_reader_and_a_second_close_is_silent() -> None:
    hub: WatchHub[str] = WatchHub()
    first = hub.watch("c")
    second = hub.watch("c")

    hub.close("c", "gone")

    assert first.queue.get_nowait() == Closed("gone")
    assert second.queue.get_nowait() == Closed("gone")

    hub.close("c", "again")
    hub.publish("c", TextDelta("a"))
    assert first.queue.empty(), "a closed reader is no longer a reader"
