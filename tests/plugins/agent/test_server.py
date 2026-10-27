"""The agent MCP server, which owns its loops.

Almost everything here runs over FastMCP's **in-memory transport**, which binds
no port and needs no LLM — `build_server(..., backend=...)` takes a scripted
backend, so the whole server including its streaming contract is exercised
without I/O.  That is why these are marked `unit` rather than `integration`:
they do not touch the network, and marking them otherwise would make `-m unit`
stop meaning "fast".

One test does bind a real socket, because the in-memory transport cannot prove
that progress notifications survive HTTP.  That one is `integration`.

The property under test throughout is that a loop **is** the conversation: a
message sent to it is answered in the context of everything said to it before,
and two loops on one server never see each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sqlite3
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
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
    keep_the_context,
    text_turn,
)
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from ruamel.yaml import YAML

import slife2.helpers.timeouts as _T
from slife2.libs.harness.markers import strip as strip_marker
from slife2.libs.host.config import (
    DEFAULT_AGENT,
    LOCAL_AGENT,
    A2ASettings,
    ModelSettings,
    default_config,
)
from slife2.libs.host.paths import agents_dir, subagent_dirs, subagent_file
from slife2.libs.model.base import Chunk, Stream
from slife2.libs.plugin.audience import client_meta
from slife2.libs.store.db import TurnStore
from slife2.libs.turn import attachments
from slife2.libs.turn.context import (
    TURN_PREFIX,
    TURN_PROMPT_PREFIX,
    TURN_SUFFIX,
    turn_footnote,
)
from slife2.libs.turn.events import (
    MessageCutIn,
    TextDelta,
    ToolsCounted,
    TurnEvent,
    TurnFinished,
    decode,
)
from slife2.libs.turn.messages import StreamChatResult, ToolCall, Usage
from slife2.libs.turn.tools import INTERRUPTED
from slife2.libs.turn.watch import (
    Closed,
    Snapshot,
    TurnStarted,
)
from slife2.libs.turn.watch import (
    decode as decode_watch,
)
from slife2.plugins.agent.server import (
    ATTACH_IMAGE,
    SUBAGENT_REMOVE,
    SUBAGENT_SET,
    Pending,
    ProgressObserver,
    _remove_tree,
    build_server,
)

pytestmark = pytest.mark.unit


def config(**agent_overrides):
    base = default_config()
    return replace(base, agent=replace(base.agent, **agent_overrides))


def tool_then_answer() -> FakeBackend:
    """One scripted turn that calls a tool, then one that answers."""
    return FakeBackend(
        asks_for_a_tool(),
        ScriptedTurn(
            result=StreamChatResult(text="It is 42.", stop_reason="stop"),
            chunks=[Chunk(text="It is 42.")],
        ),
    )


def answering(text: str) -> FakeBackend:
    return FakeBackend(ScriptedTurn(result=StreamChatResult(text=text)))


def asks_for_a_tool(
    *,
    delay: float = 0.0,  # timeouts: the value this test is built around, not a budget
    usage: Usage | None = None,
) -> ScriptedTurn:  # timeouts: the value this test is built around, not a budget
    """The iteration that calls a tool — and so gives the turn a second boundary.

    A turn that answers in one iteration has no boundary after its first, which is
    why the cut-in needs this shape to be exercised at all.  The delay is what
    keeps the arrival *inside* the first iteration instead of racing the second: a
    test that submits while the stream is still coming is testing the boundary
    the feature is about.
    """
    return ScriptedTurn(
        result=StreamChatResult(
            text="checking",
            tool_calls=(ToolCall(id="c1", name="skill_list", arguments={}),),
            stop_reason="tool_calls",
            usage=usage or Usage(),
        ),
        chunks=[Chunk(text="checking")],
        delay=delay,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def context():
    """A context store that actually answers, over the in-memory transport.

    Sending **cannot happen without it**: a store that is not there is a broken
    system rather than a degraded one, so `send_message` fails before anything is
    spent on a model call.  See
    `slife2.libs.plugin.protocol.open_server`.

    The real server rather than a stub, so what these tests exercise is the
    plugin the agent server actually talks to — the one test that is *about*
    the failure passes its own config instead.
    """
    from slife2.plugins.context.server import build_server as build_context

    async with Client(
        build_context(default_config(), embedder=StubEmbedder(), ask=keep_the_context)
    ) as client:
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def cabinet():
    """An in-memory cabinet, for the turns whose images are recorded.

    A turn with an image in it now keeps that image here (`record`), so a test
    that runs one needs the peer to answer.  Real and in-memory rather than a
    stub, for the hub fixture's reason: what the agent server makes is a call,
    and a stand-in answering a different shape would prove nothing about it.
    """
    from slife2.plugins.cabinet.server import build_server as build_cabinet

    async with Client(
        build_cabinet(default_config(), embedder=StubEmbedder())
    ) as client:
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def hub():
    """A toolhub that actually answers, over the in-memory transport.

    A turn cannot run without one, for the same reason it cannot run without
    the store — the model's tool list comes from here, our plugins included, so
    a missing hub is a broken system rather than a conversation with no tools.

    No upstreams: these tests are about the agent, and the tool servers behind a
    hub are `tests/test_toolhub.py`'s subject.  What it does prove here is that
    the registry the loop runs with is the one the hub advertised.

    The real server rather than a stub, so the tool names and schemas these
    tests see crossed a real MCP hop.
    """
    from slife2.plugins.toolhub.server import build_server as build_hub
    from tests.fakes import plugin_transports

    # The hub asks every plugin for a tool list and refuses when one does not
    # answer, so all of them need something behind them.  In-memory, which keeps
    # `skill_list` real without a port.
    async with Client(
        build_hub(
            default_config(),
            transports=plugin_transports(default_config()),
            embedder=StubEmbedder(),
        )
    ) as client:
        yield client


async def send(
    server: FastMCP,
    prompt: str,
    *,
    agent: str = DEFAULT_AGENT,
    subagent: str = "",
    **arguments,
):
    """Say one thing to a conversation, naming it the way every server does.

    A fresh client each time, deliberately: the conversation lives in the
    *server*, so a test that reconnects is exercising the same thing a TUI does
    when it comes back — and one that reconnects by accident is not silently
    passing.
    """
    async with Client(server) as client:
        return await client.call_tool(
            "send_message",
            {"agent": agent, "subagent": subagent, "prompt": prompt, **arguments},
        )


async def reset(server: FastMCP, *, agent: str = DEFAULT_AGENT, subagent: str = ""):
    """Forget a conversation."""
    async with Client(server) as client:
        return await client.call_tool("reset", {"agent": agent, "subagent": subagent})


async def attach(
    server: FastMCP, sources: list[str], *, agent: str = DEFAULT_AGENT
) -> dict[str, Any]:
    """Call `attach_image` the way the turn does, and answer its JSON.

    The caller's identity rides in `_meta`, exactly as it does when the loop
    makes the call on the model's behalf — and `.data`, because the shape is the
    subject here rather than the rendered text.
    """
    async with Client(server) as client:
        result = await client.call_tool(
            ATTACH_IMAGE, {"sources": sources}, meta=client_meta(agent)
        )
    return result.data


async def wait_for_streams(backend: FakeBackend, wanted: int = 1) -> None:
    """Block until the backend has been asked for that many model streams.

    **Generous, and asserted rather than bounded-and-ignored.**  A turn reaches
    the model only after the context has been restored and rebuilt, so a budget
    that was ample when the first thing a turn did was call the model is no
    longer: a loop that merely gave up would cancel a turn that had not begun,
    the cancel would be a no-op, and the failure would surface somewhere else
    entirely — `prompts_seen(backend, 1)` against a list with one entry, which
    says nothing about what actually went wrong.
    """
    for _ in range(1000):
        if len(backend.calls) >= wanted:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"the model was asked for {len(backend.calls)} stream(s), not {wanted}"
    )


def without_prompt(content: Any) -> Any:
    """One message's content with the per-turn prompt marker taken off the front.

    `[TURN-PROMPT:{…}]` is added to the live user message after the recall and
    removed before the turn is saved, so a model call in between is its only
    reader.  Every assertion about what a turn *said* strips it the way it strips
    the footnote: the marker is the turn's clock, not its words, and leaving it
    in would make each expectation depend on the minute the test ran.

    A list's marker is its leading text part, which is the shape a message with
    an attachment has.
    """
    if isinstance(content, str):
        return strip_marker(content, TURN_PROMPT_PREFIX)
    if isinstance(content, list) and content and content[0].get("type") == "text":
        text = str(content[0].get("text", ""))
        stripped = strip_marker(text, TURN_PROMPT_PREFIX)
        if stripped != text:
            return (
                content[1:]
                if not stripped
                else [{**content[0], "text": stripped}, *content[1:]]
            )
    return content


def without_footnote(content: Any) -> Any:
    """One message's content with the turn prompt and footnote taken off.

    A `[TURN:{…}]` footnote is on every user message after its turn is saved,
    so leaving it in would make every assertion about *which turns are in hand*
    longer and clock-dependent without making it stronger — the same argument
    `prompts_seen` makes for the system prompt below, and the footnote has its
    own test (`test_a_turn_in_memory_carries_its_own_footnote`).

    Only a *trailing* one is cut, and only when it is closed: a message may
    legitimately contain the literal marker in prose, and the footnote is always
    a suffix.  That is the rule the envelope itself follows, for the same reason.
    """
    content = without_prompt(content)
    if not isinstance(content, str) or not content.endswith(TURN_SUFFIX):
        return content
    start = content.rfind(TURN_PREFIX)
    return content[:start].rstrip() if start != -1 else content


def prompts_seen(backend: FakeBackend, call: int = 0) -> list[str]:
    """What the model was sent on one of its calls, as role-tagged text.

    The system prompt is dropped: it is the same on every call and its content
    is the subject of its own tests, so including it here would only make every
    assertion about the conversation longer without making it stronger.  The
    turn footnote goes for the same reason, and `without_footnote` says why.
    """
    return [
        f"{m.role}:{without_footnote(m.content)}"
        for m in backend.calls[call][0]
        if m.role != "system"
    ]


# --- the surface -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_server_exposes_its_own_tools(context, hub) -> None:
    """There is no `open_loop` because there is nothing to open.

    An id a server mints is an id a caller has to keep, and keeping it is where
    every lifetime problem starts.  A conversation is started by the first
    message that names it, so sending *is* opening — and what is left is one verb
    and one way to start over.

    `_check_new_input` is not a verb a caller has any use for: it is the name a
    message that arrives mid-turn is delivered under, and it is declared here
    because the *pair* the harness writes into the conversation has to name a
    tool the request carries.  **`attach_image` is the same arrangement for the
    other half of a message** — a turn's pictures — and it is the opposite kind
    of tool in one respect: the model calls it far more often than the harness
    does, because naming a file in prose is what most people write.

    **And the four `model_*` tools are this server's for the same reason they
    are marked for the model**: `default:` and `providers:` are what this
    process turns a conversation's model into, so the edit and the read that
    honours it are one process.  They are listed with the rest because a
    server's tool list is what it serves, not what it recommends.

    **`answer_approval` is the one entry here that a *client* calls rather than
    a model or the harness**: it is how the window answers an `_approve`
    request a turn is parked on, and it is on this server because the waiting
    turn is (`slife2.libs.turn.loop`'s gate, SDD §7.7).

    **`check_async` and `cancel_async` are declared here even though the task
    table is this process's memory**, because the model must reach them and
    every model tool is a catalogue row: the hub asks this server for them, and
    `tool_load.autoload` keeps them loaded like any other.
    """
    async with Client(
        build_server(
            config(), context_client=context, hub_client=hub, backend=FakeBackend()
        )
    ) as client:
        tools = await client.list_tools()
    # Sorted, because the order a server *lists* its tools in is its own
    # business — this is about which names it serves, and what they are.
    assert sorted(t.name for t in tools) == [
        "_check_new_input",
        "answer_approval",
        "attach_image",
        "cancel_async",
        "check_async",
        "model_list",
        "model_remove",
        "model_set",
        "model_switch",
        "reset",
        "send_message",
        "subagent_remove",
        "subagent_set",
        "subagents",
        "transcript",
        "watch",
    ]


@pytest.mark.asyncio
async def test_the_background_tools_are_ordinary_model_tools(context, hub) -> None:
    """**The task table is this process's, and the model still reaches it.**

    `check_async` and `cancel_async` are declared on this server rather than
    injected into the registry, so the hub catalogues them next to every other
    model tool and autoload keeps them loaded.  Their bodies still run here:
    a task id this process never issued is a refusal, not a transport fault.
    """
    async with Client(
        build_server(
            config(), context_client=context, hub_client=hub, backend=FakeBackend()
        )
    ) as client:
        result = await client.call_tool(
            "check_async", {"task_id": "nosuch"}, raise_on_error=False
        )

    assert result.is_error
    assert "no async task 'nosuch'" in str(result.content)


@pytest.mark.asyncio
async def test_a_new_conversation_is_built_from_the_config_as_it_is_now(
    context, hub
) -> None:
    """**The live half of the model config**: a conversation reads the config.

    `main` passes `load_cached`, so an edit — by the `model_*` tools or by hand —
    reaches the *next* conversation with no restart, while one already running
    keeps the model it started on.  The source is a seam here, so the test needs
    no file on disk: it answers one config and then another.
    """
    base = config()
    provider = base.providers["deepseek"]
    other = replace(
        base,
        providers={
            **base.providers,
            "deepseek": replace(
                provider,
                models={**provider.models, "other": ModelSettings(model="other")},
            ),
        },
        default="deepseek/other",
    )
    reads: list[int] = []

    def source():
        reads.append(1)
        return base if len(reads) == 1 else other

    server = build_server(
        base,
        context_client=context,
        hub_client=hub,
        backend=FakeBackend(text_turn("one"), text_turn("two"), text_turn("three")),
        source=source,
    )

    first = await send(server, "hello")
    again = await send(server, "again")
    assert first.data["model"] == "deepseek/deepseek-flash"
    assert again.data["model"] == "deepseek/deepseek-flash", "it keeps its model"
    assert len(reads) == 1, "read once, when the conversation started — not per turn"

    elsewhere = await send(server, "hello", agent="elsewhere")
    assert elsewhere.data["model"] == "deepseek/other", "the next one reads again"
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_a_turn_in_memory_carries_its_own_footnote(context, hub) -> None:
    """The half a rebuild does not cover: the turns *this* process ran.

    `messages_from_turns` annotates everything that came out of the store, so a
    restored conversation's turns are addressable.  The turns added since are in
    no rebuilt list — the loop appended them — so without a second annotation
    the newest turns are the only ones a keep-list cannot name, and "keep
    everything" is the only thing that can be said about them.

    A rebuild that decides to keep everything rebuilds nothing at all, and the
    fixture's discriminator answers `{}` — which *is* that case — so
    `messages_from_turns` is never reached and the annotation is the only thing
    that can be putting a footnote here.  That is what makes this a test of the
    annotation rather than of the rebuild's renderer.
    """
    base = default_config()
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first")),
        ScriptedTurn(result=StreamChatResult(text="second")),
    )
    # Snapshotted when the call is made, not read out of `backend.calls`
    # afterwards: that keeps *references* to the loop's own Message objects, so a
    # later read shows the annotation on a message that had none when it was
    # sent — which is exactly the question this test is asking.
    as_sent: list[list[str]] = []
    inner = backend.stream

    def stream(messages, tools):
        as_sent.append(
            [
                f"{m.role}:{without_prompt(m.content)}"
                for m in messages
                if m.role != "system"
            ]
        )
        return inner(messages, tools)

    backend.stream = stream  # type: ignore[method-assign]
    server = build_server(base, context_client=context, hub_client=hub, backend=backend)

    await send(server, "one", channel="tui")
    await send(server, "two", channel="tui")

    # A turn's own call cannot see its own footnote — it has no id until it is
    # saved, which is after that call — and the call after it must.
    assert as_sent[0] == ["user:one"]
    assert as_sent[1][0].startswith("user:one [TURN:")
    footnote = json.loads(as_sent[1][0].split("[TURN:", 1)[1].rstrip("]"))
    assert footnote["turn_id"] > 0
    assert footnote["channel"] == "tui"
    assert footnote["begin"], "a turn that cannot say when it happened"
    assert as_sent[1][2] == "user:two", "and the newest turn has none yet"

    # ...and the record keeps the user's own words, so a later rebuild derives
    # the same footnote from the row rather than finding a second one in it.
    async with Client(server) as client:
        answer = await client.call_tool("transcript", {"agent": DEFAULT_AGENT})
    stored = answer.data["turns"][0]["messages"][0]["content"]
    assert stored == "one"


@pytest.mark.asyncio
async def test_each_turn_opens_with_its_own_clock_and_context_figure(
    context, hub
) -> None:
    """**The prompt marker is for the turn running, and the row never sees it.**

    `[TURN-PROMPT:{…}]` carries the clock and the status bar's context figure.
    It is added after the recall and removed before the save, so the first call
    sees it on the current message, the next call sees the first turn as the row
    holds it — words and the `[TURN:]` footnote, no prompt marker — and the
    stored rows are the person's words only.  That is also what keeps it out of
    search, recall and a later rebuild.
    """
    backend = FakeBackend(
        ScriptedTurn(
            result=StreamChatResult(
                text="first",
                usage=Usage(prompt_tokens=140_000, completion_tokens=127),
            )
        ),
        ScriptedTurn(result=StreamChatResult(text="second")),
    )
    # **Copied at call time**, because the marker is removed from the live
    # message before the turn is saved: a later read of `backend.calls` sees the
    # cleaned message, which is the row's shape and not what the model read.
    as_sent: list[list[str]] = []
    inner = backend.stream

    def stream(messages, tools):
        as_sent.append([str(m.content) for m in messages if m.role == "user"])
        return inner(messages, tools)

    backend.stream = stream  # type: ignore[method-assign]
    server = build_server(
        default_config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one")
    await send(server, "two")

    first = as_sent[0][0]
    assert first.startswith(TURN_PROMPT_PREFIX)
    end = first.index("] ")
    facts = json.loads(first[len(TURN_PROMPT_PREFIX) : end])
    assert datetime.fromisoformat(facts["now"])  # a real clock reading
    assert facts["token-usage"] == "0 (0.0%)", "no turn had run before this one"
    assert first[end + 2 :] == "one"

    previous, current = as_sent[1]
    assert TURN_PROMPT_PREFIX not in previous
    assert previous.startswith("one [TURN:")
    assert current.startswith(TURN_PROMPT_PREFIX)
    end = current.index("] ")
    second = json.loads(current[len(TURN_PROMPT_PREFIX) : end])
    assert second["token-usage"] == "140,127 (14.0%)", "the status bar's figure"
    assert current[end + 2 :] == "two"

    # ...and the record holds no marker.
    async with Client(server) as client:
        answer = await client.call_tool("transcript", {"agent": DEFAULT_AGENT})
    stored = [turn["messages"][0]["content"] for turn in answer.data["turns"]]
    assert stored == ["one", "two"]


@pytest.mark.asyncio
async def test_a_subagents_turn_in_memory_carries_its_own_footnote(
    context, hub
) -> None:
    """The maker's annotation, in the subagent's own conversation.

    A subagent owns its conversation, and its turns are written to its own
    store — so the newest turn in its live list has to be as addressable as its
    maker's.  Without the footnote the discriminator cannot name the turn it was
    just given, and a keep-list silently drops it.  The second half of the key
    is the half that used to decide this; the annotation must not read it.
    """
    base = default_config()
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first")),
        ScriptedTurn(result=StreamChatResult(text="second")),
    )
    # Snapshotted when the call is made, for the reason the maker's test gives:
    # `backend.calls` holds the loop's own Message objects, and the annotation
    # that arrives after the call would otherwise be read back into it.
    as_sent: list[list[str]] = []
    raw: list[list[str]] = []
    inner = backend.stream

    def stream(messages, tools):
        as_sent.append(
            [
                f"{m.role}:{without_prompt(m.content)}"
                for m in messages
                if m.role != "system"
            ]
        )
        raw.append([f"{m.role}:{m.content}" for m in messages if m.role != "system"])
        return inner(messages, tools)

    backend.stream = stream  # type: ignore[method-assign]
    server = build_server(base, context_client=context, hub_client=hub, backend=backend)
    inbound = f"{LOCAL_AGENT.channel}:{DEFAULT_AGENT}"

    await send(server, "one", subagent="researcher", channel=inbound)
    await send(server, "two", subagent="researcher", channel=inbound)

    # A subagent gets the same per-turn prompt its maker does, and it is gone
    # from the turn the next call sees.
    assert raw[0][0].startswith(f"user:{TURN_PROMPT_PREFIX}")
    assert TURN_PROMPT_PREFIX not in raw[1][0]
    # A turn's own call cannot see its own footnote — it has no id until it is
    # saved, which is after that call — and the call after it must.
    assert as_sent[0] == ["user:one"]
    assert as_sent[1][0].startswith("user:one [TURN:")
    footnote = json.loads(as_sent[1][0].split("[TURN:", 1)[1].rstrip("]"))
    assert footnote["turn_id"] > 0
    assert footnote["channel"] == inbound
    assert footnote["begin"], "a turn that cannot say when it happened"
    assert as_sent[1][2] == "user:two", "and the newest turn has none yet"


@pytest.mark.asyncio
async def test_a_window_can_read_the_conversation_it_left(context, hub) -> None:
    """What a terminal asks for when it opens, and the answer it needs.

    The *context* is restored by the turn path; this is the other reader of the
    same stored list — the screen, which needs the turns rather than the message
    list, because when a line was said and which turn it belongs to are the
    record's facts and not the model's.

    A read, and provably one: the turn ran before it, and nothing about the
    answer moved.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=answering("42")
    )
    await send(server, "what is 6*7?")

    async with Client(server) as client:
        answer = await client.call_tool("transcript", {"agent": DEFAULT_AGENT})
        # ...and again, to prove it is a read rather than something that
        # consumed what it answered with.
        again = await client.call_tool("transcript", {"agent": DEFAULT_AGENT})

    turns = answer.data["turns"]
    assert len(turns) == 1
    assert turns[0]["messages"][0]["role"] == "user"
    assert turns[0]["messages"][0]["content"] == "what is 6*7?"
    assert turns[0]["created_at"], "a turn that cannot say when it happened"
    assert again.data["turns"] == turns


@pytest.mark.asyncio
async def test_a_window_that_has_never_run_reads_nothing(context, hub) -> None:
    """Not an error, and not a heading: a new name has no past."""
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=FakeBackend()
    )
    async with Client(server) as client:
        answer = await client.call_tool("transcript", {"agent": "nobody"})

    assert answer.data["turns"] == []


class UnreadableStore:
    """A context plugin answering with the shapes this build refuses.

    `_adopt`, `rebuild_into` and `transcript` read the store's answers as things
    they must be able to trust, and a store this build started never returns one
    it cannot read — so a test of the refusals hands in the shape a *daemon from
    another build* does, by name.
    """

    def __init__(self, **answers: dict[str, Any]) -> None:
        self.answers = answers

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return SimpleNamespace(data=self.answers[name])


class ParkedRestore:
    """The real context store, with `restore` answering only when told to.

    The window `adopt_maker` walks into is one store round trip wide, and a test
    that hoped to land inside it would pass for the wrong reason whichever way it
    went — so the round trip is held open instead, and *when* each of the two
    restores lands is a fact.  `entered` counts them, `arrived` is set by the
    first, and each waits on its own event of `release`.

    Real underneath rather than a stub: what is under test is which loop's
    `restore` gets to replace which list, and a store answering a made-up list
    would say nothing about that.
    """

    def __init__(self, inner: Client) -> None:
        self.inner = inner
        self.entered = 0
        self.arrived = asyncio.Event()
        self.release = [asyncio.Event(), asyncio.Event()]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "restore":
            mine = self.entered
            self.entered += 1
            self.arrived.set()
            await self.release[mine].wait()
        return await self.inner.call_tool(name, arguments)


@pytest.mark.asyncio
async def test_a_store_answer_without_turn_ids_is_refused_rather_than_adopted(
    hub,
) -> None:
    """`turn_ids` was tolerated where `messages` beside it was validated.

    So an answer carrying messages and no ids was adopted as a live context the
    loop could name no turn of: the loop and the store disagreed about what the
    context was from that point on, and the next write persisted the wrong list.
    Both halves are one answer and are now read the same way — a missing or null
    `turn_ids` is refused, naming the payload (SDD Appendix C: a value that
    cannot be read is not a value that is absent).
    """
    store = UnreadableStore(restore={"messages": [], "turns": []})
    server = build_server(
        config(), context_client=store, hub_client=hub, backend=FakeBackend()
    )

    with pytest.raises(ToolError, match="turn-id list"):
        await send(server, "hi", agent="jack")


@pytest.mark.asyncio
async def test_a_rebuild_that_carried_no_counts_is_refused_rather_than_drawn_as_zero(
    hub,
) -> None:
    """`kept`/`recalled` were read as `int(payload.get(...) or 0)`.

    A reply that replaced the context list but omitted the two counts drew
    `[kept 0, recalled 0]` — a note reporting a decision nobody made, since the
    discriminator *was* paid and did decide.  The answer is refused the way a
    shape `_adopt` cannot read is, naming the payload (SDD Appendix C: a
    failure is not a verdict).
    """
    store = UnreadableStore(
        restore={"messages": [], "turn_ids": [], "turns": []},
        rebuild={"messages": [], "turn_ids": []},
    )
    server = build_server(
        config(), context_client=store, hub_client=hub, backend=answering("x")
    )

    with pytest.raises(ToolError, match="counts"):
        await send(server, "hi", agent="jack")


@pytest.mark.asyncio
async def test_a_transcript_read_of_an_unreadable_turn_list_is_refused(hub) -> None:
    """A `turns` that is not a list became `[]`.

    A context plugin answering with a shape this build cannot read drew an empty
    conversation on a screen and said nothing — the same deformation `_adopt`
    refuses, one reader over.  The shape is named and the caller is pointed at
    `slife2 down`, exactly as the adopting path does.
    """
    store = UnreadableStore(
        restore={"messages": [], "turn_ids": [], "turns": "not a list"}
    )
    server = build_server(
        config(), context_client=store, hub_client=hub, backend=FakeBackend()
    )

    async with Client(server) as client:
        with pytest.raises(ToolError, match="turn list"):
            await client.call_tool("transcript", {"agent": "jack"})


@pytest.mark.asyncio
async def test_a_turn_returns_the_final_text(context, hub) -> None:
    """The result, and this call is the *no-token* case of it.

    `send` passes no progress handler, so this is also the half of the contract
    that a non-streaming client depends on: the observer's `report_progress` is a
    silent no-op without a token, and the answer arrives anyway.  This test and
    `test_a_turn_works_without_a_progress_handler` used to be two: same server,
    same call, same assertion, and the second's stated subject — "the server does
    no extra work" — asserted by nothing, because a `send` without a handler is
    what both of them already were.  The pair that claim needs is this one and
    `test_events_arrive_as_progress_notifications`, which sends *with* a handler
    and reads the events out.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=tool_then_answer()
    )
    result = await send(server, "what is 6*7?")
    assert result.data["text"] == "It is 42."


@pytest.mark.asyncio
async def test_a_turn_leaves_one_line_saying_what_it_cost(
    context, hub, caplog: pytest.LogCaptureFixture
) -> None:
    """**The operator's index into a session.**

    Before this line the logs had no turn boundaries at all: a model call was
    logged one process over, a stored turn one process over, and a tool only
    when it failed.  One line per ending — success, failure or cancellation —
    with the facts a reader needs to find the transcript row and the calls.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=tool_then_answer()
    )
    with caplog.at_level("INFO", logger="slife2.plugins.agent.server"):
        await send(server, "what is 6*7?")

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("turn_done ")
    ]
    assert len(lines) == 1, caplog.text
    line = lines[0]
    assert f"agent={DEFAULT_AGENT}" in line
    assert "turn=" in line, "an id is written when the record landed"
    assert "stop=stop" in line
    assert "iterations=2" in line
    assert "elapsed=" in line


@pytest.mark.asyncio
async def test_a_tool_call_is_logged_when_it_succeeds(
    context, hub, caplog: pytest.LogCaptureFixture
) -> None:
    """The successful call used to leave no trace at all.

    The only tool lines in the 2026-10 corpus were warnings about failures, so
    "what did this turn actually do" had no answer in any log.  The line names
    the tool and its duration; the arguments stay out of it.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=tool_then_answer()
    )
    with caplog.at_level("INFO", logger="slife2.libs.turn.loop"):
        await send(server, "what is 6*7?")

    said = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("tool_call name=skill_list ") for message in said), (
        caplog.text
    )


@pytest.mark.asyncio
async def test_a_turn_reports_its_shape(context, hub) -> None:
    """What a caller needs to render and to bill — and no history.

    `new_messages` is deliberately absent: it was the caller's half of owning
    the conversation, and the loop owns it now.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=tool_then_answer()
    )
    result = await send(server, "what is 6*7?")

    assert result.data["iterations"] == 2
    assert result.data["stop_reason"] == "stop"
    assert "usage" in result.data
    assert "new_messages" not in result.data


@pytest.mark.asyncio
async def test_a_turn_says_what_answered(context, hub) -> None:
    """A bare provider or an empty reference does not reveal the model."""
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=answering("ok")
    )
    result = await send(server, "hi", agent="jack")
    assert result.data["model"] == default_config().default


# --- a loop is the conversation ---------------------------------------------


@pytest.mark.asyncio
async def test_a_loop_remembers_what_was_said_to_it(context, hub) -> None:
    """The inversion of the property this server used to be built on.

    Two messages on one loop, and the second model call sees the first exchange.
    Under the old stateless design the second call would have seen only its own
    prompt, because the caller had to send the history back.
    """
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first")),
        ScriptedTurn(result=StreamChatResult(text="second")),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one")
    await send(server, "two")

    assert prompts_seen(backend, 0) == ["user:one"]
    assert prompts_seen(backend, 1) == [
        "user:one",
        "assistant:first",
        "user:two",
    ]


@pytest.mark.asyncio
async def test_one_server_serves_two_loops_without_mixing_them(context, hub) -> None:
    """Two loops, two agents, one agent server, no cross-talk.

    Asserted concurrently on purpose — sequentially it would pass even if the
    loops were the same one, because the second turn would simply run after the
    first.  This is the property that lets one agent server serve every
    instance, and it is the same claim the stateless version made: what changed
    is where the history lives, not whether two agents can share a process.
    """

    class EchoBackend:
        """Answers with the prompt it was given, so mixing is detectable."""

        name = "echo"

        def stream(self, messages, tools):
            # The per-turn prompt marker is not part of what this test is
            # echoing: it is about a prompt reaching the right loop, and the
            # clock would make every reply differ for a reason that is not mixing.
            prompt = without_prompt(messages[-1].content or "")

            async def chunks():
                yield Chunk(text=f"reply to {prompt}")

            async def result():
                return StreamChatResult(text=f"reply to {prompt}")

            return Stream(chunks=chunks(), result=result())

    server = build_server(
        config(), context_client=context, hub_client=hub, backend=EchoBackend()
    )
    async with Client(server) as first, Client(server) as second:
        one, two = await asyncio.gather(
            first.call_tool("send_message", {"agent": "jack", "prompt": "alpha"}),
            second.call_tool("send_message", {"agent": "jill", "prompt": "beta"}),
        )

    assert one.data["text"] == "reply to alpha"
    assert two.data["text"] == "reply to beta"
    # ...and neither answer contains the other's prompt.
    assert "beta" not in one.data["text"]
    assert "alpha" not in two.data["text"]


@pytest.mark.asyncio
async def test_a_subagent_is_a_conversation_of_its_own(context, hub) -> None:
    """The second half of the id separates conversations, not just agents.

    Two clients that differ only in `subagent` must not see each other.  That is
    what lets a worker be a worker, and it falls out of the key rather than out
    of a rule somebody has to remember to apply.
    """
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="main")),
        ScriptedTurn(result=StreamChatResult(text="worker")),
        ScriptedTurn(result=StreamChatResult(text="main again")),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one", agent="jack")
    await send(server, "two", agent="jack", subagent="helper")

    # The worker was not told what the agent's own conversation said...
    assert prompts_seen(backend, 1) == ["user:two"]
    # ...nor the other way round, on the agent's next turn.
    await send(server, "three", agent="jack")
    assert prompts_seen(backend, 2) == [
        "user:one",
        "assistant:main",
        "user:three",
    ]


# --- an unknown loop ---------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_starts_the_conversation_over(context, hub) -> None:
    """The one lifecycle verb left, and it is the caller's to ask for.

    Idempotent, because a caller that had nothing to forget is already in the
    state it asked for.
    """
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first")),
        ScriptedTurn(result=StreamChatResult(text="second")),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one")
    assert (await reset(server)).data["reset"]
    # Nothing to forget the second time, and not an error.
    assert not (await reset(server)).data["reset"]

    await send(server, "two")
    # The new conversation opened with its system prompt and nothing else.
    assert prompts_seen(backend, 1) == ["user:two"]


@pytest.mark.asyncio
async def test_an_idle_conversation_simply_starts_again(
    context, monkeypatch, hub
) -> None:
    """The idle sweep reclaims memory, and the conversation comes back whole.

    It used to be what made an id go stale, which meant every caller needed a
    path for "your conversation is gone".  A key cannot go stale, so the sweep is
    invisible — and since the context is *stored* rather than only held, the
    second call restoring what the first one recorded is what makes "the sweep
    only reclaims memory" true rather than merely reassuring.  The turn that was
    reaped is in the prompt the next one sends, which is the observable form of
    that.
    """
    monkeypatch.setattr(_T.timeouts.pacing, "loop_idle", 0.0)
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first")),
        ScriptedTurn(result=StreamChatResult(text="second")),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one")
    await send(server, "two")

    # Swept in between, so the second call began a conversation rather than
    # continuing one — and what it began with is what the first one recorded:
    # the reaped turn is in the prompt, which is the whole claim that the sweep
    # only ever reclaimed memory.
    seen = prompts_seen(backend, 1)
    assert seen[0].startswith("user:one"), seen
    assert seen[1] == "assistant:first"
    assert seen[-1] == "user:two"


# --- the inbox ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_sent_to_a_busy_loop_waits_and_then_runs(context, hub) -> None:
    """A message is never dropped and never displaces the turn it arrives in.

    A second message arrives while the first turn is still streaming.  Under the
    TUI's old policy it would have *cancelled* the first turn and thrown it
    away; here it waits, runs in its turn, and both answers come back.

    **This is the turn with nothing to cut into**, and it is the reason both
    halves are needed.  The first turn here is one iteration: the model answered and
    asked for no tools, so the loop made one call and returned, and there was no
    later boundary for the arrival to be delivered at.  It keeps its place and
    its own turn.  `test_a_message_sent_to_a_busy_loop_is_folded_into_the_turn`
    is the other half, on a turn that does take a second iteration.

    What the second assertion still catches is the mistake the pairing exists to
    prevent: the arrival must not reach the model as *this turn's* input.  A
    message appended to the list as a user message on arrival would be read
    mid-turn — steering nobody asked for, arriving silently, and in the middle of
    an unanswered tool exchange a 400 from several providers.
    """
    backend = FakeBackend(
        text_turn(
            "slow answer",
            delay=0.15,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn("second answer"),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    first = asyncio.create_task(send(server, "one"))
    # The first turn has begun only once the backend has been asked for a
    # stream; anything earlier and the second message might win the race for
    # the lock and this would assert nothing.
    await wait_for_streams(backend)

    second = asyncio.create_task(send(server, "two"))
    await asyncio.sleep(0.05)
    assert not second.done(), "the second message did not wait for the first"

    one, two = await asyncio.gather(first, second)
    assert one.data["text"] == "slow answer"
    assert two.data["text"] == "second answer"

    # The running turn never saw the queued message.
    assert all("two" not in prompt for prompt in prompts_seen(backend, 0))
    # ...and the second turn saw the first exchange plus its own message.
    assert prompts_seen(backend, 1) == [
        "user:one",
        "assistant:slow answer",
        "user:two",
    ]


def wired(
    backend: FakeBackend, context: Client, config: Any = None
) -> tuple[Client, FastMCP]:
    """A hub and an agent that can reach each other, which takes a holder.

    **The circle is real and it is the feature.**  The pair the cut-in writes has
    to name a tool the model's request carries, so `_check_new_input` reaches the
    model the way every other tool does — through the hub — which means the hub
    asks the *agent* for a tool list, while the agent asks the hub for the same
    list.  Neither can be built first.

    So the hub is built with a transport that resolves the agent later, and the
    agent is built pointing at a hub client that has not been entered yet.  A
    transport is only *called* when the hub starts, which the caller does once
    both objects exist — and `plugin_transports`'s agent entry is a `blank_plugin`
    otherwise, which would leave the loop with a registry that has no such tool
    and the pair naming one the model was never offered.

    **`config` is for a test that needs a tool on the model's list**: what starts
    loaded is `tool_load.autoload`, and a test whose scripted model calls a
    plugin's tool has to put its name there.  Left out, the default is the
    required set — which the cut-in's own pair works with.
    """
    from slife2.plugins.toolhub.server import build_server as build_hub
    from tests.fakes import plugin_transports

    config = config or default_config()
    holder: dict[str, Any] = {}
    transports = plugin_transports(config)
    transports["agent"] = lambda _settings: holder["agent"]
    hub_server = build_hub(config, transports=transports, embedder=StubEmbedder())
    hub_client = Client(hub_server)
    server = build_server(
        config,
        context_client=context,
        hub_client=hub_client,
        backend=backend,
    )
    holder["agent"] = server
    return hub_client, server


@pytest.mark.asyncio
async def test_a_message_sent_to_a_busy_loop_is_folded_into_the_running_turn(
    context,
) -> None:
    """The cut-in: the arrival is handed to the turn that is already running.

    The first turn here takes two iterations — the model asks for `skill_list` and is told
    — so there *is* a boundary after the message arrives, and at it the message
    is delivered as a call to `_check_new_input` and answered with its own words.
    The model reads it as something that arrived mid-turn, which is what it is.

    Its sender is answered by that turn rather than by one of its own: a sender
    here is a call parked on the lock expecting `{text, usage, iterations}`, not
    a channel that can give up.
    It gets the answer the message was actually given, and `injected` says so —
    without which a caller could not tell this from a turn of its own that
    happened to say the same thing.
    """
    backend = FakeBackend(
        asks_for_a_tool(
            delay=0.1  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn("It is 42, and yes to that too."),
        # Scripted so that a second turn *can* run: if the message were not
        # folded in, this test would fail on the assertion below rather than on
        # the backend running out of turns.
        text_turn("second answer"),
    )
    hub_client, server = wired(backend, context)

    async with hub_client:
        first = asyncio.create_task(send(server, "one"))
        await wait_for_streams(backend)
        second = asyncio.create_task(send(server, "two"))
        await asyncio.sleep(0.05)
        one, two = await asyncio.gather(first, second)

    # The turn answered both, and the answer is the same answer.
    assert one.data["text"] == "It is 42, and yes to that too."
    assert one.data["injected"] is False
    assert two.data["text"] == one.data["text"]
    assert two.data["injected"] is True
    # **The same key set, `refused` included.**  The folded-in reply used to be
    # built by a second copy of the dict that had drifted and had no `refused`,
    # so a caller that read `reply["refused"]` got a `KeyError` on exactly this
    # path — the one path a caller cannot recognise by anything else in the
    # answer.  A caller must not have to know which path answered it to read a
    # field of the reply.
    assert one.data.keys() == two.data.keys()
    assert two.data["refused"] == []

    # Delivered at the boundary, as a call the model reads, not as its own input.
    assert "tool:two" in prompts_seen(backend, 1)
    assert "user:two" not in prompts_seen(backend, 1)
    # ...and it never became a turn of its own: two model calls, one turn.
    assert len(backend.calls) == 2


@pytest.mark.asyncio
async def test_a_cut_in_says_where_it_came_from_and_which_submission_it_drew(
    context,
) -> None:
    """The event is the display's one account of a message that jumped the queue.

    It carries the words — the tool's own answer — the channel the message
    arrived on, and the receipt its submitter chose.  The window needs all three
    and can derive none of them: it has no event of its own for a folded message,
    and matching by the words would confuse two people who said the same thing.
    """
    backend = FakeBackend(
        asks_for_a_tool(
            delay=0.1  # timeouts: the value this test is built around, not a budget
        ),
        text_turn("answered together"),
    )
    hub_client, server = wired(backend, context)
    seen: list[TurnEvent] = []

    async def on_progress(progress, total, message):
        event = decode(message or "")
        if event is not None:
            seen.append(event)

    async with hub_client:
        async with Client(server) as first_client:
            first = asyncio.create_task(
                first_client.call_tool(
                    "send_message",
                    {"agent": DEFAULT_AGENT, "prompt": "one"},
                    progress_handler=on_progress,
                )
            )
            await wait_for_streams(backend)
            second = asyncio.create_task(
                send(
                    server,
                    "two",
                    channel="wechat",
                    receipt="window-7",
                )
            )
            await asyncio.sleep(0.05)
            await asyncio.gather(first, second)

    cut_ins = [event for event in seen if isinstance(event, MessageCutIn)]
    assert cut_ins == [
        MessageCutIn(text="two", channel="wechat", receipt="window-7"),
    ]


@pytest.mark.asyncio
async def test_a_turn_that_failed_leaves_the_folded_message_its_own_turn(
    context,
) -> None:
    """A turn that produced no answer answered for nothing it was handed.

    The absorbing turn raises here — a provider 500 on its second call — so
    there is no repair (only a cancellation truncates) and the pair stays in that
    turn's record.  What must not follow is the folded message being counted as
    answered: its `result` is written only by a turn that produced one, so its
    sender runs its own turn and gets its own answer.

    This is the case that decides how the handoff is asked.  A design that
    inferred "it was absorbed" from the message having left the queue would see
    the empty `result` and answer this caller out of whatever the last turn
    happened to leave behind — a wrong answer rather than a missing one, and
    nothing downstream could tell.
    """

    class FailsOnTheSecondCall(FakeBackend):
        """A provider that answers once and then dies, the way a 500 does."""

        def stream(self, messages, tools) -> Stream:
            if len(self.calls) == 1:
                self.calls.append((list(messages), list(tools)))
                raise RuntimeError("the provider answered 500")
            return super().stream(messages, tools)

    backend = FailsOnTheSecondCall(
        asks_for_a_tool(
            delay=0.1  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn("second answer"),
    )
    hub_client, server = wired(backend, context)

    async with hub_client:
        first = asyncio.create_task(send(server, "one"))
        await wait_for_streams(backend)
        second = asyncio.create_task(send(server, "two"))
        await asyncio.sleep(0.05)
        with pytest.raises(Exception, match="500"):
            await first
        answer = await second

    assert answer.data["injected"] is False
    assert answer.data["text"] == "second answer"


@pytest.mark.asyncio
async def test_a_cancelled_message_leaves_the_lock_free(context, hub) -> None:
    """A caller that goes away while queued is not a lock held forever."""
    backend = FakeBackend(
        text_turn(
            "slow",
            delay=0.2,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn(
            "next"
        ),  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    first = asyncio.create_task(send(server, "one"))
    await wait_for_streams(backend)

    queued = asyncio.create_task(send(server, "doomed"))
    await asyncio.sleep(0.02)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    # The one that was already running is untouched, and the loop still works.
    result = await first
    assert result.data["text"] == "slow"
    assert (await send(server, "after")).data["text"] == "next"


# --- cancellation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_interrupted_stream_leaves_only_the_users_message(
    context, hub
) -> None:
    """The stream case: nothing had been written, so only the question remains.

    The loop appends a response only after its stream completes, so an
    interruption that lands in the stream has no work to keep.  What survives is
    the user's message — losing it is the failure this whole arrangement exists
    to prevent — and the next turn is sent that and its own message.  **It also
    carries its `[TURN:…]`**: the write is taken up by `settle_record`, so an
    interrupted turn is addressable like any other.  The batch case, where there
    *is* work to keep, is `test_an_interrupted_turn_keeps_the_tool_batch_it_finished`.

    Asserted through the next turn rather than by reaching inside: what the
    model is sent is the only thing that actually matters.
    """
    backend = FakeBackend(
        text_turn(
            "never finished",
            delay=0.4,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn(
            "fine"
        ),  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    interrupted = asyncio.create_task(send(server, "one"))
    await wait_for_streams(backend)
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted

    await send(server, "two")
    assert prompts_seen(backend, 1) == ["user:one", "user:two"]
    raw = [m for m in backend.calls[1][0] if m.role == "user"][0]
    assert str(raw.content).startswith("one [TURN:"), (
        "the interrupted turn is addressable"
    )


@pytest.mark.asyncio
async def test_an_interrupted_turn_keeps_the_tool_batch_it_finished(
    context, hub
) -> None:
    """What the turn completed survives, and the next "continue" runs on it.

    The first iteration asks for a tool and is answered; the interruption lands
    in the second iteration's stream.  The exchange the turn had finished is
    kept — in the live list, in the next model call, and in the row — which is
    what makes a follow-up resume instead of restarting from the question.
    """
    backend = FakeBackend(
        asks_for_a_tool(),
        text_turn(
            "never finished",
            delay=0.4,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn("continued"),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    interrupted = asyncio.create_task(send(server, "one"))
    await wait_for_streams(backend, 2)
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted
    # The row is written on the detached path; give it a moment before the
    # transcript read below, as the other interruption tests do.
    await asyncio.sleep(0.5)

    await send(server, "continue")

    sent = [m for m in backend.calls[2][0] if m.role != "system"]
    assert [m.role for m in sent] == ["user", "assistant", "tool", "user"]
    assert sent[1].content == "checking"
    assert sent[1].tool_calls[0].id == "c1"
    assert sent[1].tool_calls[0].name == "skill_list"
    assert sent[2].tool_call_id == "c1"
    assert sent[2].content and sent[2].content != INTERRUPTED, (
        "the call answered before the interruption, so its result is the real one"
    )

    # And the record is what the conversation resumes from after a restart.
    async with Client(server) as client:
        answer = await client.call_tool("transcript", {"agent": DEFAULT_AGENT})
    first = answer.data["turns"][0]
    assert [m["role"] for m in first["messages"]] == ["user", "assistant", "tool"]


@pytest.mark.asyncio
async def test_an_interrupted_turn_is_still_recorded(
    context, hub, tmp_path, monkeypatch
) -> None:
    """Recording is unconditional, so the cancel path is not a special case.

    A turn that happened gets a row, and a cancelled one is the shape of a
    question with no answer — which is what it was.  The alternative is a
    message the user can see, the model will see, and the database has never
    heard of.
    """
    from slife2.libs.host.paths import DATA_ENV_VAR, agent_dir
    from slife2.plugins.context.server import build_server as build_context

    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    backend = FakeBackend(
        text_turn(
            "never finished",
            delay=0.4,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget
    async with Client(
        build_context(config(), embedder=StubEmbedder(), ask=keep_the_context)
    ) as context_client:
        server = build_server(
            config(), backend=backend, context_client=context_client, hub_client=hub
        )
        interrupted = asyncio.create_task(send(server, "one"))
        await wait_for_streams(backend)
        interrupted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await interrupted
        # The write is detached from the cancelled request, so give it a moment
        # to land rather than racing it.
        await asyncio.sleep(0.5)

    rows = (
        sqlite3.connect(agent_dir(DEFAULT_AGENT) / "turns.db")
        .execute("SELECT messages FROM turn ORDER BY rowid")
        .fetchall()
    )
    assert len(rows) == 1, "the cancelled turn was not recorded"
    stored = json.loads(rows[0][0])
    assert [m["role"] for m in stored] == ["user"]
    assert stored[0]["content"] == "one"


@pytest.mark.asyncio
async def test_a_cancelled_turn_records_the_calls_that_completed(
    context, hub, caplog: pytest.LogCaptureFixture
) -> None:
    """A cancellation is not a refund.

    The turn's first model call answered and its tool ran before the second
    call was cut off.  Those tokens are on the bill, so the row and the
    `turn_done` line carry them — reporting zero was the one path where a
    timeout-cancelled turn cost nothing on paper while costing the provider
    everything it had answered.
    """
    from slife2.libs.host.paths import agent_dir

    spent = Usage(
        prompt_tokens=90, completion_tokens=10, cache_read=70, cache_creation=5
    )
    backend = FakeBackend(
        asks_for_a_tool(usage=spent),
        text_turn(
            "never finished",
            delay=0.4,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    with caplog.at_level("INFO", logger="slife2.plugins.agent.server"):
        interrupted = asyncio.create_task(send(server, "one"))
        # The first call answered and its tool ran; the cut lands in the second
        # call's stream.
        await wait_for_streams(backend, 2)
        interrupted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await interrupted
        # The row is written by a detached task; give it a moment to land.
        await asyncio.sleep(0.5)

    record = TurnStore(agent_dir(DEFAULT_AGENT) / "turns.db").turn(1)
    assert record is not None
    assert record.token_count == spent.total_tokens
    assert record.context_tokens == spent.total_tokens
    assert record.usage == spent

    lines = [
        record_.getMessage()
        for record_ in caplog.records
        if record_.getMessage().startswith("turn_done ")
    ]
    assert any(
        "stop=cancelled" in line
        and f"cache_read={spent.cache_read}" in line
        and f"cache_creation={spent.cache_creation}" in line
        for line in lines
    ), caplog.text


@pytest.mark.asyncio(loop_scope="function")
async def test_a_turn_whose_model_call_failed_is_still_recorded(
    hub, tmp_path, monkeypatch
) -> None:
    """A failure is a way a turn ends, not a reason to lose it.

    The cancel path is the case the rule was written for, and it is not the
    only one: a provider that answers a 500 has already appended the user's
    message to the conversation, so the next turn sends it and the transcript
    shows it — which makes a missing row the same "conversation the database
    has never heard of" the rule exists to prevent.  Recording is therefore
    keyed on the turn having happened, not on how it ended.
    """
    from slife2.libs.host.paths import DATA_ENV_VAR, agent_dir
    from slife2.plugins.context.server import build_server as build_context

    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))

    class Refusing(FakeBackend):
        """A model call that fails the way a provider's error does."""

        def stream(self, messages, tools) -> Stream:
            self.calls.append((list(messages), list(tools)))
            raise RuntimeError("the provider answered 500")

    async with Client(
        build_context(config(), embedder=StubEmbedder(), ask=keep_the_context)
    ) as context_client:
        server = build_server(
            config(), backend=Refusing(), context_client=context_client, hub_client=hub
        )
        with pytest.raises(Exception, match="500"):
            await send(server, "one")
        # The write is detached so that a store which is also gone cannot
        # replace the failure the caller needs to see; give it a moment.
        await asyncio.sleep(0.5)

    rows = (
        sqlite3.connect(agent_dir(DEFAULT_AGENT) / "turns.db")
        .execute("SELECT messages FROM turn ORDER BY rowid")
        .fetchall()
    )
    assert len(rows) == 1, "the failed turn was not recorded"
    stored = json.loads(rows[0][0])
    assert [m["role"] for m in stored] == ["user"]
    assert stored[0]["content"] == "one"


@pytest.mark.asyncio
async def test_a_turn_that_follows_an_unrecorded_one_keeps_it_in_the_context(
    hub,
) -> None:
    """**The turn after a failed one takes up the failed turn's id.**

    A turn whose `record` could not be waited for — a failed turn, whose write is
    detached because a cancelled handler cannot await — is left unbacked for one
    turn, and the next rebuild carries its messages verbatim.  That is only
    expressible while the unbacked run is still at the end of the list: a
    *later* turn that does get an id sits behind messages its id does not
    describe, and `covered` can no longer be read as a prefix.  Reading
    `len(messages) - covered` off the wrong end then took the newest turn's own
    tail, re-appended it *after* a list the log had just rebuilt it into, and
    silently dropped the message the mechanism exists to keep.

    **And the id has to be picked up, or the run never becomes backed at all.**
    Left unclaimed, `covered` can never advance again — the guard a turn joins
    `turn_ids` by is never true once a carried run exists, and a rebuild
    re-appends that run at the end of a list `covered` does not count — so every
    later turn is unaddressable *and* recallable: the store holds its row, and a
    recall that names it renders that turn from the row and re-appends the same
    messages verbatim.  The exchange then appears twice in the list the turn runs
    on, which is what the recall below is here to catch: it names the recorded
    turn, and the same conversation is asserted to come out of it once.

    Asserted through the model, which is the only reader that matters: the third
    turn is sent the whole conversation, in order, once.
    """
    from slife2.plugins.context.server import build_server as build_context

    class RefusesOnce(FakeBackend):
        """A provider that answers a 500 to the first call and works after it."""

        def stream(self, messages, tools) -> Stream:
            if not self.calls:
                self.calls.append((list(messages), list(tools)))
                raise RuntimeError("the provider answered 500")
            return super().stream(messages, tools)

    backend = RefusesOnce(text_turn("second answer"), text_turn("third answer"))
    answers = iter(["{}", "{}", '{"recall": {"query": "one"}}'])

    async def ask(agent, subagent, model, messages, prompt) -> str:
        return next(answers)

    async with Client(
        build_context(config(), embedder=StubEmbedder(), ask=ask)
    ) as context_client:
        server = build_server(
            config(), context_client=context_client, hub_client=hub, backend=backend
        )
        with pytest.raises(Exception, match="500"):
            await send(server, "one")
        assert (await send(server, "two")).data["text"] == "second answer"
        assert (await send(server, "three")).data["text"] == "third answer"

    assert prompts_seen(backend, 2) == [
        "user:one",
        "user:two",
        "assistant:second answer",
        "user:three",
    ], "the unanswered message is still in the conversation, and nothing is twice"


@pytest.mark.asyncio
async def test_a_failed_turn_is_annotated_when_the_next_turn_settles_its_id(
    context, hub
) -> None:
    """A failed turn's id and its footnote are written by the same step.

    The write of a failed turn is detached, so the turn has no id when it ends
    and `annotate_turn` cannot run there.  The next turn waits for the write
    (`settle_record`) and is the first moment the id exists — so that is where
    the footnote belongs too.  An id that joins `turn_ids` without one is a turn
    the discriminator can never name in a keep-list, which is the whole reason
    the footnote exists.

    The spelling is checked against the row, not merely for being present: the
    live footnote and the one a rebuild derives have to agree to the character,
    or every rebuild costs a prompt-cache miss.
    """

    class RefusesOnce(FakeBackend):
        """A provider that answers a 500 to the first call and works after it."""

        def stream(self, messages, tools) -> Stream:
            if not self.calls:
                self.calls.append((list(messages), list(tools)))
                raise RuntimeError("the provider answered 500")
            return super().stream(messages, tools)

    backend = RefusesOnce(text_turn("second answer"))
    # Snapshotted when the call is made: `backend.calls` holds the loop's own
    # Message objects, and the second turn's annotation arrives after its call.
    as_sent: list[list[str]] = []
    inner = backend.stream

    def stream(messages, tools):
        as_sent.append(
            [
                f"{m.role}:{without_prompt(m.content)}"
                for m in messages
                if m.role != "system"
            ]
        )
        return inner(messages, tools)

    backend.stream = stream  # type: ignore[method-assign]
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    with pytest.raises(Exception, match="500"):
        await send(server, "one", channel="tui")
    await send(server, "two", channel="tui")

    # The failed turn's own call had no id to carry; the next turn's call is
    # the first one after the write has been waited for.
    assert as_sent[0] == ["user:one"]
    assert as_sent[1][0].startswith("user:one [TURN:")
    assert as_sent[1][1] == "user:two", "and the newest turn has none yet"

    async with Client(server) as client:
        answer = await client.call_tool("transcript", {"agent": DEFAULT_AGENT})
    failed = answer.data["turns"][0]
    expected = turn_footnote(
        failed["turn_id"],
        failed["created_at"],
        failed["completed_at"],
        failed["channel"],
    )
    assert as_sent[1][0] == f"user:one [TURN:{expected}]", (
        "the live footnote is the row's, byte for byte"
    )


@pytest.mark.asyncio
async def test_a_cancelled_trim_keeps_the_completed_turn_it_follows(
    hub, tmp_path, monkeypatch
) -> None:
    """A cancellation *after the answer* is not a reason to lose the answer.

    The trim runs once a turn has already answered — it is post-turn bookkeeping,
    and it suspends on a real call to the hub (`unload_tools`) — so a client
    whose stream closes during it cancels a turn that has *succeeded*.  Obeying
    that cancellation abandons `run_turn_into`'s `finally` before the outcome is
    assigned, and `send_message`'s handler then finds an empty `outcome.messages`:
    it records nothing and re-raises, while the loop still holds the completed
    turn's messages.  That is the row SDD §5.6's rule ("every turn of a loop that
    records is written") exists to guarantee, gone, and the loop's `covered`/
    `turn_ids` left out of step with the list — a completed turn the model will
    be sent again but the log has never heard of.

    Asserted through the record, because that is the half that vanished: the
    completed turn must have its row, and the row must be the turn the model
    gave.  The trim is held open so the cancellation provably lands *after* the
    turn and *inside* the trim rather than during the turn (a different path,
    with tests of its own).  And the loop must come out of it whole — the very
    next turn runs, and is sent the completed turn exactly once — since the other
    face of the same bug is `covered`/`turn_ids` left out of step with the list.
    """
    from slife2.libs.host.paths import DATA_ENV_VAR, agent_dir
    from slife2.plugins.agent import server as server_module
    from slife2.plugins.context.server import build_server as build_context

    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    backend = FakeBackend(text_turn("the answer"), text_turn("second answer"))

    entered = asyncio.Event()
    release = asyncio.Event()
    held = 0

    async def slow_unload(_client):
        """The suspension window the cancellation needs, and nothing unloaded.

        Only the *first* trim is held: the follow-up turn's trim must run to
        completion, or the test would hang on the very thing it is not about.
        """
        nonlocal held
        held += 1
        if held == 1:
            entered.set()
            await release.wait()
        return {"unloaded": [], "text": ""}

    monkeypatch.setattr(server_module, "unload_tools", slow_unload)

    async with Client(
        build_context(config(), embedder=StubEmbedder(), ask=keep_the_context)
    ) as context_client:
        server = build_server(
            config(), backend=backend, context_client=context_client, hub_client=hub
        )
        interrupted = asyncio.create_task(send(server, "one"))
        await wait_for_streams(backend)
        # The model has answered and the turn is now in the trim.  Bounded by a
        # real budget rather than a bare `wait`, so a trim that never started
        # fails the test instead of hanging it.
        await asyncio.wait_for(entered.wait(), timeout=_T.timeouts.work.hub)
        interrupted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await interrupted
        # No sleep to race: the fix settles the turn *before* the cancellation
        # is re-raised, so the row is written by the time `await` above returns.

        # **The loop is still whole.**  The completed turn is in the record *and*
        # still in the conversation, so the next turn is sent it exactly once —
        # the two halves SDD §5.6 requires to agree, and the half a stale `covered`
        # would have broken.
        assert (await send(server, "two")).data["text"] == "second answer"
        assert prompts_seen(backend, 1) == [
            "user:one",
            "assistant:the answer",
            "user:two",
        ]

    rows = (
        sqlite3.connect(agent_dir(DEFAULT_AGENT) / "turns.db")
        .execute("SELECT messages FROM turn ORDER BY rowid")
        .fetchall()
    )
    # The interrupted-trim turn first, then the follow-up — and the first one is
    # the whole completed turn rather than the user's message alone.
    assert len(rows) == 2, "the completed turn was not recorded"
    stored = json.loads(rows[0][0])
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[0]["content"] == "one"
    assert stored[1]["content"] == "the answer"


def test_two_identical_prompts_are_two_places_in_the_queue() -> None:
    """The inbox holds submissions, not values.

    `deque.remove` compares by equality, so a `Pending` that compared by its
    fields would make two callers who sent the same text a single entry: the
    turn that starts takes its own message out, and the removal on the way out
    — a no-op for the entry it already took — then silently takes the place of
    the caller still waiting behind it.  The queue would count fewer waiters
    than it has, and `MAX_QUEUED`, whose whole job is to bound that wait, would
    stop firing when it should.
    """
    first = Pending(prompt="same", images=[], channel="tui")
    second = Pending(prompt="same", images=[], channel="tui")
    queue: deque[Pending] = deque([first, second])

    # What `send_message` does: once when the turn starts, once on the way out.
    with contextlib.suppress(ValueError):
        queue.remove(first)
    with contextlib.suppress(ValueError):
        queue.remove(first)

    assert list(queue) == [second], "the waiting caller lost its place"


@pytest.mark.asyncio(loop_scope="function")
async def test_a_second_session_opens_clients_that_work(tmp_path, monkeypatch) -> None:
    """A client the lifespan closed is also a client it forgot.

    FastMCP runs the lifespan once per client session over the in-memory
    transport — `lifespan`'s own comment says so, and `send` reconnects on
    every call — so a client left in `db_conn`/`hub_conn` after being closed is
    one the next session is handed already shut, and every turn of that session
    fails at its first hop.  No other test here notices, because every other
    one injects its clients rather than letting the server own them.
    """
    from slife2.libs.host.config import CONTEXT_SERVER_NAME, TOOLHUB_SERVER_NAME
    from slife2.libs.host.paths import DATA_ENV_VAR
    from slife2.plugins.agent import server as server_module
    from slife2.plugins.context.server import build_server as build_context
    from slife2.plugins.toolhub.server import build_server as build_hub
    from tests.fakes import plugin_transports

    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    opened: list[str] = []

    async def open_ours(_url, **kwargs):
        name = kwargs.get("name", "")
        opened.append(name)
        if name == CONTEXT_SERVER_NAME:
            client = Client(
                build_context(
                    default_config(), embedder=StubEmbedder(), ask=keep_the_context
                )
            )
        else:
            assert name == TOOLHUB_SERVER_NAME
            client = Client(
                build_hub(
                    default_config(),
                    transports=plugin_transports(default_config()),
                    embedder=StubEmbedder(),
                )
            )
        await client.__aenter__()
        return client

    monkeypatch.setattr(server_module, "open_server", open_ours)
    server = build_server(
        config(), backend=FakeBackend(text_turn("the answer"), text_turn("the answer"))
    )

    assert (await send(server, "one")).data["text"] == "the answer"
    assert (await send(server, "two")).data["text"] == "the answer"
    assert opened.count(CONTEXT_SERVER_NAME) == 2, "a session reused a closed client"


# --- persistence -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_is_written_to_the_db(tmp_path, monkeypatch, hub) -> None:
    """The turn lands in that agent's own database, and nowhere else.

    Both halves matter.  Written at all, because a store that nothing
    calls is a plugin that does nothing; and written to *that agent's* file,
    because isolation between agents is the reason the file is per-agent in the
    first place.

    Every column is checked, not just the two obvious ones.  A column that is
    silently empty because nobody wired it up looks exactly like a column that
    is empty because there was nothing to put in it, and only an assertion can
    tell those apart.
    """
    from slife2.libs.host.paths import DATA_ENV_VAR, agent_dir, agents_dir
    from slife2.plugins.context.server import build_server as build_context

    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config()

    # Two model calls, of different sizes, so the two token columns can be told
    # apart: one is the turn's total, the other is only the last call's.
    backend = FakeBackend(
        ScriptedTurn(
            result=StreamChatResult(
                text="checking",
                tool_calls=(ToolCall(id="c1", name="skill_list", arguments={}),),
                usage=Usage(prompt_tokens=100, completion_tokens=10),
                stop_reason="tool_calls",
            ),
        ),
        ScriptedTurn(
            result=StreamChatResult(
                text="It is 42.",
                usage=Usage(prompt_tokens=130, completion_tokens=5),
                stop_reason="stop",
            ),
        ),
    )

    async with Client(
        build_context(cfg, embedder=StubEmbedder(), ask=keep_the_context)
    ) as context_client:
        server = build_server(
            cfg, backend=backend, context_client=context_client, hub_client=hub
        )
        await send(server, "what is 2+2?", agent="jack", channel="tui")

    jack = agent_dir("jack") / "turns.db"
    assert jack.is_file(), "the turn was not recorded"
    row = (
        sqlite3.connect(jack)
        .execute(
            "SELECT messages, summary, tags, created_at, completed_at,"
            " channel, who_helped, what_model, token_count, context_tokens FROM turn"
        )
        .fetchone()
    )
    (
        stored,
        summary,
        tags,
        created_at,
        completed_at,
        channel,
        who_helped,
        what_model,
        token_count,
        context_tokens,
    ) = row

    assert who_helped == "jack"
    assert channel == "tui"
    assert created_at and completed_at
    assert created_at <= completed_at
    # The bill for the turn against the size it had grown to by the end.
    assert (token_count, context_tokens) == (245, 135)
    # No `turn_summarize` call in this turn, so the two hooks stay empty; the
    # capture path is the test below.
    assert (summary, tags) == ("", "")
    # What actually answered, which with an injected backend is the backend —
    # not the config's default, which is a model that never ran.
    assert what_model == "fake"

    # The stored messages are the whole turn, opening with what the user said —
    # there is no column for it, so this entry is the only place it is kept.
    # The system prompt is *not* here: it belongs to the loop, not the turn, and
    # a copy per row is how it would accumulate.
    turns_messages = json.loads(stored)
    assert [m["role"] for m in turns_messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert turns_messages[0]["content"] == "what is 2+2?"

    # ...and no other agent's database was created along the way: only the
    # agents that were named have a directory.
    #
    # The one file here that is nobody's is the tool catalogue — the hub's, and
    # per home (`slife2.plugins.toolhub.server.Catalogue.store`): this turn asked a
    # hub that was itself built against another data directory, so *this*
    # home's catalogue was built by this home's first ask.  Which is the
    # rule rather than a leak: a home is served the tool set it is configured
    # with, in the file that home owns.
    here = list(agents_dir().iterdir())
    assert sorted(p.name for p in here if p.is_dir()) == ["jack"]
    assert [p.name for p in here if p.is_file()] and all(
        p.name.startswith("tools.db") for p in here if p.is_file()
    ), "the only file in a home's agents/ is its own tool catalogue"


@pytest.mark.asyncio
async def test_a_turn_summarize_call_annotates_the_turn_being_saved(
    tmp_path, monkeypatch, hub
) -> None:
    """The rowid-less annotation is applied at the save point, not written early."""
    from slife2.libs.host.paths import DATA_ENV_VAR, agent_dir
    from slife2.plugins.context.server import build_server as build_context

    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    cfg = config()

    backend = FakeBackend(
        ScriptedTurn(
            result=StreamChatResult(
                text="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="turn_summarize",
                        arguments={
                            "summary": "the quarterly forecast",
                            "tags": "planning",
                        },
                    ),
                ),
                usage=Usage(prompt_tokens=100, completion_tokens=10),
                stop_reason="tool_calls",
            ),
        ),
        ScriptedTurn(
            result=StreamChatResult(
                text="Noted.",
                usage=Usage(prompt_tokens=120, completion_tokens=5),
                stop_reason="stop",
            ),
        ),
    )

    async with Client(
        build_context(cfg, embedder=StubEmbedder(), ask=keep_the_context)
    ) as context_client:
        server = build_server(
            cfg, backend=backend, context_client=context_client, hub_client=hub
        )
        await send(server, "remember this", agent="jack", channel="tui")

    row = (
        sqlite3.connect(agent_dir("jack") / "turns.db")
        .execute("SELECT summary, tags FROM turn")
        .fetchone()
    )
    assert row == ("the quarterly forecast", "planning")


@pytest.mark.asyncio
async def test_a_trim_is_recorded_in_the_turn_rather_than_done_silently(
    tmp_path, monkeypatch
) -> None:
    """Over the budget, the harness takes tools away **and says so in the turn**.

    The failure this exists for is a model whose next request carries fewer
    tools than it believes it has: it reaches for one that is gone, and nothing
    in the conversation explains why.  So the trim is written as a tool pair —
    `_func_tool_unload`, called with no names — and the model reads which tools
    left, in the turn that took them.

    **Only when something actually moved.**  The pair is written from the
    *answer*, not from the attempt: a turn under the budget gets no pair at all,
    because an empty one every turn is a record of something that did not happen
    and a model reading those learns to distrust the mechanism.

    The evictable tools have to be outside `tool_load.autoload`: the names on
    that list are what the budget may not take, so the only way to be over the
    cap is to have loaded something else — which is the case the budget was
    built for.
    """
    from slife2.libs.host.config import ToolLoadSettings, ToolServerSettings
    from slife2.libs.host.paths import DATA_ENV_VAR, agent_dir
    from slife2.plugins.context.server import build_server as build_context
    from slife2.plugins.toolhub.server import build_server as build_hub
    from tests.fakes import plugin_transports, upstream_server

    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    # A cap of three, so the five the config starts loaded are already over it
    # and the two the model loads are the only unprotected tools there are.
    base = default_config()
    cfg = replace(
        base,
        tool_load=ToolLoadSettings(threshold=3),
        mcp={
            "fake": ToolServerSettings(name="fake", command="in-memory"),
        },
        agent=base.agent,
    )

    # The model loads a tool server's two tools, then answers.
    backend = FakeBackend(
        ScriptedTurn(
            result=StreamChatResult(
                text="loading",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="func_tool_load",
                        arguments={"names": ["fake__echo", "fake__boom"]},
                    ),
                ),
                stop_reason="tool_calls",
            ),
        ),
        ScriptedTurn(result=StreamChatResult(text="done", stop_reason="stop")),
    )

    async with (
        Client(
            build_hub(
                cfg,
                transports=plugin_transports(
                    cfg, {"fake": lambda settings: upstream_server()}
                ),
                embedder=StubEmbedder(),
            )
        ) as hub_client,
        Client(
            build_context(cfg, embedder=StubEmbedder(), ask=keep_the_context)
        ) as context_client,
    ):
        server = build_server(
            cfg, backend=backend, context_client=context_client, hub_client=hub_client
        )
        await send(server, "look something up", agent="jack", channel="tui")

    stored = json.loads(
        sqlite3.connect(agent_dir("jack") / "turns.db")
        .execute("SELECT messages FROM turn")
        .fetchone()[0]
    )

    # On the wire a call is `{id, type, function: {name, arguments}}`, and the
    # arguments are a JSON *string* there — the one place the format's
    # awkwardness is dealt with.
    trims = [
        (message["tool_calls"][0], stored[index + 1])
        for index, message in enumerate(stored)
        if message.get("tool_calls")
        and message["tool_calls"][0]["function"]["name"] == "_func_tool_unload"
    ]
    assert len(trims) == 1, "one trim, one pair — and it is in the saved turn"
    call, result = trims[0]
    assert call["function"]["arguments"] == "{}", "no names: the budget, not a choice"
    assert call["id"] == result["tool_call_id"], "answered, as the wire requires"
    assert result["role"] == "tool"
    answer = json.loads(result["content"])
    # Sorted, because the order is the budget's (least recently used first) and
    # what this pins is *what went*, said to the model in the pair.
    assert sorted(answer["unloaded"]) == ["fake__boom", "fake__echo"], (
        "the pair says what went"
    )


@pytest.mark.asyncio
async def test_a_trim_answer_naming_no_names_is_refused_rather_than_read_as_none(
    context, hub, monkeypatch, caplog
) -> None:
    """`found.get("unloaded") or []` read a missing key as "nothing was unloaded".

    So an answer from the hub that this build cannot read — the shape a daemon
    from another build returns — was taken as an honest no-op: no pair was
    written, and the model went on believing it still held the tools that had
    gone.  Absent is not empty: both halves of the answer are read as strictly
    as `_adopt` reads the store's, so an answer without the key is a failure and
    only a *readable* empty answer is a verdict (SDD Appendix C: a failure is
    not a verdict).  The trim is post-turn, so the refusal is caught and logged
    rather than failing a turn that has already answered.
    """
    import logging

    from slife2.plugins.agent import server as server_module

    async def no_names(_client: Any) -> dict[str, Any]:
        # Every key but the one the trim acts on: the same silence an empty list
        # makes, from an answer that says nothing at all.
        return {"refused": [], "not_loaded": [], "text": "the list is fine"}

    monkeypatch.setattr(server_module, "unload_tools", no_names)
    caplog.set_level(logging.WARNING, logger="slife2.plugins.agent.server")
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=answering("done")
    )

    result = await send(server, "hi", agent="jack")

    assert result.data["text"] == "done", "bookkeeping did not fail the turn"
    assert "names it unloaded" in caplog.text


@pytest.mark.asyncio
async def test_opening_a_loop_fails_when_the_context_store_is_gone(
    hub, real_dialer: Any
) -> None:
    """A missing context store is a broken system, not a degraded one.

    Asked before anything is spent rather than at the write: the point of the
    guard is to find out *before* a model call has been paid for, not once the
    answer exists and has nowhere to go.

    The store here is a URL nothing is listening on, which is what a `slife2
    down` looks like from the agent server's side.  Port 9 rather than the
    configured 24010, which would make this pass or fail on whether the developer
    happens to have slife2 running.

    **The sentence asserted is `open_server`'s, so the dial is the subject** and
    it is the real one being asked — `real_dialer` says so, because the suite's
    `no_live_stack` guard refuses every dial a test did not inject, and a test
    that refuses its own subject asserts a sentence the product never wrote.
    """
    real_dialer("slife2.plugins.agent.server")
    base = default_config()
    cfg = replace(
        base,
        servers={**base.servers, "context": replace(base.servers["context"], port=9)},
    )
    server = build_server(cfg, hub_client=hub, backend=answering("the answer"))

    with pytest.raises(ToolError, match="nothing is listening on 127.0.0.1:9"):
        await send(server, "hi", agent="jack")


# --- images ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_images_reach_the_model_as_content_parts(context, hub, cabinet) -> None:
    """A prompt with an image is a list of parts, not a string."""
    from slife2.libs.host.config import ModelSettings, ProviderSettings

    backend = answering("I see it")
    vision = replace(
        default_config(),
        providers={
            "deepseek": ProviderSettings(
                api="openai-completions",
                base_url="https://example.test",
                api_key_ref="${K:-x}",
                models={
                    "deepseek-flash": ModelSettings(
                        model="deepseek-flash", input=("text", "image")
                    )
                },
            )
        },
    )
    server = build_server(
        vision,
        context_client=context,
        hub_client=hub,
        cabinet_client=cabinet,
        backend=backend,
    )
    await send(server, "what is this?", images=["data:image/png;base64,AAAA"])

    sent = next(m for m in backend.calls[0][0] if m.role == "user")
    parts = without_prompt(sent.content)
    assert isinstance(parts, list)
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1]["image_url"]["url"].endswith("AAAA")
    # **And it is the last message**: nothing the harness adds follows it now
    # that the attachment is the model's own call.  The reply names what was
    # attached, which is what a later turn keeps when the pixels are gone.
    assert backend.calls[0][0][-1] is sent


@pytest.mark.asyncio
async def test_images_are_refused_by_a_model_that_cannot_read_them(
    context, hub
) -> None:
    """Dropping an attachment somebody made is worse than saying no.

    The config listing only `text` under `input` is the config saying so, and
    the alternative is a model that quietly ignores what was sent.  Note that
    the *default* model is a vision model, so this has to be built explicitly —
    which is the point: the check reads the config rather than assuming.

    **The refusal is a tool result and not an exception.**  The turn simply runs,
    the tool answers with why, and the model reads that — so it can tell the
    person instead of the turn failing with a message only a programmer sees.  A
    tool result also needs no assistant line written for it: the history stays
    well formed without the harness speaking for the model.
    """
    from slife2.libs.host.config import ModelSettings, ProviderSettings

    text_only = replace(
        default_config(),
        providers={
            "deepseek": ProviderSettings(
                api="openai-completions",
                base_url="https://example.test",
                api_key_ref="${K:-x}",
                models={"deepseek-flash": ModelSettings(model="deepseek-flash")},
            )
        },
    )
    backend = answering("ok")
    result = await send(
        build_server(
            text_only, context_client=context, hub_client=hub, backend=backend
        ),
        "look",
        images=["data:image/png;base64,AAAA"],
    )

    # **And the caller is told which source it was**, which is the half that was
    # missing: a reply that quietly carried fewer pictures than the call asked
    # for is the one thing a caller cannot detect for itself.
    assert result.data["refused"], "the refusal reaches the caller"
    assert "cannot read images" in result.data["refused"][0]

    # The model was asked with the text alone — nothing was encoded for it, so
    # there is no part a provider would reject — and the caller is told which
    # source was not attached, because a call whose pictures were fewer than it
    # asked for cannot tell by itself.
    asked = next(m for m in backend.calls[0][0] if m.role == "user")
    assert isinstance(asked.content, str)
    text = without_prompt(asked.content)
    assert isinstance(text, str)
    assert text.startswith("look")
    assert "data:image" not in text


def carries_an_image(messages) -> bool:
    """Whether a request would carry a picture — the one thing this fake refuses.

    Of *parts* and not of content: a message repaired by the drop is still a list
    of parts, and a list of text is what every provider accepts.
    """
    return any(
        isinstance(message.content, list)
        and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in message.content
        )
        for message in messages
    )


class RefusesImages(FakeBackend):
    """A provider that answers a request carrying an image with a 400.

    **Nothing is scripted for that call and none is consumed**, because the
    request itself is the failure: it is not one turn's bad luck but the request
    every later turn would send, which is what the repair is about.
    """

    def stream(self, messages, tools) -> Stream:
        if carries_an_image(messages):
            self.calls.append((list(messages), list(tools)))
            raise RuntimeError("400: You have uploaded an unsupported image")
        return super().stream(messages, tools)


@pytest.mark.asyncio
async def test_a_refused_image_does_not_end_the_conversation(context, hub) -> None:
    """The repair: the pixels go, the words stay, and the next turn runs.

    What is wrong without it is not the failed turn — that is recorded, and the
    person's own message stays in the conversation — but every turn *after* it.
    A failed turn's messages are left unbacked, so the next rebuild carries them
    verbatim, and a picture the provider refused is refused again by every
    request that follows.  The loop lives in the server process and outlives the
    terminal, so there is no restart that gets back to a usable conversation:
    that is the state this asserts against.
    """
    backend = RefusesImages(text_turn("the answer after"))
    server = build_server(
        default_config(), context_client=context, hub_client=hub, backend=backend
    )

    with pytest.raises(Exception, match="unsupported image") as failed:
        await send(server, "what is this?", images=["data:image/png;base64,AAAA"])
    # Told, rather than left to notice later that the picture stopped being sent.
    assert attachments.DROPPED_IMAGES in str(failed.value)

    answer = await send(server, "hi")

    assert answer.data["text"] == "the answer after"
    sent = next(m for m in backend.calls[1][0] if m.role == "user")
    assert without_footnote(sent.content[0]["text"]) == "what is this?"
    assert sent.content[1]["type"] == "text"
    assert "image not stored" in sent.content[1]["text"]
    assert "data:image" not in sent.content[1]["text"], "the bytes are gone"


@pytest.mark.asyncio
async def test_the_attachment_report_names_what_went_in_and_what_did_not(
    context, hub, tmp_path: Path
) -> None:
    """The model's own call answers which sources it can now see.

    **The batch rule reaches the model as two lists**, and that is what makes it
    useful: one unreadable path among two must not read as a call that failed,
    because the fault is the file's and the other picture is attached.  A refusal
    carried in the *same* answer is also the only place the model can learn it —
    a request that quietly carried fewer pictures than it named is the one thing
    it cannot detect for itself.
    """
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"pretend png bytes")
    server = build_server(
        config(),
        context_client=context,
        hub_client=hub,
        backend=FakeBackend(text_turn("ok")),
    )
    # A loop exists only for a conversation that has run, and this is the model
    # calling the tool inside a turn of one.
    await send(server, "look")

    answer = await attach(server, [str(shot), str(tmp_path / "gone.png")])

    assert answer["attached"] == [str(shot)]
    assert len(answer["refused"]) == 1
    assert str(tmp_path / "gone.png") in answer["refused"][0]
    assert "no such file" in answer["refused"][0], "and why, not just which"


def marker_of(content: Any) -> dict[str, Any]:
    """The `[Image:…]` payload in a stored message's parts, or fail."""
    parts = (
        content if isinstance(content, list) else [{"type": "text", "text": content}]
    )
    text = next(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and str(part.get("text") or "").startswith("[Image:")
    )
    return json.loads(text[len("[Image:") : -1])


@pytest.mark.asyncio
async def test_a_kept_image_is_a_cabinet_reference_in_the_row(
    context, hub, cabinet
) -> None:
    """The row names the cabinet copy; the live conversation keeps the pixels.

    The image is a cabinet document now, so the row is text plus an address
    rather than a note saying the picture is gone.  The live list is not
    rewritten — the model may still be reasoning about it in this very turn —
    and the next turn still sends the pixels.
    """
    backend = FakeBackend(text_turn("I see it"), text_turn("and again"))
    server = build_server(
        config(),
        context_client=context,
        hub_client=hub,
        cabinet_client=cabinet,
        backend=backend,
    )
    data = "data:image/png;base64,AAAA"
    await send(server, "what is this?", images=[data])
    await send(server, "and now?")

    live = [m for m in backend.calls[1][0] if m.role == "user"][0]
    assert isinstance(live.content, list) and any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in live.content
    ), "the conversation still carries the pixels"

    async with Client(server) as client:
        answer = await client.call_tool("transcript", {"agent": DEFAULT_AGENT})
    stored = answer.data["turns"][0]["messages"][0]["content"]
    marker = marker_of(stored)
    assert marker["key"].startswith("pictures/")
    assert marker["mime"] == "image/png"
    text = "".join(
        str(part.get("text") or "") for part in stored if isinstance(part, dict)
    )
    assert "data:image" not in text and "AAAA" not in text


@pytest.mark.asyncio
async def test_a_marker_key_attaches_the_image_again(context, hub, cabinet) -> None:
    """The reference is an address: `attach_image` takes its key.

    A rebuilt or restored conversation carries the marker instead of the
    picture, and the cabinet is how the picture comes back.  No new syntax: the
    key is an ordinary source to the tool that already attaches images.
    """
    backend = FakeBackend(text_turn("I see it"), text_turn("again"))
    server = build_server(
        config(),
        context_client=context,
        hub_client=hub,
        cabinet_client=cabinet,
        backend=backend,
    )
    await send(server, "what is this?", images=["data:image/png;base64,AAAA"])

    async with Client(server) as client:
        answer = await client.call_tool("transcript", {"agent": DEFAULT_AGENT})
    key = marker_of(answer.data["turns"][0]["messages"][0]["content"])["key"]

    attached = await attach(server, [key])
    assert attached["attached"] == [key]

    await send(server, "look again")
    live = [m for m in backend.calls[1][0] if m.role == "user"][0]
    urls = [
        part["image_url"]["url"]
        for part in live.content
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]
    assert any(url.startswith("data:image/png;base64,") for url in urls), (
        "the cabinet key came back as the picture"
    )


@pytest.mark.asyncio
async def test_an_interrupted_image_turn_claims_its_id(context, hub, cabinet) -> None:
    """The cabinet is what makes the id safe: the retry picture is durable.

    The turn is interrupted mid-stream; its write is detached, and the next
    turn takes the id up (`settle_record`) and writes the footnote.  A
    keep-list can name it, which was impossible while the only copy of the
    image was the live message a rebuild would replace.
    """
    backend = FakeBackend(
        text_turn(
            "never finished",
            delay=0.4,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn("continued"),
    )
    server = build_server(
        config(),
        context_client=context,
        hub_client=hub,
        cabinet_client=cabinet,
        backend=backend,
    )

    interrupted = asyncio.create_task(
        send(server, "look", images=["data:image/png;base64,AAAA"])
    )
    await wait_for_streams(backend)
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted
    await send(server, "continue")

    live = [m for m in backend.calls[1][0] if m.role == "user"][0]
    footnote = next(
        part["text"]
        for part in live.content
        if isinstance(part, dict) and str(part.get("text") or "").startswith("[TURN:")
    )
    turn_id = json.loads(footnote[len("[TURN:") : -1])["turn_id"]

    async with Client(server) as client:
        answer = await client.call_tool("transcript", {"agent": DEFAULT_AGENT})
    assert answer.data["turns"][0]["turn_id"] == turn_id
    assert marker_of(answer.data["turns"][0]["messages"][0]["content"])["key"]


@pytest.mark.asyncio
async def test_an_interrupted_subagent_image_turn_claims_its_id(
    context, hub, cabinet
) -> None:
    """The same rule on a subagent's own conversation and its own cabinet."""
    backend = FakeBackend(
        text_turn(
            "never finished",
            delay=0.4,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn("continued"),
    )
    server = build_server(
        config(),
        context_client=context,
        hub_client=hub,
        cabinet_client=cabinet,
        backend=backend,
    )

    interrupted = asyncio.create_task(
        send(
            server,
            "look",
            subagent="researcher",
            images=["data:image/png;base64,AAAA"],
        )
    )
    await wait_for_streams(backend)
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted
    await send(server, "continue", subagent="researcher")

    live = [m for m in backend.calls[1][0] if m.role == "user"][0]
    footnote = next(
        part["text"]
        for part in live.content
        if isinstance(part, dict) and str(part.get("text") or "").startswith("[TURN:")
    )
    assert json.loads(footnote[len("[TURN:") : -1])["turn_id"] > 0

    async with Client(server) as client:
        answer = await client.call_tool(
            "transcript", {"agent": DEFAULT_AGENT, "subagent": "researcher"}
        )
    assert marker_of(answer.data["turns"][0]["messages"][0]["content"])[
        "key"
    ].startswith("pictures/")


def refusing_store() -> tuple[FastMCP, dict[str, Any]]:
    """A context store that refuses its next record and accepts after.

    The `restore`/`rebuild` shapes are the real ones; `remember` is the refusal
    under test.  A counter rather than an always-refusing store, because the
    turn *after* the failed one has to be recordable for the test to see what
    the conversation holds.
    """
    stub = FastMCP("context-stub")

    @stub.tool
    async def restore(
        agent: str, messages: list[dict], subagent: str = ""
    ) -> dict[str, Any]:
        return {"messages": messages, "turn_ids": [], "turns": []}

    @stub.tool
    async def rebuild(
        agent: str,
        messages: list[dict],
        turn_ids: list[int],
        prompt: str,
        model: str,
        carried: int = 0,
        subagent: str = "",
        expected: list[int] | None = None,
    ) -> dict[str, Any]:
        return {
            "messages": messages,
            "turn_ids": turn_ids,
            "changed": False,
            "kept": len(turn_ids),
            "recalled": 0,
        }

    state = {"refuse_next": True, "next_id": 1}

    @stub.tool
    async def remember(
        agent: str,
        messages: list[dict],
        subagent: str = "",
        token_count: int = 0,
        context_tokens: int = 0,
        usage: dict[str, int] | None = None,
        who_helped: str = "",
        what_model: str = "",
        channel: str = "",
        summary: str = "",
        tags: str = "",
        created_at: str = "",
        completed_at: str = "",
    ) -> dict[str, Any]:
        if state["refuse_next"]:
            state["refuse_next"] = False
            raise ToolError("this store refuses the request")
        turn_id = state["next_id"]
        state["next_id"] += 1
        return {"turn_id": turn_id}

    return stub, state


@pytest.mark.asyncio
async def test_a_refused_record_fails_the_turn_and_rolls_it_back(context, hub) -> None:
    """A turn the store will not write is a hard failure, not a silent memory.

    The live list is rolled back to the prefix the turn began on: a later turn
    must not run on a message no keep-list can name.  The same rule holds for an
    agent's own conversation and for a subagent's.
    """
    stub, state = refusing_store()

    async with Client(stub) as store:
        for subagent in ("", "researcher"):
            state["refuse_next"] = True
            backend = FakeBackend(text_turn("first"), text_turn("second"))
            server = build_server(
                config(), context_client=store, hub_client=hub, backend=backend
            )
            with pytest.raises(Exception, match="refuses the request"):
                await send(server, "one", subagent=subagent)
            await send(server, "two", subagent=subagent)
            assert prompts_seen(backend, 1) == ["user:two"], (
                f"the unrecorded turn was not rolled back for {subagent!r}"
            )


@pytest.mark.asyncio
async def test_a_refused_record_of_an_interrupted_turn_rolls_it_back(
    context, hub
) -> None:
    """The detached path applies the same rule: no row, no message.

    The interruption's write happens after its caller is gone, so
    `settle_record` is where the refusal is seen — and it rolls the tail back
    before the next turn runs.
    """
    stub, _state = refusing_store()
    backend = FakeBackend(
        text_turn(
            "never finished",
            delay=0.4,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn("second"),
    )
    async with Client(stub) as store:
        server = build_server(
            config(), context_client=store, hub_client=hub, backend=backend
        )
        interrupted = asyncio.create_task(send(server, "one"))
        await wait_for_streams(backend)
        interrupted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await interrupted
        await send(server, "two")

    assert prompts_seen(backend, 1) == ["user:two"], (
        "the unrecorded interrupted turn was rolled back"
    )


@pytest.mark.asyncio
async def test_a_refused_image_keep_fails_the_turn(context, hub) -> None:
    """The cabinet is a hard failure too, and leaves no half-recorded turn."""
    stub = FastMCP("cabinet-stub")

    @stub.tool
    async def image_save(url: str) -> dict[str, Any]:
        raise ToolError("the cabinet refuses the image")

    backend = FakeBackend(text_turn("I see it"), text_turn("continued"))
    async with Client(stub) as keeper:
        server = build_server(
            config(),
            context_client=context,
            hub_client=hub,
            cabinet_client=keeper,
            backend=backend,
        )
        with pytest.raises(Exception, match="refuses the image"):
            await send(server, "look", images=["data:image/png;base64,AAAA"])
        await send(server, "continue")

    assert prompts_seen(backend, 1) == ["user:continue"], (
        "the turn whose image could not be kept is not in the conversation"
    )


@pytest.mark.asyncio
async def test_a_model_that_cannot_read_images_is_refused_by_name(context, hub) -> None:
    """The refusal is the tool's, and it is the tool the model called.

    The config listing only `text` under `input` is the config saying so, and the
    alternative is a model that quietly ignores what it was handed.  Nothing was
    attached, so this is a refusal of the call rather than a fact about the
    batch: the model's declared input is enough to refuse it before there is
    anything to encode.
    """
    from slife2.libs.host.config import ProviderSettings

    text_only = replace(
        default_config(),
        providers={
            "deepseek": ProviderSettings(
                api="openai-completions",
                base_url="https://example.test",
                api_key_ref="${K:-x}",
                models={"deepseek-flash": ModelSettings(model="deepseek-flash")},
            )
        },
    )
    server = build_server(
        text_only,
        context_client=context,
        hub_client=hub,
        backend=FakeBackend(text_turn("ok")),
    )
    await send(server, "look")

    with pytest.raises(ToolError, match="cannot read images"):
        await attach(server, ["data:image/png;base64,AAAA"])


@pytest.mark.asyncio
async def test_the_models_own_attachment_is_repaired_the_same_way(context) -> None:
    """The route the report came in on: `attach_image`, then a refused call.

    Nothing here distinguishes a picture the model attached from one a caller
    passed — the repair works on the conversation rather than on who put the
    picture in it — and that is the property worth having, since naming a file
    in a sentence is the way a person actually sends one.
    """
    backend = RefusesImages(
        ScriptedTurn(
            result=StreamChatResult(
                text="let me look",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="attach_image",
                        arguments={"sources": ["data:image/png;base64,AAAA"]},
                    ),
                ),
                stop_reason="tool_calls",
            )
        ),
        text_turn("a cat, and the next turn works"),
    )
    base = default_config()
    hub_client, server = wired(
        backend,
        context,
        replace(
            base,
            tool_load=replace(
                base.tool_load,
                autoload=(*base.tool_load.autoload, "attach_image"),
            ),
        ),
    )

    async with hub_client:
        with pytest.raises(Exception, match="unsupported image"):
            await send(server, "看看这张图 D:\\shots\\a.png")
        answer = await send(server, "hi")

    assert answer.data["text"] == "a cat, and the next turn works"


@pytest.mark.asyncio
async def test_a_failure_with_no_image_anywhere_is_raised_as_it_was(
    context, hub
) -> None:
    """The repair stays quiet when there is nothing to repair.

    One handler catches every failure, so this is the branch that keeps an
    ordinary one ordinary: a sentence about pictures on a turn that never had one
    is the system explaining a thing that did not happen.
    """

    class Refusing(FakeBackend):
        """The `:932` shape: a model call that fails the way a 500 does."""

        def stream(self, messages, tools) -> Stream:
            self.calls.append((list(messages), list(tools)))
            raise RuntimeError("the provider answered 500")

    server = build_server(
        default_config(), context_client=context, hub_client=hub, backend=Refusing()
    )

    with pytest.raises(Exception, match="500") as failed:
        await send(server, "one")

    assert attachments.DROPPED_IMAGES not in str(failed.value)


# --- the system prompt -------------------------------------------------------


def template(tmp_path, body: str):
    """A system prompt template on disk, as the config expects."""
    path = tmp_path / "system.j2"
    path.write_text(body, encoding="utf-8")
    return str(path)


@pytest.mark.asyncio
async def test_the_system_prompt_is_a_template(tmp_path, context, hub) -> None:
    backend = answering("ok")
    server = build_server(
        config(system_prompt=template(tmp_path, "be terse")),
        context_client=context,
        hub_client=hub,
        backend=backend,
    )
    await send(server, "hi")

    seen = backend.calls[0][0]
    assert seen[0].role == "system"
    assert seen[0].content == "be terse"


@pytest.mark.asyncio
async def test_the_template_is_rendered_with_the_loops_own_agent_name(
    tmp_path, context, hub
) -> None:
    """One template, personalised by whoever the loop belongs to.

    Rendered when the loop is opened rather than once at startup, because the
    agent name is a property of the *loop* — the server is shared, so two
    instances are two names asking one process.  Rendering at startup would hand
    the first caller's name to everybody, which is the failure this asserts
    against.
    """
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="ok")),
        ScriptedTurn(result=StreamChatResult(text="ok")),
    )
    server = build_server(
        config(system_prompt=template(tmp_path, "You are {{ agent_name }}.")),
        context_client=context,
        hub_client=hub,
        backend=backend,
    )

    await send(server, "hello", agent="jack")
    await send(server, "hello", agent="jill")

    assert backend.calls[0][0][0].content == "You are jack."
    assert backend.calls[1][0][0].content == "You are jill."


@pytest.mark.asyncio
async def test_the_prompt_knows_whether_anybody_is_watching(
    tmp_path, context, hub
) -> None:
    """Three states of one question, and the third is the interesting one.

    `_approve` is a courtesy extended to somebody who is there, and the model is
    what decides whether to use it: nothing in the machinery branches on being
    unattended, so the prompt is where an unattended agent has to be able to find
    out (`slife2 --headless`, SDD §10.5).  **And the server is what has to find
    it out**, in a process that never saw that command line: the question is not
    about this call (`watched`) but about who is holding the agent's name, which
    is what the claim records.

    The third case is the one a narrower reading would miss: **nobody is running
    under that name at all** is the same situation, not a fourth thing — a peer
    asking something of an agent with no window is told that there is nobody,
    rather than served by a conversation nobody is running (`slife2.libs.harness.handover`).
    """
    from slife2.libs.host.runtime import AgentClaim, clear_claim, write_claim

    body = "you are {{ agent_name }}; {{ 'nobody' if headless else 'somebody' }}"

    async def prompt_with(claim: AgentClaim | None) -> str:
        clear_claim(DEFAULT_AGENT)
        if claim is not None:
            write_claim(claim)
        backend = answering("ok")
        server = build_server(
            config(system_prompt=template(tmp_path, body)),
            context_client=context,
            hub_client=hub,
            backend=backend,
        )
        await send(server, "hi")
        return backend.calls[0][0][0].content

    unattended = await prompt_with(AgentClaim.now(DEFAULT_AGENT, headless=True))
    attended = await prompt_with(AgentClaim.now(DEFAULT_AGENT))
    nobody = await prompt_with(None)

    assert unattended == f"you are {DEFAULT_AGENT}; nobody"
    assert attended == f"you are {DEFAULT_AGENT}; somebody"
    assert nobody == f"you are {DEFAULT_AGENT}; nobody"


@pytest.mark.asyncio
async def test_a_headless_prompt_names_its_reply_tools_not_their_schema(
    context, hub
) -> None:
    """The unattended sentence points a reply at the sender's tool.

    **A headless window has nobody to read prose** (`slife2 --headless`,
    SDD §10.5): what reaches it came from a peer, a channel or a schedule, so
    the answer has to leave the way the sender can receive it.  The prompt names
    the tools; the arguments — `task_response`, `message_type` — stay in the
    tool descriptions, exactly as they do for a subagent
    (`test_a_subagents_prompt_names_its_reply_tools_not_their_schema`).
    """
    from slife2.libs.host.runtime import AgentClaim, clear_claim, write_claim

    clear_claim(DEFAULT_AGENT)
    write_claim(AgentClaim.now(DEFAULT_AGENT, headless=True))
    try:
        backend = answering("ok")
        server = build_server(
            config(), context_client=context, hub_client=hub, backend=backend
        )
        await send(server, "hi")
        prompt = backend.calls[0][0][0].content
    finally:
        clear_claim(DEFAULT_AGENT)

    assert "peer_agent_send_message" in prompt
    assert "peer_agent_broadcast" in prompt
    assert "peer_agent_group_send_message" in prompt
    assert "channel's own send tool" in prompt
    assert "task_response" not in prompt
    assert "message_type" not in prompt


@pytest.mark.asyncio
async def test_a_missing_template_is_refused_at_load(tmp_path) -> None:
    """A prompt that will not render is a config mistake worth naming once."""
    from slife2.libs.host.config import ConfigError, load

    path = tmp_path / "slife2.yaml"
    # A provider, because a file without one is refused before its own
    # mistakes are named (SDD §4.1).
    path.write_text(
        "providers:\n"
        "  local:\n"
        "    api: openai-completions\n"
        "    base_url: https://example.test/v1\n"
        "    models: [{model: big}]\n"
        "embeddings:\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://127.0.0.1:17347/v1\n"
        "      model: big-emb\n"
        "agent:\n  system_prompt: not-here.j2\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="not-here.j2"):
        load(path)


# --- limits ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_iterations_comes_from_the_config(context, hub) -> None:
    backend = FakeBackend(
        *[
            ScriptedTurn(
                result=StreamChatResult(
                    tool_calls=(ToolCall(id=f"c{i}", name="skill_list", arguments={}),),
                    stop_reason="tool_calls",
                )
            )
            for i in range(2)
        ]
    )
    server = build_server(
        config(max_iterations=2),
        context_client=context,
        hub_client=hub,
        backend=backend,
    )
    result = await send(server, "loop forever")

    assert len(backend.calls) == 2
    assert result.data["stop_reason"] == "max_iterations"


# --- the streaming contract --------------------------------------------------


async def send_with_progress(
    server: FastMCP, prompt: str, handler, *, agent: str = DEFAULT_AGENT
):
    async with Client(server) as client:
        return await client.call_tool(
            "send_message",
            {"agent": agent, "prompt": prompt},
            progress_handler=handler,
        )


@pytest.mark.asyncio
async def test_events_arrive_as_progress_notifications(context, hub) -> None:
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=tool_then_answer()
    )
    seen: list[TurnEvent] = []

    async def on_progress(progress, total, message):
        event = decode(message or "")
        assert event is not None, f"not one of our payloads: {message!r}"
        seen.append(event)

    await send_with_progress(server, "what is 6*7?", on_progress)

    kinds = [type(e).__name__ for e in seen]
    # The context first, and that ordering is the point of the event: what the
    # turn runs on is decided before the turn says anything, so a display that
    # learned it last would be showing one turn's context under another turn's
    # answer.
    assert kinds[0] == "ContextChosen"
    assert "ToolsCounted" in kinds
    assert "TextDelta" in kinds
    assert "ToolCallStarted" in kinds
    assert "ToolCallFinished" in kinds
    assert kinds[-1] == "TurnFinished"

    # **The size of the catalogue, not the length of the list beside it.**
    # This turn's model list is `tool_then_answer`'s tools, which is a handful;
    # the count is the hub's own whole-catalogue answer, and it is asked again
    # with each model call — two of them here.
    counted = [event for event in seen if isinstance(event, ToolsCounted)]
    catalogued = await hub.call_tool("list_tools", {})
    assert [event.total for event in counted] == [catalogued.data["total"]] * 2
    # The request's list is the hub's list: every model tool, the background
    # pair included, is a catalogue row and nothing is added beside it.
    assert [event.loaded for event in counted] == [len(catalogued.data["tools"])] * 2

    finished = seen[-1]
    assert finished.text == "It is 42."  # type: ignore[union-attr]
    assert finished.iterations == 2  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_progress_values_are_a_monotonic_counter(context, hub) -> None:
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=tool_then_answer()
    )
    values: list[float] = []

    async def on_progress(progress, total, message):
        values.append(progress)
        assert total is None, "inventing a total would render a fake percentage"

    await send_with_progress(server, "x", on_progress)

    assert values == sorted(values)
    assert len(set(values)) == len(values)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_a_broken_observer_does_not_lose_the_message(
    context, hub, monkeypatch
) -> None:
    """A UI that throws must not end a turn — the server's own events included.

    `ContextChosen` is reported *before* the turn runs, so an exception from the
    observer there lands before the prompt has been appended or recorded: it
    propagates out of `send_message`, the caller is told the turn failed, and the
    message is gone — nowhere in the conversation and nowhere in the log.  The
    same observer failing on a `TextDelta` is swallowed by `AgentLoop._emit`,
    which is the whole reason that seam exists; the two events the *server* raises
    were the ones not routed through it.
    """
    from slife2.plugins.agent import server as server_module

    class Broken:
        """A context whose progress stream is closed: every report throws."""

        def __init__(self, _ctx: Any, **_options: Any) -> None: ...

        async def on_event(self, _event: TurnEvent) -> None:
            raise RuntimeError("the progress stream is closed")

    monkeypatch.setattr(server_module, "ProgressObserver", Broken)

    backend = FakeBackend(text_turn("the answer"))
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    result = await send(server, "one")

    assert result.data["text"] == "the answer"
    # The message survived with it: it reached the model, which is the half the
    # old raise destroyed.
    assert prompts_seen(backend) == ["user:one"]


@pytest.mark.asyncio
async def test_a_broken_observer_does_not_kill_an_approval_request(
    context, hub, monkeypatch
) -> None:
    """The other server-side raise: the `_approve` request, from inside the turn.

    The request is announced to whoever is watching before the turn waits for the
    answer, and a broken stream throws from that announcement.  Left to
    propagate it would kill the very turn that is waiting for the answer — a tool
    call the model is entitled to make, refused because a *display* failed —
    where the same observer failing on a `TextDelta` is swallowed.  Routed through
    `AgentLoop._emit`, the request is still registered in `loop.approvals`, so an
    answer arriving by the ordinary route (`answer_approval`) is taken and the
    turn runs on.
    """
    from slife2.plugins.agent import server as server_module

    class Broken:
        def __init__(self, _ctx: Any, **_options: Any) -> None: ...

        async def on_event(self, _event: TurnEvent) -> None:
            raise RuntimeError("the progress stream is closed")

    monkeypatch.setattr(server_module, "ProgressObserver", Broken)

    backend = FakeBackend(
        ScriptedTurn(
            result=StreamChatResult(
                text="checking",
                tool_calls=(
                    ToolCall(id="c1", name="skill_list", arguments={"_approve": True}),
                ),
                stop_reason="tool_calls",
            ),
            chunks=[Chunk(text="checking")],
        ),
        text_turn("done"),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    turn = asyncio.create_task(send(server, "one", watched=True))
    await wait_for_streams(backend)
    # Answer the request the way a watching client does: a call of its own.  The
    # announcement never reached anyone (the observer is broken), so the test
    # polls for the future to exist — which is itself the claim, since it is the
    # emit that threw before.
    async with Client(server) as client:
        for _ in range(500):
            answer = await client.call_tool(
                "answer_approval",
                {"agent": DEFAULT_AGENT, "call_id": "c1", "approved": True},
            )
            if answer.data["answered"]:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the approval request never reached the loop")

    assert (await turn).data["text"] == "done"


@pytest.mark.asyncio
async def test_report_progress_is_a_no_op_without_a_token() -> None:
    """The token, and not this module, is what decides whether a stream exists.

    `ProgressObserver.on_event` calls `ctx.report_progress` unconditionally, and
    the whole of what makes a non-streaming client safe is that **FastMCP's own
    `Context` checks for a progress token itself**.  That is a claim about
    somebody else's code, so it is asserted against somebody else's code: a real
    server, a real `Context`, the observer's own call — once with a client that
    sent no token, and once with one that did.  The test this replaces handed the
    observer a duck-typed context whose `report_progress` returned `None`, so it
    could not fail whatever FastMCP did, and a regression that made
    `report_progress` raise without a token would have surfaced as turns failing
    for every client that is not streaming.
    """
    from slife2.libs.turn.events import TextDelta

    server: FastMCP = FastMCP("progress")

    @server.tool
    async def report(ctx: Context) -> str:
        await ProgressObserver(ctx).on_event(TextDelta("x"))
        return "reported"

    # No token: the observer's call is the no-op that has to be silent.
    async with Client(server) as client:
        assert (await client.call_tool("report", {})).data == "reported"

    # A token: the same call reaches the handler, which is what makes the first
    # half about the token rather than about the call never doing anything.
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async with Client(server) as client:
        await client.call_tool("report", {}, progress_handler=on_progress)

    assert [one[0] for one in seen] == [1]


# --- everything above, over a real socket ------------------------------------


@asynccontextmanager
async def over_http(server: FastMCP) -> AsyncGenerator[str]:
    """Serve a built server on a real port, and yield its URL.

    A real socket rather than the in-memory transport, because both of the
    properties asserted below are properties of the wire: that progress
    notifications survive HTTP framing, and that closing a response stream
    cancels the turn behind it.  Neither can be shown in-process.
    """
    app = server.http_app(path="/mcp", json_response=False, stateless_http=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    import uvicorn

    uv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    serve_task = asyncio.create_task(uv.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if uv.started:
                break
            await asyncio.sleep(0.05)
        assert uv.started, "the server did not start"
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uv.should_exit = True
        await asyncio.wait_for(
            serve_task,
            timeout=5,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        sock.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_progress_streams_over_real_http(context, hub) -> None:
    """The one test that binds a port for the streaming contract.

    The in-memory transport cannot prove that progress notifications survive
    HTTP framing, and `json_response=True` would break them silently while still
    returning correct results — so this asserts they arrive *during* the call
    with more than one of them, not as a single buffered dump at the end.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=tool_then_answer()
    )
    arrivals: list[tuple[float, str]] = []

    async def on_progress(progress, total, message):
        event = decode(message or "")
        if event is not None:
            arrivals.append((progress, type(event).__name__))

    async with over_http(server) as url, Client(url) as client:
        result = await client.call_tool(
            "send_message",
            {"agent": "jack", "prompt": "what is 6*7?"},
            progress_handler=on_progress,
        )

    assert result.data["text"] == "It is 42."
    assert len(arrivals) > 1
    assert arrivals[-1][1] == "TurnFinished"
    assert "ToolCallStarted" in [name for _, name in arrivals]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_cancelled_turn_is_repaired_over_real_http(context, hub) -> None:
    """The load-bearing assumption, measured rather than read.

    Everything the loop does on the way out depends on one property of the
    transport: closing a response stream cancels the handler behind it, *at the
    await point it has reached*.  At this revision that is the only cancellation
    signal there is — a conforming client never puts `notifications/cancelled`
    on the wire, and the server acknowledges one that arrives without acting on
    it, because honouring it by client-chosen request id would let one caller
    cancel another's work.

    So the assertion is end-to-end and behavioural: a turn is cut off
    mid-stream, and the *next* turn's model call shows the user's message
    survived and nothing the interrupted turn produced did.  The alternative —
    an assistant message whose tool calls are only partly answered — is a 400
    from every provider, and nothing inside the process can show that.
    """
    backend = FakeBackend(
        text_turn(
            "never finished",
            delay=0.4,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn(
            "fine"
        ),  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    async with over_http(server) as url:
        async with Client(url) as client:
            interrupted = asyncio.create_task(
                client.call_tool(
                    "send_message", {"agent": DEFAULT_AGENT, "prompt": "one"}
                )
            )
            await wait_for_streams(backend)

            interrupted.cancel()
            with pytest.raises(asyncio.CancelledError):
                await interrupted

        # A fresh connection, which also shows the server was not poisoned by
        # the cancellation, and one more message — which is how the repair is
        # observed: what the loop holds is what the model is next sent.
        async with Client(url) as client:
            result = await client.call_tool(
                "send_message", {"agent": DEFAULT_AGENT, "prompt": "two"}
            )

    assert result.data["text"] == "fine"
    assert prompts_seen(backend, 1) == ["user:one", "user:two"]


# --- subagents ---------------------------------------------------------------


async def set_subagent(
    server: FastMCP,
    name: str,
    *,
    agent: str = DEFAULT_AGENT,
    model: str = "",
    role: str = "subagent",
    fork: bool = False,
):
    """Name a subagent, with the caller's client id attached — as a turn does."""
    async with Client(server) as client:
        return await client.call_tool(
            SUBAGENT_SET,
            {"name": name, "model": model, "role": role, "fork": fork},
            meta=client_meta(agent),
        )


async def remove_subagent(server: FastMCP, name: str, *, agent: str = DEFAULT_AGENT):
    """Unmake one, the same way."""
    async with Client(server) as client:
        return await client.call_tool(
            SUBAGENT_REMOVE, {"name": name}, meta=client_meta(agent)
        )


def subagent_of(agent: str, name: str) -> Path:
    """Where a subagent's directory is, without asking the code under test.

    Spelt the long way on purpose: what the tests around this one are *for* is
    that a subagent is a directory in a named container and nothing else is, so a
    helper that asked `agent_dir` where it lives would agree with any layout.
    """
    return agents_dir() / agent / "subagents" / name


@pytest.mark.asyncio
async def test_a_subagent_is_a_directory_inside_its_makers(context, hub) -> None:
    """`subagent_set` makes a member: a directory, and the record that says so.

    The record is what `subagent_remove` proves its blast radius with, and it
    holds the whole of what a subagent *is* — the model it runs on, the role it
    was made for, whether it is a fork, and when it was made — which is the part
    of an agent's identity that is neither its conversation nor its work, so it
    is in the file rather than in a registry somewhere.  Which is also what makes
    a subagent's settings as short-lived as the subagent: `subagent_remove`
    deletes the directory, and there is nothing else holding them.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=FakeBackend()
    )

    made = await set_subagent(
        server,
        "researcher",
        model="deepseek/deepseek-flash",
        role="code reviewer",
        fork=True,
    )

    assert made.data["created"] is True
    assert made.data["name"] == "researcher"
    assert made.data["model"] == "deepseek/deepseek-flash"
    assert made.data["role"] == "code reviewer"
    assert made.data["fork"] is True
    assert subagent_of(DEFAULT_AGENT, "researcher").is_dir()
    record = subagent_file(DEFAULT_AGENT, "researcher")
    assert record.is_file(), "the file is what makes the directory a subagent's"
    stored = YAML(typ="safe").load(record.read_text(encoding="utf-8"))
    # The answer names what the model acts on; when it was made is the record's
    # own, the part the model never reads.
    assert stored.pop("created_at")
    assert stored == {
        "name": "researcher",
        "model": "deepseek/deepseek-flash",
        "role": "code reviewer",
        "fork": True,
    }


@pytest.mark.asyncio
async def test_naming_a_subagent_twice_is_the_same_subagent(context, hub) -> None:
    """`set` rather than `add`: what a model may say twice without thinking.

    What a second call changes is what the subagent *runs on* and what it is
    *for* — its model and its role, which are its settings.  What it does not
    change is when it was made and whether it was made as a fork, and those two
    are one answer rather than two rules: a fork's conversation is a copy
    already taken, so a later call cannot re-decide what it is made of — the
    same reason `created_at` is kept.  Removing it and naming it again is how a
    maker asks for a different one.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=FakeBackend()
    )

    first = await set_subagent(server, "researcher", model="one/one", fork=True)
    before = YAML(typ="safe").load(
        subagent_file(DEFAULT_AGENT, "researcher").read_text(encoding="utf-8")
    )
    again = await set_subagent(
        server, "researcher", model="two/two", role="code reviewer", fork=False
    )
    after = YAML(typ="safe").load(
        subagent_file(DEFAULT_AGENT, "researcher").read_text(encoding="utf-8")
    )

    assert first.data["created"] is True
    assert again.data["created"] is False, "the same subagent, not a second one"
    assert again.data["model"] == "two/two"
    assert again.data["role"] == "code reviewer"
    assert again.data["fork"] is True, "made as a fork, and that is what it is"
    assert after["created_at"] == before["created_at"], "when it was made is kept"
    assert len(subagent_dirs(DEFAULT_AGENT)) == 1


@pytest.mark.asyncio
async def test_a_subagent_cannot_make_a_subagent(context, hub) -> None:
    """The tree stops at one level, and stops by name rather than by accident.

    The local network holds a place for a maker's *own* subagents and no further
    in (`slife2.plugins.a2a.server.place_for`), so a member of a member would be
    a conversation with nothing listening for it — a refusal from the tool rather
    than a subagent that exists and can never answer (SDD §4.5.3).
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=FakeBackend()
    )

    async with Client(server) as client:
        with pytest.raises(ToolError) as refused:
            await client.call_tool(
                SUBAGENT_SET,
                {"name": "deeper"},
                meta=client_meta(DEFAULT_AGENT, "researcher"),
            )

    assert "cannot make a subagent" in str(refused.value)
    assert not subagent_of(DEFAULT_AGENT, "researcher").exists()


@pytest.mark.asyncio
async def test_the_cap_refuses_a_subagent_past_it(context, hub) -> None:
    """`subagent: max_running` is the bound, and it counts the maker's own."""
    base = config()
    server = build_server(
        replace(base, subagent=replace(base.subagent, max_running=1)),
        context_client=context,
        hub_client=hub,
        backend=FakeBackend(),
    )

    await set_subagent(server, "one")
    with pytest.raises(ToolError) as refused:
        await set_subagent(server, "two")

    assert "max_running" in str(refused.value)
    # And naming the one that is already there is not a second subagent.
    assert (await set_subagent(server, "one")).data["created"] is False


@pytest.mark.asyncio
async def test_a_name_the_agent_already_uses_is_refused(context, hub) -> None:
    """A subagent's name is a directory inside the agent's, and some are taken.

    `cabinet` is one of them — the agent's own — and a name that resolves to
    something which is not a subagent is refused rather than adopted, so a call
    cannot turn the cabinet into a subagent by naming it.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=FakeBackend()
    )
    cabinet = subagent_of(DEFAULT_AGENT, "cabinet")
    cabinet.mkdir(parents=True)
    (cabinet / "note.md").write_text("mine", encoding="utf-8")

    with pytest.raises(ToolError) as refused:
        await set_subagent(server, "cabinet")

    assert "not a subagent" in str(refused.value)
    assert (cabinet / "note.md").read_text(encoding="utf-8") == "mine"


@pytest.mark.asyncio
async def test_removing_a_subagent_takes_its_directory_with_it(context, hub) -> None:
    """One directory, one operation — the whole reason the nesting exists.

    Idempotent, and `reset`'s answer for the same question: a caller that wanted
    something gone and finds it gone is in the state it asked for.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=FakeBackend()
    )
    await set_subagent(server, "researcher")
    (subagent_of(DEFAULT_AGENT, "researcher") / "work.txt").write_text(
        "something it made", encoding="utf-8"
    )

    done = await remove_subagent(server, "researcher")

    assert done.data == {"name": "researcher", "removed": True}
    assert not subagent_of(DEFAULT_AGENT, "researcher").exists()
    assert (await remove_subagent(server, "researcher")).data["removed"] is False


@pytest.mark.asyncio
async def test_removing_something_that_is_not_a_subagent_is_refused(
    context, hub
) -> None:
    """The blast radius is proved by the record, not by the name.

    A directory without it is not a subagent's, so the one deletion this system
    does cannot be pointed at an agent's own cabinet by naming it.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=FakeBackend()
    )
    cabinet = subagent_of(DEFAULT_AGENT, "cabinet")
    cabinet.mkdir(parents=True)
    (cabinet / "note.md").write_text("mine", encoding="utf-8")

    with pytest.raises(ToolError) as refused:
        await remove_subagent(server, "cabinet")

    assert "not a subagent" in str(refused.value)
    assert (cabinet / "note.md").read_text(encoding="utf-8") == "mine"


@pytest.mark.asyncio
async def test_a_subagents_turns_are_recorded_in_its_own_store(context, hub) -> None:
    """What a subagent owns, it owns: its conversation is written, and to itself.

    This is the reversal `Loop.records` was — a subagent's turns used to be
    deliberately unwritten — and what it means now is one file per subagent,
    inside the subagent's own directory, which is what `subagent_remove` deletes.
    """
    server = build_server(
        config(),
        context_client=context,
        hub_client=hub,
        backend=FakeBackend(text_turn("hello from the subagent")),
    )

    await send(server, "one", subagent="researcher")

    own = subagent_of(DEFAULT_AGENT, "researcher") / "turns.db"
    assert own.is_file(), "recorded, in its own store"
    assert not (agents_dir() / DEFAULT_AGENT / "turns.db").exists(), (
        "and not in the maker's — nobody said anything to the maker"
    )
    assert TurnStore(own).count() == 1


@pytest.mark.asyncio
async def test_a_subagent_is_told_its_own_name_whose_it_is_and_where_work_comes_from(
    context, hub
) -> None:
    """Its prompt says who it is, whose it is, and where its work comes from.

    The agent's name alone would be a model told it is somebody else; told
    *whose* subagent it is and that a task or message arrives from that agent or
    a sibling, it can address its answer.
    """
    backend = FakeBackend(text_turn("hello"), text_turn("hello"))
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one", subagent="researcher")
    await send(server, "two")

    subagent_prompt = backend.calls[0][0][0].content
    maker_prompt = backend.calls[1][0][0].content
    assert subagent_prompt.startswith("You are researcher,")
    assert f"one of {DEFAULT_AGENT}'s subagents" in subagent_prompt, (
        "it is told whose it is, which is also the member it answers"
    )
    assert f"from {DEFAULT_AGENT} or another subagent" in subagent_prompt
    assert maker_prompt.startswith(f"You are {DEFAULT_AGENT},")
    assert f"You are {DEFAULT_AGENT}," not in subagent_prompt, (
        "its maker's name would be a model told it is somebody else"
    )


@pytest.mark.asyncio
async def test_a_subagents_prompt_names_its_reply_tools_not_their_schema(
    context, hub
) -> None:
    """The prompt names the reply tools; their schema stays on the tools.

    A subagent's answer has to leave by `subagent_send_message`,
    `subagent_broadcast` or `subagent_group_send_message`, and naming them here
    is what points a fresh conversation at the right ones.  *How* to answer —
    `task_response` and `message_type` — and the group's casual-silence rule
    belong to the tool descriptions, where the model already reads the
    arguments; repeating them here would be a second schema kept in step by
    hand.
    """
    backend = FakeBackend(text_turn("hello"))
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one", subagent="researcher")

    prompt = backend.calls[0][0][0].content
    assert "subagent_send_message" in prompt
    assert "subagent_broadcast" in prompt
    assert "subagent_group_send_message" in prompt
    assert "task_response" not in prompt
    assert "message_type" not in prompt


def a_second_model():
    """A config with two models on it, so "which one it runs on" is observable.

    The default config has one provider, and a subagent that names no model is
    supposed to run on its maker's — so a second model is what makes the
    difference between "read the subagent's file" and "read the default" visible
    at all.
    """
    from slife2.libs.host.config import ProviderSettings

    base = default_config()
    return replace(
        base,
        providers={
            **base.providers,
            "other": ProviderSettings(
                api="openai-completions",
                base_url="https://example.test",
                api_key_ref="${K:-x}",
                models={"slow": ModelSettings(model="slow")},
            ),
        },
    )


@pytest.mark.asyncio
async def test_a_subagent_runs_on_the_model_its_own_file_names(context, hub) -> None:
    """The point of naming a model: the subagent runs on it, and not on another.

    Read when its conversation starts and not when the record was written, which
    is what makes the file a *config* rather than a note: a subagent that has not
    started yet runs on what its file says now, and one that has started keeps
    what it began on — the same rule `model_switch` and the config's `default:`
    follow, one level in.
    """
    server = build_server(
        a_second_model(),
        context_client=context,
        hub_client=hub,
        backend=FakeBackend(text_turn("ok"), text_turn("ok")),
    )
    await set_subagent(server, "researcher", model="other/slow")

    named = await send(server, "one", subagent="researcher")
    plain = await send(server, "one", subagent="helper")

    assert named.data["model"] == "other/slow"
    assert plain.data["model"] == "deepseek/deepseek-flash", "the maker's own"


@pytest.mark.asyncio
async def test_a_subagents_prompt_is_its_own_and_says_what_it_is(
    tmp_path, context, hub
) -> None:
    """`role` reaches the prompt, and the prompt is not the operator's.

    `agent.system_prompt` is what an agent's *own* conversation opens with — a
    person's prompt for the agent they are running, naming them and saying
    whether anybody is watching — and a subagent is told what it is instead
    (`subagent.j2`).  Which is why `role` is a word and not a template: it is the
    one part of what a subagent is that its maker decides.
    """
    backend = FakeBackend(text_turn("ok"), text_turn("ok"))
    server = build_server(
        config(system_prompt=template(tmp_path, "be terse")),
        context_client=context,
        hub_client=hub,
        backend=backend,
    )
    await set_subagent(server, "researcher", role="code reviewer")
    await set_subagent(server, "helper")

    await send(server, "one", subagent="researcher")
    await send(server, "one")

    named = backend.calls[0][0][0].content
    assert named.startswith(
        f"You are researcher, one of {DEFAULT_AGENT}'s subagents — your role is code reviewer."
    )
    assert f"from {DEFAULT_AGENT} or another subagent" in named
    assert backend.calls[1][0][0].content == "be terse", "the operator's, for its maker"
    record = YAML(typ="safe").load(
        subagent_file(DEFAULT_AGENT, "helper").read_text(encoding="utf-8")
    )
    assert record["role"] == "subagent", "the role a subagent is made with unnamed"


@pytest.mark.asyncio
async def test_a_fork_starts_from_a_copy_of_its_makers_conversation(
    context, hub
) -> None:
    """`fork: true` is a subagent made of somebody else's conversation.

    What it is handed is the maker's context as it stood when the fork's
    first turn started — everything the maker had said and been told — and
    `prompts_seen` is the request the fork's first turn actually sent, so the
    claim is about the model call and not about a field somewhere.
    """
    backend = FakeBackend(text_turn("the maker's answer"), text_turn("the fork's"))
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one")
    await set_subagent(server, "researcher", fork=True)
    await send(server, "two", subagent="researcher")

    assert prompts_seen(backend, 1) == [
        "user:one",
        "assistant:the maker's answer",
        "user:two",
    ]
    head = backend.calls[1][0][0].content
    assert head.startswith(
        f"You are researcher, one of {DEFAULT_AGENT}'s subagents — your role is subagent."
    )
    # **A fork is not told that it is one** (the designer's call): the copy
    # above is the whole of what makes it a fork, and explaining it earned less
    # than it cost.
    assert "fork" not in head.lower()
    assert "copy" not in head.lower()


@pytest.mark.asyncio
async def test_a_fork_taken_from_a_maker_that_has_said_nothing(context, hub) -> None:
    """The empty case, which is an ordinary one: a maker that is just starting.

    Nothing is carried and nothing fails — the fork is handed an empty
    conversation and runs on its own message, exactly as a subagent with no
    history would.
    """
    backend = FakeBackend(text_turn("the fork's"))
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await set_subagent(server, "researcher", fork=True)
    await send(server, "one", subagent="researcher")

    assert prompts_seen(backend, 0) == ["user:one"]


@pytest.mark.asyncio
async def test_a_fork_writes_nothing_down(context, hub) -> None:
    """Its turns are not recorded — not under its own key, and not under its maker's.

    The maker's conversation is what a fork is *made of*, so writing the fork's
    turns there would file one agent's work as another's history; and writing
    them under the fork's own key would put a copy of a conversation nobody had
    into a store whose whole job is to be that conversation's memory — and then
    hand it back, on the next start, as the context to continue from.
    """
    backend = FakeBackend(text_turn("the maker's"), text_turn("the fork's"))
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one")
    await set_subagent(server, "researcher", fork=True)
    await send(server, "two", subagent="researcher")

    assert not (subagent_of(DEFAULT_AGENT, "researcher") / "turns.db").exists()
    assert TurnStore(agents_dir() / DEFAULT_AGENT / "turns.db").count() == 1, (
        "the maker's own turn, and nothing the fork said"
    )


@pytest.mark.asyncio
async def test_a_cancelled_fork_turn_is_not_rolled_back_by_the_next_message(
    context, hub
) -> None:
    """A fork's `record` answers `None` because it has no log, not because it failed.

    The turn's messages are the only copy a fork has, so treating that `None` as
    a refused write and rolling the tail back deleted the user's message and the
    completed tool batch — the work the cancellation path keeps on purpose.
    """
    backend = FakeBackend(
        text_turn("the maker's"),
        text_turn(
            "never finished",
            delay=0.4,  # timeouts: the value this test is built around, not a budget
        ),  # timeouts: the value this test is built around, not a budget
        text_turn("continued"),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one")
    await set_subagent(server, "researcher", fork=True)
    interrupted = asyncio.create_task(send(server, "two", subagent="researcher"))
    await wait_for_streams(backend, 2)
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted

    await send(server, "three", subagent="researcher")
    assert "user:two" in prompts_seen(backend, 2), (
        "the cancelled fork turn is still in its conversation"
    )
    assert "user:three" in prompts_seen(backend, 2)


@pytest.mark.asyncio
async def test_a_fork_does_not_outlive_its_session(context, hub) -> None:
    """The next start removes the record; a subagent with a store is kept.

    A fork is its conversation, and its conversation is in memory — so the
    `subagent.yaml` a restart finds describes something that no longer exists.
    Left there, the name would answer as a *new* fork of wherever its maker
    stands now: a different subagent wearing the old one's name.  So the start
    of a session ends the last one's forks, and frees their names.

    A non-fork subagent is the contrast, and the reason this is about the record
    rather than about tidiness: it owns a store, so a restart must keep it.
    """
    backend = FakeBackend(text_turn("the maker's"))
    first = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )
    await set_subagent(first, "researcher", fork=True)
    await set_subagent(first, "writer")

    assert subagent_of(DEFAULT_AGENT, "researcher").is_dir()
    assert subagent_of(DEFAULT_AGENT, "writer").is_dir()

    # A second server on the same home is a restart: its first session is where
    # the previous one's forks end.
    second = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )
    await send(second, "one")

    assert not subagent_of(DEFAULT_AGENT, "researcher").exists(), (
        "a fork's record outlived the conversation it described"
    )
    assert subagent_of(DEFAULT_AGENT, "writer").is_dir(), (
        "a subagent with a stored conversation is not a fork's to lose"
    )

    # And the name is free again rather than being a half-subagent that `set`
    # would silently take for the old one.
    remade = await set_subagent(second, "researcher", fork=True)
    assert remade.data["created"] is True


@pytest.mark.asyncio
async def test_the_idle_sweep_does_not_forget_a_fork(context, hub, monkeypatch) -> None:
    """A fork's own turns are the only copy, so an idle fork is not idle state.

    For every other conversation the sweep only reclaims memory: the turns are
    in its store, and the next message restores them.  A fork has no store, so
    reaping it would not reclaim anything — it would lose the conversation and
    hand the next message a fresh copy of whatever the maker has said since.
    """
    monkeypatch.setattr(_T.timeouts.pacing, "loop_idle", 0.0)
    backend = FakeBackend(
        text_turn("the maker's"),
        text_turn("the fork's"),
        text_turn("again"),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one")
    await set_subagent(server, "researcher", fork=True)
    await send(server, "two", subagent="researcher")
    await send(server, "three", subagent="researcher")

    assert prompts_seen(backend, 2) == [
        "user:one",
        "assistant:the maker's",
        "user:two",
        "assistant:the fork's",
        "user:three",
    ], "the fork kept its own turn instead of starting again from its maker"


@pytest.mark.asyncio
async def test_a_fork_is_a_copy_and_not_a_shared_conversation(context, hub) -> None:
    """The maker moves on without it, and that is true in both directions.

    A copy rather than the same list, and both halves matter: what the maker says
    after the fork was made is not something the fork has read, and the fork's
    own turns are not in the maker's conversation either.  Reading "share
    everything with the agent that made you" as *one* conversation with two
    readers is the reading this rejects, and it is rejected on what a fork is
    for: its job is to answer *about* a conversation, which it cannot do from
    inside it.
    """
    backend = FakeBackend(
        text_turn("one"),
        text_turn("forked"),
        text_turn("three"),
        text_turn("again"),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )

    await send(server, "one")
    await set_subagent(server, "researcher", fork=True)
    await send(server, "two", subagent="researcher")
    await send(server, "three")
    await send(server, "four", subagent="researcher")

    assert prompts_seen(backend, 3) == [
        "user:one",
        "assistant:one",
        "user:two",
        "assistant:forked",
        "user:four",
    ], "the fork's copy, then its own turns, and nothing the maker said since"
    assert prompts_seen(backend, 2) == [
        "user:one",
        "assistant:one",
        "user:three",
    ], "and the maker never saw what the fork said"


@pytest.mark.asyncio
async def test_a_fork_is_taken_from_a_maker_that_is_mid_turn(context, hub) -> None:
    """The copy ends at the last exchange that finished, and it has to.

    A maker calls `subagent_set` *as* a tool call, so the conversation it is in
    the middle of ends with an assistant message whose call has no result yet —
    the one history every provider refuses (`trim_tools` records the same thing
    about the same pair).  A copy taken then is a conversation nobody can be
    sent, so what a fork is handed is the conversation up to the point its maker
    can still be read from: the message it is answering (`two`) is in the copy,
    and the half-finished exchange is not.

    **The maker is parked on `_approve` rather than merely slow**, so that *when*
    the fork is made is a fact this test knows instead of a race it hopes goes
    one way — a race would leave the assertion below passing for the wrong
    reason, since a maker that had not appended its call yet produces the same
    expected list.
    """
    from slife2.libs.turn.events import ToolApprovalRequested

    backend = FakeBackend(
        text_turn("one"),
        ScriptedTurn(
            result=StreamChatResult(
                text="checking",
                tool_calls=(
                    ToolCall(id="c1", name="skill_list", arguments={"_approve": True}),
                ),
                stop_reason="tool_calls",
            ),
            chunks=[Chunk(text="checking")],
        ),
        text_turn("the fork's"),
        text_turn("done"),
    )
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )
    await send(server, "one")

    parked = asyncio.Event()

    async def on_progress(_progress, _total, message):
        if isinstance(decode(message or ""), ToolApprovalRequested):
            parked.set()

    async def maker():
        async with Client(server) as client:
            return await client.call_tool(
                "send_message",
                {"agent": DEFAULT_AGENT, "prompt": "two", "watched": True},
                progress_handler=on_progress,
            )

    turn = asyncio.create_task(maker())
    await asyncio.wait_for(parked.wait(), timeout=10)

    await set_subagent(server, "researcher", fork=True)
    # **Bounded, because the way this test fails is a deadlock.**  A fork that
    # waits on the maker's lock waits out the maker's *whole turn*, and the
    # maker here is parked on an approval this test cannot answer until after
    # this line — so the regression is not a wrong value or an exception, it is
    # a lane that never finishes.  A timeout is that deadlock saying so.
    await asyncio.wait_for(
        send(server, "three", subagent="researcher"),
        timeout=10,  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget

    assert prompts_seen(backend, 2) == [
        "user:one",
        "assistant:one",
        "user:two",
        "user:three",
    ], "the maker's half-finished exchange is not a history a model can be sent"

    # And the maker's own turn is untouched by any of it: it was waiting for an
    # answer to a question, and it gets one.
    async with Client(server) as client:
        await client.call_tool(
            "answer_approval",
            {"agent": DEFAULT_AGENT, "call_id": "c1", "approved": True},
        )
    assert (await turn).data["text"] == "done"


@pytest.mark.asyncio
async def test_a_fork_made_while_the_maker_is_live_keeps_its_message(
    context, hub, monkeypatch
) -> None:
    """**The maker's conversation is read under the maker's lock.**

    `adopt_maker` restores the maker's loop when the maker has not restored it
    yet, and that is a state a live maker is in for one store round trip: the
    loop is in `loops` from the moment `conversation` made it, and `send_message`
    sets `restored` only once the store has answered.  A fork arriving in that
    window ran the same `restore_into` as the maker — *not* under the maker's
    lock, which the maker is holding — and two restores of one list are one
    answer landing on top of the other: the fork's, arriving late, replaces a
    list the maker has already appended its user message to.  The turn then
    advances the orphaned list while `loop.messages` holds one that never saw it,
    so `outcome.messages` comes out empty, nothing is recorded, and the fork is
    handed a copy that never had the maker's message in it.

    **Parked rather than raced.**  The maker's restore and the fork's are each
    held open on an event, so *when* each lands is a fact this test knows instead
    of a hope: the maker's is released first and its turn is proved running
    before the fork's is let go.  With the lock the fork cannot be in its restore
    at all until the maker's turn has ended, which is why the fork's copy is
    taken from the live list — the maker's finished turn and all.
    """
    backend = FakeBackend(text_turn("the maker's answer"), text_turn("the fork's"))
    store = ParkedRestore(context)
    server = build_server(
        config(), context_client=store, hub_client=hub, backend=backend
    )

    maker = asyncio.create_task(send(server, "one"))
    await asyncio.wait_for(store.arrived.wait(), timeout=_T.timeouts.work.hub)
    await set_subagent(server, "researcher", fork=True)
    fork = asyncio.create_task(send(server, "two", subagent="researcher"))
    # **Where the two builds part company.**  Without the lock the fork is in
    # its own restore by now — the maker's is parked and holds nothing — and this
    # wait sees two; with it the fork is parked on the maker's lock and is not in
    # a restore at all.  Nothing below depends on which of those happened.
    for _ in range(100):
        if store.entered == 2:
            break
        await asyncio.sleep(0.01)
    store.release[0].set()
    # The maker's turn is running — its user message is appended and the model
    # has been asked — which is the moment the fork's restore used to land on
    # top of it.
    await wait_for_streams(backend, 1)
    store.release[1].set()

    assert (await maker).data["text"] == "the maker's answer"
    assert (await fork).data["text"] == "the fork's"
    # The copy the fork runs on: the maker's whole finished turn, then its own
    # message.  An empty copy is the fork having read a list the maker's own
    # restore had just replaced.
    assert prompts_seen(backend, 1) == [
        "user:one",
        "assistant:the maker's answer",
        "user:two",
    ]
    # ...and the maker's turn is in its own log, which is the other half of the
    # same damage: a turn that advanced an orphaned list records nothing.
    assert TurnStore(agents_dir() / DEFAULT_AGENT / "turns.db").count() == 1


def test_an_unfinished_exchange_is_what_a_copy_leaves_off() -> None:
    """`settled_messages` directly, because it is a rule about message lists.

    A turn in flight ends wherever it has got to, and a copy of a conversation is
    a *request* — so the rule is stated over the shapes a list can have and not
    over one path through the server.  What it must not do is cut into a finished
    exchange: a tool round the model has already been told about is context, and
    losing it would be a copy that knows less than the agent it was made from.
    """
    from slife2.libs.turn.messages import Message
    from slife2.plugins.agent.server import settled_messages

    head = Message(role="system", content="you are")
    asked = Message(role="user", content="one")
    called = Message(
        role="assistant", content=None, tool_calls=[ToolCall(id="c1", name="t")]
    )
    answered = Message(role="tool", content="result", tool_call_id="c1")
    spoke = Message(role="assistant", content="two")
    both = Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="c1", name="t"), ToolCall(id="c2", name="t")],
    )

    assert settled_messages([]) == []
    assert settled_messages([head, asked]) == [head, asked]

    # The two shapes a turn in flight leaves: a call nobody has answered, and a
    # batch that answered only the first of two.
    assert settled_messages([head, asked, called]) == [head, asked]
    assert settled_messages([head, asked, both, answered]) == [head, asked]

    # And the shapes that are histories, all of which stay whole: an answer, a
    # finished exchange, and the same exchange with the model still to speak.
    whole = [head, asked, called, answered, spoke]
    assert settled_messages(whole) == whole
    assert settled_messages([head, asked, called, answered]) == [
        head,
        asked,
        called,
        answered,
    ]
    assert settled_messages(whole + [called]) == whole


def test_a_removal_waits_for_a_file_and_then_says_so_in_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Windows will not delete a file that is open, and a model cannot read `WinError 32`.**

    What met the model instead was the OS's own sentence, in the OS's own
    language — `[WinError 32] 另一个程序正在使用此文件，进程无法访问。:
    …\\tester\\turns.db` — which names neither what happened nor what to do.  The
    holder is the subagent's own conversation store, and the honest answer is to
    wait out a write that is still landing (milliseconds) and then, if it is
    still held, to say the file is open in another process and to ask again.

    Refused as a whole rather than half-done: a removal that deleted most of a
    subagent would leave something that is neither one nor a directory anybody
    can finish removing.
    """
    import shutil

    real_rmtree = shutil.rmtree
    directory = tmp_path / "subagents" / "researcher"
    directory.mkdir(parents=True)
    busy = directory / "turns.db"
    busy.write_bytes(b"")

    def busy_error() -> PermissionError:
        return PermissionError(32, "The process cannot access the file", str(busy))

    attempts: list[int] = []

    def always_held(path, *args, **kwargs):
        attempts.append(1)
        raise busy_error()

    monkeypatch.setattr("slife2.plugins.agent.server.shutil.rmtree", always_held)
    monkeypatch.setattr(_T.timeouts.storage, "sqlite_busy", 0.05)
    with pytest.raises(ToolError) as refused:
        _remove_tree(directory)

    assert len(attempts) > 1, "it did not wait for the holder at all"
    said = str(refused.value)
    assert str(busy) in said, "the refusal does not name the file that is held"
    assert "another process" in said and "Ask again" in said
    assert "WinError" not in said, "the OS's own words are not an answer"
    assert directory.exists(), "a refused removal leaves the directory alone"

    # A holder that lets go inside the budget is waited out rather than reported:
    # the first attempt finds the file busy, the second finds it free.
    monkeypatch.setattr(_T.timeouts.storage, "sqlite_busy", 5.0)
    calls: list[int] = []

    def goes_quiet(path, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise busy_error()
        real_rmtree(path)

    monkeypatch.setattr("slife2.plugins.agent.server.shutil.rmtree", goes_quiet)
    _remove_tree(directory)

    assert len(calls) == 2, "a holder that let go was not waited out"
    assert not directory.exists()


@pytest.mark.asyncio
async def test_removing_a_subagent_tells_the_network_before_it_deletes(
    context, hub
) -> None:
    """**The place on the network goes first, and it is the third `forget`.**

    A removal is one directory and three processes hold something of it: this
    server holds the conversation, the context store holds the list it would
    restore from, and the subagent network holds the *place* — an MQTT connection
    subscribed to the name, answering whoever sends to it.  A deleted directory
    cannot unsubscribe anything, so the plugin is told, and this is asserted at
    the one moment the order is visible: the recorder looks at the filesystem as
    the call arrives, and finds the subagent still there.
    """
    told: list[dict[str, Any]] = []
    still_there: list[bool] = []
    mcp: FastMCP = FastMCP("mesh")

    @mcp.tool
    async def forget(agent: str, subagent: str = "") -> dict[str, Any]:
        told.append({"agent": agent, "subagent": subagent})
        still_there.append(subagent_of(DEFAULT_AGENT, subagent).exists())
        return {"agent": agent, "subagent": subagent, "forgotten": True}

    base = default_config()
    async with Client(mcp) as mesh:
        server = build_server(
            replace(
                base,
                subagent=replace(base.subagent, network=A2ASettings(configured=True)),
            ),
            context_client=context,
            hub_client=hub,
            backend=FakeBackend(),
            mesh_client=mesh,
        )
        await set_subagent(server, "researcher")
        directory = subagent_of(DEFAULT_AGENT, "researcher")
        assert directory.is_dir(), "nothing to remove, so nothing to tell"

        answer = await remove_subagent(server, "researcher")

    assert answer.data["removed"] is True
    assert told == [{"agent": DEFAULT_AGENT, "subagent": "researcher"}]
    assert still_there == [True], "the network was told after the files went"
    assert not directory.exists()


@pytest.mark.asyncio
async def test_a_machine_with_no_subagent_network_removes_without_telling_anyone(
    context, hub
) -> None:
    """The one case `leave_the_mesh` returns without a call, and it is the usual one.

    A machine with no `subagent:` section has no network to tell — that section's
    presence is what a network *is* (`SubagentSettings.network.configured`) — so a
    removal is this server's loop and the files, and nothing is dialled.  Which is
    what every test above this one exercises: their config has no such section,
    and the `no_live_stack` guard would refuse the dial if one were made.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=FakeBackend()
    )
    await set_subagent(server, "researcher")

    assert (await remove_subagent(server, "researcher")).data["removed"] is True
    assert not subagent_of(DEFAULT_AGENT, "researcher").exists()


# --- watching a conversation -------------------------------------------------


@contextlib.asynccontextmanager
async def watching(
    server: FastMCP, *, agent: str = DEFAULT_AGENT, subagent: str = ""
) -> AsyncGenerator[tuple[list[Any], asyncio.Task[Any]]]:
    """A watch call held open, and the frames it has been handed so far.

    The call is started as a task and *probed* for its snapshot rather than
    slept on: what proves a watcher is registered is that the server has
    answered it at all — the same question every caller of the tool asks.
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

    async with Client(server) as client:
        task = asyncio.create_task(
            client.call_tool(
                "watch",
                {"agent": agent, "subagent": subagent},
                progress_handler=on_progress,
                timeout=None,
            )
        )
        try:
            await asyncio.wait_for(snapshot.wait(), timeout=10)
            yield frames, task
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def wait_for_frame(
    frames: list[Any], wanted: type, *, timeout: float = 10
) -> Any:
    """Wait for one frame of a type, and answer with it.

    A poll rather than an event: a test wants *a particular* frame out of a
    stream whose length it cannot know, and the stream is live — the progress
    callback appends in arrival order.
    """
    for _ in range(int(timeout / 0.01)):
        for item in frames:
            if isinstance(item, wanted):
                return item
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {wanted.__name__} arrived; frames: {frames!r}")


@pytest.mark.asyncio
async def test_the_subagent_list_is_the_makers_directories(context, hub) -> None:
    """The list a screen offers is the directories, not the mesh roster.

    A subagent is a directory carrying its record
    (`slife2.libs.host.paths.subagent_dirs`), so that is the list — one that
    asked `subagent_list` would be the model's roster, which connects the mesh
    and wakes members to answer, and a picker must not do either.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=FakeBackend()
    )
    await set_subagent(server, "researcher", role="code reviewer")
    await set_subagent(server, "scout", model="deepseek/deepseek-flash")

    async with Client(server) as client:
        answer = await client.call_tool("subagents", {"agent": DEFAULT_AGENT})
        none = await client.call_tool("subagents", {"agent": "jill"})

    assert answer.data == {
        "subagents": [
            {
                "name": "researcher",
                "model": "",
                "role": "code reviewer",
                "fork": False,
            },
            {
                "name": "scout",
                "model": "deepseek/deepseek-flash",
                "role": "subagent",
                "fork": False,
            },
        ]
    }, "sorted, with the record's own facts"
    assert none.data == {"subagents": []}, "an agent that made none has none"


@pytest.mark.asyncio
async def test_a_watch_opens_with_the_history_and_not_a_replayed_turn(
    context, hub
) -> None:
    """A turn already in the store is the snapshot's, and the tap stands down.

    The tap holds the last or current turn, and a turn that has just been
    recorded is both in the store and in the tap.  `recorded_turn` is what tells
    the two apart: when the snapshot contains that id, replaying the tap would
    draw the same turn twice.
    """
    server = build_server(
        config(),
        context_client=context,
        hub_client=hub,
        backend=answering("hello back"),
    )
    await send(server, "hello", subagent="scout")

    async with watching(server, subagent="scout") as (frames, _):
        first = frames[0]
        assert isinstance(first, Snapshot)
        assert len(first.turns) == 1
        assert any(
            message.get("role") == "user" for message in first.turns[0]["messages"]
        ), "the stored turn is the one that was sent"
        # Give the handler its turn at the replay decision, then assert it made
        # none: one frame is the whole stream for a conversation with no live
        # turn.
        await asyncio.sleep(0.05)
        assert [type(item) for item in frames] == [Snapshot]


@pytest.mark.asyncio
async def test_a_watched_conversation_streams_its_next_turn(context, hub) -> None:
    """A watcher that is already attached sees a turn's whole vocabulary.

    `TurnStarted` is the one frame a turn's own caller never needs: it sent the
    message.  A watcher did not, so it is what says what is being answered.
    """
    server = build_server(
        config(),
        context_client=context,
        hub_client=hub,
        backend=FakeBackend(text_turn("hi")),
    )

    async with watching(server, subagent="scout") as (frames, _):
        await send(server, "hello", subagent="scout")
        ended = await wait_for_frame(frames, TurnFinished)
        started = await wait_for_frame(frames, TurnStarted)

        assert started.text == "hello"
        assert started.channel == ""
        assert any(isinstance(item, TextDelta) for item in frames)
        assert ended.text == "hi"


@pytest.mark.asyncio
async def test_a_watch_that_joins_mid_turn_is_handed_the_turn_from_its_start(
    context, hub
) -> None:
    """**The tap is why switching to a running conversation shows its start.**

    The store has nothing for a turn in flight — a turn is written when it ends
    — so a watcher that only read the store would see the answer without the
    question, and the deltas after the moment it arrived without the ones
    before.  The tap holds the turn from `TurnStarted` on, and the snapshot
    here is genuinely empty: the turn's start can only have come from it.
    """
    backend = FakeBackend(text_turn("abcdef", delay=0.15))
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=backend
    )
    running = asyncio.create_task(send(server, "slow one", subagent="scout"))
    # The turn has reached the model, and has streamed at least one chunk by
    # the time the watch opens — so the replay has something to replay.
    await wait_for_streams(backend, 1)
    await asyncio.sleep(0.25)

    async with watching(server, subagent="scout") as (frames, _):
        first = frames[0]
        assert isinstance(first, Snapshot)
        assert first.turns == [], "the store has no turn yet — it is in flight"

        started = await wait_for_frame(frames, TurnStarted)
        assert started.text == "slow one", "the message the watcher never sent"
        await wait_for_frame(frames, TurnFinished)
        assert any(isinstance(item, TextDelta) for item in frames)

    assert (await running).data["text"] == "abcdef"


@pytest.mark.asyncio
async def test_removing_a_subagent_ends_its_watch(context, hub) -> None:
    """The last frame says why, and the call returns rather than going quiet.

    A stream that simply stopped would be indistinguishable from an idle
    conversation, which is the opposite news.  `Closed` is the sentence, and the
    handler's own return is what makes the far end's call come back.
    """
    server = build_server(
        config(), context_client=context, hub_client=hub, backend=FakeBackend()
    )
    await set_subagent(server, "scout")

    async with watching(server, subagent="scout") as (frames, task):
        await remove_subagent(server, "scout")
        result = await asyncio.wait_for(task, timeout=10)

    assert result.data == {"closed": True}
    assert isinstance(frames[-1], Closed)
    assert "scout was removed" in frames[-1].reason
