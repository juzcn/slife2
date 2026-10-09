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
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from slife2.audience import FOR_THE_MODEL
from slife2.config import ToolLoadSettings, ToolServerSettings, default_config
from slife2.toolhub import (
    MAX_LOAD_NAMES,
    PLUGIN,
    Catalogue,
    Upstream,
    flatten,
    make_client,
    mcp_config,
    model_name,
    proxied_name,
    sanitise,
)
from slife2.toolhub import (
    build_server as build_hub,
)
from tests.fakes import plugin_transports

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
    threshold: int | None = None,
    **kwargs: Any,
) -> FastMCP:
    """A hub over in-db servers, keyed by entry name.

    `connected` maps a name to a *transport*, which is anything a `Client` can
    be built from and therefore always a callable: `lambda settings: server` for
    an in-memory one, or a function that raises, for the tests about a server
    that will not start.  `entries` are
    configured but *not* wired, which is how the one test that spawns a real
    process gets a real transport.

    **Every plugin is here, and only the builtins and the db are real.**  They
    are the servers slife2 starts, the hub asks each of them for a tool list, and
    it refuses to hand one out at all when one does not answer — which is what
    `test_a_plugin_that_is_not_answering_fails_the_list` is about, and not
    something every other test should have to trip over.  The db is real because
    the hub is a client of it: the tool catalogue is there, so a hub built here
    has a catalogue, which is what `connected={"db": refuses_to_start}` takes
    away for the tests about that.

    **The tool servers below are `autoload: true`**, which is what a test wants
    them to be: these are servers whose tools are in the model's list, so a test
    about naming, routing or the sanitiser is about that and not about having to
    load one first.  What it means to be on demand instead is
    `test_a_servers_tools_are_on_demand_until_they_are_loaded`, which is the same
    hub built without the flag.
    """
    base = default_config()
    # The tools first, then the transports: the db server is built *from* the
    # config — it reads which entries are `autoload` and which are switched off
    # — so a hub whose catalogue was built before the entries were added would
    # seed every one of them unloaded.
    config = replace(
        base,
        tools={
            **{
                name: ToolServerSettings(name=name, command="in-memory", autoload=True)
                for name in [*base.plugins(), *(connected or {})]
                if name not in base.plugins()
            },
            **(entries or {}),
        },
    )
    if threshold is not None:
        # The budget is a hundred by default, which is the right number and not
        # a testable one: a trim is how the eviction *order* is observed from
        # outside, and the order is only visible when something is over the cap.
        config = replace(config, tool_load=ToolLoadSettings(threshold=threshold))
    wired = plugin_transports(config, connected)
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

    **And they arrive under their own names.**  A hop is not something the model
    is told about: `now` came through a connection the same way `arxiv__search`
    does, and a name carrying the server would be the one place the hop leaked
    into what the model reads.
    """
    async with Client(hub_for()) as hub:
        listed = await hub.call_tool("list_tools", {})
        reported = await hub.call_tool("servers", {})

    # Named rather than compared against the whole list: the hub also serves
    # tools of its own (below), and this test is about where the builtins come
    # from, not about the list being exactly this.  The `server` field is what
    # says whose they are — the name no longer does.
    ours = {
        tool["name"] for tool in listed.data["tools"] if tool["server"] == "builtins"
    }
    assert ours == {"echo", "now", "calc"}
    # Three sources, and each is a real one: the builtins are a plugin, the
    # db offers the model its two history tools (they carry the mark), and the
    # third is this process's own — `tool_search`, `func_tool_load`, `skill_use`.
    assert {tool["server"] for tool in listed.data["tools"]} == {
        "builtins",
        "db",
        "toolhub",
    }

    # Found by name, not by position: the hub asks every plugin, so the
    # builtins are one row among several and are not first.
    row = next(one for one in reported.data["servers"] if one["name"] == "builtins")
    assert row["kind"] == "plugin"
    assert row["required"] is True
    assert row["state"] == "ready"
    assert row["tools"] == 3
    assert row["loaded"] == 3, "a plugin's tools are in the model's list"


def write_skill(data_dir: Path, name: str = "one") -> Path:
    """One skill on disk, in the `skills/` a bare `scan()` looks in."""
    folder = data_dir / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\nThe body.\n", encoding="utf-8"
    )
    return folder


@pytest.mark.asyncio
async def test_a_local_tool_is_served_by_the_hub_itself(isolated_runtime: Path) -> None:
    """No server behind it, so no connection and no row in `servers()`.

    A skill is a document on this machine: there is nothing for the hub to
    connect to, nothing a hop could reach, and the directory is one the hub is
    already holding.  The name says as much — `skill_use`, not
    `{server}__{tool}` — and the answer is the file, read at the moment of the
    call rather than snapshotted when the hub started.
    """
    write_skill(isolated_runtime)
    async with Client(hub_for()) as hub:
        listed = await hub.call_tool("list_tools", {})
        answer = await call(hub, "skill_use", {"name": "one"})
        reported = await hub.call_tool("servers", {})
        # Found on every call rather than at startup: a skill dropped into the
        # folder is there for the next call, with no restart to ask for and no
        # listing to keep in step.
        write_skill(isolated_runtime, "two")
        later = await call(hub, "skill_use", {"name": "two"})

    assert "skill_use" in names(listed.data)
    tool = next(one for one in listed.data["tools"] if one["name"] == "skill_use")
    # This process's own tool, catalogued as this process's: the folder it reads
    # is not a source of tools, and the row says who serves it.
    assert tool["server"] == "toolhub"
    assert tool["tool"] == "skill_use"
    assert "name" in tool["parameters"]["properties"]

    assert answer["ok"] is True
    assert answer["text"].rstrip().endswith("The body.")
    assert later["ok"] is True

    # It is a source of tools and not a tool server, and `servers()` is about
    # what has a connection — there is none here to report on.
    assert "skills" not in {row["name"] for row in reported.data["servers"]}


@pytest.mark.asyncio
async def test_a_local_tool_that_says_no_is_a_refusal(isolated_runtime: Path) -> None:
    """`ok` false and text the model can act on — the same contract an upstream
    that refused gets, because the model cannot tell the two apart and should
    not have to."""
    write_skill(isolated_runtime)
    async with Client(hub_for()) as hub:
        answer = await call(hub, "skill_use", {"name": "nothing"})

    assert answer["ok"] is False
    assert "one" in answer["text"], "the answer names what does exist"


def plugin_with_two_kinds_of_tool() -> FastMCP:
    """One of our own servers, offering one tool of each kind.

    Which is what every one of them is: `slife2-db` serves `turn_list` to
    the model and `remember` to the agent, and the difference is not visible in
    anything but the tool itself.

    The model's one is `digest` and not that `turn_list`, because our tools are
    one namespace: two plugins cannot offer a name between them, and the db in
    this hub is the real one.
    """
    server = FastMCP("plugin")

    @server.tool(meta=FOR_THE_MODEL)
    def digest(limit: int = 10) -> str:
        """What was said, newest first."""
        return str(limit)

    @server.tool
    def remember(text: str) -> str:
        """The server's own API.  Nothing here marks it, so nothing offers it."""
        return text

    return server


@pytest.mark.asyncio
async def test_a_plugins_tool_is_the_models_only_when_it_says_so() -> None:
    """The rule the two sources turn on, and it is opt-in.

    A plugin's tools belong to that plugin's own code until one of them
    says otherwise — because the ones that would leak are the ones a model
    would reach for: `remember` here stands in for a write into any agent's
    database.  An entry under `tools:` needs no mark at all: the operator opted
    in by writing it down, which is what the tests above are listing.
    """
    async with Client(
        hub_for(connected={"agent": lambda settings: plugin_with_two_kinds_of_tool()})
    ) as hub:
        listed = await hub.call_tool("list_tools", {})
        unreachable = await call(hub, "remember", {"text": "hi"})

    assert "digest" in names(listed.data)
    # Not merely unlisted: the name is not routable either, so a model that
    # remembered it from somewhere gets an answer rather than a write.  An
    # unmarked tool has no advertised name at all — there is nothing to prefix
    # and nothing to hide behind, so the name it would be reached by is its own.
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
    # The label once, and exactly: the row holds the description the server
    # wrote and this is the one place the server's name is put in front of it.
    assert echo["description"] == "[fake] Echo it back."
    assert "text" in echo["parameters"]["properties"]


@pytest.mark.asyncio
async def test_a_plugins_tool_is_named_as_itself() -> None:
    """Bare, where somebody else's carries its server — in the same list.

    A plugin is a server slife2 starts, which is a fact about this system's
    arrangement and not about the tool: `builtins__now` told the model about a
    division it has no use for, and the plugin a tool is served by is reported
    in `servers()` to whoever is debugging.  What the prefix is *for* is here
    too: two of somebody else's servers may each offer a `search`.
    """
    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()})
    ) as hub:
        listed = await hub.call_tool("list_tools", {})

    def called(server: str) -> set[str]:
        return {
            tool["name"] for tool in listed.data["tools"] if tool["server"] == server
        }

    # The same list, the same hop, the same `_advertise`: only the category
    # differs, and it is what the name is decided by.
    assert called("builtins") == {"echo", "now", "calc"}
    assert called("fake") == {"fake__echo", "fake__boom", "fake__read_file"}


@pytest.mark.asyncio
async def test_two_plugins_cannot_offer_one_name() -> None:
    """Ours are one namespace, and the second offering of a name is refused.

    That is the price of bare names, and it is paid in the catalogue — the one
    place a naming rule can be enforced — whose `name` is a row's identity.  A
    plugin whose list cannot be recorded is a plugin this hub cannot describe,
    and a listing that quietly dropped one of the `echo`s would be the failure
    nobody can see.

    **Which of the two loses is not asserted, because it is a race.**  Both
    plugins are asked at once (the hub starts every connect together), so either
    can be the one that merges second, and the second is the one refused —
    whether the catalogue catches it while planning ("already `builtins`'s
    tool") or the insert trips its own primary key.  What is not a race is the
    outcome: one of them holds the name, and the model's list does not come back
    short.
    """
    server = FastMCP("plugin")

    @server.tool(meta=FOR_THE_MODEL)
    def echo(text: str) -> str:
        """A second `echo`, which the builtins already offer."""
        return text

    async with Client(hub_for(connected={"agent": lambda settings: server})) as hub:
        with pytest.raises(ToolError, match="a plugin is not answering"):
            await hub.call_tool("list_tools", {})
        reported = await hub.call_tool("servers", {})

    owners = {
        one["name"]: one
        for one in reported.data["servers"]
        if one["name"] in ("builtins", "agent")
    }
    assert len([one for one in owners.values() if one["state"] == "ready"]) == 1
    refused = next(one for one in owners.values() if one["state"] != "ready")
    # And the report says why, which it is the tool for: "a plugin is not
    # answering" is the wrong answer for a plugin that answered.
    assert refused["error"]


@pytest.mark.asyncio
async def test_a_servers_tools_are_on_demand_until_they_are_loaded() -> None:
    """The gate, and the whole reason the tool catalogue exists.

    What the model is handed is the tools it has *loaded*, not everything
    installed: a server with ninety tools would otherwise cost a prompt on every
    request.  `autoload: true` is the operator saying this server's are wanted
    every turn, which is what every other test in this file relies on.
    """
    entry = ToolServerSettings(name="fake", command="in-memory", autoload=False)
    async with Client(
        hub_for(
            connected={"fake": lambda settings: upstream_server()},
            entries={"fake": entry},
        )
    ) as hub:
        before = await hub.call_tool("list_tools", {})
        assert "fake__echo" not in names(before.data)

        # Findable, though: the catalogue holds it and the search answers for
        # the whole of what is installed, not only what is in front of the model.
        found = await call(hub, "tool_search", {"query": "echo"})
        assert "fake__echo" in found["text"]
        assert "func_tool_load" in found["text"], "and the answer says how to use it"

        loaded = await call(hub, "func_tool_load", {"names": ["fake__echo"]})
        assert loaded["ok"] is True
        after = await hub.call_tool("list_tools", {})
        assert "fake__echo" in names(after.data)

        # And the call works either way — loading is about *seeing* a tool, and
        # the route is what makes one callable (v1's rule, kept).
        assert (await call(hub, "fake__echo", {"text": "hi"}))["ok"] is True


@pytest.mark.asyncio
async def test_the_budget_spares_the_tool_the_model_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recency is a *call*, and the hub is where it is learned.

    **No notification from the loop is involved, and none is needed.**  A model's
    tool call has one path — the hub's `call_tool`, which is the process that
    reaches the far end — so the stamp is written beside the call it just
    routed, and it is written *before* the answer goes back: the harness trims
    the list at the turn boundary, and a stamp that could land after it would
    cost the model the tool it had spent the turn using.

    All three tools are loaded in one call, so they share a `last_loaded` — the
    clock is second-precision on purpose.  What separates them afterwards is
    what the model *did* with them: `fake__echo` was called and worked,
    `fake__boom` was called and refused, `fake__read_file` was never touched.  A
    refusal counts, because a model reaching for a tool is the evidence the
    budget decides on; and ordering by the load alone would take them in name
    order instead, which is the bug this whole arrangement exists to fix.
    """
    when = "2026-01-01T00:00:00+00:00"
    monkeypatch.setattr("slife2.db.now", lambda: when)
    entry = ToolServerSettings(name="fake", command="in-memory", autoload=False)
    async with Client(
        hub_for(
            connected={"fake": lambda settings: upstream_server()},
            entries={"fake": entry},
            threshold=1,
        )
    ) as hub:
        # The upstream has to have answered before a name of its can be loaded:
        # `list_tools` is the ask that starts the connect and gives it a moment.
        await hub.call_tool("list_tools", {})

        loaded = await call(
            hub,
            "func_tool_load",
            {"names": ["fake__boom", "fake__echo", "fake__read_file"]},
        )
        assert loaded["ok"] is True, loaded["text"]

        when = "2026-01-02T00:00:00+00:00"
        assert (await call(hub, "fake__echo", {"text": "hi"}))["ok"] is True
        when = "2026-01-03T00:00:00+00:00"
        assert (await call(hub, "fake__boom"))["ok"] is False, "refused, and counted"

        when = "2026-01-04T00:00:00+00:00"
        trimmed = await hub.call_tool("_func_tool_unload", {})

    assert trimmed.data["unloaded"] == [
        "fake__read_file",
        "fake__echo",
        "fake__boom",
    ], "least recently called first, and the never-called one before both"


@pytest.mark.asyncio
async def test_loading_says_why_it_cannot() -> None:
    """A name that resolves to nothing is answered with the way to find one.

    The refusals that depend on a row's own state are the store's to make and
    are tested there (`tests/test_toolsdb.py`); what this checks is the hop —
    that the model reads a sentence, and that the sentence says what to do next.
    """
    async with Client(hub_for()) as hub:
        unknown = await call(hub, "func_tool_load", {"names": ["nothing__at_all"]})

    assert unknown["ok"] is False
    assert "unknown tool" in unknown["text"]
    assert "tool_search" in unknown["text"], "the way to find the right name"


@pytest.mark.asyncio
async def test_a_name_that_does_not_exist_is_not_a_tool_the_system_needs() -> None:
    """`unknown` and `refused` are different facts, and one of them was false.

    The db answers `unknown` for a name no row holds; the hub filed it with the
    refusals, whose sentence is "the system needs them" — so a caller asking
    about a tool it invented was told the process protects it.
    """
    async with Client(hub_for()) as hub:
        answer = await hub.call_tool(
            "_func_tool_unload", {"names": ["nothing__at_all"]}
        )

    assert answer.data["unknown"] == ["nothing__at_all"]
    assert answer.data["refused"] == []
    assert "no such tool" in answer.data["text"]


@pytest.mark.asyncio
async def test_loading_too_many_names_at_once_is_refused_whole() -> None:
    """Each name is its own catalogue write and its own embedding call.

    A list longer than the budget holds is one the next turn boundary takes
    back anyway, and truncating it would leave a model diffing two lists to
    work out which half it got.
    """
    async with Client(hub_for()) as hub:
        many = [f"fake__t{number}" for number in range(MAX_LOAD_NAMES + 1)]
        answer = await call(hub, "func_tool_load", {"names": many})

    assert answer["ok"] is False
    assert str(MAX_LOAD_NAMES) in answer["text"], "the message says the bound"


@pytest.mark.asyncio
async def test_a_browse_says_how_to_load_one() -> None:
    """The empty query is the "what is installed, and how do I get one" question.

    Withholding the mechanism there withheld it exactly where a model asks
    about it — the hint was appended only for a *search*, which is the case
    where the model has already found its way to the mechanism.
    """
    async with Client(hub_for()) as hub:
        answer = await call(hub, "tool_search", {})

    assert answer["text"], "a browse answers with what is installed"
    assert "func_tool_load" in answer["text"]


@pytest.mark.asyncio
async def test_a_page_size_a_model_wrote_as_text_still_answers() -> None:
    """A schema saying `integer` is not a promise about what arrives.

    A model writes `"5"` for `limit` as readily as `5`, and the hub's own tools
    are the one surface no framework validates: a server's tool is checked
    against its schema before the body runs, while `tool_search` is a closure
    called with a plain dict.  So the reading is this module's job, and the
    failure it prevents is a `ValueError` escaping as an MCP error — a model
    reading our traceback where it asked for a page.
    """
    async with Client(hub_for()) as hub:
        found = await call(hub, "tool_search", {"query": "echo", "limit": "1"})

    assert found["ok"] is True
    assert found["text"], "the page came back rather than the parse failure"


@pytest.mark.asyncio
async def test_a_tool_of_a_switched_off_server_is_not_an_unknown_tool() -> None:
    """`enabled: false` leaves the row and takes the server away.

    Which is the whole reason the catalogue keeps rows for a source that is not
    connected: "there is a tool for this, and its server is switched off" is a
    different answer from "no such tool", and it is the one a person needs —
    nothing is wrong, somebody turned it off.

    The row survives a restart because it is on disk; what makes it `disabled`
    is the boot pass, which is told which sources the config switches off.
    """
    live = replace(
        default_config(),
        tools={"fake": ToolServerSettings(name="fake", command="in-memory")},
    )
    async with Client(
        build_hub(
            live,
            transports=plugin_transports(
                live, {"fake": lambda settings: upstream_server()}
            ),
        )
    ) as first:
        # The list first, as the agent asks for it before every model call —
        # which is also the moment a server's tools become catalogue rows.
        await first.call_tool("list_tools", {})
        found = await call(first, "tool_search", {"query": "echo"})
        assert "fake__echo" in found["text"], "recorded while the server was up"

    # The same data directory, with the entry switched off: no connection, no
    # transport, and the row it left behind.
    off = replace(
        default_config(),
        tools={
            "fake": ToolServerSettings(name="fake", command="in-memory", enabled=False)
        },
    )
    async with Client(build_hub(off, transports=plugin_transports(off))) as hub:
        refused = await call(hub, "func_tool_load", {"names": ["fake__echo"]})
        found = await call(hub, "tool_search", {"query": "echo"})

    assert refused["ok"] is False
    assert "switched off" in refused["text"]
    assert "fake__echo" in found["text"], "shown, and marked as not usable"
    assert "NOT USABLE: disabled" in found["text"]


@pytest.mark.asyncio
async def test_the_loaded_set_outlives_the_process() -> None:
    """The point of moving the tool table out of memory and into the catalogue.

    A hub that restarts — or a second conversation on a hub that has been up for
    days — finds the tools that were loaded still loaded, because the row is on
    disk and not in a snapshot.  Nothing here re-asks a server for anything: the
    data directory is the whole of what carried it across.
    """
    connected = {"fake": lambda settings: upstream_server()}
    entries = {
        "fake": ToolServerSettings(name="fake", command="in-memory", autoload=False)
    }

    async with Client(hub_for(connected=connected, entries=entries)) as first:
        # The list first, as the agent asks for it before every model call —
        # which is also what makes a server's tools *known* to the catalogue.
        await first.call_tool("list_tools", {})
        assert (await call(first, "func_tool_load", {"names": ["fake__echo"]}))["ok"]

    async with Client(hub_for(connected=connected, entries=entries)) as second:
        listed = await second.call_tool("list_tools", {})

    assert "fake__echo" in names(listed.data)


@pytest.mark.asyncio
async def test_the_harness_trims_the_list_and_says_what_it_took() -> None:
    """`_func_tool_unload`, the trim's two ways to be called.

    **The answer names what went**, and that is the reason the trim is a call at
    a turn boundary rather than a rule inside the gate: the agent server is the
    party that has to know what the model just lost, and — because the trim is
    written into the conversation as a tool pair — so is the model.  It is in
    the model's list for that second reason, the one `_`-prefixed name that is,
    and the four tools the system works by are refused rather than obeyed.

    The budget itself is the store's (`tests/test_toolsdb.py` has the counting);
    what this checks is the hop and the two ways to call it.
    """
    config = replace(
        default_config(),
        tools={"fake": ToolServerSettings(name="fake", command="in-memory")},
    )
    hub = build_hub(
        config,
        transports=plugin_transports(
            config, {"fake": lambda settings: upstream_server()}
        ),
    )
    async with Client(hub) as client:
        listed = await client.call_tool("list_tools", {})
        # The one `_` name a model sees, and the only way to trim its own list.
        assert "_func_tool_unload" in names(listed.data)

        await call(client, "func_tool_load", {"names": ["fake__echo", "fake__boom"]})
        named = await client.call_tool("_func_tool_unload", {"names": ["fake__echo"]})
        after = await client.call_tool("list_tools", {})

        # No names means "enforce the budget", and at a hundred there is nothing
        # to do — which is an answer, not a silence.
        idle = await client.call_tool("_func_tool_unload", {})
        refused = await client.call_tool(
            "_func_tool_unload", {"names": ["tool_search"]}
        )

    assert named.data["unloaded"] == ["fake__echo"]
    assert "fake__echo" in named.data["text"], "the caller reads the name off this"
    assert "fake__echo" not in names(after.data)
    assert "fake__boom" in names(after.data), "only what was named"

    assert idle.data["unloaded"] == []
    assert "within its budget" in idle.data["text"]

    assert refused.data["refused"] == ["tool_search"]
    assert refused.data["unloaded"] == []


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

    Under its own name, which is the other half of that: the model is not told
    which of the connections behind its list a name came down.
    """
    async with Client(hub_for()) as hub:
        result = await call(hub, "calc", {"e": "6*7"})

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
    assert "calc" in result["text"]


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

    # The builtins are still there, and the model simply has fewer tools.  So
    # is the hub's own three, which never depended on anybody connecting — and
    # `_func_tool_unload` is *not* one of them: it is the harness's tool, and
    # the gate keeps it out of the model's list however the catalogue holds it.
    # Found by whose they are rather than by the shape of the name, which no
    # longer says it: a broken entry's `broken__anything` would have passed for
    # one of ours under the old filter.
    served_here = {
        tool["name"] for tool in listed.data["tools"] if tool["server"] == "toolhub"
    }
    assert served_here == {
        "skill_use",
        "tool_search",
        "func_tool_load",
        "_func_tool_unload",
    }
    assert "now" in names(listed.data)
    assert "broken__anything" not in names(listed.data)

    rows = {row["name"]: row for row in reported.data["servers"]}
    assert rows["broken"]["state"] == "failed"
    assert "no such program" in rows["broken"]["error"]
    assert rows["broken"]["tools"] == 0, "it never listed anything"


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
async def test_a_plugin_that_is_not_answering_fails_the_list() -> None:
    """The rule that separates a plugin from an entry in `tools:`.

    A model that has quietly lost `now` and `calc` is a failure nobody can see,
    so the hub refuses rather than serving a shorter list.  An upstream that is
    merely down is the opposite case and is left out instead — see
    `test_a_broken_upstream_leaves_the_hub_working`, which is the same setup
    with the flag the other way.
    """

    def refuses_to_start(settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("no such program: python")

    async with Client(hub_for(connected={"builtins": refuses_to_start})) as hub:
        with pytest.raises(ToolError, match="a plugin is not answering"):
            await hub.call_tool("list_tools", {})

        # The report still works, and says which one and why.
        reported = await hub.call_tool("servers", {})

    row = next(one for one in reported.data["servers"] if one["name"] == "builtins")
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
    assert "echo" in result["text"]


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


class RecordingCatalogue(Catalogue):
    """A catalogue that keeps what it was told, for the tests that drive one
    `Upstream` on its own.

    The hub always has a real catalogue behind it — `hub_for` stands the db
    server up over the in-memory transport — and this is for the tests that
    build a connection directly, where what is under test is the link's own
    behaviour and what the rows say is somebody else's business.  It is a
    `Catalogue` and not a stand-in object because that is the type the
    connection takes; the connection it would open is never asked for.
    """

    def __init__(self) -> None:
        super().__init__(_never_connect)
        self.merges: list[tuple[str, str, list[dict[str, Any]]]] = []
        self.states: list[tuple[str, str]] = []

    async def merge(
        self, source: str, category: str, tools: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        self.merges.append((source, category, [dict(tool) for tool in tools]))
        return {"inserted": [str(tool["name"]) for tool in tools]}

    async def source_state(self, source: str, state: str) -> None:
        self.states.append((source, state))


async def _never_connect() -> Client:  # pragma: no cover - never reached
    raise AssertionError("this catalogue is a recorder; it opens nothing")


@pytest.mark.asyncio
async def test_a_changed_tool_list_is_read_again() -> None:
    """What the peer's `tools/list_changed` leads to, without the notification.

    Nothing polls: the listing is dropped and the next ask re-reads it — which
    is also what makes the source stop counting as *live* for one ask, so its
    rows are out of the model's list until it has answered again.
    """
    recorded = RecordingCatalogue()
    upstream = Upstream(
        ToolServerSettings(name="fake", command="in-memory"),
        transport=lambda settings: upstream_server(),
        catalogue=recorded,
    )
    assert await upstream.ready() is True
    assert upstream.attempt is None
    assert recorded.merges[0][0] == "fake", "the listing went to the catalogue"

    upstream.invalidate()
    assert upstream.snapshot()["state"] != "ready"

    assert await upstream.ready() is True
    assert upstream.snapshot()["state"] == "ready"
    assert len(recorded.merges) == 2, "the second ask re-listed and re-merged"
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


class FlakyCatalogue:
    """A catalogue connection whose first client is dead.

    Stands in for the case a long-lived hub actually meets — `slife2-db`
    restarted underneath it — and for the refusal it has to keep apart from
    that.  A real db cannot be made to die on demand, so the client is what is
    faked, exactly as `FakeClient` is for an upstream.
    """

    def __init__(self, *, dies: bool = False, refuses: bool = False) -> None:
        self.dies = dies
        self.refuses = refuses
        self.calls = 0

    async def call_tool(self, name: str, arguments: Any = None, **_: Any) -> Any:
        self.calls += 1
        if self.dies:
            raise ConnectionError("the db is not there")
        if self.refuses:
            raise ToolError("'x__echo' is already x__echo's tool")
        return types.SimpleNamespace(data={"outcome": "loaded"})


@pytest.mark.asyncio
async def test_the_catalogue_reconnects_when_the_db_goes_away() -> None:
    """The connection is opened once and kept, and the hub outlives the db.

    Without dropping it, a db that restarts under a running hub fails every
    later call — `tool_search`, `func_tool_load`, every merge — until the hub
    is restarted too, and the hub is the longest-lived process here.
    """
    made: list[FlakyCatalogue] = []

    async def connect() -> Any:
        client = FlakyCatalogue(dies=not made)
        made.append(client)
        return client

    catalogue = Catalogue(connect)

    assert await catalogue.set_load("echo", "loaded") == {"outcome": "loaded"}
    assert len(made) == 2, "the dead connection was kept and used again"
    assert made[0].calls == 1, "the dead client is tried once, not twice"


@pytest.mark.asyncio
async def test_a_refusal_is_not_a_catalogue_that_is_gone() -> None:
    """The db answered and said no, and the two are told apart by the SDK.

    `ToolError` is what FastMCP raises when the *peer's tool* reported an
    error, and nothing else raises it — a dead link raises its own exception —
    so this is the framework's distinction rather than a guess.  Reporting a
    name collision as "the tool catalogue is not answering" sends the reader to
    examine a plugin that is working perfectly.
    """
    made: list[FlakyCatalogue] = []

    async def connect() -> Any:
        client = FlakyCatalogue(refuses=True)
        made.append(client)
        return client

    catalogue = Catalogue(connect)

    with pytest.raises(ToolError, match="already"):
        await catalogue.merge("arxiv", "mcp", [])

    assert len(made) == 1, "a refusal is not a reason to rebuild the link"


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


def test_the_naming_rule_is_one_question_about_the_category() -> None:
    """The whole of it: whose tool is this, and so does the name carry a server.

    Stated here once because both paths ask it — the listing names a tool on the
    way in, and the row's own name is what the model reads on the way back out.
    """
    assert model_name("builtins", "now", PLUGIN) == "now"
    # Sanitised either way: a plugin is our code, and our code can still name a
    # tool something a provider rejects.
    assert model_name("db", "read.file", PLUGIN) == "read_file"
    assert model_name("filesystem", "read_file", "mcp") == "filesystem__read_file"
    assert model_name("github", "search", "rest") == "github__search"


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
        # The entry is on demand — it says nothing about `autoload` — so its
        # tool is *not* in the list yet, and the model has to ask for it.  That
        # is the whole of the mechanism, end to end: no config flag, so the
        # search-and-load path is what puts a server's tools in front of a model.
        listed = await client.call_tool("list_tools", {})
        assert names(listed.data) == {
            "_func_tool_unload",
            "calc",
            "echo",
            "now",
            # The db's two history tools are the model's — they carry the mark —
            # so the real db plugin brings them along.  **Ours are bare**, and
            # `spawned__greet` below is the only name on this surface with
            # somebody else's server in front of it: there is one set of tools
            # here, slife2's, and a plugin's name in front of one would be a
            # division the model has no use for.
            "turn_list",
            "turn_read",
            "skill_use",
            "tool_search",
            "func_tool_load",
        }

        loaded = await call(client, "func_tool_load", {"names": ["spawned__greet"]})
        assert loaded["ok"] is True

        # One step later, without a new turn: the list is rebuilt before every
        # model call, so loading takes effect inside the same turn.
        again = await client.call_tool("list_tools", {})
        assert "spawned__greet" in names(again.data)

        result = await call(client, "spawned__greet", {"name": "ada"})
        assert result == {"text": "hello ada", "ok": True}
