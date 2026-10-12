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
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
import pytest_asyncio
from fakes import FakeBackend, StubEmbedder, plugin_transports, text_turn
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2.agent_task_server import (
    AGENT_TASK_CANCEL,
    AGENT_TASK_LIST,
    AGENT_TASK_START,
    AGENT_TASK_STATUS,
    CHANNEL,
    MARKER_PREFIX,
    build_server,
)
from slife2.audience import client_meta, for_the_model
from slife2.config import AgentTaskSettings, Config, default_config
from slife2.tui import control

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

AGENT = "jack"
DESCRIPTION = "Find the retry policy."


def config(**task_overrides: Any) -> Config:
    base = default_config()
    return replace(base, agent_task=AgentTaskSettings(**task_overrides))


@dataclass
class Discriminator:
    """The context plugin's one model call, scripted — and counted.

    `{}` is §5.1's common answer: what is in hand is the answer, so nothing is
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
            self.handed.append(
                {"agent": agent, "text": text, "channel": channel}
            )


@dataclass
class AgentServer:
    """A recorder standing in for `slife2-agent`'s `send_message`.

    `gone` is for the test that needs *both* paths to fail.  It is wired rather
    than left out on purpose: a config's `agent` address is a real port, and a
    test that fell back to it would be talking to whatever daemon happens to be
    running on the machine — which is the test passing for the worst reason.
    """

    client: Client | None = None
    gone: bool = False
    handed: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mcp: FastMCP = FastMCP("agent-server")

        @self.mcp.tool
        async def send_message(
            agent: str, prompt: str, subagent: str = "", channel: str = ""
        ) -> dict[str, Any]:
            if self.gone:
                raise ConnectionError("the agent server is gone")
            self.handed.append(
                {
                    "agent": agent,
                    "prompt": prompt,
                    "subagent": subagent,
                    "channel": channel,
                }
            )
            return {"text": "ok"}


@dataclass
class Built:
    """A task server, and the two things a test needs to look at it with."""

    mcp: FastMCP
    backend: FakeBackend
    window: Window | None = None
    agent: AgentServer | None = None

    def messages(self) -> list[Any]:
        """The message list of the **last** model call this server made."""
        return self.backend.calls[-1][0]


def build(
    *turns: Any,
    hub: Client,
    context: Store,
    window: Window | None = None,
    agent: AgentServer | None = None,
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
        agent_client=agent.client if agent else None,
        source=lambda: config(**task_overrides),
    )
    return Built(mcp=mcp, backend=backend, window=window, agent=agent)


@pytest_asyncio.fixture(loop_scope="function")
async def window() -> AsyncIterator[Window]:
    """A recorder window, connected — a `Client` nobody entered is not a peer."""
    node = Window()
    async with Client(node.mcp) as client:
        node.client = client
        yield node


@pytest_asyncio.fixture(loop_scope="function")
async def agent_server() -> AsyncIterator[AgentServer]:
    node = AgentServer()
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
    from slife2.context_server import build_server as build_context

    ask = Discriminator()
    async with Client(
        build_context(default_config(), embedder=StubEmbedder(), ask=ask)
    ) as client:
        yield Store(client=client, ask=ask)


@pytest_asyncio.fixture(loop_scope="function")
async def hub() -> Any:
    """The real toolhub, with every plugin it asks standing in memory."""
    from slife2.toolhub import build_server as build_hub

    base = default_config()
    async with Client(
        build_hub(base, transports=plugin_transports(base), embedder=StubEmbedder())
    ) as client:
        yield client


async def ask(
    client: Client, tool: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One call, named the way every server in this system is named.

    The identity rides in `_meta` rather than in an argument
    (`slife2.audience`), because a process that serves every agent cannot guess
    whose task a call is for — which is `UNADDRESSED`'s whole subject.
    """
    result = await client.call_tool(tool, arguments or {}, meta=client_meta(AGENT))
    return result.data


async def start(built: Built, name: str = "research") -> dict[str, Any]:
    async with Client(built.mcp) as client:
        return await ask(
            client, AGENT_TASK_START, {"name": name, "description": DESCRIPTION}
        )


async def wait_for_handover(node: Window | AgentServer, count: int = 1) -> None:
    """Wait for a hand-over to land.

    **Generous, and asserted rather than bounded-and-ignored** — the rule
    `tests/test_server.py` states for its own wait.  A delivery happens *after*
    the task stops, because it is a message and a message is not part of the
    loop, so a test that read the status and asserted at once would be racing the
    very thing it is testing.  That race is not theoretical: it appeared the
    moment `retire` moved ahead of the hand-over, and it showed up as a test
    failing for a reason that had nothing to do with what it asserts.
    """
    for _ in range(400):
        if len(node.handed) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{count} hand-over(s) never arrived ({len(node.handed)} did)")


async def wait_for_log(caplog: pytest.LogCaptureFixture, needle: str) -> None:
    """Wait for a line the delivery writes about itself, which is how a test
    waits for a delivery that has *nothing* to show for itself."""
    for _ in range(400):
        if needle in caplog.text:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{needle!r} was never logged")


async def settle(built: Built, name: str = "research") -> dict[str, Any]:
    """Wait for a task to stop, by asking the plugin — never by sleeping long.

    The poll is the test's, which is the one place polling is right: the model is
    told not to do it (that is what the tool's description is for), and a test
    has no other way to wait for a background loop.
    """
    async with Client(built.mcp) as client:
        for _ in range(400):
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
    # The gate (§8.1): a tool the model may call says so, and absence is the
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
            await client.call_tool(
                AGENT_TASK_START, {"name": "x", "description": "y"}
            )


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
    from slife2.toolclient import remote_tools

    assert {spec.name for spec in built.backend.calls[0][1]} == {
        tool.spec.name for tool in await remote_tools(hub)
    }


async def test_the_context_is_rebuilt_exactly_once(hub: Client, store: Store) -> None:
    """One turn, so one rebuild — and *not* zero, which is the thing that would
    be wrong: a context that was never chosen is the one turn in this system
    whose context nobody decided (§5.1)."""
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
    """§7's rule one level down: a failure is a message, not a silence.  A task
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


async def test_the_cap_refuses_the_task_past_it(hub: Client, store: Store) -> None:
    built = build(
        text_turn("one", delay=1.0), hub=hub, context=store, max_running=1
    )
    await start(built, "first")
    async with Client(built.mcp) as client:
        with pytest.raises(ToolError, match="already running"):
            await ask(client, AGENT_TASK_START, {"name": "second", "description": "y"})
    await settle(built, "first")


async def test_two_tasks_may_not_share_one_name(hub: Client, store: Store) -> None:
    """The name *is* the id, so a second one under it is a refusal rather than a
    second task nothing could address."""
    built = build(text_turn("one", delay=1.0), hub=hub, context=store)
    await start(built, "research")
    async with Client(built.mcp) as client:
        with pytest.raises(ToolError, match="already running"):
            await ask(client, AGENT_TASK_START, {"name": "research", "description": "again"})
    await settle(built, "research")


# ── stopping one ─────────────────────────────────────────────────────────────


async def test_cancelling_a_running_task_takes_its_answer_with_it(
    hub: Client, store: Store, window: Window
) -> None:
    """A task that was called off has nothing to report, so nothing is handed
    over — §3's rule for a cancelled turn, one level down."""
    built = build(
        text_turn("never sent", delay=5.0), hub=hub, context=store, window=window
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


async def test_a_name_nobody_knows_answers_unknown(
    hub: Client, store: Store
) -> None:
    built = build(hub=hub, context=store)
    async with Client(built.mcp) as client:
        stopped = await ask(client, AGENT_TASK_CANCEL, {"name": "nothing"})
        asked = await ask(client, AGENT_TASK_STATUS, {"name": "nothing"})
    assert stopped["status"] == "unknown"
    assert asked["status"] == "unknown"


# ── the way back ─────────────────────────────────────────────────────────────


async def test_with_no_window_showing_the_agent_server_is_given_the_answer(
    hub: Client, store: Store, agent_server: AgentServer
) -> None:
    """The case a background task exists for: start it, close the window, walk
    away.  Built against **the real `slife2-tui`** with nothing attached, because
    what makes this fire is that plugin's own refusal rather than a stub's."""
    # Nothing has attached, so `submit` refuses with the terminal's own error
    # rather than a stand-in's — and the client is connected, so that refusal is
    # the refusal being tested rather than "not connected".
    async with Client(control.build_server()) as nobody:
        built = build(
            text_turn("three tries"),
            hub=hub,
            context=store,
            real_window=nobody,
            agent=agent_server,
        )
        await start(built)
        await settle(built)
        await wait_for_handover(agent_server)

    assert len(agent_server.handed) == 1
    sent = agent_server.handed[0]
    assert sent["agent"] == AGENT
    # The agent's *own* conversation, and that is the point — `subagent` empty is
    # §3's "this agent's conversation", not a worker's.
    assert sent["subagent"] == ""
    assert sent["channel"] == f"{CHANNEL}:research"
    assert sent["prompt"].startswith(MARKER_PREFIX)
    assert sent["prompt"].endswith("three tries")


async def test_a_hand_over_that_did_not_come_back_is_not_repeated(
    hub: Client,
    store: Store,
    broken_window: Window,
    agent_server: AgentServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**The measured bug this rule comes from.**  A call that did not come back
    says nothing about whether it wrote: the words may be down the window's
    stream already, and giving them to the agent server as well is how one answer
    arrives *twice* — which the agent then acts on twice.  So the unknown case is
    logged and not repeated, and §1.2 states the same rule from the caller's
    side.

    `agent_server` is wired and must stay empty: that is the assertion, not the
    absence of a fallback.
    """
    with caplog.at_level("WARNING"):
        built = build(
            text_turn("three tries"),
            hub=hub,
            context=store,
            window=broken_window,
            agent=agent_server,
        )
        await start(built)
        await settle(built)
        await wait_for_log(caplog, "did not come back")

    assert agent_server.handed == []
    assert "did not come back" in caplog.text


async def test_a_refusal_is_the_one_failure_the_fallback_is_for(
    hub: Client, store: Store, dead_window: Window, agent_server: AgentServer
) -> None:
    """The other side of the same rule: a *refusal* proves nothing was written,
    so the answer is not lost — it goes to the agent server, which is the whole
    reason that fallback exists."""
    built = build(
        text_turn("three tries"),
        hub=hub,
        context=store,
        window=dead_window,
        agent=agent_server,
    )
    await start(built)
    await settle(built)
    await wait_for_handover(agent_server)

    assert len(agent_server.handed) == 1
    assert agent_server.handed[0]["prompt"].endswith("three tries")


async def test_an_answer_nobody_can_be_given_is_logged_not_raised(
    hub: Client,
    store: Store,
    dead_window: Window,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both paths gone is the one outcome worth saying loudly, and it is a *log*
    rather than a failure: the task is over either way, and raising here would
    replace the answer's own fate with a traceback nobody asked for.

    The agent server is wired and *gone* rather than left out, so the fallback
    fails where the test can see it — a config's `agent` address is a real port,
    and reaching it would mean talking to whatever daemon is running on this
    machine.
    """
    gone = AgentServer(gone=True)
    async with Client(gone.mcp) as client:
        gone.client = client
        with caplog.at_level("ERROR"):
            built = build(
                text_turn("lost"),
                hub=hub,
                context=store,
                window=dead_window,
                agent=gone,
            )
            await start(built)
            await settle(built)
            await wait_for_log(caplog, "could not be handed over")
    assert "could not be handed over" in caplog.text


# ── what the registry remembers ──────────────────────────────────────────────


async def test_the_registry_forgets_from_the_tail(
    hub: Client, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded, because a model can start tasks for as long as somebody keeps
    asking it to — and forgetting is why `unknown` is an honest answer rather
    than a defect."""
    import slife2.agent_task_server as module

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
    import slife2.agent_task_server as module

    monkeypatch.setattr(module, "RETIRED", 1)
    built = build(
        text_turn("first"), text_turn("second"), hub=hub, context=store
    )
    await start(built, "research")
    await settle(built, "research")
    await start(built, "research")
    await settle(built, "research")

    async with Client(built.mcp) as client:
        listed = await ask(client, AGENT_TASK_LIST)
    assert [row["name"] for row in listed["tasks"]] == ["research"]
    assert listed["tasks"][0]["status"] == "finished"
