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

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2 import paths
from slife2.audience import client_meta, for_the_model
from slife2.config import default_config
from slife2.schedule_server import (
    CHANNEL,
    SCHEDULE_LIST,
    SCHEDULE_REMOVE,
    SCHEDULE_RUN_NOW,
    SCHEDULE_RUN_SETTLE,
    SCHEDULE_RUN_SKIP,
    SCHEDULE_RUNS,
    SCHEDULE_SET,
    build_server,
    due,
    poll_once,
    sweep_once,
)
from slife2.schedule_store import (
    FAILED,
    MISSED,
    PENDING,
    RAN,
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
    window: Window | None = None, conversation: Conversation | None = None
) -> FastMCP:
    """The plugin, wired to recorders and with its clock not running."""
    return build_server(
        default_config(),
        window_client=window.client if window else None,
        agent_client=conversation.client if conversation else None,
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


# ── the surface ──────────────────────────────────────────────────────────────


async def test_the_seven_tools_are_the_models_and_nothing_else_is() -> None:
    """Seven, and each says so: `schedule_run_settle` is the one v1 did not have
    to expose, because there the cabinet reported its own success (§1.7)."""
    tools = await build().list_tools()
    assert sorted(t.name for t in tools) == [
        SCHEDULE_LIST,
        SCHEDULE_REMOVE,
        SCHEDULE_RUN_NOW,
        SCHEDULE_RUN_SETTLE,
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
    async with Client(build()) as client:
        written = await write(client, cron="0 9 * * *", timezone="Asia/Shanghai")
        assert written["cron"] == "0 9 * * *"
        assert written["enabled"] is True
        # Computed from the expression rather than stored, so it is right the
        # moment it is read.
        assert written["next_run"].endswith("09:00:00+08:00")

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
    the rendered template — and the run it opens is `pending`, because the turn
    is what closes it."""
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
    # The timestamp the trigger carried is the run's name, so the model can say
    # which run it is closing.
    assert run.due_at in handed["text"]
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


# ── closing a run ────────────────────────────────────────────────────────────


async def open_a_run(*, seconds: float = 90) -> str:
    """Fire one task, and answer with the due time of the run it opened."""
    window = Window()
    async with Client(window.mcp) as client:
        mcp = build(window)
        async with Client(mcp) as plugin:
            await write(plugin)
        await poll_once(
            default_config(), window_client=client, reference=later(seconds)
        )
    run = ledger().newest("daily")
    assert run is not None
    return run.due_at


async def test_settling_a_run_closes_it_and_keeps_the_note() -> None:
    """The note is what a later reading of the ledger has instead of the work."""
    due_at = await open_a_run()
    async with Client(build()) as client:
        settled = await ask(
            client,
            SCHEDULE_RUN_SETTLE,
            {"name": "daily", "due_at": due_at, "note": "wrote 400 words"},
        )
    assert settled["status"] == RAN
    assert settled["note"] == "wrote 400 words"
    assert settled["ran_at"]
    # And the answer to "did it run" is now in the ledger for anybody who asks.
    async with Client(build()) as client:
        runs = await ask(client, SCHEDULE_RUNS, {"name": "daily"})
    assert [row["status"] for row in runs["runs"]] == [RAN]


async def test_skipping_a_run_is_an_answer_rather_than_a_gap() -> None:
    due_at = await open_a_run(seconds=600)
    async with Client(build()) as client:
        skipped = await ask(
            client,
            SCHEDULE_RUN_SKIP,
            {"name": "daily", "due_at": due_at, "note": "the day is over"},
        )
    assert skipped["status"] == SKIPPED
    # A run that never happened has no moment it happened at.
    assert skipped["ran_at"] == ""


async def test_closing_with_no_due_at_closes_the_newest_open_run() -> None:
    """The run you were just handed: the trigger carried its timestamp, and a
    model that read it does not have to send it back."""
    due_at = await open_a_run()
    async with Client(build()) as client:
        settled = await ask(client, SCHEDULE_RUN_SETTLE, {"name": "daily"})
    assert settled["due_at"] == due_at
    assert settled["status"] == RAN


async def test_closing_with_nothing_open_is_refused() -> None:
    """Rather than inventing a timestamp, which would close a run nobody asked
    about."""
    async with Client(build()) as client:
        await write(client)
        with pytest.raises(ToolError, match="no run waiting to be closed"):
            await ask(client, SCHEDULE_RUN_SETTLE, {"name": "daily"})


async def test_running_now_backfills_the_run_that_was_missed(window: Window) -> None:
    """The same run, re-opened: same identity, same due time, and now fired."""
    async with Client(build(window)) as client:
        await write(client, cron=ONCE_A_YEAR)
    await poll_once(
        default_config(), window_client=window.client, reference=later(days=400)
    )
    missed = ledger().newest("daily")
    assert missed is not None and missed.status == MISSED

    async with Client(build(window)) as client:
        fired = await ask(
            client, SCHEDULE_RUN_NOW, {"name": "daily", "due_at": missed.due_at}
        )

    assert fired["due_at"] == missed.due_at
    assert fired["status"] == PENDING
    assert len(window.handed) == 1


async def test_running_now_refuses_a_run_that_has_already_ended() -> None:
    """`ran` and `skipped` are answers, and a tool that quietly turned one back
    into work would make the ledger's history a function of the last thing
    anybody said."""
    due_at = await open_a_run()
    async with Client(build()) as client:
        await ask(client, SCHEDULE_RUN_SETTLE, {"name": "daily", "due_at": due_at})
        with pytest.raises(ToolError, match="already ran"):
            await ask(client, SCHEDULE_RUN_NOW, {"name": "daily", "due_at": due_at})


async def test_removing_a_task_takes_its_runs_with_it() -> None:
    """The one delete, and it is the model saying "this is not a thing any
    more" — keeping the runs of a task that no longer exists would be keeping
    rows nothing can name."""
    due_at = await open_a_run()
    async with Client(build()) as client:
        await ask(client, SCHEDULE_REMOVE, {"name": "daily"})
        listed = await ask(client, SCHEDULE_LIST)
        runs = await ask(client, SCHEDULE_RUNS, {})

    assert listed["tasks"] == []
    assert runs["runs"] == []
    assert ledger().run("daily", due_at) is None


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

    did = await sweep_once(default_config())

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

    await sweep_once(default_config())

    assert window.handed == []
    # …and the run is findable, which is the whole of the answer it needs.
    async with Client(build()) as client:
        runs = await ask(client, SCHEDULE_RUNS, {"name": "daily", "status": FAILED})
    assert [row["due_at"] for row in runs["runs"]] == ["2026-10-12T09:00:00+08:00"]


async def test_a_sweep_with_nothing_to_do_is_quiet(window: Window) -> None:
    async with Client(build(window)) as client:
        await write(client)

    did = await sweep_once(default_config())

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
