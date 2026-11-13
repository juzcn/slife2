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
server it would be a loop the conversation owns, which is the arrangement SDD §5.5
exists to argue against.

**It does not own the loop, but it does own the dispatch.**  The split is two
ways — *timing and record* | *execution* — and the execution half is a dispatch
into an agent task, because this build has no worker pool to hand it to
(SDD §14.2).  A fire asks the terminal whether a window is showing the agent: a
window that is not there means the answer would have nowhere to go, so the run
is `missed` and the instance that comes up later decides whether to backfill it.
A window that is there is started directly as an ordinary agent task
(`agent_task_start`), in that window's home.  Nothing is put in front of the
model, and nothing in this file makes a model call: the loop is
`slife2-agent-task`'s, exactly as it is when a model asks for one.

**The plugin is the caller, so the ledger closes itself.**  `agent_task_start` is
answered with the task id, so a later poll can ask what became of it.  A prompt
that told a model to start the work *and close the run when the answer comes
back* would leave the id in a conversation and the run's fate to that model's
memory — and the answer arrives in a later turn, wearing a marker that says
nothing about schedules.  A run that is forgotten there is `pending` forever,
which is the one state the ledger cannot account for.

**The product is not this plugin's either.**  The cabinet is a plugin of its
own and knows nothing about schedules (SDD §8.3), so storing a report is not
the same write as closing a run.  The work is an agent task whose answer is
delivered back to the conversation by *that* plugin — so this ledger records
that a run happened, not what it produced.  What came of it is read where it was
said, and delivery is the agent-task plugin's fact rather than this ledger's: a
task is `finished` only when its answer reached the conversation, so a run whose
task could not deliver is closed `failed` and can be run again.

**The ledger is the feature.**  `pending → ran | failed | missed | skipped`.  A
run is written **before** the window is asked, so the anchor advances and a fire
cannot repeat on the next poll; a window that is not showing the agent closes
the run `missed`; a run whose task ends is `ran` or `failed` from what the task
did; a run the scheduler could not start or follow is `failed` too; and a run a
stopped process left `pending` is closed by the next start's sweep — `missed` if
it never started, `failed` if it had.  The poll also records the newest fire
each task missed while nothing was running — and **nothing is announced**: a
note handed over would be a second thing said to a conversation, and it is
deferred rather than half-built (SDD §14.2).  The ledger is where the answer is
and `schedule_runs` is how it is read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sqlite3
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.helpers.clock import at, now
from slife2.helpers.timeutil import InvalidTimeBound, normalize_bound
from slife2.libs.harness.handover import window_attached
from slife2.libs.host.config import (
    AGENT_TASK_SERVER_NAME,
    SCHEDULE_SERVER_NAME,
    Config,
    load_cached,
)
from slife2.libs.host.paths import home_key, in_home, schedule_dbs
from slife2.libs.paging import page_window
from slife2.libs.plugin.audience import FOR_THE_MODEL, conversation_meta
from slife2.libs.plugin.protocol import (
    agent_reader,
    close_server,
    house_server,
    open_server,
    serve_plugin,
    tool_payload,
)
from slife2.libs.plugin.vocabulary import (
    AGENT_TASK_LIST,
    AGENT_TASK_START,
)
from slife2.libs.plugin.vocabulary import (
    FINISHED as TASK_FINISHED,
)
from slife2.libs.plugin.vocabulary import (
    RUNNING as TASK_RUNNING,
)
from slife2.plugins.schedule import expressions
from slife2.plugins.schedule.store import (
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

SERVER_NAME = SCHEDULE_SERVER_NAME

#: This server's key in the config's `servers:` table.  **It owns no section**,
#: and that is the whole of what this line means: `wechat` and `tui` are the
#: other two with nothing to configure.  A machine that should not run scheduled
#: tasks is a machine where nobody writes one.
CONFIG_KEY = "schedule"

#: The six tools.  There is deliberately no `schedule_run_settle` — its
#: absence is the design: a run is closed by the harness when the background
#: task doing it ends, so no run waits on a model to remember it.
SCHEDULE_SET = "schedule_set"
SCHEDULE_REMOVE = "schedule_remove"
SCHEDULE_LIST = "schedule_list"
SCHEDULE_RUNS = "schedule_runs"
SCHEDULE_RUN_NOW = "schedule_run_now"
SCHEDULE_RUN_SKIP = "schedule_run_skip"

#: What a task name may be.  Safe as a filename is not the requirement — nothing
#: is named after it — but safe as *a word a model says back* is, and the two
#: come out the same set: a leading letter or digit, then letters, digits, dot,
#: dash or underscore, at most 64 characters.
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

#: The page `schedule_list` answers with, and the most it will answer with.
#: **The same ceiling the ledger's listing uses** (`RUNS_MAX`), and a default
#: between the two because the tables differ: a task row is a name, an expression
#: and a shortened description, and there is one per task somebody wrote down,
#: where a run row is one per fire and the ledger never stops growing.  A task listing
#: is therefore a page rather than a wall for the same reason the run listing
#: is — a model that has scheduled for a year is a model whose `schedule_list`
#: would otherwise be truncated by the harness and read back through `dump_read`
#: or `dump_grep` (`tool_result.tokens`).
TASKS_LIMIT = 50
TASKS_MAX = 200


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
    a ledger that hides the outage in its own noise.

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
    return ("fire" if late <= _T.timeouts.pacing.miss_grace else "missed", at(found))


class RunLocks:
    """One lock per scheduled task, for the length of this process.

    **A ledger is one file per agent, and this one process serves every agent
    and every home** — so a maker and its subagent, a turn and its own
    background task, are all callers of the same run.  Every one of the three
    operations that changes a run is a read, a decision and a write with a peer
    call between them (`fire`'s `record → window check → dispatch`,
    `schedule_run_now`'s `reopen → dispatch`, `schedule_run_skip`'s
    `find → settle`).  Two callers in that window both see "no task yet" and
    both dispatch: the second start is refused by the agent-task plugin (one
    name, one running task), and the refusal settles the *first* call's run
    `failed` while the work goes on.  The ledger then says the run did not
    happen while its answer is on its way.

    **Keyed by the task and not by the run**, because an empty `due_at` is
    "decide one now" and the moment it resolves to is what the operation is
    computing.  The map is bounded by the schedule tasks anybody has named —
    a handful — not by fires, which are unbounded.

    **The store's own writes are still guarded one level down** (`advance`,
    `settle`, `reopen`, `attach` are compare-and-sets): this lock is what makes
    a whole decision atomic, and the store's guards are what keep a write from
    two decisions that were never inside one lock from undoing each other.
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def for_task(self, store: ScheduleStore, name: str) -> asyncio.Lock:
        """This task's lock, made on the first ask.

        The key is `home_key`'s canonical spelling rather than the caller's: a
        tool call and a poll resolve the same ledger through two spellings of
        one directory, and two locks for one task is the race this class is.
        """
        key = (home_key(store.home), store.agent, name)
        found = self._locks.get(key)
        if found is None:
            found = asyncio.Lock()
            self._locks[key] = found
        return found

    @staticmethod
    @contextlib.asynccontextmanager
    async def held(
        locks: RunLocks | None, store: ScheduleStore, name: str
    ) -> AsyncIterator[None]:
        """Hold this task's lock, or nothing when the caller has no registry.

        A caller with no registry is a test's direct `poll_once`; production
        builds the registry in `build_server`, which is the one place that owns
        the process's state.
        """
        if locks is None:
            yield
            return
        async with locks.for_task(store, name):
            yield


# ── the ledgers ──────────────────────────────────────────────────────────────


def ledgers() -> list[ScheduleStore]:
    """Every agent's ledger on this machine, opened.

    A file whose owner is not written down is **skipped with a warning** rather
    than fired for the name of the directory it sits in: the directory holds the
    sanitised agent name, so a run started under that name would go to an agent
    that is not the ledger's — and it would look like it worked.

    **A file that cannot be opened is a file skipped, not a clock stopped**, and
    the skip is **loud**: a stray or truncated `schedule.db` raises out of
    `adopt` — SQLite will not read a file that is not a database — and the whole
    point of one loop over every agent is that one agent's damage is not every
    agent's.  Skipping is what keeps the clock serving the ledgers it can, and
    it is also the one failure the model cannot be shown: `schedule_list` reads
    the caller's own ledger, so an operator asking about *another* agent's tasks
    sees a healthy clock.  So the skip is an **error** naming the file, which is
    the only place it can be seen, and it is why the ownerless-file warning
    beside it is a different level: that file was never this build's and its
    skip was designed (SDD §9.4), where this one is a damaged ledger somebody has to
    go and look at (SDD Appendix C).

    **Only the failures that mean "this file cannot be opened" are skipped.**
    A broad `except Exception` would make a bug in `adopt` — or a schema this
    build wrote and then cannot read — look like an operator's damaged file, and
    the skip is the one shape that hides the clock's own defect.  A `TypeError`
    is the clock's and propagates; SQLite's and the filesystem's are the file's.
    """
    found: list[ScheduleStore] = []
    for ledger in schedule_dbs():
        try:
            store = ScheduleStore.adopt(ledger.path, ledger.home)
        except (OSError, sqlite3.DatabaseError):
            logger.error(
                "%s could not be opened, so nothing in it will be fired until "
                "an operator fixes it",
                ledger.path,
                exc_info=True,
            )
            continue
        if store is None:
            logger.warning(
                "%s does not say whose ledger it is, so nothing in it will be "
                "fired; it was not written by this build",
                ledger.path,
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
    tasks_client: Client | None = None,
) -> Run:
    """Start one due task's work, or record why it did not start.

    **A fire is not a turn.**  The work is an ordinary agent task and this
    plugin starts it itself, the same call `schedule_run_now` makes for a model.
    A trigger turn would spend a model call and a transcript row asking a model
    to press a button the plugin can press, and a model that did not press it
    would leave the run `pending` for ever (SDD §14.2).

    **The window is the gate, because the answer has nowhere else to go.**  A
    run whose agent has no window is `missed`, not started: the work would run
    and its answer would be delivered to nobody.  The instance that comes up
    later decides whether to backfill it (`schedule_run_now(name, due_at)`) or
    close it (`schedule_run_skip`).  A terminal that cannot be asked at all is a
    `failed` run with the reason on it — that is a peer that is gone, not a
    proven absence.

    **The record comes first.**  A run is written as `pending` before anything
    is asked or started, which is what makes an interrupted fire *visible*: the
    anchor has moved, so the poll will not try it again, and a `pending` row is
    a thing the next start can find.  A row written after the window was asked
    would simply not exist in the case that matters.

    **A run that is already settled is not re-fired**, which is what stops a
    poll that ran twice from starting the same work twice.
    """
    run = store.record(task, due_at)
    if run.status != PENDING:
        return run
    attached = await window_attached(config, store.agent, client=window_client)
    if attached is not True:
        if attached is False:
            settled = store.settle(
                task.name,
                due_at,
                MISSED,
                error="the agent's instance was not running when this was due",
            )
            logger.info(
                "scheduled run of %s for %s missed: no window is showing it",
                task.name,
                store.agent,
            )
        else:
            settled = store.settle(
                task.name,
                due_at,
                FAILED,
                error=(
                    "the terminal could not be asked whether the agent's "
                    "instance was running"
                ),
            )
            logger.warning(
                "scheduled run of %s for %s failed: the window could not be asked",
                task.name,
                store.agent,
            )
        assert settled is not None
        return settled
    try:
        return await dispatch(config, store, task, due_at, tasks_client=tasks_client)
    except ToolError as exc:
        # `dispatch` settles the run and raises so the calling model is told;
        # the poll's answer is the row, and an exception here would skip the
        # rest of this ledger's tasks.
        logger.warning(
            "scheduled run of %s for %s failed: %s", task.name, store.agent, exc
        )
        found = store.run(task.name, due_at)
        assert found is not None
        return found


async def tasks_server(
    config: Config, client: Client | None = None
) -> tuple[Client, bool]:
    """The agent-task plugin, and whether this call is the one that opened it.

    `slife2-agent-task` is the plugin that holds loops, and this one hands a run
    to it rather than running anything itself (SDD §9.4).  Opened per call and closed
    after it, the way a hand-over is: a dispatch happens once per run rather than
    once per turn, and a long-lived connection to a plugin that may restart is a
    connection to re-establish.  An injected client is used as it is and left
    open — it belongs to whoever handed it in.
    """
    if client is not None:
        return client, False
    return (
        await open_server(
            config.server("agent-task").url,
            name=AGENT_TASK_SERVER_NAME,
            fallback_tool=AGENT_TASK_LIST,
            timeout=_T.timeouts.transport.tasks,
        ),
        True,
    )


async def dispatch(
    config: Config,
    store: ScheduleStore,
    task: Task,
    due_at: str,
    *,
    tasks_client: Client | None = None,
) -> Run:
    """Start one run's work as a background task of the agent's own.

    **This is the half that makes the ledger need nothing from a model later.**
    The plugin makes the call, so it is handed the task's id, so it can ask what
    became of it on a later poll — where a prompt that told a *model* to start one
    would leave the id in a conversation and the run's fate to that model's
    memory (SDD §9.4).

    The work is an ordinary agent task: it runs as the agent, on the agent's own
    context, with the agent's prompt and tools, and its answer is delivered back
    to the conversation by `slife2-agent-task` — this plugin never hands the
    answer anywhere, and never sees it.

    **A dispatch that fails closes the run**, with the peer's own words as the
    error, and then raises so the turn that asked is told.  The alternative — a
    `pending` row with no task behind it — is the one state nothing can resolve:
    the poll cannot ask about a task that was never started, and the sweep would
    have to guess.
    """
    # The row exists before the call does, so that a dispatch which fails has
    # something to record the failure against — and so that the anchor moves
    # whether or not the work ever starts.
    store.record(task, due_at)
    # **Opening the peer is part of the dispatch.**  A `tasks_server` call sits
    # outside the `try` at the caller's peril: a failure to reach the agent-task
    # plugin — a port that is not listening, a peer that answers as somebody else
    # — would escape as a raw non-`ToolError` and leave the run `pending` with an
    # empty `agent_task_id`.  That is the one state nothing can resolve: the poll
    # cannot ask about a task that was never started, and the sweep only runs at
    # startup.  So the open is inside the same guard as the call, and both have
    # the same ending: the run is `failed`, and the turn that asked is told.
    client: Client | None = None
    owned = False
    try:
        client, owned = await tasks_server(config, tasks_client)
        answer = await client.call_tool(
            AGENT_TASK_START,
            {"name": task.name, "description": task.description},
            # The identity rides in `_meta` because a task is *an agent's*, and
            # this process serves every agent: without it the peer would have to
            # guess whose loop to start, and would refuse with its own
            # unaddressed answer (`slife2.libs.plugin.audience.UNADDRESSED`).
            meta=conversation_meta(store.agent),
            timeout=_T.timeouts.transport.tasks,
        )
    except Exception as exc:  # noqa: BLE001 - refusals and a missing peer alike
        store.settle(task.name, due_at, FAILED, error=str(exc))
        raise ToolError(
            f"the run of {task.name!r} due at {due_at} could not be started: {exc}"
        ) from exc
    finally:
        if owned and client is not None:
            await close_server(client)
    payload = tool_payload(answer)
    started = str(payload.get("task_id") or "")
    if not started:
        # **A task whose id cannot be read is a run the ledger cannot follow.**
        # The row is `pending` with an empty `agent_task_id`, which is the one
        # state nothing can resolve (the docstring above): `in_flight` skips it
        # and the poll has nothing to ask about.  So it takes the failed
        # dispatch's ending — the run is closed with the reason, and the turn
        # that asked is told.  The task itself may still be running; its answer
        # still reaches the conversation through the agent-task plugin.
        reason = (
            "the agent task was started, but the answer named no task id, so "
            "its ending cannot be followed"
        )
        store.settle(task.name, due_at, FAILED, error=reason)
        raise ToolError(
            f"the run of {task.name!r} due at {due_at} could not be followed: {reason}"
        )
    attached = store.attach(task.name, due_at, started)
    assert attached is not None  # the run was read a moment ago
    if attached.agent_task_id != started:
        # **The run was settled while the task was starting, and the task is
        # still running.**  `attach` is a compare-and-set, so it refused to put
        # an id on a row that is no longer `pending` — a skip landed between the
        # record and this write.  The answer still arrives through the
        # agent-task plugin, and the ledger keeps the decision that won; what
        # cannot happen is a second line here claiming the work is in flight.
        logger.warning(
            "the run of %s due at %s was closed while its task was starting; "
            "the task %s is still running",
            task.name,
            due_at,
            started,
        )
        return attached
    logger.info(
        "scheduled run of %s started for %s as %s", task.name, store.agent, started
    )
    return attached


async def reconcile(config: Config, *, tasks_client: Client | None = None) -> list[str]:
    """Close the runs whose background task has ended.

    The other half of the dispatch: a run in flight is `pending` with a task id on
    it, and this asks the peer what became of each one.  `finished` is `ran`, and
    **the moment comes off the task's own `finished_at`** rather than off the
    clock — the work ended when it ended, not when this poll happened to look.
    A task is `finished` only when its answer reached the conversation, so an
    undelivered answer arrives here as `failed`.

    `failed` and `cancelled` are both `failed` — and so is a delivery failure,
    because `finished` means the answer reached the conversation.  The ledger's
    question is whether the run's product arrived; the account of *why* is in the
    conversation when it was delivered, and in the log when it was not.

    **A task the peer no longer holds is `failed` too**, with the reason saying
    nothing was seen of its ending.  That is the honest answer rather than a
    `pending` row nobody can resolve: a task that has ended is kept for a while
    and then forgotten (`slife2.plugins.agent_task.server.RETIRED`), and one that is
    missing while this process is the one that started it did not go anywhere
    else.  A restart is the common case, and the sweep usually gets there first.
    """
    did: list[str] = []
    for store in ledgers():
        with in_home(store.home):
            did.extend(await _reconcile_one(config, store, tasks_client=tasks_client))
    return did


async def _reconcile_one(
    config: Config,
    store: ScheduleStore,
    *,
    tasks_client: Client | None,
) -> list[str]:
    """One ledger's runs asked after, **in that ledger's home**.

    The home is entered by the caller rather than here, because the ask and
    everything it reaches — the agent-task peer, the address `conversation_meta`
    stamps, the runs it settles against — are one home's view of one agent, and
    the walk is what knows which home that is (`schedule_dbs`).

    Returns what it closed, as lines, the way `poll_once` does: the caller is
    the one walking ledgers, so the caller is the one with something to say.
    """
    did: list[str] = []
    try:
        waiting = store.in_flight()
    except Exception:  # noqa: BLE001 - one bad ledger must not stop the rest
        logger.exception("%s: this ledger was skipped for this pass", store.path)
        return did
    if not waiting:
        return did
    try:
        listed = await _tasks_of(config, store.agent, tasks_client)
    except Exception as exc:  # noqa: BLE001 - a peer that is not there
        # Nothing is settled: this says nothing about the runs, only about
        # this process's view of them, and the next poll asks again.
        logger.warning("%s: the runs in flight were not checked: %s", store.agent, exc)
        return did
    for run in waiting:
        found = listed.get(run.agent_task_id)
        if found is None:
            store.settle(
                run.name,
                run.due_at,
                FAILED,
                error=(
                    "the background task doing this run is no longer held by "
                    "the agent-task plugin, so its ending was never seen"
                ),
            )
        elif found["status"] == TASK_RUNNING:
            continue
        elif found["status"] == TASK_FINISHED:
            store.settle(
                run.name,
                run.due_at,
                RAN,
                when=str(found.get("finished_at") or ""),
            )
        else:
            store.settle(
                run.name,
                run.due_at,
                FAILED,
                error=f"the background task ended as {found['status']}",
            )
        did.append(f"{store.agent}: {run.name} due {run.due_at} closed")
    return did


async def _tasks_of(
    config: Config, agent: str, client: Client | None
) -> dict[str, dict[str, Any]]:
    """This agent's background tasks, by id, as the agent-task plugin sees them."""
    peer, owned = await tasks_server(config, client)
    try:
        answer = await peer.call_tool(
            AGENT_TASK_LIST,
            {},
            meta=conversation_meta(agent),
            timeout=_T.timeouts.transport.tasks,
        )
    finally:
        if owned:
            await close_server(peer)
    rows = tool_payload(answer).get("tasks")
    if not isinstance(rows, list):
        raise ConnectionError(
            f"the agent-task plugin did not return a task list (it said {answer!r})"
        )
    return {
        str(row.get("task_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("task_id")
    }


async def poll_once(
    config: Config,
    *,
    window_client: Client | None = None,
    tasks_client: Client | None = None,
    reference: datetime | None = None,
    locks: RunLocks | None = None,
) -> list[str]:
    """One pass of the clock over every agent's ledger.

    **Two jobs, and they are the two ends of one run's life**: what has come due
    is fired, and what is in flight is asked after.  A poll that only did the
    first would leave every run `pending` for as long as nothing else closed it —
    which is what a prompt telling a *model* to close it amounts to (SDD §9.4).

    Returns what it did, as lines — for the log, and because "it did nothing" is
    the ordinary answer and a caller should be able to see that rather than infer
    it from silence.  Split out of the loop that calls it so that a test can ask
    for exactly one pass, which is the only way to have a clock in a test that is
    not a sleep.
    """
    did: list[str] = []
    # **Before the fires, not after them.**  A run whose task has finished but
    # whose row is still `pending` is settled first; otherwise the next fire of
    # the same task starts a new agent task under the same name, and
    # `agent-task:<name>` — the id both runs then carry — names only the newer
    # one.  The old run would be settled from the new task's ending, or stay
    # pending for its lifetime.  Asking after a just-started task would only
    # record `running`, so nothing is lost by asking before starting.
    did.extend(await reconcile(config, tasks_client=tasks_client))
    for store in ledgers():
        # **One bad ledger is one ledger skipped, not a pass abandoned.**  A row
        # with a hand-edited, non-ISO anchor makes `due` raise out of `moment_of`;
        # without this guard that raise would take the whole pass with it, and
        # the next pass would hit the same row — so every *other* agent's tasks
        # would quietly stop firing while the clock looked healthy.  The ledger is
        # named and skipped; the rest of this pass carries on.
        #
        # **And the pass runs in the ledger's own home.**  This process serves
        # every home on the machine (`schedule_dbs` walks them all), while the
        # ambient home is the one the daemon was started in — so everything a
        # fire reaches for is otherwise the *wrong* home's: `conversation_meta` stamps
        # the dispatch with it, the agent-task peer starts the work there, and
        # `window_attached` asks a window that is not showing this agent at all.
        # `in_home` is what makes the home a property of this
        # ledger's pass rather than of the process, which is the same move
        # `slife2-wechat` makes around each of its sessions.
        try:
            with in_home(store.home):
                # Unbounded on purpose: the poll is the one reader that wants
                # every enabled task, and it is why the page is optional there
                # (`ScheduleStore.tasks`).
                enabled, _ = store.tasks(enabled_only=True)
                for task in enabled:
                    # **The fire and a tool call for the same task are one
                    # decision** (`RunLocks`): a `schedule_run_now` landing in
                    # the record-and-dispatch would otherwise start the same run
                    # a second time.
                    async with RunLocks.held(locks, store, task.name):
                        verdict = due(task, reference)
                        if verdict is None:
                            continue
                        kind, due_at = verdict
                        if kind == "missed":
                            run = store.record(task, due_at)
                            if run.status == PENDING:
                                # Recorded and immediately closed: the moment is kept
                                # because the *gap* is the fact worth keeping, and
                                # "missed" is what says slife2 was not running rather
                                # than that it failed.
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
                            tasks_client=tasks_client,
                        )
                        did.append(
                            f"{store.agent}: {task.name} due {due_at} -> {run.status}"
                        )
        except Exception:  # noqa: BLE001 - one bad ledger must not stop the rest
            logger.exception("%s: this ledger was skipped for this pass", store.path)
            continue
    return did


async def sweep_once() -> list[str]:
    """Close what a stopped process left open, and say nothing about it.

    The ledger half of "tasks fire only while slife2 is running".  A `pending`
    run is one that was written and whose process died before anything closed
    it: one with no task was never started and is **missed**, and one that names
    a task started but had its ending lost and is **failed**.  Recording which
    is what lets an agent that looks decide whether to run it again.  Nothing
    else is settled here: a `missed` or `failed` run from before is still a
    decision, and a decision is not this process's to make.

    **Nothing is announced.**  A note to the window would announce one, and it
    is deferred rather than half-built (SDD §14.2) because the ledger already
    holds the answer and `schedule_runs` is how it is read — which is what
    makes "did it run?" a question with an answer rather than a message nobody
    asked for.
    """
    did: list[str] = []
    for store in ledgers():
        try:
            failed = store.sweep()
        except Exception:  # noqa: BLE001 - one bad ledger must not stop the rest
            logger.exception("%s: this ledger was skipped for this sweep", store.path)
            continue
        if failed:
            did.append(f"{store.agent}: {failed} run(s) never settled")
    return did


async def poll_forever(
    config: Callable[[], Config],
    *,
    window_client: Client | None = None,
    tasks_client: Client | None = None,
    locks: RunLocks | None = None,
) -> None:
    """The loop: sweep once, then ask the clock every `_T.timeouts.pacing.schedule_poll`.

    **One loop for every agent**, which is the second half of why this is a
    plugin rather than something each conversation does: two windows on one agent
    must not both fire its tasks.  The config is read through `config()` on every
    pass rather than captured, so an address that moved is picked up by the next
    fire — the same rule the agent server and the task server follow.

    **A failing pass is a failing pass, and the clock goes on.**  The guards are
    *inside* the loop, around the sweep and around each ask, because the failure
    that matters is a local one — a stray or truncated ledger, a row with an
    anchor that will not parse — and a guard outside the loop would turn one bad
    row on one agent into every agent's tasks going quiet until the process
    restarted, which is the opposite of what one shared clock is for.  Only a
    cancellation ends the loop.
    """
    try:
        for line in await sweep_once():
            logger.info("%s", line)
    except asyncio.CancelledError:
        # The ordinary way this ends.  Re-raised because the sweep of a
        # cancelled task is what tells `asyncio` the cancellation was taken —
        # and there is nothing here to unwind: a fire is a record and a
        # dispatch, both of which either happened or did not.
        raise
    except Exception:  # noqa: BLE001 - one bad ledger must not stop the clock
        logger.exception("the startup sweep failed; the clock goes on")

    while True:
        await asyncio.sleep(_T.timeouts.pacing.schedule_poll)
        try:
            for line in await poll_once(
                config(),
                window_client=window_client,
                tasks_client=tasks_client,
                locks=locks,
            ):
                logger.info("%s", line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad ledger must not stop the clock
            logger.exception("a pass of the schedule clock failed; it goes on")


# ── the plugin ───────────────────────────────────────────────────────────────


def build_server(
    config: Config,
    *,
    window_client: Client | None = None,
    tasks_client: Client | None = None,
    source: Callable[[], Config] | None = None,
    polling: bool = True,
) -> FastMCP:
    """Build the schedule server.

    **Three seams, and all three are peers**: what a fire says to an agent, and
    the agent-task plugin a run is handed to and asked about.  A test wires a
    recorder window, a recorder agent server and the real `agent-task` over the
    in-memory transport, and the rest is real — the real store on a temporary data
    directory, the real cron, the real ledger rules.  `source` is the config to
    read when the clock runs, so a test can hand in one no file backs.

    `polling=False` is for a test that wants the clock's *decisions* rather than
    its timing: the loop is not started, and `poll_once`/`sweep_once` are called
    directly.  A build that started a 30-second loop inside a test would be a
    test that either waits or races.
    """
    live: Callable[[], Config] = source or load_cached
    clock: asyncio.Task[None] | None = None
    #: This process's per-task locks (`RunLocks`).  Built here rather than at
    #: module level so two servers in one test do not share state, and handed to
    #: the clock as well as used by the tools — they are the same decisions.
    locks = RunLocks()

    def store_for(agent: str) -> ScheduleStore:
        return ScheduleStore.open(agent)

    agent_of = agent_reader("agent whose schedule it could be", "a schedule")

    def task_of(store: ScheduleStore, name: str) -> Task:
        """One task, or a refusal naming what is missing.

        Takes the store rather than reopening one, because every caller is
        inside the task's lock (`RunLocks`) and the store is what names it.
        """
        found = store.task(name)
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
                    live,
                    window_client=window_client,
                    tasks_client=tasks_client,
                    locks=locks,
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

        The clock runs it — not you, and not the person. When it comes due the
        clock starts it as an agent task of your own, with this description as
        the task; no turn is opened. Say in `description` what to do and what
        "done" looks like, because it is the whole of what that task is given and
        nobody can answer a question it raises.

        `cron` is the usual five fields — `minute hour day-of-month month
        day-of-week` — and `0 9 * * *` is every morning at nine. `timezone` is an
        IANA name like `Asia/Shanghai`; left out, the task follows this machine's
        clock as it is now and therefore does not follow a daylight-saving
        change.

        This is the whole task, so anything you leave out is cleared. `enabled`
        false keeps it written down and never fires it.

        Args:
            name: The task's name — how it is listed, run and removed.
            description: What to do when it comes due, and what "done" looks
                like. It is the whole of what that run is given.
            cron: Five fields — `minute hour day-of-month month day-of-week`.
            timezone: An IANA name like `Asia/Shanghai`; empty follows this
                machine's clock.
            enabled: False keeps the task but never fires it.
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

        Args:
            name: The task to forget, by name.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        # Under the task's lock: a removal between a fire's record and its
        # dispatch would leave a run whose task no longer exists, and a
        # removal between `run_now`'s lookup and its dispatch would do the same
        # while a background task started for it.
        async with RunLocks.held(locks, store, name.strip()):
            removed = store.remove_task(name.strip())
        if not removed:
            raise ToolError(f"there is no scheduled task called {name!r}.")
        logger.info("scheduled task %s removed for %s", name, agent)
        return {"name": name.strip(), "removed": True}

    @mcp.tool(name=SCHEDULE_LIST, meta=FOR_THE_MODEL)
    async def schedule_list(
        ctx: Context, limit: int = 0, offset: int = 0
    ) -> dict[str, Any]:
        """This agent's scheduled tasks, by name, with when each next comes due.

        Each row carries the whole description, shortened, and `next_run` — which
        is what the expression means right now rather than something stored, so
        it is right the moment you read it. A task that is switched off is here
        too, and says so.

        `limit` bounds this page (at most 200); left out it is 50. `offset` skips
        that many, which is how you page back.

        Args:
            limit: How many tasks this page may hold; 50 when left out.
            offset: How many tasks to skip — that is how you page.

        Returns:
            `tasks` — the rows of this page — and `total`, so
            `offset + len(tasks) < total` says whether more remain.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        bounded, skipped = page_window(
            limit, offset, unset=TASKS_LIMIT, cap=TASKS_MAX
        )
        found, total = store.tasks(limit=bounded, offset=skipped)
        return {
            "tasks": [row(task) for task in found],
            "total": total,
            "limit": bounded,
            "offset": skipped,
        }

    @mcp.tool(name=SCHEDULE_RUNS, meta=FOR_THE_MODEL)
    async def schedule_runs(
        ctx: Context,
        name: str = "",
        status: Literal["", "pending", "ran", "failed", "missed", "skipped"] = "",
        limit: int = 0,
        offset: int = 0,
    ) -> dict[str, Any]:
        """What a scheduled task has actually done, newest first.

        `name` narrows it to one task, `status` to one state — `pending`, `ran`,
        `failed`, `missed` or `skipped` — and `limit` bounds this page (at most
        200); left out it is 20. `offset` skips that many, which is how you page
        back through a ledger that only ever grows. Each row is one fire, named
        by the moment it was *due* rather than by when it ran, with the note
        whoever closed it left.

        This is the answer to "did it run", and it is the only place that answer
        exists: a run nobody closed stays here until somebody does.

        Args:
            name: Only this task's runs; empty lists every task's.
            status: Only this state; empty lists every state.
            limit: How many runs this page may hold; 20 when left out.
            offset: How many runs to skip — that is how you page.

        Returns:
            `runs` — the rows of this page — and `total`, so
            `offset + len(runs) < total` says whether more remain.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        wanted = status.strip()
        if wanted and wanted not in STATUSES:
            raise ToolError(
                f"{wanted!r} is not a run state; they are {', '.join(STATUSES)}."
            )
        bounded, skipped = page_window(
            limit, offset, unset=RUNS_LIMIT, cap=RUNS_MAX
        )
        found, total = store.runs(name.strip(), wanted, bounded, skipped)
        return {
            "runs": [run.row() for run in found],
            "total": total,
            "limit": bounded,
            "offset": skipped,
        }

    @mcp.tool(name=SCHEDULE_RUN_NOW, meta=FOR_THE_MODEL)
    async def schedule_run_now(
        ctx: Context, name: str, due_at: str = ""
    ) -> dict[str, Any]:
        """Start a scheduled run's work now, or backfill one that was missed.

        **A run that came due while the agent had no window is `missed`**, and
        this is how it is answered once the instance is up: call it with that
        run's `due_at` to re-open the same run and start the work as a
        background task of yours. It returns at once; do not wait for it and do
        not follow it up — the answer arrives on its own, and **the run closes
        itself on the next poll after that task ends**, so nothing here has to
        be remembered later.

        `due_at` names the run — the moment it was due, as the ledger holds it.
        Any spelling of that moment will do; a value that is not a moment is
        refused rather than answered with "no such run".

        With no `due_at` it is a new run due at this moment, for work somebody
        wants done early. With the `due_at` of a run that was **missed** or that
        failed to start, it re-opens *that* run — the same run, the same due time
        — which is how a gap is answered.

        The answer is the run's row, with the background task's id on it. A
        `pending` row means the work is in flight; `failed` means it could not
        be started or followed, or its answer could not be delivered, and the
        error says why.

        Args:
            name: The scheduled task, by name.
            due_at: The run's due time, as the ledger holds it; empty starts a
                new run due now.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        name = name.strip()
        # **The address is read now, not when this server was built.**  The
        # clock reads `live()` on every pass for exactly this reason: an edited
        # file can move `servers.agent-task`, and a manual run that kept
        # dialling the old address would fail while the clock's fires succeeded
        # (or the reverse after a partial restart).
        live_config = live()
        # **One decision at a time for this task** (`RunLocks`).  Everything
        # below is the run's state, and the same three steps are reached by a
        # maker, its subagent, the poll's fire and the model's own call.
        # `_reconcile_one`, the reopen, the in-flight check and the dispatch are
        # one sequence here because a second caller between any two of them
        # reads "no task yet" and dispatches again.
        async with RunLocks.held(locks, store, name):
            task = task_of(store, name)
            # **A finished run of this task is settled before another starts.**
            # `agent_task_start` names a task after the schedule task, and that
            # name is its id, so a still-`pending` predecessor would share the
            # id with this run.  The poll does the same before its fires; this
            # is the manual path, and it owes the same order.
            await _reconcile_one(live_config, store, tasks_client=tasks_client)
            stamp = due_at.strip()
            if stamp:
                stamp = _key_of(store, task.name, stamp)
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
            # A run already in flight is left alone rather than started twice:
            # the row carries the task doing it, and two calls for one due time
            # is a conversation retrying rather than a second run.
            found = store.run(task.name, stamp)
            if found is not None and found.agent_task_id and found.status == PENDING:
                logger.info(
                    "the run of %s due at %s is already in flight for %s",
                    task.name,
                    stamp,
                    agent,
                )
                return found.row()
            return (
                await dispatch(
                    live_config, store, task, stamp, tasks_client=tasks_client
                )
            ).row()

    @mcp.tool(name=SCHEDULE_RUN_SKIP, meta=FOR_THE_MODEL)
    async def schedule_run_skip(
        ctx: Context, name: str, due_at: str = "", note: str = ""
    ) -> dict[str, Any]:
        """Close a run that is not going to happen, and say why.

        The run by its due time — any spelling of that moment will do — or the
        newest one still open; and the opposite ending. A skipped run is an
        answer rather than a gap: the ledger says the task came due, that nobody
        did it, and what the reason was.

        **A run that has come due is not yours to decline** — the fire starts
        it, or records it `missed` when the instance is not there. This is for
        the runs that *did not happen*: one that could not be started, one whose
        moment passed while no instance was running, where doing it late is the
        other answer, and a run that sits open with nothing doing it until
        somebody says so.

        **And a run that is *being* done is not one of those.** `pending` with an
        `agent_task_id` means the work is happening now, and its answer is on its
        way to you however this call goes — so skipping it is refused, and the
        row you get back is the one that is still in flight. A run closes itself
        when the task doing it ends; `schedule_runs` says which run is which.

        Args:
            name: The scheduled task, by name.
            due_at: The run's due time; empty closes the newest open run.
            note: Why it is being skipped — this is what the ledger keeps.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        name = name.strip()
        # Under the task's lock: a skip that lands between a dispatch's record
        # and its attach would close a run whose background task is starting;
        # `attach` refuses the write, and the work would run with the ledger
        # saying it did not.
        async with RunLocks.held(locks, store, name):
            task = task_of(store, name)
            stamp = due_at.strip()
            stamp = (
                _key_of(store, task.name, stamp) if stamp else _newest_open(store, task)
            )
            settled = store.settle(task.name, stamp, SKIPPED, note=note.strip())
        assert settled is not None  # the run was read a moment ago
        # `settle` refuses this one — a `skipped` row would claim a run did not
        # happen while a background task went on producing an answer — and the
        # refusal is turned into words here, the way `schedule_run_now` turns
        # `reopen`'s refusal into words.  The other refusal `settle` makes, on a
        # run that already ended, is answered with the row as it stands: there
        # the model can see for itself what happened, and here it cannot see why
        # nothing did.
        if settled.status == PENDING and settled.agent_task_id:
            raise ToolError(
                f"the run of {task.name!r} due at {stamp!r} is being done: it is "
                f"running as the background task {settled.agent_task_id!r}, whose "
                f"answer is on its way to you. It closes itself when that task "
                f"ends, so there is nothing here to skip."
            )
        logger.info("scheduled run of %s skipped for %s", task.name, agent)
        return settled.row()

    return mcp


def _key_of(store: ScheduleStore, name: str, stamp: str) -> str:
    """The key a run answers to, for a `due_at` a model wrote — or the refusal.

    **A run is keyed by the string it was written with, and a model writes the
    same moment its own way.**  `+00:00` for a moment this ledger holds in local
    time, `20261013T110700`, a space where the column has a `T`, no offset at
    all: each of those is that instant and none of them is that key, so the
    lookup would answer "there is no run due at …" — the answer a real no-match
    gives.  `slife2.helpers.timeutil` exists to stop exactly that, and its own rule is
    that a bound nobody understood must not produce the same answer as a
    no-match, so a value in no grammar is told apart from a run that is not
    there.  Both refusals are here rather than at the two call sites because both
    tools take the same argument and would otherwise write the same two
    sentences.

    **The stamp as written is tried first, and that first try is not a courtesy.**
    `at()` puts an offset into *today's* local zone, so re-spelling a key that
    already matches would break a run written on the other side of a
    daylight-saving change: the row says `-05:00`, the machine now says `-04:00`,
    and the run stops being findable under its own due time.
    """
    if store.run(name, stamp) is not None:
        return stamp
    try:
        spelled = normalize_bound(stamp, role="due_at")
    except InvalidTimeBound as exc:
        raise ToolError(str(exc)) from exc
    if spelled != stamp and store.run(name, spelled) is not None:
        return spelled
    raise ToolError(
        f"{name!r} has no run due at {stamp!r}; {SCHEDULE_RUNS} lists what it has."
    )


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
