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
import socket
from dataclasses import replace

import pytest
from fakes import FakeBackend, ScriptedTurn
from fastmcp import Client, FastMCP

from slife2.config import default_config
from slife2.events import TurnEvent, decode
from slife2.llm.base import Chunk, Stream
from slife2.messages import StreamChatResult, ToolCall
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


async def call(server: FastMCP, **arguments):
    async with Client(server) as client:
        return await client.call_tool("run_turn", arguments)


# --- the surface -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_server_exposes_exactly_one_tool() -> None:
    """`reset` is gone: with no server-side history there is nothing to reset."""
    async with Client(build_server(config(), backend=FakeBackend())) as client:
        tools = await client.list_tools()
    assert [t.name for t in tools] == ["run_turn"]


@pytest.mark.asyncio
async def test_run_turn_returns_the_final_text() -> None:
    server = build_server(config(), backend=tool_then_answer())
    result = await call(server, messages=[], prompt="what is 6*7?")
    assert result.data["text"] == "It is 42."


@pytest.mark.asyncio
async def test_run_turn_reports_what_the_caller_must_remember() -> None:
    server = build_server(config(), backend=tool_then_answer())
    result = await call(server, messages=[], prompt="what is 6*7?")

    roles = [m["role"] for m in result.data["new_messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert result.data["steps"] == 2
    assert result.data["stop_reason"] == "stop"


# --- statelessness -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_server_remembers_nothing_between_calls() -> None:
    """Two identical calls from an empty history behave identically.

    This is the property that makes the design stateless: if the server kept
    anything, the second call would see the first.
    """
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first")),
        ScriptedTurn(result=StreamChatResult(text="second")),
    )
    server = build_server(config(), backend=backend)

    first = await call(server, messages=[], prompt="hi")
    second = await call(server, messages=[], prompt="hi")

    assert first.data["text"] == "first"
    assert second.data["text"] == "second"
    # The second call saw only its own prompt, not the first exchange.
    for messages, _ in backend.calls:
        assert [m.role for m in messages if m.role != "system"] == ["user"]


@pytest.mark.asyncio
async def test_one_server_serves_two_instances_without_mixing_them() -> None:
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

    server = build_server(config(), backend=EchoBackend())

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
async def test_history_sent_by_the_caller_is_used() -> None:
    """The caller owns memory; sending it back is what continues a conversation."""
    backend = answering("second answer")
    server = build_server(config(), backend=backend)

    history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "first answer"},
    ]
    await call(server, messages=history, prompt="two")

    seen = backend.calls[0][0]
    assert [m.role for m in seen] == ["system", "user", "assistant", "user"]
    assert seen[2].content == "first answer"


@pytest.mark.asyncio
async def test_a_full_round_trip_carries_the_conversation() -> None:
    """Feed each result's `new_messages` back in, as a client would."""
    backend = FakeBackend(
        ScriptedTurn(result=StreamChatResult(text="first answer")),
        ScriptedTurn(result=StreamChatResult(text="second answer")),
    )
    server = build_server(config(), backend=backend)

    history: list[dict] = []
    first = await call(server, messages=history, prompt="one")
    history.extend(first.data["new_messages"])
    await call(server, messages=history, prompt="two")

    seen = backend.calls[1][0]
    assert [m.role for m in seen] == ["system", "user", "assistant", "user"]
    assert seen[2].content == "first answer"


@pytest.mark.asyncio
async def test_an_interrupted_turn_leaves_the_caller_history_intact() -> None:
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
    server = build_server(config(), backend=backend)
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
async def test_the_system_prompt_is_a_template(tmp_path) -> None:
    backend = answering("ok")
    server = build_server(
        config(system_prompt=template(tmp_path, "be terse")), backend=backend
    )
    await call(server, messages=[], prompt="hi")

    seen = backend.calls[0][0]
    assert seen[0].role == "system"
    assert seen[0].content == "be terse"


@pytest.mark.asyncio
async def test_the_template_is_rendered_per_turn_with_the_agent_name(tmp_path) -> None:
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
async def test_the_system_prompt_is_not_handed_back_to_the_caller(tmp_path) -> None:
    """Otherwise it would accumulate one copy per turn in the caller's history."""
    server = build_server(
        config(system_prompt=template(tmp_path, "be terse")), backend=answering("ok")
    )
    result = await call(server, messages=[], prompt="hi")
    assert all(m["role"] != "system" for m in result.data["new_messages"])


@pytest.mark.asyncio
async def test_a_caller_supplied_system_message_is_not_doubled(tmp_path) -> None:
    backend = answering("ok")
    server = build_server(
        config(system_prompt=template(tmp_path, "from config")), backend=backend
    )
    await call(
        server, messages=[{"role": "system", "content": "from caller"}], prompt="hi"
    )

    seen = backend.calls[0][0]
    assert [m.content for m in seen if m.role == "system"] == ["from caller"]


# --- limits ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_steps_comes_from_the_config() -> None:
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
    server = build_server(config(max_steps=2), backend=backend)
    result = await call(server, messages=[], prompt="loop forever")

    assert len(backend.calls) == 2
    assert result.data["stop_reason"] == "max_steps"


# --- the streaming contract --------------------------------------------------


@pytest.mark.asyncio
async def test_events_arrive_as_progress_notifications() -> None:
    server = build_server(config(), backend=tool_then_answer())
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
async def test_progress_values_are_a_monotonic_counter() -> None:
    server = build_server(config(), backend=tool_then_answer())
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
async def test_a_turn_works_without_a_progress_handler() -> None:
    """A non-streaming client gets the same answer and the server does no extra work."""
    server = build_server(config(), backend=tool_then_answer())
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
async def test_progress_streams_over_real_http() -> None:
    """The one test that binds a port.

    The in-memory transport cannot prove that progress notifications survive
    HTTP framing, and `json_response=True` would break them silently while still
    returning correct results — so this asserts they arrive *during* the call
    with more than one of them, not as a single buffered dump at the end.
    """
    server = build_server(config(), backend=tool_then_answer())
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
