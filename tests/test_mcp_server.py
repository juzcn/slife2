"""The shared MCP layer: identity, and the house rules every server applies.

`identifies` is what decides whether a URL is pointing at the server we meant,
and three plugins depend on it — the launcher, the agent loop's backend, and
the TUI.  The tests here are split between the fake (which can say what was
*not* asked) and a real server over the in-memory transport (which is the only
thing that proves the name is really on the wire, since a fake can be told to
report anything).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP

from slife2 import mcp_server as mcp_server_module
from slife2.mcp_server import house_server, identifies, open_server

pytestmark = pytest.mark.unit


class CountingClient:
    """A client that records whether the tool list was asked for."""

    def __init__(self, *, name: str | None, tools: tuple[str, ...] = ()) -> None:
        self.server_info = SimpleNamespace(name=name) if name else None
        self.tools = list(tools)
        self.list_calls = 0

    async def list_tools(self) -> list[SimpleNamespace]:
        self.list_calls += 1
        return [SimpleNamespace(name=n) for n in self.tools]


@pytest.mark.asyncio
async def test_a_matching_name_is_enough_and_costs_no_round_trip() -> None:
    """The whole reason for reading the handshake rather than listing tools.

    The tool list is deliberately empty — if the name decides, a client that
    exposes nothing still identifies, and `list_tools` is never called.  That
    `list_calls == 0` is the point of the test, not a detail.
    """
    client = CountingClient(name="slife2-db")
    assert await identifies(client, "slife2-db", fallback_tool="remember")
    assert client.list_calls == 0


@pytest.mark.asyncio
async def test_a_server_calling_itself_something_else_is_not_ours() -> None:
    """A name is not a hint to combine with the tool list.

    This is what a stale build of one of our own servers looks like: the right
    tool, the wrong build.  Accepting it on the strength of the tool name is how
    a signature change turns into a refusal on every turn.
    """
    client = CountingClient(name="someone-else", tools=("remember",))
    assert not await identifies(client, "slife2-db", fallback_tool="remember")
    assert client.list_calls == 0


@pytest.mark.asyncio
async def test_no_name_falls_back_to_the_tool_list() -> None:
    """A synthesized or absent identity is a real case, so the fallback is
    load-bearing: a client pinned to an exact modern protocol version reports an
    empty name, and a server from another implementation may report none."""
    present = CountingClient(name=None, tools=("remember", "turn_list"))
    absent = CountingClient(name=None, tools=("stream_chat",))

    assert await identifies(present, "slife2-db", fallback_tool="remember")
    assert not await identifies(absent, "slife2-db", fallback_tool="remember")
    assert present.list_calls == 1 and absent.list_calls == 1


@pytest.mark.asyncio
async def test_a_real_server_reports_the_name_it_was_built_with() -> None:
    """The claim the whole scheme rests on, measured rather than assumed.

    `identifies` reads `Client.server_info`, which is populated from the
    handshake the client performs as part of connecting.  A fake can be told to
    report a name; only a real server shows that one is actually there.
    """
    mcp = house_server("slife2-db", instructions="Keep what was said.")
    async with Client(mcp) as client:
        assert await identifies(client, "slife2-db", fallback_tool="remember")
        assert not await identifies(client, "slife2-agent", fallback_tool="remember")


@pytest.mark.asyncio
async def test_house_server_keeps_error_details() -> None:
    """FastMCP's default replaces an exception's message with a generic one.

    Every server here listens on loopback and serves its own operator, and the
    message is the actionable half — "model not found" and "the API key did not
    resolve" are the two things a user actually has to act on.  Asserted through
    a real call rather than on an attribute, because the attribute is FastMCP's
    business and what matters is what reaches the caller.
    """
    mcp: FastMCP = house_server("slife2-test")

    @mcp.tool
    async def refuses() -> str:
        raise ValueError("model not found")

    with pytest.raises(Exception, match="model not found"):
        async with Client(mcp) as client:
            await client.call_tool("refuses", {})


def test_house_server_carries_instructions_and_omits_empty_ones() -> None:
    """The MCP field for global guidance: passed through when there is some, and
    absent rather than empty when there is not — the spec's own framing is that
    no instructions beat poorly written ones."""
    assert house_server("slife2-test", instructions="Read me.").instructions == (
        "Read me."
    )
    assert not house_server("slife2-test").instructions


# --- connecting: the rule every client follows -------------------------------


class StubTransport:
    """The MCP client `open_server` would have built."""

    def __init__(self, *, name: str | None = None, tools: tuple[str, ...] = ()) -> None:
        self.server_info = SimpleNamespace(name=name) if name else None
        self.tools = list(tools)
        self.closed = False

    async def __aenter__(self) -> StubTransport:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def list_tools(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=n) for n in self.tools]


def stub(monkeypatch, transport: StubTransport) -> None:
    """Stand in for the transport, on a URL with no port.

    No port because the TCP gate runs before anything is constructed, and this
    URL is not somewhere a socket could be opened anyway.
    """
    monkeypatch.setattr(mcp_server_module, "Client", lambda *a, **k: transport)


@pytest.mark.asyncio
async def test_a_port_with_nothing_on_it_is_refused_before_the_protocol() -> None:
    """Ask the port first, because asking the protocol costs seconds.

    Port 9 is the discard port: nothing answers it by convention.  The message
    is the assertion because only the gate can produce it — a transport failure
    would say the client could not connect, which is the thing this exists to
    avoid waiting two seconds for.
    """
    with pytest.raises(ConnectionError, match="nothing is listening on 127.0.0.1:9"):
        await open_server("http://127.0.0.1:9/mcp", name="slife2-db")


@pytest.mark.asyncio
async def test_a_connected_server_we_did_not_ask_for_is_refused_and_closed(
    monkeypatch,
) -> None:
    """Being reachable is not the same as being the one we meant."""
    transport = StubTransport(name="someone-elses-server")
    stub(monkeypatch, transport)

    with pytest.raises(ConnectionError, match="not slife2-db"):
        await open_server("http://test/mcp", name="slife2-db")

    assert transport.closed, "a client we are not keeping has to be released"


@pytest.mark.asyncio
async def test_a_server_we_recognise_is_handed_back_still_open(monkeypatch) -> None:
    """The other half: what comes back is the caller's to keep and to close."""
    transport = StubTransport(name="slife2-db")
    stub(monkeypatch, transport)

    client = await open_server("http://test/mcp", name="slife2-db")

    assert client is transport
    assert not transport.closed
