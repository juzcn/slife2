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
from fastmcp.exceptions import ToolError

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

#: The sections every file must have (SDD §4.1).  These homes are read through
#: `load_cached`, so each one is a file the loader has to accept.
REQUIRED = """\
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    models: [{model: big}]
embeddings:
  providers:
    local:
      base_url: http://127.0.0.1:17347/v1
      model: big-emb
"""

#: Two homes, each naming one command of its own.  Distinct names, because the
#: question is not which one wins but whether the other can be seen at all.
MINE = (
    REQUIRED
    + """
cli:
  mine-only:
    command: mine
    description: Recorded in this home.
"""
)

THEIRS = (
    REQUIRED
    + """
cli:
  theirs-only:
    command: theirs
    description: Recorded in the other one.
"""
)


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

    Every test in this repo builds its server with a config object, so a builder
    that went looking for a file instead would be a test reading a home's own
    `slife2.yaml` — or the developer's — rather than the config it was handed.
    What `source=None` means is "there is nothing live here": one config, for
    every home.
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
# What a family says in its own words is now a *field*: which noun a row of its
# listing sits under, what one exposed item is called, and which listing tool a
# refusal points at.  All three differ between the two connection families, and
# all three used to be written out in this module with a name no family serves —
# `set_enabled`, `_list` — so a model reading either was told to call a tool that
# does not exist.  These drive the *real* family plugins, because what is wrong
# is the wire from the plugin's vocabulary to the family's answer and not the
# answer alone.


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
async def test_a_page_with_no_tools_says_which_kind_of_nothing_it_is() -> None:
    """**Three ways to have no tools, and one word says which.**

    Switched off, not answering and answering with nothing are three answers
    (`tools_payload`), and each used to be a sentence ending by naming the tool
    that reports the entry's state — all three said `` `_list` ``, which no
    family serves.  The state *is* the answer now, in the same words the listing
    and the hub use, and the rows sit under the family's own word for one: a
    `tool` here, an `operation` next door.
    """
    mcp = only_entry(default_config(), "mcp", "off")
    rest = only_entry(default_config(), "rest-api", "off")
    async with Client(build_mcp_tools(mcp)) as client:
        asked = await client.call_tool("mcp_list_tools", {"name": "off"})
    async with Client(build_restapi(rest)) as client:
        asked_rest = await client.call_tool("rest_api_list_tools", {"name": "off"})

    assert asked.data["state"] == "off", "the switch, and the word the listing uses"
    assert asked_rest.data["state"] == "off"
    assert asked.data["tools"] == [] and asked.data["total"] == 0
    assert asked_rest.data["operations"] == []
    assert "tools" not in asked_rest.data, "the other family's word for a row"
    assert "`_list`" not in str(asked.data), "no family serves a tool called that"


@pytest.mark.asyncio
async def test_a_refusal_names_the_family_s_own_list_tool() -> None:
    """**The name differs per family, so the family supplies it.**

    A name this process does not hold is refused rather than answered with an
    empty page — `total: 0` would read as a server that offers nothing — and the
    sentence points at the listing tool, which is `mcp_list` here and
    `rest_api_list` next door.  Both were once written out in this module as
    `` `_list` ``, a name no family serves.
    """
    mcp = only_entry(default_config(), "mcp", "off")
    rest = only_entry(default_config(), "rest-api", "off")
    async with Client(build_mcp_tools(mcp)) as client:
        with pytest.raises(ToolError) as asked:
            await client.call_tool("mcp_list_tools", {"name": "nowhere"})
    async with Client(build_restapi(rest)) as client:
        with pytest.raises(ToolError) as asked_rest:
            await client.call_tool("rest_api_list_tools", {"name": "nowhere"})

    assert "mcp_list" in str(asked.value)
    assert "rest_api_list" in str(asked_rest.value)
    for answer in (str(asked.value), str(asked_rest.value)):
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
        category="mcp",
        entries=entries,
        links=Links(),
    )

    await family.put(replace(entries[1], command="elsewhere"))
    declared = await family.declare()

    assert [source["name"] for source in declared["sources"]] == ["a", "b", "c"]
