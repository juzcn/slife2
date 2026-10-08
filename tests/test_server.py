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
import json
import socket
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
import pytest_asyncio
from fakes import FakeBackend, ScriptedTurn, text_turn
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2.config import DEFAULT_AGENT, default_config
from slife2.events import TurnEvent, decode
from slife2.llm.base import Chunk, Stream
from slife2.messages import StreamChatResult, ToolCall, Usage
from slife2.server.server import ProgressObserver, build_server

pytestmark = pytest.mark.unit


def config(**agent_overrides):
    base = default_config()
    return replace(base, agent=replace(base.agent, **agent_overrides))


def tool_then_answer() -> FakeBackend:
    """One scripted turn that calls `calc`, then one that answers."""
    return FakeBackend(
        ScriptedTurn(
            result=StreamChatResult(
                text="checking",
                tool_calls=(ToolCall(id="c1", name="calc", arguments={"e": "6*7"}),),
                stop_reason="tool_calls",
            ),
            chunks=[Chunk(text="checking")],
        ),
        ScriptedTurn(
            result=StreamChatResult(text="It is 42.", stop_reason="stop"),
            chunks=[Chunk(text="It is 42.")],
        ),
    )


def answering(text: str) -> FakeBackend:
    return FakeBackend(ScriptedTurn(result=StreamChatResult(text=text)))


@pytest_asyncio.fixture(loop_scope="function")
async def memory():
    """A db server that actually answers, over the in-memory transport.

    Sending **cannot happen without it**: a db server that is not there is a
    broken system rather than a degraded one, so `send_message` fails before
    anything is spent on a model call.  See
    `slife2.mcp_server.open_server`.

    The real server rather than a stub, so what these tests exercise is the
    component the agent server actually talks to — the one test that is *about*
    the failure passes its own config instead.
    """
    from slife2.db_server import build_server as build_db

    async with Client(build_db(default_config())) as client:
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def hub():
    """A toolhub that actually answers, over the in-memory transport.

    A turn cannot run without one, for the same reason it cannot run without
    memory — the model's tool list comes from here, builtins included, so a
    missing hub is a broken system rather than a conversation with no tools.

    No upstreams: these tests are about the agent, and the tool servers behind a
    hub are `tests/test_toolhub.py`'s subject.  What it does prove here is that
    the registry the loop runs with is the one the hub advertised.

    The real server rather than a stub, so the tool names and schemas these
    tests see crossed a real MCP hop.
    """
    from slife2.toolhub import build_server as build_hub
    from tests.fakes import component_transports

    # The hub asks every component for a tool list and refuses when one does not
    # answer, so all of them need something behind them.  In-memory, which keeps
    # `calc` and `now` real without a port.
    async with Client(
        build_hub(
            default_config(), transports=component_transports(default_config())
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


def prompts_seen(backend: FakeBackend, call: int = 0) -> list[str]:
    """What the model was sent on one of its calls, as role-tagged text.

    The system prompt is dropped: it is the same on every call and its content
    is the subject of its own tests, so including it here would only make every
    assertion about the conversation longer without making it stronger.
    """
    return [
        f"{m.role}:{m.content}" for m in backend.calls[call][0] if m.role != "system"
    ]


# --- the surface -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_server_exposes_two_tools(memory, hub) -> None:
    """There is no `open_loop` because there is nothing to open.

    An id a server mints is an id a caller has to keep, and keeping it is where
    every lifetime problem starts.  A conversation is started by the first
    message that names it, so sending *is* opening — and what is left is one verb
    and one way to start over.
    """
    async with Client(
        build_server(
            config(), memory_client=memory, hub_client=hub, backend=FakeBackend()
        )
    ) as client:
        tools = await client.list_tools()
    assert [t.name for t in tools] == ["send_message", "reset"]


@pytest.mark.asyncio
async def test_a_turn_returns_the_final_text(memory, hub) -> None:
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=tool_then_answer()
    )
    result = await send(server, "what is 6*7?")
    assert result.data["text"] == "It is 42."


@pytest.mark.asyncio
async def test_a_turn_reports_its_shape(memory, hub) -> None:
    """What a caller needs to render and to bill — and no history.

    `new_messages` is deliberately absent: it was the caller's half of owning
    the conversation, and the loop owns it now.
    """
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=tool_then_answer()
    )
    result = await send(server, "what is 6*7?")

    assert result.data["steps"] == 2
    assert result.data["stop_reason"] == "stop"
    assert "usage" in result.data
    assert "new_messages" not in result.data


@pytest.mark.asyncio
async def test_a_turn_says_what_answered(memory, hub) -> None:
    """A bare provider or an empty reference does not reveal the model."""
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=answering("ok")
    )
    result = await send(server, "hi", agent="jack")
    assert result.data["model"] == default_config().default


# --- a loop is the conversation ---------------------------------------------


@pytest.mark.asyncio
async def test_a_loop_remembers_what_was_said_to_it(memory, hub) -> None:
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
        config(), memory_client=memory, hub_client=hub, backend=backend
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
async def test_one_server_serves_two_loops_without_mixing_them(memory, hub) -> None:
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
            prompt = messages[-1].content or ""

            async def chunks():
                yield Chunk(text=f"reply to {prompt}")

            async def result():
                return StreamChatResult(text=f"reply to {prompt}")

            return Stream(chunks=chunks(), result=result())

    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=EchoBackend()
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
async def test_a_subagent_is_a_conversation_of_its_own(memory, hub) -> None:
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
        config(), memory_client=memory, hub_client=hub, backend=backend
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
async def test_reset_starts_the_conversation_over(memory, hub) -> None:
    """The one lifecycle verb left, and it is the caller's to ask for.

    Idempotent, because a caller that had nothing to forget is already in the
    state it asked for.
    """
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first")),
        ScriptedTurn(result=StreamChatResult(text="second")),
    )
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=backend
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
    memory, monkeypatch, hub
) -> None:
    """The idle sweep reclaims memory, and that is the whole of what it does.

    It used to be what made an id go stale, which meant every caller needed a
    path for "your conversation is gone".  A key cannot go stale, so the sweep is
    invisible: the next message starts a conversation under the same name, and
    nothing has to be told that it happened.
    """
    monkeypatch.setattr("slife2.server.server.LOOP_IDLE_SECONDS", 0.0)
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first")),
        ScriptedTurn(result=StreamChatResult(text="second")),
    )
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=backend
    )

    await send(server, "one")
    await send(server, "two")

    # Swept in between, so the second call began a conversation rather than
    # continuing one — and nothing raised either way.
    assert prompts_seen(backend, 1) == ["user:two"]


# --- the inbox ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_sent_to_a_busy_loop_waits_and_then_runs(memory, hub) -> None:
    """The core claim of the whole design, and the reason loops exist at all.

    A second message arrives while the first turn is still streaming.  Under the
    TUI's old policy it would have *cancelled* the first turn and thrown it
    away; here it queues, runs in its turn, and both answers come back.

    The second assertion is the one that catches a real mistake: the queued
    message must not be visible to the turn that is already running.  The loop
    re-reads its message list at every step, so a message appended on arrival
    would be read by the model mid-turn — steering nobody asked for, arriving
    silently.
    """
    backend = FakeBackend(
        text_turn("slow answer", delay=0.15),
        text_turn("second answer"),
    )
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=backend
    )

    first = asyncio.create_task(send(server, "one"))
    # The first turn has begun only once the backend has been asked for a
    # stream; anything earlier and the second message might win the race for
    # the lock and this would assert nothing.
    for _ in range(200):
        if backend.calls:
            break
        await asyncio.sleep(0.01)
    assert backend.calls, "the first turn never reached the model"

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


@pytest.mark.asyncio
async def test_a_cancelled_message_leaves_the_lock_free(memory, hub) -> None:
    """A caller that goes away while queued is not a lock held forever."""
    backend = FakeBackend(text_turn("slow", delay=0.2), text_turn("next"))
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=backend
    )

    first = asyncio.create_task(send(server, "one"))
    for _ in range(200):
        if backend.calls:
            break
        await asyncio.sleep(0.01)

    queued = asyncio.create_task(send(server, "doomed"))
    await asyncio.sleep(0.02)
    queued.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await queued

    # The one that was already running is untouched, and the loop still works.
    result = await first
    assert result.data["text"] == "slow"
    assert (await send(server, "after")).data["text"] == "next"


# --- cancellation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_interrupted_turn_keeps_the_users_message_and_drops_the_rest(
    memory, hub
) -> None:
    """The sharpest edge in the system, and it is back because the state is.

    Two things have to hold at once.  The user's message survives — losing it is
    the failure this whole arrangement exists to prevent — and nothing the
    interrupted turn produced survives with it, because a cancellation can land
    between an assistant message that asked for three tool calls and the second
    of their results, and *that* list is a 400 from every provider.

    Asserted through the next turn rather than by reaching inside: what the
    model is sent is the only thing that actually matters.
    """
    backend = FakeBackend(text_turn("never finished", delay=0.4), text_turn("fine"))
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=backend
    )

    interrupted = asyncio.create_task(send(server, "one"))
    for _ in range(200):
        if backend.calls:
            break
        await asyncio.sleep(0.01)
    interrupted.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await interrupted

    await send(server, "two")
    assert prompts_seen(backend, 1) == ["user:one", "user:two"]


@pytest.mark.asyncio
async def test_an_interrupted_turn_is_still_recorded(
    memory, hub, tmp_path, monkeypatch
) -> None:
    """Recording is unconditional, so the cancel path is not a special case.

    A turn that happened gets a row, and a cancelled one is the shape of a
    question with no answer — which is what it was.  The alternative is a
    message the user can see, the model will see, and the database has never
    heard of.
    """
    from slife2.db_server import build_server as build_db
    from slife2.paths import DATA_ENV_VAR, db_dir

    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    backend = FakeBackend(text_turn("never finished", delay=0.4))
    async with Client(build_db(config())) as memory_client:
        server = build_server(
            config(), backend=backend, memory_client=memory_client, hub_client=hub
        )
        interrupted = asyncio.create_task(send(server, "one"))
        for _ in range(200):
            if backend.calls:
                break
            await asyncio.sleep(0.01)
        interrupted.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await interrupted
        # The write is detached from the cancelled request, so give it a moment
        # to land rather than racing it.
        await asyncio.sleep(0.5)

    rows = (
        sqlite3.connect(db_dir() / f"{DEFAULT_AGENT}.turn.db")
        .execute("SELECT messages FROM turn ORDER BY rowid")
        .fetchall()
    )
    assert len(rows) == 1, "the cancelled turn was not recorded"
    stored = json.loads(rows[0][0])
    assert [m["role"] for m in stored] == ["user"]
    assert stored[0]["content"] == "one"


# --- memory ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_is_written_to_memory(tmp_path, monkeypatch, hub) -> None:
    """The turn lands in that agent's own database, and nowhere else.

    Both halves matter.  Written at all, because a memory component that nothing
    calls is a component that does nothing; and written to *that agent's* file,
    because isolation between agents is the reason the file is per-agent in the
    first place.

    Every column is checked, not just the two obvious ones.  A column that is
    silently empty because nobody wired it up looks exactly like a column that
    is empty because there was nothing to put in it, and only an assertion can
    tell those apart.
    """
    from slife2.db_server import build_server as build_db
    from slife2.paths import DATA_ENV_VAR, db_dir

    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    cfg = config()

    # Two model calls, of different sizes, so the two token columns can be told
    # apart: one is the turn's total, the other is only the last call's.
    backend = FakeBackend(
        ScriptedTurn(
            result=StreamChatResult(
                text="checking",
                tool_calls=(ToolCall(id="c1", name="calc", arguments={"e": "6*7"}),),
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

    async with Client(build_db(cfg)) as memory_client:
        server = build_server(
            cfg, backend=backend, memory_client=memory_client, hub_client=hub
        )
        await send(server, "what is 2+2?", agent="jack", channel="human")

    jack = db_dir() / "jack.turn.db"
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
    assert channel == "human"
    assert created_at and completed_at
    assert created_at <= completed_at
    # The bill for the turn against the size it had grown to by the end.
    assert (token_count, context_tokens) == (245, 135)
    # Retrieval hooks: nothing writes them yet, and the schema is where the
    # later feature finds them rather than an ALTER TABLE.
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

    # ...and no other agent's database was created along the way.
    assert [p.name for p in db_dir().glob("*.db")] == ["jack.turn.db"]


@pytest.mark.asyncio
async def test_opening_a_loop_fails_when_the_db_server_is_gone(hub) -> None:
    """A missing db server is a broken system, not a degraded one.

    Asked before anything is spent rather than at the write: the point of the
    guard is to find out *before* a model call has been paid for, not once the
    answer exists and has nowhere to go.

    The store here is a URL nothing is listening on, which is what a `slife2
    down` looks like from the agent server's side.  Port 9 rather than the
    configured 8010, which would make this pass or fail on whether the developer
    happens to have slife2 running.
    """
    base = default_config()
    cfg = replace(
        base,
        servers={**base.servers, "db": replace(base.servers["db"], port=9)},
    )
    server = build_server(cfg, hub_client=hub, backend=answering("the answer"))

    with pytest.raises(ToolError, match="nothing is listening on 127.0.0.1:9"):
        await send(server, "hi", agent="jack")


# --- images ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_images_reach_the_model_as_content_parts(memory, hub) -> None:
    """A prompt with an image is a list of parts, not a string."""
    from slife2.config import ModelSettings, ProviderSettings

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
    server = build_server(vision, memory_client=memory, hub_client=hub, backend=backend)
    await send(server, "what is this?", images=["data:image/png;base64,AAAA"])

    sent = backend.calls[0][0][-1]
    assert isinstance(sent.content, list)
    assert sent.content[0] == {"type": "text", "text": "what is this?"}
    assert sent.content[1]["image_url"]["url"].endswith("AAAA")


@pytest.mark.asyncio
async def test_images_are_refused_by_a_model_that_cannot_read_them(memory, hub) -> None:
    """Dropping an attachment somebody made is worse than saying no.

    The config listing only `text` under `input` is the config saying so, and
    the alternative is a model that quietly ignores what was sent.  Note that
    the *default* model is a vision model, so this has to be built explicitly —
    which is the point: the check reads the config rather than assuming.
    """
    from slife2.config import ModelSettings, ProviderSettings

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
    with pytest.raises(Exception, match="cannot read images"):
        await send(
            build_server(
                text_only, memory_client=memory, hub_client=hub, backend=answering("ok")
            ),
            "look",
            images=["data:image/png;base64,AAAA"],
        )


# --- the system prompt -------------------------------------------------------


def template(tmp_path, body: str):
    """A system prompt template on disk, as the config expects."""
    path = tmp_path / "system.j2"
    path.write_text(body, encoding="utf-8")
    return str(path)


@pytest.mark.asyncio
async def test_the_system_prompt_is_a_template(tmp_path, memory, hub) -> None:
    backend = answering("ok")
    server = build_server(
        config(system_prompt=template(tmp_path, "be terse")),
        memory_client=memory,
        hub_client=hub,
        backend=backend,
    )
    await send(server, "hi")

    seen = backend.calls[0][0]
    assert seen[0].role == "system"
    assert seen[0].content == "be terse"


@pytest.mark.asyncio
async def test_the_template_is_rendered_with_the_loops_own_agent_name(
    tmp_path, memory, hub
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
        memory_client=memory,
        hub_client=hub,
        backend=backend,
    )

    await send(server, "hello", agent="jack")
    await send(server, "hello", agent="jill")

    assert backend.calls[0][0][0].content == "You are jack."
    assert backend.calls[1][0][0].content == "You are jill."


@pytest.mark.asyncio
async def test_a_missing_template_is_refused_at_load(tmp_path) -> None:
    """A prompt that will not render is a config mistake worth naming once."""
    from slife2.config import ConfigError, load

    path = tmp_path / "slife2.yaml"
    path.write_text("agent:\n  system_prompt: not-here.j2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not-here.j2"):
        load(path)


# --- limits ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_steps_comes_from_the_config(memory, hub) -> None:
    backend = FakeBackend(
        *[
            ScriptedTurn(
                result=StreamChatResult(
                    tool_calls=(ToolCall(id=f"c{i}", name="now", arguments={}),),
                    stop_reason="tool_calls",
                )
            )
            for i in range(2)
        ]
    )
    server = build_server(
        config(max_steps=2), memory_client=memory, hub_client=hub, backend=backend
    )
    result = await send(server, "loop forever")

    assert len(backend.calls) == 2
    assert result.data["stop_reason"] == "max_steps"


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
async def test_events_arrive_as_progress_notifications(memory, hub) -> None:
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=tool_then_answer()
    )
    seen: list[TurnEvent] = []

    async def on_progress(progress, total, message):
        event = decode(message or "")
        assert event is not None, f"not one of our payloads: {message!r}"
        seen.append(event)

    await send_with_progress(server, "what is 6*7?", on_progress)

    kinds = [type(e).__name__ for e in seen]
    assert kinds[0] == "TextDelta"
    assert "ToolCallStarted" in kinds
    assert "ToolCallFinished" in kinds
    assert kinds[-1] == "TurnFinished"

    finished = seen[-1]
    assert finished.text == "It is 42."  # type: ignore[union-attr]
    assert finished.steps == 2  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_progress_values_are_a_monotonic_counter(memory, hub) -> None:
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=tool_then_answer()
    )
    values: list[float] = []

    async def on_progress(progress, total, message):
        values.append(progress)
        assert total is None, "inventing a total would render a fake percentage"

    await send_with_progress(server, "x", on_progress)

    assert values == sorted(values)
    assert len(set(values)) == len(values)


@pytest.mark.asyncio
async def test_a_turn_works_without_a_progress_handler(memory, hub) -> None:
    """A non-streaming client gets the same answer and the server does no extra work."""
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=tool_then_answer()
    )
    result = await send(server, "x")
    assert result.data["text"] == "It is 42."


@pytest.mark.asyncio
async def test_report_progress_is_a_no_op_without_a_token() -> None:
    """Asserted directly because the failure it guards is invisible.

    `report_progress` checks for a progress token itself.  If that ever changed,
    every non-streaming client would start raising — and the symptom would look
    like a bug in the loop, not in the observer.
    """

    class NoTokenContext:
        async def report_progress(self, *args, **kwargs):
            return None

    from slife2.events import TextDelta

    observer = ProgressObserver(NoTokenContext())  # type: ignore[arg-type]
    await observer.on_event(TextDelta("x"))  # must not raise


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
        await asyncio.wait_for(serve_task, timeout=5)
        sock.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_progress_streams_over_real_http(memory, hub) -> None:
    """The one test that binds a port for the streaming contract.

    The in-memory transport cannot prove that progress notifications survive
    HTTP framing, and `json_response=True` would break them silently while still
    returning correct results — so this asserts they arrive *during* the call
    with more than one of them, not as a single buffered dump at the end.
    """
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=tool_then_answer()
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
async def test_a_cancelled_turn_is_repaired_over_real_http(memory, hub) -> None:
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
    backend = FakeBackend(text_turn("never finished", delay=0.4), text_turn("fine"))
    server = build_server(
        config(), memory_client=memory, hub_client=hub, backend=backend
    )

    async with over_http(server) as url:
        async with Client(url) as client:
            interrupted = asyncio.create_task(
                client.call_tool(
                    "send_message", {"agent": DEFAULT_AGENT, "prompt": "one"}
                )
            )
            for _ in range(200):
                if backend.calls:
                    break
                await asyncio.sleep(0.01)
            assert backend.calls, "the turn never reached the model"

            interrupted.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
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
