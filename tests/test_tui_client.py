"""What the TUI puts on the wire.

`MCPAgentClient` is the one component with nothing else watching it — every TUI
test injects a stand-in — and that blind spot has now cost two parameters:
`--model` reached the window title and nothing else, and an image was read,
turned into a `data:` URL, handed to this class, and dropped.  Both were
invisible from either end, because the client honoured its own signature and the
server honoured its own schema.

So this is a test of the *payload*: which tool is called, which keys go out, and
what is in them.  The transport underneath is stubbed, because the wire is not
the part that was wrong.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from slife2 import mcp_server as mcp_server_module
from slife2.tui.client import MCPAgentClient

pytestmark = pytest.mark.unit


class FakeTransport:
    """The MCP client `MCPAgentClient` would have built, with the wire stubbed."""

    def __init__(
        self, tools: tuple[str, ...] = ("send_message",), *, name: str | None = None
    ) -> None:
        self.tools = tools
        #: What the handshake reported this server calls itself.  `None` is a
        #: server that reported no name — the case `identifies` falls back to
        #: `tools` for — so the default exercises that path.
        self.server_info = SimpleNamespace(name=name) if name else None
        #: Every (tool, arguments) pair handed to `call_tool`, in order.
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeTransport:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_tools(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=name) for name in self.tools]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> SimpleNamespace:
        # Copied, because a real transport serialises the payload and a fake
        # that kept the reference would let a later mutation show up in a
        # recorded call.
        self.sent.append((name, copy.deepcopy(arguments)))
        if name == "reset":
            return SimpleNamespace(data={"reset": True})
        return SimpleNamespace(data={"text": "ok", "model": "fake/model"})


def install(monkeypatch, transport: FakeTransport) -> None:
    """Point the client at a stubbed transport instead of a URL.

    Patched in `slife2.mcp_server` rather than here: the client no longer builds
    its own `Client` — `open_server` does, for every component in this system —
    so that module is where the construction happens and therefore where a test
    has to stand in for it.
    """
    monkeypatch.setattr(mcp_server_module, "Client", lambda *a, **k: transport)


def sent(transport: FakeTransport, tool: str) -> list[dict[str, Any]]:
    return [arguments for name, arguments in transport.sent if name == tool]


@pytest.mark.asyncio
async def test_a_turn_names_the_client_it_speaks_for(monkeypatch) -> None:
    """The client id *is* the conversation, so it goes out with every message.

    There is no second call to make first: the server starts a conversation
    under a key it has not seen, so naming the key is the whole of addressing
    one.  That is what removed the handle — and with it the "your conversation
    is gone" path every caller used to need.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient(
        "http://test/mcp", agent="jack", model="deepseek/deepseek-flash"
    )

    await client.connect()
    await client.run_turn("hi", lambda event: None, images=["data:image/png;base64,AA"])

    (arguments,) = sent(transport, "send_message")
    assert arguments == {
        "agent": "jack",
        "subagent": "",
        "prompt": "hi",
        "channel": "human",
        "model": "deepseek/deepseek-flash",
        "images": ["data:image/png;base64,AA"],
    }
    assert client.client_id == ("jack", "")


@pytest.mark.asyncio
async def test_a_payload_says_nothing_it_does_not_have_to(monkeypatch) -> None:
    """An absent key and an empty one mean the same thing to the server — it
    chooses — so the payload sends neither rather than both spellings."""
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack")

    await client.connect()
    await client.run_turn("hi", lambda event: None)

    (arguments,) = sent(transport, "send_message")
    assert "model" not in arguments
    assert "images" not in arguments
    # The channel is not optional: this client exists to serve somebody typing,
    # and that is a fact about the caller rather than something it may lack.
    assert arguments["channel"] == "human"


@pytest.mark.asyncio
async def test_a_subagent_name_is_carried_as_part_of_the_id(monkeypatch) -> None:
    """The second half of the id is what makes a worker a separate conversation.

    A TUI is never a worker, so it does not pass one — but the id it sends has
    the same shape either way, which is what lets every server key on one thing.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack", subagent="helper")

    await client.connect()
    await client.run_turn("hi", lambda event: None)

    assert client.client_id == ("jack", "helper")
    assert sent(transport, "send_message")[0]["subagent"] == "helper"


@pytest.mark.asyncio
async def test_resetting_names_the_same_id(monkeypatch) -> None:
    """Forgetting a conversation is a command about a key, not a handle.

    Nothing about this client changes: the next message under the same id simply
    begins a conversation the way the first one did.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack")
    await client.connect()
    await client.run_turn("hi", lambda event: None)

    await client.reset()
    await client.run_turn("after reset", lambda event: None)

    assert sent(transport, "reset") == [{"agent": "jack", "subagent": ""}]
    assert client.client_id == ("jack", "")
    assert len(sent(transport, "send_message")) == 2


@pytest.mark.asyncio
async def test_a_failed_turn_keeps_the_connection(monkeypatch) -> None:
    """A `ToolError` is the server answering, not the transport dying.

    Dropping the connection here would turn a bad API key into a reconnect storm
    on every retry.
    """
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack")
    await client.connect()

    async def explode(name, arguments, **kwargs):
        if name == "send_message":
            raise ToolError("the API key did not resolve")
        return await FakeTransport.call_tool(transport, name, arguments, **kwargs)

    monkeypatch.setattr(transport, "call_tool", explode)

    with pytest.raises(ToolError, match="API key"):
        await client.run_turn("hi", lambda event: None)
    assert client.client_id == ("jack", "")


@pytest.mark.asyncio
async def test_a_server_that_is_not_ours_is_refused(monkeypatch) -> None:
    """The URL may point at some other MCP server, and saying so beats a turn
    that fails later with a message about an unknown tool.

    This transport reports no name, so the check falls back to the tool list —
    which is the weaker check, and the one that has to keep working.
    """
    install(monkeypatch, FakeTransport(tools=("stream_chat",)))
    client = MCPAgentClient("http://test/mcp")

    with pytest.raises(ConnectionError, match="not slife2-agent"):
        await client.connect()


@pytest.mark.asyncio
async def test_a_server_that_says_it_is_something_else_is_refused(monkeypatch) -> None:
    """The name the handshake reports decides on its own.

    It is not a hint to combine with the tool list: a server that calls itself
    something else is not ours even if it happens to expose a tool by that name,
    which is exactly what a *stale* build of one of our own servers looks like.
    """
    install(monkeypatch, FakeTransport(name="someone-elses-server"))
    client = MCPAgentClient("http://test/mcp")

    with pytest.raises(ConnectionError, match="not slife2-agent"):
        await client.connect()


@pytest.mark.asyncio
async def test_a_named_server_is_accepted_without_listing_tools(monkeypatch) -> None:
    """The common case: the handshake already answered, so nothing more is asked.

    `tools` is deliberately wrong here — if the name matches, the tool list is
    never consulted, and asserting that is how the round trip stays saved.
    """
    transport = FakeTransport(tools=("nothing_like_it",), name="slife2-agent")
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp")

    await client.connect()
    await client.run_turn("hi", lambda event: None)
    assert sent(transport, "send_message")[0]["prompt"] == "hi"
