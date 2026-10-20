"""slife2-toolhub: the model's tool list, and the servers behind it.

The hub is a *peer* server — the agent calls it and the model never sees it — so
what these tests are about is the two things it decides: what the model may
call, and what happens when the thing behind a tool is not there.  The second is
the interesting half, because there are three different failures and only one of
them is ours.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import time
import types
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp import MCPError

import slife2.helpers.timeouts as _T
from slife2.clients.gateway import (
    LINK_DOWN_CODE,
    flatten,
    make_client,
    mcp_config,
    proxied_name,
    sanitise,
)
from slife2.libs.host.config import (
    ToolLoadSettings,
    ToolServerSettings,
    default_config,
    load_cached,
)
from slife2.libs.host.paths import catalogue_db, in_home
from slife2.libs.host.paths import home as home_dir
from slife2.libs.plugin.audience import FOR_THE_MODEL, client_meta
from slife2.libs.plugin.family import Family, Link, Links
from slife2.libs.plugin.protocol import LIST_SOURCES
from slife2.libs.store.catalogue import ToolStore
from slife2.plugins.cli.server import build_server as build_cli
from slife2.plugins.toolhub.server import (
    MATCH_FLOOR,
    MAX_LOAD_NAMES,
    PLUGIN,
    WEAK_FLOOR,
    Catalogue,
    _confidence,
    _parameters,
    configured_sources,
    model_name,
)
from slife2.plugins.toolhub.server import (
    build_server as build_hub,
)
from tests.fakes import StubEmbedder, config_file, plugin_transports, upstream_server

#: Two homes' `cli:` sections, for the test that needs one process to serve
#: both: distinct names, because the question is not which one wins.
MINE_CLI = """
cli:
  mine-only:
    command: mine
    description: Recorded in this home.
"""

THEIRS_CLI = """
cli:
  theirs-only:
    command: theirs
    description: Recorded in the other one.
"""

pytestmark = pytest.mark.unit


# --- stand-ins ----------------------------------------------------------------


def hub_for(
    *,
    connected: dict[str, Any] | None = None,
    entries: dict[str, ToolServerSettings] | None = None,
    threshold: int | None = None,
    load_fake_tools: bool = True,
    autoload: tuple[str, ...] = (),
    **kwargs: Any,
) -> FastMCP:
    """A hub over in-db servers, keyed by entry name.

    `connected` maps a name to a *transport*, which is anything a `Client` can
    be built from and therefore always a callable: `lambda settings: server` for
    an in-memory one, or a function that raises, for the tests about a server
    that will not start.  `entries` are
    configured but *not* wired, which is how the one test that spawns a real
    process gets a real transport.

    **Every plugin is here, and the ones with something to say are real.**  They
    are the servers slife2 starts, the hub asks each of them for a tool list, and
    it refuses to hand one out at all when one does not answer — which is what
    `test_a_plugin_that_is_not_answering_fails_the_list` is about, and not
    something every other test should have to trip over.  The db is real because
    the hub is a client of it: the tool catalogue is there, so a hub built here
    has a catalogue, which is what `connected={"db": refuses_to_start}` takes
    away for the tests about that; `skills-server`, `cli-server`, `mcp-tools` and
    `restapi-tools` are real for the same reason — what they declare is what a
    search finds.  Only a name nothing here knows falls through to a blank
    plugin.

    **The fake servers' tools are put in `tool_load.autoload`**, which is what a
    test wants them to be: these are servers whose tools are in the model's
    list, so a test about naming, routing or the sanitiser is about that and not
    about having to load one first.  `load_fake_tools=False` is the opposite —
    `test_a_servers_tools_are_on_demand_until_they_are_loaded` builds the same
    hub without them — and `autoload=` adds names a test needs (a plugin's tool,
    or a fake server's one beyond the three in `upstream_server`).
    """
    base = default_config()
    # The tools first, then the transports: a hub whose catalogue was opened
    # before the entries were added would seed every one of them unloaded.
    config = replace(
        base,
        mcp={
            **{
                name: ToolServerSettings(name=name, command="in-memory")
                for name in [*base.plugins(), *(connected or {})]
                if name not in base.plugins()
            },
            **(entries or {}),
        },
    )
    # `upstream_server`'s three tools, by the names the hub advertises.  A
    # `connected` entry that is another server simply has no rows under these
    # names, and a list entry with no row is a no-op.
    fake = (
        tuple(
            f"{name}__{tool}"
            for name in (connected or {})
            for tool in ("echo", "boom", "read_file")
        )
        if load_fake_tools
        else ()
    )
    # The budget is a hundred by default, which is the right number and not a
    # testable one: a trim is how the eviction *order* is observed from outside,
    # and the order is only visible when something is over the cap.
    config = replace(
        config,
        tool_load=ToolLoadSettings(
            threshold=threshold if threshold is not None else base.tool_load.threshold,
            autoload=(*base.tool_load.autoload, *fake, *autoload),
        ),
    )
    wired = plugin_transports(config, connected, kwargs.get("client_factory"))
    # The embedder is the hub's own now: the catalogue is a file this process
    # opens, and its vectors come from the *same* embedding plugin the context
    # store's do — one hop, one model, or the two indexes would be built by
    # different things and could not be ranked against each other.
    kwargs.setdefault("embedder", StubEmbedder())
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


async def wait_until_ready(hub: Client, name: str, *, timeout: float = 15.0) -> None:
    """Wait until a configured source has answered with its tool list.

    **`list_tools` deliberately does not wait for a server that is still
    starting** — that bound is what keeps an `npx` install out of a turn — so a
    test that wants the real process path has to wait for the source itself.
    Without this a load can run while the child is still coming up, fail with
    "not answering", and the next `list_tools` then omits a tool the test
    expected to be loaded.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = await hub.call_tool("servers", {})
        rows = answer.data["servers"]
        if any(row["name"] == name and row["state"] == "ready" for row in rows):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{name} was not ready within {timeout}s")


#: A transport that is never looked at, and the marker it hands back, for the
#: tests that fake the client — `Client` is what turns a transport into a
#: connection, and those tests replace `Client`.
#:
#: It is a marker rather than `None` because a client factory is handed what the
#: transport *returned*, not the transport itself.  That is how it tells the
#: upstream it is meant to misbehave for from the ones that must keep working —
#: the plugins being the ones that must, since a hub with one of them down
#: lists nothing.
UNUSED = object()


def unused(settings: ToolServerSettings) -> Any:
    return UNUSED


# --- the two families nothing connects to -------------------------------------


def hub_with_documents(cli: Mapping[str, Any] | None = None) -> FastMCP:
    """A hub whose config also carries `cli:` entries.

    A mapping rather than keywords, because the interesting names have hyphens
    in them: `browser-harness` is a `cli:` entry *and* the skill that documents
    it, which is the collision the namespaced row name exists for.
    """
    from slife2.libs.host.config import CliToolSettings

    config = replace(
        default_config(),
        cli={
            name: CliToolSettings(name=name, **entry)
            for name, entry in (cli or {}).items()
        },
    )
    return build_hub(
        config, transports=plugin_transports(config), embedder=StubEmbedder()
    )


@pytest.mark.asyncio
async def test_a_skill_is_a_row_so_a_search_can_find_it(isolated_runtime: Path) -> None:
    """The whole reason a playbook is catalogued: `tool_search` reads rows.

    `skill_use` reads the *folder*, so a skill nobody catalogued is a skill only
    a model that already knew its name could ever read — and finding out what is
    installed is the question a search exists to answer.  The name is namespaced
    because a name is a row's identity and the collision is real: this config
    has `browser-harness` as a `cli:` entry *and* as the skill documenting it.
    """
    write_skill(
        isolated_runtime,
        "browser-harness",
        description="Drive a browser and take screenshots.",
        body="Use CDP to click, type and capture.",
    )
    hub = hub_with_documents(
        {
            "browser-harness": {
                "command": "browser-harness",
                "description": "Direct browser control via CDP.",
            }
        }
    )

    async with Client(hub) as client:
        found = await call(
            client,
            "tool_search",
            {"keywords": ["browser", "screenshots"], "sentences": []},
        )
        listed = await client.call_tool("list_tools", {})

    assert "skill:browser-harness" in found["text"]
    assert "cli:browser-harness" in found["text"], "and the command beside it"
    # ...and neither is a tool, so neither is in the model's list: findable and
    # callable are two different things.
    assert not [name for name in names(listed.data) if ":" in name]


@pytest.mark.asyncio
async def test_a_skill_dropped_in_later_is_findable_at_once(
    isolated_runtime: Path,
) -> None:
    """Installing a skill is dropping a directory in, and the README says so.

    A row written only at the hub's start would make a new playbook *readable*
    and *unfindable* at the same time — the folder is read on every call and the
    catalogue would not be — so a search mirrors first.  A merge of an unchanged
    folder writes nothing, which is what makes it affordable.
    """
    hub = hub_with_documents()

    async with Client(hub) as client:
        before = await call(
            client, "tool_search", {"keywords": ["browser"], "sentences": []}
        )
        write_skill(isolated_runtime, "browser-harness", description="Drive a browser.")
        after = await call(
            client, "tool_search", {"keywords": ["browser"], "sentences": []}
        )

        # And a deleted one stops being a hit: the merge's other half.
        shutil.rmtree(isolated_runtime / "skills" / "browser-harness")
        gone = await call(
            client, "tool_search", {"keywords": ["browser"], "sentences": []}
        )

    assert "skill:browser-harness" not in before["text"]
    assert "skill:browser-harness" in after["text"]
    assert "skill:browser-harness" not in gone["text"]


@pytest.mark.asyncio
async def test_a_switched_off_command_is_a_row_that_says_so() -> None:
    """A `cli:` entry mirrors its own `enabled` flag, and keeps mirroring it.

    The one family whose status the *config* decides, which is why a merge may
    write one: `enabled: false` is a fact about the file, not a state a
    connection could be in.  A re-merge must not undo it — a mirror that
    re-enabled what the operator switched off would do it on every search.
    """
    # Both descriptions carry the word the search is for, because that is now
    # how two rows are seen in one answer: there is no browse to list them with.
    hub = hub_with_documents(
        {
            "yt-dlp": {
                "command": "yt-dlp",
                "description": "Download video from 1000+ sites.",
            },
            "iflow": {
                "command": "iflow",
                "description": "Download nothing.",
                "enabled": False,
            },
        }
    )

    async with Client(hub) as client:
        found = await call(
            client, "tool_search", {"keywords": ["download"], "sentences": []}
        )
        again = await call(
            client, "tool_search", {"keywords": ["download"], "sentences": []}
        )

    assert "cli:yt-dlp" in found["text"]

    def row_named(answer: dict[str, Any], name: str) -> dict[str, Any]:
        rows = json.loads(answer["text"])["results"]
        return next(row for row in rows if row["name"] == name)

    assert row_named(found, "cli:iflow")["status"] == "disabled"
    assert row_named(again, "cli:iflow")["status"] == "disabled", (
        "still off after a re-merge"
    )


@pytest.mark.asyncio
async def test_a_document_answers_for_itself_when_it_is_called(
    isolated_runtime: Path,
) -> None:
    """A search result invites a call, so both families answer like adults.

    The model read the name in a search result and tried it.  What it must not
    get is the answer the routing gives for a *server* that has gone away
    ("not a server this hub is configured with any more") — neither of these
    ever had one, and the step that does reach the thing is one sentence away.
    """
    write_skill(isolated_runtime, "browser-harness", description="Drive a browser.")
    hub = hub_with_documents(
        {"yt-dlp": {"command": "yt-dlp", "description": "Download video."}}
    )

    async with Client(hub) as client:
        read = await call(client, "skill:browser-harness")
        loaded = await call(
            client, "func_tool_load", {"names": ["skill:browser-harness"]}
        )
        command = await call(client, "cli:yt-dlp")
        cli_load = await call(client, "func_tool_load", {"names": ["cli:yt-dlp"]})

    assert read["ok"] is False, "a playbook is not a tool to call"
    assert "skill_use(name='browser-harness')" in read["text"]

    # A load's verdict is in the answer rather than in the call's status: the
    # name did not land, and the reason is what the model acts on.
    assert "a playbook, read with skill_use" in _load_reason(loaded)
    assert "a command already installed" in _load_reason(cli_load)

    assert command["ok"] is False, "a command is not a tool to call either"
    assert "nothing here runs one yet" in command["text"]


# --- what the model may call --------------------------------------------------


@pytest.mark.asyncio
async def test_a_plugins_tools_arrive_through_a_connection_like_anything_else() -> None:
    """Not as a shortcut in the hub, which is the whole point of a plugin.

    It is somebody's server — slife2's own — reached the way every other tool
    is, so it is in `servers()`, it has a connection that can fail, and it would
    appear in whatever a tool search is eventually built on.

    **And its tools arrive under their own names.**  A hop is not something the
    model is told about: `turn_read` came through a connection the same way
    `arxiv__search` does, and a name carrying the server would be the one place
    the hop leaked into what the model reads.
    """
    async with Client(
        hub_for(autoload=("turn_list", "turn_search", "turn_summarize", "turn_read"))
    ) as hub:
        listed = await hub.call_tool("list_tools", {})
        reported = await hub.call_tool("servers", {})

    # Named rather than compared against the whole list: the hub also serves
    # tools of its own (below), and this test is about where a plugin's tools
    # come from, not about the list being exactly this.  The `server` field is
    # what says whose they are — the name no longer does.
    ours = {
        tool["name"] for tool in listed.data["tools"] if tool["server"] == "context"
    }
    assert ours == {"turn_list", "turn_search", "turn_summarize", "turn_read"}
    # Which sources are in the *loaded* list is the operator's list now:
    # `context` (this test puts its four on it), `skills-server` (`skill_use` is
    # required) and the hub's own three.  The other plugins are connected and
    # real all the same — `servers()` reports them — their tools are simply on
    # demand, which is the default for a plugin's tools.
    assert {tool["server"] for tool in listed.data["tools"]} == {
        "context",
        "skills-server",
        "toolhub",
    }
    assert {row["name"] for row in reported.data["servers"]} >= {
        "cli-server",
        "context",
        "mcp-tools",
        "restapi-tools",
        "skills-server",
    }

    # Found by name, not by position: the hub asks every plugin, so each is one
    # row among several and none is first.
    row = next(one for one in reported.data["servers"] if one["name"] == "context")
    assert row["kind"] == "plugin"
    assert row["required"] is True
    assert row["state"] == "ready"
    assert row["tools"] == 4
    assert row["loaded"] == 4, "a plugin's tools are in the model's list"


@pytest.mark.asyncio
async def test_a_plugins_returns_section_reaches_the_catalogue() -> None:
    """**The `Returns:` prose is a fact the hub keeps, and `tools/list` has it.**

    FastMCP drops the section from the description when it parses arguments, so
    before this the catalogue had nothing saying what a tool's answer holds.  The
    plugin's `DeclareReturns` middleware puts it on `_meta`; the hub reads the
    same key and stores it, which is what makes it available without the hub
    knowing anything about a plugin's source.
    """
    async with Client(hub_for()) as hub:
        await hub.call_tool("list_tools", {})

    with sqlite3.connect(catalogue_db()) as connection:
        returns = connection.execute(
            "SELECT returns FROM tool WHERE name = ?", ("turn_list",)
        ).fetchone()

    assert returns is not None, "the context plugin listed turn_list"
    assert returns[0].startswith("`entries`"), returns[0]
    assert "token_count" in returns[0]


def write_skill(
    data_dir: Path,
    name: str = "one",
    *,
    description: str = "d",
    body: str = "The body.",
) -> Path:
    """One skill on disk, in the `skills/` a bare `scan()` looks in."""
    folder = data_dir / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return folder


@pytest.mark.asyncio
async def test_a_local_tool_is_served_by_the_hub_itself() -> None:
    """The set-level tools, and why they are the ones that stayed.

    `tool_search`, `func_tool_load` and `_func_tool_unload` are questions about
    the *whole* catalogue — what exists, what the model is holding, what the
    budget takes back — so the process that owns the set answers them and no
    plugin can.  Their names carry no server because there is none to name:
    they are functions in this process, and the row says so.
    """
    async with Client(hub_for()) as hub:
        listed = await hub.call_tool("list_tools", {})
        reported = await hub.call_tool("servers", {})

    served_here = {
        tool["name"] for tool in listed.data["tools"] if tool["server"] == "toolhub"
    }
    assert served_here == {"tool_search", "func_tool_load", "_func_tool_unload"}
    search = next(one for one in listed.data["tools"] if one["name"] == "tool_search")
    assert search["tool"] == "tool_search"
    # The two inputs, and not the single `query` that preceded them: the keyword
    # leg cannot read a sentence and the semantic leg was never given the words,
    # so the split is the whole point of this schema — and both being `required`
    # is what stops one of the two legs from silently never running
    # (`TOOL_SEARCH_PARAMETERS`).  This assertion named `query` for a while after
    # that field was gone.  `unloaded_only` is the optional third: it narrows a
    # search, and is not one of the two the legs need.
    assert set(search["parameters"]["properties"]) == {
        "keywords",
        "sentences",
        "unloaded_only",
    }
    assert search["parameters"]["required"] == ["keywords", "sentences"]
    # It is a source of tools and not a tool server: there is no connection
    # here for `servers()` to report on.
    assert "toolhub" not in {row["name"] for row in reported.data["servers"]}


@pytest.mark.asyncio
async def test_a_skill_is_served_by_its_own_plugin(isolated_runtime: Path) -> None:
    """A family with nothing to connect to, served by the process that owns it.

    A skill is a document on this machine — no address, no protocol, nothing a
    hop could reach — and the folder it lives in is the config section's other
    half.  Both belong to the skills server rather than to the hub, which is
    what `skills-server` in the `server` field says; the name is still bare
    (`skill_use`, not `{server}__{tool}`), because a plugin is one of ours and
    the model choosing it has no use for which process answers.
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
    assert tool["server"] == "skills-server"
    assert tool["tool"] == "skill_use"
    assert "name" in tool["parameters"]["properties"]

    assert answer["ok"] is True
    assert answer["text"].rstrip().endswith("The body.")
    assert later["ok"] is True

    # The plugin is a server and `servers()` reports it; the *catalogue source*
    # its rows are filed under is not, because a source is not a connection.
    rows = {row["name"]: row for row in reported.data["servers"]}
    assert rows["skills-server"]["state"] == "ready"
    assert rows["skills-server"]["required"] is True
    assert "skills" not in rows


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


# --- what a plugin may declare -------------------------------------------------


def declaring(answer: dict[str, Any]) -> FastMCP:
    """A plugin that answers `list_sources` with exactly what it is handed."""
    server = FastMCP("declaring")

    @server.tool(name=LIST_SOURCES)
    def list_sources() -> dict[str, Any]:
        """Declare whatever the test said to."""
        return answer

    return server


def one_source(**overrides: Any) -> dict[str, Any]:
    """A declaration of one source, with whatever the test is about overridden."""
    source: dict[str, Any] = {
        "name": "somewhere",
        "category": "cli",
        "enabled": True,
        "up": True,
        "rows": [{"name": "cli:thing", "description": "d", "status": "enabled"}],
    }
    source.update(overrides)
    return {"sources": [source]}


async def found(hub: Client, query: str) -> str:
    """What a search for *query* answers with.

    The catalogue is read by *asking for something*, which is the whole of what
    `tool_search` does now: it finds a tool by what it does and cannot
    enumerate, so a test that wants to know whether a row exists asks for the
    row — by its name, which is part of the text a row is found by.
    """
    answer = await call(hub, "tool_search", {"keywords": [query], "sentences": []})
    return str(answer["text"])


def _load_reason(answer: dict[str, Any]) -> str:
    """Why the one name a test asked to load did not land.

    `func_tool_load` answers a verdict per name — `loaded`, and `failed` with a
    reason each — and a test that is about one name wants that reason.
    """
    failed = json.loads(answer["text"])["failed"]
    assert len(failed) == 1, failed
    return str(failed[0]["reason"])


def as_cli_server(answer: dict[str, Any]) -> dict[str, Any]:
    """The wiring that makes a plugin under test be the one that declares."""
    return {"cli-server": lambda settings: declaring(answer)}


class ReDeclaring:
    """A plugin that answers twice, differently.

    What taking an entry out of a section looks like from the hub's side is *the
    same server, answering the next time without it* — so the seam a test needs
    is the answer, not a second plugin.  `declaring` above is the fixed case;
    this one is the same shape with the list made changeable between two asks.
    """

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer

    def server(self) -> FastMCP:
        server = FastMCP("declaring")

        @server.tool(name=LIST_SOURCES)
        def list_sources() -> dict[str, Any]:
            """Declare whatever the test has set now."""
            return self.answer

        return server


@pytest.mark.asyncio
async def test_a_plugin_declares_the_sources_it_holds() -> None:
    """The rows land under the source the *plugin* named — not under the
    plugin's own name, which is the whole reason a source's health can be about
    a connection without its documents being about one."""
    async with Client(hub_for(connected=as_cli_server(one_source()))) as hub:
        rows = await found(hub, "cli:thing")

    assert "cli:thing" in rows


@pytest.mark.asyncio
async def test_a_declared_source_is_not_the_plugins_own_name() -> None:
    """A source's status is written across every row it owns, so a plugin that
    filed what it holds under its own name would have it marked broken whenever
    the plugin faltered."""
    answer = one_source(name="cli-server")
    async with Client(hub_for(connected=as_cli_server(answer))) as hub:
        rows = await found(hub, "cli:thing")

    assert "cli:thing" not in rows


@pytest.mark.asyncio
async def test_a_plugin_may_not_declare_the_category_the_hub_owns() -> None:
    """**`plugin` means *the servers slife2 starts*, and only this process mints
    one.**

    That is the category whose rows the audience gate decides about, so a plugin
    able to declare one could put an unmarked tool in the model's list — which is
    exactly what the gate exists to stop.  It is refused, and the refusal is what
    keeps `slife2.libs.plugin.audience` the only way in for a plugin's own tools.
    """
    answer = one_source(
        category="plugin", rows=[{"name": "sneaky", "status": "enabled"}]
    )
    async with Client(hub_for(connected=as_cli_server(answer))) as hub:
        listed = await hub.call_tool("list_tools", {})
        rows = await found(hub, "sneaky")

    assert "sneaky" not in names(listed.data)
    assert "sneaky" not in rows


@pytest.mark.parametrize("category", ["mcp", "rest", "skill", "cli"])
@pytest.mark.asyncio
async def test_the_other_categories_are_what_a_plugin_is_for(category: str) -> None:
    """Every category but `plugin` is declarable, and the exclusion is the rule.

    What it does *not* move is a trust boundary: these rows are the operator's
    configuration — somebody else's servers, a folder of playbooks, a list of
    commands — which this process merged with no mark at all before, having read
    the same config.  What it does buy is that the two ways a plugin reaches the
    model stay apart: its own tools by `tools/list`, gated; what it *holds*, by
    declaration, where the entry is the opt-in.
    """
    answer = one_source(category=category)
    async with Client(hub_for(connected=as_cli_server(answer))) as hub:
        rows = await found(hub, "cli:thing")

    assert "cli:thing" in rows


@pytest.mark.asyncio
async def test_a_switched_off_source_is_declared_but_not_merged() -> None:
    """`enabled: false` is the operator's switch, and merging is what must not
    happen.

    A plugin cannot list what it does not connect to, so a switched-off entry has
    no rows to declare — and an *empty* list is the one thing it must not send
    either, because a merge is the whole truth about a source and would purge
    exactly the rows this system keeps on purpose.  So it declares the source and
    withholds the list, and the hub writes the state instead: what was already in
    the catalogue stays, saying there is a tool for this and somebody turned it
    off.
    """
    answer = one_source(enabled=False, up=False, rows=None, name="nowhere")
    async with Client(hub_for(connected=as_cli_server(answer))) as hub:
        listed = await hub.call_tool("list_tools", {})
        reported = await hub.call_tool("servers", {})
        rows = await found(hub, "cli:thing")

    assert "cli:thing" not in rows, "nothing was merged, so nothing was inserted"
    assert "nowhere" not in {one["name"] for one in reported.data["servers"]}, (
        "a source with no transport is not a server, so there is no row for it"
    )
    assert "cli:thing" not in names(listed.data)


@pytest.mark.asyncio
async def test_a_switched_off_server_is_reported_as_off() -> None:
    """`enabled: false` is the commonest answer to "why is my tool missing",
    and the one answer a report that left the source out could not give.

    It was left out because the state words were all about a *link* and nothing
    was asked to connect — which made `connecting` a wait nobody was making and
    `failed` an error nobody hit.  The word was added rather than the row dropped
    (`DeclaredSource.snapshot`), because the report is what a person reads and
    "somebody turned it off" is a different problem from a server that will not
    start.
    """
    answer = one_source(
        name="nowhere",
        category="mcp",
        enabled=False,
        up=False,
        rows=None,
        transport="stdio",
    )
    async with Client(hub_for(connected=as_cli_server(answer))) as hub:
        reported = await hub.call_tool("servers", {})

    rows = {row["name"]: row for row in reported.data["servers"]}
    assert rows["nowhere"]["state"] == "off"
    assert rows["nowhere"]["tools"] == "not counted", (
        "it never answered, so this process has no count of it"
    )
    assert rows["nowhere"]["error"] == ""


@pytest.mark.asyncio
async def test_an_uncounted_source_says_not_counted_rather_than_zero() -> None:
    """**A count nobody has is not a count of zero.**

    `servers()` reads its numbers from `catalogue.sources`, which has an entry
    only for a source that has rows.  A source that has never answered used to
    report `tools: 0` / `loaded: 0` — a confident zero about a fact this process
    does not have, on the very tool a person reads when a tool is missing
    (DESIGN.md §11.1, "a value that cannot be read is not a value that is
    absent").  A source that *has* been counted keeps its number, so the two
    cases stay apart.
    """
    answer = one_source(
        name="nowhere", category="mcp", up=False, rows=None, transport="stdio"
    )
    async with Client(hub_for(connected=as_cli_server(answer))) as hub:
        reported = await hub.call_tool("servers", {})

    rows = {row["name"]: row for row in reported.data["servers"]}
    assert rows["nowhere"]["state"] == "connecting"
    assert rows["nowhere"]["tools"] == "not counted"
    assert rows["nowhere"]["loaded"] == "not counted"


@pytest.mark.asyncio
async def test_only_one_of_ours_may_declare() -> None:
    """Somebody else's server defining `list_sources` gets nothing.

    The hub asks every entry under `mcp:` for a tool list and takes all of it,
    so discovery by name would otherwise hand a stranger the one channel into the
    catalogue that the audience gate does not filter.
    """
    async with Client(
        hub_for(connected={"stranger": lambda settings: declaring(one_source())})
    ) as hub:
        rows = await found(hub, "cli:thing")

    assert "cli:thing" not in rows


@pytest.mark.asyncio
async def test_a_plugin_that_is_down_costs_freshness_and_nothing_else() -> None:
    """The refresh runs on the search path, so it may not wait for anybody.

    `begin_connecting` starts an attempt rather than awaiting one, and a source
    that is not up is skipped: the rows it declared last time are still in the
    catalogue, and the next search asks again.  A `settle` here — five seconds,
    for a plugin that may never answer — would put a start-up inside the one
    call a model makes while it is stuck, so the clock is the assertion: the
    answer has to come back well inside that window.
    """

    def refuses_to_start(settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("no such program: slife2-skills")

    started = time.monotonic()
    async with Client(hub_for(connected={"skills-server": refuses_to_start})) as hub:
        found = await call(hub, "tool_search", {"keywords": ["turn"], "sentences": []})
    elapsed = time.monotonic() - started

    assert found["ok"] is True
    assert elapsed < _T.timeouts.ready.list_settle, (
        "the search waited for a plugin that is not coming up"
    )


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
    database.  An entry under `mcp:` needs no mark at all: the operator opted
    in by writing it down, which is what the tests above are listing.
    """
    async with Client(
        hub_for(
            connected={"agent": lambda settings: plugin_with_two_kinds_of_tool()},
            autoload=("digest",),
        )
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
    arrangement and not about the tool: `context__turn_read` told the model
    about a division it has no use for, and the plugin a tool is served by is
    reported in `servers()` to whoever is debugging.  What the prefix is *for*
    is here too: two of somebody else's servers may each offer a `search`.
    """
    async with Client(
        hub_for(
            connected={"fake": lambda settings: upstream_server()},
            autoload=("turn_list", "turn_search", "turn_summarize", "turn_read"),
        )
    ) as hub:
        listed = await hub.call_tool("list_tools", {})

    def called(server: str) -> set[str]:
        return {
            tool["name"] for tool in listed.data["tools"] if tool["server"] == server
        }

    # The same list, the same hop, the same `_advertise`: only the category
    # differs, and it is what the name is decided by.
    assert called("context") == {
        "turn_list",
        "turn_search",
        "turn_summarize",
        "turn_read",
    }
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
    whether the catalogue catches it while planning ("already `context`'s
    tool") or the insert trips its own primary key.  What is not a race is the
    outcome: one of them holds the name, and the model's list does not come back
    short.
    """
    server = FastMCP("plugin")

    @server.tool(meta=FOR_THE_MODEL)
    def turn_list() -> str:
        """A second `turn_list`, which the context store already offers."""
        return ""

    async with Client(hub_for(connected={"agent": lambda settings: server})) as hub:
        with pytest.raises(ToolError, match="a plugin is not answering"):
            await hub.call_tool("list_tools", {})
        reported = await hub.call_tool("servers", {})

    owners = {
        one["name"]: one
        for one in reported.data["servers"]
        if one["name"] in ("context", "agent")
    }
    assert len([one for one in owners.values() if one["state"] == "ready"]) == 1
    refused = next(one for one in owners.values() if one["state"] != "ready")
    # And the report says why, which it is the tool for.  The sentence is
    # asserted rather than merely required to exist: **"not answering" is the
    # wrong answer for a plugin that answered**, which is this comment's whole
    # claim and the half a bare truthiness check left to the reader.
    assert refused["error"], "the report says why a plugin was refused"
    assert "not answering" not in refused["error"], refused["error"]


@pytest.mark.asyncio
async def test_a_servers_tools_are_on_demand_until_they_are_loaded() -> None:
    """The gate, and the whole reason the tool catalogue exists.

    What the model is handed is the tools it has *loaded*, not everything
    installed: a server with ninety tools would otherwise cost a prompt on every
    request.  Only `tool_load.autoload` starts a tool loaded — for a plugin's
    and somebody else's alike — which is what the other tests in this file rely
    on when they put a fake server's names in it.
    """
    entry = ToolServerSettings(name="fake", command="in-memory")
    async with Client(
        hub_for(
            connected={"fake": lambda settings: upstream_server()},
            entries={"fake": entry},
            load_fake_tools=False,
        )
    ) as hub:
        before = await hub.call_tool("list_tools", {})
        assert "fake__echo" not in names(before.data)

        # Findable, though: the catalogue holds it and the search answers for
        # the whole of what is installed, not only what is in front of the model.
        found = await call(hub, "tool_search", {"keywords": ["echo"], "sentences": []})
        assert "fake__echo" in found["text"]

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
    monkeypatch.setattr("slife2.libs.store.catalogue.now", lambda: when)
    entry = ToolServerSettings(name="fake", command="in-memory")
    async with Client(
        hub_for(
            connected={"fake": lambda settings: upstream_server()},
            entries={"fake": entry},
            threshold=1,
            load_fake_tools=False,
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
async def test_a_stamp_that_cannot_be_written_is_said_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**The LRU stamp is a write, and a write that failed is not a write that
    happened.**  `Catalogue.touch` feeds the budget's eviction order, and it is
    written beside the call it stamps — so a suppressed failure leaves the
    budget evicting the wrong tool with nothing anywhere to say so (DESIGN.md
    DESIGN.md §11.1).  The call the model made still succeeded, so this is not the model's
    error; it is logged, naming the tool, out of band.
    """

    def broken(self: ToolStore, name: str) -> int:
        raise RuntimeError("the catalogue file is locked")

    monkeypatch.setattr(ToolStore, "touch", broken)
    caplog.set_level(logging.WARNING, logger="slife2.plugins.toolhub.server")

    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()})
    ) as hub:
        answer = await call(hub, "fake__echo", {"text": "hi"})

    assert answer["ok"] is True, "the tool ran; a failed stamp is not its failure"
    assert any(
        "fake__echo" in record.getMessage() and "stamp" in record.getMessage()
        for record in caplog.records
    ), "the tool that was not stamped is named"


@pytest.mark.asyncio
async def test_loading_says_why_it_cannot() -> None:
    """A name that resolves to nothing is answered with the way to find one.

    The refusals that depend on a row's own state are the store's to make and
    are tested there (`tests/test_toolsdb.py`); what this checks is the hop —
    that the model reads which name did not land and why.
    """
    async with Client(hub_for()) as hub:
        unknown = await call(hub, "func_tool_load", {"names": ["nothing__at_all"]})

    answer = json.loads(unknown["text"])
    assert answer["loaded"] == []
    assert [entry["name"] for entry in answer["failed"]] == ["nothing__at_all"]
    reason = answer["failed"][0]["reason"]
    assert "unknown tool" in reason
    assert "tool_search" in reason, "the way to find the right name"


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
    assert "no tool has that name" not in answer.data["text"], "no prose at all"
    assert json.loads(answer.data["text"])["unknown"] == ["nothing__at_all"]


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
async def test_a_search_with_nothing_to_look_for_is_refused() -> None:
    """A search finds a tool; it does not enumerate them.

    An empty query used to be a *browse* — the answer to "what is installed" —
    and that made this one tool do two jobs: finding a thing by what it does,
    which ranks, and listing everything, which cannot.  The listing is a
    different question with a different shape, so the search declines it.

    **And it names nothing else.**  The refusal used to end "ask for the list",
    pointing at a tool it cannot see; a real turn's log has the model following
    that pointer into `skill_use` with an invented name, and getting "no skill
    called '__list__'".  What is asserted here is that the refusal says which of
    the two inputs would have been a search, and that it promises no other tool.

    Both fields empty is the case the schema's `required` invites — a model can
    fill one with `[]` — and it is refused rather than answered with a page,
    which matters more than it looks: the request that produced this behaviour
    in the wild was a model reading the catalogue ten rows at a time to answer
    "what tools do you have".
    """
    async with Client(hub_for()) as hub:
        omitted = await call(hub, "tool_search", {})
        empty = await call(hub, "tool_search", {"keywords": [], "sentences": []})
        blank = await call(
            hub, "tool_search", {"keywords": ["  "], "sentences": ["\t"]}
        )

    for answer in (omitted, empty, blank):
        assert answer["ok"] is False, "nothing to look for is not a search"
        assert "not a search" in answer["text"], "and it says why"
        assert "keywords" in answer["text"] and "sentences" in answer["text"], (
            "and it names the two inputs that would have been one"
        )
        assert "list" not in answer["text"].lower(), (
            "and it sends the model to no other tool"
        )


@pytest.mark.asyncio
async def test_the_two_inputs_go_to_the_two_legs() -> None:
    """Words are matched exactly, sentences by meaning, and a row both found wins.

    **Splitting them is the point**, not two names for one string: the keyword
    leg asks for every term it is handed, so a sentence would demand six words
    at once and match nothing — which is most of why a sentence-shaped query was
    really a semantic one.  A row the keyword leg found *and* the semantic leg
    found outranks a row only one of them did, which is the fusion doing what it
    is for.
    """
    hub = hub_with_documents(
        {"yt-dlp": {"command": "yt-dlp", "description": "Download video."}}
    )
    async with Client(hub) as client:
        # The word is in the row's own text, so the keyword leg answers alone...
        word = await call(
            client, "tool_search", {"keywords": ["video"], "sentences": []}
        )
        # ...and with no keyword at all, only meaning can find it.  The stub's
        # vectors make a vocab-free sentence match everything, so what this
        # asserts is that the *semantic* leg answered with no keyword given.
        meaning = await call(
            client, "tool_search", {"keywords": [], "sentences": ["download a video"]}
        )
        both = await call(
            client,
            "tool_search",
            {"keywords": ["video"], "sentences": ["download a video"]},
        )

    assert "cli:yt-dlp" in word["text"] and word["ok"]
    assert "cli:yt-dlp" in meaning["text"] and meaning["ok"]
    assert "cli:yt-dlp" in both["text"] and both["ok"]


@pytest.mark.asyncio
async def test_a_keywords_only_search_reaches_the_leg_that_can_answer_it() -> None:
    """The words are a sentence when they are all there is.

    A keywords-only call used to be answered by the keyword leg alone — and that
    leg asks for *every* term it is handed, so two words that no single row holds
    together return nothing even where one of them names the row.  Measured
    against a real catalogue that was 23 of 36 tools found against the semantic
    leg's 36, which is why the call is handed to both (`local_tools.search`).

    The pair here is chosen so the AND cannot be satisfied: `trump` is the stub's
    word for one the row does *not* hold and `工具` is one it does, so nothing
    answers this except the second leg reading the same two words as text.
    """
    hub = hub_with_documents(
        {"papers": {"command": "papers", "description": "Find papers. 工具"}}
    )
    async with Client(hub) as client:
        words = await call(
            client, "tool_search", {"keywords": ["工具", "trump"], "sentences": []}
        )

    assert "cli:papers" in words["text"], "found by meaning, from words alone"


@pytest.mark.asyncio
async def test_a_search_says_how_sure_it_is_and_numbers_its_rows() -> None:
    """The answer is a structure, and its first line is a verdict.

    A flat list of the ten nearest rows is the same shape whether this catalogue
    can answer the question or cannot — and telling those apart is the one thing
    a caller needs from it.  So both are asserted: a page that has an answer says
    the number it found, and a page that has none says so instead of presenting
    ten rows as though they were matches.

    The rows are asserted field by field because that is what a model reads:
    `meaning` is the number that ordered the page, `matched` says whether the
    caller's own words were in that row's text, and `description` may be ninety
    lines long (the whole of it is kept — `_record` argues why).
    """
    hub = hub_with_documents({"x": {"command": "x", "description": "工具"}})
    async with Client(hub) as client:
        found = await call(
            client, "tool_search", {"keywords": [], "sentences": ["工具"]}
        )
        nothing = await call(
            client, "tool_search", {"keywords": [], "sentences": ["trump"]}
        )

    answer = json.loads(found["text"])
    assert answer["confidence"] == "sure", answer
    assert answer["best"] == 1.0

    assert json.loads(nothing["text"])["confidence"] == "none", nothing["text"]

    rows = answer["results"]
    # The precondition, not the claim: the fields below are asserted of every
    # row, and an empty list would make that loop vacuous.
    assert rows, "there are result rows to read"
    for row in rows:
        assert row["meaning"] is not None, f"and carries its number: {row!r}"
    assert rows[0]["name"] == "cli:x", "the row asked for is the one on top"


@pytest.mark.asyncio
async def test_the_rows_the_words_matched_are_listed_first() -> None:
    """A page in two tiers has to *be* ordered that way, or its numbers read as broken.

    Measured on the report's own call: a row the words matched at `meaning 0.38`
    sat above one they did not at `0.56`.  That ordering is right — the words are
    certain and a cosine is graded — but the header named *which* rows were which
    and not that one group is listed above the other, so a reader took "the rest
    are by meaning" as a claim about the whole column and the column looked out
    of order.  The order is the claim now, and it is asserted positionally; what
    the header said about it is `matched`'s per-row field.

    The stub makes the two tiers maximally obvious: `alpha` is the only row the
    words match and scores nothing by meaning, `beta` is the reverse.
    """
    hub = hub_with_documents(
        {
            "alpha": {"command": "alpha", "description": "工具"},
            "beta": {"command": "beta", "description": "测试"},
        }
    )
    async with Client(hub) as client:
        answer = await call(
            client, "tool_search", {"keywords": ["工具"], "sentences": ["测试"]}
        )

    rows = json.loads(answer["text"])["results"]
    assert [row["name"] for row in rows][0] == "cli:alpha", "the worded row is on top"
    assert rows[0]["matched"] == "words", rows[0]
    assert rows[-1]["matched"] == "", "and the other matched nothing"


@pytest.mark.asyncio
async def test_a_match_in_a_tools_returns_text_says_returns_not_parameters() -> None:
    """**The hidden column the words are in is named.**

    `token_count` is in `turn_list`'s `Returns:` prose and in neither its
    description nor its parameters; before `returns` was indexed this query
    could not have matched the row at all, and once it was, the answer still
    pointed at the parameters.  The label is the pointer a reader follows to
    check the mark, so it has to name the column the words are in.
    """
    async with Client(hub_for()) as hub:
        answer = await call(
            hub, "tool_search", {"keywords": ["token_count"], "sentences": []}
        )

    rows = json.loads(answer["text"])["results"]
    row = next(one for one in rows if one["name"] == "turn_list")
    assert row["matched"] == "returns", row


@pytest.mark.asyncio
async def test_tool_search_unloaded_only_hides_what_the_list_already_holds() -> None:
    """**The default is unchanged; the flag is the discovery view.**

    `tool_search` still searches everything unless the caller asks otherwise —
    the best answer may be a tool already in the list, and `loaded` says so on
    the row.  `unloaded_only` is the explicit "show me what I do not have".
    """
    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()})
    ) as hub:
        everything = await call(
            hub, "tool_search", {"keywords": ["echo"], "sentences": []}
        )
        only = await call(
            hub,
            "tool_search",
            {"keywords": ["echo"], "sentences": [], "unloaded_only": True},
        )

    everything_names = {
        row["name"] for row in json.loads(everything["text"])["results"]
    }
    only_names = {row["name"] for row in json.loads(only["text"])["results"]}
    assert "fake__echo" in everything_names, "the loaded tool is part of the default"
    assert "fake__echo" not in only_names, "and the flag is what hides it"


def test_the_confidence_is_the_judgement_the_model_cannot_make_itself() -> None:
    """Three words, pinned where the floors are.

    `_confidence` is called with the page and its best number, so the verdict can
    be asserted without a catalogue — and it has to be *this* function's, because
    the floors are this system's: `best` is a cosine, and what counts as a
    match is not something a reading model can derive from the number.

    The middle case is the one that is easy to get wrong.  The caller's own words
    reaching a row is evidence — certain, where a cosine is graded — so a page
    whose only evidence is that says `weak` rather than `none`, whatever the
    numbers say.
    """
    worded = [{"matched_in": "line"}]
    plain = [{}]

    assert _confidence(plain, 0.7) == "sure"
    assert _confidence(plain, MATCH_FLOOR) == "sure", "the floor is inclusive"
    assert _confidence(plain, 0.2) == "none"
    assert _confidence(worded, 0.2) == "weak", "the words are evidence"
    assert _confidence(plain, WEAK_FLOOR) == "weak", "and so is a close-ish number"


@pytest.mark.asyncio
async def test_a_search_answer_names_no_other_tool() -> None:
    """**What a result carries is the row, not an instruction about the next tool.**

    The answer used to end "Call func_tool_load(name) to put one of these in your
    list."  It is a true sentence, and it is exactly the cross-reference this
    surface is trying not to carry: `func_tool_load` is in every model's list from
    the first turn, its own description says what it does, and a tool's answer
    that explains another tool is a second place the mechanism is written down —
    one that goes stale the moment either name changes.
    """
    hub = hub_with_documents({"yt-dlp": {"command": "yt-dlp", "description": "d"}})
    async with Client(hub) as client:
        answer = await call(
            client, "tool_search", {"keywords": ["yt-dlp"], "sentences": []}
        )

    assert "cli:yt-dlp" in answer["text"], "the row is still the answer"
    assert "func_tool_load" not in answer["text"], answer["text"]


@pytest.mark.asyncio
async def test_a_search_answer_without_results_is_not_nothing_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A search answer that cannot be read is not a search that found nothing.**

    "Nothing matched" is a verdict a model acts on rather than a failure it
    retries, and an answer with no `results` key used to be rendered as exactly
    that — a confident negative made out of damage (DESIGN.md §11.1, "a failure
    is not a verdict").  Only a *readable* empty list may say it; anything else
    comes back as `ok` false with the failure named.
    """

    async def no_results(self: ToolStore, **_: Any) -> dict[str, Any]:
        return {"rows": []}

    monkeypatch.setattr(ToolStore, "search", no_results)

    async with Client(hub_for()) as hub:
        answer = await call(
            hub, "tool_search", {"keywords": ["video"], "sentences": []}
        )

    assert answer["ok"] is False, "an unreadable answer fails the call"
    assert "could not read" in answer["text"], "it is not a verdict, and it says so"
    assert "results" in answer["text"], "naming what was wrong"


def test_a_schema_that_will_not_parse_is_a_tool_with_no_arguments() -> None:
    """**The tolerance is the store's spelling of "nothing to declare", kept on
    purpose.**  The schema column holds a JSON document *or* a sentinel this
    process deliberately does not claim to spell (`_row_of`), so a value that
    will not parse becomes "no arguments" rather than failing the whole list.

    What that gives up is registered rather than hidden: a row whose *real*
    schema was damaged is handed to the model as a tool with no arguments, so
    the model reads the tool's own error several frames from the cause
    (SDD Appendix C).  Failing the row instead would need this half to tell the
    sentinel from the damage, which is the opinion it must not have.
    """
    assert _parameters("n/a") == {}, "the store's sentinel is not damage"
    assert _parameters('{"type": "object"}') == {"type": "object"}
    assert _parameters("[1, 2]") == {}, "a schema that is not a mapping"


@pytest.mark.asyncio
async def test_an_entry_removed_from_the_config_leaves_no_rows() -> None:
    """**The other moment an entry can be removed — slife2 was not running.**

    The mirror of the test below, and the contrast is the whole point:
    `enabled: false` keeps the rows and says so, because the entry is still in
    the section and the switch is the operator's to flip back.  Taking the entry
    *out* is not a switch, and there is nothing left to come back to — so the
    rows go, rather than staying as a search hit that leads to a refusal.  A row
    marked `error` would be found by a search, ranked by the meaning leg and
    counted by the budget, and `func_tool_load` would refuse it with "the server
    that owns it is not answering": a true sentence about a link, and the wrong
    one about a server nothing is configured to ask.

    The boot pass is what does it here (`ToolStore.reset`, handed every name the
    config carries); the same removal while slife2 *is* running is
    `test_an_entry_taken_out_of_a_section_takes_its_rows_with_it` below.
    """
    entries = {"fake": ToolServerSettings(name="fake", command="in-memory")}
    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()}, entries=entries)
    ) as first:
        await first.call_tool("list_tools", {})
        assert "fake__echo" in await found(first, "echo"), "recorded while it ran"

    # The same data directory with the entry gone from `mcp:`.
    async with Client(hub_for()) as second:
        rows = await found(second, "echo")
        refused = await call(second, "func_tool_load", {"names": ["fake__echo"]})

    assert "fake__echo" not in rows
    answer = json.loads(refused["text"])
    reason = answer["failed"][0]["reason"]
    assert answer["loaded"] == []
    assert "unknown tool" in reason, (
        f"a tool whose entry is gone is not a tool whose server is down: {reason}"
    )


@pytest.mark.asyncio
async def test_an_entry_taken_out_of_a_section_takes_its_rows_with_it() -> None:
    """**The runtime half: the entry was removed while the hub was answering.**

    Nothing restarts, so the boot pass never runs — what notices is the plugin
    that holds the section, which now answers `list_sources` without a source it
    held last time.  A source is gone rather than unreachable, and the hub
    settles it the same way the boot pass does, so a removal does not depend on
    whether a process happened to be up when it was made.
    """
    holding = ReDeclaring(one_source(name="cli"))
    async with Client(
        hub_for(connected={"cli-server": lambda settings: holding.server()})
    ) as hub:
        assert "cli:thing" in await found(hub, "cli:thing"), "held, and so catalogued"

        holding.answer = {"sources": []}
        rows = await found(hub, "cli:thing")
        refused = await call(hub, "func_tool_load", {"names": ["cli:thing"]})

    assert "cli:thing" not in rows
    assert "unknown tool" in json.loads(refused["text"])["failed"][0]["reason"]


@pytest.mark.asyncio
async def test_a_plugin_that_stops_answering_keeps_the_rows_it_declared() -> None:
    """**The guard on the other side of that deletion.**

    A plugin that is not answering declares nothing, and "declares nothing" is
    what a removal looks like from the same seat — so the two are told apart by
    the thing that actually differs: whether the plugin *answered*.  One that
    did not is skipped whole, and its sources are left exactly as they were; the
    rows belong to a server that is merely out of reach, and a hub that deleted
    them would empty a catalogue every time a plugin restarted, then re-embed
    every tool when it came back.
    """
    entries = {"fake": ToolServerSettings(name="fake", command="in-memory")}
    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()}, entries=entries)
    ) as first:
        await first.call_tool("list_tools", {})

    def refuses_to_start(settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("no such program: slife2-mcp-tools")

    # `mcp-tools` is the plugin that holds `fake` — with it down, nothing
    # declares the entry, and the entry is still in the config.
    async with Client(
        hub_for(connected={"mcp-tools": refuses_to_start}, entries=entries)
    ) as second:
        rows = await found(second, "echo")

    assert "fake__echo" in rows, "nothing answered for it, so nothing forgot it"


@pytest.mark.asyncio
async def test_a_call_to_a_source_whose_plugin_is_down_says_which_it_is() -> None:
    """**The row is real, so the answer may not say the config is what is wrong.**

    A plugin that is not answering declares nothing, so this home's address book
    loses its sources while their rows stay on purpose (the test above) — and the
    answer for a call to one of them named the two shapes that *cannot* be
    reached: a switched-off entry is still declared, and refused by its plugin
    instead, and an entry whose plugin did answer has had its rows deleted
    (`Upstream.declare`'s `forget_source`).  So the sentence was false about the
    one case it is reached in — the config still names `fake`, and the thing to
    go and look at is the plugin — which is a wrong diagnosis handed to whoever
    reads it, and the reason the hub may not answer a routing failure with a
    guess about the config.
    """
    entries = {"fake": ToolServerSettings(name="fake", command="in-memory")}
    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()}, entries=entries)
    ) as first:
        await first.call_tool("list_tools", {})

    def refuses_to_start(settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("no such program: slife2-mcp-tools")

    # `mcp-tools` is the plugin that holds `fake` — with it down, nothing
    # declares the entry, and the entry is still in the config.
    async with Client(
        hub_for(connected={"mcp-tools": refuses_to_start}, entries=entries)
    ) as second:
        answer = await call(second, "fake__echo")

    assert answer["ok"] is False
    assert "not a server this hub is configured with any more" not in answer["text"]
    assert "'fake'" in answer["text"], "the answer names what the call belongs to"
    assert "`servers`" in answer["text"], "and where the plugin's health is reported"


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
        mcp={"fake": ToolServerSettings(name="fake", command="in-memory")},
    )
    async with Client(
        build_hub(
            live,
            transports=plugin_transports(
                live, {"fake": lambda settings: upstream_server()}
            ),
            embedder=StubEmbedder(),
        )
    ) as first:
        # The list first, as the agent asks for it before every model call —
        # which is also the moment a server's tools become catalogue rows.
        await first.call_tool("list_tools", {})
        found = await call(
            first, "tool_search", {"keywords": ["echo"], "sentences": []}
        )
        assert "fake__echo" in found["text"], "recorded while the server was up"

    # The same data directory, with the entry switched off: no connection, no
    # transport, and the row it left behind.
    off = replace(
        default_config(),
        mcp={
            "fake": ToolServerSettings(name="fake", command="in-memory", enabled=False)
        },
    )
    async with Client(
        build_hub(off, transports=plugin_transports(off), embedder=StubEmbedder())
    ) as hub:
        refused = await call(hub, "func_tool_load", {"names": ["fake__echo"]})
        found = await call(hub, "tool_search", {"keywords": ["echo"], "sentences": []})

    # The verdict, not a failed call: the name did not land, and the answer says
    # which name and why — the reason is the one the model has to act on, and an
    # `Error:` line would carry the same words under a worse label.
    answer = json.loads(refused["text"])
    assert answer["loaded"] == []
    assert answer["failed"][0]["name"] == "fake__echo"
    assert "switched off" in answer["failed"][0]["reason"]

    rows = json.loads(found["text"])["results"]
    # Found *and* marked, which is the whole of what a switched-off entry owes a
    # model: the name is in the answer, and its row says why calling it will not
    # work — where an absent row would say the tool does not exist.
    kept = [row for row in rows if row["name"] == "fake__echo"]
    assert kept, f"shown, not usable: {rows}"
    assert kept[0]["status"] == "disabled", "and the row carries the verdict"


@pytest.mark.asyncio
async def test_the_loaded_set_outlives_the_process() -> None:
    """The point of moving the tool table out of memory and into the catalogue.

    A hub that restarts — or a second conversation on a hub that has been up for
    days — finds the tools that were loaded still loaded, because the row is on
    disk and not in a snapshot.  Nothing here re-asks a server for anything: the
    data directory is the whole of what carried it across.
    """
    connected = {"fake": lambda settings: upstream_server()}
    entries = {"fake": ToolServerSettings(name="fake", command="in-memory")}

    async with Client(
        hub_for(connected=connected, entries=entries, load_fake_tools=False)
    ) as first:
        # The list first, as the agent asks for it before every model call —
        # which is also what makes a server's tools *known* to the catalogue.
        await first.call_tool("list_tools", {})
        assert (await call(first, "func_tool_load", {"names": ["fake__echo"]}))["ok"]

    async with Client(
        hub_for(connected=connected, entries=entries, load_fake_tools=False)
    ) as second:
        listed = await second.call_tool("list_tools", {})

    assert "fake__echo" in names(listed.data)


@pytest.mark.asyncio
async def test_a_restart_does_not_read_the_configs_own_sources_as_gone() -> None:
    """**The window that made a restart look like a broken server.**

    Opening the catalogue runs the file's boot pass, which withdraws the verdict
    on rows whose source the config no longer names (`slife2.libs.store.catalogue.ToolStore.reset`)
    — so the set of names it is handed has to be every name the config carries,
    not the plugins alone (`slife2.plugins.toolhub.server.configured_sources`).  Given the
    plugins alone it read every `mcp:` entry as a source that had gone, and the
    truth came back only when the holding plugin had started, reached that entry
    and declared it: a real section is twenty `npx` servers, so in between a
    search called those tools unusable and `func_tool_load` refused them as "its
    owner is not answering", about a server that answers.

    **The plugin that declares the entry is deliberately unreachable**, so this
    measures the boot pass and nothing else: with `mcp-tools` down, nothing
    re-declares `fake`, so its rows are exactly what the boot pass left.  That is
    the window, held open on purpose.
    """
    entries = {"fake": ToolServerSettings(name="fake", command="in-memory")}

    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()}, entries=entries)
    ) as first:
        await first.call_tool("list_tools", {})
        assert (await call(first, "func_tool_load", {"names": ["fake__echo"]}))["ok"]

    def refuses_to_start(settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("no such program: slife2-mcp-tools")

    async with Client(
        hub_for(connected={"mcp-tools": refuses_to_start}, entries=entries)
    ) as second:
        loaded = await call(second, "func_tool_load", {"names": ["fake__echo"]})

    assert loaded["ok"] is True, (
        "the boot pass withdrew a configured source's verdict, so loading one of "
        f"its tools was refused: {loaded['text']}"
    )


def test_an_entrys_rows_carry_no_verdict_of_their_own() -> None:
    """A `mcp:` entry has a connection, so its rows may not state a status.

    Whether a server is *answering* is the runtime's to say, because the runtime
    is the party that connects — and a family stamping `enabled` on each row
    takes that decision away from it: the merge then writes the verdict as an
    *update* rather than through the reconnect that exists for it
    (`slife2.libs.store.catalogue.ToolStore._plan`), which rewrites the row's keyword document and
    reports a tool nobody touched as changed.  That is the other half of what a
    restart cost: the boot pass withdrew the verdict on every row of every
    `mcp:` entry, and this stamp wrote each one back as a change — measured on
    the real catalogue, 239 rows per restart, logged as `0 added, 239 changed`
    about a config nobody edited.

    Only the two document families carry a status, because neither has a
    connection a verdict could come from — the other side of this is
    `test_a_document_row_carries_its_own_verdict`, and
    `test_a_source_that_answers_again_is_re_enabled_without_rewriting_the_row`
    is the store half of the same rule.
    """
    held = Link(
        ToolServerSettings(name="serper", command="in-memory"),
        home=".",
        directory=".",
        category="mcp",
    )
    row = held._row(
        types.SimpleNamespace(
            name="search", description="Find things.", input_schema=None
        )
    )

    assert "status" not in row
    assert set(row) == {"name", "description", "remote_name", "schema", "returns"}


@pytest.mark.asyncio
async def test_servers_does_not_count_a_dead_servers_tools_as_held() -> None:
    """Two numbers, and only one of them is about now.

    `tools` is what the source last offered — a row on disk, and the reason a
    server that has stopped answering does not look like an empty one.  `loaded`
    says the model is holding them, and a load outlives the connection, so a
    dead server leaves every one of its rows saying `loaded` while the gate hands
    over none of them.  Taken against the live set, that contradiction is what
    this asserts — it was the one number in `servers()` that could disagree with
    `list_tools`.
    """
    entries = {"fake": ToolServerSettings(name="fake", command="in-memory")}

    def refuses_to_start(settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("no such program: in-memory")

    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()}, entries=entries)
    ) as first:
        await first.call_tool("list_tools", {})
        assert (await call(first, "func_tool_load", {"names": ["fake__echo"]}))["ok"]

    async with Client(
        hub_for(connected={"fake": refuses_to_start}, entries=entries)
    ) as second:
        listed = await second.call_tool("list_tools", {})
        reported = await second.call_tool("servers", {})

    rows = {row["name"]: row for row in reported.data["servers"]}
    assert "fake__echo" not in names(listed.data), "down, so it is not in the list"
    assert rows["fake"]["tools"] == 3, "what it last offered is still on disk"
    assert rows["fake"]["loaded"] == 0, "and none of it is in the model's list"


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
        mcp={"fake": ToolServerSettings(name="fake", command="in-memory")},
    )
    hub = build_hub(
        config,
        transports=plugin_transports(
            config, {"fake": lambda settings: upstream_server()}
        ),
        embedder=StubEmbedder(),
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
    assert json.loads(named.data["text"])["unloaded"] == ["fake__echo"], "as JSON"
    assert "fake__echo" not in names(after.data)
    assert "fake__boom" in names(after.data), "only what was named"

    assert idle.data["unloaded"] == []
    assert set(idle.data["text"]) and "unloaded" in idle.data["text"], (
        "said, not silent"
    )

    assert refused.data["refused"] == ["tool_search"]
    assert refused.data["unloaded"] == []


@pytest.mark.asyncio
async def test_unloading_a_document_row_is_not_a_refusal() -> None:
    """**A playbook or a command is not a tool the system protects.**

    `_func_tool_unload` matched `unloaded`/`already`/`unknown` and sent
    everything else to the `refused` arm — and `set_load` answers
    `no_load_state` for the two document categories, which are rows that never
    had a load state to take away.  The model was told "refused (the system
    needs them)" about a row nothing needs, which is a false statement about the
    system made in the system's own voice.  `func_tool_load` has split this
    outcome out from the start; this is the other half of the same answer.
    """
    hub = hub_with_documents({"yt-dlp": {"command": "yt-dlp", "description": "d"}})
    async with Client(hub) as client:
        # A search is what asks every family to declare, so it is also what puts
        # the row in the catalogue this then trims.
        await call(client, "tool_search", {"keywords": ["yt-dlp"], "sentences": []})
        answer = await client.call_tool("_func_tool_unload", {"names": ["cli:yt-dlp"]})

    assert answer.data["refused"] == [], "nothing was told a lie"
    assert answer.data["no_load_state"] == ["cli:yt-dlp"]
    assert "not a tool to unload" not in answer.data["text"], "no prose to parse"


@pytest.mark.asyncio
async def test_the_four_trim_outcomes_are_four_different_words() -> None:
    """**Four outcomes and no fifth, and `refused` means exactly one of them.**

    `set_load` answers one of four when it is asked for `unloaded` — the move,
    `already`, `unknown` and `no_load_state` — because `disabled`/`error` are
    answers to a *load* (`slife2.libs.store.catalogue._load_outcome`).  The trim's last arm sent
    everything it did not recognise to `refused`, which made a fifth outcome look
    reachable: it put "refused (the system needs them)" — a sentence about what
    this process protects — one branch away from being said about a row.  The
    three arms a row can land in are asserted together here, because it is their
    *separation* that was wrong: any two of them collapsed would pass a test of
    one.  The fourth, `no_load_state`, is the document families' and is the
    sibling test above.
    """
    entries = {"fake": ToolServerSettings(name="fake", command="in-memory")}
    async with Client(
        hub_for(
            connected={"fake": lambda settings: upstream_server()},
            entries=entries,
            load_fake_tools=False,
        )
    ) as client:
        # A listing first, so the rows exist: a tool server's are on demand
        # (not in `tool_load.autoload`), and the one loaded here is the one
        # that moves.
        await client.call_tool("list_tools", {})
        await call(client, "func_tool_load", {"names": ["fake__echo"]})
        answer = await client.call_tool(
            "_func_tool_unload",
            {"names": ["fake__echo", "fake__boom", "nowhere", "tool_search"]},
        )

    assert answer.data["unloaded"] == ["fake__echo"], "the one that moved"
    assert answer.data["not_loaded"] == ["fake__boom"], "a row with the state asked for"
    assert answer.data["unknown"] == ["nowhere"], "a name nothing holds"
    assert answer.data["refused"] == ["tool_search"], "and only a name the system needs"


@pytest.mark.asyncio
async def test_a_trim_answer_without_unloaded_is_not_a_trim_that_took_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A trim whose answer cannot be read is not a trim that took nothing out.**

    `_unload_as_text` read its five lists as `found.get(key) or []`, so a `found`
    without an `unloaded` key was rendered as the same "nothing to unload: the
    list is within its budget" a real no-op produces — a verdict a model acts on
    rather than a failure it retries, and the worst one to be wrong about, since
    the model has just lost tools (DESIGN.md §11.1, "a failure is not a verdict").
    Only a readable empty list may say it; anything else raises, and the local
    route answers `ok` false.
    """

    async def no_answer(*_: Any, **__: Any) -> dict[str, Any]:
        return {"rows": []}

    monkeypatch.setattr("slife2.plugins.toolhub.server.unload_tools", no_answer)

    async with Client(hub_for()) as hub:
        answer = await call(hub, "_func_tool_unload", {})

    assert answer["ok"] is False, "an unreadable answer is a failure"
    assert "within its budget" not in answer["text"], "not a verdict"
    assert "could not be read" in answer["text"], "and it names what was wrong"


@pytest.mark.asyncio
async def test_a_call_reaches_the_upstream() -> None:
    async with Client(
        hub_for(connected={"fake": lambda settings: upstream_server()})
    ) as hub:
        result = await call(hub, "fake__echo", {"text": "hello"})

    assert result == {"text": "hello", "ok": True}


@pytest.mark.asyncio
async def test_a_plugins_own_tool_is_called_through_the_hub_like_any_other() -> None:
    """Through the same proxy a third-party tool goes through: one hop to the
    server that serves it, and no branch in `call_tool` that knows it is ours.

    Under its own name, which is the other half of that: the model is not told
    which of the connections behind its list a name came down.
    """
    async with Client(hub_for()) as hub:
        result = await call(hub, "skill_list", {})

    assert result["ok"] is True
    assert json.loads(result["text"])["skills"] == [], "the real plugin's answer"


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
    # Not `"weather" in result["text"]`: `unknown_tool` interpolates the name it
    # was asked for into every answer, so that line passed whatever the answer
    # said.  Naming an alternative is the claim, and it is the check below.
    assert "tool_search" in result["text"], result["text"]


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

    async with Client(
        hub_for(connected={"broken": refuses_to_start}, autoload=("turn_list",))
    ) as hub:
        listed = await hub.call_tool("list_tools", {})
        reported = await hub.call_tool("servers", {})

    # Our plugins are still there, and the model simply has fewer tools.  So
    # is everything the hub serves itself, which never depended on anybody
    # connecting — including `_func_tool_unload`, the one `_`-prefixed name a
    # model sees, because the harness's trim is recorded as a call to it.
    # Found by whose they are rather than by the shape of the name, which no
    # longer says it: a broken entry's `broken__anything` would have passed for
    # one of ours under the old filter.
    served_here = {
        tool["name"] for tool in listed.data["tools"] if tool["server"] == "toolhub"
    }
    assert served_here == {
        "tool_search",
        "func_tool_load",
        "_func_tool_unload",
    }
    # And `skill_use`, which used to be one of those and is now a plugin's.
    assert "skill_use" in {
        tool["name"]
        for tool in listed.data["tools"]
        if tool["server"] == "skills-server"
    }
    assert "turn_list" in names(listed.data)
    assert "broken__anything" not in names(listed.data)

    rows = {row["name"]: row for row in reported.data["servers"]}
    assert rows["broken"]["state"] == "failed"
    assert "no such program" in rows["broken"]["error"]
    assert rows["broken"]["tools"] == "not counted", "it never listed anything"


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
    """The rule that separates a plugin from an entry in `mcp:`.

    A model that has quietly lost `turn_read` is a failure nobody can see,
    so the hub refuses rather than serving a shorter list.  An upstream that is
    merely down is the opposite case and is left out instead — see
    `test_a_broken_upstream_leaves_the_hub_working`, which is the same setup
    with the flag the other way.
    """

    def refuses_to_start(settings: ToolServerSettings) -> Any:
        raise FileNotFoundError("no such program: python")

    async with Client(hub_for(connected={"skills-server": refuses_to_start})) as hub:
        with pytest.raises(ToolError, match="a plugin is not answering"):
            await hub.call_tool("list_tools", {})

        # The report still works, and says which one and why.
        reported = await hub.call_tool("servers", {})

    row = next(
        one for one in reported.data["servers"] if one["name"] == "skills-server"
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
    assert "tool_search" in result["text"]


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
            # The dispatcher's own refusal for a send onto a torn-down
            # transport: "nothing reached the far end", which is the only case
            # that earns a retry (`slife2.clients.gateway.LINK_DOWN_CODE`).
            raise MCPError(code=LINK_DOWN_CODE, message="Connection closed")
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text="answered")], is_error=self.refuses
        )


def clients(*, dies: bool = False, refuses: bool = False, first_dies: bool = False):
    """A client factory, and the list it appends each client it builds to.

    **Fake only for the upstream under test**, which it recognises by the
    marker its transport returned: the plugins are required upstreams, and a
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
async def test_a_call_that_dies_at_the_transport_is_answered_and_the_next_rebuilds() -> (
    None
):
    """**A call that went out is never asked again** — and the source does not stay broken.

    The link died with the answer on its way, so the far end may have run the
    call: a retry of it is a second render, a second file write, a second
    message sent.  `slife2.clients.gateway` reads no exception as licence to
    retry, because the SDK raises the same code for a request that was in flight
    as for a send onto a link already gone.

    What the model gets is the failure, and what the *next* ask gets is a new
    link — connecting and asking there is a first attempt rather than a second
    one, which is the half that keeps one dropped link from costing every call
    that follows.
    """
    make, made = clients(first_dies=True)

    async with Client(hub_for(connected={"fake": unused}, client_factory=make)) as hub:
        first = await call(hub, "fake__echo")
        second = await call(hub, "fake__echo")

    assert first["ok"] is False, "the call that went out is answered, not run again"
    assert second == {"text": "answered", "ok": True}, "and the next one reconnects"
    assert len(made) == 2, "one connection per call, and no rebuild of its own"


@pytest.mark.asyncio
async def test_an_upstream_that_is_down_costs_one_attempt_per_call() -> None:
    """The other half: a dead server must not be hammered either.

    Named for the *upstream*, because `slife2.clients.gateway` has this same
    test at the link's own layer: this one is the hub's half — a `mcp:` entry,
    reached through the plugin that holds it.  Every call costs one attempt, and
    the failure names the entry rather than being retried into a second one.
    """
    make, made = clients(dies=True)

    async with Client(hub_for(connected={"fake": unused}, client_factory=make)) as hub:
        result = await call(hub, "fake__echo")

    assert result["ok"] is False
    assert len(made) == 1, "one attempt, because the attempt is what may have run"


@pytest.mark.asyncio
async def test_an_upstream_refusal_does_not_rebuild_the_link() -> None:
    """The peer answered.  Rebuilding would only be told the same thing again."""
    make, made = clients(refuses=True)

    async with Client(hub_for(connected={"fake": unused}, client_factory=make)) as hub:
        result = await call(hub, "fake__echo")

    assert result["ok"] is False
    assert len(made) == 1, "a peer-reported error is not a transport failure"
    assert made[0].calls == 1


@pytest.mark.asyncio
async def test_a_slow_connect_is_not_cancelled_by_the_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug a live run against a real endpoint found, and the reason the
    waiting is an `asyncio.wait` rather than a timeout around a gather.

    Giving up on *waiting* for a connection is not a reason to stop making it:
    the gather spelling cancels the attempt it was waiting on, which leaves the
    connection `idle` with no error and no tools — the one state that describes
    nothing at all.

    The wait is the *family's* now, since it is the family that holds the link to
    the entry under test: the hub's own `settle` is about its links to the
    plugins, and a plugin answers `list_sources` whether or not its entries have
    come up yet.
    """
    monkeypatch.setattr(_T.timeouts.ready, "list_settle", 0.05)

    class Slow(FakeClient):
        async def __aenter__(self) -> FakeClient:
            await asyncio.sleep(0.2)
            return self

    def make(transport: Any, handler: Any) -> Any:
        # Fake only for the entry under test, recognised by the marker its
        # transport returned: this factory now reaches the *plugin* too, and a
        # hub whose client to `mcp-tools` were a fake would have a plugin that
        # declares nothing.
        if transport is not UNUSED:
            return make_client(transport, handler)
        return Slow()

    async with Client(hub_for(connected={"fake": unused}, client_factory=make)) as hub:
        # The first ask is answered while the connect is still in flight, which
        # is the point: a declaration says "not up yet" rather than holding the
        # ask open, and the attempt keeps running in the background.
        early = await hub.call_tool("servers", {})
        assert "fake" in {one["name"] for one in early.data["servers"]}

        # The attempt was left alone, so it finishes on its own — and the next
        # ask is the one that finds it.
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


class GatedClient:
    """A client whose teardown waits for the test to let it go.

    The window `Family.close` is exposed to is *inside* its loop, and the only
    way to hold it open on purpose is to make the thing it awaits — a client's
    `__aexit__` — block until the test says otherwise.
    """

    def __init__(self, gate: asyncio.Event, entered: asyncio.Event) -> None:
        self._gate = gate
        self._entered = entered

    async def __aenter__(self) -> GatedClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._entered.set()
        await self._gate.wait()

    async def list_tools(self) -> list[Any]:
        return [
            types.SimpleNamespace(
                name="echo", description="Echo.", input_schema={"type": "object"}
            )
        ]


@pytest.mark.asyncio
async def test_the_family_teardown_survives_a_management_call() -> None:
    """**`Family.close` mutates nothing, but it does suspend.**

    It awaits inside its loop — a cancelled attempt, then the client's own
    `__aexit__` — and a management tool landing between two iterations
    (`mcp_set`, `mcp_remove`, `*_set_enabled`, all reached through the family's
    own server) changed `_held` underneath the iteration, so the lifespan's
    teardown died with "dictionary changed size during iteration" and left every
    link after the one being closed still open.  It is the last moment the
    process has to drop them, so it is the worst one to lose.

    **The links are the pool's, so this holds the `Connection` objects
    themselves** (`Links`): letting go of a hold and closing the connection are
    two steps now, and it is the second one this is about — a teardown that
    dropped every hold and closed one link would read as success otherwise.
    """
    gate, entered = asyncio.Event(), asyncio.Event()
    family = Family(
        category="mcp",
        entries=[
            ToolServerSettings(name="one", command="in-memory"),
            ToolServerSettings(name="two", command="in-memory"),
        ],
        # One pool for this family alone, which is what a server's own `Links` is:
        # nothing here is shared, so nothing here depends on another home.
        links=Links(),
        client_factory=lambda transport, handler: GatedClient(gate, entered),
    )
    one, two = family.held("one"), family.held("two")
    assert one is not None and two is not None
    first, second = one.connection, two.connection
    await first.ready()
    await second.ready()

    closing = asyncio.create_task(family.close())
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    # A management call arriving mid-teardown.  Switched off, so it starts no
    # attempt of its own: what is under test is the mapping growing, not a link.
    await family.put(
        ToolServerSettings(name="three", command="in-memory", enabled=False)
    )
    gate.set()
    await closing

    assert family.held("three") is not None, "the management call did land"
    assert not first.usable
    assert not second.usable, "the rest of the loop ran"


# Two tests lived here — "the catalogue reconnects when the db goes away" and
# "a refusal is not a catalogue that is gone" — plus the `FlakyCatalogue` they
# shared, and all three were about a *link*: a kept MCP client, a transport
# failure worth exactly one reconnect, and the SDK's own `ToolError`-versus-
# transport distinction that told a name collision apart from a dead peer.  The
# catalogue is a file this process opens now, so there is no link to keep,
# nothing to reconnect, and no second kind of failure to hold apart: a refusal
# is a `ValueError` out of `ToolStore` and anything else is this process come
# apart.  `slife2.plugins.toolhub.server._refused` is where that line is drawn, and what it
# decides — one source's bad rows do not fail the tool list, a broken store does
# — is covered by the tests about a source that offers a name somebody owns and
# by the three below.


class YieldingEmbedder(StubEmbedder):
    """`StubEmbedder` with a suspension point in it, as a real embed call is.

    The catalogue's merge is three awaited steps — plan, embed, write — and the
    window a second merge can slip into is between the first and the last, so
    the embed has to be one of them.  The stub's own `embed` never yields, which
    is exactly why a test that wants the interleave cannot use it.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(0)
        return await super().embed(texts)


def a_catalogue_that_cannot_record(source: str) -> tuple[Any, dict[str, bool]]:
    """One source's listing, and a catalogue that cannot take it.

    Returns `(merge, state)`: monkeypatch the first over `ToolStore.merge`, and
    flip `state["broken"]` to let the catalogue work again.  The seam exists
    because a *store* raising is not something a config can express, and what is
    under test is what the hub does about it rather than how it happens.
    """
    working = ToolStore.merge
    state = {"broken": True}

    async def merge(self, source_id, category, rows, **kwargs):
        if source_id == source and state["broken"]:
            raise RuntimeError("the embedding model is down")
        return await working(self, source_id, category, rows, **kwargs)

    return merge, state


def a_catalogue_that_cannot_record_in(path: Path) -> Any:
    """A catalogue that cannot take *any* listing into one home's file.

    `a_catalogue_that_cannot_record`'s seam, keyed by the file rather than by the
    source — because the claim is about which *home* a fault belongs to, and the
    source in question is the same one in both.
    """
    working = ToolStore.merge

    async def merge(self, source_id, category, rows, **kwargs):
        if self.path == path:
            raise RuntimeError("that home's catalogue file is locked")
        return await working(self, source_id, category, rows, **kwargs)

    return merge


@pytest.mark.asyncio
async def test_a_catalogue_that_cannot_record_fails_the_list_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The third failure rule, and the one that used to reach nobody at all.**

    A catalogue that cannot take a listing is a broken system too, and it must
    not be filed as the *source's* problem: `context` answered perfectly well, so
    a row saying it is "connecting" would send the operator to the wrong place.
    What the old code did was worse than a wrong word — it raised inside the
    connect attempt that nothing awaits, so the reason arrived only as
    asyncio's GC-time "Task exception was never retrieved", naming neither the
    server nor the cause, and every ask after it failed identically forever.
    """
    merge, _state = a_catalogue_that_cannot_record("context")
    monkeypatch.setattr(ToolStore, "merge", merge)

    async with Client(hub_for()) as hub:
        with pytest.raises(ToolError, match="the embedding model is down"):
            await hub.call_tool("list_tools", {})

        # The report still works, which is the whole job of `servers` — and the
        # catalogue is not a source, so it is a field rather than a row.
        reported = await hub.call_tool("servers", {})

    assert "the embedding model is down" in reported.data["catalogue"]
    rows = {row["name"]: row for row in reported.data["servers"]}
    assert rows["context"]["error"] == "", "it answered; it is not the broken one"


@pytest.mark.asyncio
async def test_a_catalogue_answer_without_tools_fails_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A shape a hub cannot read is not a hub with no tools.**

    An answer with no `tools` key used to be served as an empty tool list, which
    is the one answer a model cannot tell from a hub that has nothing — a shape
    change would hand it zero tools and report success.  DESIGN.md §4.2 says the
    opposite in as many words: a hub that cannot read a plugin's tool list
    refuses to list anything, so the list fails and names what came back
    (DESIGN.md §11.1, "a value that cannot be read is not a value that is absent").
    """
    monkeypatch.setattr(ToolStore, "injectable", lambda self, sources: {"rows": []})

    async with Client(hub_for()) as hub:
        with pytest.raises(ToolError, match="without a tool list"):
            await hub.call_tool("list_tools", {})


@pytest.mark.asyncio
async def test_a_catalogue_that_works_again_is_not_still_reported_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record is cleared by the merge that works, or the hub stays broken.

    A failure that outlived its cause would be a hub refusing to list anything
    long after the embedding endpoint came back — the same "connecting, forever"
    shape, reached from the other side.
    """
    merge, state = a_catalogue_that_cannot_record("context")
    monkeypatch.setattr(ToolStore, "merge", merge)

    async with Client(hub_for(autoload=("turn_read",))) as hub:
        with pytest.raises(ToolError, match="the embedding model is down"):
            await hub.call_tool("list_tools", {})

        # The failed listing dropped the link, so this ask re-lists the plugin
        # and the merge is tried again — and this time it works.
        state["broken"] = False
        listed = await hub.call_tool("list_tools", {})
        reported = await hub.call_tool("servers", {})

    assert "turn_read" in names(listed.data)
    assert reported.data["catalogue"] == ""


@pytest.mark.asyncio
async def test_two_asks_at_once_do_not_race_the_catalogue() -> None:
    """**One hub answers every conversation, so two asks really do overlap.**

    `ToolStore.merge` plans its writes and applies them in a second pass with an
    embed in between, so two overlapping merges of one source are a
    check-then-act race on the tool table's primary key: both plan an insert for
    a name neither has stored, and the second one is an `IntegrityError` — a
    conversation failing over what is a scheduling accident.

    Two details make the window real rather than theoretical.  The source is
    **declared for the first time at the ask**, not at startup: a row already
    stored plans an update and there is nothing to race for.  And the embed
    **yields** (`YieldingEmbedder`), because a merge over a wire does and the
    stub's does not — which is the whole of the window.
    """
    declaring = ReDeclaring({"sources": []})
    async with Client(
        hub_for(
            connected={"cli-server": lambda settings: declaring.server()},
            embedder=YieldingEmbedder(),
        )
    ) as hub:
        # Nothing is held yet, so every one of these asks plans the same inserts.
        declaring.answer = one_source()
        listed = await asyncio.gather(
            *(hub.call_tool("list_tools", {}) for _ in range(3))
        )
        rows = await found(hub, "cli:thing")

    assert all("tools" in one.data for one in listed)
    assert "cli:thing" in rows


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
    assert model_name("context", "turn_read", PLUGIN) == "turn_read"
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
        # The entry is not in `tool_load.autoload`, so its tool is *not* in the
        # list yet and the model has to ask for it.  That is the whole of the
        # mechanism, end to end: no per-server flag, so the search-and-load path
        # is what puts a server's tools in front of a model.
        listed = await client.call_tool("list_tools", {})
        assert names(listed.data) == {
            # The config's required names that have a row here: the hub's own
            # three, and the playbook reader.  `_check_new_input` is required
            # too, but this test's `agent` plugin is a blank one, so nothing
            # serves it — a name in the list with no row is a no-op.
            "_func_tool_unload",
            "skill_use",
            "tool_search",
            "func_tool_load",
        }

        # The child is a real process. `list_tools` starts its connect and does
        # not wait for it, so the load below has to be given a source that has
        # answered — otherwise it fails with "not answering" and the next list
        # omits the tool for a reason the test never checked.
        await wait_until_ready(client, "spawned")

        loaded = await call(client, "func_tool_load", {"names": ["spawned__greet"]})
        assert loaded["ok"] is True
        payload = json.loads(loaded["text"])
        assert payload["loaded"] == ["spawned__greet"], payload
        assert payload["failed"] == [], payload

        # One iteration later, without a new turn: the list is rebuilt before every
        # model call, so loading takes effect inside the same turn.
        again = await client.call_tool("list_tools", {})
        assert "spawned__greet" in names(again.data)

        result = await call(client, "spawned__greet", {"name": "ada"})
        assert result == {"text": "hello ada", "ok": True}


@pytest.mark.asyncio
async def test_a_second_home_is_served_its_own_declarations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**One hub, one set of plugins, two homes — and two tool sets.**

    What a plugin declares is its section of the *caller's* `slife2.yaml`, so a
    hub answering two homes has two address books and two catalogues
    (`Catalogue.store`).  Held for the process, the second home's search would
    find the first one's commands and route its calls to servers configured in a
    directory that home has never heard of.

    The cli server is the real one, reading the caller's file (`load_cached`),
    because that is the half under test — every other plugin here is the fixed
    stand-in the rest of this module uses.
    """
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))
    config_file(mine, MINE_CLI)
    config_file(theirs, THEIRS_CLI)
    base = default_config()

    async def search(hub: Client, home: Path, query: str) -> str:
        """A search as the caller in *home*: the meta is what names it."""
        with in_home(home):
            answer = await hub.call_tool(
                "call_tool",
                {
                    "name": "tool_search",
                    "arguments": {"keywords": [query], "sentences": []},
                },
                meta=client_meta("jack"),
            )
        return str(answer.data["text"])

    hub = build_hub(
        base,
        source=load_cached,
        transports={
            # The stand-ins first and the one that matters last: a dictionary
            # spread the other way round would quietly put the fixed cli-server
            # back and leave this testing nothing.
            **plugin_transports(base, {}, None),
            "cli-server": lambda settings: build_cli(base, source=load_cached),
        },
        embedder=StubEmbedder(),
    )
    async with Client(hub) as client:
        here = await search(client, mine, "cli:mine-only")
        there = await search(client, theirs, "cli:theirs-only")
        # ...and neither home finds the other's, which is the half that a table
        # keyed by nothing but the source name would get wrong.
        missing = await search(client, mine, "cli:theirs-only")

    assert "cli:mine-only" in here
    assert "cli:theirs-only" in there
    assert "cli:theirs-only" not in missing


@pytest.mark.asyncio
async def test_a_second_home_holds_every_tool_the_machine_has(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A home's catalogue is complete from the moment it exists.**

    The hub reaches its plugins *itself* (`upstreams`), and one set of links
    serves every home — so what those links list is a fact about the machine, and
    a file built from the caller's own declarations alone is missing all of it:
    the hub's own tools, which are what finds and loads everything else, and
    every plugin's.  Measured before this against the real plugin set, that was
    twenty-four of twenty-eight tools missing — and a home nobody had sent a
    message in did not get a file at all.

    A listing is re-read only when the link is due (`Connection.connecting`,
    `timeouts.pacing.relist_after`), so the second home cannot repair this by
    waiting for one: what it is missing has to be written from what the hub
    already holds (`Catalogue.remember`, `Catalogue.seed`).
    """
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))
    config_file(mine, MINE_CLI)
    config_file(theirs, THEIRS_CLI)
    base = default_config()

    hub = build_hub(
        base,
        source=load_cached,
        transports=plugin_transports(base, {}, None),
        embedder=StubEmbedder(),
    )

    async def listed(hub_client: Client, home: Path) -> set[str]:
        """What the model may call, asked as the caller in *home*."""
        with in_home(home):
            answer = await hub_client.call_tool(
                "list_tools", {}, meta=client_meta("jack")
            )
        return names(answer.data)

    async with Client(hub) as client:
        here = await listed(client, mine)
        there = await listed(client, theirs)

    assert here, "the machine's plugins offer the model something"
    assert not (here - there), f"the second home is missing {sorted(here - there)}"
    # And the home has a catalogue of its own rather than an answer borrowed
    # from the first one's — `catalogue_db` is `agents/tools.db`, and until this
    # change a home that had sent no message had no such file.
    assert (theirs / "agents" / "tools.db").is_file()


@pytest.mark.asyncio
async def test_a_relist_reaches_a_home_that_is_not_asking(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A re-read of a plugin belongs to every home, not to the asking one.**

    A link is re-read on the ask that finds it due, and that ask belongs to one
    home — but what the re-read found is the machine's tool set, so a home that
    asked earlier would otherwise be holding a list the hub no longer agrees
    with, and would keep holding it until *its* next ask happened to be the one
    that found the link due.

    Read out of the first home's file rather than asked of the hub again: asking
    would re-list the plugin and merge the tool in on its own, which is the one
    thing this test must not do.
    """
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))
    config_file(mine, "")
    config_file(theirs, "")
    # Every ask finds the link due, so the second home's ask is a re-read.
    monkeypatch.setattr(_T.timeouts.pacing, "relist_after", 0.0)
    base = default_config()

    grown = FastMCP("grown")

    @grown.tool(meta=FOR_THE_MODEL)
    def first() -> str:
        """What this plugin offered before anybody asked."""
        return ""

    transports = {**plugin_transports(base, {}, None), "media": lambda _: grown}
    hub = build_hub(
        base, source=load_cached, transports=transports, embedder=StubEmbedder()
    )

    async def listed(hub_client: Client, home: Path) -> None:
        with in_home(home):
            await hub_client.call_tool("list_tools", {}, meta=client_meta("jack"))

    async with Client(hub) as client:
        await listed(client, mine)

        # The plugin grows a tool, and nothing asks in `mine` again.
        @grown.tool(meta=FOR_THE_MODEL)
        def second() -> str:
            """What it grew after the first home had asked."""
            return ""

        await listed(client, theirs)

    held = ToolStore(mine / "agents" / "tools.db", threshold=100)
    assert held.route("second") is not None, "the re-read stopped at the asking home"


@dataclass
class SlowOpenEmbedder(StubEmbedder):
    """A `StubEmbedder` that stops inside the first embedding it is asked for.

    **The window under test is measured in embedding calls.**  A store's open is
    `seed`'s writes — one call behind each source — and then `sync_indexes`,
    which may re-embed the whole catalogue; parking the first call is parking the
    open at a place the test can stand on, rather than racing a sleep against
    whatever the machine is doing.  `reached` says the park happened, `proceed`
    is what lets the open go.
    """

    pause: bool = False
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    proceed: asyncio.Event = field(default_factory=asyncio.Event)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.pause:
            self.pause = False
            self.reached.set()
            await self.proceed.wait()
        return await StubEmbedder.embed(self, texts)


@pytest.mark.asyncio
async def test_a_listing_that_lands_during_an_open_reaches_that_store(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The window between `seed`'s snapshot and the file's publication.**

    `Catalogue.store()` builds a home's file and the build is not instant:
    `seed` writes every source the machine holds and `sync_indexes` may re-embed
    the whole catalogue.  Two writers can put a source into that file, and one
    landing *inside* the window was covered by neither — the fan-out at the end
    of `remember` walks `_stores`, which cannot hold a file that has not
    finished opening (publishing it is the last thing `store` does), and `seed`'s
    snapshot of `_held` was taken before those rows were held.  So the source
    landed in every home *but* the one that was opening, and nothing repaired it:
    the next write for that source is its next relist
    (`slife2.helpers.timeouts.timeouts.pacing.relist_after`, minutes) or a restart.

    **The halves are exclusive now** — `remember` holds `_held` and does the
    fan-out under the lock an open is under — so a listing is either in the
    snapshot (`seed` writes it) or held after the file was published (the
    fan-out finds it).  This drives the window open by hand, with the open parked
    inside `seed` and one source already held so that there is a write to park
    in, and reads the files rather than asking the hub again: asking would
    re-list the machine's sources and repair the very thing under test.
    """
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))
    config = default_config()
    embedder = SlowOpenEmbedder()

    async def open_store() -> ToolStore:
        """The three steps `open_catalogue` is: build the file, seed it, index it."""
        store = await asyncio.to_thread(
            ToolStore, catalogue_db(), threshold=100, known=configured_sources(config)
        )
        await catalogue.seed(str(home_dir()), store)
        await store.sync_indexes(embedder)
        return store

    async def embedder_of_call() -> StubEmbedder:
        return embedder

    catalogue = Catalogue(open_store, embedder_of_call)

    # A first home, and one source the machine already holds: that source is what
    # the second home's open has to write, and therefore where its open can be
    # parked.
    await catalogue.store()
    await catalogue.remember(
        "machine", PLUGIN, [{"name": "first", "description": "Already held."}]
    )

    embedder.pause = True
    with in_home(theirs):
        opening = asyncio.create_task(catalogue.store())
    await embedder.reached.wait()  # inside `seed`, past its snapshot

    # A listing lands now, from the home that is *not* opening — what the hub's
    # own relist is: a fact about the machine, so every home's file gets it.
    with in_home(mine):
        landing = asyncio.create_task(
            catalogue.remember(
                "late", PLUGIN, [{"name": "late__one", "description": "Mid-open."}]
            )
        )

    # **The open is let go as soon as the listing has finished**, which is the
    # order that loses the rows — and it is the listing that says so rather than
    # a sleep, so the failure cannot be timed away.  Blocked on the open's lock,
    # the listing never finishes, and the timer is then what ends the wait.
    loop = asyncio.get_running_loop()
    landing.add_done_callback(lambda _: embedder.proceed.set())
    let_go = loop.call_later(0.5, embedder.proceed.set)
    await asyncio.gather(opening, landing)
    let_go.cancel()

    opened = ToolStore(theirs / "agents" / "tools.db", threshold=100)
    assert opened.route("first") is not None, "the open finished its own seeding"
    assert opened.route("late__one") is not None, (
        "the listing landed in every home but the one that was opening"
    )


class DeclaringPerHome:
    """A plugin whose answer is the *caller's* list, the way a family plugin's is.

    **The shape a process-global set gets wrong.**  What a plugin holds is its
    section of the caller's `slife2.yaml`, so the same server holds one home's
    list and answers nothing at all for another's — which the stubs that answer
    one fixed list for everybody cannot express.  `home_dir()` inside the call is
    the caller's (`slife2.libs.plugin.audience.with_home` is what puts it there),
    and the answer is looked up in it.
    """

    def __init__(self) -> None:
        #: By the home the call is for, which is what a section is.
        self.per_home: dict[str, dict[str, Any]] = {}

    def server(self) -> FastMCP:
        server = FastMCP("declaring-per-home")
        per_home = self.per_home

        @server.tool(name=LIST_SOURCES)
        def list_sources() -> dict[str, Any]:
            """Declare what *this* home holds, and nothing for any other."""
            return per_home.get(str(home_dir()), {"sources": []})

        return server


@pytest.mark.asyncio
async def test_a_source_one_home_stopped_holding_is_forgotten(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**"What it held last time" is per home, because the answer is.**

    `declare` asks a plugin for the *calling* home's section, so the names it
    answers with are that home's entries.  A set kept for the process is
    therefore one home's answer read as another's — and the damage is not a
    wrong row but a *deletion*: a home that holds nothing declares nothing, so
    the difference against a set another home's ask filled is that home's
    sources, and `forget_source` runs against *this* home's catalogue, where the
    rows are this home's tools.  The set then holds nothing, so the home that
    did hold them can never compute the removal a deletion is for: an entry
    taken out of its section leaves its rows standing for the life of the
    process, and a search keeps offering tools the row's own text says go the
    next time the plugin that held them answers.
    """
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))
    plugin = DeclaringPerHome()
    plugin.per_home[str(mine)] = one_source(name="cli")

    async def asked(hub: Client, home: Path, query: str) -> str:
        """One search as the caller in *home*: the meta is what names it."""
        with in_home(home):
            answer = await hub.call_tool(
                "call_tool",
                {
                    "name": "tool_search",
                    "arguments": {"keywords": [query], "sentences": []},
                },
                meta=client_meta("jack", home=str(home)),
            )
        return str(answer.data["text"])

    async with Client(
        hub_for(connected={"cli-server": lambda settings: plugin.server()})
    ) as hub:
        held = await asked(hub, mine, "cli:thing")
        # **The other home asks, and holds nothing.**  Its answer is about its
        # own section, which is empty — and this is the ask that used to empty
        # the set the first home's removal is computed against.
        nothing = await asked(hub, theirs, "cli:thing")
        # Then the operator takes the entry out of the first home's section.
        plugin.per_home[str(mine)] = {"sources": []}
        gone = await asked(hub, mine, "cli:thing")

    assert "cli:thing" in held, "the entry was in this home's catalogue"
    assert "cli:thing" not in nothing, "the other home never held it"
    assert "cli:thing" not in gone, "a source it stopped holding kept its rows"


@pytest.mark.asyncio
async def test_a_fault_in_one_homes_catalogue_is_not_anothers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The fault belongs to the file it was written to.**

    `_write` is called for one home's file — including the fan-out writes
    `remember` makes into every open store — and it kept the reason keyed by
    source alone, so a catalogue that would not take a listing in one home
    failed `list_tools` in *all* of them, with a reason naming a source and no
    home: a turn reading a file that was working, refused with "the tool
    catalogue is not working", which is the third failure rule pointed at the
    wrong party.
    """
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))
    config_file(mine, MINE_CLI)
    config_file(theirs, THEIRS_CLI)
    base = default_config()
    with in_home(theirs):
        broken = catalogue_db()
    monkeypatch.setattr(ToolStore, "merge", a_catalogue_that_cannot_record_in(broken))

    hub = build_hub(
        base,
        source=load_cached,
        transports=plugin_transports(base, {}, None),
        embedder=StubEmbedder(),
    )
    async with Client(hub) as client:
        with in_home(theirs):
            with pytest.raises(ToolError, match="that home's catalogue file is locked"):
                await client.call_tool("list_tools", {}, meta=client_meta("jack"))
        with in_home(mine):
            listed = await client.call_tool("list_tools", {}, meta=client_meta("jack"))

    assert "skill_use" in names(listed.data), "this home's catalogue is working"


@pytest.mark.asyncio
async def test_a_verdict_is_written_into_every_homes_catalogue(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A source the hub reaches itself has its rows in every home.**

    `remember` writes a plugin's listing into every home's file, because one set
    of links serves them all — so the *verdict* on that source has to travel the
    same way.  Written into the ambient home alone, the other ones go on reading
    that tool as `enabled` (and a search goes on offering it) while the server
    behind it is down, for the life of the process: the rows and the verdict are
    two halves of one fact about one source.

    Driven at the catalogue, which is where the fan-out lives: two homes' files
    with the same source in them, and one verdict.
    """
    from slife2.plugins.toolhub.server import Catalogue

    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))

    async def open_store() -> ToolStore:
        return ToolStore(catalogue_db(), threshold=100)

    async def embedder() -> Any:
        return StubEmbedder()

    catalogue = Catalogue(open_store, embedder)
    rows = [{"name": "serper__search", "description": "Search the web."}]
    for home in (mine, theirs):
        with in_home(home):
            await catalogue.store()
            await catalogue.merge("serper", "mcp", rows)
    with in_home(mine):
        await catalogue.source_state("serper", "error")

    def status(home: Path) -> str:
        with in_home(home):
            path = catalogue_db()
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT status FROM tool WHERE source_id = ?", ("serper",)
            ).fetchone()
        return str(row[0])

    assert status(mine) == "error"
    assert status(theirs) == "error", "the other home still reads that tool as up"
