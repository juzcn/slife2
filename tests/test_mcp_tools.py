"""`slife2-mcp-tools`' own five tools: the model editing `tools:`.

The family used to hold a section and declare it; now it can be *changed*, by
the model, through the `mcp_*` names v1 used.  What these tests are about is the
three things that make that safe rather than clever: what is written is the file
the operator reads, what is refused is refused before anything is written, and
what changed takes effect here and now — with the connection rebuilt, not a flag
flipped on an object that says one thing and does another.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from slife2.config import Config, load
from slife2.configfile import read_section
from slife2.mcp_tools import build_server
from slife2.toolhub import build_server as build_hub
from tests.fakes import StubEmbedder, answering, plugin_transports

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
tools:
  existing:
    command: in-memory
    env:
      SERPER_API_KEY: ${MCP_TEST_KEY}
    description: Already configured.
"""


def write_config(isolated_runtime: Path, text: str = CONFIG) -> Path:
    isolated_runtime.mkdir(parents=True, exist_ok=True)
    path = isolated_runtime / "slife2.yaml"
    path.write_text(text, encoding="utf-8")
    return path


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


async def ask(client: Client, tool: str, **arguments: Any) -> str:
    """What one of these tools said, as the model reads it.

    The text blocks and not `result.data`: every one of these tools answers with
    a sentence, and that is the surface under test — what a model is told, not
    what a caller could parse out of it.
    """
    result = await client.call_tool(tool, arguments)
    return "".join(getattr(block, "text", "") for block in result.content or [])


def written(isolated_runtime: Path) -> str:
    return (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")


def section(isolated_runtime: Path) -> dict[str, Any]:
    """The `tools:` section as the file has it."""
    return read_section("tools", path=isolated_runtime / "slife2.yaml")


# --- what the listing shows ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_listing_shows_the_file_and_not_the_credential(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**`${VAR}` comes back as `${VAR}`, and this is why the listing reads the file.**

    What this process *holds* is the resolved settings, so a listing built from
    them would print the operator's live API key into the conversation and into
    the turn log.  The file is also simply the better answer: it is what a person
    reads and edits, and it is the thing these tools change.
    """
    monkeypatch.setenv("MCP_TEST_KEY", "sk-live-do-not-print-me")

    async with Client(family(isolated_runtime)) as client:
        listed = await ask(client, "mcp_list")

    assert "existing" in listed
    assert "${MCP_TEST_KEY}" in listed, "the reference, as written"
    assert "sk-live-do-not-print-me" not in listed


@pytest.mark.asyncio
async def test_the_listing_says_which_servers_are_working(isolated_runtime) -> None:
    """The answer to "what do I have", and each entry's link in one word."""
    async with Client(family(isolated_runtime)) as client:
        listed = await ask(client, "mcp_list")

    assert "existing [ready]" in listed
    assert "stdio in-memory" in listed
    assert "Already configured." in listed


# --- setting one --------------------------------------------------------------


@pytest.mark.asyncio
async def test_setting_a_server_writes_it_and_connects_it(isolated_runtime) -> None:
    """**The whole of what an upsert does**, in one call: the file, and the link.

    v1 could report a tool count because its gateway held the connection; so can
    this, and for the same reason — the process that writes the entry is the
    process that holds it.
    """
    async with Client(family(isolated_runtime)) as client:
        answer = await ask(
            client, "mcp_set", name="added", command="in-memory", description="Added."
        )
        listed = await ask(client, "mcp_list")
        tools = await ask(client, "mcp_list_tools", name="added")

    assert "connected" in answer
    assert "2 tool(s)" in answer, answer
    assert section(isolated_runtime)["added"]["command"] == "in-memory"
    assert "added [ready]" in listed
    assert "echo" in tools and "second" in tools


@pytest.mark.asyncio
async def test_setting_it_again_does_not_rebuild_the_link(isolated_runtime) -> None:
    """An upsert a caller may repeat: identical settings, no teardown.

    What it costs to get this wrong is a fresh `npx` start and a new handshake
    for a call that changed nothing — v1's bug was comparing the argument
    (`${VAR}`) against the resolved pool (the key), so identical calls never
    matched; both sides here go through the config layer and are both resolved.
    """
    async with Client(family(isolated_runtime)) as client:
        await ask(client, "mcp_set", name="added", command="in-memory")
        first = await ask(client, "mcp_list_tools", name="added")
        await ask(client, "mcp_set", name="added", command="in-memory")
        again = await ask(client, "mcp_list_tools", name="added")

    assert "2 tool(s)" in first and "2 tool(s)" in again
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
    assert "connected" in answer
    assert "added [ready]" in listed


@pytest.mark.asyncio
async def test_an_entry_the_config_refuses_is_refused_before_anything_is_written(
    isolated_runtime,
) -> None:
    """The parser the loader uses, used here — a restart later is too late.

    An entry naming no transport is one `slife2.config` refuses at every start,
    so writing it would be an edit that reports success and takes the next start
    down; one naming *both* is the same shape, from the other side.
    """
    write_config(isolated_runtime)
    before = written(isolated_runtime)

    async with Client(family(isolated_runtime)) as client:
        neither = await ask(client, "mcp_set", name="nowhere", description="d")
        both = await ask(
            client, "mcp_set", name="nowhere", command="npx", url="https://x.test/mcp"
        )

    assert neither.startswith("[refused]") and "needs `command`" in neither
    assert both.startswith("[refused]") and "both `command` and `url`" in both
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
        answer = await ask(client, "mcp_set", name="agent", command="in-memory")

    assert answer.startswith("[refused]") and "slife2 itself runs" in answer
    assert "agent" not in section(isolated_runtime)


# --- removing and switching ---------------------------------------------------


@pytest.mark.asyncio
async def test_removing_a_server_takes_its_entry_and_stops_it(isolated_runtime) -> None:
    async with Client(family(isolated_runtime)) as client:
        await ask(client, "mcp_set", name="added", command="in-memory")
        answer = await ask(client, "mcp_remove", name="added")
        listed = await ask(client, "mcp_list")
        gone = await ask(client, "mcp_list_tools", name="added")

    assert "gone from `tools:`" in answer
    assert "added" not in section(isolated_runtime)
    assert "added" not in listed
    assert "not a server this process holds" in gone


@pytest.mark.asyncio
async def test_removing_something_that_is_not_there_says_so(isolated_runtime) -> None:
    """Nothing is uninstalled and nothing is written; the answer is the truth."""
    write_config(isolated_runtime)
    before = written(isolated_runtime)

    async with Client(family(isolated_runtime)) as client:
        answer = await ask(client, "mcp_remove", name="never-existed")

    assert "not a server under `tools:`" in answer
    assert written(isolated_runtime) == before


@pytest.mark.asyncio
async def test_switching_one_off_keeps_the_entry_and_stops_the_server(
    isolated_runtime,
) -> None:
    """Off is a decision, not a deletion — the file's own vocabulary.

    The entry stays, the link goes, and the listing says `off` rather than
    `failed`: a server nobody asked to connect has not failed at anything.
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

    assert "switched off" in off
    assert written_off["enabled"] is False
    assert "existing [off]" in listed
    assert "no tool list to show" in tools
    assert "connected" in on
    assert "existing [ready]" in back
    assert "enabled" not in section(isolated_runtime)["existing"], "on is the default"


@pytest.mark.asyncio
async def test_the_cap_says_how_much_is_behind_it(isolated_runtime) -> None:
    """A listing is capped for the caller's context, and a cap that is not
    announced reads as the whole list."""
    async with Client(family(isolated_runtime)) as client:
        tools = await ask(client, "mcp_list_tools", name="existing", limit=1)

    assert "2 tool(s), showing 1" in tools
    assert "1 more" in tools


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
