"""The time-window grammar a model writes.

The parts worth testing are the parts that are *not* obvious: a period word names
an edge rather than an instant, `<N> months ago` clamps at a month end, and a
bound nobody understands has to be an error rather than an empty window.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta

import pytest

from slife2.helpers import timeutil
from slife2.helpers.timeutil import BOUND_GRAMMAR, InvalidTimeBound, normalize_bound
from tests.fakes import StubEmbedder

pytestmark = pytest.mark.unit


def _today() -> date:
    return date.today()


#: The day every word-meaning test is asked about.  A fixed date rather than the
#: runner's, because the alternative is two readings of one clock: the
#: expectation built with `date.today()` in the test and the answer resolved with
#: another call inside the module, which disagree whenever a run straddles
#: midnight — and, for a period word, a month end.  `2026-10-07` is a Wednesday
#: in the middle of a month and a quarter, which is what the week, month and
#: quarter tests below need to be interesting.
DAY = date(2026, 10, 7)


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    """Pin the module's day, so a word's meaning is a fact rather than a race.

    The module reads its day through `slife2.helpers.timeutil._today` precisely so
    that this can be done; see that function for why the seam is there.
    """
    monkeypatch.setattr(timeutil, "_today", lambda: DAY)
    return DAY


# --- ISO, and the offsets it arrives in ---------------------------------------


def test_an_iso_date_passes_through() -> None:
    assert normalize_bound("2026-01-31") == "2026-01-31"


def test_a_non_canonical_iso_bound_is_the_date_it_names() -> None:
    """**`fromisoformat` accepts spellings the column does not hold.**

    The comparison against `created_at` is lexicographic, and `20261007` is
    shorter than every `YYYY-MM-DDTHH:MM:SS+HH:MM` stamp — so as written it
    sorts *before* all of them, which made `since=20261007` match no turn at all
    and `until=20261007` match every one. A window silently empty one way and
    silently unbounded the other, from a spelling the module claims to accept.
    """
    assert normalize_bound("20261007") == "2026-10-07"
    assert normalize_bound("20261007", role="until") == "2026-10-08", "a day ends late"
    assert normalize_bound("2026-W41-2") == "2026-10-06", "a week date is a date too"
    # The seconds are filled in; the offset is the new part, and the test below
    # owns that one.
    assert (
        datetime.fromisoformat(normalize_bound("2026-10-07T09:30"))
        == datetime(2026, 10, 7, 9, 30).astimezone()
    )
    # And something no parser knows is still a refusal rather than a window.
    with pytest.raises(InvalidTimeBound):
        normalize_bound("2026-10")


def test_a_utc_datetime_becomes_local() -> None:
    """The column holds local time, and the model may write any offset.

    Left alone, `2026-01-01T00:00:00Z` and `2026-01-01T08:00:00+08:00` are the
    same instant written two ways and compare as two different strings — which
    is a window that is wrong by the size of the offset, at the boundary only,
    and therefore never noticed.

    **Asserted against this machine's offset rather than a fixed `+08:00`.**  The
    offset the conversion produces is the one fact here that is not this
    module's to decide, and a runner in UTC is a machine where "becomes local"
    is a no-op: the old assertion named `+08:00` and passed only on a clock set
    to it, so CI failed on all three platforms for six days.  What every
    timezone shares is the property below — the instant survives, it comes back
    in local time, and two spellings of it become one string.
    """
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    local = normalize_bound("2026-01-01T00:00:00Z")

    # The same instant, in local time.  Against the offset of *that* moment
    # rather than of today: a zone that observes DST does not have one offset.
    assert datetime.fromisoformat(local) == moment
    assert datetime.fromisoformat(local).utcoffset() == moment.astimezone().utcoffset()

    # Which is the whole point: one instant written two ways, one string.
    assert normalize_bound("2026-01-01T08:00:00+08:00") == local


def test_a_bound_with_no_offset_is_local_time_too() -> None:
    """**The spelling a model is likeliest to write is the one that drops the turn.**

    `2026-10-07T09:31:02` and `2026-10-07T09:31:02+08:00` name one instant, but
    the bare string is a prefix of the column's own and sorts just before it —
    so `until=BARE` excluded the turn stored at exactly that second while
    `until=+08:00` kept it, and there was no matching edge on `since`.  Two
    spellings of one instant, two windows, at the boundary only and therefore
    never noticed.

    **Written against this machine's offset rather than a fixed `+08:00`.**  The
    column holds local time, so the bare spelling can only be read at the local
    offset — and a runner in UTC is a machine where that offset is `+00:00`.  A
    fixed-`+08:00` assertion would pass only on a clock set to it, the failure
    `test_a_utc_datetime_becomes_local` above is already on record for.  What
    every timezone shares is that the two spellings become one string, which is
    what is asserted.
    """
    moment = datetime(2026, 10, 7, 9, 31, 2)
    bare = moment.isoformat(sep="T")  # no offset, which is what a model writes
    local = moment.astimezone().isoformat(timespec="seconds")

    assert "T" in bare and "+" not in bare, "the spelling under test carries no offset"
    assert normalize_bound(bare) == local
    assert normalize_bound(bare, role="until") == local, "and the end of the window too"


def test_a_bound_that_reaches_the_offset_step_unparseable_is_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Unreachable-by-construction code that weakens an invariant is gone.**

    The re-parse that puts a bound back into local-offset form used to `return
    resolved` on failure — the un-normalised string — which is exactly the
    silent window the module exists to prevent.  The branch cannot happen
    through `normalize_bound`'s own grammars, so it is driven here by handing
    the offset step a value `fromisoformat` refuses: it now raises
    `InvalidTimeBound` rather than returning the bound as written.
    """
    import slife2.helpers.timeutil as timeutil

    monkeypatch.setattr(timeutil, "_canonical", lambda value: "2026-10-07Txx:yy")
    with pytest.raises(InvalidTimeBound):
        normalize_bound("half past whenever")


# --- day words ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "offset"),
    [("yesterday", -1), ("today", 0), ("tomorrow", 1)],
)
def test_a_day_word_is_that_day(word: str, offset: int, today: date) -> None:
    expected = (today + timedelta(days=offset)).isoformat()
    assert normalize_bound(word, role="since") == expected


def test_case_and_spacing_do_not_matter() -> None:
    assert normalize_bound("  Last   Week  ") == normalize_bound("last week")


# --- period words, which name an edge -----------------------------------------


def test_a_period_word_is_a_different_instant_from_each_end(today: date) -> None:
    """The point of `role`, and the thing a reader gets wrong.

    `since=last month` is its first day and `until=last month` its last — one
    word bounding a window from either end, rather than naming one instant that
    a caller would then have to know how to extend.

    **Both ends exactly, and not "or the first of this month".**  The line here
    used to be `since == first-of-this-month or since < until`, which passed on
    its second clause — the same claim as the line below it — so the one thing
    this test is named for, that the two roles disagree, was asserted by nothing.
    `until` is the day *after* last month ends, which is the module's own
    date-means-a-whole-day rule and worth spelling out here rather than left to
    the weekday test below.
    """
    since = normalize_bound("last month", role="since")
    until = normalize_bound("last month", role="until")

    # `until` is the day *after* last month ends — the module's date-means-a-
    # whole-day rule — so the two ends are the 1st and the 1st, one month apart.
    assert (
        since == (today.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
    )
    assert until == today.replace(day=1).isoformat()
    assert datetime.fromisoformat(since).day == 1
    assert datetime.fromisoformat(until) > datetime.fromisoformat(since)


def test_a_period_is_not_today_minus_a_month(today: date) -> None:
    """`last month` snaps to the calendar; "a month ago" does not.

    For a mid-month today the two differ by twenty-odd days, so this is the
    difference between a window that holds last month and one that holds half
    of it.
    """
    snapped = datetime.fromisoformat(normalize_bound("last month", role="since"))
    measured = datetime.fromisoformat(normalize_bound("1 month ago", role="since"))

    assert snapped.day == 1
    assert measured.day == today.day


def test_a_week_starts_on_monday(today: date) -> None:
    monday = datetime.fromisoformat(normalize_bound("this week", role="since")).date()
    assert monday.weekday() == 0
    assert today - monday < timedelta(days=7)


def test_a_quarter_starts_on_a_quarter() -> None:
    start = datetime.fromisoformat(normalize_bound("this quarter", role="since")).date()
    assert (start.month - 1) % 3 == 0
    assert start.day == 1


# --- offsets, which name a point ----------------------------------------------


@pytest.mark.parametrize("unit", ["day", "week", "month", "year"])
def test_an_ago_offset_is_measured_back_from_today(unit: str, today: date) -> None:
    resolved = datetime.fromisoformat(
        normalize_bound(f"1 {unit} ago", role="since")
    ).date()
    assert resolved < today


def test_an_ago_offset_clamps_at_a_short_month() -> None:
    """`timedelta` cannot say "a month", so slife2 has six lines instead of
    reaching for `relativedelta`, and the case that decides whether they are
    right is the one where the day does not exist in the target month: the 31st
    of March minus a month is the 28th of February, not the 3rd of March and not
    an exception.
    """
    from slife2.helpers.timeutil import _shift_months

    assert _shift_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert _shift_months(date(2024, 3, 31), -1) == date(2024, 2, 29)
    assert _shift_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert _shift_months(date(2026, 12, 15), 1) == date(2027, 1, 15)


def test_the_plural_and_the_singular_are_the_same_word() -> None:
    assert normalize_bound("3 days ago", role="since") == normalize_bound(
        "3 day ago", role="since"
    )


# --- the two bounds that mean a whole day -------------------------------------


def test_a_date_only_until_includes_that_whole_day() -> None:
    """The off-by-one that looks like an off-by-one and is not.

    `created_at` carries a time, so `created_at <= '2026-10-06'` sorts before
    everything written on the 6th — `until=yesterday` would exclude yesterday,
    silently, which is the worst shape a window can fail in.
    """
    assert normalize_bound("2026-10-06", role="until") == "2026-10-07"
    assert normalize_bound("2026-10-06", role="since") == "2026-10-06"


def test_until_yesterday_reaches_the_end_of_yesterday(today: date) -> None:
    resolved = datetime.fromisoformat(normalize_bound("yesterday", role="until"))
    assert resolved.date() == today
    assert resolved.hour == 0


def test_now_is_a_time_of_day_and_not_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`now` is a second, and the test says which one rather than reading a clock.

    Asserting `resolved.hour == datetime.now().hour` compared the module's
    reading of the clock against the test's own, one line later: an hour boundary
    crossed in between fails a test about the grammar.  The moment is pinned
    instead.

    **And the pin is the moment, not the clock that reads it.**  `normalize_bound`
    hands the column a *local* string (`helpers/clock.at` converts), so a pinned
    `+08:00` comes back as `09:31` here and as `01:31` on a UTC runner — which is
    what this asserted, and why it failed on every CI lane and nowhere else.  The
    rule is the one `test_a_bound_with_no_offset_is_local_time_too` states two
    tests below: what every timezone shares is the *instant*, and an aware
    datetime compares by exactly that.
    """
    pinned = datetime.fromisoformat("2026-10-07T09:31:02+08:00")
    monkeypatch.setattr(timeutil, "now", lambda: "2026-10-07T09:31:02+08:00")

    resolved = datetime.fromisoformat(normalize_bound("now", role="since"))

    assert resolved == pinned, "the same moment, in whatever zone the runner keeps"
    # `resolved.date() == date(2026, 10, 7)` stood here too, and it was the same
    # mistake one field over: a runner at UTC−3 reads this instant as the *6th*.
    # And the claim in the name — a time of day, not the start of one, which is
    # what a `now` floored to 00:00 produced.  This one is about a clock, so it
    # is said of the runner's own: the pinned instant is 01:31:02 UTC, whose
    # seconds-of-day is not a whole number of hours, and no zone's offset turns a
    # time like that into midnight.
    assert resolved.time() != time(0, 0)


# --- what it refuses ----------------------------------------------------------


@pytest.mark.parametrize("value", ["上个月", "whenever", "19/07/2026", "soon", ""])
def test_a_bound_in_no_known_grammar_is_an_error(value: str) -> None:
    """Not an empty window, which is what passing it through produced.

    SQLite compares the unknown string lexicographically and matches nothing, so
    a bound nobody understood answered exactly like a real no-match — an answer
    that looks like information and is not.
    """
    with pytest.raises(InvalidTimeBound):
        normalize_bound(value, role="since")


def test_the_error_says_what_the_grammar_is() -> None:
    with pytest.raises(InvalidTimeBound, match="until bound"):
        normalize_bound("whenever", role="until")
    assert "today" in BOUND_GRAMMAR and "ago" in BOUND_GRAMMAR


def test_the_tool_tells_the_model_the_grammar_it_implements() -> None:
    """The sentence is single-sourced, the way a `description=` argument would
    single-source it.

    A tool's docstring has to be a string *literal* to become `__doc__` at all,
    so the grammar cannot be interpolated into it from here — which makes this
    the check that it was copied rather than paraphrased.  A model told one
    grammar and refused by another has no way to tell which one is lying, and
    what it gets back is a `ToolError` it reads as its own mistake.

    It is the *parameter's* schema the sentence has to reach, not the tool's
    description: FastMCP lifts a docstring's `Args:` block out into one schema
    entry per argument, which is the better place for it anyway — the grammar is
    read on the argument it is about.

    Compared with whitespace collapsed, because the sentence is wrapped in the
    source and arrives unwrapped.
    """
    assert _flat(BOUND_GRAMMAR) in _flat(_since_schema("turn_list")["description"])
    assert _flat(BOUND_GRAMMAR) in _flat(_since_schema("turn_search")["description"])


def _since_schema(tool: str) -> dict:
    """That tool's `since` as the model is handed it."""
    from fastmcp import Client

    from slife2.libs.host.config import default_config
    from slife2.plugins.context.server import build_server

    async def read() -> dict:
        async with Client(
            build_server(default_config(), embedder=StubEmbedder())
        ) as client:
            (found,) = [one for one in await client.list_tools() if one.name == tool]
        return found.input_schema["properties"]["since"]

    return asyncio.run(read())


def _flat(text: str) -> str:
    return " ".join(text.split())
