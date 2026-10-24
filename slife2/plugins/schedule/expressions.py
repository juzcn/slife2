"""When a scheduled task next fires — the one module that knows cron.

**A cron expression is a question about a calendar, and this is where that
question is asked.**  It is a library rather than a plugin because there is
nothing here to hold: no credential, no connection, no loop — a parser and a
timezone policy, which is what SDD §2.4 calls a library.  The plugin that
*holds* the timers is `slife2.plugins.schedule.server` and it imports this.

**`croniter` underneath, and nothing outside this module imports it.**  The
reason is in the corners rather than in the fields: cron's `day_or` rule (a
restricted day-of-month and a restricted day-of-week are a **union**, not an
intersection) is nobody's first guess, `get_prev` is what a catch-up sweep
needs and hand-rolling it backwards is its own class of bug, and `is_valid`
with `strict=True` is what rejects `0 9 31 2 *` — February the 31st, a task
that would be stored enabled and could never fire.  Wrapping it here means two
things are this project's rather than a dependency's contract: the **timezone
policy** and the **error contract**.  A caller catches `ScheduleError` and has
caught everything; an unknown IANA zone surfaces as that, not as a
`ZoneInfoNotFoundError` escaping past a handler that only knows about cron.

**A fire is a wall-clock reading in the task's own timezone.**  That is the rule
the whole feature rests on — `0 9 * * *` means nine in the morning *where the
task says*, not nine in the morning wherever the machine happened to be when the
row was written — and it is what makes a timezone column worth having.

What cron itself leaves open, this module states rather than inherits, because a
person debugging a missed fire deserves an answer:

* **An empty timezone is the local zone as it is now.**  `zoneinfo` has no
  "system local" name, and the offset in effect this minute is not the offset in
  effect in six months, so a task with no zone named **does not follow a DST
  change** while one naming `Asia/Shanghai` (or `Europe/London`) does.  Being
  explicit is the fix and it costs one string.
* **A spring-forward gap fires once, at the instant the clock reached that
  reading.**  `Europe/London` at `0 1 * * *` on the last Sunday in March names a
  01:00 that does not exist; the fire lands at 02:00 BST, which is when the wall
  clock said 01:00.  The alternative — skipping the day — loses a day's work
  over a clock that was moved by somebody else.
* **A fall-back repeat fires once, at the first occurrence.**  The other 01:00 is
  an hour later and the task has already run.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

__all__ = [
    "ScheduleError",
    "is_valid",
    "latest_fire",
    "next_run",
    "previous_run",
    "zone",
]


class ScheduleError(ValueError):
    """An expression that cannot be read, or a fire that cannot be reached.

    One exception for both, deliberately: a caller is deciding what to tell a
    model, and "this cron is wrong" and "this cron never fires" are the same
    sentence to the model and the same branch to the caller.
    """


def zone(timezone: str) -> ZoneInfo | None:
    """The zone a task named, or raise naming it.

    Asked up front rather than at the first fire, because a task stored with a
    timezone that does not exist is a task that will fail at three in the morning
    with nobody watching.  `None` for the empty string, which is the local zone
    and not a zone this can hand back — see the DST note in the module docstring.
    """
    if not timezone:
        return None
    try:
        return ZoneInfo(timezone)
    except Exception as exc:  # noqa: BLE001 - KeyError, OSError, a missing tzdata
        raise ScheduleError(f"unknown timezone {timezone!r}: {exc}") from exc


def _reference(moment: datetime, timezone: str) -> datetime:
    """A moment restated in the zone the expression is to be read in.

    Naive input is taken as the system's local time, which is what
    `datetime.now()` returns and what `slife2.helpers.clock` stamps — so the one caller
    that has no zone to name still asks a question with an answer.
    """
    if moment.tzinfo is None:
        moment = moment.astimezone()
    found = zone(timezone)
    return moment.astimezone(found) if found is not None else moment


def is_valid(expression: str, *, strict: bool = True) -> bool:
    """Whether this is a cron expression that can fire.

    `strict` is on by default and that is the interesting half: it is what turns
    "the five fields parse" into "some date matches them", which is the question
    somebody writing a task is actually asking.  `0 9 31 2 *` parses and never
    fires, and a tool that accepted it would have stored a task whose only
    symptom is silence.
    """
    return croniter.is_valid(expression, strict=strict)


def _croniter(expression: str, reference: datetime) -> croniter:
    """A reader positioned at `reference`, on every path that needs one.

    `day_or=True` is cron's own rule and croniter's default, stated here anyway:
    it is the single most surprising thing about the five fields, and a default
    that happens to be right is a default a future reader has to go and look up.
    """
    if not is_valid(expression):
        raise ScheduleError(f"not a cron expression that can fire: {expression!r}")
    return croniter(expression, reference, day_or=True)


def next_run(expression: str, after: datetime, timezone: str = "") -> datetime:
    """The first fire strictly after `after`, in the task's zone.

    Strictly, because `after` is the anchor a fire already moved — asking
    "what is next" about a moment that is itself a fire must not answer with that
    same moment, or a poll would fire the same run forever.
    """
    reference = _reference(after, timezone)
    try:
        return _croniter(expression, reference).get_next(datetime)
    except ScheduleError:
        raise
    except Exception as exc:  # noqa: BLE001 - croniter's own error classes
        raise ScheduleError(f"no fire after {reference.isoformat()}: {exc}") from exc


def previous_run(expression: str, before: datetime, timezone: str = "") -> datetime:
    """The last fire strictly before `before`, in the task's zone."""
    reference = _reference(before, timezone)
    try:
        return _croniter(expression, reference).get_prev(datetime)
    except ScheduleError:
        raise
    except Exception as exc:  # noqa: BLE001 - croniter's own error classes
        raise ScheduleError(f"no fire before {reference.isoformat()}: {exc}") from exc


def latest_fire(
    expression: str, after: datetime, until: datetime, timezone: str = ""
) -> datetime | None:
    """The newest fire in `(after, until]`, or `None` if there was none.

    **This is the whole of what a poll needs, and it is one call rather than a
    list.**  A task due every five minutes that slife2 has been away from for a
    week has two thousand fires behind it, and none of them is actionable except
    the last: the ones before it were missed as surely as it was.  So the
    question is not "which fires happened" but "when did this last come due",
    which is `get_prev` asked from just past `until`.

    The `+1s` is what makes the window inclusive of `until` itself, since
    `get_prev` is strictly before its reference.  Cron's granularity is a minute,
    so a second cannot step over a fire.

    **Both halves of this are about instants rather than clock readings**, and
    that is the subtlety of asking a calendar question across a daylight-saving
    change.  Arithmetic on an aware datetime moves the naive fields and resets
    `fold`, so `reference + timedelta(seconds=1)` inside a fall-back hour can land
    an hour *away*; the nudge below is therefore one second of *instant*, made on
    the timestamp and restated in the task's zone.  And the anchor suppression
    compares `found` with `after` as instants: `found` and `after` carry the same
    `ZoneInfo` instance, and Python compares two aware datetimes with identical
    tzinfo by their naive fields (PEP 495) — the wrong question there, because a
    reading that looks newer than the anchor can be an hour *older* by the clock
    the fire happened on, so a wall-clock comparison silently drops every fire
    inside the repeated hour.  Instants are what "the newest fire behind this
    task" means, the same difference `due()` asks about when it measures lateness.

    **The one thing an instant comparison gets wrong is a reading the anchor has
    already been at**, and that is why the fall-back rule is stated rather than
    left to fall out.  A fall-back hour names the same wall reading twice, an hour
    apart, so the second occurrence is *strictly newer as an instant* than the
    anchor the first one left — and by instants alone the next poll finds it and
    fires it, which is the second fire the module docstring says does not happen.
    Equality of readings is the whole of the exception: `0 1 * * *` in
    `Europe/London` on the fall-back day fires at `01:00+01:00`, and the
    `01:00+00:00` an hour behind it is that same occurrence — while a task due
    every minute, anchored at `01:20+01:00`, still finds `01:12+00:00`, an older
    reading and a newer instant, which is a fire.
    """
    reference = _reference(until, timezone)
    nudge = datetime.fromtimestamp(reference.timestamp() + 1, reference.tzinfo)
    found = previous_run(expression, nudge, timezone)
    anchor = _reference(after, timezone)
    if found.timestamp() <= anchor.timestamp():
        return None
    # The same reading twice is the fall-back's one occurrence and not two.  As
    # *readings* — the zone stripped from both — because each has already been
    # restated in the task's zone, so "the same reading" is the task's wall clock
    # rather than this machine's.
    if found.replace(tzinfo=None) == anchor.replace(tzinfo=None):
        return None
    return found
