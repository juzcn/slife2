"""`slife2-skills`' tools that write the folder: install, remove, switch.

The one family whose install is *files* rather than a config line, and the only
place in this system where a model names paths on the operator's machine.  What
these tests are about is that boundary, and the two halves that follow from what
a skill is: the folder is the truth about what is installed, and the config is
the truth about whether anybody wants it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.config import load
from slife2.mcp_server import LIST_SOURCES
from slife2.skills import USE_TOOL
from slife2.skills_server import build_server

pytestmark = pytest.mark.unit

#: A config the loader accepts.  On disk, because two of these tools write it.
CONFIG = """\
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    api_key: ${SKILLS_TEST_KEY:-none}
    models:
      - model: big
        context_window: 100000
        max_tokens: 4000
default: local/big
"""


def config_on_disk(isolated_runtime: Path, text: str = CONFIG) -> Path:
    isolated_runtime.mkdir(parents=True, exist_ok=True)
    path = isolated_runtime / "slife2.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def skill(root: Path, name: str, *, body: str = "The old body.") -> Path:
    """One skill already on disk, in the `skills/` a bare `scan()` looks in."""
    folder = root / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(manifest(name, body=body), encoding="utf-8")
    return folder


def manifest(name: str, description: str = "what it is for", body: str = "The body."):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def files(main: str, **extra: str) -> list[dict[str, str]]:
    """The `files` argument: a manifest, and whatever else the test writes."""
    return [
        {"path": "SKILL.md", "content": main},
        *({"path": path, "content": text} for path, text in extra.items()),
    ]


async def ask(client: Client, tool: str, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    return "".join(getattr(block, "text", "") for block in result.content or [])


# --- installing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_installing_a_skill_writes_the_folder_and_reads_at_once(
    isolated_runtime: Path,
) -> None:
    """**The whole of how a skill is installed, and why nothing restarts.**

    A skill is a directory with a `SKILL.md` in it, so this writes one — and the
    folder is re-read on every ask, so what it wrote is catalogued and readable
    from the same moment, with no list to keep in step and no start to wait for.
    """
    config_on_disk(isolated_runtime)
    async with Client(build_server(load())) as client:
        answer = await ask(
            client,
            "skill_set",
            name="note-taking",
            files=files(
                manifest("note-taking", description="Keep notes."),
                **{"scripts/save.py": "print('saved')\n"},
            ),
        )
        declared = await client.call_tool(LIST_SOURCES, {})
        read = await ask(client, USE_TOOL, name="note-taking")

    folder = isolated_runtime / "skills" / "note-taking"
    assert "installed (2 file(s))" in answer
    assert (folder / "SKILL.md").is_file()
    assert (folder / "scripts" / "save.py").read_text(encoding="utf-8") == (
        "print('saved')\n"
    )
    rows = {row["name"]: row for row in declared.data["sources"][0]["rows"]}
    assert rows["skill:note-taking"]["description"] == "Keep notes."
    assert "Keep notes." in read


@pytest.mark.asyncio
async def test_a_file_that_leaves_the_skill_directory_is_refused(
    isolated_runtime: Path,
) -> None:
    """**The boundary, and the only place a model names a path.**

    A playbook is written by a model, and one of its `path` values becomes a path
    on the operator's machine — so `../` in one of them is how a skill would
    rewrite the config that describes it, or the turn log beside it.  Resolved
    before the check, because `scripts/../../..` is only outside once it has been
    resolved, and refused *before* anything is created, so a refused install
    leaves nothing at all behind.
    """
    config_on_disk(isolated_runtime)
    before = (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")

    async with Client(build_server(load())) as client:
        answer = await ask(
            client,
            "skill_set",
            name="sneaky",
            files=files(manifest("sneaky"), **{"../escape.txt": "no"}),
        )

    assert answer.startswith("[refused]") and "outside" in answer
    assert not (isolated_runtime / "escape.txt").exists()
    assert not (isolated_runtime / "skills" / "sneaky").exists()
    assert (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_a_name_that_is_a_path_is_refused(isolated_runtime: Path) -> None:
    """The same boundary through the *name*: it is a directory name, and the
    resolution is what decides, not the spelling."""
    config_on_disk(isolated_runtime)

    async with Client(build_server(load())) as client:
        answer = await ask(
            client, "skill_set", name="../outside", files=files(manifest("x"))
        )
        removed = await ask(client, "skill_remove", name="../slife2.yaml")

    assert answer.startswith("[refused]") and "outside" in answer
    assert removed.startswith("[refused]")
    assert (isolated_runtime / "slife2.yaml").is_file()


@pytest.mark.asyncio
async def test_a_skill_with_no_manifest_is_refused(isolated_runtime: Path) -> None:
    """The manifest is what makes a directory a skill (`scan`), so a set of files
    without one is an install that would report success and install nothing
    anything can find."""
    config_on_disk(isolated_runtime)

    async with Client(build_server(load())) as client:
        answer = await ask(
            client,
            "skill_set",
            name="incomplete",
            files=[{"path": "notes.txt", "content": "not a skill"}],
        )

    assert answer.startswith("[refused]") and "SKILL.md" in answer
    assert not (isolated_runtime / "skills" / "incomplete").exists()


@pytest.mark.asyncio
async def test_replacing_a_skill_leaves_no_half_written_one(
    isolated_runtime: Path,
) -> None:
    """Whole or not at all: written into a staging directory, then swapped in.

    What this protects is a model following a playbook that stops in the middle
    of a command — worse than one told the skill is not installed — so the
    staging directory has to be gone whether the install worked or not.
    """
    config_on_disk(isolated_runtime)
    skill(isolated_runtime, "one", body="The old body.")

    async with Client(build_server(load())) as client:
        await ask(
            client,
            "skill_set",
            name="one",
            files=files(manifest("one"), **{"scripts/new.py": "print(1)\n"}),
        )
        read = await ask(client, USE_TOOL, name="one")

    folders = {path.name for path in (isolated_runtime / "skills").iterdir()}
    assert folders == {"one"}, "no staging or replaced directory left behind"
    assert "The old body." not in read, "the manifest is the one just written"
    assert (isolated_runtime / "skills" / "one" / "scripts" / "new.py").is_file()


# --- removing and switching ---------------------------------------------------


@pytest.mark.asyncio
async def test_removing_a_skill_takes_the_folder_and_leaves_the_entry(
    isolated_runtime: Path,
) -> None:
    """The `skills:` entry is what the playbook is *given*, and it is the
    operator's — so uninstalling the files does not throw away the wiring that
    re-installing them needs, and a name with no folder is not a mistake."""
    config_on_disk(
        isolated_runtime,
        CONFIG + "skills:\n  one:\n    env:\n      SOME_KEY: ${SOME_KEY:-x}\n",
    )
    skill(isolated_runtime, "one")

    async with Client(build_server(load())) as client:
        answer = await ask(client, "skill_remove", name="one")
        missing = await ask(client, "skill_remove", name="one")

    assert "uninstalled" in answer and "`skills:` entry stays" in answer
    assert not (isolated_runtime / "skills" / "one").exists()
    assert "SOME_KEY" in (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")
    assert "is not installed" in missing


@pytest.mark.asyncio
async def test_switching_a_skill_off_keeps_it_installed_and_unreadable(
    isolated_runtime: Path,
) -> None:
    """**The one thing the folder cannot say about a skill.**

    What is installed is a directory, and a directory has no opinion about
    whether anybody wants it — so the switch is written in the config, and both
    halves follow: the row says `disabled`, and `skill_use` refuses rather than
    reading a playbook the operator took out of the way.  The files are
    untouched, which is the difference between this and `skill_remove`.
    """
    config_on_disk(isolated_runtime)
    skill(isolated_runtime, "one")

    async with Client(build_server(load())) as client:
        off = await ask(client, "skill_set_enabled", name="one", enabled=False)
        declared = await client.call_tool(LIST_SOURCES, {})
        with pytest.raises(ToolError) as raised:
            await client.call_tool(USE_TOOL, {"name": "one"})
        listed = await ask(client, "skill_list")
        on = await ask(client, "skill_set_enabled", name="one", enabled=True)

    rows = {row["name"]: row for row in declared.data["sources"][0]["rows"]}
    assert "off" in off
    assert rows["skill:one"]["status"] == "disabled"
    assert "switched off" in str(raised.value)
    assert "one [off]" in listed
    assert (isolated_runtime / "skills" / "one" / "SKILL.md").is_file(), (
        "files untouched"
    )
    assert "on again" in on
    assert "enabled" not in (isolated_runtime / "slife2.yaml").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_the_switch_is_written_for_a_skill_with_no_entry_yet(
    isolated_runtime: Path,
) -> None:
    """**A skill nobody wrote an entry for is on**, so the only fact worth writing
    down is that it is off.

    Which is why this is not `configfile.set_enabled` alone: that call refuses an
    entry that is not there and writes nothing, and a tool that reported success
    over an unwritten file is the exact failure the section exists to avoid.
    """
    config_on_disk(isolated_runtime)
    skill(isolated_runtime, "one")

    async with Client(build_server(load())) as client:
        await ask(client, "skill_set_enabled", name="one", enabled=False)
        declared = await client.call_tool(LIST_SOURCES, {})

    assert "enabled: false" in (isolated_runtime / "slife2.yaml").read_text(
        encoding="utf-8"
    )
    rows = {row["name"]: row for row in declared.data["sources"][0]["rows"]}
    assert rows["skill:one"]["status"] == "disabled"


@pytest.mark.asyncio
async def test_the_listing_says_what_each_skill_is_for_and_what_it_needs(
    isolated_runtime: Path,
) -> None:
    """The half DESIGN.md §9 named as next: the name a model reads with `skill_use`
    has to come from somewhere, and its requirements are worth knowing *before*
    reading instructions that would fail halfway through."""
    config_on_disk(isolated_runtime)
    folder = isolated_runtime / "skills" / "needy"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\n"
        "name: needy\n"
        "description: Needs a key.\n"
        "metadata:\n"
        "  openclaw:\n"
        "    requires:\n"
        "      env: [SOME_KEY]\n"
        "---\n\nThe body.\n",
        encoding="utf-8",
    )

    async with Client(build_server(load())) as client:
        listed = await ask(client, "skill_list")

    assert "needy [on]" in listed
    assert "Needs a key." in listed
    assert "SOME_KEY" in listed, "and whether it has been supplied"
