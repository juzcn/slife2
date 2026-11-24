"""One page's size and start, from what a caller asked for.

Three listings ask the same question — the hub's and the families' rows, the
schedule's tasks and runs, and the store's SQL queries — and each answers with
the numbers it *applied*, because a caller pages by `offset` and cannot compute
its next page from a number the tool interpreted differently than it reported.

**A non-positive `limit` means "nobody said", not "no bound".**  A negative one
must never reach SQLite, where `LIMIT -1` means no limit at all: a model asking
for `-1` would walk past the ceiling the listing exists to keep.  What "nobody
said" should become is the caller's own fact — a default page, or the cap — so
it is a parameter rather than a rule buried here.
"""

from __future__ import annotations

#: The largest integer SQLite can bind — a signed 64-bit one — and therefore
#: the largest page start it can be asked for.  **A model can write more digits
#: than that**, and the bind raises `OverflowError` rather than answering the
#: empty page the number means; clamping here is the one place every listing
#: gets the answer, because every listing takes its window from this module.
MAX_SQLITE_INT = 2**63 - 1


def page_window(limit: int, offset: int, *, unset: int, cap: int) -> tuple[int, int]:
    """The page size and its start, both already bounded.

    `offset` is floored at zero for the same reason it is echoed back: it is the
    number the caller will add its page to, and a negative one would make the
    next page overlap this one.  It is capped at `MAX_SQLITE_INT` for the other
    end of the same arithmetic: a value past what SQLite can bind is a page no
    row can be on, and binding it would raise instead of answering.
    """
    return (
        min(int(limit), cap) if limit > 0 else unset,
        min(max(0, int(offset)), MAX_SQLITE_INT),
    )
