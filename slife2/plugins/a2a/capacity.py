"""The soft cap both A2A task tables keep.

A table is bounded in memory rather than by a timer: past the cap it forgets a
batch of the records least worth keeping, so the prune is not work on every
write.  The order is the policy — terminal records first, then live work, then a
paused task, whose peer is waiting on an answer somebody has to give.

**One copy of the order**, because the outbound task store and the inbound table
had two: the inbound one learned to put a paused task after a live round, and
the outbound one would have gone on evicting it first — the same record, read
two ways, in one plugin.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping

from slife2.plugins.a2a.states import INTERRUPTED, TERMINAL_STATUSES

#: How far below the cap a prune takes the table.
#:
#: **A batch and not one record**, because the prune sorts and doing that on
#: every write past the cap would pay for the order once per send.  The slack
#: is the room the next writes spend before the next sort.
SLACK = 50


def eviction_rank(status: str) -> int:
    """Where a record of *status* goes in the prune's order, lowest first."""
    if status in TERMINAL_STATUSES:
        return 0
    if status == INTERRUPTED:
        return 2
    return 1


def prune_to_cap[T](
    records: MutableMapping[str, T],
    cap: int,
    *,
    key: Callable[[T], str],
    status: Callable[[T], str],
    age: Callable[[T], float],
) -> None:
    """Forget the excess past *cap*, terminal first and paused last.

    A table at or below the cap is left alone.  Past it, a batch is taken and
    the oldest within each rank go first, so a burst of sends to slow peers
    cannot grow the table past the cap for long.
    """
    if len(records) <= cap:
        return
    excess = len(records) - cap + SLACK
    ordered = sorted(
        records.values(),
        key=lambda one: (eviction_rank(status(one)), age(one)),
    )
    for record in ordered[:excess]:
        records.pop(key(record), None)
