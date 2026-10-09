"""slife2-skills: the playbooks, and the two things it serves about them.

The server is a plugin like any other, so what is worth testing is not that MCP
works — `test_builtins.py` does that for the shape — but the two decisions it
owns and the hub used to: that `skill_use` is *derived* from a signature rather
than hand-written, and that `list_sources` answers with the folder's whole
list, which is what makes a deleted skill stop being a hit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.audience import for_the_model
from slife2.config import default_config, load
from slife2.mcp_server import LIST_SOURCES
from slife2.skills import USE_TOOL
from slife2.skills_server import (
    CATEGORY,
    CONFIG_KEY,
    SERVER_NAME,
    SOURCE,
    build_server,
    catalogue,
)

pytestmark = pytest.mark.unit


def skill(
    root: Path,
    name: str,
    *,
    description: str = "what it is for",
    body: str = "The body.",
) -> Path:
    """One skill on disk, in the `skills/` a bare `scan()` looks in."""
    folder = root / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return folder


# --- what it serves -----------------------------------------------------------


def test_the_server_is_named_where_a_client_can_find_it() -> None:
    assert SERVER_NAME == "slife2-skills"


@pytest.mark.asyncio
async def test_the_reader_is_the_one_marked_tool_and_the_declaration_is_not(
    isolated_runtime: Path,
) -> None:
    """**The audience mark decides what the model may call**, and it is the only
    thing that does.

    `skill_use` is the model's.  `list_sources` is the hub's, and it has to
    stay out of the model's list for the reason the whole gate exists: a tool
    that declares rows is a tool that could declare a category — and a plugin
    able to name its own category could offer the model `remember`.
    """
    skill(isolated_runtime, "one")
    async with Client(build_server(default_config())) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert set(listed) == {USE_TOOL, LIST_SOURCES}
    assert for_the_model(listed[USE_TOOL].meta)
    assert listed[LIST_SOURCES].meta is None or not for_the_model(
        listed[LIST_SOURCES].meta
    )


@pytest.mark.asyncio
async def test_the_schema_is_read_off_the_signature(isolated_runtime: Path) -> None:
    """What the model sees is derived, which is why it cannot drift.

    `skill_use` used to be a hand-written parameters dict beside a
    hand-written description, because the hub had no function to decorate.  Now
    it has one, and the argument it takes is the schema.
    """
    skill(isolated_runtime, "one")
    async with Client(build_server(default_config())) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    schema = listed[USE_TOOL].input_schema
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["required"] == ["name"]
    assert "playbook" in (listed[USE_TOOL].description or "")


# --- reading one --------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_use_reads_the_document(isolated_runtime: Path) -> None:
    skill(isolated_runtime, "one", body="Do it.")
    async with Client(build_server(default_config())) as client:
        result = await client.call_tool(USE_TOOL, {"name": "one"})

    assert result.data.rstrip().endswith("Do it.")


@pytest.mark.asyncio
async def test_a_skill_dropped_in_later_is_readable_at_once(
    isolated_runtime: Path,
) -> None:
    """The folder is read on every call, not snapshotted at startup.

    It is the same promise `list_sources` keeps for a search, and it holds
    here for the same reason: there is nothing to keep in step.
    """
    skill(isolated_runtime, "one")
    async with Client(build_server(default_config())) as client:
        skill(isolated_runtime, "two", body="Second body.")
        result = await client.call_tool(USE_TOOL, {"name": "two"})

    assert result.data.rstrip().endswith("Second body.")


@pytest.mark.asyncio
async def test_an_unknown_name_is_a_failure_the_model_reads(
    isolated_runtime: Path,
) -> None:
    """`ok` false used to travel to the caller as a value; over MCP the only way
    to say "this call failed" is to fail, and the text has to survive the hop."""
    skill(isolated_runtime, "one")
    async with Client(build_server(default_config())) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(USE_TOOL, {"name": "nothing"})

    assert "one" in str(caught.value), "the answer names what does exist"


@pytest.mark.asyncio
async def test_a_declared_key_is_read_here_and_reported_before_the_model_acts(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`skills:` is this server's half of the config, and it is resolved here.

    A skill whose header declares `requires.env` gets its value from the config,
    through the same chain as a provider key.  What the model is owed is the
    *difference* — told before it acts on instructions that would fail, rather
    than finding out when the commands fail.  The key is held by this process
    for the same reason `slife2-mcp-tools` holds a tool server's: one process
    knows the answer, and nothing about a declared key needs an address or a
    protocol.
    """
    monkeypatch.delenv("BAIDU_API_KEY", raising=False)
    folder = isolated_runtime / "skills" / "one"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\n"
        "name: one\n"
        "metadata: {h: {requires: {env: [BAIDU_API_KEY]}}}\n"
        "---\n\nRun it.\n",
        encoding="utf-8",
    )
    path = isolated_runtime / "slife2.yaml"
    path.write_text(
        "skills:\n  one:\n    env: {BAIDU_API_KEY: secret}\n", encoding="utf-8"
    )

    async with Client(build_server(load(path))) as client:
        with_key = await client.call_tool(USE_TOOL, {"name": "one"})

    async with Client(build_server(default_config())) as client:
        without = await client.call_tool(USE_TOOL, {"name": "one"})

    assert "are met" in with_key.data
    assert "not configured" in without.data


# --- declaring rows -----------------------------------------------------------


def test_a_skill_is_a_row_whose_schema_is_the_document(isolated_runtime: Path) -> None:
    """A playbook *is* its documentation, so the text is what a search ranks.

    That is the whole reason "drive a browser" can reach
    `skill:browser-harness` — and the row is stored as the document `skill_use`
    hands back, so what a search ranks and what a call returns are one text.
    """
    skill(isolated_runtime, "browser-harness", description="Drive a browser.")
    rows = catalogue(default_config())

    assert [row["name"] for row in rows] == ["skill:browser-harness"]
    row = rows[0]
    assert row["description"] == "Drive a browser."
    assert row["remote_name"] == "browser-harness"
    assert "The body." in str(row["schema"])
    assert row["status"] == "enabled"


def test_an_unreadable_manifest_is_a_row_that_says_so(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder said the skill is installed, so a search that found nothing
    would send the model looking for a file the operator believes is there."""
    skill(isolated_runtime, "one")
    manifest = isolated_runtime / "skills" / "one" / "SKILL.md"

    def refuse(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("cannot read")

    monkeypatch.setattr(Path, "read_text", refuse)
    rows = catalogue(default_config())

    assert manifest.exists()
    assert [row["status"] for row in rows] == ["error"]
    assert rows[0]["schema"] == ""


def test_no_skills_is_no_rows(isolated_runtime: Path) -> None:
    """A fresh install is a folder that is not there, not an error."""
    assert catalogue(default_config()) == []


def test_the_source_is_not_the_servers_own_name(isolated_runtime: Path) -> None:
    """The rows are filed under the *family*, and the server under its key.

    A source's verdict is written across every row it owns
    (`slife2.db.ToolStore.set_source_state`), so a source holding both this
    server's tool and its documents would mark every playbook broken whenever
    the connection faltered — and these rows have no connection whose state a
    verdict could come from.
    """
    assert SOURCE == "skills"
    assert SOURCE != CONFIG_KEY


@pytest.mark.asyncio
async def test_the_declaration_is_one_source_holding_every_skill(
    isolated_runtime: Path,
) -> None:
    """One source, and its rows are the whole folder.

    The plugin names the source rather than the hub deriving it from the
    plugin's own name, because a source's verdict is written across every row it
    owns — and `transport` is empty, which is what says this is a source of rows
    and not a server: there is nothing here to connect to.
    """
    skill(isolated_runtime, "browser-harness", description="Drive a browser.")
    async with Client(build_server(default_config())) as client:
        result = await client.call_tool(LIST_SOURCES, {})

    (source,) = result.data["sources"]
    assert source["name"] == SOURCE
    assert source["category"] == CATEGORY
    assert source["enabled"] and source["up"]
    assert source["transport"] == ""
    assert [row["name"] for row in source["rows"]] == ["skill:browser-harness"]
