"""slife2-toolhub: the model's tool list, and the servers behind it.

The hub is a *peer* server — the agent calls it and the model never sees it — so
what these tests are about is the two things it decides: what the model may
call, and what happens when the thing behind a tool is not there.  The second is
the interesting half, because there are three different failures and only one of
them is ours.
"""

from __future__ import annotations

import asyncio
import types
from dataclasses import replace
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from slife2.audience import FOR_THE_MODEL
from slife2.config import ToolServerSettings, default_config
from slife2.toolhub import (
    Upstream,
    flatten,
    make_client,
    mcp_config,
    proxied_name,
    sanitise,
)
from slife2.toolhub import (
    build_server as build_hub,
)
from tests.fakes import component_transports

pytestmark = pytest.mark.unit


# --- stand-ins ----------------------------------------------------------------


def upstream_server() -> FastMCP:
    """A stand-in for somebody else's MCP server."""
    server = FastMCP("upstream")

    @server.tool
    def echo(text: str) -> str:
        """Echo it back."""
        return text

    @server.tool
    def boom() -> str:
        """Always fails, the way a real tool refuses a request."""
        raise ValueError("the peer said no")

    # A name a provider rejects, which is what the sanitiser exists for.  A real
    # one arrives this way: nothing in MCP stops an upstream from calling a tool
    # whatever it likes.
    server.add_tool(
        Tool.from_function(lambda: "ok", name="read.file", description="Dotted.")
    )
    return server


def hub_for(
    *,
    connected: dict[str, Any] | None = None,
    entries: dict[str, ToolServerSettings] | None = None,
    **kwargs: Any,
) -> FastMCP:
    """A hub over in-db servers, keyed by entry name.

    `connected` maps a name to a *transport*, which is anything a `Client` can
    be built from and therefore always a callable: `lambda settings: server` for
    an in-memory one, or a function that raises, for the tests about a server
    that will not start.  `entries` are
    configured but *not* wired, which is how the one test that spawns a real
    process gets a real transport.

    **Every component is here, and only the builtins are real.**  They are the
    servers slife2 starts, the hub asks each of them for a tool list, and it
    refuses to hand one out at all when one does not answer — which is what
    `test_a_component_that_is_not_answering_fails_the_list` is about, and not
    something every other test should have to trip over.  The rest offer the
    model nothing, which is what a component's tools are until one of them says
    otherwise.
    """
    base = default_config()
    wired = component_transports(base, connected)
    config = replace(
        base,
        tools={
            **{
                name: ToolServerSettings(name=name, command="in-memory")
                for name in wired
                if name not in base.components()
            },
            **(entries or {}),
        },
    )
    return build_hub(config, transports=wired, **kwargs)


def names(payload: dict[str, Any]) -> set[str]:
    return {tool["name"] for tool in payload["tools"]}


async def call(hub: Client, name: str, arguments: dict[str, Any] | None = None):
    """Run one of the model's tools, the way the agent does.

    Through `call_tool` and not directly: the hub's own tool surface is three
    tools, and `{server}__{tool}` names exist only inside the list it hands out.
    """
    result = await hub.call_tool(
        "call_tool", {"name": name, "arguments": arguments or {}}
    )
    return result.data


#: A transport that is never looked at, and the marker it hands back, for the
#: tests that fake the client — `Client` is what turns a transport into a
#: connection, and those tests replace `Client`.
#:
#: It is a marker rather than `None` because a client factory is handed what the
#: transport *returned*, not the transport itself.  That is how it tells the
#: upstream it is meant to misbehave for from the ones that must keep working —
#: the builtins being the one that must, since a hub without them lists nothing.
UNUSED = object()


def unused(settings: ToolServerSettings) -> Any:
    return UNUSED


# --- what the model may call --------------------------------------------------


@pytest.mark.asyncio
async def test_the_builtins_arrive_through_a_connection_like_anything_else() -> None:
    """Not as a shortcut in the hub, which is the whole point of them being here.

    They are somebody's server — slife2's own — reached the way every other tool
    is, so they are in `servers()`, they have a connection that can fail, and
    they would appear in whatever a tool search is eventually built on.
    """
    async with Client(hub_for()) as hub:
        listed = await hub.call_tool("list_tools", {})
        reported = await hub.call_tool("servers", {})

    assert names(listed.data) == {"builtins__echo", "builtins__now", "builtins__calc"}
    assert {tool["server"] for tool in listed.data["tools"]} == {"builtins"}

    # Found by name, not by position: the hub asks every component, so the
    # builtins are one row among several and are not first.
    row = next(
        one for one in reported.data["servers"] if one["name"] == "builtins"
    )
    assert row["kind"] == "component"
    assert row["required"] is True
    assert row["state"] == "ready"


def component_with_two_kinds_of_tool() -> FastMCP:
    """One of our own servers, offering one tool of each kind.

    Which is what every one of them is: `slife2-db` serves `turn_list` to
    the model and `remember` to the agent, and the difference is not visible in
    anything but the tool itself.
    """
    server = FastMCP("component")

    @server.tool(meta=FOR_THE_MODEL)
    def turn_list(limit: int = 10) -> str:
        """What was said, newest first."""
        return str(limit)

    @server.tool
    def remember(text: str) -> str:
        """The server's own API.  Nothing here marks it, so nothing offers it."""
        return text

    return server


@pytest.mark.asyncio
async def test_a_components_tool_is_the_models_only_when_it_says_so() -> None:
    """The rule the two sources turn on, and it is opt-in.

    A component's tools belong to that component's own code until one of them
    says otherwise — because the ones that would leak are the ones a model
    would reach for: `remember` here stands in for a write into any agent's
    database.  An entry under `tools:` needs no mark at all: the operator opted
    in by writing it down, which is what the tests above are listing.
    """
    async with Client(
        hub_for(connected={"db": lambda settings: component_with_two_kinds_of_tool()})
    ) as hub:
        listed = await hub.call_tool("list_tools", {})
        unreachable = await call(hub, "db__remember", {"text": "hi"})

    assert "db__turn_list" in names(listed.data)
    assert "db__remember" not in names(listed.data)
    # Not merely unlisted: the name is not routable either, so a model that
    # remembered it from somewhere gets an answer rather than a write.
    assert unreachable["ok"] is False
    assert "unknown tool" in unreachable["text"]


@pytest.mark.asyncio
async def test_an_upstream_tool_is_named_for_its_server() -> None:
    """`{server}__{tool}`, and the description says whose it is.

    A model choosing between four tools called `search` cannot do it from the
    name alone, and the name is not allowed to carry a sentence.
    """
    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()})
    ) as hub:
        listed = await hub.call_tool("list_tools", {})

    echo = next(tool for tool in listed.data["tools"] if tool["name"] == "fake__echo")
    assert echo["server"] == "fake"
    assert echo["tool"] == "echo"
    assert echo["description"].startswith("[fake] ")
    assert "text" in echo["parameters"]["properties"]


@pytest.mark.asyncio
async def test_a_call_reaches_the_upstream() -> None:
    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()})
    ) as hub:
        result = await call(hub, "fake__echo", {"text": "hello"})

    assert result == {"text": "hello", "ok": True}


@pytest.mark.asyncio
async def test_a_builtin_is_called_through_the_hub_like_any_other() -> None:
    """Through the same proxy a third-party tool goes through: one hop to the
    server that serves it, and no branch in `call_tool` that knows it is ours.
    """
    async with Client(hub_for()) as hub:
        result = await call(hub, "builtins__calc", {"e": "6*7"})

    assert result == {"text": "42", "ok": True}


@pytest.mark.asyncio
async def test_a_refusal_is_text_with_ok_false() -> None:
    """Not an exception, and not a success with an apology under it.

    The model reads the text and corrects itself; the transcript shows the call
    as failed, because it was.
    """
    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()})
    ) as hub:
        result = await call(hub, "fake__boom")

    assert result["ok"] is False
    assert "the peer said no" in result["text"]


@pytest.mark.asyncio
async def test_an_unknown_name_says_what_is_available() -> None:
    """The error path is a feedback channel: it names the alternatives."""
    async with Client(hub_for()) as hub:
        result = await call(hub, "weather")

    assert result["ok"] is False
    assert "weather" in result["text"]
    assert "builtins__calc" in result["text"]


@pytest.mark.asyncio
async def test_a_name_a_provider_would_reject_is_made_legal() -> None:
    """One dotted tool name would otherwise 400 every turn of every conversation.

    The upstream's own name is kept beside the legal one, because the sanitised
    name can no longer be used to address the far end.
    """
    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()})
    ) as hub:
        listed = await hub.call_tool("list_tools", {})
        result = await call(hub, "fake__read_file")

    dotted = next(
        tool for tool in listed.data["tools"] if tool["name"] == "fake__read_file"
    )
    assert dotted["tool"] == "read.file"
    assert result == {"text": "ok", "ok": True}


# --- what happens when something is missing -----------------------------------


@pytest.mark.asyncio
async def test_a_broken_upstream_leaves_the_hub_working() -> None:
    """The distinction the whole module is built around.

    A tool server that will not start is the operator's configuration and
    somebody else's process, so it is reported and left out — while the hub
    itself, which *is* our system, keeps serving everything else.
    """

    def refuses_to_start(settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("no such program: npx")

    async with Client(hub_for(connected={"broken": refuses_to_start})) as hub:
        listed = await hub.call_tool("list_tools", {})
        reported = await hub.call_tool("servers", {})

    # The builtins are still there, and the model simply has fewer tools.
    assert names(listed.data) == {"builtins__echo", "builtins__now", "builtins__calc"}

    rows = {row["name"]: row for row in reported.data["servers"]}
    assert rows["broken"]["state"] == "failed"
    assert "no such program" in rows["broken"]["error"]


@pytest.mark.asyncio
async def test_servers_says_which_are_ready_and_what_they_offer() -> None:
    async with Client(
        hub_for(
            connected={
                "fake": lambda settings: upstream_server(),
                "other": lambda settings: upstream_server(),
            }
        )
    ) as hub:
        reported = await hub.call_tool("servers", {})

    rows = {row["name"]: row for row in reported.data["servers"]}
    assert rows["fake"]["state"] == "ready"
    assert rows["fake"]["tools"] == 3
    assert rows["fake"]["transport"] == "stdio"
    assert rows["other"]["kind"] == "mcp"


@pytest.mark.asyncio
async def test_a_component_that_is_not_answering_fails_the_list() -> None:
    """The rule that separates a component from an entry in `tools:`.

    A model that has quietly lost `now` and `calc` is a failure nobody can see,
    so the hub refuses rather than serving a shorter list.  An upstream that is
    merely down is the opposite case and is left out instead — see
    `test_a_broken_upstream_leaves_the_hub_working`, which is the same setup
    with the flag the other way.
    """

    def refuses_to_start(settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("no such program: python")

    async with Client(hub_for(connected={"builtins": refuses_to_start})) as hub:
        with pytest.raises(ToolError, match="a component is not answering"):
            await hub.call_tool("list_tools", {})

        # The report still works, and says which one and why.
        reported = await hub.call_tool("servers", {})

    row = next(
        one for one in reported.data["servers"] if one["name"] == "builtins"
    )
    assert row["state"] == "failed"
    assert "no such program" in row["error"]


@pytest.mark.asyncio
async def test_a_tool_from_a_server_that_never_started_is_unknown() -> None:
    """It was never advertised, so there is nothing to call — and the answer
    names what there is, which is what the model needs to try something else."""

    def refuses_to_start(settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("gone")

    async with Client(hub_for(connected={"broken": refuses_to_start})) as hub:
        result = await call(hub, "broken__anything")

    assert result["ok"] is False
    assert "unknown tool" in result["text"]
    assert "builtins__echo" in result["text"]


# --- the link, and what is done about a bad one -------------------------------


class FakeClient:
    """A client that fails the way a dead link does, or answers.

    The rebuild rule is about a link that dies *during a call*, and nothing
    about a real transport can be made to fail on demand — so the client is what
    is faked, and the transport is a placeholder nothing looks at.
    """

    def __init__(self, *, dies: bool = False, refuses: bool = False) -> None:
        self.dies = dies
        self.refuses = refuses
        self.calls = 0

    async def __aenter__(self) -> FakeClient:
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
    """A client factory, and the list it appends each client it builds to.

    **Fake only for the upstream under test**, which it recognises by the
    marker its transport returned: the builtins are a required upstream, and a
    hub whose every client misbehaved would fail `list_tools` before reaching
    the case being tested.  So `made` is exactly the connections to one server,
    which is what the assertions are counts of.
    """
    made: list[FakeClient] = []

    def make(transport: Any, handler: Any) -> Any:
        if transport is not UNUSED:
            return make_client(transport, handler)
        client = FakeClient(dies=dies or (first_dies and not made), refuses=refuses)
        if first_dies and made:
            client.dies = False
        made.append(client)
        return client

    return make, made


@pytest.mark.asyncio
async def test_a_call_that_dies_at_the_transport_is_retried_once() -> None:
    """Once, and it succeeds when the second link is good.

    A link that died mid-call is the one failure where trying again is not
    superstition — the tool never ran, or ran and could not say so.
    """
    make, made = clients(first_dies=True)

    async with Client(hub_for(connected={"fake": unused}, client_factory=make)) as hub:
        result = await call(hub, "fake__echo")

    assert result == {"text": "answered", "ok": True}
    assert len(made) == 2, "one connection, then one rebuild"


@pytest.mark.asyncio
async def test_a_server_that_is_down_costs_two_timeouts_not_more() -> None:
    """The other half of "once": a dead server must not be retried forever."""
    make, made = clients(dies=True)

    async with Client(hub_for(connected={"fake": unused}, client_factory=make)) as hub:
        result = await call(hub, "fake__echo")

    assert result["ok"] is False
    assert len(made) == 2


@pytest.mark.asyncio
async def test_a_refusal_does_not_rebuild_the_link() -> None:
    """The peer answered.  Rebuilding would only be told the same thing again."""
    make, made = clients(refuses=True)

    async with Client(hub_for(connected={"fake": unused}, client_factory=make)) as hub:
        result = await call(hub, "fake__echo")

    assert result["ok"] is False
    assert len(made) == 1, "a peer-reported error is not a transport failure"
    assert made[0].calls == 1


@pytest.mark.asyncio
async def test_a_changed_tool_list_is_read_again() -> None:
    """What the peer's `tools/list_changed` leads to, without the notification.

    Nothing polls: the snapshot is dropped and the next ask re-reads it.
    """
    upstream = Upstream(
        ToolServerSettings(name="fake", command="in-memory"),
        transport=lambda settings: upstream_server(),
    )
    assert await upstream.ready() is True
    assert upstream.attempt is None

    upstream.invalidate()
    assert upstream.snapshot()["state"] != "ready"

    assert await upstream.ready() is True
    assert upstream.snapshot()["state"] == "ready"
    await upstream.close()


@pytest.mark.asyncio
async def test_a_slow_connect_is_not_cancelled_by_the_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug a live run against a real endpoint found, and the reason `settle`
    uses `asyncio.wait` rather than a timeout around a gather.

    Giving up on *waiting* for a connection is not a reason to stop making it:
    the gather spelling cancels the attempt it was waiting on, which left the
    upstream `idle` with no error and no tools — the one state that describes
    nothing at all.
    """
    monkeypatch.setattr("slife2.toolhub.LIST_SETTLE_SECONDS", 0.05)

    class Slow(FakeClient):
        async def __aenter__(self) -> FakeClient:
            await asyncio.sleep(0.2)
            return self

    def make(transport: Any, handler: Any) -> FakeClient:
        return Slow()

    async with Client(hub_for(connected={"fake": unused}, client_factory=make)) as hub:
        early = await hub.call_tool("servers", {})
        assert early.data["servers"][0]["state"] == "connecting"

        # The attempt was left alone, so it finishes on its own.
        await asyncio.sleep(0.3)
        listed = await hub.call_tool("list_tools", {})

    assert "fake__echo" in names(listed.data)


@pytest.mark.asyncio
async def test_an_entry_the_client_refuses_is_a_failed_server() -> None:
    """Building the connection is inside the `try`, deliberately.

    A malformed entry — a URL the SDK will not accept, a key it does not know —
    raises while the transport is being built, and that is one server that
    cannot be reached rather than a hub with a hole in it.  It used to escape
    the handler and leave the upstream `idle`.
    """

    def make(transport: Any, handler: Any) -> Any:
        raise ValueError("not a URL")

    async with Client(hub_for(connected={"fake": unused}, client_factory=make)) as hub:
        reported = await hub.call_tool("servers", {})

    row = reported.data["servers"][0]
    assert row["state"] == "failed"
    assert "not a URL" in row["error"]


# --- how an entry becomes a connection ----------------------------------------


def test_a_stdio_entry_becomes_a_command() -> None:
    built = mcp_config(
        ToolServerSettings(
            name="serper",
            command="npx",
            args=("-y", "serper-search-scrape-mcp-server"),
            env={"SERPER_API_KEY": "secret"},
        ),
        cwd="/data",
    )
    assert built == {
        "mcpServers": {
            "serper": {
                "command": "npx",
                "args": ["-y", "serper-search-scrape-mcp-server"],
                "transport": "stdio",
                "cwd": "/data",
                "env": {"SERPER_API_KEY": "secret"},
            }
        }
    }


def test_an_http_entry_becomes_a_url() -> None:
    built = mcp_config(
        ToolServerSettings(
            name="arxiv", url="https://example.invalid/mcp", headers={"X": "y"}
        ),
        cwd="/data",
    )
    assert built["mcpServers"]["arxiv"] == {
        "url": "https://example.invalid/mcp",
        "transport": "http",
        "headers": {"X": "y"},
    }


def test_an_entrys_own_cwd_wins_over_the_default() -> None:
    """Which matters because an argument of `.` has to mean something."""
    entry = mcp_config(
        ToolServerSettings(name="filesystem", command="npx", cwd="/elsewhere"),
        cwd="/data",
    )
    assert entry["mcpServers"]["filesystem"]["cwd"] == "/elsewhere"


def test_names_are_made_legal_without_losing_what_they_were() -> None:
    assert sanitise("read.file") == "read_file"
    assert sanitise("weird/name:2") == "weird_name_2"
    assert sanitise("already-fine_1") == "already-fine_1"
    assert proxied_name("my server", "read.file") == "my_server__read_file"


def test_flatten_describes_what_is_not_text() -> None:
    """Dropped would be silent, and silence reads as "the tool did nothing"."""
    blocks = [
        types.SimpleNamespace(text="here"),
        types.SimpleNamespace(mime_type="image/png", data="aGk="),
    ]
    assert flatten(blocks) == "here\n[SimpleNamespace: image/png, 4 characters]"
    assert flatten([]) == "(the tool returned nothing)"


# --- the real thing -----------------------------------------------------------

#: A minimal MCP server on stdio, written out and spawned.  Nothing here is
#: clever: it exists to be a *process*, because the in-memory transport cannot
#: prove that a config entry becomes one.
UPSTREAM_SCRIPT = '''
from fastmcp import FastMCP

mcp = FastMCP("spawned")


@mcp.tool
def greet(name: str) -> str:
    """Say hello."""
    return f"hello {name}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
'''


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_configured_entry_becomes_a_process_that_answers(tmp_path) -> None:
    """The whole path, once: config entry -> transport -> child -> tool call.

    Everything else in this file fakes the connection, and deliberately — but
    the one thing a fake cannot say is that `mcp_config` produces something a
    client can actually connect to.
    """
    import sys

    script = tmp_path / "upstream.py"
    script.write_text(UPSTREAM_SCRIPT, encoding="utf-8")

    # `entries` and not `connected`: this is the one test that wants the real
    # transport, so the entry is configured and left to build its own.
    hub = hub_for(
        entries={
            "spawned": ToolServerSettings(
                name="spawned",
                command=sys.executable,
                args=(str(script),),
                cwd=str(tmp_path),
            )
        }
    )

    async with Client(hub) as client:
        listed = await client.call_tool("list_tools", {})
        assert names(listed.data) == {
            "builtins__echo",
            "builtins__now",
            "builtins__calc",
            "spawned__greet",
        }

        result = await call(client, "spawned__greet", {"name": "ada"})
        assert result == {"text": "hello ada", "ok": True}
