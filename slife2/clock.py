"""The one clock.

Everything slife2 writes down happens at a time, and those times have to be
comparable — a turn's `created_at` against its `completed_at`, a daemon record
against the claim that names it, a `created_at` range query against the index
over that column.  Letting each writer spell the format out is how that quietly
stops being true: a single `+00:00` or a microsecond precision among local
seconds-precision strings breaks both lexicographic ordering and any bound that
falls inside the same second.

**Local time with an explicit offset, seconds precision.**  Local, because these
are timestamps a person reads and compares against the clock on the wall — "when
did I ask that" is not a question UTC answers helpfully.  With an offset,
because a bare local string is ambiguous the moment two machines in different
zones write to the same store.  Seconds, because nothing here needs more, and
coarser precision makes two events in the same second compare equal rather than
merely close — which is what a reader assumes anyway.

This is v1's convention (`slife.timeutil.now_local_seconds`), kept because the
databases are v1's schema and a future port of its time-window queries should
compare correctly against rows written here.
"""

from __future__ import annotations

from datetime import datetime


def at(moment: datetime) -> str:
    """A given moment, written the way everything here writes time.

    The second caller is `slife2.timeutil`, which takes a bound a model wrote —
    `+00:00`, a bare `Z`, whatever the provider's clock said — and has to put it
    back into the local-offset form the `created_at` column holds.  A bound
    converted anywhere else would be the same string shape written twice, which
    is the drift this module exists to prevent.
    """
    return moment.astimezone().isoformat(timespec="seconds")


def now() -> str:
    """The current wall clock, as `YYYY-MM-DDTHH:MM:SS+HH:MM`."""
    return at(datetime.now())
