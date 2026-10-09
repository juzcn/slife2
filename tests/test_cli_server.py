"""slife2-cli: the `cli:` section, which serves rows and no tools at all.

This is the smallest plugin in the system and the one whose shape is easiest to
get wrong, so what is tested is mostly what it *refuses* to be: not a tool the
model can call, not a runner of anything, and not a filter over the config —
because a switched-off entry that vanished would make "the operator turned it
off" and "nobody ever wrote it down" the same silence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from slife2.cli_server import CONFIG_KEY, SERVER_NAME, SOURCE, build_server, rows
from slife2.config import default_config, load
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


def config_for(tmp_path: Path):
    path = tmp_path / "slife2.yaml"
    path.write_text(CLI, encoding="utf-8")
    return load(path)


def test_the_server_is_named_where_a_client_can_find_it() -> None:
    assert SERVER_NAME == "slife2-cli"


def test_the_source_is_not_the_servers_own_name() -> None:
    """Filing under the server's own key would write the runtime's verdict on
    these rows: a source's state is written across everything it owns, and
    whether a *command* can be reached is not something a connection knows."""
    assert SOURCE == "cli"
    assert SOURCE != CONFIG_KEY


def test_an_entry_is_a_row_a_search_can_find(tmp_path: Path) -> None:
    """`remote_name` is the command and `schema` is everything else the entry
    knows, because that is what a search matches on when the model asks for a
    job rather than a name."""
    rows_by_name = {row["name"]: row for row in rows(config_for(tmp_path))}

    assert set(rows_by_name) == {"cli:yt-dlp", "cli:iflow"}
    row = rows_by_name["cli:yt-dlp"]
    assert row["description"] == "Download a video."
    assert row["remote_name"] == "yt-dlp"
    assert "yt-dlp" in str(row["schema"])
    assert "uv pip install yt-dlp" in str(row["schema"]), (
        "how to get it is searched too"
    )
    assert row["status"] == "enabled"


def test_a_switched_off_entry_is_still_a_row(tmp_path: Path) -> None:
    """Told it exists and is off beats not being told — and the verdict is the
    row's own, which is the one thing a merge honours from a source rather than
    from the runtime, because there is no connection to have a state."""
    row = {one["name"]: one for one in rows(config_for(tmp_path))}["cli:iflow"]

    assert row["status"] == "disabled"


def test_no_config_is_no_rows(isolated_runtime: Path) -> None:
    assert rows(default_config()) == []


@pytest.mark.asyncio
async def test_it_offers_the_model_nothing(tmp_path: Path) -> None:
    """A plugin with no model-facing tools is the ordinary case — the db, the
    embedder and the agent server are all the same — and it is what makes the
    publisher's missing audience mark load-bearing rather than tidy."""
    async with Client(build_server(config_for(tmp_path))) as client:
        listed = [tool.name for tool in await client.list_tools()]

    assert listed == [LIST_SOURCES]


@pytest.mark.asyncio
async def test_the_publisher_answers_the_whole_section(tmp_path: Path) -> None:
    """The whole list rather than a difference: that is what makes a deleted
    entry stop being a hit, and what lets the hub be the only writer."""
    async with Client(build_server(config_for(tmp_path))) as client:
        result = await client.call_tool(LIST_SOURCES, {})

    (source,) = result.data["sources"]
    assert source["name"] == SOURCE
    assert source["category"] == SOURCE
    assert source["enabled"] and source["up"]
    assert source["transport"] == "", "a command has no connection to be reached by"
    assert {row["name"] for row in source["rows"]} == {"cli:yt-dlp", "cli:iflow"}
