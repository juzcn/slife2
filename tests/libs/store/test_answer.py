"""The one envelope the three searches answer with.

Each server dresses its own rows; what is shared is the page around them.  The
cases here are the ones a reader has to be able to tell apart: a ranked page
with a number, a page only the words found (no number, and still evidence), a
browse (no verdict at all), and an empty ranked page (a real "none").
"""

from __future__ import annotations

import pytest

from slife2.libs.store.answer import search_answer

pytestmark = pytest.mark.unit


def test_a_ranked_page_carries_its_best_score_and_a_verdict() -> None:
    answer = search_answer(
        [{"name": "one"}, {"name": "two"}],
        scores=[0.31, 0.72],
        worded=True,
        limit=10,
    )
    assert answer == {
        "results": [{"name": "one"}, {"name": "two"}],
        "best": 0.72,
        "confidence": "sure",
        "browsed": False,
        "limit": 10,
    }


def test_a_page_only_the_words_found_has_no_score_and_is_still_evidence() -> None:
    answer = search_answer([{"name": "one"}], scores=[None], worded=True, limit=10)

    assert answer["best"] is None, "nothing was measured, which is not a zero"
    assert answer["confidence"] == "weak", (
        "the words are certain where a cosine is graded"
    )


def test_a_browse_is_not_a_low_confidence_search() -> None:
    answer = search_answer([{"name": "one"}], scores=[None], browsed=True, limit=20)

    assert answer["browsed"] is True
    assert answer["best"] is None
    assert answer["confidence"] is None, "a browse judged nothing"


def test_an_empty_ranked_page_says_none() -> None:
    answer = search_answer([], limit=10)

    assert answer["best"] is None
    assert answer["confidence"] == "none"
