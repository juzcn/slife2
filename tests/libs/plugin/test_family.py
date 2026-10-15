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

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from slife2.libs.host.config import Config, default_config, load_cached
from slife2.libs.host.paths import in_home
from slife2.libs.plugin.audience import client_meta
from slife2.libs.plugin.protocol import LIST_SOURCES
from slife2.plugins.cli.server import build_server as build_cli
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
