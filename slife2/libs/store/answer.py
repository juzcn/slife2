"""The one answer the three searches give.

`tool_search`, `cabinet_search` and `turn_search` are one question over three
corpora — the tool catalogue, the cabinet and a conversation's turns — and a
model that has learned one has learned all three.  They share both halves of
their spelling: the inputs (`keywords` for the words leg, `sentences` for the
meaning leg) and this envelope.

**What is shared is the page, not the row.**  A tool, a document and a turn are
not one thing, so each corpus keeps its own fields; what a reader should not
have to relearn is where the hits are, how sure the page is, and how big it is.
The envelope is those four answers in one shape, whichever corpus produced them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: Where "the meaning leg is sure" begins, and where "it found nothing" ends.
#:
#: Measured with the real embedder: 36 answerable catalogue queries scored
#: 0.477-0.782 and 10 unanswerable ones 0.389-0.512, so the bands overlap by
#: 0.035 and no cutoff is exact.  `MATCH_FLOOR` is the conservative end (every
#: unanswerable query caught, 32 of 36 answerable ones kept); `WEAK_FLOOR` exists
#: to be *said* rather than decided — the band between them is genuinely
#: undecided, and the tier is a sentence in the answer, not a row removed.
#:
#: **The words override both.**  A row whose text contains every word the caller
#: gave is certain whatever it scores — `pandoc` scores 0.514 against the row
#: *named* `mcp-pandoc`.
#:
#: **One scale, because the three corpora share an embedder.**  The measurement
#: is the catalogue's, and every search presents the same tier: a cosine comes
#: from the model rather than the corpus, and the tier labels a page without
#: cutting it.  A corpus with its own floors would make the same question answer
#: in three vocabularies again.
MATCH_FLOOR = 0.55
WEAK_FLOOR = 0.48


def confidence(best: float | None, *, worded: bool) -> str:
    """How sure this page is, as one of three words.

    **A judgement the model cannot make for itself**, which is why it is here and
    not left to the numbers: the floors are this system's, and a page of the ten
    nearest rows says nothing about whether any of them is for the thing asked —
    the same shape came back for a query a corpus can answer and for one it
    cannot, and the caller was left to guess which it had.

    `sure` is a hit above the match floor, `weak` is one above the weak floor (or
    a page the caller's own words reached at all — that evidence is certain where
    a cosine is graded), and `none` is neither.  `best` is `None` when nothing on
    the page was scored by meaning; that is "not measured", never a zero, and it
    is why the word evidence is a separate argument.

    **And never a count of matches**, because there is no such number to give:
    the floors label a page and do not cut it — a weak answer still shows its
    page — so a count would be the page size wearing the word "found".
    """
    if best is not None and best >= MATCH_FLOOR:
        return "sure"
    if worded or (best is not None and best >= WEAK_FLOOR):
        return "weak"
    return "none"


def search_answer(
    results: Sequence[Mapping[str, Any]],
    *,
    scores: Sequence[float | None] = (),
    worded: bool = False,
    browsed: bool = False,
    limit: int,
) -> dict[str, Any]:
    """A search's page, dressed in the one envelope.

    `results` are the hits in the order the search meant them to be read, under
    the search's own key rather than a listing's `entries` — a hit is the thing
    that was sought, and the noun is the same whichever corpus answered.

    `scores` are the meaning scores the page carries (`None` where nothing was
    measured); `best` is their highest, so the number a reader sees is the one
    that ordered the page.  `worded` says the caller's own words were found in
    at least one hit — certain evidence, which is what lets a words-only page
    be `weak` rather than `none`.  `browsed` says the page is not a search at
    all: no words and no sentence were given, the corpus was listed newest
    first, and no verdict was asked or given.  `limit` is the page size
    *applied*, so a reader can trust it beside a page it may page through
    elsewhere (`turn_list`).

    **A browse is not a low-confidence search.**  Its rows may be exactly what
    the caller wants — newest first is the right answer to "what is here" — so
    `confidence` is `None` rather than `none`, and the two questions stay
    distinguishable in the answer.
    """
    measured = [score for score in scores if isinstance(score, float)]
    best = max(measured) if measured else None
    return {
        "results": list(results),
        "best": best,
        "confidence": None if browsed else confidence(best, worded=worded),
        "browsed": browsed,
        "limit": limit,
    }
