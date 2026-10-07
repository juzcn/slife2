"""What the TUI puts on the wire.

`MCPAgentClient` is the one component with nothing else watching it — every TUI
test injects a stand-in — and that blind spot has now cost two parameters:
`--model` reached the window title and nothing else, and an image was read,
turned into a `data:` URL, handed to this class, and dropped.  Both were
invisible from either end, because the client honoured its own signature and the
server honoured its own schema.

So this is a test of the *payload*: which keys go out and what is in them.  The
transport underneath is stubbed, because the wire is not the part that was
wrong.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from slife2 import mcp_server as mcp_server_module
from slife2.tui.client import MCPAgentClient

pytestmark = pytest.mark.unit


class FakeTransport:
    """The MCP client `MCPAgentClient` would have built, with the wire stubbed."""

    def __init__(
        self, tools: tuple[str, ...] = ("run_turn",), *, name: str | None = None
    ) -> None:
        self.tools = tools
        #: What the handshake reported this server calls itself.  `None` is a
        #: server that reported no name — the case `identifies` falls back to
        #: `tools` for — so the default exercises that path.
        self.server_info = SimpleNamespace(name=name) if name else None
        #: Every argument dict handed to `call_tool`, in order.
        self.sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeTransport:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_tools(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=name) for name in self.tools]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> SimpleNamespace:
        # Copied, because `arguments["messages"]` *is* the client's history list
        # and the client extends it in place as soon as this returns.  A real
        # transport serialises the payload and has no such problem; a fake that
        # keeps the reference would show every past turn carrying the present
        # conversation.
        self.sent.append(copy.deepcopy(arguments))
        return SimpleNamespace(
            data={"text": "ok", "new_messages": [{"role": "assistant"}]}
        )


def install(monkeypatch, transport: FakeTransport) -> None:
    """Point the client at a stubbed transport instead of a URL.

    Patched in `slife2.mcp_server` rather than here: the client no longer builds
    its own `Client` — `open_server` does, for every component in this system —
    so that module is where the construction happens and therefore where a test
    has to stand in for it.
    """
    monkeypatch.setattr(mcp_server_module, "Client", lambda *a, **k: transport)


@pytest.mark.asyncio
async def test_a_turn_carries_everything_the_caller_gave_it(monkeypatch) -> None:
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient(
        "http://test/mcp", agent="jack", model="deepseek/deepseek-flash"
    )

    await client.connect()
    await client.run_turn("hi", lambda event: None, images=["data:image/png;base64,AA"])

    (sent,) = transport.sent
    assert sent == {
        "messages": [],
        "prompt": "hi",
        "agent": "jack",
        "channel": "human",
        "model": "deepseek/deepseek-flash",
        "images": ["data:image/png;base64,AA"],
    }


@pytest.mark.asyncio
async def test_a_payload_says_nothing_it_does_not_have_to(monkeypatch) -> None:
    """An absent key and an empty one mean the same thing to the server — it
    chooses — so the payload sends neither rather than both spellings."""
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack")

    await client.connect()
    await client.run_turn("hi", lambda event: None)

    (sent,) = transport.sent
    assert "model" not in sent
    assert "images" not in sent
    # The channel is not optional: this client exists to serve somebody typing,
    # and that is a fact about the caller rather than something it may lack.
    assert sent["channel"] == "human"


@pytest.mark.asyncio
async def test_the_conversation_grows_by_what_the_server_returned(monkeypatch) -> None:
    transport = FakeTransport()
    install(monkeypatch, transport)
    client = MCPAgentClient("http://test/mcp", agent="jack")
    await client.connect()

    assert await client.run_turn("hi", lambda event: None) == "ok"
    await client.run_turn("again", lambda event: None)

    # The second turn carries what the first one produced: the server keeps no
    # state, so this list is the entire conversation.
    assert transport.sent[1]["messages"] == [{"role": "assistant"}]

    client.reset()
    await client.run_turn("after reset", lambda event: None)
    assert transport.sent[2]["messages"] == []


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
    assert transport.sent[0]["prompt"] == "hi"
