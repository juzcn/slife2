"""slife2-schedule — the clock, and the ledger of what it fired.

**A scheduled task is the one thing in this system that starts work nobody asked
for at the moment it starts.**  The model writes the definition — a name, a
description, a cron expression and a timezone — and from then on a clock fires
it, which is a thing no other plugin here does: an agent task is started by a
turn, a WeChat message arrives from a person, a turn comes from a terminal.  This
is the fourth way a conversation gets something to do, and the only one with no
person and no model behind it.

**It is a plugin because what it holds is timers** — the sentence `agent-task`
was justified with, and it means the same thing here.  A timer has a lifetime
that is not a turn's, and it is wrong anywhere but in a process of its own: in a
library each importer gets a clock, so two windows on one agent fire the same
task twice and a task one process wrote is invisible to the next; in the agent
server it would be a loop the conversation owns, which is the arrangement §3
exists to argue against.

**What it does not own is the loop, and that is why there is no model call in
this file.**  v1 split the feature three ways — *timing* | *execution* | *record*
— and the execution half was a dispatch into its subagent worker pool.  There is
no pool here (§9), so a fire does what §1.2 is for: `slife2-tui`'s `submit` puts
the task in front of the agent as a turn of its own, the agent server's
`send_message` is the fallback when no window is showing, and **the agent decides
what to do about it**.  Work that must run long is handed to `agent_task_start`
by the agent, in the turn the trigger opened.  So this plugin is a poll, a store
and seven tools, and the loop it assembles is no loop at all.

**What it does not own either is the product.**  v1's `memfiles` owned the
`schedules` *and* the `reports`, so `report_save` flipped a run to `ran` inside
the one write that stored the report.  Here the cabinet is a plugin of its own
and knows nothing about schedules (§1.4), so a run is closed by an explicit
`schedule_run_settle` carrying a one-line **note** of what it produced.  That is
one tool v1 did not have to expose, and it is the price of the split rather than a
coupling invented to hide it.

**The ledger is the feature.**  `pending → ran | failed | missed | skipped`, and
*no note means it failed*: a run is written **before** the hand-over, so the
anchor advances and a fire cannot repeat on the next poll; a hand-over that
refuses proves nothing was written and the run is `failed`; a hand-over that does
not come back is §1.2's third answer and leaves the row **pending**, because a
run whose fate is unknown must not be reported as either.  Coming up again, the
sweep fails what a stopped process left pending, and the poll records the newest
fire each task missed — and **neither is announced**.  v1 printed unsettled runs
into every turn's prompt; a note handed over on the trigger's channel is the
change that would replace it, and it is deferred rather than half-built
(DESIGN.md §9).  The ledger is already where the answer is and `schedule_runs` is
how it is read, so a **fire is the only thing this plugin ever says to a
conversation** — one path, one template, and a prompt nobody had to edit.

**No marker, and that is deliberate.**  A message handed over here wears only its
**channel** (`schedule:<name>`), which is what tells the window and the model
where it came from — and the template's own first line says which task, the way
`agent_task.j2` does.  There is nothing for a marker to add: the marker
vocabulary exists so a *result* can be matched to what produced it, a trigger is
not a result, and the answer a run eventually produces — when the agent runs it
as an agent task, which is the long case this feature is for — already arrives
wearing `[AsyncTaskResult:{…}]`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

from slife2 import schedule as expressions
from slife2.audience import FOR_THE_MODEL, request_client
from slife2.clock import at, now
from slife2.config import Config, load_cached
from slife2.handover import hand_over
from slife2.mcp_server import house_server, serve_plugin
from slife2.paths import schedule_dbs
from slife2.prompt import render
from slife2.schedule_store import (
    FAILED,
    MISSED,
    PENDING,
    RAN,
    RUNS_LIMIT,
    SKIPPED,
    STATUSES,
    UNSETTLED,
    Run,
    ScheduleStore,
    Task,
    moment_of,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-schedule"

#: This server's key in the config's `servers:` table.  **It owns no section**,
#: and that is the whole of what this line means: `wechat` and `tui` are the
#: other two with nothing to configure, and v1 had no section for this feature
#: either.  A machine that should not run scheduled tasks is a machine where
#: nobody writes one.
CONFIG_KEY = "schedule"

#: The seven tools.  v1's six over its own tables, plus the one it got for free —
#: `schedule_run_settle`, which there was the cabinet reporting a success.
SCHEDULE_SET = "schedule_set"
SCHEDULE_REMOVE = "schedule_remove"
SCHEDULE_LIST = "schedule_list"
SCHEDULE_RUNS = "schedule_runs"
SCHEDULE_RUN_NOW = "schedule_run_now"
SCHEDULE_RUN_SETTLE = "schedule_run_settle"
SCHEDULE_RUN_SKIP = "schedule_run_skip"

#: The channel a trigger arrives on, and the first half of what it is keyed by:
#: `schedule:daily_diary`.  The window draws a bubble from the channel alone
#: (`slife2.tui.widgets.channel_prefix`), so the name has to be *in* it for a
#: person to see which task spoke.
CHANNEL = "schedule"

#: How often the clock is asked.  Cron's unit is a minute, so this fires within
#: half a minute of the due time and no expression can tell the difference —
#: v1's number, and the reason it chose it.
POLL_SECONDS = 30.0

#: How late a fire may be and still count as *now* rather than as *missed*.  A
#: fire whose due time is within this of the moment the clock looked is one that
#: came due while slife2 was running; older than this, slife2 was not running,
#: and the right answer is to say so rather than to run yesterday's work at
#: today's prices.  v1's number.
MISS_GRACE_SECONDS = 120.0

#: What a task name may be.  Safe as a filename is not the requirement — nothing
#: is named after it — but safe as *a word a model says back* is, and the two
#: come out the same set: a leading letter or digit, then letters, digits, dot,
#: dash or underscore, at most 64 characters.  v1's expression.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: How much of a description a listing row shows.  The same glance
#: `agent_task_list` gives: `schedule_set` answers with the whole thing, and a
#: list answered with thirty descriptions in full is a list nobody reads.
DESCRIPTION_CHARS = 120

#: The most runs a listing will answer with.  The *default* is the store's
#: (`RUNS_LIMIT`), because that is a fact about reading a table that grows for as
#: long as a task lives; this is the tool's own ceiling, because a model asking
#: for ten thousand rows is a model asking for something no answer can carry.
RUNS_MAX = 200

#: The template.  Machinery rather than something an operator writes — it is how
#: this system tells a loop that nobody is listening, in the same sense
#: `agent_task.j2` is — so it is asked for by bare name and is the shipped one.
#: **One template, because this plugin says one thing**: a run that was missed
#: and a run that never settled are recorded and findable with `schedule_runs`,
#: and nothing announces them (DESIGN.md §9).
TRIGGER_TEMPLATE = "schedule.j2"

#: What a call that does not say which agent it is for is told.  A shared process
#: cannot guess it: one `slife2-schedule` serves every agent, and whose clock this
#: is, is a fact only the caller has.
UNADDRESSED = (
    "This call did not say which conversation it is for, so there is no agent "
    "whose schedule it could be — one process serves every agent, and a schedule "
    "is the agent's."
)


# ── the clock's decision ─────────────────────────────────────────────────────


def due(task: Task, reference: datetime | None = None) -> tuple[str, str] | None:
    """Whether this task has come due, and what to do about it.

    `("fire", due_at)` when the newest fire behind the task is fresh enough to
    run now, `("missed", due_at)` when it is older than the grace window, and
    `None` when there is nothing to do — which is the answer on almost every poll
    of almost every task, and therefore the one worth paying for.

    **One fire is ever considered, and it is the newest.**  A task due every five
    minutes that slife2 has been away from for a week has two thousand fires
    behind it and only the last one is a decision — the ones before it were
    missed as surely as it was, and a ledger with a row per minute of downtime is
    a ledger that hides the outage in its own noise.  This is v1's rule and v1's
    reason.

    The answer is a *pair* rather than a boolean because the two cases are the
    same question about the same moment and different things to do with it, and a
    caller that had to work out which is which would be asking cron again.
    """
    reference = reference or datetime.now()
    anchor = moment_of(task.last_due or task.created_at)
    found = expressions.latest_fire(task.cron, anchor, reference, task.timezone)
    if found is None:
        return None
    # Both sides are aware, so this is a difference of instants rather than of
    # clock readings — which is the only way it can be asked about a task whose
    # zone is not this machine's.
    late = (reference.astimezone() - found.astimezone()).total_seconds()
    return ("fire" if late <= MISS_GRACE_SECONDS else "missed", at(found))


def channel_for(name: str) -> str:
    """The channel a task's messages come in on: the key, and the task's name.

    **Every message this plugin hands over names a task**, and there is exactly
    one kind of message: a fire.  A hand-over carries the channel and the words
    and nothing else, so
    the channel is the only place "which task is this about" can live and still
    reach the window, which draws a bubble from the channel alone.
    """
    return f"{CHANNEL}:{name}"


# ── the ledgers ──────────────────────────────────────────────────────────────


def ledgers() -> list[ScheduleStore]:
    """Every agent's ledger on this machine, opened.

    A file whose owner is not written down is **skipped with a warning** rather
    than fired for the name of the directory it sits in: the directory holds the
    sanitised agent name, so a trigger sent to that name would go to a
    conversation that is not the one that asked — and it would look like it
    worked.
    """
    found: list[ScheduleStore] = []
    for path in schedule_dbs():
        store = ScheduleStore.adopt(path)
        if store is None:
            logger.warning(
                "%s does not say whose ledger it is, so nothing in it will be "
                "fired; it was not written by this build",
                path,
            )
            continue
        found.append(store)
    return found


async def fire(
    config: Config,
    store: ScheduleStore,
    task: Task,
    due_at: str,
    *,
    window_client: Client | None = None,
    agent_client: Client | None = None,
) -> Run:
    """Hand one due task to its agent, and record what came of it.

    **The record comes first.**  A run is written as `pending` before anything is
    handed over, which is what makes an interrupted fire *visible*: the anchor
    has moved, so the poll will not try it again, and a row that says `pending`
    is a thing the next start can find and the model can act on.  A row written
    after the hand-over would simply not exist in the case that matters.

    **A run that is already settled is not re-fired**, which is what makes
    `schedule_run_now` idempotent and what stops a poll that ran twice from
    handing the same work over twice.  See the module docstring for the three
    answers to a hand-over and why only one of them is an error.
    """
    run = store.record(task, due_at)
    if run.status != PENDING:
        return run
    handed = await hand_over(
        config,
        store.agent,
        render(
            TRIGGER_TEMPLATE,
            name=task.name,
            description=task.description,
            due_at=due_at,
        ),
        channel_for(task.name),
        what=f"scheduled task {task.name}",
        window_client=window_client,
        agent_client=agent_client,
    )
    if handed is False:
        # `False` is the one answer that proves nothing was written anywhere, so
        # the run is finished rather than left open — and it says why, because
        # "no conversation could be reached" is a fact about this machine rather
        # than about the task.
        settled = store.settle(
            task.name,
            due_at,
            FAILED,
            error="no conversation could be reached for this run",
        )
        assert settled is not None
        return settled
    # `True` and `None` both leave it pending: the first because the turn is what
    # closes it, the second because nothing is known.  A run whose fate is
    # unknown must not be reported as either.
    return run


async def poll_once(
    config: Config,
    *,
    window_client: Client | None = None,
    agent_client: Client | None = None,
    reference: datetime | None = None,
) -> list[str]:
    """One pass of the clock over every agent's ledger.

    Returns what it did, as lines — for the log, and because "it did nothing" is
    the ordinary answer and a caller should be able to see that rather than infer
    it from silence.  Split out of the loop that calls it so that a test can ask
    for exactly one pass, which is the only way to have a clock in a test that is
    not a sleep.
    """
    did: list[str] = []
    for store in ledgers():
        for task in store.tasks(enabled_only=True):
            verdict = due(task, reference)
            if verdict is None:
                continue
            kind, due_at = verdict
            if kind == "missed":
                run = store.record(task, due_at)
                if run.status == PENDING:
                    # Recorded and immediately closed: the moment is kept because
                    # the *gap* is the fact worth keeping, and "missed" is what
                    # says slife2 was not running rather than that it failed.
                    store.settle(
                        task.name,
                        due_at,
                        MISSED,
                        error="slife2 was not running when this was due",
                    )
                did.append(f"{store.agent}: {task.name} missed {due_at}")
                continue
            run = await fire(
                config,
                store,
                task,
                due_at,
                window_client=window_client,
                agent_client=agent_client,
            )
            did.append(f"{store.agent}: {task.name} due {due_at} -> {run.status}")
    return did


async def sweep_once(_config: Config) -> list[str]:
    """Close what a stopped process left open, and say nothing about it.

    The ledger half of "tasks fire only while slife2 is running".  A `pending`
    run is one whose trigger was written and whose process died before anything
    closed it, and it is **failed** rather than unknown — this never finished,
    and recording that is what lets an agent that looks decide whether to
    backfill it.  Nothing else is settled here: a `missed` or `failed` run from
    before is still a decision, and a decision is not this process's to make.

    **Nothing is announced.**  v1 printed unsettled runs into every turn's
    prompt; a note handed over on the trigger's channel is the change that would
    replace it, and it is deferred rather than half-built (DESIGN.md §9) because
    the ledger already holds the answer and `schedule_runs` is how it is read.  So
    a **fire is the only thing this plugin ever says to a conversation**, which is
    what keeps its whole surface one path with one template — and what makes "did
    it run?" a question with an answer rather than a message nobody asked for.
    """
    did: list[str] = []
    for store in ledgers():
        failed = store.sweep()
        if failed:
            did.append(f"{store.agent}: {failed} run(s) never settled")
    return did


async def poll_forever(
    config: Callable[[], Config],
    *,
    window_client: Client | None = None,
    agent_client: Client | None = None,
) -> None:
    """The loop: sweep once, then ask the clock every `POLL_SECONDS`.

    **One loop for every agent**, which is the second half of why this is a
    plugin rather than something each conversation does: two windows on one agent
    must not both fire its tasks.  The config is read through `config()` on every
    pass rather than captured, so an address that moved is picked up by the next
    fire — the same rule the agent server and the task server follow.
    """
    try:
        for line in await sweep_once(config()):
            logger.info("%s", line)
        while True:
            await asyncio.sleep(POLL_SECONDS)
            for line in await poll_once(
                config(), window_client=window_client, agent_client=agent_client
            ):
                logger.info("%s", line)
    except asyncio.CancelledError:
        # The ordinary way this ends.  Re-raised because the sweep of a
        # cancelled task is what tells `asyncio` the cancellation was taken —
        # and there is nothing here to unwind: a fire is a record and a
        # hand-over, both of which either happened or did not.
        raise
    except Exception:  # noqa: BLE001 - the clock must not die of one bad ledger
        logger.exception("the schedule clock stopped")


# ── the plugin ───────────────────────────────────────────────────────────────


def build_server(
    config: Config,
    *,
    window_client: Client | None = None,
    agent_client: Client | None = None,
    source: Callable[[], Config] | None = None,
    polling: bool = True,
) -> FastMCP:
    """Build the schedule server.

    **Two seams, and both are the hand-over's**: what a fire says to an agent is
    the only thing here that leaves the process, so a test exercising one wires a
    recorder window and an agent server over the in-memory transport and the rest
    is real — the real store on a temporary data directory, the real cron, the
    real ledger rules.  `source` is the config to read when the clock runs, so a
    test can hand in one no file backs.

    `polling=False` is for a test that wants the clock's *decisions* rather than
    its timing: the loop is not started, and `poll_once`/`sweep_once` are called
    directly.  A build that started a 30-second loop inside a test would be a
    test that either waits or races.
    """
    live: Callable[[], Config] = source or load_cached
    clock: asyncio.Task[None] | None = None

    def store_for(agent: str) -> ScheduleStore:
        return ScheduleStore.open(agent)

    def agent_of(ctx: Context) -> str:
        """The agent a call is on behalf of, refused rather than guessed."""
        found = request_client(ctx)
        if found is None or not found[0]:
            raise ToolError(UNADDRESSED)
        return found[0]

    def task_of(agent: str, name: str) -> Task:
        """One task, or a refusal naming what is missing."""
        found = store_for(agent).task(name)
        if found is None:
            raise ToolError(
                f"there is no scheduled task called {name!r} for this agent; "
                f"{SCHEDULE_LIST} says what there is."
            )
        return found

    def row(task: Task, reference: datetime | None = None) -> dict[str, Any]:
        """A task as a tool answers it, with when it next comes due.

        The next fire is computed rather than stored, because it is a fact about
        the expression and the clock rather than about anything this system wrote
        down — a stored one would be a second answer to keep in step with the
        task, and the one that goes stale after an edit.
        """
        answered = task.row()
        try:
            answered["next_run"] = at(
                expressions.next_run(
                    task.cron, reference or datetime.now(), task.timezone
                )
            )
        except expressions.ScheduleError as exc:
            # Only reachable for a row written by something that is not this
            # build — `schedule_set` refuses an unreachable expression — and a
            # listing that fails entirely over one bad row is worse than one that
            # says so in that row.
            answered["next_run"] = f"unreachable: {exc}"
        answered["description"] = task.description[:DESCRIPTION_CHARS]
        return answered

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        nonlocal clock
        if polling:
            clock = asyncio.create_task(
                poll_forever(
                    live, window_client=window_client, agent_client=agent_client
                )
            )
        try:
            yield {}
        finally:
            if clock is not None:
                clock.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await clock
                clock = None

    #  No `instructions`: each tool's own description says what the model cannot
    #  work out for itself — `house_server` says the same thing about the spec's
    #  own advice, and `agent_task_server` makes the same choice for the same
    #  four-word reason.
    mcp: FastMCP = house_server(SERVER_NAME, lifespan=lifespan)

    @mcp.tool(name=SCHEDULE_SET, meta=FOR_THE_MODEL)
    async def schedule_set(
        ctx: Context,
        name: str,
        description: str,
        cron: str,
        timezone: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Write a scheduled task, or replace one that exists.

        The clock runs it — not you, and not the person. When it comes due you
        are handed the description as a turn, with the order to run it as a
        background task of your own; say in `description` what to do and what
        "done" looks like, because it is the whole of what that task is given and
        nobody can answer a question it raises.

        `cron` is the usual five fields — `minute hour day-of-month month
        day-of-week` — and `0 9 * * *` is every morning at nine. `timezone` is an
        IANA name like `Asia/Shanghai`; left out, the task follows this machine's
        clock as it is now and therefore does not follow a daylight-saving
        change.

        This is the whole task, so anything you leave out is cleared. `enabled`
        false keeps it written down and never fires it.
        """
        agent = agent_of(ctx)
        name = name.strip()
        if not SAFE_NAME.match(name):
            raise ToolError(
                f"{name!r} cannot name a scheduled task: use a letter or digit "
                f"first, then letters, digits, `.`, `-` or `_`, up to 64 "
                f"characters."
            )
        if not description.strip():
            raise ToolError(
                f"scheduled task {name!r} needs a description — it is the whole "
                f"of what the task is handed when it comes due, and nobody can "
                f"answer a question it raises."
            )
        if not expressions.is_valid(cron):
            raise ToolError(
                f"{cron!r} is not a cron expression that can fire; five fields, "
                f"`minute hour day-of-month month day-of-week`, for example "
                f"`0 9 * * *`."
            )
        try:
            expressions.zone(timezone)
        except expressions.ScheduleError as exc:
            raise ToolError(str(exc)) from exc
        task = store_for(agent).put_task(
            name, description, cron, timezone, enabled=enabled
        )
        logger.info("scheduled task %s written for %s", name, agent)
        return row(task)

    @mcp.tool(name=SCHEDULE_REMOVE, meta=FOR_THE_MODEL)
    async def schedule_remove(ctx: Context, name: str) -> dict[str, Any]:
        """Forget a scheduled task, and everything it ever did.

        Deleting rather than switching off, and the difference is the history:
        `schedule_set(enabled=False)` keeps the ledger, and this takes it. Do it
        when the task is not a thing any more; switch it off when it is.
        """
        agent = agent_of(ctx)
        removed = store_for(agent).remove_task(name.strip())
        if not removed:
            raise ToolError(f"there is no scheduled task called {name!r}.")
        logger.info("scheduled task %s removed for %s", name, agent)
        return {"name": name.strip(), "removed": True}

    @mcp.tool(name=SCHEDULE_LIST, meta=FOR_THE_MODEL)
    async def schedule_list(ctx: Context) -> dict[str, Any]:
        """Every scheduled task this agent has, with when each next comes due.

        Each row carries the whole description, shortened, and `next_run` — which
        is what the expression means right now rather than something stored, so
        it is right the moment you read it. A task that is switched off is here
        too, and says so.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        return {"tasks": [row(task) for task in store.tasks()]}

    @mcp.tool(name=SCHEDULE_RUNS, meta=FOR_THE_MODEL)
    async def schedule_runs(
        ctx: Context, name: str = "", status: str = "", limit: int = 0
    ) -> dict[str, Any]:
        """What a scheduled task has actually done, newest first.

        `name` narrows it to one task, `status` to one state — `pending`, `ran`,
        `failed`, `missed` or `skipped` — and `limit` bounds the rows (at most
        200). Each row is one fire, named by the moment it was *due* rather than
        by when it ran, with the note whoever closed it left.

        This is the answer to "did it run", and it is the only place that answer
        exists: a run nobody closed stays here until somebody does.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        wanted = status.strip()
        if wanted and wanted not in STATUSES:
            raise ToolError(
                f"{wanted!r} is not a run state; they are {', '.join(STATUSES)}."
            )
        bounded = min(limit or RUNS_LIMIT, RUNS_MAX)
        rows = [run.row() for run in store.runs(name.strip(), wanted, bounded)]
        return {"runs": rows}

    @mcp.tool(name=SCHEDULE_RUN_NOW, meta=FOR_THE_MODEL)
    async def schedule_run_now(
        ctx: Context, name: str, due_at: str = ""
    ) -> dict[str, Any]:
        """Run a scheduled task now, or backfill a run that was missed.

        With `due_at` it re-opens **that** run — the same run, the one whose due
        time was in the trigger — and fires it, which is how a missed or failed
        run is answered. Without it, this is a new run due at this moment,
        for when somebody wants the work done early.

        The answer is the run's row: `pending` when the agent has been told about
        it and has not closed it yet, `failed` when no conversation could be
        reached. **It does not wait for the work** — a run is closed by
        `schedule_run_settle`, from the turn this starts.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        task = task_of(agent, name.strip())
        stamp = due_at.strip()
        if stamp:
            found = store.run(task.name, stamp)
            if found is None:
                raise ToolError(
                    f"{task.name!r} has no run due at {stamp!r}; {SCHEDULE_RUNS} "
                    f"lists what it has."
                )
            reopened = store.reopen(task.name, stamp)
            assert reopened is not None
            if reopened.status != PENDING:
                raise ToolError(
                    f"the run of {task.name!r} due at {stamp!r} is already "
                    f"{reopened.status}, and a run that has ended is not run "
                    f"again."
                )
        else:
            stamp = now()
        run = await fire(
            config,
            store,
            task,
            stamp,
            window_client=window_client,
            agent_client=agent_client,
        )
        logger.info("scheduled run of %s fired for %s", task.name, agent)
        return run.row()

    @mcp.tool(name=SCHEDULE_RUN_SETTLE, meta=FOR_THE_MODEL)
    async def schedule_run_settle(
        ctx: Context, name: str, due_at: str = "", note: str = ""
    ) -> dict[str, Any]:
        """Close a run that has been done, and say what it produced.

        `due_at` is which run — the timestamp the trigger carried. Left out, this
        closes the newest run that is still open, which is the right answer for
        the run you were just handed.

        The `note` is one line: what you found, or what you made, or where it
        went. It is what a later reading of the ledger has instead of the work
        itself, and a run closed with nothing to say is a run nobody can account
        for — but the run is closed all the same, and closing it is the part that
        matters.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        task = task_of(agent, name.strip())
        stamp = due_at.strip() or _newest_open(store, task)
        settled = store.settle(task.name, stamp, RAN, note=note.strip())
        if settled is None:
            raise ToolError(
                f"{task.name!r} has no run due at {stamp!r}; {SCHEDULE_RUNS} "
                f"lists what it has."
            )
        logger.info("scheduled run of %s settled for %s", task.name, agent)
        return settled.row()

    @mcp.tool(name=SCHEDULE_RUN_SKIP, meta=FOR_THE_MODEL)
    async def schedule_run_skip(
        ctx: Context, name: str, due_at: str = "", note: str = ""
    ) -> dict[str, Any]:
        """Close a run that is not going to happen, and say why.

        The same addressing as `schedule_run_settle` — the run by its due time,
        or the newest one still open — and the opposite ending. A skipped run is
        an answer rather than a gap: the ledger says the task came due, that
        nobody did it, and what the reason was.

        **A task that has come due is not yours to decline** — the trigger tells
        you to run it. This is for the runs that *did not happen*: one that could
        not be started, and one whose moment passed while slife2 was not running,
        where doing it late is the other answer and a `pending` row is the one
        ending that accounts for nothing.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        task = task_of(agent, name.strip())
        stamp = due_at.strip() or _newest_open(store, task)
        settled = store.settle(task.name, stamp, SKIPPED, note=note.strip())
        if settled is None:
            raise ToolError(
                f"{task.name!r} has no run due at {stamp!r}; {SCHEDULE_RUNS} "
                f"lists what it has."
            )
        logger.info("scheduled run of %s skipped for %s", task.name, agent)
        return settled.row()

    return mcp


def _newest_open(store: ScheduleStore, task: Task) -> str:
    """The run a settle with no `due_at` means: the newest one still open.

    Refused rather than answered with a guess when there is none, because a tool
    that invented a timestamp would close a run nobody asked about — and the
    model is the one that can go and look.
    """
    found = store.newest(task.name, UNSETTLED)
    if found is None:
        raise ToolError(
            f"{task.name!r} has no run waiting to be closed. If you have just "
            f"been handed one, pass its `due_at`."
        )
    return found.due_at


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=build_server,
        logger=logger,
        note=lambda _config: "the clock",
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
