"""The grammar a model writes a time window in.

`turn_list` takes `since` and `until`, and a model that means "yesterday" is not
going to write `2026-10-06T00:00:00+08:00`.  So the bounds accept both, and this
is the single place that grammar is implemented — one function, one error, and
one sentence describing what it takes, which is what the tool's argument
documentation repeats.

`clock.py` writes the same timestamp format for the same reason: the same
strings go into the same schema, so a window has to mean the same thing wherever
it is read.  Two things are deliberately absent — `relativedelta` (see
`_shift_months`) and the `granularity` argument that went with it, because
slife2 has one kind of time column and it is a timestamp.

**A period word anchors to the period's edge, not to today.**
`since=last month` is the first of last month and `until=last month` is its last
day — the same word is two different instants, and neither is "today minus a
month", which for a mid-month today lands twenty-odd days off.

**Anything unrecognized raises.**  It used to pass through unchanged, on the
theory that the SQL layer would reject it — and SQLite does not reject it:
`created_at >= '上个月'` is a string comparison that matches nothing, so a bound
nobody understood produced the same empty answer as a real no-match.  That is
the one outcome a window must never produce silently.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta

from slife2.helpers.clock import at, now

#: What the grammar accepts, spelled once so the sentence a model reads and the
#: code that decides cannot drift apart.
BOUND_GRAMMAR = (
    "an ISO date or datetime, or one of: today, yesterday, tomorrow, now, "
    "last|this week|month|quarter|year, or '<N> day(s)|week(s)|month(s)|year(s) ago'"
)


class InvalidTimeBound(ValueError):
    """A `since`/`until` written in no grammar this module speaks."""


#: Day words, as name -> offset in days from today.
_DAY_WORDS: dict[str, int] = {"yesterday": -1, "today": 0, "tomorrow": 1}

#: Calendar periods `last`/`this` may name, as name -> months per period
#: (`week` is 0: weeks are day-based, see `_shift_period`).
_PERIOD_MONTHS: dict[str, int] = {
    "week": 0,
    "month": 1,
    "quarter": 3,
    "year": 12,
}

_PERIOD_RE = re.compile(r"^(last|this)\s+(week|month|quarter|year)$")
_AGO_RE = re.compile(r"^(\d+)\s+(day|week|month|year)s?\s+ago$")


def _bound_error(role: str, value: str) -> str:
    return f"invalid {role} bound {value!r} — expected {BOUND_GRAMMAR}"


def _shift_months(day: date, months: int) -> date:
    """A date shifted by whole months, clamped to a shorter month's end.

    `timedelta` has no month or year unit, because a month is not a fixed number
    of days — the usual answer is `dateutil.relativedelta`, and this is the
    same arithmetic in six lines rather than a dependency slife2 would carry for
    one function.  The clamp is the part that matters and the part that is easy
    to get wrong: `2026-03-31` minus a month is `2026-02-28`, not an exception
    and not the third of March.
    """
    total = day.year * 12 + (day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _shift_period(start: date, period: str, count: int) -> date:
    """Shift a period's first day by `count` whole periods.

    `start` must already *be* a period start, so the month arithmetic cannot
    clamp — day 1 never overflows a shorter month.
    """
    if period == "week":
        return start + timedelta(weeks=count)
    return _shift_months(start, _PERIOD_MONTHS[period] * count)


def _period_start(day: date, period: str) -> date:
    """The first day of the period containing `day` (weeks start Monday)."""
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    if period == "quarter":
        return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
    if period == "year":
        return date(day.year, 1, 1)
    raise InvalidTimeBound(_bound_error("since", period))


def _resolve_period(which: str, period: str, role: str, today: date) -> date:
    """`last|this <period>` as an edge of that period.

    `since` takes the period's first day and `until` its last, so one word
    bounds the window from either end rather than naming one instant.
    """
    current = _period_start(today, period)
    first = current if which == "this" else _shift_period(current, period, -1)
    if role == "until":
        return _shift_period(first, period, 1) - timedelta(days=1)
    return first


def _resolve_ago(count: int, unit: str, today: date) -> date:
    """`<N> <unit> ago` — a *point*, deliberately not period-anchored.

    `3 days ago` names a day, not a period, so unlike `last week` it is measured
    back from today rather than snapped to a boundary.
    """
    if unit == "day":
        return today - timedelta(days=count)
    if unit == "week":
        return today - timedelta(weeks=count)
    if unit == "month":
        return _shift_months(today, -count)
    return _shift_months(today, -12 * count)


def _resolve_relative(key: str, role: str, today: date) -> date | None:
    """The date a relative phrase names, or `None` if it is not one."""
    if key in _DAY_WORDS:
        return today + timedelta(days=_DAY_WORDS[key])
    period = _PERIOD_RE.match(key)
    if period:
        return _resolve_period(period.group(1), period.group(2), role, today)
    ago = _AGO_RE.match(key)
    if ago:
        return _resolve_ago(int(ago.group(1)), ago.group(2), today)
    return None


def _canonical(value: str) -> str | None:
    """An ISO date or datetime, in the exact shape the `created_at` column holds.

    **Normalised rather than passed through, and that is the whole of what this
    is for.**  The comparison is lexicographic against
    `YYYY-MM-DDTHH:MM:SS+HH:MM`, and `fromisoformat` accepts several other
    spellings of the same instant — `20261007`, `2026-W41-2` — which a model
    does write.  Compared as written, `20261007` is shorter than every row's
    stamp and sorts *before* all of them: `since=20261007` matched nothing and
    `until=20261007` matched everything, which is a window silently empty one
    way and silently unbounded the other.  The module's own rule is that
    anything it cannot place raises.

    A date and a datetime are told apart here because the caller's `until` rule
    needs to know which it was: a date means a day, and a day ends at its end.
    `None` for a string that is neither.
    """
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value).isoformat(sep="T")
    except ValueError:
        return None


def _today() -> date:
    """Today, as this module reads it — **and the seam a test pins it through**.

    A function rather than an inline `date.today()` in `normalize_bound` for the
    same reason `slife2.libs.turn.tokens` resolves its vocabulary through one:
    a test that asserts what "yesterday" means has to say *which day* it is
    asking about.  Reading the runner's clock from both sides — the expectation
    built with `date.today()` in the test and the answer resolved with another
    call in here — makes a run whose two readings straddle midnight, or a month
    end, fail for a reason that has nothing to do with the grammar.  Pinning this
    is what turns "yesterday" into a fact.
    """
    return date.today()


def normalize_bound(value: str, *, role: str = "since") -> str:
    """A model-written bound, as a string the `created_at` column compares with.

    `role` is the bound's own name — `"since"` or `"until"` — and it is not
    decoration: it is what anchors a period word to the period's first or last
    day, and what advances a date-only `until` by one day.

    That last one is worth stating because it looks like an off-by-one.  A bound
    of `2026-10-07` against a column holding `2026-10-07T09:31:02+08:00` sorts
    *before* every turn written that day, so `until=yesterday` would silently
    exclude yesterday.  A date means a day, and a day ends at its end.

    Raises:
        InvalidTimeBound: If the value is in no grammar this module speaks.
    """
    # Internal whitespace collapsed, so "last   month" reads as "last month".
    # Nothing here is cached: every word resolves against `today`, read per
    # call, so a server that has been up for weeks cannot answer with the date
    # it started on.
    key = " ".join(value.strip().lower().split())
    today = _today()

    if key == "now":
        # A time of day, not a calendar date — so `since=now` is this second
        # rather than the start of today.
        resolved = now()
    else:
        day = _resolve_relative(key, role, today)
        if day is not None:
            resolved = day.isoformat()
        else:
            canonical = _canonical(value.strip())
            if canonical is None:
                raise InvalidTimeBound(_bound_error(role, value))
            resolved = canonical

    if role == "until" and len(resolved) == 10 and "T" not in resolved:
        resolved = (date.fromisoformat(resolved) + timedelta(days=1)).isoformat()

    # A model may write UTC or some other offset, and the column holds local
    # time.  Comparing the two lexicographically is wrong exactly at the
    # boundary — the same instant written two ways is two different strings.
    #
    # **A bound that carries no offset is put through `at` too.**  A bare
    # `2026-10-07T09:31:02` means the second the wall clock in this zone read,
    # which is the column's own reckoning — but as a *string* it is a prefix of
    # the column's `2026-10-07T09:31:02+08:00` and sorts just before the instant
    # it names.  That made `until` fall one second short (the turn at the named
    # second was excluded) while `since` did not, so two spellings of one instant
    # gave two windows and the spelling a model is likeliest to write dropped the
    # turn.  `at` attaches the local offset, which is the shape the column holds
    # and the only one where the comparison means the second it says.
    if "T" in resolved:
        try:
            moment = datetime.fromisoformat(resolved)
        except ValueError:
            # Unreachable by construction — `resolved` here is either
            # `_canonical`'s output or `now()`, both written by `fromisoformat`
            # — but it raises rather than returning the bound un-normalised:
            # skipping the local-offset rewrite is the one thing this module
            # guarantees, so a value that reached here unparseable is a bound it
            # cannot place (SDD Appendix C).
            raise InvalidTimeBound(_bound_error(role, value)) from None
        resolved = at(moment)

    return resolved


def can_place(value: str, *, role: str = "since") -> bool:
    """Whether :func:`normalize_bound` can place *value* — the question a caller
    asks while a bad bound is still something it can decide about.

    **The same knowledge, asked before the raise instead of after it.**  A
    caller that hands a model-written bound straight to the store finds out it
    is unreadable as an `InvalidTimeBound` out of the middle of a query — which
    is the right thing for a caller with no answer, and the wrong thing for one
    that has a better answer available.  `slife2.libs.turn.context`'s discriminator reply
    is the second kind: a bound it cannot place is a reply it should drop, and
    an exception out of there fails a whole turn that had a perfectly good
    fallback.

    A predicate rather than a second parser: `"now"`, a period word, a relative
    phrase and an ISO spelling are four grammars this module speaks, and a
    caller that reimplemented "is this one of them" would be the second place a
    grammar is written down.
    """
    try:
        normalize_bound(value, role=role)
    except InvalidTimeBound:
        return False
    return True
