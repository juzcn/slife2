"""The grammar a model writes a time window in.

`turn_list` takes `since` and `until`, and a model that means "yesterday" is not
going to write `2026-10-06T00:00:00+08:00`.  So the bounds accept both, and this
is the single place that grammar is implemented — one function, one error, and
one sentence describing what it takes, which is what the tool's argument
documentation repeats.

It is v1's (`slife.timeutil`), ported because `clock.py` writes v1's timestamp
format for exactly this reason: the two systems write the same strings into the
same schema, so a window has to mean the same thing on both sides.  Two things
changed in the port and nothing else did — `relativedelta` is gone (see
`_shift_months`) and the `granularity` argument is gone with it, because slife2
has one kind of time column and it is a timestamp.

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

from slife2.clock import at, now

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
    of days — v1 reaches for `dateutil.relativedelta` to say so, and this is the
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


def _is_iso(value: str) -> bool:
    """Whether `value` is an ISO date or datetime we can compare against.

    `fromisoformat` covers a bare date too (midnight), and from 3.11 accepts a
    `Z` suffix as well as an explicit offset.
    """
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


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
    today = date.today()

    if key == "now":
        # A time of day, not a calendar date — so `since=now` is this second
        # rather than the start of today.
        resolved = now()
    else:
        day = _resolve_relative(key, role, today)
        if day is not None:
            resolved = day.isoformat()
        elif _is_iso(value.strip()):
            resolved = value.strip()
        else:
            raise InvalidTimeBound(_bound_error(role, value))

    if role == "until" and len(resolved) == 10 and "T" not in resolved:
        resolved = (date.fromisoformat(resolved) + timedelta(days=1)).isoformat()

    # A model may write UTC or some other offset, and the column holds local
    # time.  Comparing the two lexicographically is wrong exactly at the
    # boundary — the same instant written two ways is two different strings.
    if "T" in resolved:
        try:
            moment = datetime.fromisoformat(resolved)
        except ValueError:
            return resolved
        if moment.tzinfo is not None:
            resolved = at(moment)

    return resolved
