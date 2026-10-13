"""The clock, the ledger, and the seven tools over them.

Everything here runs over FastMCP's **in-memory transport** on the isolated data
directory `conftest` gives every test, and the plugin is built with
`polling=False`: the 30-second loop is not started, and the test asks for exactly
one pass with a `reference` moment it chose.  A test that waited on a real clock
would be a test that is either slow or flaky, and the decisions here are all
decidable from a moment — which is the reason `due`, `poll_once` and `sweep_once`
are callable at all.

**What is *not* faked is the ledger.** The store, the SQLite file, the anchor, the
`UNIQUE(task_id, due_at)` and the sweep are the real ones, because the properties
under test are exactly those: a fire cannot repeat, a settled run cannot be
reopened, and a run whose fate is unknown is not reported as either outcome. A
fake store would test the fake.

The window and the agent server are recorders rather than the real plugins —
what is under test is *what this plugin hands over*: the channel, the rendered
template, and which of the two paths it took.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.schedule_server as schedule_server
import slife2.timeouts as _T
from slife2 import paths
from slife2.agent_task_server import (
    FAILED as TASK_FAILED,
)
from slife2.agent_task_server import (
    FINISHED as TASK_FINISHED,
)
from slife2.agent_task_server import (
    RUNNING as TASK_RUNNING,
)
from slife2.audience import client_meta, for_the_model, request_client
from slife2.config import ServerSettings, default_config
from slife2.schedule import latest_fire
from slife2.schedule_server import (
    CHANNEL,
    SCHEDULE_LIST,
    SCHEDULE_REMOVE,
    SCHEDULE_RUN_NOW,
    SCHEDULE_RUN_SKIP,
    SCHEDULE_RUNS,
    SCHEDULE_SET,
    build_server,
    due,
    poll_forever,
    poll_once,
    reconcile,
    sweep_once,
)
from slife2.schedule_store import (
    FAILED,
    MISSED,
    PENDING,
    RAN,
    RUNS_LIMIT,
    SKIPPED,
    ScheduleStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

AGENT = "jack"
OTHER = "jill"
DESCRIPTION = "Write the day's entry."
EVERY_MINUTE = "* * * * *"
#: A cron whose period is longer than the grace window, which is what makes a
#: *missed* fire constructible at all: `latest_fire` answers with the newest fire
#: behind the moment asked about, so a task due every minute is always fresh and
#: only a sparse one can be late by more than the window.
ONCE_A_YEAR = "0 0 1 1 *"
#: What the recorder peer says a finished task finished at.  A fixed moment
#: because it is asserted on: `ran_at` comes off *this*, not off the clock, and a
#: test that read it from `now()` would pass whichever one the code used.
FINISHED_AT = "2026-10-12T22:31:07+08:00"


# ── the peers, as recorders ──────────────────────────────────────────────────


@dataclass
class Window:
    """A recorder standing in for `slife2-tui`, and what it was handed."""

    client: Client | None = None
    #: Refuse, the way a terminal that is not showing that conversation does —
    #: which is the one failure that *proves* nothing was written, and so the
    #: one the fallback is allowed to act on.
    refuse: bool = False
    #: Raise something that is not a refusal: the words may or may not have
    #: landed, which is the whole of what makes this different from `refuse`.
    broken: bool = False
    handed: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mcp: FastMCP = FastMCP("window")

        @self.mcp.tool
        async def submit(agent: str, text: str, channel: str) -> None:
            if self.broken:
                raise ConnectionError("the stream dropped mid-call")
            if self.refuse:
                raise ToolError(f"no window is showing {agent}'s conversation")
            self.handed.append({"agent": agent, "text": text, "channel": channel})


@dataclass
class Conversation:
    """A recorder standing in for the agent server's `send_message`.

    The fallback's own peer, and it is reached only when the window refused —
    which is the case a background task exists for: start it, close the window,
    walk away.
    """

    client: Client | None = None
    refuse: bool = False
    handed: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mcp: FastMCP = FastMCP("agent")

        @self.mcp.tool
        async def send_message(agent: str, prompt: str, channel: str) -> None:
            if self.refuse:
                raise ToolError("this conversation cannot be reached")
            self.handed.append({"agent": agent, "prompt": prompt, "channel": channel})


@dataclass
class Tasks:
    """A recorder standing in for `slife2-agent-task`.

    **What is under test is what this plugin hands that peer and what it makes of
    the answer**, so the recorder is the peer's whole surface: `agent_task_start`,
    which answers with an id and runs nothing, and `agent_task_list`, whose
    statuses a test sets by hand.  One `status` field rather than a transcript,
    because the question asked of this peer is one question — what became of the
    task I started.

    It records the identity each call arrived under, which is the half of the
    contract a fake can silently get wrong: a task is an *agent's*, and a shared
    process that forgot to say whose would be refused by the real one.
    """

    client: Client | None = None
    refuse: bool = False
    status: str = TASK_RUNNING
    started: list[dict[str, Any]] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mcp: FastMCP = FastMCP("tasks")

        def who(ctx: Context) -> str:
            found = request_client(ctx)
            return found[0] if found else ""

        @self.mcp.tool
        async def agent_task_start(
            ctx: Context, name: str, description: str
        ) -> dict[str, Any]:
            if self.refuse:
                raise ToolError(f"an agent task named {name!r} is already running")
            self.started.append(
                {"agent": who(ctx), "name": name, "description": description}
            )
            return {
                "task_id": f"agent-task:{name}",
                "name": name,
                "status": TASK_RUNNING,
                "started_at": "",
                "finished_at": "",
            }

        @self.mcp.tool
        async def agent_task_list(ctx: Context) -> dict[str, Any]:
            self.asked.append(who(ctx))
            return {
                "tasks": [
                    {
                        "task_id": f"agent-task:{row['name']}",
                        "name": row["name"],
                        "status": self.status,
                        "started_at": "",
                        "finished_at": (
                            FINISHED_AT if self.status == TASK_FINISHED else ""
                        ),
                    }
                    for row in self.started
                ]
            }


@pytest_asyncio.fixture(loop_scope="function")
async def tasks() -> AsyncIterator[Tasks]:
    node = Tasks()
    async with Client(node.mcp) as client:
        node.client = client
        yield node


@pytest_asyncio.fixture(loop_scope="function")
async def window() -> AsyncIterator[Window]:
    node = Window()
    async with Client(node.mcp) as client:
        node.client = client
        yield node


@pytest_asyncio.fixture(loop_scope="function")
async def dead_window() -> AsyncIterator[Window]:
    node = Window(refuse=True)
    async with Client(node.mcp) as client:
        node.client = client
        yield node


@pytest_asyncio.fixture(loop_scope="function")
async def broken_window() -> AsyncIterator[Window]:
    node = Window(broken=True)
    async with Client(node.mcp) as client:
        node.client = client
        yield node


@pytest_asyncio.fixture(loop_scope="function")
async def conversation() -> AsyncIterator[Conversation]:
    node = Conversation()
    async with Client(node.mcp) as client:
        node.client = client
        yield node


# ── helpers ──────────────────────────────────────────────────────────────────


def build(
    window: Window | None = None,
    conversation: Conversation | None = None,
    tasks: Tasks | None = None,
) -> FastMCP:
    """The plugin, wired to recorders and with its clock not running."""
    return build_server(
        default_config(),
        window_client=window.client if window else None,
        agent_client=conversation.client if conversation else None,
        tasks_client=tasks.client if tasks else None,
        polling=False,
    )


async def ask(
    client: Client,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    agent: str = AGENT,
) -> dict[str, Any]:
    """One call, with the identity where every hop carries it (`_meta`)."""
    result = await client.call_tool(tool, arguments or {}, meta=client_meta(agent))
    return result.data


async def write(
    client: Client,
    name: str = "daily",
    *,
    cron: str = EVERY_MINUTE,
    description: str = DESCRIPTION,
    timezone: str = "",
    enabled: bool = True,
    agent: str = AGENT,
) -> dict[str, Any]:
    return await ask(
        client,
        SCHEDULE_SET,
        {
            "name": name,
            "description": description,
            "cron": cron,
            "timezone": timezone,
            "enabled": enabled,
        },
        agent=agent,
    )


def ledger(agent: str = AGENT) -> ScheduleStore:
    """The real ledger on this test's data directory."""
    return ScheduleStore.open(agent)


def later(seconds: float = 0, days: float = 0) -> datetime:
    """A moment this far past now, for the clock to be asked about.

    Relative rather than absolute because the fire has to sit a known distance
    from the *anchor*, which is stamped when the task is written — and the
    distance is what decides whether a fire is fresh or missed.
    """
    return datetime.now() + timedelta(seconds=seconds, days=days)


def free_port() -> int:
    """A port nothing is listening on, so a peer addressed there is refused.

    Bound and released rather than a magic number: the point of the test is that
    `open_server` fails to reach it, and a port some other test's server happens
    to hold would turn that into a hang.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def until(predicate: Any, *, timeout: float = 5.0) -> None:
    """Wait for a background loop to get somewhere, or say it did not."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the background loop did not get there in time")


# ── the surface ──────────────────────────────────────────────────────────────


async def test_the_six_tools_are_the_models_and_nothing_else_is() -> None:
    """Six, which is v1's six.  There is no `schedule_run_settle` because there is
    nothing for a model to close: a run ends when the background task doing it
    ends, and the plugin is the one that asks (§1.7)."""
    tools = await build().list_tools()
    assert sorted(t.name for t in tools) == [
        SCHEDULE_LIST,
        SCHEDULE_REMOVE,
        SCHEDULE_RUN_NOW,
        SCHEDULE_RUN_SKIP,
        SCHEDULE_RUNS,
        SCHEDULE_SET,
    ]
    # The gate (§8.1): a tool the model may call says so, and absence is the
    # answer "no" rather than "nobody has decided yet".
    assert all(for_the_model(t.meta) for t in tools)


async def test_a_call_that_does_not_say_whose_schedule_it_is_is_refused() -> None:
    """A shared process cannot guess the agent, so it refuses rather than
    answering with somebody else's tasks."""
    async with Client(build()) as client:
        with pytest.raises(ToolError, match="did not say which conversation"):
            await client.call_tool(SCHEDULE_LIST, {})


# ── writing a task ───────────────────────────────────────────────────────────


async def test_a_task_is_written_read_back_and_listed() -> None:
    """The fire is recomputed on the way out, and read in the reader's own zone.

    `next_run` is a fact about the expression and the clock rather than about
    anything this system wrote down, so it is computed when the task is read —
    which is what makes it right after an edit rather than stale.

    **And it goes out through `slife2.clock.at`**, like every stamp this system
    hands to a person: local time with an explicit offset.  So the offset here is
    *this machine's*, and asserting `+08:00` was asserting the runner's clock —
    it passed on a machine set to it and failed on the other three, which is the
    failure `tests/test_timeutil.py` writes down for the same reason.  What every
    timezone shares is the fire itself, which is also the half worth holding:
    `timezone: Asia/Shanghai` is the claim that `0 9 * * *` fires at nine in the
    morning *there*, and that is what this asserts — without naming an offset the
    machine is not obliged to have.
    """
    async with Client(build()) as client:
        written = await write(client, cron="0 9 * * *", timezone="Asia/Shanghai")
        assert written["cron"] == "0 9 * * *"
        assert written["enabled"] is True

        fire = datetime.fromisoformat(written["next_run"])
        assert fire.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M") == "09:00"
        # The other half of the same rule, and the half that says *which* zone
        # the string came back in: the offset it carries is this machine's for
        # that instant, compared against the machine rather than named.
        assert fire.utcoffset() == fire.astimezone().utcoffset()

        listed = await ask(client, SCHEDULE_LIST)
        assert [row["name"] for row in listed["tasks"]] == ["daily"]


async def test_writing_a_task_whole_replaces_what_it_does_not_mention() -> None:
    """An upsert rather than a patch: the arguments *are* the task, so there is
    no way to reach one that is half of two descriptions."""
    async with Client(build()) as client:
        await write(client, timezone="Asia/Shanghai")
        again = await write(client, description="Something else entirely.")
        assert again["description"] == "Something else entirely."
        # And a field left out is cleared rather than kept.
        assert again["timezone"] == ""


async def test_switching_a_task_off_and_on_again_does_not_report_a_gap() -> None:
    """A task that was switched off missed nothing — it was off — so the anchor
    moves to now when it comes back, instead of the whole disabled stretch
    arriving as one enormous absence."""
    async with Client(build()) as client:
        await write(client)
        await write(client, enabled=False)
        assert ledger().tasks()[0].last_due
        before = ledger().task("daily")
        assert before is not None and not before.enabled

        await write(client, enabled=True)
        after = ledger().task("daily")
        assert after is not None and after.enabled
        # The anchor is fresh: nothing between the two writes can be read as a
        # fire that was missed.
        assert after.last_due >= before.updated_at


async def test_a_cron_that_can_never_fire_is_refused() -> None:
    """`0 9 31 2 *` parses and never fires, and a tool that accepted it would
    have stored a task whose only symptom is silence."""
    async with Client(build()) as client:
        with pytest.raises(ToolError, match="can fire"):
            await write(client, cron="0 9 31 2 *")


async def test_an_unknown_timezone_is_refused_at_the_write() -> None:
    """Asked up front rather than at the first fire, which would be three in the
    morning with nobody watching."""
    async with Client(build()) as client:
        with pytest.raises(ToolError, match="unknown timezone"):
            await write(client, timezone="Mars/Olympus")


async def test_a_name_that_is_not_a_word_is_refused() -> None:
    async with Client(build()) as client:
        with pytest.raises(ToolError, match="cannot name a scheduled task"):
            await write(client, name="../etc/passwd")


async def test_a_task_needs_a_description_that_stands_alone() -> None:
    """The description is the whole of what the task is handed when it comes
    due, and nobody can answer a question it raises."""
    async with Client(build()) as client:
        with pytest.raises(ToolError, match="needs a description"):
            await write(client, description="   ")


# ── the clock ────────────────────────────────────────────────────────────────


async def test_a_task_that_has_come_due_is_handed_to_the_conversation(
    window: Window,
) -> None:
    """A fire is a turn of the agent's own, on the task's own channel, wearing
    the rendered template — and the run it opens is `pending` with no task behind
    it yet, because *starting the work* is what the turn is for."""
    async with Client(build(window)) as client:
        await write(client, cron=EVERY_MINUTE)

    did = await poll_once(
        default_config(), window_client=window.client, reference=later(90)
    )

    assert len(window.handed) == 1
    handed = window.handed[0]
    assert handed["agent"] == AGENT
    # The name is in the channel because a hand-over carries the channel and the
    # words and nothing else, and the window draws the bubble from the channel.
    assert handed["channel"] == f"{CHANNEL}:daily"
    assert "▸ Scheduled task: daily" in handed["text"]
    assert DESCRIPTION in handed["text"]

    run = ledger().newest("daily")
    assert run is not None and run.status == PENDING
    # **Open, with nothing doing it yet**: the turn has not been answered, so
    # `schedule_run_now` has not been called.
    assert run.agent_task_id == ""
    # The trigger's one instruction is that call, carrying the due time that names
    # the run — and it says the run closes itself, so there is nothing to
    # remember in a later turn.
    assert run.due_at in handed["text"]
    assert SCHEDULE_RUN_NOW in handed["text"]
    assert did == [f"{AGENT}: daily due {run.due_at} -> {PENDING}"]


async def test_a_fire_older_than_the_grace_window_is_missed_rather_than_run(
    window: Window,
) -> None:
    """Slife2 was not running when this came due, and yesterday's work at
    today's prices is not what the person asked for."""
    async with Client(build(window)) as client:
        await write(client, cron=ONCE_A_YEAR)

    await poll_once(
        default_config(), window_client=window.client, reference=later(days=400)
    )

    assert window.handed == []
    run = ledger().newest("daily")
    assert run is not None and run.status == MISSED
    assert "not running" in run.error


async def test_a_task_that_is_not_due_yet_does_nothing_twice(
    window: Window,
) -> None:
    """`None` is the answer on almost every poll, and the anchor is what keeps it
    that way: the same moment asked about again finds nothing, because the fire
    it found is already on record."""
    async with Client(build(window)) as client:
        await write(client, cron=EVERY_MINUTE)
        reference = later(90)

        first = await poll_once(
            default_config(), window_client=window.client, reference=reference
        )
        second = await poll_once(
            default_config(), window_client=window.client, reference=reference
        )

    assert len(first) == 1
    # Nothing new to do: the anchor moved, so the same fire is behind it.
    assert second == []
    assert len(window.handed) == 1


async def test_a_disabled_task_is_never_fired(window: Window) -> None:
    async with Client(build(window)) as client:
        await write(client, enabled=False)

    did = await poll_once(
        default_config(), window_client=window.client, reference=later(600)
    )

    assert did == []
    assert window.handed == []
    assert ledger().newest("daily") is None


async def test_a_hand_over_that_refuses_falls_back_to_the_conversation(
    dead_window: Window, conversation: Conversation
) -> None:
    """A window that is not showing that agent is not a lost result — the turn
    runs, is recorded, and is drawn when a window opens and restores."""
    async with Client(build(dead_window, conversation)) as client:
        await write(client)

    await poll_once(
        default_config(),
        window_client=dead_window.client,
        agent_client=conversation.client,
        reference=later(90),
    )

    assert len(conversation.handed) == 1
    assert conversation.handed[0]["channel"] == f"{CHANNEL}:daily"
    run = ledger().newest("daily")
    assert run is not None and run.status == PENDING


async def test_a_run_nothing_could_be_reached_for_is_failed(
    dead_window: Window,
) -> None:
    """`False` is the one answer that proves nothing was written anywhere, so the
    run is finished rather than left open — and it says why."""
    conversation = Conversation(refuse=True)
    async with Client(conversation.mcp) as agent_client:
        conversation.client = agent_client
        async with Client(build(dead_window, conversation)) as client:
            await write(client)
        await poll_once(
            default_config(),
            window_client=dead_window.client,
            agent_client=agent_client,
            reference=later(90),
        )

    run = ledger().newest("daily")
    assert run is not None and run.status == FAILED
    assert "no conversation could be reached" in run.error


async def test_a_hand_over_that_does_not_come_back_leaves_the_run_pending(
    broken_window: Window, conversation: Conversation
) -> None:
    """The third answer. The words may already be in the conversation, so the
    fallback is *not* tried — one message arriving twice is worse than one
    arriving late — and the run is not called failed either, because nothing
    about it is known."""
    async with Client(build(broken_window, conversation)) as client:
        await write(client)

    await poll_once(
        default_config(),
        window_client=broken_window.client,
        agent_client=conversation.client,
        reference=later(90),
    )

    assert conversation.handed == []
    run = ledger().newest("daily")
    assert run is not None and run.status == PENDING


async def test_the_clock_is_one_loop_that_sees_every_agents_ledger(
    window: Window,
) -> None:
    """One process serves every agent, so the poll walks the ledgers — and whose
    each one is comes from the row inside it rather than from the directory
    name, which is the sanitised one."""
    async with Client(build(window)) as client:
        await write(client, name="mine", agent=AGENT)
        await write(client, name="theirs", agent=OTHER)

    did = await poll_once(
        default_config(), window_client=window.client, reference=later(90)
    )

    assert sorted(entry["agent"] for entry in window.handed) == [AGENT, OTHER]
    assert {line.split(":")[0] for line in did} == {AGENT, OTHER}
    assert ledger(OTHER).newest("theirs") is not None


async def test_a_ledger_that_does_not_say_whose_it_is_is_skipped(
    window: Window, caplog: pytest.LogCaptureFixture
) -> None:
    """The `meta` row is what makes a fire safe. The directory holds the
    *sanitised* agent name, so a trigger sent under it would go to a conversation
    that is not the one that asked — and it would look like it worked."""
    with caplog.at_level(logging.WARNING):
        # The schema, and no owner: what a file left by something that is not
        # this build looks like.
        ScheduleStore(paths.schedule_db(AGENT))
        did = await poll_once(
            default_config(), window_client=window.client, reference=later(600)
        )

    assert did == []
    assert window.handed == []
    assert "does not say whose ledger" in caplog.text


# ── starting a run's work ────────────────────────────────────────────────────


async def test_run_now_starts_the_work_as_a_background_task_of_the_agents(
    tasks: Tasks,
) -> None:
    """**The whole of what a trigger asks for.** The plugin makes the call, so it
    is handed the id — and that is why a run can close itself later instead of a
    model having to remember anything (§1.7)."""
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        run = await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})

    assert run["status"] == PENDING
    assert run["agent_task_id"] == "agent-task:daily"
    # Handed over as the agent, under the task's own name, with the description
    # as the whole of what the work is told.
    assert tasks.started == [
        {"agent": AGENT, "name": "daily", "description": DESCRIPTION}
    ]


async def test_a_run_already_in_flight_is_not_started_twice(tasks: Tasks) -> None:
    """A second call for the same due time is a conversation retrying, not a
    second run."""
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        first = await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
        again = await ask(
            client, SCHEDULE_RUN_NOW, {"name": "daily", "due_at": first["due_at"]}
        )

    assert again["agent_task_id"] == first["agent_task_id"]
    assert len(tasks.started) == 1


async def test_a_dispatch_that_is_refused_closes_the_run_and_says_why(
    tasks: Tasks,
) -> None:
    """**A `pending` row with no task behind it is the one state nothing can
    resolve** — the poll cannot ask about a task that was never started, and the
    sweep would have to guess."""
    tasks.refuse = True
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        with pytest.raises(ToolError, match="could not be started"):
            await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})

    run = ledger().newest("daily")
    assert run is not None and run.status == FAILED
    assert "already running" in run.error


async def test_the_work_ending_closes_the_run_at_the_moment_it_ended(
    tasks: Tasks,
) -> None:
    """`ran_at` comes off the task's own `finished_at` rather than off the clock:
    the work ended when it ended, not when a poll happened to look."""
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
    tasks.status = TASK_FINISHED

    did = await reconcile(default_config(), tasks_client=tasks.client)

    run = ledger().newest("daily")
    assert run is not None and run.status == RAN
    assert run.ran_at == FINISHED_AT
    assert did == [f"{AGENT}: daily due {run.due_at} closed"]
    # And the answer to "did it run" is in the ledger for anybody who asks.
    async with Client(build(tasks=tasks)) as client:
        listed = await ask(client, SCHEDULE_RUNS, {"name": "daily", "status": RAN})
    assert len(listed["runs"]) == 1


async def test_a_run_still_running_is_left_where_it_is(tasks: Tasks) -> None:
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})

    assert await reconcile(default_config(), tasks_client=tasks.client) == []
    run = ledger().newest("daily")
    assert run is not None and run.status == PENDING
    # Whose tasks were asked about: a task is an agent's, and the identity is how
    # the peer knows which ones.
    assert tasks.asked == [AGENT]


async def test_a_background_task_that_failed_closes_the_run(tasks: Tasks) -> None:
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
    tasks.status = TASK_FAILED

    await reconcile(default_config(), tasks_client=tasks.client)

    run = ledger().newest("daily")
    assert run is not None and run.status == FAILED
    assert TASK_FAILED in run.error


async def test_a_task_the_peer_no_longer_holds_closes_the_run(tasks: Tasks) -> None:
    """A task its own plugin has forgotten is not a run to leave open: a
    `pending` row nothing can resolve is the state this design exists to avoid."""
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
    tasks.started.clear()  # the peer's registry no longer holds it

    await reconcile(default_config(), tasks_client=tasks.client)

    run = ledger().newest("daily")
    assert run is not None and run.status == FAILED
    assert "never seen" in run.error


async def test_the_poll_asks_after_the_task_has_ended(tasks: Tasks) -> None:
    """Both ends of a run's life in one pass: the clock's fires, then the asking
    about what is in flight.

    **Asserted on the run `run_now` started, not on the newest one**, and that is
    a flake rather than a preference.  The task here fires `EVERY_MINUTE` and this
    is the one poll in the file that asks with the *real* clock, so a pass that
    straddles a minute boundary opens a second run behind the one under test:
    `newest` then answers with a row nothing has reported on yet and the
    assertion fails, about once in a few hundred runs, for a reason that has
    nothing to do with what is being tested.  Naming the run makes the extra one
    harmless — which is also the rule the module docstring gives: ask a question
    about the moment you chose.
    """
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        started = await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
    tasks.status = TASK_FINISHED

    did = await poll_once(default_config(), tasks_client=tasks.client)

    assert any(line.endswith("closed") for line in did)
    run = ledger().run("daily", started["due_at"])
    assert run is not None and run.status == RAN


async def test_run_now_backfills_the_run_that_was_missed(
    tasks: Tasks, window: Window
) -> None:
    """The same run — same identity, same due time — and now it has work behind
    it."""
    async with Client(build(tasks=tasks, window=window)) as client:
        await write(client, cron=ONCE_A_YEAR)
    await poll_once(
        default_config(), window_client=window.client, reference=later(days=400)
    )
    missed = ledger().newest("daily")
    assert missed is not None and missed.status == MISSED

    async with Client(build(tasks=tasks)) as client:
        started = await ask(
            client, SCHEDULE_RUN_NOW, {"name": "daily", "due_at": missed.due_at}
        )

    assert started["due_at"] == missed.due_at
    assert started["status"] == PENDING
    assert started["agent_task_id"] == "agent-task:daily"


async def test_run_now_refuses_a_run_that_has_already_ended(tasks: Tasks) -> None:
    """`ran` and `skipped` are answers, and a tool that quietly turned one back
    into work would make the ledger's history a function of the last thing
    anybody said."""
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        run = await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
    tasks.status = TASK_FINISHED
    await reconcile(default_config(), tasks_client=tasks.client)

    async with Client(build(tasks=tasks)) as client:
        with pytest.raises(ToolError, match="already ran"):
            await ask(
                client, SCHEDULE_RUN_NOW, {"name": "daily", "due_at": run["due_at"]}
            )


async def test_skipping_a_run_is_an_answer_rather_than_a_gap(tasks: Tasks) -> None:
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        run = await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
        skipped = await ask(
            client,
            SCHEDULE_RUN_SKIP,
            {"name": "daily", "due_at": run["due_at"], "note": "the day is over"},
        )

    assert skipped["status"] == SKIPPED
    assert skipped["note"] == "the day is over"
    # A run that never happened has no moment it happened at.
    assert skipped["ran_at"] == ""


async def test_skipping_with_no_due_at_closes_the_newest_open_run(
    tasks: Tasks,
) -> None:
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        run = await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
        skipped = await ask(client, SCHEDULE_RUN_SKIP, {"name": "daily"})

    assert skipped["due_at"] == run["due_at"]
    assert skipped["status"] == SKIPPED


async def test_skipping_with_nothing_open_is_refused() -> None:
    """Rather than inventing a timestamp, which would close a run nobody asked
    about."""
    async with Client(build()) as client:
        await write(client)
        with pytest.raises(ToolError, match="no run waiting to be closed"):
            await ask(client, SCHEDULE_RUN_SKIP, {"name": "daily"})


async def test_removing_a_task_takes_its_runs_with_it(tasks: Tasks) -> None:
    """The one delete, and it is the model saying "this is not a thing any
    more" — keeping the runs of a task that no longer exists would be keeping
    rows nothing can name."""
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        run = await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
        await ask(client, SCHEDULE_REMOVE, {"name": "daily"})
        listed = await ask(client, SCHEDULE_LIST)
        runs = await ask(client, SCHEDULE_RUNS, {})

    assert listed["tasks"] == []
    assert runs["runs"] == []
    assert ledger().run("daily", run["due_at"]) is None


# ── coming up again ──────────────────────────────────────────────────────────


async def test_the_sweep_fails_what_a_stopped_process_left_pending(
    window: Window,
) -> None:
    """A `pending` row is a trigger that was written and a process that died
    before anything closed it — so the answer is not "unknown", it is that this
    never finished."""
    async with Client(build(window)) as client:
        await write(client)
    # What the process left behind: a run written, and nothing after it.
    store = ledger()
    task = store.task("daily")
    assert task is not None
    store.record(task, "2026-10-12T09:00:00+08:00")

    did = await sweep_once()

    run = ledger().run("daily", "2026-10-12T09:00:00+08:00")
    assert run is not None and run.status == FAILED
    assert "stopped before" in run.error
    assert any("never settled" in line for line in did)


async def test_the_sweep_announces_nothing_because_the_ledger_is_where_you_look(
    window: Window,
) -> None:
    """**A fire is the only thing this plugin says to a conversation.** v1 printed
    unsettled runs into every turn's prompt; here that would be one plugin
    reaching into a prompt another plugin owns, and a note handed over on the
    trigger's channel is deferred rather than half-built (DESIGN.md §9). What the
    agent gets instead is a ledger it can read — which is what makes "did it run?"
    a question with an answer rather than a message nobody asked for."""
    async with Client(build(window)) as client:
        await write(client)
    store = ledger()
    task = store.task("daily")
    assert task is not None
    store.record(task, "2026-10-12T09:00:00+08:00")

    await sweep_once()

    assert window.handed == []
    # …and the run is findable, which is the whole of the answer it needs.
    async with Client(build()) as client:
        runs = await ask(client, SCHEDULE_RUNS, {"name": "daily", "status": FAILED})
    assert [row["due_at"] for row in runs["runs"]] == ["2026-10-12T09:00:00+08:00"]


async def test_a_sweep_with_nothing_to_do_is_quiet(window: Window) -> None:
    async with Client(build(window)) as client:
        await write(client)

    did = await sweep_once()

    assert did == []
    assert window.handed == []


# ── the decision, on its own ─────────────────────────────────────────────────


async def test_a_task_whose_anchor_is_ahead_of_the_clock_has_nothing_to_do() -> None:
    """The pure function, asked directly: a task written a moment ago and due
    once a day has no fire behind it at all."""
    async with Client(build()) as client:
        await write(client, cron="0 9 * * *")
    task = ledger().task("daily")
    assert task is not None

    assert due(task, later(1)) is None


# ── one bad ledger is not every agent's outage ───────────────────────────────


async def test_the_clock_survives_a_pass_that_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A raise from a pass must not stop the loop for every agent.**

    The guard used to wrap the whole `while True`, so an exception from
    `poll_once` — a stray or truncated ledger, a row with an anchor that will not
    parse — left the loop, was logged once, and the coroutine *returned*: the
    clock never ran again until the process restarted, and every agent's tasks
    went quiet.
    """
    real = schedule_server.poll_once
    calls = 0

    async def flaky(*args: Any, **kwargs: Any) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("a pass that cannot be read")
        return await real(*args, **kwargs)

    monkeypatch.setattr(schedule_server, "poll_once", flaky)
    monkeypatch.setattr(_T.timeouts.pacing, "schedule_poll", 0.01)

    clock = asyncio.create_task(poll_forever(lambda: default_config()))
    try:
        # The old code stopped after the first raise, so the second call is the
        # whole assertion: the clock asked again.
        await until(lambda: calls >= 2)
    finally:
        clock.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await clock

    assert calls >= 2


async def test_one_bad_ledger_row_does_not_stop_the_other_agents(
    window: Window,
) -> None:
    """A row with a hand-edited, non-ISO anchor makes `due` raise out of
    `moment_of`, and one ledger's damage must not take the whole pass with it —
    or every *other* agent's tasks would quietly stop firing."""
    async with Client(build(window)) as client:
        await write(client, name="theirs", agent=OTHER, cron=EVERY_MINUTE)
        await write(client, name="mine", agent=AGENT, cron=EVERY_MINUTE)

    # What a hand-edited ledger looks like: an anchor that is not a time.
    connection = sqlite3.connect(paths.schedule_db(AGENT))
    connection.execute("UPDATE scheduled_tasks SET last_due = 'not-a-time'")
    connection.commit()
    connection.close()

    did = await poll_once(
        default_config(), window_client=window.client, reference=later(90)
    )

    # The good ledger fired; the bad one was skipped rather than aborting the pass.
    assert [entry["agent"] for entry in window.handed] == [OTHER]
    assert any(OTHER in line for line in did)


async def test_a_ledger_that_cannot_be_opened_is_skipped_not_fatal() -> None:
    """A stray or truncated `schedule.db` raises out of `adopt` — SQLite will not
    read a file that is not a database — and one agent's damage must not stop the
    clock for every agent, so the walk skips it and serves the rest."""
    broken = paths.schedule_db("jack")
    broken.write_bytes(b"not a sqlite database")

    async with Client(build()) as client:
        await write(client, agent=OTHER)

    # The walk neither raises nor loses the good ledger to the bad one.
    assert [store.agent for store in schedule_server.ledgers()] == [OTHER]


# ── a dispatch whose peer will not answer ────────────────────────────────────


async def test_a_dispatch_whose_peer_cannot_be_reached_closes_the_run() -> None:
    """**Opening the agent-task peer is part of the dispatch.**

    `tasks_server` used to sit outside the `try`, so a port that is not listening
    escaped as a raw non-`ToolError` *and* left the run `pending` with an empty
    `agent_task_id` — the state `in_flight` skips and the sweep only reaches at
    startup.  The failure has to leave a run something can resolve.
    """
    config = default_config()
    config.servers["agent-task"] = ServerSettings(port=free_port())
    async with Client(build_server(config, polling=False)) as client:
        await write(client)
        with pytest.raises(ToolError, match="could not be started"):
            await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})

    run = ledger().newest("daily")
    assert run is not None and run.status == FAILED
    assert run.error, "and the run says why it could not be started"


# ── backfilling a run that failed after its task started ─────────────────────


async def test_run_now_backfills_a_run_that_failed_after_its_task_started(
    tasks: Tasks,
) -> None:
    """A run that failed *after* its background task started still names that
    task, and a backfill must not read the stale id as "already in flight".

    `reopen` used to leave `agent_task_id` in place, so the row came back
    `pending` **with** a task: `schedule_run_now`'s in-flight guard saw exactly
    that pair and answered without dispatching anything, so the backfill did
    nothing and the next reconcile settled it `failed` again.
    """
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        run = await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
    # The task started, then failed: the id stays on the failed row.
    tasks.status = TASK_FAILED
    await reconcile(default_config(), tasks_client=tasks.client)
    failed = ledger().newest("daily")
    assert failed is not None and failed.status == FAILED
    assert failed.agent_task_id, "the failed row still names the task that died"

    tasks.status = TASK_RUNNING
    tasks.started.clear()
    async with Client(build(tasks=tasks)) as client:
        started = await ask(
            client, SCHEDULE_RUN_NOW, {"name": "daily", "due_at": run["due_at"]}
        )

    assert started["status"] == PENDING
    assert started["agent_task_id"] == "agent-task:daily"
    assert len(tasks.started) == 1, "the backfill really started the work"


async def test_a_run_still_in_flight_is_not_reopened_into_a_second_task(
    tasks: Tasks,
) -> None:
    """The other half: a `pending` run *does* keep its id, so a second call for
    its due time is not a second task."""
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        first = await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
        again = await ask(
            client, SCHEDULE_RUN_NOW, {"name": "daily", "due_at": first["due_at"]}
        )

    assert again["agent_task_id"] == first["agent_task_id"]
    assert len(tasks.started) == 1


# ── an ending is not rewritten ───────────────────────────────────────────────


async def test_skipping_a_run_that_already_ran_does_not_rewrite_the_ledger(
    tasks: Tasks,
) -> None:
    """`ran` and `skipped` are the two endings, and the last tool call must not
    be able to rewrite history: a skip on a finished run used to clear its
    `ran_at` and file it as `skipped`."""
    async with Client(build(tasks=tasks)) as client:
        await write(client)
        run = await ask(client, SCHEDULE_RUN_NOW, {"name": "daily"})
    tasks.status = TASK_FINISHED
    await reconcile(default_config(), tasks_client=tasks.client)
    ran = ledger().newest("daily")
    assert ran is not None and ran.status == RAN and ran.ran_at

    async with Client(build(tasks=tasks)) as client:
        answered = await ask(
            client,
            SCHEDULE_RUN_SKIP,
            {"name": "daily", "due_at": run["due_at"], "note": "too late"},
        )

    # The tool is told nothing happened by getting the row back unchanged.
    assert answered["status"] == RAN
    assert answered["ran_at"] == ran.ran_at
    assert ledger().newest("daily").status == RAN


# ── the ledger read is bounded ───────────────────────────────────────────────


async def test_a_limit_that_is_not_positive_does_not_bypass_the_ceiling() -> None:
    """SQLite reads `LIMIT -1` as **no limit at all**, so a negative limit passed
    straight through made `schedule_runs(limit=-1)` answer with the whole ledger —
    past the 200-row ceiling the tool exists to keep."""
    async with Client(build()) as client:
        await write(client)
        store = ledger()
        task = store.task("daily")
        assert task is not None
        for minute in range(RUNS_LIMIT + 5):
            store.record(task, f"2026-10-12T09:{minute:02d}:00+08:00")
        listed = await ask(client, SCHEDULE_RUNS, {"name": "daily", "limit": -1})

    assert len(listed["runs"]) == RUNS_LIMIT, "the default bound, not the whole table"


# ── the fire, chosen by instant ──────────────────────────────────────────────


async def test_the_newest_fire_is_chosen_by_instant_not_by_clock_reading() -> None:
    """A fall-back hour names the same wall reading twice, an hour apart.

    `found` and the anchor are restated in the task's zone, so they carry the
    same `ZoneInfo` and Python compares their naive fields (PEP 495).  Inside the
    repeated hour that is the wrong question: the fire at `01:12Z` is an hour
    *newer* than an anchor at `00:20Z`, while its wall reading (`01:12`) is
    *older* than the anchor's (`01:20`) — so a wall-clock comparison drops it, and
    the fires inside the repeated hour are neither fired nor recorded as missed.
    """
    # Europe/London: the clocks go back on 2026-10-25, so 01:00-02:00 happens
    # twice — once at +01:00 and again an hour later at +00:00.
    anchor = datetime.fromisoformat("2026-10-25T01:20+01:00")  # 00:20Z
    until = datetime.fromisoformat("2026-10-25T01:12+00:00")  # 01:12Z

    found = latest_fire("* * * * *", anchor, until, "Europe/London")

    assert found is not None, "the newest fire is an hour newer by the clock"
    assert found.timestamp() > anchor.timestamp()
    assert found.timestamp() <= until.timestamp()
