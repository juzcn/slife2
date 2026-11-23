"""The agent task: one turn of the agent's own, run on a description, answered
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
is `tests/plugins/tui/test_tui_control.py`'s subject.  The two places the real
one is used are the fallback, whose whole trigger is that plugin's own refusal,
and nothing
else.

The properties under test throughout: a task is the agent's own work — its
identity, a clone of its context, its tools — it answers by a message rather
than by a return value, and it is one turn with exactly one rebuild in it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fakes import (
    FakeBackend,
    ScriptedTurn,
    StubEmbedder,
    a_home,
    plugin_transports,
    text_turn,
)
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.libs.harness.markers import strip as strip_marker
from slife2.libs.host import statefile
from slife2.libs.host.config import AgentTaskSettings, Config, default_config
from slife2.libs.host.paths import subagent_file
from slife2.libs.model.base import Chunk
from slife2.libs.plugin.audience import (
    FOR_THE_MODEL,
    conversation_meta,
    for_the_model,
)
from slife2.libs.plugin.vocabulary import NOTHING_WAITING
from slife2.libs.turn.context import TURN_PROMPT_PREFIX
from slife2.libs.turn.events import (
    MessageCutIn,
    TextDelta,
    TurnFinished,
)
from slife2.libs.turn.messages import StreamChatResult, ToolCall, Usage
from slife2.libs.turn.tools import (
    ATTACH_IMAGE,
    CHECK_NEW_INPUT,
    CONTEXT_REBUILD,
    TURN_COMPACT,
)
from slife2.libs.turn.watch import Closed, Snapshot, TaskBrief
from slife2.libs.turn.watch import decode as decode_watch
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

#: The live-id footnote the view appends to every addressable message.
LIVE_ID_RE = re.compile(r'\s*\[MESSAGE:\{"id": "[^"]+"\}\]')


def without_id(content: Any) -> str:
    """A view message's text with the live-id footnote taken off."""
    return LIVE_ID_RE.sub("", str(content or ""))


AGENT = "jack"
DESCRIPTION = "Find the retry policy."


def config(**task_overrides: Any) -> Config:
    base = default_config()
    compact_at = task_overrides.pop("compact_at", None)
    if compact_at is not None:
        base = replace(base, agent=replace(base.agent, compact_at=compact_at))
    return replace(base, agent_task=AgentTaskSettings(**task_overrides))


@dataclass
class DecisionModel:
    """The context plugin's one model call, scripted — and counted.

    `mode: keep` is SDD §5.5's common answer: what is in hand is the answer, so
    nothing is fetched and nothing is written.  Counting the calls is how "one
    turn, one rebuild" is asserted rather than assumed, and the two are the
    same fact.
    """

    answer: str = '{"mode": "keep"}'
    calls: list[Any] = field(default_factory=list)

    async def __call__(
        self,
        agent: str,
        subagent: str,
        model: str,
        messages: Any,
        *,
        usage: str = "",
    ) -> str:
        self.calls.append(messages)
        return self.answer


@dataclass
class Selector:
    """The context plugin's live selector, scripted — and counted.

    The answer drops everything it is offered, which is what makes a task's
    automatic pass visible; the existing tests never reach it, and a stub keeps
    the fixture off a real model server if one ever does.
    """

    calls: list[str] = field(default_factory=list)

    async def __call__(
        self,
        agent: str,
        subagent: str,
        model: str,
        messages: Any,
        selectable: list[str],
        *,
        usage: str = "",
        previous: str = "",
        repair: str = "",
    ) -> str:
        self.calls.append(usage)
        return json.dumps({"compact": selectable})


@dataclass
class Store:
    """The real `slife2-context`, over the in-memory transport, and its calls."""

    client: Client
    ask: DecisionModel
    select: Selector | None = None


class UnreadableStore:
    """A context plugin answering with the shapes this task server refuses.

    The turn runtime's `restore` reads the store's answer as a thing it must be
    able to trust, and a store this build started never returns one it cannot
    read — so a test of the refusal hands in the shape a *daemon from another
    build* does, by name.
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
    asserting: the task is `failed`, not `finished`.  The pace is a registry
    value and patching one is what `slife2.helpers.timeouts` exists for; the
    ladder's length is the design and is left alone.
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
    """The real context store, and the decision model it will ask."""
    from slife2.plugins.context.server import build_server as build_context

    ask = DecisionModel()
    select = Selector()
    async with Client(
        build_context(default_config(), embedder=StubEmbedder(), ask=ask, select=select)
    ) as client:
        yield Store(client=client, ask=ask, select=select)


@pytest_asyncio.fixture(loop_scope="function")
async def hub() -> Any:
    """The real toolhub, with every plugin it asks standing in memory.

    **The agent entry declares the live-conversation tools**, as the agent
    server does in production: a task's registry replaces their bodies with its
    own and takes the declarations from the hub, and the automatic
    `_context_rebuild` asks the registry whether the name is there before it
    runs.  `hub_declaring_them` is the same arrangement for a test that also
    records the agent server's bodies.
    """
    from slife2.plugins.toolhub.server import build_server as build_hub

    base = default_config()
    async with Client(
        build_hub(
            base,
            transports=plugin_transports(
                base, {"agent": lambda _settings: declaring_agent([])}
            ),
            embedder=StubEmbedder(),
        )
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
        meta=conversation_meta(AGENT, home=str(home) if home else ""),
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
    `tests/plugins/agent/test_server.py` states for its own wait.  A delivery
    happens *after* the loop returns, and the task's status is not final until
    it lands (a
    successful loop that cannot deliver ends `failed`), so a test that read the
    status and asserted at once would be racing the very thing it is testing.
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
    has no other way to wait for a background task.

    **The bound is a wall clock, not a count of polls.**  A count makes the
    budget depend on how fast each poll is answered, and a loaded CI runner
    spent 400 of them before a turn of two one-second sleeps had finished.  A
    deadline asks the question the test means: has the task stopped by now.

    **And the wall clock has to outlast the runner's stalls, not the task's
    work.**  A task's first two calls — `restore` and `rebuild` — carry no
    timeout of their own (the context store is a peer of the turn, not a
    probe), and a Windows CI worker has been measured taking 43 s to set up a
    neighbouring test and 35 s to run a hub listing.  Thirty seconds was less
    than one such stall: the task was still working when `settle` gave up, and
    the teardown then reported its interrupted call.  Two minutes leaves room
    for the stall and still fails a real hang inside the suite's own
    `faulthandler_timeout`.
    """
    deadline = time.monotonic() + 120.0  # timeouts: a test's patience
    async with Client(built.mcp) as client:
        while time.monotonic() < deadline:
            answer = await ask(client, AGENT_TASK_STATUS, {"name": name})
            if answer["status"] != "running":
                return answer
            await asyncio.sleep(0.01)
    raise AssertionError(f"task {name} never stopped")


async def wait_for_call(built: Built) -> None:
    """Wait until the turn has reached the model, so a watch joins mid-turn."""
    deadline = time.monotonic() + 30.0  # timeouts: a test's patience
    while time.monotonic() < deadline:
        if built.backend.calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the task never reached the model")


async def wait_for_frame(
    frames: list[Any], kind: type, *, timeout: float = 10.0
) -> Any:
    """The first frame of *kind*, polled — the watch stream is a callback."""
    deadline = time.monotonic() + timeout  # timeouts: a test's patience
    while time.monotonic() < deadline:
        for item in frames:
            if isinstance(item, kind):
                return item
        await asyncio.sleep(0.01)
    raise AssertionError(f"{kind.__name__} never arrived; frames: {frames}")


async def wait_for_rebuild(frames: list[Any], *, timeout: float = 10.0) -> Any:
    """The rebuild's answer, polled — the harness auto call's note.

    The auto path writes the pair and emits the tool's own answer as a
    `MessageCutIn`; that line is what a watcher replays from the turn's start.
    """
    deadline = time.monotonic() + timeout  # timeouts: a test's patience
    while time.monotonic() < deadline:
        for item in frames:
            if isinstance(item, MessageCutIn) and item.text.startswith(
                ("kept ", "No rebuild")
            ):
                return item
        await asyncio.sleep(0.01)
    raise AssertionError(f"the rebuild never finished; frames: {frames}")


@contextlib.asynccontextmanager
async def watching(
    built: Built, name: str = "research"
) -> AsyncIterator[tuple[list[Any], asyncio.Task[Any]]]:
    """A watch call held open, and the frames it has been handed so far.

    The call is started as a task and *probed* for its snapshot rather than
    slept on: what proves a watcher is registered is that the server has
    answered it at all.  The helper cancels the call on the way out, which is
    the detach; a test that waits for `Closed` gets the normal return instead.
    """
    frames: list[Any] = []
    snapshot = asyncio.Event()

    async def on_progress(
        progress: float, total: float | None, message: str | None
    ) -> None:
        item = decode_watch(message or "")
        if item is None:
            return
        frames.append(item)
        if isinstance(item, Snapshot):
            snapshot.set()

    async with Client(built.mcp) as client:
        call = asyncio.create_task(
            client.call_tool(
                "watch",
                {"name": name},
                meta=conversation_meta(AGENT),
                progress_handler=on_progress,
                timeout=None,
            )
        )
        try:
            await asyncio.wait_for(snapshot.wait(), timeout=10)
            yield frames, call
        finally:
            call.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await call


# ── the surface ──────────────────────────────────────────────────────────────


async def test_the_model_has_its_four_tools_and_the_watch_beside_them(
    hub: Client, store: Store
) -> None:
    """The model's four, and the one management read beside them.

    `watch` is the terminal's, not the model's: it is what `Ctrl+O` holds open,
    and the gate (SDD §7.1) is what says so — a tool the model may call says
    `for_the_model`, and an absent mark is the answer "no" rather than
    "nobody has decided yet".
    """
    tools = await build(hub=hub, context=store).mcp.list_tools()
    assert sorted(t.name for t in tools if for_the_model(t.meta)) == [
        AGENT_TASK_CANCEL,
        AGENT_TASK_LIST,
        AGENT_TASK_START,
        AGENT_TASK_STATUS,
    ]
    assert sorted(t.name for t in tools if not for_the_model(t.meta)) == ["watch"]


async def test_a_call_that_does_not_say_whose_task_it_is_is_refused(
    hub: Client, store: Store
) -> None:
    """A shared process cannot guess the agent, so it refuses rather than
    answering with somebody else's tasks."""
    async with Client(build(hub=hub, context=store).mcp) as client:
        # No conversation in `_meta` (`slife2/client` is absent): not a call for
        # anybody.
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
    hub: Client, store: Store, window: Window
) -> None:
    """The agent's prompt, the hub's tools, the template as the user message.

    Three claims in one turn because they are one fact: a task is the agent
    working.  The user message is the *rendered* template rather than the
    description verbatim, which is what tells the loop that nobody is listening
    and that what it sends when it stops is the whole of what comes back.
    """
    built = build(text_turn("three tries"), hub=hub, context=store, window=window)
    await start(built)
    await settle(built)

    messages = built.messages()
    assert messages[0].role == "system"
    assert AGENT in str(messages[0].content)
    users = [message for message in messages if message.role == "user"]
    assert len(users) == 1
    opening = users[0].content
    assert isinstance(opening, str)
    # A task gets the same per-turn prompt every other conversation does, and
    # the rendered template follows it.
    assert opening.startswith(TURN_PROMPT_PREFIX)
    opening = strip_marker(opening, TURN_PROMPT_PREFIX)
    assert opening.startswith("▸ Agent task: research")
    assert DESCRIPTION in opening
    assert "nobody can answer a question" in opening
    # The prompt is the one place the exclusions are stated, now that the
    # registry does not filter them.
    assert "do not use `_async`" in opening

    # **The hub's own list, by name** — compared against what the hub
    # advertises rather than against two names, because there is no per-caller
    # *tool set* here: a task gets the names its agent does.  The opening
    # template is what tells it what not to call; the registry does not filter.
    from slife2.clients.toolclient import remote_tools

    assert {spec.name for spec in built.backend.calls[0][1]} == {
        tool.spec.name for tool in await remote_tools(hub)
    }


async def test_the_context_is_rebuilt_exactly_once(
    hub: Client, store: Store, window: Window
) -> None:
    """One turn, so one rebuild — and *not* zero, which is the thing that would
    be wrong: a context that was never chosen is the one turn in this system
    whose context nobody decided (SDD §5.5)."""
    built = build(text_turn("done"), hub=hub, context=store, window=window)
    await start(built)
    await settle(built)

    assert len(store.ask.calls) == 1
    # The decision reads the conversation; the task's own opening is its last
    # user message, not a prompt passed beside the list.
    assert any(
        "▸ Agent task: research" in str(message) for message in store.ask.calls[0]
    )


async def test_the_context_it_runs_on_is_the_agents_own(
    hub: Client, store: Store, window: Window
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
            meta=conversation_meta(AGENT),
        )

    built = build(text_turn("done"), hub=hub, context=store, window=window)
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
    turns nothing could name, and the task's turn and the store disagreed about
    what it held from that point on — the identical deformation the agent
    server's `_adopt` was fixed for.  Both halves of one answer are read the same
    way: a missing or null `turn_ids` is refused, naming the payload
    (SDD Appendix C: a value that cannot be read is not a value that is
    absent).  A task has no record to corrupt, so the refusal is the task's own
    failed answer rather than a store that quietly kept the context it had.
    """
    caplog.set_level(logging.WARNING, logger="slife2.plugins.agent_task.server")
    built = build(
        text_turn("done"),
        hub=hub,
        context=Store(
            client=UnreadableStore(restore={"messages": [], "turns": []}),
            ask=DecisionModel(),
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


async def test_a_running_task_is_not_reported_with_a_finish_time(
    hub: Client, store: Store, window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**`finished_at` is empty until the task stops** — the field's contract,
    and the loop's stamp does not bend it: the moment is written when the loop
    returns, but the status stays `running` through the hand-over, so a status
    read in that window carried a finish time beside `running`, two
    contradictory facts.  The report withholds the stamp while the status is
    `running`; the marker, which claims the outcome the answer will have,
    carries it — and the settled report and the marker agree on the moment.

    The hand-over is gated to hold the task in that window: the delivery is
    where a successful loop is still `running`, and the gate is what makes the
    read land inside it rather than race it.
    """
    import slife2.plugins.agent_task.server as module

    entered = asyncio.Event()
    release = asyncio.Event()
    real_hand_over = module.hand_over

    async def gated_hand_over(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await release.wait()
        await real_hand_over(*args, **kwargs)

    monkeypatch.setattr(module, "hand_over", gated_hand_over)
    built = build(text_turn("done"), hub=hub, context=store, window=window)
    await start(built)
    await asyncio.wait_for(entered.wait(), timeout=10)

    async with Client(built.mcp) as client:
        running = await ask(client, AGENT_TASK_STATUS, {"name": "research"})
        listed = await ask(client, AGENT_TASK_LIST)
    assert running["status"] == "running"
    assert running["finished_at"] == "", "a running task reported a finish time"
    assert listed["tasks"][0]["finished_at"] == ""

    release.set()
    answer = await settle(built)
    assert answer["status"] == "finished"
    assert answer["finished_at"]
    await wait_for_handover(window)

    text = window.handed[0]["text"]
    payload = json.loads(text[len(MARKER_PREFIX) : text.index("]")])
    assert payload["status"] == "finished"
    assert payload["finished_at"] == answer["finished_at"], (
        "the marker and the settled report disagree about when the work ended"
    )


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
    this server is `runtime.current()`'s (`run_loop`), and the sibling agent
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
    theirs = a_home(isolated_runtime.parent / "theirs")
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
    theirs = a_home(isolated_runtime.parent / "theirs")
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


async def test_two_tasks_decide_privately_and_leave_the_conversation_alone(
    hub: Client, window: Window
) -> None:
    """**A task's rebuild is a clone: it cannot move the agent's live context.**

    Two tasks of one agent may decide differently about the same conversation —
    the cap allows `max_running` of them.  Each decision is the task's own: the
    first keeps one of the two turns, the second clears its clone.  What the
    store holds afterwards is **unchanged**: the two turns the agent saved, in
    the order it saved them.  A task records nothing, so a turn that leaves no
    record must not change what the conversation comes back to.

    The lock this test used to exercise is gone with the write it protected:
    `restore` is a read and `rebuild` publishes nothing, so the two tasks can
    interleave their pre-turn work without either seeing the other's decision.
    """
    from slife2.libs.host.paths import agent_dir
    from slife2.libs.store.db import TurnStore
    from slife2.plugins.context.server import build_server as build_context

    asked: list[str] = []
    parked = asyncio.Event()
    second_in = asyncio.Event()
    gate = asyncio.Event()
    keep_one = ""

    async def deciding(
        agent: str, subagent: str, model: str, messages: Any, *, usage: str = ""
    ) -> str:
        """The first task parks inside its rebuild; the second clears its own."""
        asked.append("")
        if len(asked) == 1:
            parked.set()
            await gate.wait()
            return keep_one
        second_in.set()
        return '{"mode": "clear"}'

    async with Client(
        build_context(default_config(), embedder=StubEmbedder(), ask=deciding)
    ) as context_client:
        store = Store(client=context_client, ask=DecisionModel())
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
                    meta=conversation_meta(AGENT),
                )
                ids.append(stored.data["turn_id"])
        # The first task's own decision keeps one of the two turns.
        keep_one = json.dumps({"mode": "ids", "turn_ids": [ids[0]]})

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
        # The second task's snapshot is taken while the first is parked: with
        # the write gone there is nothing to serialise, so its own rebuild runs.
        for _ in range(100):
            if second_in.is_set():
                break
            await asyncio.sleep(0.01)
        gate.set()
        await wait_for_handover(window, 2)
        assert len(built.backend.calls) == 2, "one model call per task"

    turns = TurnStore(agent_dir(AGENT) / "turns.db").context_turns()
    assert turns == ids, "a task does not move the conversation's live context"

    # Each task ran on its own decision: the first kept alpha, the second
    # cleared its clone.  The opening prompt names the task, so the calls are
    # identified without relying on scheduling order.
    by_name: dict[str, str] = {}
    for messages, _tools in built.backend.calls:
        text = "\n".join(str(message.content) for message in messages)
        for name in ("one", "two"):
            if f"Agent task: {name}" in text:
                by_name[name] = text
    assert "alpha is here" in by_name["one"]
    assert "beta is here" not in by_name["one"], "the first task's own keep-list"
    assert "alpha is here" not in by_name["two"]
    assert "beta is here" not in by_name["two"], "the second task's own clear"


# ── stopping one ─────────────────────────────────────────────────────────────


async def test_cancelling_a_running_task_takes_its_answer_with_it(
    hub: Client, store: Store, window: Window
) -> None:
    """A task that was called off has nothing to report, so nothing is handed
    over — SDD §5.6's rule for a cancelled turn, one level down."""
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


async def test_a_task_whose_answer_reached_no_window_is_failed(
    hub: Client,
    store: Store,
    caplog: pytest.LogCaptureFixture,
    a_quick_ladder: None,
) -> None:
    """The answer is the product, so a task that could not deliver it is not
    `finished` — however much work it did.

    Built against **the real `slife2-tui`** with nothing attached, because what
    makes this fire is that plugin's own refusal rather than a stub's: no
    terminal is showing this agent, and there is no second road for the answer
    (`slife2.libs.harness.handover`).  The work happened; the answer reached
    nobody; the status says `failed` and the log says why.
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
        answer = await settle(built)
        await wait_for_log(caplog, "was not delivered")

    assert answer["status"] == "failed"
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
    repeated, and the task is `failed` rather than a doubled `finished` one.
    """
    caplog.set_level(logging.ERROR, logger="slife2.plugins.agent_task.server")
    built = build(
        text_turn("three tries"),
        hub=hub,
        context=store,
        window=broken_window,
    )
    await start(built)
    answer = await settle(built)
    await wait_for_log(caplog, "was not repeated")

    assert answer["status"] == "failed"
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
    delivery like any other, and the task ends `failed`.
    """
    caplog.set_level(logging.ERROR, logger="slife2.plugins.agent_task.server")
    built = build(
        text_turn("three tries"),
        hub=hub,
        context=store,
        window=dead_window,
    )
    await start(built)
    answer = await settle(built)
    await wait_for_log(caplog, "was not delivered")

    assert answer["status"] == "failed"
    assert "was not delivered" in caplog.text


async def test_a_hand_over_that_crashed_is_said_not_left_on_the_coroutine(
    hub: Client,
    store: Store,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A task has nobody behind it to raise to.**

    `run` is a detached coroutine: an exception that leaves it is stored on a
    task nothing reads, and the answer beside it is cleared by the `finally`
    that unwinds — a trace nobody saw where the design owes the agent a
    message.  Any failure of the hand-over is a failure to deliver, however it
    arrived, and it is said at `error` with the trace.
    """
    import slife2.plugins.agent_task.server as module

    async def crashed(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the hand-over itself came apart")

    monkeypatch.setattr(module, "hand_over", crashed)
    caplog.set_level(logging.ERROR, logger="slife2.plugins.agent_task.server")
    built = build(text_turn("three tries"), hub=hub, context=store)
    await start(built)
    answer = await settle(built)
    await wait_for_log(caplog, "nothing else would report it")

    assert answer["status"] == "failed"
    assert "nothing else would report it" in caplog.text


# ── what the registry remembers ──────────────────────────────────────────────


async def test_the_registry_forgets_from_the_tail(
    hub: Client, store: Store, window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded, because a model can start tasks for as long as somebody keeps
    asking it to — and forgetting is why `unknown` is an honest answer rather
    than a defect."""
    import slife2.plugins.agent_task.server as module

    monkeypatch.setattr(module, "RETIRED", 2)
    built = build(
        text_turn("a"),
        text_turn("b"),
        text_turn("c"),
        hub=hub,
        context=store,
        window=window,
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
    hub: Client, store: Store, window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry is keyed by `(agent, name)` and the *retired* list holds
    objects rather than keys, because a name that has been started again is a
    different task under the same key — and evicting by key would throw the new
    one away."""
    import slife2.plugins.agent_task.server as module

    monkeypatch.setattr(module, "RETIRED", 1)
    built = build(
        text_turn("first"), text_turn("second"), hub=hub, context=store, window=window
    )
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
    hub: Client,
    store: Store,
    window: Window,
    caplog: pytest.LogCaptureFixture,
    monkeypatch,
) -> None:
    """The trim is not optional (SDD §9.3), and a task has no next turn for a dead
    hub to surface on.

    The agent server can leave its trim best-effort because its next request
    rebuilds the tool list from the hub and fails loudly if it is gone; a task
    runs one turn, so a skip with a warning and nothing else is a skip nobody
    ever reads.  What the task keeps is the outcome — bookkeeping does not fail a
    turn that already answered — and what it adds is that the skip is *said* at
    `error`, naming the task, because the loaded set is one list for the whole
    system.  Registered as a fallback in SDD Appendix C.
    """
    import slife2.libs.turn.runtime as runtime_module

    async def no_hub(_client: Any) -> dict[str, Any]:
        raise ConnectionError("the hub is gone")

    monkeypatch.setattr(runtime_module, "unload_tools", no_hub)
    caplog.set_level(logging.ERROR, logger="slife2.plugins.agent_task.server")
    built = build(text_turn("three tries"), hub=hub, context=store, window=window)
    await start(built)
    await settle(built)

    assert "was not trimmed" in caplog.text


async def test_a_trim_answer_naming_no_names_is_refused_rather_than_read_as_none(
    hub: Client,
    store: Store,
    window: Window,
    caplog: pytest.LogCaptureFixture,
    monkeypatch,
) -> None:
    """An answer missing `unloaded` read as "nothing was unloaded".

    The same deformation the agent server's trim was fixed for, one plugin over:
    `found.get("unloaded") or []` took a hub answer this build cannot read — the
    shape a daemon from another build returns — as an honest no-op, so no pair
    was written and the record never said the list had shrunk.  Absent is not
    empty: both halves of the answer are read strictly, so a missing key is a
    failure and only a *readable* empty answer is a verdict (SDD Appendix C: a
    failure is not a verdict).  The skip is still best-effort and said at
    `error`, naming the task, because the trim is post-turn bookkeeping.
    """
    import slife2.libs.turn.runtime as runtime_module

    async def no_names(_client: Any) -> dict[str, Any]:
        # Every key but the one the trim acts on: the same silence an empty list
        # makes, from an answer that says nothing at all.
        return {"refused": [], "not_loaded": [], "text": "the list is fine"}

    monkeypatch.setattr(runtime_module, "unload_tools", no_names)
    caplog.set_level(logging.ERROR, logger="slife2.plugins.agent_task.server")
    built = build(text_turn("three tries"), hub=hub, context=store, window=window)
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
    (SDD Appendix C, "a failure is not a verdict").

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
    can still offer rather than an error with no reader (SDD Appendix C, "a
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


# ── watching a task ──────────────────────────────────────────────────────────


async def test_a_watch_opens_with_the_brief_and_replays_a_turn_it_joined(
    hub: Client, store: Store, window: Window
) -> None:
    """A task has no stored conversation, so the brief is its history.

    The turn in flight is in the tap, and a watcher that joins after the model
    has been called is handed the context decision and the deltas it already
    produced — not just the tail.  The tap's start is the `TaskBrief`, which is
    the one frame a task's own caller never needs and a watcher cannot infer.
    """
    built = build(
        text_turn("abcdef", delay=0.05), hub=hub, context=store, window=window
    )
    await start(built)
    await wait_for_call(built)

    async with watching(built) as (frames, call):
        first = await wait_for_frame(frames, Snapshot)
        assert first.turns == [], "a task records nothing to open with"

        brief = await wait_for_frame(frames, TaskBrief)
        assert brief.name == "research"
        assert brief.description == DESCRIPTION
        assert brief.status == "running", "the status the task had when it began"

        # The rebuild's pair is in the tap even though it ran after this
        # watcher joined — the other half of the replay.
        await wait_for_rebuild(frames)
        finished = await wait_for_frame(frames, TurnFinished)
        assert finished.text == "abcdef"
        closed = await wait_for_frame(frames, Closed)
        assert "finished" in closed.reason
        assert any(isinstance(item, TextDelta) for item in frames)

        result = await asyncio.wait_for(call, timeout=10)

    assert result.data == {"closed": True}
    await wait_for_handover(window)


async def test_a_watch_of_a_stopped_task_says_what_happened(
    hub: Client, store: Store, window: Window
) -> None:
    """A late watch is answered rather than refused.

    The task is still in the registry until the retired cap forgets it, so the
    screen opens on the brief with the final status and the sentence its ending
    carried.  There are no events to replay: a task's turn is gone with the
    coroutine that ran it.
    """
    built = build(text_turn("done"), hub=hub, context=store, window=window)
    await start(built)
    await wait_for_handover(window)
    await settle(built)

    async with watching(built) as (frames, call):
        result = await asyncio.wait_for(call, timeout=10)

    assert result.data == {"closed": True}
    assert [type(item) for item in frames] == [Snapshot, TaskBrief, Closed]
    brief = frames[1]
    assert brief.status == "finished"
    assert "delivered" in frames[2].reason


async def test_cancelling_a_watched_task_closes_with_cancelled(
    hub: Client, store: Store, window: Window
) -> None:
    """A cancelled task has no answer and no hand-over; the watcher is told."""
    built = build(text_turn("abcdef", delay=0.1), hub=hub, context=store, window=window)
    await start(built)
    await wait_for_call(built)

    async with watching(built) as (frames, call):
        await wait_for_frame(frames, TaskBrief)
        async with Client(built.mcp) as client:
            stopped = await ask(client, AGENT_TASK_CANCEL, {"name": "research"})
        assert stopped["status"] == "cancelled"

        closed = await wait_for_frame(frames, Closed)
        result = await asyncio.wait_for(call, timeout=10)

    assert result.data == {"closed": True}
    assert closed.reason == "the task was cancelled"


async def test_a_cancelled_runs_finally_leaves_the_replacements_watch_alone(
    hub: Client, store: Store, window: Window, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The old turn's ending must not end the new task's watch.**

    `agent_task_cancel` writes `cancelled` synchronously, but the cancelled
    coroutine's `finally` runs later — and the name is startable again the
    moment the status is not `running`.  When the new task's `start_tap` has
    replaced the tap, the old `finally` closing and dropping by key alone sent
    the new task's watchers the old task's `Closed` and took the new tap's
    replay from later joiners (`WatchHub.drop_if`'s reason).

    The two hand-overs are gated to make the window deterministic: the first
    holds the old run *after* its cancellation and *before* its `finally`,
    the second holds the new task mid-delivery so a late watcher joins while
    its tap is still the live one.  The old `finally` then runs against the
    replacement's tap, and the assertions are what it must leave alone.
    """
    import slife2.plugins.agent_task.server as module

    made: list[Any] = []
    real_task = module.AgentTask

    def recording(**fields: Any) -> Any:
        task = real_task(**fields)
        made.append(task)
        return task

    monkeypatch.setattr(module, "AgentTask", recording)

    first_entered = asyncio.Event()
    first_held = asyncio.Event()
    first_release = asyncio.Event()
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    real_hand_over = module.hand_over
    deliveries = 0

    async def gated_hand_over(*args: Any, **kwargs: Any) -> None:
        nonlocal deliveries
        deliveries += 1
        if deliveries == 1:
            first_entered.set()
            try:
                # Never set: the cancellation is what ends this wait, and the
                # except below holds the run before its `finally` runs.
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_held.set()
                await first_release.wait()
                raise
        if deliveries == 2:
            second_entered.set()
            await second_release.wait()
        await real_hand_over(*args, **kwargs)

    monkeypatch.setattr(module, "hand_over", gated_hand_over)
    built = build(
        text_turn("old answer"),
        text_turn("new answer"),
        hub=hub,
        context=store,
        window=window,
    )
    await start(built)
    await asyncio.wait_for(first_entered.wait(), timeout=10)

    async with Client(built.mcp) as client:
        stopped = await ask(client, AGENT_TASK_CANCEL, {"name": "research"})
    assert stopped["status"] == "cancelled"
    await asyncio.wait_for(first_held.wait(), timeout=10)
    old_handle = made[0].handle
    assert old_handle is not None

    # The name is free again: a second task under it, with its own tap.  It
    # runs its turn and parks in its hand-over, still `running`.
    await start(built)
    assert made[1].tap is not made[0].tap
    await asyncio.wait_for(second_entered.wait(), timeout=10)

    async with watching(built) as (frames1, call1):
        await wait_for_frame(frames1, TaskBrief)
        # The old run's finally runs now, against the replacement's tap.
        first_release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await old_handle

        # A watcher joining after the old finally still replays the new
        # task's turn from its start — the tap the old drop would have taken.
        async with watching(built) as (frames2, call2):
            await wait_for_rebuild(frames2)
            second_release.set()
            closed2 = await wait_for_frame(frames2, Closed)
            assert "finished" in closed2.reason
            assert "cancelled" not in closed2.reason
            result2 = await asyncio.wait_for(call2, timeout=10)
        assert result2.data == {"closed": True}

        # The watcher that was there when the old finally ran was not closed
        # by the old task's ending; it ends with the new task's own.
        closed1 = await wait_for_frame(frames1, Closed)
        assert "cancelled" not in closed1.reason, (
            "the cancelled task's finally closed the replacement's watcher"
        )
        assert "finished" in closed1.reason
        result1 = await asyncio.wait_for(call1, timeout=10)

    assert result1.data == {"closed": True}
    await wait_for_handover(window)
    assert len(window.handed) == 1, "the cancelled task delivered an answer"
    assert window.handed[0]["text"].endswith("new answer")


# ── the turn a task stands in for ────────────────────────────────────────────

SUBAGENT = "scout"


def a_subagent(*, role: str = "researcher") -> None:
    """A subagent record in the test's home, which is what makes the name one.

    A subagent with no model runs where its maker runs (`SubagentSpec`), so the
    record changes the prompt and the role and leaves the model at the default —
    which is what this test is about.
    """
    statefile.write(
        subagent_file(AGENT, SUBAGENT),
        {"name": SUBAGENT, "model": "", "role": role},
    )


def tool_turn(*calls: ToolCall, usage: Usage | None = None) -> ScriptedTurn:
    """One scripted model response that calls tools, then a text turn follows."""
    return ScriptedTurn(
        result=StreamChatResult(
            text="",
            tool_calls=calls,
            stop_reason="tool_calls",
            usage=usage or Usage(),
        ),
        chunks=[Chunk(text="")],
    )


def tool_result(messages: list[Any], name: str) -> Any:
    """The result of the call to *name* in one model call's message list.

    The turn opens with `_context_rebuild`'s harness pair, so the first tool
    message is not the one a test about `_check_new_input` or `attach_image`
    wants; the pair's id is what tells them apart.
    """
    ids = {
        call.id
        for message in messages
        if message.role == "assistant"
        for call in message.tool_calls
        if call.name == name
    }
    return next(
        message
        for message in messages
        if message.role == "tool" and message.tool_call_id in ids
    )


def declaring_agent(reached: list[str]) -> FastMCP:
    """The agent server's *declarations* for the four live-conversation tools.

    The hub must advertise the names — a task's registry replaces their bodies
    and takes their declarations from the hub — and reaching one of these bodies
    is the aliasing the task's own bodies exist to prevent.  So every body
    records itself, and a test asserts the list stayed empty.
    """
    mcp = FastMCP("agent")

    @mcp.tool(name=CHECK_NEW_INPUT, meta=FOR_THE_MODEL)
    async def check_new_input(ctx: Context) -> str:
        reached.append(CHECK_NEW_INPUT)
        return "the agent's queued message"

    @mcp.tool(name=ATTACH_IMAGE, meta=FOR_THE_MODEL)
    async def attach_image(ctx: Context, sources: list[str]) -> dict[str, Any]:
        reached.append(ATTACH_IMAGE)
        return {"attached": [], "refused": []}

    @mcp.tool(name=TURN_COMPACT, meta=FOR_THE_MODEL)
    async def turn_compact(ctx: Context) -> str:
        reached.append(TURN_COMPACT)
        return "the agent's compaction"

    @mcp.tool(name=CONTEXT_REBUILD, meta=FOR_THE_MODEL)
    async def context_rebuild(ctx: Context) -> str:
        reached.append(CONTEXT_REBUILD)
        return "the agent's rebuild"

    return mcp


def hub_declaring_them(reached: list[str]) -> Client:
    """A hub whose `agent` is `declaring_agent` and whose others are the usual."""
    from slife2.plugins.toolhub.server import build_server as build_hub

    base = default_config()
    return Client(
        build_hub(
            base,
            transports=plugin_transports(
                base, {"agent": lambda _settings: declaring_agent(reached)}
            ),
            embedder=StubEmbedder(),
        )
    )


async def test_a_subagents_task_runs_on_the_subagents_own_conversation(
    hub: Client, store: Store, window: Window
) -> None:
    """**The maker is a conversation, not an agent name.**

    A subagent that starts a task is asking for a turn of *its* conversation:
    `subagent.j2` with its role, the subagent's context, and the subagent's plan.
    The answer still reaches the agent's window — a subagent has none of its own
    — but the turn is the subagent's, which is what the prompt and the restored
    history are here to prove.
    """
    a_subagent(role="researcher")
    async with Client(store.client.transport) as writer:  # the same store
        await writer.call_tool(
            "remember",
            {
                "agent": AGENT,
                "subagent": SUBAGENT,
                "messages": [
                    {
                        "role": "user",
                        "content": "scout settled on three retries",
                    }
                ],
                "who_helped": SUBAGENT,
                "channel": "subagent:scout",
                "created_at": "2026-10-12T09:00:00+08:00",
                "completed_at": "2026-10-12T09:00:01+08:00",
            },
            meta=conversation_meta(AGENT, SUBAGENT),
        )

    built = build(text_turn("three"), hub=hub, context=store, window=window)
    async with Client(built.mcp) as client:
        await client.call_tool(
            AGENT_TASK_START,
            {"name": "research", "description": DESCRIPTION},
            meta=conversation_meta(AGENT, SUBAGENT),
        )
    await settle(built)

    messages = built.messages()
    system = str(messages[0].content)
    assert "one of jack's subagents" in system, system
    assert "your role is researcher" in system, system
    assert any(
        "scout settled on three retries" in str(message.content) for message in messages
    ), "the task ran on the subagent's context, not the agent's"
    # The answer goes to the only window there is: the agent's.
    assert window.handed[0]["agent"] == AGENT


async def test_a_task_has_no_cut_in_and_takes_nothing_from_the_maker(
    store: Store, window: Window
) -> None:
    """`_check_new_input` in a task answers "nothing waiting" — and reaches no
    agent server.

    A task is one user message and one turn: it has no inbox, and the message
    queue the agent server holds is its maker's, not the task's.  The tool keeps
    its declaration (the hub advertises it to every model), and its body is the
    task's own — so the agent server's `_check_new_input`, which would take the
    maker's queued message, is never called.
    """
    reached: list[str] = []
    hub = hub_declaring_them(reached)
    async with hub:
        built = build(
            tool_turn(ToolCall(id="c1", name=CHECK_NEW_INPUT, arguments={})),
            text_turn("done"),
            hub=hub,
            context=store,
            window=window,
        )
        await start(built)
        await settle(built)

    assert reached == [], "the task reached the agent server's cut-in"
    tool = tool_result(built.messages(), CHECK_NEW_INPUT)
    assert NOTHING_WAITING in str(tool.content), "the view carries the id footnote"


async def test_a_task_compacts_itself_at_the_threshold(
    store: Store, window: Window
) -> None:
    """**A task is one turn with no record**, so a turn that overflows has no
    next turn to recover on.  The same automatic pass the agent server runs
    keeps it alive: the harness asks, the selector drops, the answer arrives.

    The names the hub declares for `_turn_compact` stay the hub's; the body is
    the task's own, so the agent server's compaction is never reached.
    """
    reached: list[str] = []
    hub = hub_declaring_them(reached)
    high = Usage(prompt_tokens=500, completion_tokens=100)
    async with hub:
        built = build(
            tool_turn(
                ToolCall(id="c1", name=CHECK_NEW_INPUT, arguments={}), usage=high
            ),
            tool_turn(
                ToolCall(id="c2", name=CHECK_NEW_INPUT, arguments={}), usage=high
            ),
            text_turn("done"),
            hub=hub,
            context=store,
            window=window,
            compact_at=0.0005,
        )
        await start(built)
        await settle(built)

    assert reached == [], "the task reached the agent server's compaction"
    assert store.select is not None and store.select.calls, "the harness asked"
    calls = [
        call
        for message in built.messages()
        if message.role == "assistant"
        for call in message.tool_calls
    ]
    assert not any(call.id in {"c1", "c2"} for call in calls), (
        "the dropped exchanges are out of the view the last call ran on"
    )
    assert window.handed and "done" in window.handed[-1]["text"]


async def test_a_tasks_attach_image_lands_on_its_own_next_call(
    store: Store, window: Window
) -> None:
    """The picture goes on the task's *own* model call, which is the next one.

    A task's single user message is the rendered description, so that is where
    `attach_image` injects — the task's list is the only list its next request
    carries.  The agent server's version, which would inject into the maker's
    live loop, is never reached.
    """
    reached: list[str] = []
    hub = hub_declaring_them(reached)
    source = "data:image/png;base64,AAAA"
    async with hub:
        built = build(
            tool_turn(
                ToolCall(id="c1", name=ATTACH_IMAGE, arguments={"sources": [source]})
            ),
            text_turn("I see it"),
            hub=hub,
            context=store,
            window=window,
        )
        await start(built)
        await settle(built)

    assert reached == [], "the task reached the agent server's attach_image"
    messages = built.messages()
    opening = [message for message in messages if message.role == "user"][0]
    assert isinstance(opening.content, list), "the task's message carries the parts"
    urls = [
        part["image_url"]["url"]
        for part in opening.content
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]
    assert urls == [source]
    tool = tool_result(messages, ATTACH_IMAGE)
    assert json.loads(without_id(tool.content)) == {
        "attached": [source],
        "refused": [],
    }


async def test_a_task_of_a_subagent_that_is_gone_does_not_recreate_the_name(
    hub: Client, store: Store, window: Window
) -> None:
    """A name with no record is not a conversation, and a task does not make one.

    The record's *existence* is what makes a directory a subagent's; a task that
    ran on a removed name would restore a conversation whose store recreates the
    directory without a record, and that blocks the name for `subagent_create`
    and `subagent_remove` for ever.  So the task fails with a message — which is
    the one thing it can still deliver.
    """
    built = build(text_turn("never reached"), hub=hub, context=store, window=window)
    async with Client(built.mcp) as client:
        await client.call_tool(
            AGENT_TASK_START,
            {"name": "research", "description": DESCRIPTION},
            meta=conversation_meta(AGENT, SUBAGENT),
        )
    stopped = await settle(built)

    assert stopped["status"] == "failed"
    assert not subagent_file(AGENT, SUBAGENT).parent.exists(), (
        "the task recreated a record-less subagent directory"
    )
    assert "no subagent" in window.handed[0]["text"]
