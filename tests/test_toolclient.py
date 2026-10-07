"""The agent's half of the toolhub hop: a listed tool becomes a runnable one.

Two contracts meet here — the payload the hub sends and the shape the loop
expects — so this file tests both ends of the same round trip: mostly against a
client that answers the way the hub does, and once against the real hub, which
is what keeps the two halves from drifting apart.
"""

from __future__ import annotations

import types
from dataclasses import replace
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools import Tool

from slife2.builtins import build_server as build_builtins
from slife2.config import Config, ToolServerSettings, default_config
from slife2.messages import ToolCall
from slife2.toolclient import (
    CALL_TOOL,
    LIST_TOOLS,
    UpstreamTool,
    remote_tools,
)
from slife2.toolhub import build_server as build_hub
from slife2.tools import ToolFailed, ToolRegistry

pytestmark = pytest.mark.unit


def answer(payload: Any) -> Any:
    """What `Client.call_tool` hands back for a tool that returned a mapping."""
    return types.SimpleNamespace(data=payload, content=[], is_error=False)


class FakeHub:
    """A client that answers the way the hub does, and records what it was asked.

    The client half only ever calls `call_tool`, so this is the whole of the
    surface it needs — and it lets one case be expressed exactly: a hub that
    answers with something this build cannot read.
    """

    def __init__(self, *, tools: list[dict[str, Any]] | None = None, junk: Any = None):
        self.tools = (
            tools
            if tools is not None
            else [
                UpstreamTool(
                    name="fake__echo",
                    server="fake",
                    tool="echo",
                    description="[fake] Echo it back.",
                    parameters={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                ).to_wire()
            ]
        )
        self.junk = junk
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
        self.calls.append((name, arguments or {}))
        if name == LIST_TOOLS:
            return answer(self.junk if self.junk is not None else {"tools": self.tools})
        if name == CALL_TOOL:
            return answer({"text": "answered", "ok": True})
        raise AssertionError(f"the agent asked for {name!r}")


# --- the list becomes tools ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_listed_tool_becomes_a_tool_the_loop_can_run() -> None:
    hub = FakeHub()
    tools = await remote_tools(hub)

    assert [tool.spec.name for tool in tools] == ["fake__echo"]
    assert tools[0].spec.description == "[fake] Echo it back."
    assert "text" in tools[0].spec.parameters["properties"]
    assert hub.calls == [(LIST_TOOLS, {})]


@pytest.mark.asyncio
async def test_running_one_calls_back_through_the_same_connection() -> None:
    hub = FakeHub()
    registry = ToolRegistry(await remote_tools(hub))

    text, ok = await registry.execute(
        ToolCall(id="c1", name="fake__echo", arguments={"text": "hi"})
    )

    assert (text, ok) == ("answered", True)
    assert hub.calls[-1] == (
        CALL_TOOL,
        {"name": "fake__echo", "arguments": {"text": "hi"}},
    )


@pytest.mark.asyncio
async def test_a_failed_call_is_a_failure_not_a_success_with_bad_news() -> None:
    """The distinction the `ok` flag carries, all the way to the transcript.

    A tool that returned "Error: ..." as an ordinary result would render as a
    call that worked, with a discouraging message under it.
    """

    class Refusing(FakeHub):
        async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
            if name == CALL_TOOL:
                return answer({"text": "the peer said no", "ok": False})
            return await super().call_tool(name, arguments)

    registry = ToolRegistry(await remote_tools(Refusing()))
    text, ok = await registry.execute(ToolCall(id="c1", name="fake__echo"))

    # Rendered bare — no exception class in front of a message that is already
    # the whole story.  See `slife2.tools.ToolFailed`.
    assert (text, ok) == ("Error: the peer said no", False)


@pytest.mark.asyncio
async def test_a_refusal_with_no_text_still_says_which_tool() -> None:
    class Silent(FakeHub):
        async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
            if name == CALL_TOOL:
                return answer({"text": "", "ok": False})
            return await super().call_tool(name, arguments)

    registry = ToolRegistry(await remote_tools(Silent()))
    text, ok = await registry.execute(ToolCall(id="c1", name="fake__echo"))

    assert ok is False
    assert "fake__echo" in text


# --- a hub this build cannot read ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "junk",
    [
        {},
        {"tools": "not a list"},
        {"something": "else"},
    ],
)
async def test_a_payload_this_build_cannot_read_is_loud(junk: Any) -> None:
    """Never an empty tool list.

    The likely cause is a hub daemon left over from a previous build, and
    quietly handing the model a short list turns a version mismatch into a model
    that has lost its abilities without anyone being told.
    """
    with pytest.raises(ConnectionError, match="slife2 down"):
        await remote_tools(FakeHub(junk=junk))


def test_a_listed_tool_tolerates_what_it_does_not_understand() -> None:
    """`parameters` is the one field with a shape; the rest is text."""
    tool = UpstreamTool.from_wire({"name": "a__b", "parameters": "nonsense"})
    assert tool.parameters == {}
    assert (tool.server, tool.tool) == ("", "")


# --- the two halves, against each other --------------------------------------


def hub_with_upstream() -> FastMCP:
    """A real hub over a real (in-memory) upstream server."""
    upstream = FastMCP("upstream")

    @upstream.tool
    def echo(text: str) -> str:
        """Echo it back."""
        return text

    config: Config = replace(
        default_config(),
        tools={"fake": ToolServerSettings(name="fake", command="in-memory")},
    )
    return build_hub(
        config,
        transports={
            "fake": lambda settings: upstream,
            "builtins": lambda settings: build_builtins(default_config()),
        },
    )


@pytest.mark.asyncio
async def test_the_two_halves_agree_over_a_real_hop() -> None:
    """One round trip, both contracts: the hub's payload and the loop's tools.

    The names, the schemas and the dispatch are all crossing MCP here rather
    than being read off a dict this file wrote itself.
    """
    async with Client(hub_with_upstream()) as hub:
        registry = ToolRegistry(await remote_tools(hub))
        assert [spec.name for spec in registry.specs] == [
            "builtins__echo",
            "builtins__now",
            "builtins__calc",
            "fake__echo",
        ]
        text, ok = await registry.execute(
            ToolCall(id="c1", name="fake__echo", arguments={"text": "through"})
        )
        assert (text, ok) == ("through", True)

        # A builtin, through the hub, through the builtins server: two hops and
        # the same answer, which is what makes them not a special case.
        text, ok = await registry.execute(
            ToolCall(id="c2", name="builtins__calc", arguments={"e": "2+2"})
        )
        assert (text, ok) == ("4", True)


@pytest.mark.asyncio
async def test_a_hub_that_is_the_wrong_build_says_so() -> None:
    """The same loudness, reached through a real connection rather than a fake."""
    hub = FastMCP("not the hub")

    @hub.tool
    def list_tools() -> str:
        """Answers, but not with what the agent expects."""
        return "hello"

    async with Client(hub) as client:
        with pytest.raises(ConnectionError, match="slife2 down"):
            await remote_tools(client)


def test_tool_failed_and_an_ordinary_raise_are_rendered_differently() -> None:
    """The one exception whose class name adds nothing to its message."""
    assert issubclass(ToolFailed, Exception)
    assert Tool.__name__ == "Tool"
