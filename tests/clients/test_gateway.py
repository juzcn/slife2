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
from mcp import MCPError

import slife2.helpers.timeouts as _T
from slife2.clients import gateway
from slife2.clients.gateway import (
    LINK_DOWN_CODE,
    Connection,
    mcp_config,
    proxied_name,
    sanitise,
)
from slife2.libs.host.config import ToolServerSettings

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
async def test_a_listing_that_has_aged_is_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What replaced the notification, now that it cannot be relied on.

    2026-07-28 moved change notifications onto a `subscriptions/listen` stream
    the *client* has to open, and this build's client cannot open one — so an
    answer that stopped being true has to be re-read on a clock instead.  The
    bound is set to zero here, which is the one thing a test can do and a session
    cannot: make the very next ask already overdue.

    What it must not cost is the *tool*: the aged listing stays usable while it is
    being replaced, so a source never blinks out of the model's list because its
    answer got old.
    """
    monkeypatch.setattr(_T.timeouts.pacing, "relist_after", 0.0)
    seen = Recorder()
    link = connection(seen)
    assert await link.ready() is True

    link.connecting()
    assert link.usable is True, "aged is not the same as unusable"
    attempt = link.attempt
    assert attempt is not None, "an aged listing is due to be read again"
    await attempt

    assert len(seen.listings) == 2, "and reading it again hands it over again"
    assert link.error == "", "a re-list that worked leaves no error behind"
    await link.close()


@pytest.mark.asyncio
async def test_a_listing_that_is_fresh_is_not_read_again() -> None:
    """And it is the age that decides, not the ask.

    Every step of every turn asks for the tool list, so re-reading per ask would
    put one `tools/list` per source inside each of them for an answer that did
    not change — which is the cost the bound exists to refuse.
    """
    seen = Recorder()
    link = connection(seen)
    assert await link.ready() is True

    for _ in range(3):
        link.connecting()
        assert link.attempt is None, "a current listing is not due for another"

    assert len(seen.listings) == 1
    await link.close()


@pytest.mark.asyncio
async def test_a_call_does_not_reread_an_aged_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call is by the far end's own name, so the age changes nothing about it.

    The peer resolves the name; nothing on this side consults the listing.  So a
    call must not wait behind a re-list, which is what would happen if `ready`
    refreshed — it is the question calls ask.
    """
    monkeypatch.setattr(_T.timeouts.pacing, "relist_after", 0.0)
    seen = Recorder()
    link = connection(seen)
    assert await link.ready() is True

    assert await link.call("echo", {"text": "hi"}) == ("hi", True)
    assert len(seen.listings) == 1, "the call read no listing"
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

    Nothing about a real transport can be made to fail on demand, so the client
    is what is faked and the transport is a placeholder nothing looks at.

    `dies` raises `CONNECTION_CLOSED`, **which is the SDK's code for two
    different situations**: a send onto a transport that is already torn down,
    and a request that was in flight when the read stream ended (`mcp/shared/
    jsonrpc_dispatcher.py`'s `_fan_out_closed` wakes every pending waiter with
    it, and the streamable HTTP transport synthesises it for an SSE stream that
    ended with no answer).  The fake is ambiguous on purpose, because the real
    exception is — which is why the connection may not read it as "nothing was
    asked"; see `test_a_call_whose_answer_was_lost_is_not_run_again`.
    """

    def __init__(self, *, dies: bool, refuses: bool, times_out: bool = False) -> None:
        self.dies = dies
        self.refuses = refuses
        self.times_out = times_out
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
            # The code a dead link really reports (`slife2.clients.gateway.
            # LINK_DOWN_CODE`) — and the code a request that was in flight when
            # the link went reports too, which is what the connection may not
            # treat as "nothing was asked".  A generic exception is a third case
            # and must not drop the link at all — see the test below.
            raise MCPError(code=LINK_DOWN_CODE, message="Connection closed")
        if self.times_out:
            raise TimeoutError("the call did not answer")
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text="answered")], is_error=self.refuses
        )


def clients(*, dies: bool = False, refuses: bool = False, times_out: bool = False):
    """A client factory, and the list of clients it built."""
    made: list[DeadClient] = []

    def make(_transport: Any, _handler: Any) -> Any:
        made.append(DeadClient(dies=dies, refuses=refuses, times_out=times_out))
        return made[-1]

    return make, made


@pytest.mark.asyncio
async def test_a_call_whose_answer_was_lost_is_not_run_again() -> None:
    """**The code does not say nothing was asked**, and reading it as if it did
    ran a model's tool twice.

    `CONNECTION_CLOSED` is raised for a send onto a transport that is already
    torn down *and* — with the same code — to wake every request that was in
    flight when the read stream ended (`mcp/shared/jsonrpc_dispatcher.py`'s
    `_fan_out_closed`), and the streamable HTTP transport synthesises it for an
    SSE stream that ended with no answer.  Both are reachable through a real
    call: an `mcp:` HTTP upstream whose response stream is cut mid-call, and a
    stdio child that dies mid-tool after its output file was written.

    Since the two cannot be told apart, the attempted call is answered as a
    failure and never re-run — the second execution is a second file write, a
    second render, a second message sent — and the dead link is dropped so the
    *next* ask rebuilds it.
    """
    make, made = clients(dies=True)
    link = connection(client_factory=make)

    text, ok = await link.call("echo", {})

    assert ok is False
    assert "Connection closed" in text, "the far end's failure, as a value"
    assert len(made) == 1, "not rebuilt: the request may have reached the far end"
    assert made[0].calls == 1, "and certainly not run a second time"
    await link.close()


@pytest.mark.asyncio
async def test_a_link_that_died_is_rebuilt_for_the_next_call() -> None:
    """The source does not stay broken: what the call dropped, the next ask builds.

    That is what "the source stops counting as usable when a call fails at the
    transport" costs — one call is answered with the failure, and the ask after
    it reconnects and re-lists rather than every call finding the same dead
    client.
    """
    make, made = clients(dies=True)
    link = connection(client_factory=make)

    assert (await link.call("echo", {}))[1] is False
    assert link.usable is False, "the source stopped counting as usable"

    assert (await link.call("echo", {}))[1] is False
    assert len(made) == 2, "the second call reconnected, on a link that died too"
    await link.close()


@pytest.mark.asyncio
async def test_a_timeout_is_neither_rebuilt_nor_retried() -> None:
    """**A call that timed out was still made**, so it is never tried again.

    The rebuild rule is about a link that was down *before* the call went out —
    that is what makes trying again safe.  A timeout is the other case: the
    request went out and the answer did not come back, so the far end may still
    be running it, and calling again is a second `file_save`, a second video
    render, a second message sent.

    **The sentence is no longer this module's to write**, and that is the change
    the assertion records: there is no clock here any more (`slife2.clients.gateway`'s
    docstring argues why), so a timeout that arrives at this layer is the far
    end's own — the caller's `_timeout` is what produces the "did not answer
    within" sentence, at the layer that owns it.
    """
    make, made = clients(times_out=True)
    link = connection(client_factory=make)

    text, ok = await link.call("echo", {})

    assert ok is False
    assert "TimeoutError" in text, "the far end's failure, as a value"
    assert len(made) == 1, "not rebuilt: the call may have run"
    assert made[0].calls == 1, "and certainly not called again"


@pytest.mark.asyncio
async def test_a_failure_that_is_not_the_link_is_not_retried() -> None:
    """**The narrow branch is the whole point of the rule.**

    A generic exception — something the far end threw, a wrapper this process
    does not recognise — may have been produced *after* the call ran, so trying
    again is a second execution of something that already happened.  It is
    answered instead, and the connection is left alone: the call that finds the
    dispatcher refusing a send is the one that earns the rebuild.
    """

    class Throws(DeadClient):
        """A client that connects and lists, and then fails in its call."""

        #: Counted on the class so the test can read it after the factory has
        #: built and dropped the instance.
        calls = 0

        async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
            Throws.calls += 1
            raise RuntimeError("the far end threw somewhere in the middle")

    link = connection(
        client_factory=lambda _transport, _handler: Throws(dies=False, refuses=False)
    )

    text, ok = await link.call("echo", {})

    assert ok is False
    assert "the far end threw" in text
    assert Throws.calls == 1, "not called again: it may already have run"
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


def test_the_legal_alphabet_is_ascii() -> None:
    """**`isalnum` is not the rule a provider implements.**

    It is Unicode-aware, so `搜索` passed through untouched and 400'd every
    request in that conversation — the exact failure `sanitise` exists to
    prevent, and one the punctuation cases above never reached.
    """
    assert sanitise("搜索") == "__"
    assert sanitise("café") == "caf_", "a letter outside ASCII is still outside"
    assert sanitise("read_file-2") == "read_file-2", "and the legal set survives"


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

    source = Path(gateway.__file__).read_text(encoding="utf-8")
    assert "sqlite3" not in source
    assert "from slife2.libs.store.db import" not in source
    assert "import slife2.libs.store.db" not in source
    assert "Catalogue" not in source


def test_the_gateway_names_no_time_value_that_does_not_exist() -> None:
    """The docstring told a reader to look up a symbol defined nowhere.

    A name in prose is read as a thing to go and find, so `LIST_SETTLE_SECONDS`
    — which the prose claimed `list_tools` waited on — sent the reader on an
    errand and hid what actually bounds the wait (the asker's brief patience for
    the attempts it started, `_T.timeouts.ready.list_settle`).  The registry is
    the one seat for a time value; a name that lives only in a sentence is a
    second seat for a number nobody can find.
    """

    source = Path(gateway.__file__).read_text(encoding="utf-8")
    assert "LIST_SETTLE_SECONDS" not in source
    assert "CALL_TIMEOUT_SECONDS" not in source


@pytest.mark.asyncio
async def test_the_connect_budget_is_not_a_deadline_on_a_call(monkeypatch) -> None:
    """**A connect budget is not a client's patience**, and it was read as one.

    `Client(timeout=…)` is not this module's to set: FastMCP turns it into the
    session's read timeout, which the SDK applies as a **total deadline on every
    request made over that connection** — so pinning it to the 60 s connect
    budget capped every call to somebody else's server at a minute, whatever the
    caller had asked for.  A twenty-minute render called with the model's
    `_timeout: 600` died at 60 s as an ordinary-looking `ok=False`, with the tool
    still running at the far end, and the module's own prose ("no clock lives
    here") was what it contradicted.

    Asserted the way it was found: shrink the registry value — the documented way
    to tune it, and the reason consumers read it at call time — and then make a
    call that outlives it.  A client-wide deadline coming back would fail this
    with a timeout naming a number nobody asked for.
    """
    slow = FastMCP("slow")

    @slow.tool
    def render(seconds: float = 0.0) -> str:
        """Take longer than the budget below, the way a real render does."""
        import time

        time.sleep(seconds)
        return "rendered"

    monkeypatch.setattr(_T.timeouts.ready, "connect_tools", 0.2)

    client = gateway.make_client(slow, lambda _message: None)
    async with client:
        result = await client.call_tool(
            "render", {"seconds": 0.6}, raise_on_error=False
        )

    assert "rendered" in gateway.flatten(result.content)
