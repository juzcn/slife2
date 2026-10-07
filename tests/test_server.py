"""The agent MCP server.

Almost everything here runs over FastMCP's **in-memory transport**, which binds
no port and needs no LLM — `build_server(..., backend=...)` takes a scripted
backend, so the whole server including its streaming contract is exercised
without I/O.  That is why these are marked `unit` rather than `integration`:
they do not touch the network, and marking them otherwise would make `-m unit`
stop meaning "fast".

One test does bind a real socket, because the in-memory transport cannot prove
that progress notifications survive HTTP.  That one is `integration`.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace

import pytest
import pytest_asyncio
from fakes import FakeBackend, ScriptedTurn
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2.config import default_config
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
    """A memory server that actually answers, over the in-memory transport.

    Every turn writes one, and a turn **cannot run without it**: a memory server
    that is not there is a broken system rather than a degraded one, so
    `run_turn` fails before it spends anything on a model call.  See
    `slife2.mcp_server.open_server`.

    The real server rather than a stub, so what these tests exercise is the
    component the agent server actually talks to — the one test that is *about*
    the failure passes its own client instead.
    """
    from slife2.memory_server import build_server as build_memory

    async with Client(build_memory(default_config())) as client:
        yield client


async def call(server: FastMCP, **arguments):
    async with Client(server) as client:
        return await client.call_tool("run_turn", arguments)


# --- the surface -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_server_exposes_exactly_one_tool(memory) -> None:
    """`reset` is gone: with no server-side history there is nothing to reset."""
    async with Client(
        build_server(config(), memory_client=memory, backend=FakeBackend())
    ) as client:
        tools = await client.list_tools()
    assert [t.name for t in tools] == ["run_turn"]


@pytest.mark.asyncio
async def test_run_turn_returns_the_final_text(memory) -> None:
    server = build_server(config(), memory_client=memory, backend=tool_then_answer())
    result = await call(server, messages=[], prompt="what is 6*7?")
    assert result.data["text"] == "It is 42."


@pytest.mark.asyncio
async def test_run_turn_reports_what_the_caller_must_remember(memory) -> None:
    server = build_server(config(), memory_client=memory, backend=tool_then_answer())
    result = await call(server, messages=[], prompt="what is 6*7?")

    roles = [m["role"] for m in result.data["new_messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert result.data["steps"] == 2
    assert result.data["stop_reason"] == "stop"


# --- statelessness -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_server_remembers_nothing_between_calls(memory) -> None:
    """Two identical calls from an empty history behave identically.

    This is the property that makes the design stateless: if the server kept
    anything, the second call would see the first.
    """
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first")),
        ScriptedTurn(result=StreamChatResult(text="second")),
    )
    server = build_server(config(), memory_client=memory, backend=backend)

    first = await call(server, messages=[], prompt="hi")
    second = await call(server, messages=[], prompt="hi")

    assert first.data["text"] == "first"
    assert second.data["text"] == "second"
    # The second call saw only its own prompt, not the first exchange.
    for messages, _ in backend.calls:
        assert [m.role for m in messages if m.role != "system"] == ["user"]


@pytest.mark.asyncio
async def test_one_server_serves_two_instances_without_mixing_them(memory) -> None:
    """Two instances, one agent server on one port, no cross-talk.

    This is why the agent server can be a single process: it keeps no state, so
    a second instance is not a second server.  Each caller sends its whole
    history and gets an answer computed from that history alone.

    Asserted concurrently on purpose — sequentially it would pass even if the
    server kept something, because the second call would simply overwrite it.
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

    server = build_server(config(), memory_client=memory, backend=EchoBackend())

    async with Client(server) as first, Client(server) as second:
        one, two = await asyncio.gather(
            first.call_tool("run_turn", {"messages": [], "prompt": "alpha"}),
            second.call_tool("run_turn", {"messages": [], "prompt": "beta"}),
        )

    assert one.data["text"] == "reply to alpha"
    assert two.data["text"] == "reply to beta"
    # ...and neither answer contains the other's prompt.
    assert "beta" not in one.data["text"]
    assert "alpha" not in two.data["text"]


@pytest.mark.asyncio
async def test_a_turn_is_written_to_memory(tmp_path, monkeypatch) -> None:
    """The turn lands in the caller's own database, and nowhere else.

    Both halves matter.  Written at all, because a memory component that nothing
    calls is a component that does nothing; and written to *that agent's* file,
    because isolation between agents is the reason the file is per-agent in the
    first place.

    Every column is checked, not just the two obvious ones.  A column that is
    silently empty because nobody wired it up looks exactly like a column that
    is empty because there was nothing to put in it, and only an assertion can
    tell those apart.
    """
    import sqlite3

    from slife2.memory_server import build_server as build_memory
    from slife2.paths import DATA_ENV_VAR, turns_dir

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

    async with Client(build_memory(cfg)) as memory_client:
        server = build_server(cfg, backend=backend, memory_client=memory_client)
        async with Client(server) as client:
            await client.call_tool(
                "run_turn",
                {
                    "messages": [],
                    "prompt": "what is 2+2?",
                    "agent": "jack",
                    "channel": "human",
                },
            )

    jack = turns_dir() / "jack.turn.db"
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
    turns_messages = json.loads(stored)
    assert [m["role"] for m in turns_messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert turns_messages[0]["content"] == "what is 2+2?"

    # ...and no other agent's database was created along the way.
    assert [p.name for p in turns_dir().glob("*.db")] == ["jack.turn.db"]


@pytest.mark.asyncio
async def test_a_turn_fails_when_the_memory_server_is_gone() -> None:
    """A missing memory server is a broken system, not a degraded one.

    The store here is a URL nothing is listening on, which is what a `slife2
    down` looks like from the agent server's side.  The turn is *refused* rather
    than answered-and-not-recorded, which is what the CLI already does at
    startup — `slife2` will not draw anything if a component will not come up,
    so a component that goes missing later takes the same answer.  See
    `slife2.mcp_server.open_server`.

    Port 9 rather than the configured 8010, which would make this pass or fail
    on whether the developer happens to have slife2 running.
    """
    base = default_config()
    cfg = replace(
        base,
        servers={**base.servers, "memory": replace(base.servers["memory"], port=9)},
    )
    server = build_server(cfg, backend=answering("the answer"))

    with pytest.raises(ToolError, match="nothing is listening on 127.0.0.1:9"):
        await call(server, messages=[], prompt="hi", agent="jack")


@pytest.mark.asyncio
async def test_images_reach_the_model_as_content_parts(memory) -> None:
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
    server = build_server(vision, memory_client=memory, backend=backend)
    await call(
        server,
        messages=[],
        prompt="what is this?",
        images=["data:image/png;base64,AAAA"],
    )

    sent = backend.calls[0][0][-1]
    assert isinstance(sent.content, list)
    assert sent.content[0] == {"type": "text", "text": "what is this?"}
    assert sent.content[1]["image_url"]["url"].endswith("AAAA")


@pytest.mark.asyncio
async def test_images_are_refused_by_a_model_that_cannot_read_them(memory) -> None:
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
        await call(
            build_server(text_only, memory_client=memory, backend=answering("ok")),
            messages=[],
            prompt="look",
            images=["data:image/png;base64,AAAA"],
        )


@pytest.mark.asyncio
async def test_history_sent_by_the_caller_is_used(memory) -> None:
    """The caller owns memory; sending it back is what continues a conversation."""
    backend = answering("second answer")
    server = build_server(config(), memory_client=memory, backend=backend)

    history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "first answer"},
    ]
    await call(server, messages=history, prompt="two")

    seen = backend.calls[0][0]
    assert [m.role for m in seen] == ["system", "user", "assistant", "user"]
    assert seen[2].content == "first answer"


@pytest.mark.asyncio
async def test_a_full_round_trip_carries_the_conversation(memory) -> None:
    """Feed each result's `new_messages` back in, as a client would."""
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first answer")),
        ScriptedTurn(result=StreamChatResult(text="second answer")),
    )
    server = build_server(config(), memory_client=memory, backend=backend)

    history: list[dict] = []
    first = await call(server, messages=history, prompt="one")
    history.extend(first.data["new_messages"])
    await call(server, messages=history, prompt="two")

    seen = backend.calls[1][0]
    assert [m.role for m in seen] == ["system", "user", "assistant", "user"]
    assert seen[2].content == "first answer"


@pytest.mark.asyncio
async def test_an_interrupted_turn_leaves_the_caller_history_intact(memory) -> None:
    """Cancellation needs no repair, because nothing shared was written.

    The plan called this the sharpest correctness edge in the system; making the
    caller own the history removes it rather than solving it.
    """
    backend = FakeBackend(
        ScriptedTurn(
            result=StreamChatResult(text="never"),
            chunks=[Chunk(text="partial")],
            delay=5.0,
        ),
        ScriptedTurn(result=StreamChatResult(text="fine")),
    )
    server = build_server(config(), memory_client=memory, backend=backend)
    history: list[dict] = [{"role": "user", "content": "earlier"}]

    async with Client(server) as client:
        task = asyncio.create_task(
            client.call_tool("run_turn", {"messages": history, "prompt": "slow"})
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task

    # The caller's list is untouched — it never handed over ownership.
    assert history == [{"role": "user", "content": "earlier"}]
    # And the server is not poisoned: a fresh call still works.
    result = await call(server, messages=[], prompt="again")
    assert result.data["text"] == "fine"


# --- the system prompt -------------------------------------------------------


@pytest.mark.asyncio
def template(tmp_path, body: str):
    """A system prompt template on disk, as the config expects."""
    path = tmp_path / "system.j2"
    path.write_text(body, encoding="utf-8")
    return str(path)


@pytest.mark.asyncio
async def test_the_system_prompt_is_a_template(tmp_path, memory) -> None:
    backend = answering("ok")
    server = build_server(
        config(system_prompt=template(tmp_path, "be terse")),
        memory_client=memory,
        backend=backend,
    )
    await call(server, messages=[], prompt="hi")

    seen = backend.calls[0][0]
    assert seen[0].role == "system"
    assert seen[0].content == "be terse"


@pytest.mark.asyncio
async def test_the_template_is_rendered_per_turn_with_the_agent_name(
    tmp_path, memory
) -> None:
    """One template, personalised by whoever is asking.

    Rendered per turn rather than once, because the agent name arrives *with the
    request* — the server is shared, so two instances are two names asking one
    process.  Rendering at startup would hand the first caller's name to
    everybody, which is the failure this asserts against.
    """
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="ok")),
        ScriptedTurn(result=StreamChatResult(text="ok")),
    )
    server = build_server(
        config(system_prompt=template(tmp_path, "You are {{ agent_name }}.")),
        memory_client=memory,
        backend=backend,
    )

    await call(server, messages=[], prompt="hello", agent="jack")
    await call(server, messages=[], prompt="hello", agent="jill")

    first = backend.calls[0][0][0]
    second = backend.calls[1][0][0]
    assert first.content == "You are jack."
    assert second.content == "You are jill."


@pytest.mark.asyncio
async def test_a_missing_template_is_refused_at_load(tmp_path) -> None:
    """A prompt that will not render is a config mistake worth naming once."""
    from slife2.config import ConfigError, load

    path = tmp_path / "slife2.yaml"
    path.write_text("agent:\n  system_prompt: not-here.j2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not-here.j2"):
        load(path)


@pytest.mark.asyncio
async def test_the_system_prompt_is_not_handed_back_to_the_caller(
    tmp_path, memory
) -> None:
    """Otherwise it would accumulate one copy per turn in the caller's history."""
    server = build_server(
        config(system_prompt=template(tmp_path, "be terse")),
        memory_client=memory,
        backend=answering("ok"),
    )
    result = await call(server, messages=[], prompt="hi")
    assert all(m["role"] != "system" for m in result.data["new_messages"])


@pytest.mark.asyncio
async def test_a_caller_supplied_system_message_is_not_doubled(
    tmp_path, memory
) -> None:
    backend = answering("ok")
    server = build_server(
        config(system_prompt=template(tmp_path, "from config")),
        memory_client=memory,
        backend=backend,
    )
    await call(
        server, messages=[{"role": "system", "content": "from caller"}], prompt="hi"
    )

    seen = backend.calls[0][0]
    assert [m.content for m in seen if m.role == "system"] == ["from caller"]


# --- limits ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_steps_comes_from_the_config(memory) -> None:
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
    server = build_server(config(max_steps=2), memory_client=memory, backend=backend)
    result = await call(server, messages=[], prompt="loop forever")

    assert len(backend.calls) == 2
    assert result.data["stop_reason"] == "max_steps"


# --- the streaming contract --------------------------------------------------


@pytest.mark.asyncio
async def test_events_arrive_as_progress_notifications(memory) -> None:
    server = build_server(config(), memory_client=memory, backend=tool_then_answer())
    seen: list[TurnEvent] = []

    async def on_progress(progress, total, message):
        event = decode(message or "")
        assert event is not None, f"not one of our payloads: {message!r}"
        seen.append(event)

    async with Client(server) as client:
        await client.call_tool(
            "run_turn",
            {"messages": [], "prompt": "what is 6*7?"},
            progress_handler=on_progress,
        )

    kinds = [type(e).__name__ for e in seen]
    assert kinds[0] == "TextDelta"
    assert "ToolCallStarted" in kinds
    assert "ToolCallFinished" in kinds
    assert kinds[-1] == "TurnFinished"

    finished = seen[-1]
    assert finished.text == "It is 42."  # type: ignore[union-attr]
    assert finished.steps == 2  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_progress_values_are_a_monotonic_counter(memory) -> None:
    server = build_server(config(), memory_client=memory, backend=tool_then_answer())
    values: list[float] = []

    async def on_progress(progress, total, message):
        values.append(progress)
        assert total is None, "inventing a total would render a fake percentage"

    async with Client(server) as client:
        await client.call_tool(
            "run_turn", {"messages": [], "prompt": "x"}, progress_handler=on_progress
        )

    assert values == sorted(values)
    assert len(set(values)) == len(values)


@pytest.mark.asyncio
async def test_a_turn_works_without_a_progress_handler(memory) -> None:
    """A non-streaming client gets the same answer and the server does no extra work."""
    server = build_server(config(), memory_client=memory, backend=tool_then_answer())
    result = await call(server, messages=[], prompt="x")
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_progress_streams_over_real_http(memory) -> None:
    """The one test that binds a port.

    The in-memory transport cannot prove that progress notifications survive
    HTTP framing, and `json_response=True` would break them silently while still
    returning correct results — so this asserts they arrive *during* the call
    with more than one of them, not as a single buffered dump at the end.
    """
    server = build_server(config(), memory_client=memory, backend=tool_then_answer())
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

        arrivals: list[tuple[float, str]] = []

        async def on_progress(progress, total, message):
            event = decode(message or "")
            if event is not None:
                arrivals.append((progress, type(event).__name__))

        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            result = await client.call_tool(
                "run_turn",
                {"messages": [], "prompt": "what is 6*7?"},
                progress_handler=on_progress,
            )

        assert result.data["text"] == "It is 42."
        assert len(arrivals) > 1
        assert arrivals[-1][1] == "TurnFinished"
        assert "ToolCallStarted" in [name for _, name in arrivals]
    finally:
        uv.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5)
        sock.close()
