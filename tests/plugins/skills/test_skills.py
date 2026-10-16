"""Skills: the playbooks in `<data>/skills/`, and reading one.

These are about the reading half — a folder per skill, a manifest that names it,
and the rule that a broken file must not hide a skill that exists.  The tool
half is a line of glue, and it is tested where the tool is: through the hub, in
`test_toolhub.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slife2.plugins.skills.playbooks import (
    document,
    find,
    frontmatter,
    readiness,
    scan,
    split_frontmatter,
    unresolved,
    use,
)

pytestmark = pytest.mark.unit


def skill(
    root: Path, directory: str, *, header: str = "", body: str = "The body."
) -> Path:
    """One skill on disk: `<root>/<directory>/SKILL.md`."""
    folder = root / directory
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\n{header}\n---\n\n{body}", encoding="utf-8")
    return folder


def bom_skill(root: Path) -> Path:
    """One skill whose `SKILL.md` starts with a UTF-8 BOM, as a Windows editor or
    an upstream download writes it: `EF BB BF` before the opening fence."""
    folder = root / "bom-search"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        "﻿---\n"
        "name: real-name\ndescription: Real.\n"
        "metadata: {h: {requires: {env: [SOME_KEY]}}}\n"
        "---\n\nRun `scripts/go.py`.\n",
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def root(isolated_runtime: Path) -> Path:
    """The `skills/` a bare `scan()` looks in — the data directory's."""
    path = isolated_runtime / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- what is on disk ----------------------------------------------------------


def test_a_manifest_names_the_skill(root: Path) -> None:
    skill(
        root,
        "browser-harness",
        header="name: browser-harness\ndescription: Drive a browser.",
    )
    (found,) = scan()
    assert (found.name, found.description) == (
        "browser-harness",
        "Drive a browser.",
    )
    assert found.manifest == root / "browser-harness" / "SKILL.md"


def test_a_quoted_description_arrives_without_its_quotes(root: Path) -> None:
    """The reason the header is parsed as YAML rather than as `key: value`.

    A description with a colon in it has to be quoted, and v1's line-by-line
    reader handed the model the quotes as part of the description.  Every real
    skill has one — the quotes are what the format demands, not a typo.
    """
    skill(root, "one", header='name: one\ndescription: "Always use this: it matters."')
    assert scan()[0].description == "Always use this: it matters."


def test_a_directory_without_a_manifest_is_not_a_skill(root: Path) -> None:
    """A stray folder, or half a download, is invisible rather than a skill
    that fails when it is read."""
    (root / "notes").mkdir()
    (root / "notes" / "todo.txt").write_text("not a skill", encoding="utf-8")
    assert scan() == []


def test_a_manifest_with_no_header_is_named_after_its_directory(root: Path) -> None:
    (root / "plain" / "SKILL.md").parent.mkdir(parents=True)
    (root / "plain" / "SKILL.md").write_text("# Plain\n\nJust words.", encoding="utf-8")
    assert scan()[0].name == "plain"
    assert scan()[0].description == ""


def test_a_manifest_with_a_bom_still_has_its_frontmatter(root: Path) -> None:
    """**A `SKILL.md` copied in from a Windows editor may begin with a BOM.**

    The fence check is a `strip()`, and `'\\ufeff---'.strip()` is still
    `'\\ufeff---'` (and `'\\ufeff'.isspace()` is false) — so without a
    BOM-tolerant read the file reads as one with no frontmatter at all: the skill
    is named after its directory, its description is empty, and its
    `metadata: …requires.env` is never seen, which is what makes the miss silent.
    """
    bom_skill(root)
    (found,) = scan()
    assert (found.name, found.description) == ("real-name", "Real.")
    assert found.requires.env == ("SOME_KEY",)


def test_a_header_that_does_not_parse_leaves_the_skill_visible(root: Path) -> None:
    """The skill exists either way; a description is not worth a hidden skill."""
    skill(root, "broken", header="name: [unclosed\n")
    assert [one.name for one in scan()] == ["broken"]


def test_a_manifest_that_cannot_be_read_still_yields_its_skill(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unreadable file hides nothing — not even its own skill.

    The failure is simulated rather than made with permissions, because this
    suite runs on Windows where a read-only file is still readable.
    """
    skill(root, "locked", header="name: locked\n")
    skill(root, "fine", header="name: fine\n")
    read_text = Path.read_text

    def refuse(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.parent.name == "locked":
            raise OSError("the file is locked")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)
    assert [one.name for one in scan()] == ["fine", "locked"]


@pytest.mark.asyncio
async def test_a_manifest_that_is_not_utf8_is_the_same_unreadable_file(
    root: Path,
) -> None:
    """**A file saved in the machine's own codepage, not as UTF-8.**

    The same person `bom_skill` is about, one step further: an editor on a
    Chinese Windows writes GBK, and GBK bytes are not text this build can read.
    It has to fail the way every other unreadable manifest does — the skill is
    *kept*, with an empty header — because `UnicodeDecodeError` is a
    `ValueError` and the `except OSError` guards around the read miss it: the
    raw codec error escaped `scan` into `catalogue`, and a single such file then
    failed `list_sources` for the whole hub, every turn, until somebody found it.
    """
    skill(root, "fine", header="name: fine\n")
    folder = root / "gbk"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_bytes("---\nname: 读\n---\n\n正文。\n".encode("gbk"))

    assert [one.name for one in scan()] == ["fine", "gbk"], "kept, not hidden"

    text, ok = await use("gbk")
    assert ok is False
    assert "cannot be read" in text and "UTF-8" in text, text


def test_an_empty_directory_is_no_skills(root: Path) -> None:
    """A fresh install has none, and that is not an error."""
    assert scan() == []
    assert scan(root / "nowhere") == []


def test_the_order_does_not_depend_on_the_filesystem(root: Path) -> None:
    for name in ("c", "a", "b"):
        skill(root, name, header=f"name: {name}\n")
    assert [one.name for one in scan()] == ["a", "b", "c"]


# --- finding one --------------------------------------------------------------


def test_a_skill_is_found_by_its_header_name_or_its_directory(root: Path) -> None:
    """Both are real: a skill that declares a name wants to be addressed by it,
    and one that does not is addressed by the folder somebody put it in."""
    skill(root, "the-folder", header="name: the-name\n")
    assert find("the-name").directory.name == "the-folder"
    assert find("the-folder").name == "the-name"
    assert find("nothing") is None


def test_the_document_says_where_the_skill_lives(root: Path) -> None:
    """A body names its own files relatively, and a reader has no way to know
    what they are relative to without this line."""
    skill(root, "one", body="Run `scripts/go.py`.")
    text = document(find("one"))
    assert text.startswith(f"> **Skills root:** `{root.resolve()}`")
    assert "Run `scripts/go.py`." in text


def test_the_header_is_not_part_of_the_body() -> None:
    header, body = split_frontmatter("---\nname: one\n---\n\nBody.\n")
    assert header == "name: one"
    assert body == "Body."


def test_a_file_with_no_fence_has_no_header() -> None:
    """An unterminated fence included: whatever follows it is body, and a body
    is never wrong to show."""
    assert frontmatter("# Just a document\n") == {}
    assert split_frontmatter("---\nname: one\n") == ("", "---\nname: one\n")


def test_a_header_that_will_not_parse_is_a_skill_with_no_declarations(
    root: Path,
) -> None:
    """The fallback is a decision, and this pins what it costs.

    `frontmatter` degrades to `{}` rather than raising, so the skill still
    exists and `scan` names it after its directory — but it loses its declared
    name, its description and every `requires`, which is why a damaged header
    also stops `readiness` reporting a missing prerequisite.  That is the
    fallback DESIGN.md §10.2 registers; this is the half a reader would
    otherwise have to infer.
    """
    skill(root, "one", header="metadata: [unclosed\n")
    (found,) = scan()

    assert found.name == "one", "named after its directory, since the header is gone"
    assert found.description == ""
    assert not found.requires.declared


# --- what a skill says it needs -----------------------------------------------
#
# A skill has no process and can still have a credential: a playbook that says
# "run scripts/search.py" is worth nothing if the script dies on a missing key,
# and the model that just read it is the last one to find out.

#: The declaration the published skills actually carry — baidu-search's own
#: header, verbatim, including the namespace it is filed under.
DECLARES = (
    "name: baidu-search\n"
    'metadata: { "openclaw": { "requires": { "bins": ["python3"], '
    '"env":["BAIDU_API_KEY"]}, "primaryEnv":"BAIDU_API_KEY" } }\n'
)


def test_a_skill_declares_what_it_needs(root: Path) -> None:
    skill(root, "baidu-search", header=DECLARES)
    needs = scan()[0].requires
    assert needs.env == ("BAIDU_API_KEY",)
    assert needs.bins == ("python3",)
    assert needs.primary == "BAIDU_API_KEY"


def test_a_skill_that_declares_nothing_is_the_common_case(root: Path) -> None:
    skill(root, "job-coding", header="name: job-coding\n")
    assert not scan()[0].requires.declared


def test_the_namespace_is_the_publishers_and_any_of_them_counts(root: Path) -> None:
    """A skill is meant to say the same thing to every host that reads it; a
    reader that insisted on one name would be silently blind to a renamed one."""
    skill(
        root,
        "one",
        header='name: one\nmetadata: {"somehost": {"requires": {"env": ["K"]}}}\n',
    )
    assert scan()[0].requires.env == ("K",)


def test_a_malformed_declaration_is_not_fatal(root: Path) -> None:
    skill(root, "one", header='name: one\nmetadata: "not a mapping"\n')
    assert not scan()[0].requires.declared


def test_a_declared_name_with_a_value_behind_it_is_met(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAIDU_API_KEY", "a-real-key")
    skill(
        root,
        "one",
        header="name: one\nmetadata: {h: {requires: {env: [BAIDU_API_KEY]}}}\n",
    )
    assert "are met" in readiness(scan()[0])


def test_a_declared_name_with_nothing_behind_it_is_reported(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BAIDU_API_KEY", raising=False)
    skill(
        root,
        "one",
        header=(
            "name: one\n"
            "metadata: {h: {requires: {env: [BAIDU_API_KEY]}, primaryEnv: BAIDU_API_KEY}}\n"
        ),
    )
    note = readiness(scan()[0])
    assert "BAIDU_API_KEY" in note and "not configured" in note
    assert "credstore set BAIDU_API_KEY" in note, "and what to do about it"


def test_a_config_value_that_resolved_to_nothing_is_not_a_value(root: Path) -> None:
    """The distinction the whole check exists to make.

    `resolve_secret` leaves an unresolvable `${VAR}` verbatim on purpose, so
    "the name is in the config" and "there is a key behind the name" are two
    different facts — and a skill told the first one would run and die.
    """
    skill(root, "one", header="name: one\nmetadata: {h: {requires: {env: [K]}}}\n")
    assert unresolved("${K}")
    assert unresolved("${OTHER_NAME}")
    assert not unresolved("bce-v3/ALTAK-real")
    assert "not configured" in readiness(scan()[0], {"K": "${K}"})
    assert "are met" in readiness(scan()[0], {"K": "a-real-key"})


def test_a_program_that_is_not_on_the_path_is_reported(root: Path) -> None:
    skill(
        root,
        "one",
        header="name: one\nmetadata: {h: {requires: {bins: [no-such-program-xyz]}}}\n",
    )
    assert "no-such-program-xyz" in readiness(scan()[0])


def test_a_missing_program_is_told_how_to_be_fixed_not_how_to_set_a_key(
    root: Path,
) -> None:
    """**A program is not a credential, and the fix is not the same sentence.**

    `skills:` carries `env:` and nothing that can put anything on `PATH`, so the
    old hint — chosen by `primaryEnv` alone, whatever kind of need was unmet — sent
    a reader whose only shortfall was `ffmpeg` to `credstore set` (or to
    `skills:`), a fix that cannot install anything.
    """
    skill(
        root,
        "one",
        header="name: one\nmetadata: {h: {requires: {bins: [no-such-program-xyz]}}}\n",
    )
    note = readiness(scan()[0])
    assert "Install" in note and "no-such-program-xyz" in note
    assert "credstore" not in note, "the key fix cannot install a program"
    assert "skills:" not in note


def test_a_configured_primary_is_not_blamed_for_a_missing_program(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same bug: a `primaryEnv` that *is* supplied while the
    binary is missing must not be named as the thing to go and fix — it is
    already fixed, and naming it sends the reader in a circle."""
    monkeypatch.setenv("SOME_KEY", "a-real-key")
    skill(
        root,
        "one",
        header=(
            "name: one\n"
            "metadata: {h: {requires: {env: [SOME_KEY], "
            "bins: [no-such-program-xyz]}, primaryEnv: SOME_KEY}}\n"
        ),
    )
    note = readiness(scan()[0])
    assert "no-such-program-xyz" in note and "Install" in note
    assert "credstore set SOME_KEY" not in note


def test_a_skill_that_needs_both_is_told_both_things(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One sentence per kind, emitted only for the kind that is actually short."""
    monkeypatch.delenv("SOME_KEY", raising=False)
    skill(
        root,
        "one",
        header=(
            "name: one\n"
            "metadata: {h: {requires: {env: [SOME_KEY], "
            "bins: [no-such-program-xyz]}, primaryEnv: SOME_KEY}}\n"
        ),
    )
    note = readiness(scan()[0])
    assert "credstore set SOME_KEY" in note and "Install" in note


def test_a_skill_that_declares_nothing_says_nothing(root: Path) -> None:
    """A line on every read is a line the model learns to skip."""
    skill(root, "one", header="name: one\n")
    assert readiness(scan()[0]) == ""


@pytest.mark.asyncio
async def test_use_says_whether_the_skill_can_actually_work(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BAIDU_API_KEY", raising=False)
    skill(
        root,
        "one",
        header=(
            "name: one\n"
            "metadata: {h: {requires: {env: [BAIDU_API_KEY]}, primaryEnv: BAIDU_API_KEY}}\n"
        ),
        body="Run `scripts/go.py`.",
    )
    before, ok = await use("one")
    after, _ = await use("one", environments={"one": {"BAIDU_API_KEY": "k"}})

    assert ok
    assert "not configured" in before
    assert "are met" in after
    # The warning rides with the document rather than trailing it, and the
    # playbook is intact either way.
    assert before.rstrip().endswith("Run `scripts/go.py`.")


# --- the tool's body ----------------------------------------------------------


@pytest.mark.asyncio
async def test_use_reads_one(root: Path) -> None:
    skill(root, "one", header="name: one\ndescription: d\n", body="Do it.")
    text, ok = await use("one")
    assert ok
    assert text.endswith("Do it.")


@pytest.mark.asyncio
async def test_a_bom_does_not_reach_the_model_as_part_of_the_playbook(
    root: Path,
) -> None:
    """The other half of reading a BOM-tolerant file: the mark itself is gone.

    `document` hands over the whole `SKILL.md`, header and all — the description
    lives there and a reader is meant to see it — so the one thing a BOM must not
    do is sit invisible at the front of the playbook the model follows.
    """
    bom_skill(root)
    text, ok = await use("real-name")
    assert ok
    assert "﻿" not in text
    assert text.rstrip().endswith("Run `scripts/go.py`.")


@pytest.mark.asyncio
async def test_use_names_what_is_installed_when_the_name_is_unknown(root: Path) -> None:
    """The answer has to be actionable: the model guessed a name, and the way
    out is the list of the ones that exist."""
    skill(root, "one", header="name: one\n")
    text, ok = await use("two")
    assert not ok
    assert "'two'" in text
    assert "one" in text


@pytest.mark.asyncio
async def test_use_says_so_when_nothing_is_installed() -> None:
    """No `skills/` at all is a fresh install, not an error."""
    text, ok = await use("anything")
    assert not ok
    assert "(none installed)" in text


@pytest.mark.asyncio
async def test_use_wants_a_name() -> None:
    text, ok = await use("")
    assert not ok
    assert "name" in text
