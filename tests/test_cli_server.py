"""slife2-cli: the `cli:` section — the rows a search reads, and the tools that write it.

The rows half is the smallest plugin in the system and the shape that is easiest
to get wrong, so what is tested there is mostly what it *refuses* to be: not a
runner of anything, and not a filter over the config — because a switched-off
entry that vanished would make "the operator turned it off" and "nobody ever
wrote it down" the same silence.

The tools half is the four `cli_*` tools v1 had, and what makes this family's
version of them unlike the other three: **there is nothing to connect to.**  So
none of them rebuild a link or report a tool count; what they change is the
section, and what follows is the row — which is also why the one check worth
making is whether the program is actually on this machine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from slife2.cli_server import CONFIG_KEY, SERVER_NAME, SOURCE, build_server, rows
from slife2.config import default_config, load
from slife2.configfile import read_section
from slife2.mcp_server import LIST_SOURCES

pytestmark = pytest.mark.unit

CLI = """
cli:
  yt-dlp:
    command: yt-dlp
    description: Download a video.
    install: uv pip install yt-dlp
  iflow:
    command: iflow
    description: A switched-off one.
    enabled: false
"""


def config_for(data_dir: Path):
    """The config where slife2 looks for it, and loaded the way a start loads it.

    **In the data directory and not in `tmp_path`**, because two of these tools
    write the file: `slife2.configfile` resolves it through the same
    `find_config_path` every process uses, so a config somewhere else is one the
    loader can read by path and the writer cannot see at all.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "slife2.yaml"
    path.write_text(CLI, encoding="utf-8")
    return load()


async def ask(client: Client, tool: str, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    return "".join(getattr(block, "text", "") for block in result.content or [])


def section(data_dir: Path) -> dict[str, Any]:
    return read_section("cli", path=data_dir / "slife2.yaml")


def test_the_server_is_named_where_a_client_can_find_it() -> None:
    assert SERVER_NAME == "slife2-cli"


def test_the_source_is_not_the_servers_own_name() -> None:
    """Filing under the server's own key would write the runtime's verdict on
    these rows: a source's state is written across everything it owns, and
    whether a *command* can be reached is not something a connection knows."""
    assert SOURCE == "cli"
    assert SOURCE != CONFIG_KEY


def test_an_entry_is_a_row_a_search_can_find(isolated_runtime: Path) -> None:
    """`remote_name` is the command and `schema` is everything else the entry
    knows, because that is what a search matches on when the model asks for a
    job rather than a name."""
    rows_by_name = {row["name"]: row for row in rows(config_for(isolated_runtime).cli)}

    assert set(rows_by_name) == {"cli:yt-dlp", "cli:iflow"}
    row = rows_by_name["cli:yt-dlp"]
    assert row["description"] == "Download a video."
    assert row["remote_name"] == "yt-dlp"
    assert "yt-dlp" in str(row["schema"])
    assert "uv pip install yt-dlp" in str(row["schema"]), (
        "how to get it is searched too"
    )
    assert row["status"] == "enabled"


def test_a_switched_off_entry_is_still_a_row(isolated_runtime: Path) -> None:
    """Told it exists and is off beats not being told — and the verdict is the
    row's own, which is the one thing a merge honours from a source rather than
    from the runtime, because there is no connection to have a state."""
    row = {one["name"]: one for one in rows(config_for(isolated_runtime).cli)}[
        "cli:iflow"
    ]

    assert row["status"] == "disabled"


def test_no_config_is_no_rows(isolated_runtime: Path) -> None:
    assert rows(default_config().cli) == []


@pytest.mark.asyncio
async def test_the_model_gets_the_tools_that_edit_the_section(
    isolated_runtime: Path,
) -> None:
    """**What this plugin serves now, and which half the model is given.**

    It was the one plugin with nothing for the model — its entries are not tools
    and nothing runs one — and what changed is not that its rows became callable
    but that the section became writable.  So the four `cli_*` tools are marked
    and `list_sources` is not: that one is how the hub asks, and a model given it
    could read a catalogue overview it has `tool_search` for.

    This is the server's own tool list, which is not the model's — the mark is
    what decides *that* list, and the hub's is tested in
    `tests/test_mcp_tools.py`.
    """
    async with Client(build_server(config_for(isolated_runtime))) as client:
        listed = {tool.name for tool in await client.list_tools()}

    assert listed == {
        "cli_list",
        "cli_set",
        "cli_remove",
        "cli_set_enabled",
        LIST_SOURCES,
    }


@pytest.mark.asyncio
async def test_the_declaring_plugin_answers_the_whole_section(
    isolated_runtime: Path,
) -> None:
    """The whole list rather than a difference: that is what makes a deleted
    entry stop being a hit, and what lets the hub be the only writer."""
    async with Client(build_server(config_for(isolated_runtime))) as client:
        result = await client.call_tool(LIST_SOURCES, {})

    (source,) = result.data["sources"]
    assert source["name"] == SOURCE
    assert source["category"] == SOURCE
    assert source["enabled"] and source["up"]
    assert source["transport"] == "", "a command has no connection to be reached by"
    assert {row["name"] for row in source["rows"]} == {"cli:yt-dlp", "cli:iflow"}


# --- the tools that write the section ----------------------------------------


@pytest.mark.asyncio
async def test_recording_a_command_writes_it_and_declares_it_at_once(
    isolated_runtime: Path,
) -> None:
    """**No connection, so "now" means the row.**

    The other two families answer an edit with a link they just rebuilt and a
    tool count they just read.  A command has neither, and the honest answer is
    the one thing that did change: the entry is written, and the source declares
    it from this moment — which is what the hub reads before every search.
    """
    async with Client(build_server(config_for(isolated_runtime))) as client:
        answer = await ask(
            client,
            "cli_set",
            name="gh",
            command="gh",
            description="GitHub from the command line.",
            install="winget install GitHub.cli",
        )
        declared = await client.call_tool(LIST_SOURCES, {})

    assert "recorded as `gh`" in answer
    assert section(isolated_runtime)["gh"]["command"] == "gh"
    names = {row["name"] for row in declared.data["sources"][0]["rows"]}
    assert "cli:gh" in names, "declared without waiting for a restart"


@pytest.mark.asyncio
async def test_a_command_that_is_not_installed_says_so(isolated_runtime: Path) -> None:
    """**The one failure this family is certain to meet, said while somebody is looking.**

    A model writing a command down from a documentation page has no way to know
    whether it is on this machine, and v1 only answers after the fact — `install`
    is shown when the command turns out to be missing.  The entry is still
    written: it is a fact about what the operator wants recorded, and the note is
    about the machine.
    """
    async with Client(build_server(config_for(isolated_runtime))) as client:
        nowhere = await ask(
            client,
            "cli_set",
            name="not-a-real-program",
            command="definitely-not-installed-anywhere-9f3a",
            description="d",
        )

    assert "not on PATH" in nowhere
    assert "definitely-not-installed-anywhere-9f3a" in nowhere
    assert section(isolated_runtime)["not-a-real-program"]["command"] == (
        "definitely-not-installed-anywhere-9f3a"
    )


@pytest.mark.asyncio
async def test_an_entry_with_no_command_is_refused_before_anything_is_written(
    isolated_runtime: Path,
) -> None:
    """`command` is required loudly at load, and the same parser says so here.

    An entry naming no program is not a disabled entry — it is one that can never
    work — and the refusal has to come before the write, or the next start is
    what finds out.
    """
    config = config_for(isolated_runtime)
    before = (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")

    async with Client(build_server(config)) as client:
        answer = await ask(
            client, "cli_set", name="nothing", command="", description="d"
        )

    assert answer.startswith("[refused]") and "needs `command`" in answer
    assert (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_the_switch_takes_the_row_out_without_taking_the_entry(
    isolated_runtime: Path,
) -> None:
    """Off is a row that still exists and says so — never a row that is gone.

    That distinction is the whole reason `rows` is not a filter over the config,
    and a tool that *deleted* the row would undo it from the other end.
    """
    async with Client(build_server(config_for(isolated_runtime))) as client:
        off = await ask(client, "cli_set_enabled", name="yt-dlp", enabled=False)
        declared = await client.call_tool(LIST_SOURCES, {})
        # Read the file while it is off: switching it back on removes the key,
        # so this is the only moment the written `enabled: false` exists.
        written_off = section(isolated_runtime)["yt-dlp"]
        on = await ask(client, "cli_set_enabled", name="yt-dlp", enabled=True)

    rows_by_name = {row["name"]: row for row in declared.data["sources"][0]["rows"]}
    assert "off" in off
    assert written_off["enabled"] is False
    assert rows_by_name["cli:yt-dlp"]["status"] == "disabled"
    assert "on" in on
    assert "enabled" not in section(isolated_runtime)["yt-dlp"], "on is the default"


@pytest.mark.asyncio
async def test_forgetting_a_command_takes_the_row_and_leaves_the_program(
    isolated_runtime: Path,
) -> None:
    """It forgets; it does not uninstall.  The answer has to say which."""
    async with Client(build_server(config_for(isolated_runtime))) as client:
        answer = await ask(client, "cli_remove", name="yt-dlp")
        declared = await client.call_tool(LIST_SOURCES, {})
        missing = await ask(client, "cli_remove", name="yt-dlp")

    names = {row["name"] for row in declared.data["sources"][0]["rows"]}
    assert "forgotten" in answer
    assert "yt-dlp" not in section(isolated_runtime)
    assert "cli:yt-dlp" not in names
    assert "is not recorded" in missing


@pytest.mark.asyncio
async def test_the_listing_shows_the_entry_and_its_switch(
    isolated_runtime: Path,
) -> None:
    """`on` / `off` and not `ready` / `failed`: those are words about a link, and
    a command has none — there is nothing that could be unreachable."""
    async with Client(build_server(config_for(isolated_runtime))) as client:
        listed = await ask(client, "cli_list")

    assert "yt-dlp [on]" in listed
    assert "iflow [off]" in listed
    assert "install  uv pip install yt-dlp" in listed
    assert "Download a video." in listed
