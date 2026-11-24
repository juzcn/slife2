"""One page's size and start, as every listing asks for them."""

from __future__ import annotations

import pytest

from slife2.libs.paging import MAX_SQLITE_INT, page_window

pytestmark = pytest.mark.unit


def test_a_page_size_is_capped_and_an_absent_one_is_the_callers_default() -> None:
    assert page_window(10, 0, unset=50, cap=200) == (10, 0)
    assert page_window(1000, 0, unset=50, cap=200) == (200, 0)
    assert page_window(0, 0, unset=50, cap=200) == (50, 0)
    assert page_window(-1, 0, unset=50, cap=200) == (50, 0)


def test_a_negative_offset_is_floored_at_the_first_row() -> None:
    assert page_window(10, -5, unset=50, cap=200) == (10, 0)


def test_an_offset_past_sqlite_is_clamped_to_what_it_can_bind() -> None:
    """**A page start is bound to SQLite, and SQLite holds a signed 64-bit int.**

    A model can write more digits than that; binding the original raises
    `OverflowError` — an internal error from a call that means "past the end".
    Every listing takes its window here, so the clamp is the one answer: the
    page is empty and the offset echoed is the one applied, which keeps the
    caller's `offset + len < total` arithmetic true.
    """
    assert page_window(10, 2**63, unset=50, cap=200) == (10, MAX_SQLITE_INT)
    assert page_window(10, 10**400, unset=50, cap=200) == (10, MAX_SQLITE_INT)
    assert MAX_SQLITE_INT == 2**63 - 1
