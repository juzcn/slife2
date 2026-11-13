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


def page_window(limit: int, offset: int, *, unset: int, cap: int) -> tuple[int, int]:
    """The page size and its start, both already bounded.

    `offset` is floored at zero for the same reason it is echoed back: it is the
    number the caller will add its page to, and a negative one would make the
    next page overlap this one.
    """
    return (min(int(limit), cap) if limit > 0 else unset), max(0, int(offset))
