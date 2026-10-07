"""The shared MCP layer: identity, and the house rules every server applies.

`identifies` is what decides whether a URL is pointing at the server we meant,
and three components depend on it — the launcher, the agent loop's backend, and
the TUI.  The tests here are split between the fake (which can say what was
*not* asked) and a real server over the in-memory transport (which is the only
thing that proves the name is really on the wire, since a fake can be told to
report anything).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP

from slife2.mcp_server import house_server, identifies

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
    client = CountingClient(name="slife2-memory")
    assert await identifies(client, "slife2-memory", fallback_tool="remember")
    assert client.list_calls == 0


@pytest.mark.asyncio
async def test_a_server_calling_itself_something_else_is_not_ours() -> None:
    """A name is not a hint to combine with the tool list.

    This is what a stale build of one of our own servers looks like: the right
    tool, the wrong build.  Accepting it on the strength of the tool name is how
    a signature change turns into a refusal on every turn.
    """
    client = CountingClient(name="someone-else", tools=("remember",))
    assert not await identifies(client, "slife2-memory", fallback_tool="remember")
    assert client.list_calls == 0


@pytest.mark.asyncio
async def test_no_name_falls_back_to_the_tool_list() -> None:
    """A synthesized or absent identity is a real case, so the fallback is
    load-bearing: a client pinned to an exact modern protocol version reports an
    empty name, and a server from another implementation may report none."""
    present = CountingClient(name=None, tools=("remember", "recent"))
    absent = CountingClient(name=None, tools=("stream_chat",))

    assert await identifies(present, "slife2-memory", fallback_tool="remember")
    assert not await identifies(absent, "slife2-memory", fallback_tool="remember")
    assert present.list_calls == 1 and absent.list_calls == 1


@pytest.mark.asyncio
async def test_a_real_server_reports_the_name_it_was_built_with() -> None:
    """The claim the whole scheme rests on, measured rather than assumed.

    `identifies` reads `Client.server_info`, which is populated from the
    handshake the client performs as part of connecting.  A fake can be told to
    report a name; only a real server shows that one is actually there.
    """
    mcp = house_server("slife2-memory", instructions="Keep what was said.")
    async with Client(mcp) as client:
        assert await identifies(client, "slife2-memory", fallback_tool="remember")
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
