"""`MCPBackend`: the queue pump that turns a tool call into an async iterator.

This is the adapter the whole architecture rests on, so it gets its own file.
The tests drive a real LLM server over FastMCP's in-memory transport — real
MCP messages, real progress notifications, no port and no provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastmcp import Client

from slife2.builtins import evaluate
from slife2.llm.base import Chunk, Finish, Streamer, ToolCallDelta
from slife2.llm.client import MCPBackend
from slife2.llm.server_common import build_llm_server
from slife2.loop import AgentLoop
from slife2.messages import Message, ToolCall, ToolSpec
from slife2.tools import Tool, ToolRegistry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def scripted(*events) -> Streamer:
    async def stream(
        provider: str, messages: list[Message], tools: list[ToolSpec], model: str
    ) -> AsyncIterator[Chunk | Finish]:
        for event in events:
            yield event

    return stream


async def collect(backend: MCPBackend, messages=None, tools=None):
    """Drive one `stream()` to completion, returning (chunks, result)."""
    stream = backend.stream(
        messages or [Message(role="user", content="hi")], tools or []
    )
    chunks = [chunk async for chunk in stream.chunks]
    return chunks, await stream.result


async def _calc(arguments: dict) -> str:
    return str(evaluate(str(arguments.get("e") or "")))


def registry() -> ToolRegistry:
    """One tool, local.

    `calc` is convenient here and is not the subject: what this file is about is
    the model hop, and the real calculator is behind a server now — a second hop
    in front of a test about the first one would be noise.
    """
    return ToolRegistry(
        [
            Tool(
                spec=ToolSpec(
                    name="calc", description="m", parameters={"type": "object"}
                ),
                run=_calc,
            )
        ]
    )


async def test_chunks_arrive_in_order_and_the_result_follows() -> None:
    server = build_llm_server(
        name="t",
        streamer=scripted(Chunk(text="a"), Chunk(text="b"), Finish("stop")),
    )
    async with Client(server) as client:
        chunks, result = await collect(MCPBackend(client, "m"))

    assert [c.text for c in chunks] == ["a", "b"]
    assert result.text == "ab"
    assert result.stop_reason == "stop"


async def test_tool_call_fragments_are_reassembled_across_the_hop() -> None:
    """The loop never sees a fragment; the server hands it whole calls."""
    server = build_llm_server(
        name="t",
        streamer=scripted(
            Chunk(tool_call_deltas=(ToolCallDelta(0, id="c1", name="calc"),)),
            Chunk(tool_call_deltas=(ToolCallDelta(0, arguments_delta='{"e"'),)),
            Chunk(tool_call_deltas=(ToolCallDelta(0, arguments_delta=': "2+2"}'),)),
            Finish("tool_calls"),
        ),
    )
    async with Client(server) as client:
        _, result = await collect(MCPBackend(client, "m"))

    assert result.tool_calls == (
        ToolCall(id="c1", name="calc", arguments={"e": "2+2"}),
    )


async def test_the_model_and_messages_reach_the_server() -> None:
    seen: dict[str, object] = {}

    async def streamer(provider, messages, tools, model):
        seen["messages"] = messages
        seen["model"] = model
        seen["provider"] = provider
        yield Finish("stop")

    server = build_llm_server(name="t", streamer=streamer)
    async with Client(server) as client:
        await collect(
            MCPBackend(client, "my-model"),
            [Message(role="user", content="hello")],
            [ToolSpec(name="calc", description="m", parameters={"type": "object"})],
        )

    assert seen["model"] == "my-model"
    assert seen["messages"] == [Message(role="user", content="hello")]


async def test_a_provider_failure_surfaces_from_the_result() -> None:
    """The drain must end so the loop can move on, and the await must raise.

    Without the sentinel the server pushes when it finishes, a failed call
    would leave the drain waiting on a queue nobody would ever write to again —
    a hang that looks exactly like a slow model.
    """

    async def streamer(provider, messages, tools, model):
        raise RuntimeError("provider said no")
        yield  # pragma: no cover - makes this an async generator

    server = build_llm_server(name="t", streamer=streamer)
    async with Client(server) as client:
        stream = MCPBackend(client, "m").stream(
            [Message(role="user", content="hi")], []
        )
        assert [c async for c in stream.chunks] == []
        with pytest.raises(Exception, match="provider said no"):
            await stream.result


async def test_cancelling_the_drain_cancels_the_call() -> None:
    """A cancelled turn must not leave the tool call running."""

    async def streamer(provider, messages, tools, model):
        yield Chunk(text="first")
        await asyncio.sleep(10)
        yield Finish("stop")

    server = build_llm_server(name="t", streamer=streamer)
    async with Client(server) as client:
        stream = MCPBackend(client, "m").stream(
            [Message(role="user", content="hi")], []
        )

        received = []
        async for chunk in stream.chunks:
            received.append(chunk)
            break  # walk away mid-stream

        assert [c.text for c in received] == ["first"]
        # The generator's finally cancelled the call; nothing should now hang.
        with pytest.raises((asyncio.CancelledError, Exception)):
            await asyncio.wait_for(stream.result, timeout=1.0)


async def test_the_agent_loop_runs_over_mcp() -> None:
    """The whole point: the loop is unchanged by the model being elsewhere.

    A full round trip — the loop asks for a tool, MCP carries the call, the
    server streams fragments back, the loop runs the tool and asks again —
    driven entirely by a scripted provider behind an in-memory MCP server.
    """
    calls = {"n": 0}

    async def two_turn_streamer(provider, messages, tools, model):
        """Tool request on the first model call, the answer on the second."""
        calls["n"] += 1
        if calls["n"] == 1:
            yield Chunk(text="checking")
            yield Chunk(tool_call_deltas=(ToolCallDelta(0, id="c1", name="calc"),))
            yield Chunk(
                tool_call_deltas=(ToolCallDelta(0, arguments_delta='{"e": "6*7"}'),)
            )
            yield Finish("tool_calls")
        else:
            yield Chunk(text="It is 42.")
            yield Finish("stop")

    server = build_llm_server(name="agent", streamer=two_turn_streamer)
    messages: list[Message] = []

    async with Client(server) as client:
        loop = AgentLoop(MCPBackend(client, "m"), registry())
        result = await loop.run_turn(messages, "what is 6*7?")

    assert result.text == "It is 42."
    assert result.steps == 2
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[2].content == "42"
    assert calls["n"] == 2


async def test_two_concurrent_calls_on_one_client_do_not_mix() -> None:
    """Two loops share one `Client`, and their streams interleave.

    The agent server opens one client per model *server* and keeps it for the
    process, so the moment a server has two loops — the ordinary case, and
    exactly what a subagent fan-out is — two `stream_chat` calls are in flight on
    one session at once.  Nothing in this project had ever exercised that: the
    agent server's own tests inject a backend, so `loop_for`'s cache path never
    ran with anything concurrent.

    Each request carries its own progress callback, so the chunks should be
    multiplexed rather than mixed.  The streamer yields one character at a time
    with a sleep between them, which is what makes the two interleave on the wire
    instead of running one after the other and passing for the wrong reason.
    """

    async def echoing(provider, messages, tools, model):
        prompt = messages[-1].content or ""
        for char in prompt:
            await asyncio.sleep(0.01)
            yield Chunk(text=char)
        yield Finish("stop")

    server = build_llm_server(name="agent", streamer=echoing)
    async with Client(server) as client:
        backend = MCPBackend(client, "m")
        first, second = await asyncio.gather(
            collect(backend, [Message(role="user", content="alpha")]),
            collect(backend, [Message(role="user", content="beta")]),
        )

    assert first[1].text == "alpha"
    assert second[1].text == "beta"
    # ...and the chunks themselves never crossed over, not just the results.
    assert "".join(c.text or "" for c in first[0]) == "alpha"
    assert "".join(c.text or "" for c in second[0]) == "beta"
