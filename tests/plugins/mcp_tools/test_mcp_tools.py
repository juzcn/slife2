"""`slife2-mcp-tools`' own five tools: the model editing `mcp:`.

The family used to hold a section and declare it; now it can be *changed*, by
the model, through the `mcp_*` names.  What these tests are about is the
three things that make that safe rather than clever: what is written is the file
the operator reads, what is refused is refused before anything is written, and
what changed takes effect here and now — with the connection rebuilt, not a flag
flipped on an object that says one thing and does another.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from slife2.libs.host.config import Config, load, load_cached
from slife2.libs.host.configfile import read_section
from slife2.libs.host.paths import in_home
from slife2.libs.plugin.audience import client_meta
from slife2.plugins.mcp_tools.server import build_server
from slife2.plugins.toolhub.server import build_server as build_hub
from tests.fakes import StubEmbedder, answering, config_file, plugin_transports

pytestmark = pytest.mark.unit

#: A working config with one server already in it.  Written to disk because the
#: tools edit the file the loader reads — the point of the whole arrangement —
#: and read back with `load()`, so a test drives the same path a start does.
CONFIG = """\
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    api_key: ${SLIFE2_TEST_KEY:-none}
    models:
      - model: big
        context_window: 100000
        max_tokens: 4000
default: local/big

# The servers are here, and this comment is the file's; an edit nearby must not
# eat it.
mcp:
  existing:
    command: in-memory
    env:
      SERPER_API_KEY: ${MCP_TEST_KEY}
    description: Already configured.
"""


def write_config(data_dir: Path, text: str = CONFIG) -> Path:
    """This module's config, written where the loader looks for it."""
    return config_file(data_dir, text)


def upstream() -> FastMCP:
    """Somebody else's MCP server, with a countable number of tools."""
    server = FastMCP("upstream")

    @server.tool
    def echo(text: str) -> str:
        """Echo it back."""
        return text

    @server.tool
    def second() -> str:
        """Another one, so a cap has something to cut."""
        return "two"

    return server


def family(isolated_runtime: Path, factory: Any = None) -> FastMCP:
    """The `mcp-tools` server over the config on disk."""
    write_config(isolated_runtime)
    return build_server(load(), client_factory=factory or answering(upstream()))


async def ask(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    """What one of these tools answered, as the model reads it.

    The structured payload and not the text blocks: every one of these tools
    answers with a dict — JSON is what a model reads, and it is the surface
    under test — so parsing the text block back would be testing the renderer.
    """
    result = await client.call_tool(tool, arguments)
    assert isinstance(result.data, dict), result
    return result.data


def written(isolated_runtime: Path) -> str:
    return (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")


def section(isolated_runtime: Path) -> dict[str, Any]:
    """The `mcp:` section as the file has it."""
    return read_section("mcp", path=isolated_runtime / "slife2.yaml")


# --- what the listing shows ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_listing_shows_the_file_and_not_the_credential(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**`${VAR}` comes back as `${VAR}`, and this is why the listing reads the file.**

    What this process *holds* is the resolved settings, so a row built from them
    would put the operator's live API key into the conversation and into the turn
    log.  The file is also simply the better answer: it is what a person reads
    and edits, and it is the thing these tools change.
    """
    monkeypatch.setenv("MCP_TEST_KEY", "sk-live-do-not-print-me")

    async with Client(family(isolated_runtime)) as client:
        listed = await ask(client, "mcp_list")

    (row,) = listed["servers"]
    assert row["name"] == "existing"
    assert row["env"] == {"SERPER_API_KEY": "${MCP_TEST_KEY}"}, (
        "the reference, as written"
    )
    assert "sk-live-do-not-print-me" not in json.dumps(listed)


@pytest.mark.asyncio
async def test_the_listing_says_which_servers_are_working(isolated_runtime) -> None:
    """The answer to "what do I have": one row per entry, its link in one word.

    **One page shape for four listings**, `mcp_list` among them: the rows under
    the plural noun, and `total`/`limit`/`offset` beside them — `total` is the
    whole count and not the page's, so a cap says what it left behind.
    """
    async with Client(family(isolated_runtime)) as client:
        listed = await ask(client, "mcp_list")

    (row,) = listed["servers"]
    assert row["state"] == "ready"
    assert row["transport"] == "stdio"
    assert row["command"] == "in-memory"
    assert row["description"] == "Already configured."
    assert (listed["total"], listed["limit"], listed["offset"]) == (1, 50, 0)


@pytest.mark.asyncio
async def test_the_listing_pages_and_says_where_it_stopped(isolated_runtime) -> None:
    """**`offset + len(rows) < total` is the whole of how a model pages.**

    A cap that is applied without being announced reads as the whole section, so
    the numbers answering it back are as much of the answer as the rows are.
    """
    write_config(isolated_runtime)
    async with Client(family(isolated_runtime)) as client:
        await ask(client, "mcp_set", name="second", command="in-memory")
        first = await ask(client, "mcp_list", limit=1)
        rest = await ask(client, "mcp_list", limit=1, offset=1)

    assert first["total"] == 2 and len(first["servers"]) == 1
    assert (first["limit"], first["offset"]) == (1, 0)
    assert rest["offset"] == 1
    assert first["servers"][0]["name"] != rest["servers"][0]["name"], "the page moved"


# --- setting one --------------------------------------------------------------


@pytest.mark.asyncio
async def test_setting_a_server_writes_it_and_connects_it(isolated_runtime) -> None:
    """**The whole of what an upsert does**, in one call: the file, and the link.

    A tool count is reportable because the gateway holds the connection — the
    process that writes the entry is the process that holds it.
    """
    async with Client(family(isolated_runtime)) as client:
        answer = await ask(
            client, "mcp_set", name="added", command="in-memory", description="Added."
        )
        listed = await ask(client, "mcp_list")
        tools = await ask(client, "mcp_list_tools", name="added")

    assert answer["state"] == "ready", "the word the hub answers `servers` with"
    assert answer["tools"] == 2, answer
    assert section(isolated_runtime)["added"]["command"] == "in-memory"
    assert "added" in {row["name"] for row in listed["servers"]}
    assert {row["name"] for row in listed["servers"] if row["state"] == "ready"} == {
        "added",
        "existing",
    }
    assert {one["name"] for one in tools["tools"]} == {"added__echo", "added__second"}
    assert tools["total"] == 2


@pytest.mark.asyncio
async def test_setting_it_again_does_not_rebuild_the_link(isolated_runtime) -> None:
    """An upsert a caller may repeat: identical settings, no teardown.

    What it costs to get this wrong is a fresh `npx` start and a new handshake
    for a call that changed nothing — comparing the argument (`${VAR}`) against
    the resolved pool (the key) would mean identical calls never matched; both
    sides here go through the config layer and are both resolved.
    """
    async with Client(family(isolated_runtime)) as client:
        await ask(client, "mcp_set", name="added", command="in-memory")
        first = await ask(client, "mcp_list_tools", name="added")
        await ask(client, "mcp_set", name="added", command="in-memory")
        again = await ask(client, "mcp_list_tools", name="added")

    assert first["total"] == 2 and again["total"] == 2
    assert written(isolated_runtime).count("  added:") == 1, "one entry, not two"


@pytest.mark.asyncio
async def test_setting_it_again_replaces_the_entry_rather_than_patching_it(
    isolated_runtime,
) -> None:
    """**A `set` is the whole entry, so a transport can be switched.**

    An entry first written as a URL and then written again as a command would
    otherwise carry both, and the loader refuses an entry that names two
    transports — so the second `set` could never land.  This is what "replace,
    not merge" buys, and it is the reason the tool says so in its own words.
    """
    async with Client(family(isolated_runtime)) as client:
        await ask(
            client,
            "mcp_set",
            name="added",
            url="https://x.test/mcp",
            description="What it used to be.",
        )
        answer = await ask(client, "mcp_set", name="added", command="in-memory")
        listed = await ask(client, "mcp_list")

    entry = section(isolated_runtime)["added"]
    assert entry["command"] == "in-memory"
    assert "url" not in entry, "the transport it used to be is gone"
    assert "description" not in entry, "and a field this call did not name"
    assert answer["state"] == "ready"
    assert "added" in {row["name"] for row in listed["servers"]}


@pytest.mark.asyncio
async def test_an_entry_the_config_refuses_is_refused_before_anything_is_written(
    isolated_runtime,
) -> None:
    """The parser the loader uses, used here — a restart later is too late.

    An entry naming no transport is one `slife2.libs.host.config` refuses at every start,
    so writing it would be an edit that reports success and takes the next start
    down; one naming *both* is the same shape, from the other side.

    **Raised, and not returned as a sentence.**  A refusal answered as ordinary
    text arrives with `ok` true and reads as an edit that happened — and nothing
    was written, which is the one fact the caller has to be able to see.
    """
    write_config(isolated_runtime)
    before = written(isolated_runtime)

    async with Client(family(isolated_runtime)) as client:
        with pytest.raises(ToolError) as neither:
            await client.call_tool("mcp_set", {"name": "nowhere", "description": "d"})
        with pytest.raises(ToolError) as both:
            await client.call_tool(
                "mcp_set",
                {"name": "nowhere", "command": "npx", "url": "https://x.test/mcp"},
            )

    assert "needs `command`" in str(neither.value)
    assert "both `command` and `url`" in str(both.value)
    assert not str(neither.value).startswith("[refused]"), "the status is the call's"
    assert written(isolated_runtime) == before, "nothing was written"


@pytest.mark.asyncio
async def test_a_name_the_hub_is_already_using_is_refused(isolated_runtime) -> None:
    """**The collision that would otherwise be silent.**

    `agent` is a server slife2 itself runs, so the hub is already connected under
    that name — it drops a declaration that takes one, with a log line nobody
    reads.  The entry would be written, reported as added, and have no tools,
    and the model would have no way to find that out.
    """
    async with Client(family(isolated_runtime)) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("mcp_set", {"name": "agent", "command": "in-memory"})

    assert "slife2 itself runs" in str(raised.value)
    assert "agent" not in section(isolated_runtime)


# --- removing and switching ---------------------------------------------------


@pytest.mark.asyncio
async def test_removing_a_server_takes_its_entry_and_stops_it(isolated_runtime) -> None:
    """**`removed` and `stopped` are the two facts the removal carries.**

    The entry leaves the file and the link stops, and a caller can tell which of
    the two happened — the one thing a single sentence could not say when only
    one of them had (see the entry-not-in-the-file case below).
    """
    async with Client(family(isolated_runtime)) as client:
        await ask(client, "mcp_set", name="added", command="in-memory")
        answer = await ask(client, "mcp_remove", name="added")
        listed = await ask(client, "mcp_list")
        with pytest.raises(ToolError) as gone:
            await client.call_tool("mcp_list_tools", {"name": "added"})

    assert answer == {"name": "added", "removed": True, "stopped": True}
    assert "added" not in section(isolated_runtime)
    assert "added" not in {row["name"] for row in listed["servers"]}
    assert "not a server this process holds" in str(gone.value)


@pytest.mark.asyncio
async def test_removing_something_that_is_not_there_says_so(isolated_runtime) -> None:
    """Nothing is uninstalled and nothing is written, so the call is a failure."""
    write_config(isolated_runtime)
    before = written(isolated_runtime)

    async with Client(family(isolated_runtime)) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("mcp_remove", {"name": "never-existed"})

    assert "not a server under `mcp:`" in str(raised.value)
    assert written(isolated_runtime) == before


@pytest.mark.asyncio
async def test_switching_one_off_keeps_the_entry_and_stops_the_server(
    isolated_runtime,
) -> None:
    """Off is a decision, not a deletion — the file's own vocabulary.

    The entry stays, the link goes, and every answer says `off` rather than
    `failed`: a server nobody asked to connect has not failed at anything.

    **And `mcp_list_tools` says the same thing**, which is what the assertions
    below it are for.  A switched-off entry has no link, so it has no error
    either, and the tool's fallback read those two as "it is not answering yet"
    — one entry with three accounts: `[off]` in the listing, `off` in its own
    state, and a sentence telling the model to wait for a connection
    `enabled: false` promises will never be opened.  There is one word for it
    now, and it is the listing's own: `state`.
    """
    async with Client(family(isolated_runtime)) as client:
        off = await ask(client, "mcp_set_enabled", name="existing", enabled=False)
        listed = await ask(client, "mcp_list")
        tools = await ask(client, "mcp_list_tools", name="existing")
        # Read the file while it is off: turning it back on removes the flag, so
        # this is the only moment the written `enabled: false` exists.
        written_off = section(isolated_runtime)["existing"]
        on = await ask(client, "mcp_set_enabled", name="existing", enabled=True)
        back = await ask(client, "mcp_list")

    assert off["state"] == "off" and off["enabled"] is False
    assert written_off["enabled"] is False
    assert [row["state"] for row in listed["servers"]] == ["off"]
    assert tools["state"] == "off", "the word the listing's own row carries"
    assert tools["tools"] == [] and tools["total"] == 0
    assert "error" not in tools, "there is no link to be waiting for"
    assert on["state"] == "ready" and on["tools"] == 2
    assert [row["state"] for row in back["servers"]] == ["ready"]
    assert "enabled" not in section(isolated_runtime)["existing"], "on is the default"


@pytest.mark.asyncio
async def test_the_cap_says_how_much_is_behind_it(isolated_runtime) -> None:
    """A listing is capped for the caller's context, and a cap that is not
    announced reads as the whole list."""
    async with Client(family(isolated_runtime)) as client:
        tools = await ask(client, "mcp_list_tools", name="existing", limit=1)

    assert tools["total"] == 2, "the whole count, not the page's"
    assert tools["limit"] == 1 and tools["offset"] == 0
    assert len(tools["tools"]) == 1


# --- and it reaches the model -------------------------------------------------


@pytest.mark.asyncio
async def test_the_management_tools_are_offered_to_the_model(isolated_runtime) -> None:
    """**What `FOR_THE_MODEL` buys, and the consequence of wanting it.**

    These are the first tools this plugin offers the model — until now it only
    held other people's — so they arrive through the hub like a plugin's tools
    do, by their own bare names.  The listing and the call pair stay invisible:
    the model gets the tools of the *entries*, not the plugin's own API.

    **Asked through the hub's own `list_tools`, and not `Client(hub).list_tools()`**:
    the hub serves three tools of its own and the model's list is the *payload*
    of one of them.  The first is what a peer can call; the second is what a
    model is given, and only the second is this test's subject.
    """
    write_config(isolated_runtime)
    config: Config = load()
    # A plugin's tools are on demand now, so this test says which of them it
    # wants in front of the model.  The required set stays in: the loader
    # refuses a list without it.
    config = replace(
        config,
        tool_load=replace(
            config.tool_load,
            autoload=(
                *config.tool_load.autoload,
                "mcp_list",
                "mcp_list_tools",
                "mcp_set",
                "mcp_remove",
                "mcp_set_enabled",
            ),
        ),
    )
    hub = build_hub(
        config,
        transports=plugin_transports(config, client_factory=answering(upstream())),
        embedder=StubEmbedder(),
    )

    async with Client(hub) as client:
        result = await client.call_tool("list_tools", {})
        listed = {one["name"] for one in result.data["tools"]}

    assert {"mcp_list", "mcp_set", "mcp_remove", "mcp_set_enabled"} <= listed
    assert "list_sources" not in listed and "call_source" not in listed


@pytest.mark.asyncio
async def test_two_homes_naming_one_entry_run_one_server(
    isolated_runtime: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """**The name is what makes it the same MCP server.**

    An `mcp:` entry is a process, and a machine is where processes live — so two
    data directories that both configure `existing` are two homes naming one
    server rather than two servers.  What that cost without the rule is not
    hypothetical: `slife2 --agent jack --data-dir D` over a *copy* of a config has
    the same eighteen `mcp:` entries and the same two `rest-api:` ones, and every
    one of them was started a second time — twenty `npx`/`uvx` processes for the
    twenty servers the first home was already running.

    The transport count is the assertion, because the transport is what spawns
    the process: one factory call for two homes is one process.  The two homes'
    *declarations* are asserted as well, and they are the other half — the rows
    belong to the server, so both read one listing, while the switch and the
    description stay each home's own.
    """
    mine, theirs = isolated_runtime, isolated_runtime.parent / "theirs"
    write_config(mine, CONFIG)
    write_config(theirs, CONFIG)

    started: list[Any] = []

    def factory(transport: Any, handler: Any) -> Any:
        started.append(transport)
        return Client(upstream())

    server = build_server(load(), source=load_cached, client_factory=factory)

    async with Client(server) as client:
        with in_home(mine):
            here = await client.call_tool(
                "list_sources", {}, meta=client_meta("jack", home=str(mine))
            )
        with in_home(theirs):
            there = await client.call_tool(
                "list_sources", {}, meta=client_meta("jack", home=str(theirs))
            )

    assert len(started) == 1, f"one entry, and {len(started)} processes for it"
    held = [one for one in here.data["sources"] if one["name"] == "existing"]
    same = [one for one in there.data["sources"] if one["name"] == "existing"]
    assert held and same, "both homes hold the entry"
    assert held[0]["up"] and same[0]["up"], "and the one server answers for both"
    assert held[0]["rows"] == same[0]["rows"], (
        "the listing is the server's, not a home's"
    )
    assert not [
        record
        for record in caplog.records
        if "configured differently" in record.message
    ], "the two homes agree, so there is nothing to report"


@pytest.mark.asyncio
async def test_a_second_home_naming_one_entry_differently_is_told(
    isolated_runtime: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Two homes can disagree about what a name runs, and that is *said*.

    The name wins, so the second home gets the running server rather than a
    second process — and the warning is the whole of what keeps that from being
    silent: a reader looking at the second `slife2.yaml` would otherwise have no
    way to learn that its `command` is not the one in use.  Both homes are named,
    because the first one's directory is where the entry that *is* running lives.
    """
    mine, theirs = isolated_runtime, isolated_runtime.parent / "theirs"
    write_config(mine, CONFIG)
    write_config(
        theirs, CONFIG.replace("command: in-memory", "command: something-else")
    )

    started: list[Any] = []

    def factory(transport: Any, handler: Any) -> Any:
        started.append(transport)
        return Client(upstream())

    server = build_server(load(), source=load_cached, client_factory=factory)

    with caplog.at_level("WARNING"):
        async with Client(server) as client:
            with in_home(mine):
                await client.call_tool(
                    "list_sources", {}, meta=client_meta("jack", home=str(mine))
                )
            with in_home(theirs):
                await client.call_tool(
                    "list_sources", {}, meta=client_meta("jack", home=str(theirs))
                )

    assert len(started) == 1, "the name wins, so no second process"
    said = [
        record.getMessage()
        for record in caplog.records
        if "configured differently" in record.getMessage()
    ]
    assert len(said) == 1, caplog.text
    assert str(theirs) in said[0] and str(mine) in said[0], said[0]


@pytest.mark.asyncio
async def test_a_listing_says_how_many_tools_it_held(
    isolated_runtime: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """**The one line that says a server answered, and how much it offered.**

    A log call whose arguments do not match its own format string raises *inside*
    the logging module: the line a reader wants goes missing, the count that makes
    it worth reading goes with it, and what is printed instead is a
    `--- Logging error ---` block on stderr.  Nothing else notices — the tools are
    held, the declaration is right, the catalogue is right — which is why this is
    pinned by reading the record back the way a reader would (`getMessage` renders
    the format string, so a mismatched argument raises here rather than passing).
    """
    server = family(isolated_runtime)

    with caplog.at_level("INFO", logger="slife2.libs.plugin.family"):
        async with Client(server) as client:
            await client.call_tool("list_sources", {})

    said = [
        record.getMessage()
        for record in caplog.records
        if "tool(s) held" in str(record.msg)
    ]
    assert said == ["existing: 2 tool(s) held"], caplog.text
