"""slife2-gateway: the link to a server somebody else runs.

What is tested here is the connection as a thing in itself — connect, list, call,
report health — because that is the whole of what the module claims to be.  The
two callbacks are the contract with whoever uses it: a listing is *handed over*
rather than kept, and a failure is *reported* rather than interpreted.  Anything
about what a tool list becomes is the caller's, and is tested where the caller
lives.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP

from slife2.config import ToolServerSettings
from slife2.gateway import (
    Connection,
    mcp_config,
    proxied_name,
    sanitise,
)

pytestmark = pytest.mark.unit


def server() -> FastMCP:
    """A stand-in for somebody else's MCP server."""
    mcp = FastMCP("upstream")

    @mcp.tool
    def echo(text: str) -> str:
        """Echo it back."""
        return text

    @mcp.tool
    def boom() -> str:
        """Always fails, the way a real tool refuses a request."""
        raise ValueError("the peer said no")

    return mcp


def settings(name: str = "fake", **kwargs: Any) -> ToolServerSettings:
    return ToolServerSettings(name=name, command="in-memory", **kwargs)


class Recorder:
    """The two callbacks, kept as the lists the assertions read."""

    def __init__(self) -> None:
        self.listings: list[list[Any]] = []
        self.failures: list[Exception] = []

    async def listed(self, tools: list[Any]) -> None:
        self.listings.append(tools)

    def failed(self, exc: Exception) -> None:
        self.failures.append(exc)


def connection(recorder: Recorder | None = None, **kwargs: Any) -> Connection:
    """One link to the stand-in, with whatever the test wants recorded."""
    seen = recorder or Recorder()
    return Connection(
        settings(),
        transport=lambda _settings: server(),
        on_listed=seen.listed,
        on_failed=seen.failed,
        **kwargs,
    )


# --- connecting, and what comes back ------------------------------------------


@pytest.mark.asyncio
async def test_a_listing_is_handed_over_and_not_kept() -> None:
    """The seam, stated as a test: the caller records, this module forgets.

    A connection that kept the tool list would be the caller's second copy of
    the catalogue, which is the thing the whole arrangement exists to avoid.
    """
    seen = Recorder()
    link = connection(seen)

    assert await link.ready() is True
    assert [tool.name for tool in seen.listings[0]] == ["echo", "boom"]
    assert link.usable is True
    assert link.state == "ready"
    assert link.error == ""
    assert not hasattr(link, "tools"), "the listing is the caller's, not ours"

    await link.close()


@pytest.mark.asyncio
async def test_a_changed_tool_list_is_read_again() -> None:
    """What the peer's `tools/list_changed` leads to, without the notification.

    Nothing polls: the listing is dropped and the next ask re-reads it — which
    is also what makes the source stop counting as *usable* for one ask, so a
    caller gating on liveness holds its rows back until it has answered again.
    """
    seen = Recorder()
    link = connection(seen)
    assert await link.ready() is True

    link.invalidate()
    assert link.usable is False
    assert link.state != "ready"

    assert await link.ready() is True
    assert link.state == "ready"
    assert len(seen.listings) == 2, "the second ask re-listed and re-handed"
    await link.close()


@pytest.mark.asyncio
async def test_a_server_that_cannot_be_reached_is_reported_not_raised() -> None:
    """A connect failure is this server's problem, and it is kept as an answer.

    Nothing about a server that will not start should reach a caller as an
    exception: it is a source with no tools, and what it said is the useful fact.
    """

    def refuses(_settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("no such program: npx")

    seen = Recorder()
    link = Connection(settings(), transport=refuses, on_failed=seen.failed)

    assert await link.ready() is False
    assert link.usable is False
    assert link.state == "failed"
    assert "no such program" in link.error
    assert len(seen.failures) == 1
    assert isinstance(seen.failures[0], FileNotFoundError)
    await link.close()


@pytest.mark.asyncio
async def test_a_call_with_no_link_says_so_rather_than_raising() -> None:
    def refuses(_settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("gone")

    link = Connection(settings(), transport=refuses)
    text, ok = await link.call("echo", {"text": "hi"})

    assert ok is False
    assert "not connected" in text
    await link.close()


# --- calling ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_call_reaches_the_far_end() -> None:
    link = connection()
    try:
        assert await link.call("echo", {"text": "hello"}) == ("hello", True)
    finally:
        await link.close()


@pytest.mark.asyncio
async def test_a_refusal_is_a_value_and_not_a_broken_link() -> None:
    """The peer answered.  "The tool said no" and "the link is gone" are the two
    things this contract exists to keep apart."""
    link = connection()
    try:
        text, ok = await link.call("boom", {})
    finally:
        await link.close()

    assert ok is False
    assert "the peer said no" in text


@pytest.mark.asyncio
async def test_a_tool_the_far_end_does_not_have_is_its_answer_too() -> None:
    link = connection()
    try:
        text, ok = await link.call("nothing", {})
    finally:
        await link.close()

    assert ok is False
    assert text, "an answer, not an empty string"


class DeadClient:
    """A client whose calls fail the way a dead link does.

    The rebuild rule is about a link that dies *during a call*, and nothing about
    a real transport can be made to fail on demand — so the client is what is
    faked, and the transport is a placeholder nothing looks at.
    """

    def __init__(self, *, dies: bool, refuses: bool) -> None:
        self.dies = dies
        self.refuses = refuses
        self.calls = 0

    async def __aenter__(self) -> DeadClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        return [
            types.SimpleNamespace(
                name="echo", description="Echo.", input_schema={"type": "object"}
            )
        ]

    async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
        self.calls += 1
        if self.dies:
            raise ConnectionError("the link is gone")
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text="answered")], is_error=self.refuses
        )


def clients(*, dies: bool = False, refuses: bool = False, first_dies: bool = False):
    """A client factory, and the list of clients it built."""
    made: list[DeadClient] = []

    def make(_transport: Any, _handler: Any) -> Any:
        client = DeadClient(dies=dies or (first_dies and not made), refuses=refuses)
        if first_dies and made:
            client.dies = False
        made.append(client)
        return client

    return make, made


@pytest.mark.asyncio
async def test_a_link_that_dies_mid_call_is_rebuilt_once() -> None:
    """Once, and it succeeds when the second link is good.

    A tool that never ran is worth a second attempt; nothing about a real
    transport can be made to fail on demand, which is why the client is a seam.
    """
    make, made = clients(first_dies=True)
    link = connection(client_factory=make)

    assert await link.call("echo", {}) == ("answered", True)
    assert len(made) == 2, "one connection, then one rebuild"
    await link.close()


@pytest.mark.asyncio
async def test_a_server_that_is_down_costs_two_timeouts_not_more() -> None:
    """The other half of "once": a dead server must not be retried forever."""
    make, made = clients(dies=True)
    link = connection(client_factory=make)

    _text, ok = await link.call("echo", {})

    assert ok is False
    assert len(made) == 2
    await link.close()


@pytest.mark.asyncio
async def test_a_refusal_does_not_rebuild_the_link() -> None:
    """The peer answered.  Rebuilding would only be told the same thing again."""
    make, made = clients(refuses=True)
    link = connection(client_factory=make)

    _text, ok = await link.call("echo", {})

    assert ok is False
    assert len(made) == 1, "a peer-reported error is not a transport failure"
    assert made[0].calls == 1
    await link.close()


# --- the two translations -----------------------------------------------------


def test_a_name_a_provider_would_reject_is_made_legal() -> None:
    """A provider rejects the whole request over one illegal tool name, with a
    message about the tool list that names no server."""
    assert sanitise("read.file") == "read_file"
    assert sanitise("a-b_c") == "a-b_c"
    assert proxied_name("my.server", "read.file") == "my_server__read_file"
    assert proxied_name("arxiv", "search") == "arxiv__search"


def test_the_config_document_carries_whichever_transport_the_entry_uses() -> None:
    """The ecosystem's own `mcpServers` shape, built from either kind of entry."""
    stdio = mcp_config(
        ToolServerSettings(
            name="stdio-one", command="npx", args=("-y", "x"), env={"K": "v"}
        ),
        cwd="/data",
    )
    assert stdio["mcpServers"]["stdio-one"] == {
        "command": "npx",
        "args": ["-y", "x"],
        "transport": "stdio",
        "cwd": "/data",
        "env": {"K": "v"},
    }

    http = mcp_config(
        ToolServerSettings(name="http-one", url="https://x.test/mcp"),
        cwd="/data",
    )
    assert http["mcpServers"]["http-one"] == {
        "url": "https://x.test/mcp",
        "transport": "http",
    }


def test_an_entry_with_no_cwd_of_its_own_gets_the_one_it_was_given() -> None:
    """`cwd` matters because an entry is likely to name a path, and `.` has to
    mean something — never the daemon's own runtime folder."""
    built = mcp_config(ToolServerSettings(name="x", command="x"), cwd="/data")
    assert built["mcpServers"]["x"]["cwd"] == "/data"

    own = mcp_config(
        ToolServerSettings(name="x", command="x", cwd="/somewhere"), cwd="/data"
    )
    assert own["mcpServers"]["x"]["cwd"] == "/somewhere"


def test_the_gateway_holds_no_catalogue_and_no_database() -> None:
    """The module's claim, as a property of its imports rather than a promise.

    What a tool list *means* is the caller's, so a `sqlite3` import here — or a
    reach into the db plugin — would be the first sign that the connection and
    the catalogue had started to overlap half way.
    """
    import slife2.gateway as gateway

    source = Path(gateway.__file__).read_text(encoding="utf-8")
    assert "sqlite3" not in source
    assert "from slife2.db import" not in source
    assert "import slife2.db" not in source
    assert "Catalogue" not in source
