"""One harness line for a degrade that has no tool answer to ride.

**A fallback that continues is a fact somebody is owed.**  The tool paths put
their damage in the answer the model reads; the ones that happen between turns
— a skipped trim, a ledger that will not open, a tokenizer that is a damaged
install — have no such answer.  This is their route to the person watching: one
line in the agent's window, written through `note_to_window`, the same road
`⚡ jill online` takes.

**Once per process per code**, because these are persistent conditions rather
than events: a tokenizer that cannot be read would otherwise write a line per
estimate, and a cursor file that cannot be written one per poll.  The first
occurrence is the news; the rest are the same fact.

**The line is not the Agent's channel, and this module does not pretend it is.**
A degrade the model must act on belongs in the tool result or the harness pair
that answers the call.  A note no window took is dropped — it was about a
moment that passed — and the warning below remains in the daemon's log.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from slife2.libs.harness import notes
from slife2.libs.harness.handover import note_to_window

if TYPE_CHECKING:  # pragma: no cover - import cycle for typing only
    from slife2.libs.host.config import Config

logger = logging.getLogger(__name__)

#: `(agent, code)` this process has already drawn a line for.  A persistent
#: condition is one line; a repaired-and-broken-again process is a new run.
_seen: set[tuple[str, str]] = set()

#: One report at a time, so two calls racing on the same code draw one line.
_lock = asyncio.Lock()


def reset() -> None:
    """Forget what this process has reported — for a test, never a caller."""
    _seen.clear()


async def report(
    config: Config,
    agent: str,
    code: str,
    effect: str,
    reason: str,
    *,
    client: Any = None,
) -> bool:
    """Say, once, that a degrade happened; answer whether a line was drawn.

    `effect` is what the person can no longer count on ("scheduled tasks are
    not firing"); `reason` is the cause and the repair ("the ledger at … could
    not be opened; fix or delete it").  The warning is written whether or not a
    window took the note, so `slife2 logs` keeps the trace.

    A failure in the note itself does not propagate: this is bookkeeping after
    the fact, and a window that is gone must not turn a degrade report into
    another failure.
    """
    key = (agent, code)
    async with _lock:
        if key in _seen:
            return False
        _seen.add(key)
    logger.warning(
        "degrade code=%s agent=%s effect=%s reason=%s", code, agent, effect, reason
    )
    try:
        drawn = await note_to_window(
            config, agent, notes.degraded(effect, reason), client=client
        )
    except Exception as exc:  # noqa: BLE001 - a note is not worth a second failure
        logger.debug("the degrade note for %s did not come back: %s", agent, exc)
        return False
    return bool(drawn)
