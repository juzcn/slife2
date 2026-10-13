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

**It does not own the loop, and it does own the dispatch.**  v1 split the feature
three ways — *timing* | *execution* | *record* — and the execution half was a
dispatch into its subagent worker pool, which this build has no equivalent of
(§9).  So a fire does what §1.2 is for: `slife2-tui`'s `submit` puts the task in
front of the agent as a turn of its own, in the window running that conversation —
and that turn has **one** instruction, to call `schedule_run_now`, which is this
plugin *starting the work itself* as an ordinary agent task.  A window that is
not showing that agent is a failure, and the run ends `failed` with the reason on
it rather than waiting for a turn nobody is running.  Nothing in this file makes a model call: the loop is
`slife2-agent-task`'s, exactly as it is when a model asks for one.

**That split is what makes the ledger close itself, and it is the whole reason
the dispatch is here rather than in the prompt.**  The model's job is one
synchronous call it cannot half-do; the plugin is the caller, so the plugin is
handed the task's id, so a later poll can ask what became of it.  A prompt that
told a model to start the work *and close the run when the answer comes back*
would leave the id in a conversation and the run's fate to that model's memory —
and the answer arrives in a later turn, wearing a marker that says nothing about
schedules.  A run that is forgotten there is `pending` forever, which is the one
state the ledger cannot account for.

**The product is not this plugin's either.**  v1's `memfiles` owned the
`schedules` *and* the `reports`, so `report_save` flipped a run to `ran` inside
the one write that stored the report.  Here the cabinet is a plugin of its own
and knows nothing about schedules (§1.4), and the work is an agent task whose
answer is delivered back to the conversation by *that* plugin — so this ledger
records that a run happened, not what it produced.  What came of it is read where
it was said.

**The ledger is the feature.**  `pending → ran | failed | missed | skipped`.  A
run is written **before** the trigger is handed over, so the anchor advances and
a fire cannot repeat on the next poll; a hand-over that refuses proves nothing was
written and the run is `failed`; a hand-over that does not come back is §1.2's
third answer and leaves the row **pending**, because a run whose fate is unknown
must not be reported as either.  Coming up again, the sweep fails what a stopped
process left pending, and the poll records the newest fire each task missed — and
**neither is announced**.  v1 printed unsettled runs into every turn's prompt; a
note handed over on the trigger's channel is the change that would replace it, and
it is deferred rather than half-built (DESIGN.md §9).  The ledger is where the
answer is and `schedule_runs` is how it is read, so a **fire is the only thing
this plugin ever says to a conversation** — one path, one template, and a prompt
nobody had to edit.

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

import slife2.timeouts as _T
from slife2 import schedule as expressions
from slife2.agent_task_server import (
    AGENT_TASK_LIST,
    AGENT_TASK_START,
)
from slife2.agent_task_server import (
    FINISHED as TASK_FINISHED,
)
from slife2.agent_task_server import (
    RUNNING as TASK_RUNNING,
)
from slife2.audience import FOR_THE_MODEL, client_meta
from slife2.clock import at, now
from slife2.config import AGENT_TASK_SERVER_NAME, Config, load_cached
from slife2.handover import DeliveryFailed, hand_over
from slife2.mcp_server import (
    agent_reader,
    close_server,
    house_server,
    open_server,
    serve_plugin,
    tool_payload,
)
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
from slife2.timeutil import InvalidTimeBound, normalize_bound

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-schedule"

#: This server's key in the config's `servers:` table.  **It owns no section**,
#: and that is the whole of what this line means: `wechat` and `tui` are the
#: other two with nothing to configure, and v1 had no section for this feature
#: either.  A machine that should not run scheduled tasks is a machine where
#: nobody writes one.
CONFIG_KEY = "schedule"

#: The six tools, which is v1's six over its own tables.  The seventh it had —
#: `schedule_run_settle` — is *gone*, and its absence is the design: a run is
#: closed by the harness when the background task doing it ends, so nothing asks
#: a model to remember anything after the turn it was handed the trigger in.
SCHEDULE_SET = "schedule_set"
SCHEDULE_REMOVE = "schedule_remove"
SCHEDULE_LIST = "schedule_list"
SCHEDULE_RUNS = "schedule_runs"
SCHEDULE_RUN_NOW = "schedule_run_now"
SCHEDULE_RUN_SKIP = "schedule_run_skip"

#: The channel a trigger arrives on, and the first half of what it is keyed by:
#: `schedule:daily_diary`.  The window draws a bubble from the channel alone
#: (`slife2.tui.widgets.channel_prefix`), so the name has to be *in* it for a
#: person to see which task spoke.
CHANNEL = "schedule"

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
    return ("fire" if late <= _T.timeouts.pacing.miss_grace else "missed", at(found))


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
    skip was designed (§1.7), where this one is a damaged ledger somebody has to
    go and look at (DESIGN.md §10.2).
    """
    found: list[ScheduleStore] = []
    for path in schedule_dbs():
        try:
            store = ScheduleStore.adopt(path)
        except Exception:  # noqa: BLE001 - one unopenable file is not the clock's
            logger.error(
                "%s could not be opened, so nothing in it will be fired until "
                "an operator fixes it",
                path,
                exc_info=True,
            )
            continue
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
) -> Run:
    """Hand one due task to its agent, and open the run it will be answered in.

    **What is handed over is a trigger, not the work.**  The turn it opens has one
    instruction — call `schedule_run_now` with this due time — and the starting of
    the work happens there, so that the run cannot be opened by a process and
    started by nothing (the module docstring has the argument for splitting it
    that way rather than dispatching here).

    **The words say whose task it is, and they are the only place that can.**
    `store.agent` is also the address this is handed to, so for a hand-over that
    went the way it was aimed at the name is the reader's own — but a hand-over's
    address is a fact about the *delivery*, and the model reading the trigger sees
    only the words.  So a trigger that reaches a conversation whose ledger holds
    no such task would otherwise be indistinguishable from one that was mis-aimed;
    naming the owner makes it a claim the reader can check, and the answer it
    replaces is "not mine, and I cannot say whose".

    **The record comes first.**  A run is written as `pending` before anything is
    handed over, which is what makes an interrupted fire *visible*: the anchor has
    moved, so the poll will not try it again, and a `pending` row is a thing the
    next start can find.  A row written after the hand-over would simply not exist
    in the case that matters.

    **A run that is already settled is not re-fired**, which is what stops a poll
    that ran twice from handing the same work over twice.

    **A trigger that could not be handed over ends the run `failed`**, because
    the turn is the whole of the run: there is no other place the work can
    happen, and leaving the row `pending` would say "this may still run" about a
    fire that has already passed.  The reason is recorded with it — a fact about
    this machine rather than about the task, and the one thing a person looking
    at the ledger needs to know.
    """
    run = store.record(task, due_at)
    if run.status != PENDING:
        return run
    try:
        await hand_over(
            config,
            store.agent,
            render(
                TRIGGER_TEMPLATE,
                name=task.name,
                description=task.description,
                due_at=due_at,
                agent=store.agent,
            ),
            channel_for(task.name),
            what=f"scheduled task {task.name}",
            window_client=window_client,
        )
    except DeliveryFailed as exc:
        # The one answer that proves nothing was written anywhere, and the only
        # other outcome of a hand-over that may leave the run open (`None`) is
        # spent inside `hand_over` as a failure too — a run whose trigger did not
        # reach its conversation has not happened.
        settled = store.settle(task.name, due_at, FAILED, error=exc.reason)
        assert settled is not None
        logger.warning(
            "scheduled run of %s for %s failed: %s", task.name, store.agent, exc
        )
        return settled
    return run


async def tasks_server(
    config: Config, client: Client | None = None
) -> tuple[Client, bool]:
    """The agent-task plugin, and whether this call is the one that opened it.

    `slife2-agent-task` is the plugin that holds loops, and this one hands a run
    to it rather than running anything itself (§1.7).  Opened per call and closed
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
    memory (DESIGN.md §1.7).

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
            # unaddressed answer (`slife2.audience.UNADDRESSED`).
            meta=client_meta(store.agent),
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
    attached = store.attach(task.name, due_at, started)
    assert attached is not None  # the run was read a moment ago
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

    `failed` and `cancelled` are both `failed`, because the ledger's question is
    whether the work happened and neither of those is it; the account of *why* is
    in the conversation, where the task's answer was delivered.

    **A task the peer no longer holds is `failed` too**, with the reason saying
    nothing was seen of its ending.  That is the honest answer rather than a
    `pending` row nobody can resolve: a task that has ended is kept for a while
    and then forgotten (`slife2.agent_task_server.RETIRED`), and one that is
    missing while this process is the one that started it did not go anywhere
    else.  A restart is the common case, and the sweep usually gets there first.
    """
    did: list[str] = []
    for store in ledgers():
        try:
            waiting = store.in_flight()
        except Exception:  # noqa: BLE001 - one bad ledger must not stop the rest
            logger.exception("%s: this ledger was skipped for this pass", store.path)
            continue
        if not waiting:
            continue
        try:
            listed = await _tasks_of(config, store.agent, tasks_client)
        except Exception as exc:  # noqa: BLE001 - a peer that is not there
            # Nothing is settled: this says nothing about the runs, only about
            # this process's view of them, and the next poll asks again.
            logger.warning(
                "%s: the runs in flight were not checked: %s", store.agent, exc
            )
            continue
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
            meta=client_meta(agent),
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
) -> list[str]:
    """One pass of the clock over every agent's ledger.

    **Two jobs, and they are the two ends of one run's life**: what has come due
    is fired, and what is in flight is asked after.  A poll that only did the
    first would leave every run `pending` for as long as nothing else closed it —
    which is what a prompt telling a *model* to close it amounts to (DESIGN.md
    §1.7).

    Returns what it did, as lines — for the log, and because "it did nothing" is
    the ordinary answer and a caller should be able to see that rather than infer
    it from silence.  Split out of the loop that calls it so that a test can ask
    for exactly one pass, which is the only way to have a clock in a test that is
    not a sleep.
    """
    did: list[str] = []
    for store in ledgers():
        # **One bad ledger is one ledger skipped, not a pass abandoned.**  A row
        # with a hand-edited, non-ISO anchor makes `due` raise out of `moment_of`;
        # without this guard that raise would take the whole pass with it, and
        # the next pass would hit the same row — so every *other* agent's tasks
        # would quietly stop firing while the clock looked healthy.  The ledger is
        # named and skipped; the rest of this pass carries on.
        try:
            for task in store.tasks(enabled_only=True):
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
                )
                did.append(f"{store.agent}: {task.name} due {due_at} -> {run.status}")
        except Exception:  # noqa: BLE001 - one bad ledger must not stop the rest
            logger.exception("%s: this ledger was skipped for this pass", store.path)
            continue
    # After the fires rather than before them, so that a run started by this pass
    # is asked about on the next one: a task that has just been started is
    # `running`, and asking immediately would only add a call per fire.
    did.extend(await reconcile(config, tasks_client=tasks_client))
    return did


async def sweep_once() -> list[str]:
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
        # hand-over, both of which either happened or did not.
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

    def store_for(agent: str) -> ScheduleStore:
        return ScheduleStore.open(agent)

    agent_of = agent_reader("agent whose schedule it could be", "a schedule")

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
                    live,
                    window_client=window_client,
                    tasks_client=tasks_client,
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
        # A limit that is not a positive number is "unset", the way zero already
        # is: `or` folds zero into the default, and a *negative* must not reach
        # SQLite, where `LIMIT -1` means **no limit at all** — so a model passing
        # `-1` would walk past the ceiling this tool exists to keep.  The default
        # is itself under the ceiling, so either branch is bounded.
        bounded = min(limit, RUNS_MAX) if limit > 0 else RUNS_LIMIT
        rows = [run.row() for run in store.runs(name.strip(), wanted, bounded)]
        return {"runs": rows}

    @mcp.tool(name=SCHEDULE_RUN_NOW, meta=FOR_THE_MODEL)
    async def schedule_run_now(
        ctx: Context, name: str, due_at: str = ""
    ) -> dict[str, Any]:
        """Start a scheduled run's work now — which is what a trigger asks for.

        **This is what to call when a task has come due**, with the `due_at` the
        trigger carried: it starts the work as a background task of yours and
        returns at once. Do not wait for it and do not follow it up — the answer
        arrives on its own, and **the run closes itself** when that task ends, so
        nothing here has to be remembered later.

        `due_at` names the run — the moment it was due, as the trigger carried
        it. Any spelling of that moment will do; a value that is not a moment is
        refused rather than answered with "no such run".

        With no `due_at` it is a new run due at this moment, for work somebody
        wants done early. With the `due_at` of a run that was **missed** or that
        failed to start, it re-opens *that* run — the same run, the same due time
        — which is how a gap is answered.

        The answer is the run's row, with the background task's id on it. A
        `pending` row means the work is in flight; `failed` means it could not be
        started, and the error says why.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        task = task_of(agent, name.strip())
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
        # A run already in flight is left alone rather than started twice: the
        # row carries the task doing it, and two calls for one due time is a
        # conversation retrying rather than a second run.
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
            await dispatch(config, store, task, stamp, tasks_client=tasks_client)
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

        **A task that has come due is not yours to decline** — the trigger tells
        you to run it. This is for the runs that *did not happen*: one that could
        not be started, and one whose moment passed while slife2 was not running,
        where doing it late is the other answer and a `pending` row is the one
        ending that accounts for nothing.
        """
        agent = agent_of(ctx)
        store = store_for(agent)
        task = task_of(agent, name.strip())
        stamp = due_at.strip()
        stamp = _key_of(store, task.name, stamp) if stamp else _newest_open(store, task)
        settled = store.settle(task.name, stamp, SKIPPED, note=note.strip())
        assert settled is not None  # the run was read a moment ago
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
    gives.  `slife2.timeutil` exists to stop exactly that, and its own rule is
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
