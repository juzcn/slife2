"""The agent task: one loop of the agent's own, run on a description, answered
by a message.

Everything here runs over FastMCP's **in-memory transport**.  A task is a turn —
a model call, a tool list, a restore, a rebuild and a hand-over — so the peers
that would make those calls are handed in as clients: the real toolhub, the real
context store on the deterministic embedder, a scripted backend, and a window
that records what it is handed.  Those are `build_server`'s seams, and they are
the agent server's seams because a task is the same turn.

**The window is a recorder rather than the terminal's own plugin**, and that is
a deliberate split: what is under test here is *what this plugin sends* — the
channel, the marker, the words — while "the terminal accepts a `submit` at all"
is `tests/test_tui_control.py`'s subject.  The two places the real one is used
are the fallback, whose whole trigger is that plugin's own refusal, and nothing
else.

The properties under test throughout: a task is the agent's own work — its
identity, its context, its tools — it answers by a message rather than by a
return value, and it is one turn with exactly one rebuild in it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fakes import FakeBackend, StubEmbedder, plugin_transports, text_turn
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.libs.host.config import AgentTaskSettings, Config, default_config
from slife2.libs.plugin.audience import client_meta, for_the_model
from slife2.plugins.agent_task.server import (
    AGENT_TASK_CANCEL,
    AGENT_TASK_LIST,
    AGENT_TASK_START,
    AGENT_TASK_STATUS,
    CHANNEL,
    MARKER_PREFIX,
    build_server,
)
from slife2.plugins.tui import server as control

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

AGENT = "jack"
DESCRIPTION = "Find the retry policy."


def config(**task_overrides: Any) -> Config:
    base = default_config()
    return replace(base, agent_task=AgentTaskSettings(**task_overrides))


@dataclass
class Discriminator:
    """The context plugin's one model call, scripted — and counted.

    `{}` is DESIGN.md §2.4's common answer: what is in hand is the answer, so nothing is
    fetched and nothing is written.  Counting the calls is how "one turn, one
    rebuild" is asserted rather than assumed, and the two are the same fact.
    """

    answer: str = "{}"
    calls: list[str] = field(default_factory=list)

    async def __call__(
        self, agent: str, subagent: str, model: str, messages: Any, prompt: str
    ) -> str:
        self.calls.append(prompt)
        return self.answer


@dataclass
class Store:
    """The real `slife2-context`, over the in-memory transport, and its call."""

    client: Client
    ask: Discriminator


class UnreadableStore:
    """A context plugin answering with the shapes this task server refuses.

    `adopt` reads the store's answer as a thing it must be able to trust, and a
    store this build started never returns one it cannot read — so a test of the
    refusal hands in the shape a *daemon from another build* does, by name.
    """

    def __init__(self, **answers: dict[str, Any]) -> None:
        self.answers = answers

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return SimpleNamespace(data=self.answers[name])


@dataclass
class Window:
    """A recorder standing in for `slife2-tui`, and what it was handed.

    One tool with the same name and the same three arguments, because what the
    plugin sends *is* the thing being asserted: a `submit` is a name, words, and
    the channel they arrived on.  `client` is the *connected* client the
    `window` fixture opens — a `Client` built and not entered is a call that
    fails with "not connected", which is how a test passes for the wrong reason.
    """

    client: Client | None = None
    refuse: bool = False
    #: Raise something that is *not* a refusal — a call that did not come back.
    #: The words may or may not have landed, and that uncertainty is the whole
    #: of what makes this different from `refuse`: the terminal looks its session
    #: up before posting anything, so a refusal proves nothing was written.
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
class Built:
    """A task server, and the two things a test needs to look at it with."""

    mcp: FastMCP
    backend: FakeBackend
    window: Window | None = None

    def messages(self) -> list[Any]:
        """The message list of the **last** model call this server made."""
        return self.backend.calls[-1][0]


def build(
    *turns: Any,
    hub: Client,
    context: Store,
    window: Window | None = None,
    real_window: Client | None = None,
    **task_overrides: Any,
) -> Built:
    """A task server wired entirely to in-memory peers.

    `real_window` is for the one test that needs `slife2-tui` itself rather than
    a recorder; everything else is handed a `Window`.
    """
    backend = FakeBackend(*turns) if turns else FakeBackend()
    mcp = build_server(
        config(**task_overrides),
        backend=backend,
        context_client=context.client,
        hub_client=hub,
        window_client=real_window or (window.client if window else None),
        source=lambda: config(**task_overrides),
    )
    return Built(mcp=mcp, backend=backend, window=window)


@pytest.fixture
def a_quick_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Walk `hand_over`'s refusal ladder without waiting out its real pace.

    A refused hand-over is tried three times, `pacing.reconnect` apart — two
    seconds by design, because the window it is aimed at may be restarting — so
    a test that *ends* in a refusal spends four seconds on a fact it is not
    asserting: the run is over, or the answer was not delivered.  The pace is a
    registry value and patching one is what `slife2.helpers.timeouts` exists for;
    the ladder's length is the design and is left alone.
    """
    monkeypatch.setattr(_T.timeouts.pacing, "reconnect", 0.01)


@pytest_asyncio.fixture(loop_scope="function")
async def window() -> AsyncIterator[Window]:
    """A recorder window, connected — a `Client` nobody entered is not a peer."""
    node = Window()
    async with Client(node.mcp) as client:
        node.client = client
        yield node


@pytest_asyncio.fixture(loop_scope="function")
async def broken_window() -> AsyncIterator[Window]:
    """A window whose hand-over does not come back, the way a dropped stream
    does — and so says nothing about whether the words were written."""
    node = Window(broken=True)
    async with Client(node.mcp) as client:
        node.client = client
        yield node


@pytest_asyncio.fixture(loop_scope="function")
async def dead_window() -> AsyncIterator[Window]:
    """A window that refuses every hand-over, the way one that is not showing
    the conversation does — so the fallback is what gets exercised."""
    node = Window(refuse=True)
    async with Client(node.mcp) as client:
        node.client = client
        yield node


@pytest_asyncio.fixture(loop_scope="function")
async def store() -> Any:
    """The real context store, and the discriminator it will ask."""
    from slife2.plugins.context.server import build_server as build_context

    ask = Discriminator()
    async with Client(
        build_context(default_config(), embedder=StubEmbedder(), ask=ask)
    ) as client:
        yield Store(client=client, ask=ask)


@pytest_asyncio.fixture(loop_scope="function")
async def hub() -> Any:
    """The real toolhub, with every plugin it asks standing in memory."""
    from slife2.plugins.toolhub.server import build_server as build_hub

    base = default_config()
    async with Client(
        build_hub(base, transports=plugin_transports(base), embedder=StubEmbedder())
    ) as client:
        yield client


async def ask(
    client: Client,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    """One call, named the way every server in this system is named.

    The identity rides in `_meta` rather than in an argument
    (`slife2.libs.plugin.audience`), because a process that serves every agent cannot guess
    whose task a call is for — which is `UNADDRESSED`'s whole subject.  `home` is
    the other half of that identity, and it is what a test about two homes says:
    the same agent name in two directories is two agents.
    """
    result = await client.call_tool(
        tool,
        arguments or {},
        meta=client_meta(AGENT, home=str(home) if home else ""),
    )
    return result.data


async def start(built: Built, name: str = "research") -> dict[str, Any]:
    async with Client(built.mcp) as client:
        return await ask(
            client, AGENT_TASK_START, {"name": name, "description": DESCRIPTION}
        )


async def wait_for_handover(node: Window, count: int = 1) -> None:
    """Wait for a hand-over to land.

    **Generous, and asserted rather than bounded-and-ignored** — the rule
    `tests/test_server.py` states for its own wait.  A delivery happens *after*
    the task stops, because it is a message and a message is not part of the
    loop, so a test that read the status and asserted at once would be racing the
    very thing it is testing.  That race is not theoretical: it appeared the
    moment `retire` moved ahead of the hand-over, and it showed up as a test
    failing for a reason that had nothing to do with what it asserts.
    """
    deadline = time.monotonic() + 30.0  # timeouts: a test's patience
    while time.monotonic() < deadline:
        if len(node.handed) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{count} hand-over(s) never arrived ({len(node.handed)} did)")


async def wait_for_log(caplog: pytest.LogCaptureFixture, needle: str) -> None:
    """Wait for a line the delivery writes about itself, which is how a test
    waits for a delivery that has *nothing* to show for itself."""
    deadline = time.monotonic() + 30.0  # timeouts: a test's patience
    while time.monotonic() < deadline:
        if needle in caplog.text:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{needle!r} was never logged")


async def settle(built: Built, name: str = "research") -> dict[str, Any]:
    """Wait for a task to stop, by asking the plugin — never by sleeping long.

    The poll is the test's, which is the one place polling is right: the model is
    told not to do it (that is what the tool's description is for), and a test
    has no other way to wait for a background loop.

    **The bound is a wall clock, not a count of polls.**  A count makes the
    budget depend on how fast each poll is answered, and a loaded CI runner
    spent 400 of them before a turn of two one-second sleeps had finished.  A
    deadline asks the question the test means: has the task stopped by now.
    """
    deadline = time.monotonic() + 30.0  # timeouts: a test's patience
    async with Client(built.mcp) as client:
        while time.monotonic() < deadline:
            answer = await ask(client, AGENT_TASK_STATUS, {"name": name})
            if answer["status"] != "running":
                return answer
            await asyncio.sleep(0.01)
    raise AssertionError(f"task {name} never stopped")


# ── the surface ──────────────────────────────────────────────────────────────


async def test_the_four_tools_are_the_models_and_nothing_else_is(
    hub: Client, store: Store
) -> None:
    tools = await build(hub=hub, context=store).mcp.list_tools()
    assert sorted(t.name for t in tools) == [
        AGENT_TASK_CANCEL,
        AGENT_TASK_LIST,
        AGENT_TASK_START,
        AGENT_TASK_STATUS,
    ]
    # The gate (DESIGN.md §4.1): a tool the model may call says so, and absence is the
    # answer "no" rather than "nobody has decided yet".
    assert all(for_the_model(t.meta) for t in tools)


async def test_a_call_that_does_not_say_whose_task_it_is_is_refused(
    hub: Client, store: Store
) -> None:
    """A shared process cannot guess the agent, so it refuses rather than
    answering with somebody else's tasks."""
    async with Client(build(hub=hub, context=store).mcp) as client:
        # No `slife2/client` in `_meta`: not a call on anybody's behalf.
        with pytest.raises(ToolError, match="did not say which conversation"):
            await client.call_tool(AGENT_TASK_START, {"name": "x", "description": "y"})


async def test_a_task_needs_a_description_that_stands_alone(
    hub: Client, store: Store
) -> None:
    """The description is the task's whole context, and the refusal says so —
    the model's chance to fix it is the same turn."""
    async with Client(build(hub=hub, context=store).mcp) as client:
        with pytest.raises(ToolError, match="needs a description"):
            await ask(client, AGENT_TASK_START, {"name": "x", "description": "   "})


# ── one task, one turn ───────────────────────────────────────────────────────


async def test_the_task_runs_as_the_agent_on_the_rendered_template(
    hub: Client, store: Store
) -> None:
    """The agent's prompt, the hub's tools, the template as the user message.

    Three claims in one turn because they are one fact: a task is the agent
    working.  The user message is the *rendered* template rather than the
    description verbatim, which is what tells the loop that nobody is listening
    and that what it sends when it stops is the whole of what comes back.
    """
    built = build(text_turn("three tries"), hub=hub, context=store)
    await start(built)
    await settle(built)

    messages = built.messages()
    assert messages[0].role == "system"
    assert AGENT in str(messages[0].content)
    users = [message for message in messages if message.role == "user"]
    assert len(users) == 1
    opening = users[0].content
    assert isinstance(opening, str)
    assert opening.startswith("▸ Agent task: research")
    assert DESCRIPTION in opening
    assert "nobody can answer a question" in opening

    # **The hub's own list, unfiltered** — compared against what the hub
    # advertises rather than against two names, because the claim is that there
    # is no per-caller tool set here: a task gets what is loaded, exactly as its
    # agent does, which is what makes starting a task from inside one possible
    # rather than forbidden.
    from slife2.clients.toolclient import remote_tools

    assert {spec.name for spec in built.backend.calls[0][1]} == {
        tool.spec.name for tool in await remote_tools(hub)
    }


async def test_the_context_is_rebuilt_exactly_once(hub: Client, store: Store) -> None:
    """One turn, so one rebuild — and *not* zero, which is the thing that would
    be wrong: a context that was never chosen is the one turn in this system
    whose context nobody decided (DESIGN.md §2.4)."""
    built = build(text_turn("done"), hub=hub, context=store)
    await start(built)
    await settle(built)

    assert len(store.ask.calls) == 1
    # And it was asked about the *user message*, passed beside the list rather
    # than inside it, because a rebuild replaces the list wholesale.
    assert store.ask.calls[0].startswith("▸ Agent task: research")


async def test_the_context_it_runs_on_is_the_agents_own(
    hub: Client, store: Store
) -> None:
    """**The key is the agent's**, so a task starts from what this conversation
    has been about rather than from nothing.

    Which is the whole difference between delegating and asking a stranger: the
    description is what the caller chose to write down, and the turns in the log
    are everything else the agent already knows.  A turn is written into the
    store under the *agent's* key — as the agent server writes one — and the task
    is asserted to have run on it.
    """
    async with Client(store.client.transport) as writer:  # the same store
        await writer.call_tool(
            "remember",
            {
                "agent": AGENT,
                "messages": [
                    {
                        "role": "user",
                        "content": "we settled on three tries before giving up",
                    }
                ],
                "who_helped": AGENT,
                "channel": "tui",
                "created_at": "2026-10-12T09:00:00+08:00",
                "completed_at": "2026-10-12T09:00:01+08:00",
            },
            meta=client_meta(AGENT),
        )

    built = build(text_turn("done"), hub=hub, context=store)
    await start(built)
    await settle(built)

    assert any(
        "three tries before giving up" in str(message.content)
        for message in built.messages()
    )


# ── the answer comes back as a message ───────────────────────────────────────


async def test_a_store_answer_without_turn_ids_is_refused_rather_than_adopted(
    hub: Client, caplog: pytest.LogCaptureFixture
) -> None:
    """`turn_ids` was tolerated where `messages` beside it was validated.

    So an answer carrying messages and no ids was adopted as a context whose
    turns nothing could name, and the task's loop and the store disagreed about
    what it held from that point on — the identical deformation the agent
    server's `_adopt` was fixed for.  Both halves of one answer are read the same
    way: a missing or null `turn_ids` is refused, naming the payload
    (DESIGN.md §11.1: a value that cannot be read is not a value that is
    absent).  A task has no record to corrupt, so the refusal is the task's own
    failed answer rather than a store that quietly kept the context it had.
    """
    caplog.set_level(logging.WARNING, logger="slife2.plugins.agent_task.server")
    built = build(
        text_turn("done"),
        hub=hub,
        context=Store(
            client=UnreadableStore(restore={"messages": [], "turns": []}),
            ask=Discriminator(),
        ),
    )
    await start(built)
    answer = await settle(built)

    assert answer["status"] == "failed"
    assert "turn-id list" in caplog.text


async def test_the_answer_arrives_on_the_tasks_own_channel(
    hub: Client, store: Store, window: Window
) -> None:
    """A channel is the key a plugin runs under, and this one wears the task's
    name because the window draws the bubble from the channel and nothing else."""
    built = build(text_turn("three tries"), hub=hub, context=store, window=window)
    await start(built)
    await settle(built)
    await wait_for_handover(window)

    assert len(window.handed) == 1
    handed = window.handed[0]
    assert handed["agent"] == AGENT
    assert handed["channel"] == f"{CHANNEL}:research"
    assert handed["text"].endswith("three tries")


async def test_the_marker_says_which_task_it_was_and_when(
    hub: Client, store: Store, window: Window
) -> None:
    """Five fields, and the id is what the model's *next* call needs: asking
    about a task or stopping one names it, and this is where it learns the name
    — which is why the marker is content rather than an annotation."""
    built = build(text_turn("three tries"), hub=hub, context=store, window=window)
    await start(built)
    await settle(built)
    await wait_for_handover(window)

    text = window.handed[0]["text"]
    assert text.startswith(MARKER_PREFIX)
    payload = json.loads(text[len(MARKER_PREFIX) : text.index("]")])
    assert payload["task_id"] == f"{CHANNEL}:research"
    assert payload["name"] == "research"
    assert payload["status"] == "finished"
    assert payload["started_at"]
    assert payload["finished_at"]


async def test_a_task_that_fails_still_answers(
    hub: Client, store: Store, window: Window
) -> None:
    """SDD §5.1's rule one level down: a failure is a message, not a silence.  A task
    that vanished would leave the conversation waiting for something that is
    never coming."""
    built = build(hub=hub, context=store, window=window)
    await start(built)
    answer = await settle(built)
    await wait_for_handover(window)

    assert answer["status"] == "failed"
    assert "failed" in built.window.handed[0]["text"]  # type: ignore[union-attr]


async def test_asking_never_carries_the_answer(
    hub: Client, store: Store, window: Window
) -> None:
    """The poll answers *state* and the message carries the result, because a
    poll that could return it would put the same answer in the conversation
    twice and a model could act on it twice."""
    built = build(text_turn("three tries"), hub=hub, context=store, window=window)
    await start(built)
    answer = await settle(built)

    assert answer["status"] == "finished"
    assert "three tries" not in json.dumps(answer)
    async with Client(built.mcp) as client:
        listed = await ask(client, AGENT_TASK_LIST)
    assert "three tries" not in json.dumps(listed)
    assert listed["tasks"][0]["status"] == "finished"
    # The row carries the name and the state and a glance at the description, so a
    # name chosen a while ago still means something.
    assert listed["tasks"][0]["name"] == "research"
    assert DESCRIPTION in listed["tasks"][0]["description"]


# ── the cap, and the name ────────────────────────────────────────────────────


async def test_the_cap_is_read_off_the_config_of_the_call(
    hub: Client, store: Store
) -> None:
    """**The count is this home's, so the number it is compared against is too.**

    `running` matches on `(home, agent, name)` — one process serves every home —
    so a limit read off the *daemon's* boot config bounds a call by a number an
    operator wrote in a directory this caller has never heard of, and an edit to
    the file never reaches the call that enforces it.  Every other config read in
    this server is `live()`'s (`model_for`, `run_loop`), and the sibling agent
    server reads its own cap the same way.

    Two homes' configs in one assertion: the config this process started on says
    one task, and the config the call is in says two.  The second start is
    accepted, which is the live config's number and not the boot config's.
    """
    from slife2.plugins.agent_task.server import build_server as build_task

    backend = FakeBackend(
        text_turn(
            "never sent",
            delay=5.0,  # timeouts: the value this test is built around, not a budget
        ),
        text_turn(
            "never sent either",
            delay=5.0,  # timeouts: the value this test is built around, not a budget
        ),
    )
    mcp = build_task(
        # What the daemon started on...
        config(max_running=1),
        backend=backend,
        context_client=store.client,
        hub_client=hub,
        # ...and what this call's home says now.
        source=lambda: config(max_running=2),
    )
    async with Client(mcp) as client:
        first = await ask(
            client, AGENT_TASK_START, {"name": "one", "description": DESCRIPTION}
        )
        second = await ask(
            client, AGENT_TASK_START, {"name": "two", "description": DESCRIPTION}
        )
        for name in ("one", "two"):
            await ask(client, AGENT_TASK_CANCEL, {"name": name})

    assert first["status"] == "running"
    assert second["status"] == "running", "the boot config's number was enforced"


async def test_a_cancelled_task_holds_no_handle(
    hub: Client, store: Store, window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task that was called off holds nothing, like the two that stop cleanly.

    The three endings share one `finally` for the two things a task holds — the
    `asyncio.Task` and the answer beside it — and the cancelled path used to
    re-raise *before* it, from its own `except`, so a cancelled task kept its
    finished handle for as long as the registry remembered it: a finished
    `asyncio.Task` holds its result, and that result is the loop's frames.  What
    is released here is the same megabytes the comment is about, so it is
    asserted on the object rather than through a tool.
    """
    import slife2.plugins.agent_task.server as module

    made: list[Any] = []
    real_task = module.AgentTask

    def recording(**fields: Any) -> Any:
        """The task itself, kept so a test can look at what it holds."""
        task = real_task(**fields)
        made.append(task)
        return task

    monkeypatch.setattr(module, "AgentTask", recording)
    built = build(
        text_turn(
            "never sent",
            delay=5.0,  # timeouts: the value this test is built around, not a budget
        ),
        hub=hub,
        context=store,
        window=window,
    )
    await start(built)
    handle = made[0].handle
    async with Client(built.mcp) as client:
        stopped = await ask(client, AGENT_TASK_CANCEL, {"name": "research"})
    assert stopped["status"] == "cancelled"

    # The coroutine's own ending, awaited rather than slept away: the release is
    # in its `finally`, so the assertion below is about a build and not a race.
    assert handle is not None
    with contextlib.suppress(asyncio.CancelledError):
        await handle

    assert made[0].handle is None, "a cancelled task still holds its coroutine"
    assert made[0].text == ""


async def test_the_cap_refuses_the_task_past_it(hub: Client, store: Store) -> None:
    built = build(
        text_turn(
            "one",
            # **One chunk**, because `text_turn` splits its text a character at
            # a time and the delay is paid per chunk: three characters at a
            # second each is a turn of four seconds for a test that wants a turn
            # still running when the next call arrives.
            chunks=["one"],
            delay=1.0,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        hub=hub,
        context=store,
        max_running=1,  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget
    await start(built, "first")
    async with Client(built.mcp) as client:
        with pytest.raises(ToolError, match="already running"):
            await ask(client, AGENT_TASK_START, {"name": "second", "description": "y"})
    await settle(built, "first")


async def test_two_tasks_may_not_share_one_name(hub: Client, store: Store) -> None:
    """The name *is* the id, so a second one under it is a refusal rather than a
    second task nothing could address."""
    built = build(
        text_turn(
            "one",
            # **One chunk**, because `text_turn` splits its text a character at
            # a time and the delay is paid per chunk: three characters at a
            # second each is a turn of four seconds for a test that wants a turn
            # still running when the next call arrives.
            chunks=["one"],
            delay=1.0,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        hub=hub,
        context=store,  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget
    await start(built, "research")
    async with Client(built.mcp) as client:
        with pytest.raises(ToolError, match="already running"):
            await ask(
                client, AGENT_TASK_START, {"name": "research", "description": "again"}
            )
    await settle(built, "research")


# ── one process, several homes ────────────────────────────────────────────────


async def test_a_listing_names_only_the_callers_homes_tasks(
    hub: Client, store: Store, window: Window, isolated_runtime: Path
) -> None:
    """**The registry is keyed by `(home, agent, name)`, and the listing has to
    be too.**

    One process serves every home on the machine, so two homes may each be
    running an agent called `jack`.  A listing that matched on the agent name
    alone would hand one home's model the other home's task names, descriptions,
    states and times — rows it could then neither inspect nor stop, because
    `agent_task_status` and `agent_task_cancel` go through `find`, which does use
    the whole key and answers `unknown` for exactly those rows.  Naming tasks a
    model cannot act on is worse than naming none: the next thing it does is ask
    about one.
    """
    theirs = isolated_runtime.parent / "theirs"
    built = build(
        text_turn(
            "never sent",
            delay=5.0,  # timeouts: the value this test is built around, not a budget
        ),
        hub=hub,
        context=store,
        window=window,
    )
    async with Client(built.mcp) as client:
        started = await ask(
            client,
            AGENT_TASK_START,
            {"name": "research", "description": DESCRIPTION},
            home=theirs,
        )
        here = await ask(client, AGENT_TASK_LIST, home=isolated_runtime)
        there = await ask(client, AGENT_TASK_LIST, home=theirs)
        stopped = await ask(
            client, AGENT_TASK_CANCEL, {"name": "research"}, home=theirs
        )

    assert started["status"] == "running"
    assert here["tasks"] == [], "the other home's task is not this home's"
    assert [row["name"] for row in there["tasks"]] == ["research"]
    assert stopped["status"] == "cancelled", "and its own home can still stop it"


async def test_the_cap_counts_only_the_callers_homes_tasks(
    hub: Client, store: Store, window: Window, isolated_runtime: Path
) -> None:
    """`agent-task: max_running` bounds *this* home's agent, not its name.

    A cap counted by agent name alone would refuse a start in one home because of
    tasks running in another — a refusal with nothing behind it that neither the
    caller nor the operator could explain, since the tasks it counted are ones
    the caller cannot even ask about.
    """
    theirs = isolated_runtime.parent / "theirs"
    built = build(
        text_turn(
            "never sent",
            delay=5.0,  # timeouts: the value this test is built around, not a budget
        ),
        text_turn(
            "never sent either",
            delay=5.0,  # timeouts: the value this test is built around, not a budget
        ),
        hub=hub,
        context=store,
        window=window,
        max_running=1,  # timeouts: the value this test is built around, not a budget
    )
    async with Client(built.mcp) as client:
        await ask(
            client,
            AGENT_TASK_START,
            {"name": "theirs", "description": DESCRIPTION},
            home=theirs,
        )
        mine = await ask(
            client, AGENT_TASK_START, {"name": "mine", "description": DESCRIPTION}
        )
        await ask(client, AGENT_TASK_CANCEL, {"name": "theirs"}, home=theirs)
        await ask(client, AGENT_TASK_CANCEL, {"name": "mine"})

    assert mine["status"] == "running", "this home's cap was free"


# ── two tasks, one agent ─────────────────────────────────────────────────────


async def test_the_pre_turn_work_of_two_tasks_does_not_interleave(
    hub: Client, window: Window
) -> None:
    """**`restore` and `rebuild` are one step, and a snapshot is not a decision.**

    A rebuild's target is built from the list its *own* restore read, so a task
    that interleaves those two calls with another task's writes a list decided
    against a conversation that has moved — and the writer whose write lands
    last is the one whose snapshot is older.  What the store then holds is the
    older decision, and the next `restore` (an idle reap, a restart, a new
    terminal) rebuilds the conversation from it: a task's work dropped from the
    context with nothing anywhere saying so.

    The cap allows `max_running` tasks for one agent, so this is a state two
    calls can reach.  The agent server holds its `Loop.lock` across exactly these
    two calls; a task is the agent's own work, so this server holds its own lock
    the same way — one agent's pre-turn work at a time.

    Two turns are in the conversation, and the two tasks decide differently about
    them: the first keeps one of them, the second clears the context.  What the
    store holds afterwards is the *second* task's decision — and a build without
    the lock leaves the first task's, written from a snapshot taken before the
    second had even read one.
    """
    from slife2.libs.host.paths import agent_dir
    from slife2.libs.store.db import TurnStore
    from slife2.plugins.context.server import build_server as build_context

    asked: list[str] = []
    parked = asyncio.Event()
    second_in = asyncio.Event()
    gate = asyncio.Event()
    keep_one = ""

    async def discriminating(
        agent: str, subagent: str, model: str, messages: Any, prompt: str
    ) -> str:
        """The first task parks inside its rebuild; the second clears it."""
        asked.append(prompt)
        if len(asked) == 1:
            parked.set()
            await gate.wait()
            return keep_one
        second_in.set()
        return '{"context": "clear"}'

    async with Client(
        build_context(default_config(), embedder=StubEmbedder(), ask=discriminating)
    ) as context_client:
        store = Store(client=context_client, ask=Discriminator())
        async with Client(context_client.transport) as writer:
            ids = []
            for word in ("alpha", "beta"):
                stored = await writer.call_tool(
                    "remember",
                    {
                        "agent": AGENT,
                        "messages": [{"role": "user", "content": f"{word} is here"}],
                        "who_helped": AGENT,
                        "channel": "tui",
                        "created_at": "2026-10-12T09:00:00+08:00",
                        "completed_at": "2026-10-12T09:00:01+08:00",
                    },
                    meta=client_meta(AGENT),
                )
                ids.append(stored.data["turn_id"])
        # A keep-list for the first task's own decision: one of the two turns,
        # which is a *write* against the two that are in hand.
        keep_one = json.dumps({"context": [ids[0]]})

        built = build(
            text_turn("one answered"),
            text_turn("two answered"),
            hub=hub,
            context=store,
            window=window,
        )
        await start(built, "one")
        await asyncio.wait_for(parked.wait(), timeout=_T.timeouts.work.hub)
        await start(built, "two")
        # **The second task's snapshot is taken here, or not at all.**  Its
        # rebuild is in flight when the discriminator is asked, so its `restore`
        # has already read the list — and that read is what the first task's
        # write would have to land before to be an *interleaving* rather than a
        # sequence.  With the lock this never arrives, because the second task is
        # waiting on the first: a bounded wait, so the case the fix removes is a
        # fact instead of a hope, and the assertion below does not depend on it.
        for _ in range(100):
            if second_in.is_set():
                break
            await asyncio.sleep(0.01)
        gate.set()
        await wait_for_handover(window, 2)

    turns = TurnStore(agent_dir(AGENT) / "turns.db").context_turns()
    assert turns == [], "the conversation holds the decision that was made last"


# ── stopping one ─────────────────────────────────────────────────────────────


async def test_cancelling_a_running_task_takes_its_answer_with_it(
    hub: Client, store: Store, window: Window
) -> None:
    """A task that was called off has nothing to report, so nothing is handed
    over — DESIGN.md §2.5's rule for a cancelled turn, one level down."""
    built = build(
        text_turn(
            "never sent",
            delay=5.0,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        hub=hub,
        context=store,
        window=window,  # timeouts: the value this test is built around, not a budget
    )
    await start(built)
    async with Client(built.mcp) as client:
        stopped = await ask(client, AGENT_TASK_CANCEL, {"name": "research"})
    assert stopped["status"] == "cancelled"

    assert (await settle(built))["status"] == "cancelled"
    assert window.handed == []


async def test_a_late_cancel_cannot_take_a_result_away(
    hub: Client, store: Store, window: Window
) -> None:
    """`finished` rather than `cancelled`: the difference between *I stopped it*
    and *I arrived late*, which is why stopping a task needs no second
    vocabulary — it answers with the state, and the state is the lock."""
    built = build(text_turn("three tries"), hub=hub, context=store, window=window)
    await start(built)
    await settle(built)
    await wait_for_handover(window)

    async with Client(built.mcp) as client:
        answer = await ask(client, AGENT_TASK_CANCEL, {"name": "research"})
    assert answer["status"] == "finished"
    assert len(window.handed) == 1


async def test_a_name_nobody_knows_answers_unknown(hub: Client, store: Store) -> None:
    built = build(hub=hub, context=store)
    async with Client(built.mcp) as client:
        stopped = await ask(client, AGENT_TASK_CANCEL, {"name": "nothing"})
        asked = await ask(client, AGENT_TASK_STATUS, {"name": "nothing"})
    assert stopped["status"] == "unknown"
    assert asked["status"] == "unknown"


# ── the way back ─────────────────────────────────────────────────────────────


async def test_an_answer_with_no_window_is_undelivered_and_said(
    hub: Client,
    store: Store,
    caplog: pytest.LogCaptureFixture,
    a_quick_ladder: None,
) -> None:
    """The case a background task exists for: start it, close the window, walk
    away — and the answer reaches nobody.

    Built against **the real `slife2-tui`** with nothing attached, because what
    makes this fire is that plugin's own refusal rather than a stub's: no
    terminal is showing this agent, and there is no second road for the answer
    (`slife2.libs.harness.handover`).  The work happened; the delivery did not; the log says
    so, and nothing runs a turn in a conversation nobody is watching to make the
    two agree.
    """
    caplog.set_level(logging.ERROR, logger="slife2.plugins.agent_task.server")
    # Nothing has attached, so `submit` refuses with the terminal's own error
    # rather than a stand-in's — and the client is connected, so that refusal is
    # the refusal being tested rather than "not connected".
    async with Client(control.build_server()) as nobody:
        built = build(
            text_turn("three tries"),
            hub=hub,
            context=store,
            real_window=nobody,
        )
        await start(built)
        await settle(built)
        await wait_for_log(caplog, "was not delivered")

    assert "was not delivered" in caplog.text


async def test_a_hand_over_that_did_not_come_back_is_not_repeated(
    hub: Client,
    store: Store,
    broken_window: Window,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**The measured bug this rule comes from.**  A call that did not come back
    says nothing about whether it wrote: the words may be down the window's
    stream already, and sending them again is how one answer arrives *twice* —
    which the agent then acts on twice.  So the unknown case is logged and not
    repeated, and the task's answer is undelivered rather than doubled.
    """
    caplog.set_level(logging.ERROR, logger="slife2.plugins.agent_task.server")
    built = build(
        text_turn("three tries"),
        hub=hub,
        context=store,
        window=broken_window,
    )
    await start(built)
    await settle(built)
    await wait_for_log(caplog, "was not repeated")

    assert "was not repeated" in caplog.text


async def test_a_refusing_window_is_the_end_of_that_answer(
    hub: Client,
    store: Store,
    dead_window: Window,
    caplog: pytest.LogCaptureFixture,
    a_quick_ladder: None,
) -> None:
    """And the same for the other answer: a refusal proves nothing was written,
    which used to be the licence to run the answer through the agent server.

    There is no such road now — a fallback that runs the work where the window is
    not is exactly "carry on without the window" — so a refusal is a failed
    delivery like any other, and the task is over.
    """
    caplog.set_level(logging.ERROR, logger="slife2.plugins.agent_task.server")
    built = build(
        text_turn("three tries"),
        hub=hub,
        context=store,
        window=dead_window,
    )
    await start(built)
    await settle(built)
    await wait_for_log(caplog, "was not delivered")

    assert "was not delivered" in caplog.text


# ── what the registry remembers ──────────────────────────────────────────────


async def test_the_registry_forgets_from_the_tail(
    hub: Client, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded, because a model can start tasks for as long as somebody keeps
    asking it to — and forgetting is why `unknown` is an honest answer rather
    than a defect."""
    import slife2.plugins.agent_task.server as module

    monkeypatch.setattr(module, "RETIRED", 2)
    built = build(
        text_turn("a"), text_turn("b"), text_turn("c"), hub=hub, context=store
    )
    for name in ("one", "two", "three"):
        await start(built, name)
        await settle(built, name)

    async with Client(built.mcp) as client:
        listed = await ask(client, AGENT_TASK_LIST)
        gone = await ask(client, AGENT_TASK_STATUS, {"name": "one"})
    assert {row["name"] for row in listed["tasks"]} == {"two", "three"}
    assert gone["status"] == "unknown"


async def test_a_name_that_has_been_started_again_is_a_different_task(
    hub: Client, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry is keyed by `(agent, name)` and the *retired* list holds
    objects rather than keys, because a name that has been started again is a
    different task under the same key — and evicting by key would throw the new
    one away."""
    import slife2.plugins.agent_task.server as module

    monkeypatch.setattr(module, "RETIRED", 1)
    built = build(text_turn("first"), text_turn("second"), hub=hub, context=store)
    await start(built, "research")
    await settle(built, "research")
    await start(built, "research")
    await settle(built, "research")

    async with Client(built.mcp) as client:
        listed = await ask(client, AGENT_TASK_LIST)
    assert [row["name"] for row in listed["tasks"]] == ["research"]
    assert listed["tasks"][0]["status"] == "finished"


# ── the trim, and a status carried rather than read ─────────────────────────


async def test_a_trim_that_could_not_run_says_so_rather_than_going_quiet(
    hub: Client, store: Store, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    """The trim is not optional (DESIGN.md §6.4), and a task has no next turn for a dead
    hub to surface on.

    The agent server can leave its trim best-effort because its next request
    rebuilds the tool list from the hub and fails loudly if it is gone; a task
    runs one turn, so a skip with a warning and nothing else is a skip nobody
    ever reads.  What the task keeps is the outcome — bookkeeping does not fail a
    turn that already answered — and what it adds is that the skip is *said* at
    `error`, naming the task, because the loaded set is one list for the whole
    system.  Registered as a fallback in SDD Appendix C.
    """
    import slife2.plugins.agent_task.server as module

    async def no_hub(_client: Any) -> dict[str, Any]:
        raise ConnectionError("the hub is gone")

    monkeypatch.setattr(module, "unload_tools", no_hub)
    caplog.set_level(logging.ERROR, logger="slife2.plugins.agent_task.server")
    built = build(text_turn("three tries"), hub=hub, context=store)
    await start(built)
    await settle(built)

    assert "was not trimmed" in caplog.text


async def test_a_trim_answer_naming_no_names_is_refused_rather_than_read_as_none(
    hub: Client, store: Store, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    """An answer missing `unloaded` read as "nothing was unloaded".

    The same deformation the agent server's trim was fixed for, one plugin over:
    `found.get("unloaded") or []` took a hub answer this build cannot read — the
    shape a daemon from another build returns — as an honest no-op, so no pair
    was written and the record never said the list had shrunk.  Absent is not
    empty: both halves of the answer are read strictly, so a missing key is a
    failure and only a *readable* empty answer is a verdict (DESIGN.md §11.1: a
    failure is not a verdict).  The skip is still best-effort and said at
    `error`, naming the task, because the trim is post-turn bookkeeping.
    """
    import slife2.plugins.agent_task.server as module

    async def no_names(_client: Any) -> dict[str, Any]:
        # Every key but the one the trim acts on: the same silence an empty list
        # makes, from an answer that says nothing at all.
        return {"refused": [], "not_loaded": [], "text": "the list is fine"}

    monkeypatch.setattr(module, "unload_tools", no_names)
    caplog.set_level(logging.ERROR, logger="slife2.plugins.agent_task.server")
    built = build(text_turn("three tries"), hub=hub, context=store)
    await start(built)
    answer = await settle(built)

    assert answer["status"] == "finished", "bookkeeping did not fail the turn"
    assert "names it unloaded" in caplog.text


async def test_an_async_calls_status_is_carried_not_read_off_its_words() -> None:
    """`_runner` used to set `FAILED if result.startswith("Error") else FINISHED`.

    Which made a verdict out of a spelling, in both directions: a tool that
    failed with text not beginning `"Error"` was recorded `finished`, and an
    answer that happened to begin `"Error"` was recorded `failed` — and that
    status is the whole of what `agent_task_status`/`agent_task_list` answer
    from.  The coroutine now answers the pair a foreground call comes back as
    (`ToolRegistry.execute`), so `ok` decides and the words decide nothing
    (DESIGN.md §11.1, "a failure is not a verdict").

    Three cases, because the two the pair exists to tell apart are exactly the
    two the words cannot: a call that failed *without* saying "Error", a call
    that failed and did, and an answer that begins "Error" and is fine.
    """
    import slife2.libs.harness.async_tasks as async_tasks

    async def raises() -> tuple[str, bool]:
        raise RuntimeError("the provider is down")

    async def failed_quietly() -> tuple[str, bool]:
        return "the endpoint refused", False

    async def failed_loudly() -> tuple[str, bool]:
        return "Error: no such tool", False

    async def says_error() -> tuple[str, bool]:
        return "Error: no such thing, but here is what I found", True

    ids = {
        "raised": async_tasks.schedule(raises(), call_id="c1", tool="boom"),
        "quiet": async_tasks.schedule(failed_quietly(), call_id="c2", tool="lookup"),
        "loud": async_tasks.schedule(failed_loudly(), call_id="c3", tool="lookup"),
        "fine": async_tasks.schedule(says_error(), call_id="c4", tool="lookup"),
    }
    for task_id in ids.values():
        for _ in range(400):
            handle = async_tasks._tasks[task_id].handle  # noqa: SLF001
            if handle.done():
                break
            await asyncio.sleep(0.01)

    try:
        # No `deliver`, so nothing is handed back and the status is the whole
        # record — the one thing these four ids are asked about.
        status = {
            name: async_tasks._tasks[task_id].status  # noqa: SLF001
            for name, task_id in ids.items()
        }
        assert status == {
            "raised": async_tasks.FAILED,
            "quiet": async_tasks.FAILED,
            "loud": async_tasks.FAILED,
            "fine": async_tasks.FINISHED,
        }
    finally:
        for task_id in ids.values():
            async_tasks._tasks.pop(task_id, None)  # noqa: SLF001


async def test_a_hand_back_that_could_not_land_says_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`contextlib.suppress(Exception)` dropped the reason a hand-back failed.

    The task still recorded `delivered=False`, which is the fact — but nothing
    said *why*, so a delivery that could not land left a silence with no cause
    beside it.  The exception is now logged at `warning`, naming the task, and
    the suppress is kept: a hand-back that cannot land is a result `check_async`
    can still offer rather than an error with no reader (DESIGN.md §11.1, "a
    failure is not a verdict" — said out of band).
    """
    import slife2.libs.harness.async_tasks as async_tasks

    async def refuses(_message: str, _channel: str) -> bool:
        raise ConnectionError("no window is showing jack's conversation")

    async def answers() -> tuple[str, bool]:
        return "the answer", True

    caplog.set_level(logging.WARNING, logger="slife2.libs.harness.async_tasks")
    task_id = async_tasks.schedule(
        answers(), call_id="c1", tool="lookup", deliver=refuses
    )
    task = async_tasks._tasks[task_id]  # noqa: SLF001
    try:
        await task.handle
        assert task.delivered is False
        assert task.result == "the answer", (
            "an undelivered answer stays for check_async"
        )
        assert task_id in caplog.text
        assert "could not be handed back" in caplog.text
    finally:
        async_tasks._tasks.pop(task_id, None)  # noqa: SLF001
