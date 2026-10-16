"""One family per home: the section a plugin holds is the caller's.

A family is a plugin's view of *a config section* — the entries under `mcp:`,
the playbooks under `skills:`, the commands under `cli:` — and a section belongs
to a home.  So a process serving two data directories holds two of them, and
which one a call gets is the home that call named, exactly as the files it
resolves are (`slife2.libs.host.paths.home`).

The other half of the rule is what makes this testable at all: a server built
with a config *handed in* keeps that one for every home
(`slife2.libs.host.config.for_the_call`), which is how every test in this suite
builds its servers.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from slife2.libs.host.config import (
    Config,
    ToolServerSettings,
    default_config,
    load_cached,
)
from slife2.libs.host.paths import in_home
from slife2.libs.plugin.audience import client_meta
from slife2.libs.plugin.family import Family, Links
from slife2.libs.plugin.protocol import CALL_SOURCE, LIST_SOURCES
from slife2.plugins.cli.server import build_server as build_cli
from slife2.plugins.mcp_tools.server import build_server as build_mcp_tools
from slife2.plugins.restapi.server import build_server as build_restapi
from tests.fakes import config_file

pytestmark = pytest.mark.unit

#: Two homes, each naming one command of its own.  Distinct names, because the
#: question is not which one wins but whether the other can be seen at all.
MINE = """
cli:
  mine-only:
    command: mine
    description: Recorded in this home.
"""

THEIRS = """
cli:
  theirs-only:
    command: theirs
    description: Recorded in the other one.
"""


async def list_from(client: Client, home: Path) -> Any:
    """`list_sources` as the caller in *home* — the meta a server reads it from."""
    with in_home(home):
        return await client.call_tool(LIST_SOURCES, {}, meta=client_meta("jack"))


@pytest.mark.asyncio
async def test_a_call_gets_the_section_of_the_home_it_names(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**One process, two homes, two sections** — the whole of what a shared
    family plugin has to get right.

    Both calls go to one server built once.  What differs is the home each call
    names, and a family held for the process would answer the second caller with
    the first one's command list — which for a `mcp:` entry is a connection to
    somebody else's server, and for a `skills:` entry is the credentials a
    playbook is run with.
    """
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))
    config_file(mine, MINE)
    config_file(theirs, THEIRS)

    server = build_cli(default_config(), source=load_cached)
    async with Client(server) as client:
        here = await list_from(client, mine)
        there = await list_from(client, theirs)

    assert "mine-only" in str(here.data)
    assert "theirs-only" not in str(here.data)
    assert "theirs-only" in str(there.data)
    assert "mine-only" not in str(there.data)


@pytest.mark.asyncio
async def test_a_config_handed_in_is_the_config_every_home_gets(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The injected config wins**, and this is the half that keeps the suite
    honest.

    Every test in this repo builds its server with a config object — no file, no
    home, and nothing on disk to read — so a builder that went looking for a file
    instead would be a test reading the developer's own `slife2.yaml`, or
    answering with the built-in defaults.  What `source=None` means is "there is
    nothing live here": one config, for every home.
    """
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))
    config_file(mine, MINE)
    config_file(theirs, THEIRS)
    injected: Config = load_cached(str(config_file(tmp_path / "elsewhere", MINE)))

    server = build_cli(injected)
    async with Client(server) as client:
        there = await list_from(client, theirs)

    assert "mine-only" in str(there.data), "the injected section, not the home's"
    assert "theirs-only" not in str(there.data)


# --- the family's own vocabulary ----------------------------------------------
#
# Two sentences a family writes are about *tools*, and the tool has a different
# name in each family: the one that switches an entry back on (`call`'s refusal)
# and the one that reports an entry's state (`tools_as_text`'s three "no list"
# answers).  Both used to be written out in this module with a name that no
# family serves — `set_enabled`, `_list` — so a model reading either was told to
# call a tool that does not exist.  These drive the *real* family plugins,
# because what is wrong is the wire from the plugin's vocabulary to the family's
# sentence and not the sentence alone.


def only_entry(config: Config, section: str, name: str) -> Config:
    """*config* with one switched-off entry under *section*.

    Switched off on purpose: a disabled entry has no link at all (`Held`), so
    the family answers its refusal without ever starting a process — which is
    what makes these tests about a sentence rather than about somebody's daemon.
    """
    entry = ToolServerSettings(name=name, command="somewhere", enabled=False)
    if section == "mcp":
        return replace(config, mcp={name: entry})
    return replace(config, rest_apis={name: entry})


@pytest.mark.asyncio
async def test_a_switched_off_call_names_the_tool_that_switches_it_on() -> None:
    """**The refusal is read by a model, and it is read as an instruction.**

    A switched-off entry keeps its rows in the catalogue, so a model that holds
    one of its tools can still call it — and this is the answer it gets.  The
    sentence said "Its `set_enabled` tool turns it on", naming a tool that
    exists under no family: the real names are `mcp_set_enabled` and
    `rest_api_set_enabled`, and a model that does what it was told spends a call
    finding out that nothing serves it.  The vocabulary is the plugin's
    (`FamilyWords.enable_tool`), so the sentence takes it from there.
    """
    config = only_entry(default_config(), "mcp", "off")
    async with Client(build_mcp_tools(config)) as client:
        refused = await client.call_tool(
            CALL_SOURCE, {"source": "off", "tool": "anything", "arguments": {}}
        )

    assert refused.data["ok"] is False, "switched off, so nothing was run"
    assert "mcp_set_enabled" in refused.data["text"]
    assert "`set_enabled`" not in refused.data["text"], "no tool is called that"


@pytest.mark.asyncio
async def test_a_no_list_answer_names_the_family_s_own_list_tool() -> None:
    """**Three ways to have no list, and every one of them points somewhere.**

    Switched off, not answering, and answering with nothing are three answers
    (`tools_as_text`) and each ends by naming the tool that reports the entry's
    state — so a model that asked what a server offers has somewhere to go next.
    All three said `` `_list` ``, which is no family's tool: the MCP family's is
    `mcp_list` and the REST family's is `rest_api_list`, and the whole point of
    `FamilyWords` is that this module does not get to guess either of them.
    """
    mcp = only_entry(default_config(), "mcp", "off")
    rest = only_entry(default_config(), "rest-api", "off")
    async with Client(build_mcp_tools(mcp)) as client:
        asked = await client.call_tool("mcp_list_tools", {"name": "off"})
    async with Client(build_restapi(rest)) as client:
        asked_rest = await client.call_tool("rest_api_list_tools", {"name": "off"})

    assert "mcp_list" in str(asked.data)
    assert "rest_api_list" in str(asked_rest.data)
    for answer in (str(asked.data), str(asked_rest.data)):
        assert "`_list`" not in answer, "no family serves a tool called that"


@pytest.mark.asyncio
async def test_an_edit_leaves_an_entry_where_the_file_has_it() -> None:
    """**File order is the order a family declares its sources in.**

    `Family.put` took the old entry out and put the new one back, which is a
    move to the *end* of the mapping: an edit to the middle entry of three
    re-ordered the declaration, and the hub preserves that order into
    `servers()` — so the status view reshuffled under an edit that changed
    nothing about the order, while `mcp_list`/`rest_api_list`, which read the
    file, kept it.  An assignment to an existing key is the one operation that
    changes the value without changing the order.
    """
    entries = [
        ToolServerSettings(name=name, command="somewhere", enabled=False)
        for name in ("a", "b", "c")
    ]
    family = Family(
        default_config(),
        category="mcp",
        entries=entries,
        links=Links(),
    )

    await family.put(replace(entries[1], command="elsewhere"))
    declared = await family.declare()

    assert [source["name"] for source in declared["sources"]] == ["a", "b", "c"]
