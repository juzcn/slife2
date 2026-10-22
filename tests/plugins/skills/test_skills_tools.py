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

from slife2.libs.host.config import load
from slife2.libs.plugin.protocol import LIST_SOURCES
from slife2.plugins.skills.playbooks import USE_TOOL
from slife2.plugins.skills.server import build_server
from tests.fakes import config_file

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


def config_on_disk(data_dir: Path, text: str = CONFIG) -> Path:
    """This module's config, written where the loader looks for it."""
    return config_file(data_dir, text)


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


async def ask(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    """These tools answer with a dict, which is what the model reads."""
    result = await client.call_tool(tool, arguments)
    assert isinstance(result.data, dict), result
    return result.data


async def ask_text(client: Client, tool: str, **arguments: Any) -> str:
    """`skill_use` answers with the document itself, which is also what it reads."""
    result = await client.call_tool(tool, arguments)
    assert isinstance(result.data, str), result
    return result.data


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
        read = await ask_text(client, USE_TOOL, name="note-taking")

    folder = isolated_runtime / "skills" / "note-taking"
    assert answer == {"name": "note-taking", "state": "on", "files": 2}
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
        with pytest.raises(ToolError) as raised:
            await client.call_tool(
                "skill_set",
                {
                    "name": "sneaky",
                    "files": files(manifest("sneaky"), **{"../escape.txt": "no"}),
                },
            )

    assert "outside" in str(raised.value)
    assert not (isolated_runtime / "escape.txt").exists()
    assert not (isolated_runtime / "skills" / "sneaky").exists()
    assert (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_a_name_that_is_a_path_is_refused(isolated_runtime: Path) -> None:
    """The same boundary through the *name*: it is a directory name, and the
    resolution is what decides, not the spelling."""
    config_on_disk(isolated_runtime)

    async with Client(build_server(load())) as client:
        with pytest.raises(ToolError) as set_raised:
            await client.call_tool(
                "skill_set", {"name": "../outside", "files": files(manifest("x"))}
            )
        with pytest.raises(ToolError):
            await client.call_tool("skill_remove", {"name": "../slife2.yaml"})

    assert "outside" in str(set_raised.value)
    assert (isolated_runtime / "slife2.yaml").is_file()


@pytest.mark.asyncio
async def test_a_dot_name_is_refused_because_nothing_would_list_it(
    isolated_runtime: Path,
) -> None:
    """**The dot rule is the staging directory's, and it binds the operator too.**

    `install` stages into `.{name}.installing` and `scan` skips every
    dot-directory so that a `SIGKILL` in that window cannot become a catalogue
    row.  A skill an operator names `.notes` would therefore install, report
    success and be unfindable — no row, no `skill_list` line, and `skill_use`
    answering that no such skill exists — so the name is refused before anything
    is created, where the caller can still change it.
    """
    config_on_disk(isolated_runtime)

    async with Client(build_server(load())) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool(
                "skill_set",
                {
                    "name": ".notes",
                    "files": files(manifest("notes")),
                },
            )
        with pytest.raises(ToolError) as unreadable:
            await client.call_tool(USE_TOOL, {"name": ".notes"})

    assert "dot" in str(raised.value)
    assert not (isolated_runtime / "skills" / ".notes").exists()
    assert "no skill called" in str(unreadable.value)


@pytest.mark.asyncio
async def test_a_name_that_resolves_into_a_dot_directory_is_refused_too(
    isolated_runtime: Path,
) -> None:
    """**The dot rule is about where the name lands, and it was about its spelling.**

    `_within` resolves a name before comparing it — that is its own argument, and
    the file guard next door is built on it — while the dot check ran on the raw
    string.  So `a/../.notes` walked past it and installed into
    `<data>/skills/.notes`, and the answer was the confident one this tool
    gives on success ("`a/../.notes` is installed (1 file(s))…"): `scan` skips
    every dot-directory by rule, so there was no `skill_list` line, no catalogue
    row, and `skill_use` answered "no skill called '.notes'" — an install that
    reported success and installed nothing anything could find, which is exactly
    what the rule exists to prevent.  Checked on the resolved path, every
    spelling of the same directory is refused, and before anything is created.
    """
    config_on_disk(isolated_runtime)

    async with Client(build_server(load())) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool(
                "skill_set",
                {
                    "name": "a/../.notes",
                    "files": files(manifest("notes")),
                },
            )
        listed = await ask(client, "skill_list")

    assert "dot" in str(raised.value)
    assert not (isolated_runtime / "skills" / ".notes").exists()
    assert "notes" not in listed, "nothing was installed, so nothing is listed"


@pytest.mark.asyncio
async def test_a_name_that_is_not_one_directory_is_refused(
    isolated_runtime: Path,
) -> None:
    """**A skill is one directory directly under `skills/`, and `scan` says so.**

    `scan` lists the folder's *direct children*, so a name carrying a separator
    installed where nothing looks: `skill_set("a/b", …)` wrote `skills/a/b` —
    the per-file `mkdir(parents=True)` had made `skills/a` on the way — renamed
    it into place successfully and answered "`a/b` is installed … a search finds
    it from your next one", while there was no `skill_list` line, no catalogue
    row, and `skill_use("a/b")` answered that no skill was called that.  A
    trailing separator was the same failure one step along: the name landed in
    `one` while the staging directory was built from the spelling, so `one/`
    staged through `skills/.one` — a dot-directory `scan` skips by rule — and
    the `finally` removed the stage while leaving that behind for good.

    Both are refused now, before anything is created, which is what makes the
    last assertion here the whole of the guarantee: a refused install leaves no
    directory at all, not even the one the staging path would have made.
    """
    config_on_disk(isolated_runtime)

    async with Client(build_server(load())) as client:
        for name in ("a/b", "one/", "a/./b"):
            with pytest.raises(ToolError) as raised:
                await client.call_tool(
                    "skill_set",
                    {"name": name, "files": files(manifest("x"))},
                )
            assert "one skill directory" in str(raised.value), name
        listed = await ask(client, "skill_list")

    skills = isolated_runtime / "skills"
    folders = {path.name for path in skills.iterdir()} if skills.is_dir() else set()
    assert folders == set(), f"nothing installed, nothing left behind: {folders}"
    assert listed == {"skills": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.asyncio
async def test_a_skill_with_no_manifest_is_refused(isolated_runtime: Path) -> None:
    """The manifest is what makes a directory a skill (`scan`), so a set of files
    without one is an install that would report success and install nothing
    anything can find."""
    config_on_disk(isolated_runtime)

    async with Client(build_server(load())) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool(
                "skill_set",
                {
                    "name": "incomplete",
                    "files": [{"path": "notes.txt", "content": "not a skill"}],
                },
            )

    assert "SKILL.md" in str(raised.value)
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
        read = await ask_text(client, USE_TOOL, name="one")

    folders = {path.name for path in (isolated_runtime / "skills").iterdir()}
    assert folders == {"one"}, "no staging or replaced directory left behind"
    assert "The old body." not in read, "the manifest is the one just written"
    assert (isolated_runtime / "skills" / "one" / "scripts" / "new.py").is_file()


# --- removing and switching ---------------------------------------------------


@pytest.mark.asyncio
async def test_removing_a_skill_takes_the_folder_and_its_entry(
    isolated_runtime: Path,
) -> None:
    """**The `skills:` entry describes this skill, so it goes with the folder.**

    It is the playbook's `env` and switch; a skill that is uninstalled and an
    entry that still says what it is given are two halves of one thing apart,
    and the section would grow a row per thing ever installed.  `skill_set`
    writes the entry again — with the env it is given, or with the operator's
    when it is not — so removing it loses no wiring that a reinstall needs.

    A name the folder does not have is **refused**, though: nothing was deleted,
    so a `skill_remove` that answered as ordinary text would arrive with `ok`
    true and read as an uninstall that happened.
    """
    config_on_disk(
        isolated_runtime,
        CONFIG + "skills:\n  one:\n    env:\n      SOME_KEY: ${SOME_KEY:-x}\n",
    )
    skill(isolated_runtime, "one")

    async with Client(build_server(load())) as client:
        answer = await ask(client, "skill_remove", name="one")
        with pytest.raises(ToolError) as missing:
            await client.call_tool("skill_remove", {"name": "one"})

    assert answer == {"name": "one", "removed": True}
    assert not (isolated_runtime / "skills" / "one").exists()
    assert "SOME_KEY" not in (isolated_runtime / "slife2.yaml").read_text(
        encoding="utf-8"
    ), "the entry went with the folder"
    assert "is not installed" in str(missing.value)


@pytest.mark.asyncio
async def test_removing_by_the_header_name_removes_its_directory(
    isolated_runtime: Path,
) -> None:
    """A skill answers to two names; removal must delete the folder, not the name.

    `find` prefers the frontmatter `name:`, so a model calls `skill_remove` with
    a name the directory does not have.  Passing that name to `remove` resolved
    to no folder, after the config entry had already been deleted.
    """
    config_on_disk(
        isolated_runtime,
        CONFIG + "skills:\n  the-name:\n    env:\n      SOME_KEY: ${SOME_KEY:-x}\n",
    )
    folder = isolated_runtime / "skills" / "the-folder"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(manifest("the-name"), encoding="utf-8")

    async with Client(build_server(load())) as client:
        answer = await ask(client, "skill_remove", name="the-name")

    assert answer == {"name": "the-name", "removed": True}
    assert not folder.exists()
    assert "the-name" not in (isolated_runtime / "slife2.yaml").read_text(
        encoding="utf-8"
    ), "the entry went with the folder"


@pytest.mark.asyncio
async def test_installing_a_skill_writes_the_env_it_is_given(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The `skills:` entry is half of what installing a skill means.**

    The other half is the folder.  `skill_set` used to write only files, so a
    model that installed a playbook declaring `requires.env` had no way to
    supply the value and the next `skill_use` reported a key nobody could set.
    The entry is read-modify-written rather than replaced: the switch is
    `skill_set_enabled`'s and a reinstall must not turn a skill back on.

    An install that omits `env` keeps what is there, and `{}` is the explicit
    clear — the two answers a caller needs on a reinstall.
    """
    monkeypatch.setenv("SOME_KEY", "a-real-key")
    config_on_disk(
        isolated_runtime,
        CONFIG + "skills:\n  one:\n    enabled: false\n",
    )
    needy = (
        "---\n"
        "name: one\n"
        "metadata: {h: {requires: {env: [SOME_KEY]}}}\n"
        "---\n\nThe body.\n"
    )
    async with Client(build_server(load())) as client:
        first = await ask(
            client,
            "skill_set",
            name="one",
            files=files(needy),
            env={"SOME_KEY": "${SOME_KEY}"},
        )
        written = (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")
        listed = await ask(client, "skill_list")

        # Omitted: the entry the operator has is left alone.
        await ask(client, "skill_set", name="one", files=files(needy))
        kept = (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")

        # Empty: the explicit clear, and the switch still survives.
        await ask(
            client,
            "skill_set",
            name="one",
            files=files(needy),
            env={},
        )
        cleared = (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")

    assert first == {"name": "one", "state": "off", "files": 1}
    assert "SOME_KEY" in written and "a-real-key" not in written, (
        "the reference is written, not the secret it resolves to"
    )
    assert "enabled: false" in written, "the switch is not this tool's to reset"
    assert "are met" in listed["skills"][0]["requirements"], (
        "the resolved value reaches the listing at once"
    )
    assert "SOME_KEY" in kept
    assert "SOME_KEY" not in cleared
    assert "enabled: false" in cleared


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
    assert off == {"name": "one", "enabled": False, "state": "off"}
    assert rows["skill:one"]["status"] == "disabled"
    assert "switched off" in str(raised.value)
    assert [row["state"] for row in listed["skills"]] == ["off"]
    assert (isolated_runtime / "skills" / "one" / "SKILL.md").is_file(), (
        "files untouched"
    )
    assert on["state"] == "on" and on["enabled"] is True
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
    """The half DESIGN.md §10 named as next: the name a model reads with `skill_use`
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

    (row,) = listed["skills"]
    assert row["name"] == "needy"
    assert row["state"] == "on"
    assert row["description"] == "Needs a key."
    assert "SOME_KEY" in row["requirements"], "and whether it has been supplied"
