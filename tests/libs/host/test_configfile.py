"""Writing `slife2.yaml`: the four properties an edit has to have.

It is the first thing in slife2 that writes the config, so each of them is a new
promise rather than a regression guard: comments and the rest of the file
survive, a `set` replaces the whole entry rather than patching a field, an edit
that changes nothing does not touch the file, and a file the loader would refuse
is not left on disk.
"""

from __future__ import annotations

import os

import pytest

from slife2.libs.host.config import ConfigError, load
from slife2.libs.host.configfile import (
    config_path,
    read_scalar,
    read_section,
    remove,
    set_enabled,
    set_scalar,
    update_entry,
    upsert,
)
from slife2.libs.host.paths import DATA_ENV_VAR

#: A config with everything an edit must not disturb: comments above, beside and
#: between entries, a long single-line value, Chinese text, and quote styles that
#: differ from line to line.
CONFIG = """\
# slife2.yaml — the explanation lives here, and a write must not eat it.
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    # The key is a reference; the value is in the credential store.
    api_key: ${SLIFE2_TEST_KEY:-none}
    models:
      - model: big
        name: 'Big one'
        context_window: 100000
        max_tokens: 4000
embeddings:
  providers:
    local:
      base_url: http://127.0.0.1:17347/v1
      model: big-emb
default: local/big

# ── Other people's servers ──────────────────────────────────────────────────
mcp:
  # Kept for a rainy day.  Not a description anybody wrote quickly.
  slow-one:
    command: npx
    args:
      - -y
      - some-server
    description: A very long single line of prose that ruamel would love to wrap at eighty columns and must not, because the file churns on every write if it does.
    enabled: false
  chinese:
    url: https://example.test/mcp
    description: 高德地图官方 MCP Server，提供地理编码、逆地理编码、IP定位、天气查询。

# ── Commands already on this machine ────────────────────────────────────────
cli:
  yt-dlp:
    command: yt-dlp
    description: Download video.
"""


def added(path, name: str, section: str = "mcp") -> str:
    """The lines a new entry occupies, so a test can read the entry it wrote.

    Split out because the interesting assertions are about what is *not* in the
    block — an empty `url`, an `enabled` the caller never mentioned — and a
    substring search over the whole file would find the next entry's.
    """
    text = path.read_text(encoding="utf-8")
    block = text.split(f"  {name}:", 1)[1]
    return block.split("\n# ", 1)[0]


#: The sections a file cannot leave out (SDD §4.1).  The texts below are about
#: comments and entries, and the writer judges every result through the loader
#: — so a file it edits has to be a file the loader accepts.
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


def write(tmp_path, text: str = CONFIG):
    """A config file at *tmp_path*, creating the directory it goes in.

    The `mkdir` is for the test that writes into the data directory, which the
    autouse fixture also gives a config; a test that writes a file of its own
    there must still be able to before anything has resolved the path.
    """
    path = tmp_path / "slife2.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- what a write must not cost ----------------------------------------------


def test_a_write_keeps_the_whole_file_around_it(tmp_path) -> None:
    """**The reason the writer is a document editor and not a dict dump.**

    `slife2.yaml` is mostly explanation: the file the operator reads is the
    comments, and a rewrite that reflows the entries around the one being
    changed is a rewrite of a file they wrote.  So what is asserted is not "the
    comments are still there" but the stronger thing — every line that was there
    is still there, in order, and they are all still one line each.
    """
    path = write(tmp_path)
    before = path.read_text(encoding="utf-8")

    upsert("mcp", "newcomer", {"url": "https://new.test/mcp"}, path=path)

    after = path.read_text(encoding="utf-8")
    original = before.splitlines()
    remaining = iter(after.splitlines())
    assert all(line in remaining for line in original), (
        "a line of the file the edit had nothing to do with was rewritten"
    )
    assert "  newcomer:\n    url: https://new.test/mcp\n" in after
    assert after.count("description: A very long single line") == 1
    assert "高德地图官方 MCP Server，提供地理编码" in after, "not \\uXXXX escaped"


def test_an_update_replaces_the_entry_rather_than_patching_it(tmp_path) -> None:
    """**The whole entry, so a `set` can say a field is gone.**

    `slow-one` is written again with no `description` and no `enabled`; both were
    facts about the *old* entry and neither survives.  This is what makes
    switching a server from `url` to `command` possible at all — a merge would
    keep the stale `url`, and the loader would then refuse the entry for naming
    two transports.  What an entry written fresh does keep is the comment in the
    section above it, because that belongs to the section and not to the fields.
    """
    path = write(tmp_path)

    upsert("mcp", "slow-one", {"command": "uvx", "args": ["other-server"]}, path=path)

    entry = load(path).mcp["slow-one"]
    assert entry.command == "uvx"
    assert entry.args == ("other-server",)
    assert entry.description == "", "a field the caller did not name is gone"
    assert entry.enabled is True, "and the switch it did not name is back to default"
    assert "# Kept for a rainy day." in path.read_text(encoding="utf-8")


def test_a_write_leaves_the_entry_below_it_its_comment(tmp_path) -> None:
    """**Replacing an entry is not only replacing the entry.**

    ruamel keeps "the comment above entry N+1" as the *trailing* comment of
    entry N's last value (the fact `_tail_of` documents), so writing a fresh
    mapping over N throws that block away with the old mapping and its
    `ca.items` — and the operator's explanation of the entry below disappears in
    a write that reported success.  `remove` does the surgery for the same
    hazard (`_drop_entry`); these are the other two ways to reach it, and both
    are exercised because they write through different callers.

    The entry being written *keeps its own* comment, which is the other half:
    that one belongs to the section and to a replacement, and only the block
    introducing the *next* entry is at stake.
    """
    path = tmp_path / "slife2.yaml"
    path.write_text(
        REQUIRED + "mcp:\n"
        "  # Describes slow-one, which is written again.\n"
        "  slow-one:\n"
        "    command: npx\n"
        "  # Describes filesystem, which stays.\n"
        "  filesystem:\n"
        "    command: fs\n"
        "  # Describes tail, which stays.\n"
        "  tail:\n"
        "    command: tail\n",
        encoding="utf-8",
    )

    # The first key, then a middle one: the block that introduces the entry
    # after each is in that entry's deepest last slot, and a replacement leaves
    # both behind.
    upsert("mcp", "slow-one", {"command": "uvx"}, path=path)
    update_entry(
        "mcp", "filesystem", lambda current: {**current, "args": ["-x"]}, path=path
    )

    after = path.read_text(encoding="utf-8")
    assert "Describes slow-one" in after, "the entry's own comment is the section's"
    assert "Describes filesystem" in after, "and the next entry keeps its own"
    assert after.index("Describes filesystem") < after.index("filesystem:")
    assert "Describes tail" in after
    assert after.index("Describes tail") < after.index("tail:")
    assert list(load(path).mcp) == ["slow-one", "filesystem", "tail"]


def test_a_field_that_says_nothing_is_not_written(tmp_path) -> None:
    """An empty value is four spellings of "no value", and none is a field.

    `url: ''` in particular reads as a claim that there *is* a URL, and the
    loader refuses an entry that names both transports — so an upsert that wrote
    its empty arguments through would make the next entry unloadable.
    """
    path = write(tmp_path)

    upsert(
        "mcp",
        "newcomer",
        {"command": "npx", "args": [], "env": {}, "url": "", "description": None},
        path=path,
    )

    block = added(path, "newcomer")
    assert "command: npx" in block
    assert "url" not in block, "an entry that names no url must not claim one"
    assert "env" not in block
    assert load(path).mcp["newcomer"].args == ()


def test_enabled_is_written_only_when_it_is_false(tmp_path) -> None:
    """Off is a decision and on is the default, so only one is a line.

    Which is the file's own convention — `enabled: false` appears four times in
    the checked-in config and `enabled: true` never — and it is what makes the
    switch's presence in a diff mean something.
    """
    path = write(tmp_path)

    upsert("mcp", "newcomer", {"url": "https://new.test/mcp"}, path=path)
    # Every `set` is the whole entry, so a second one restates what it keeps —
    # an update that named only `enabled` would replace the entry with one the
    # loader refuses for having no transport.
    entry = {"url": "https://new.test/mcp", "description": "d"}
    upsert("mcp", "newcomer", {**entry, "enabled": True}, path=path)
    assert "enabled" not in added(path, "newcomer")

    upsert("mcp", "newcomer", {**entry, "enabled": False}, path=path)
    assert "enabled: false" in added(path, "newcomer")


# --- removal and the switch --------------------------------------------------


def test_a_removal_takes_the_entry_and_says_whether_it_was_there(tmp_path) -> None:
    path = write(tmp_path)

    assert remove("mcp", "chinese", path=path) is True
    assert "chinese" not in path.read_text(encoding="utf-8")
    assert "slow-one" in path.read_text(encoding="utf-8")
    assert remove("mcp", "chinese", path=path) is False


def test_an_edit_that_changes_nothing_does_not_touch_the_file(tmp_path) -> None:
    """A reader who sees the file change should be able to believe something did.

    The removal of an entry that is not there is the case that makes this worth
    asserting: it is a normal answer to a normal request — two callers removing
    the same server — and it must not read as a write.

    **The inode is the assertion, and the text is the corroboration.**  Equal
    bytes are not proof that nothing was written: a rewrite that happened to
    render the same document would satisfy that alone, and the mtime — the
    obvious second witness — is only as sharp as the filesystem's clock, so on a
    coarse one both edits are stamped in the same tick and the claim quietly
    degrades to the text comparison above it.  Every write goes through
    `_swap`'s `mkstemp` and rename, so a write that happened is always a
    *different file*, at any granularity: the number the operating system gives
    the document is what a state-changing edit moves.
    """
    path = write(tmp_path)
    before = path.read_text(encoding="utf-8")
    stamp = path.stat()

    assert remove("mcp", "nothing-here", path=path) is False
    assert set_enabled("mcp", "nothing-here", False, path=path) is False

    assert path.read_text(encoding="utf-8") == before
    assert path.stat().st_ino == stamp.st_ino, "the file was rewritten"
    assert path.stat().st_mtime_ns == stamp.st_mtime_ns


def test_removing_the_last_entry_empties_the_section_and_keeps_its_explanation(
    tmp_path,
) -> None:
    """**The section is a place, and emptying it does not move it.**

    Found by a removal that failed every time the section had a comment inside
    it: ruamel renders a block mapping that has become empty with a comment still
    hanging off it as the key, that comment, then `{}` at column zero — which is
    not YAML, so the write was refused and rolled back.  And the obvious fix —
    drop the section — is the wrong one here, because `slife2.yaml` explains each
    section in a comment block *above* its key: a `mcp:` that disappeared would
    take the paragraph with it, and come back at the bottom of the file the next
    time an entry was added.

    So three things are asserted, and the third is the one that is easy to get
    wrong: the section stays where it was, the comment that explains the
    *section* stays above it, and the comment that described the *entry* goes
    with the entry — a comment about something that no longer exists is the one
    thing a comment must not be.
    """
    path = tmp_path / "slife2.yaml"
    path.write_text(
        REQUIRED + "# ── REST APIs ──\n"
        "rest-api:\n"
        "  # Documents the entry, and the entry is going.\n"
        "  registry:\n"
        "    spec: https://registry.example.test/openapi.json\n",
        encoding="utf-8",
    )

    assert remove("rest-api", "registry", path=path) is True

    after = path.read_text(encoding="utf-8")
    assert "rest-api: {}" in after, "the section is left, and left where it was"
    assert "# ── REST APIs ──" in after, "the section's own explanation"
    assert "Documents the entry" not in after, "and nothing about the entry"
    assert load(path).rest_apis == {}, "the file still reads"

    # And back: an entry written into the empty section lands in it, in place.
    upsert(
        "rest-api",
        "registry",
        {"spec": "https://registry.example.test/openapi.json"},
        path=path,
    )
    assert "registry" in load(path).rest_apis
    assert "rest-api: {}" not in path.read_text(encoding="utf-8")


def test_a_removal_leaves_every_other_entry_its_comment(tmp_path) -> None:
    """**A comment belongs to the entry under it, and a deletion moves two.**

    ruamel keeps "the comment above an entry" as the *trailing* comment of the
    entry before it (for the first entry, as the mapping's pending comment), so
    deleting an entry from the middle of a section moves two comment blocks and
    only one of the moves is right: the survivor's comment is destroyed along
    with the deleted entry, and the deleted entry's own comment slides up over
    the survivor.  Left alone, the file ends up with a comment describing
    something that no longer exists attached to the one entry it is least true
    of — the failure the test above names for a section's *last* entry, here for
    a middle one.

    Both positions are exercised, because they are two different ruamel slots —
    the first key's comment lives on the mapping, every later one on its
    predecessor — and a fix for one leaves the other wrong.
    """
    path = tmp_path / "slife2.yaml"
    path.write_text(
        REQUIRED + "mcp:\n"
        "  # Describes slow-one, and slow-one is going.\n"
        "  slow-one:\n"
        "    command: npx\n"
        "  # Describes filesystem, which stays.\n"
        "  filesystem:\n"
        "    command: fs\n"
        "  # Describes tail, which stays.\n"
        "  tail:\n"
        "    command: tail\n",
        encoding="utf-8",
    )

    # The first key: its comment rides on the mapping, the survivor's on it.
    assert remove("mcp", "slow-one", path=path) is True

    after = path.read_text(encoding="utf-8")
    assert "slow-one" not in after
    assert "Describes slow-one" not in after, "its comment goes with it"
    assert "Describes filesystem" in after, "the survivor keeps its own"
    assert after.index("Describes filesystem") < after.index("filesystem:"), (
        "and it is still above the entry it describes, not stranded"
    )
    assert list(load(path).mcp) == ["filesystem", "tail"]

    # A middle key: the same two moves, in ruamel's other slot.
    assert remove("mcp", "filesystem", path=path) is True

    after = path.read_text(encoding="utf-8")
    assert "filesystem" not in after
    assert "Describes filesystem" not in after
    assert "Describes tail" in after
    assert after.index("Describes tail") < after.index("tail:")
    assert list(load(path).mcp) == ["tail"]


def test_an_entry_that_ends_on_a_sequence_keeps_the_next_comment(tmp_path) -> None:
    """**The shape every entry in `slife2.yaml` actually has, and the walk that
    stopped one node too early.**

    `_tail_of` used to descend only while a value was a mapping, and the
    trailing block is not stored on the key when that key holds a *block
    sequence*: the parser keeps "the comment above the next entry" on the last
    *item* of the sequence (`ca.items[i][0]`, which is not the slot a mapping
    uses either).  So `slow-one`'s `args:` made the walk stop at `args`,
    `ca.items["args"]` was empty, nothing was taken — and the block went out with
    the sequence when the entry was replaced or deleted, in a write that
    reported success.

    This is not a corner: `args:` ends every `mcp:` entry in the file this
    module edits, `models:` ends every `providers:` one, and `model_add` writes
    through exactly that path.  All three operations are exercised here because
    each one reaches the walk from a different place — `_swap_entry` takes it
    before writing a fresh mapping, `update_entry` writes through that same
    caller, and `_drop_entry` takes it before the `del` that would free it.
    """
    path = tmp_path / "slife2.yaml"
    sequence_ending = (
        REQUIRED + "mcp:\n"
        "  # Describes slow-one, which is written again.\n"
        "  slow-one:\n"
        "    command: npx\n"
        "    args:\n"
        "      - -y\n"
        "      - some-server\n"
        "  # Describes filesystem, which stays.\n"
        "  filesystem:\n"
        "    command: fs\n"
        "    args:\n"
        "      - -x\n"
        "  # Describes tail, which stays.\n"
        "  tail:\n"
        "    command: tail\n"
        "    args:\n"
        "      - -f\n"
    )

    def kept(entry: str, after: str) -> None:
        """The block describes *entry*, and prints above the entry it describes."""
        text = path.read_text(encoding="utf-8")
        assert after in text, f"the block introducing {entry} was destroyed"
        assert text.index(after) < text.index(f"{entry}:"), "and it is not stranded"

    # `upsert` replaces the first entry outright, taking the survivor off it
    # first — which is the block "Describes filesystem" lives in.
    path.write_text(sequence_ending, encoding="utf-8")
    upsert("mcp", "slow-one", {"command": "uvx", "args": ["other-server"]}, path=path)
    kept("filesystem", "Describes filesystem")
    kept("tail", "Describes tail")

    # `update_entry` goes through the same caller with a different edit.
    path.write_text(sequence_ending, encoding="utf-8")
    update_entry(
        "mcp", "slow-one", lambda current: {**current, "args": ["-z"]}, path=path
    )
    kept("filesystem", "Describes filesystem")

    # `remove` deletes the middle entry, so the block that has to be re-hung is
    # the one introducing `tail` — and its holder is the doomed entry's own
    # sequence, which the `del` frees.
    path.write_text(sequence_ending, encoding="utf-8")
    assert remove("mcp", "filesystem", path=path) is True
    text = path.read_text(encoding="utf-8")
    assert "Describes filesystem" not in text, "its own comment goes with it"
    kept("tail", "Describes tail")
    assert list(load(path).mcp) == ["slow-one", "tail"]

    # And the first key, where the survivor is re-hung above the new first entry.
    path.write_text(sequence_ending, encoding="utf-8")
    assert remove("mcp", "slow-one", path=path) is True
    kept("filesystem", "Describes filesystem")
    assert list(load(path).mcp) == ["filesystem", "tail"]


def test_the_switch_is_written_where_the_entry_is(tmp_path) -> None:
    """`slife2.libs.host.config` reads `enabled` off the entry, so that is where it goes."""
    path = write(tmp_path)

    assert set_enabled("mcp", "chinese", False, path=path) is True
    assert load(path).mcp["chinese"].enabled is False

    assert set_enabled("mcp", "chinese", True, path=path) is True
    assert load(path).mcp["chinese"].enabled is True
    assert "enabled" not in added(path, "chinese")


# --- the two refusals --------------------------------------------------------


def test_a_file_the_loader_refuses_is_not_left_on_disk(tmp_path) -> None:
    """**The write is validated by the reader, and rolled back if it fails.**

    An entry with neither `command` nor `url` is one `slife2.libs.host.config` refuses —
    so the alternative to this test is an edit that reports success and takes
    the system down at the *next* start, in a process that never saw the edit.
    What must survive is not only "the call raised" but "the file is the file it
    was", because a half-applied config is the one outcome worse than neither.
    """
    path = write(tmp_path)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        upsert("mcp", "broken", {"description": "neither transport"}, path=path)

    assert "needs `command`" in str(raised.value)
    assert path.read_text(encoding="utf-8") == before


def test_a_section_edit_refuses_when_there_is_no_config_to_edit(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A missing file is refused, not created.**

    A home runs on its config and `load` refuses one without it (SDD §4.1), so
    a write that created the file would invent the home's whole description from
    the single section being edited — an edit that reported success over a home
    its operator never described.  The message has to say what to do instead,
    and it is the loader's own sentence.
    """
    missing = isolated_runtime / "slife2.yaml"
    missing.unlink()
    assert not missing.exists()

    with pytest.raises(ConfigError) as raised:
        upsert("mcp", "newcomer", {"url": "https://new.test/mcp"})

    assert "no slife2.yaml" in str(raised.value)
    assert not missing.exists(), "the refusal must not be a file it created"


def test_the_path_is_the_one_the_loader_reads(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`config_path` and `slife2.libs.host.config` must not disagree about the file.

    `--data-dir` is the knob for where an installation lives, and a writer that
    resolved it differently would edit a file nothing reads.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(isolated_runtime))
    write(isolated_runtime)

    assert config_path() == isolated_runtime / "slife2.yaml"


# --- the top-level scalar, and the locked update -----------------------------


def test_a_scalar_is_written_and_still_judged_by_the_caller(tmp_path) -> None:
    """`default:` is not a section, so it needs a writer of its own.

    And `load` accepts any string there — only `Config.resolve` knows a
    reference from a typo — which is why the caller passes its own judge and a
    write naming no model is rolled back like any other the reader refuses.
    """
    path = write(tmp_path)

    set_scalar("default", "local/big", path=path)
    assert read_scalar("default", path=path) == "local/big"
    assert load(path).default == "local/big"
    # Everything the one assignment had nothing to do with is untouched.
    assert "# slife2.yaml — the explanation lives here" in path.read_text(
        encoding="utf-8"
    )

    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        set_scalar(
            "default",
            "nobody/knows",
            check=lambda config: config.resolve("nobody/knows"),
            path=path,
        )
    assert path.read_text(encoding="utf-8") == before, "rolled back"


def test_a_scalar_that_is_not_there_reads_as_the_default(tmp_path) -> None:
    path = write(tmp_path)
    assert read_scalar("nobody-wrote-this", default="?", path=path) == "?"


def test_an_update_reads_and_writes_inside_one_lock(tmp_path) -> None:
    """**The whole point of `update_entry`.**  A list is read *and* written.

    `upsert` writes exactly what it is handed, so a caller that read an entry's
    list and wrote it back through `upsert` would be a read-modify-write with
    the lock only on the write — and the cost of the interleave is silent: two
    callers add one item to one entry, and one of them is gone.
    """
    path = write(tmp_path)

    def add(current: dict) -> dict:
        seen = [one for one in current.get("args", []) if isinstance(one, str)]
        return {**current, "args": [*seen, "extra"]}

    assert update_entry("mcp", "slow-one", add, path=path) is True

    entry = load(path).mcp["slow-one"]
    assert entry.args == ("-y", "some-server", "extra")
    assert entry.command == "npx", "the caller only touched `args`"
    assert entry.enabled is False, "and only the list moved"


def test_an_update_that_answers_none_deletes_the_entry(tmp_path) -> None:
    path = write(tmp_path)

    assert update_entry("mcp", "slow-one", lambda current: None, path=path) is True
    assert "slow-one" not in load(path).mcp
    assert "chinese" in load(path).mcp, "its neighbours stay"


def test_an_update_of_something_that_is_not_there_says_so(tmp_path) -> None:
    path = write(tmp_path)
    assert (
        update_entry("mcp", "ghost", lambda current: {"url": "u"}, path=path) is False
    )
    assert load(path).mcp["ghost"].url == "u"


# --- a value that cannot be read is not a value that is absent ----------------
#
# SDD Appendix C's first deformation, at the one place in this module where it
# is data loss rather than a wrong answer: every path below used to answer
# "there is nothing here" about a value it could not read, and then — on the
# write paths — replace it with the empty one it had just invented.
#
# Both fixtures are **valid YAML**, which is the point: a section that is a
# string is a mistake a person makes in a file they hand-edit, and it is not a
# file ruamel refuses.  Written as `mcp: "oops"` above the entries it would not
# parse at all, and the test would be measuring the parser rather than the
# reader.

#: `mcp:` as a scalar, with nothing under it.
BROKEN_SECTION = """\
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    api_key: ${SLIFE2_TEST_KEY:-none}
    models:
      - model: big
        name: 'Big one'
default: local/big

mcp: "oops"
"""

#: And one entry inside a section that is a mapping.
BROKEN_ENTRY = """\
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    api_key: ${SLIFE2_TEST_KEY:-none}
    models:
      - model: big
        name: 'Big one'
default: local/big

mcp:
  chinese: "oops"
"""


def test_a_section_that_is_not_a_mapping_is_refused_not_answered_empty(tmp_path):
    """`mcp: "oops"` is a mistake, and a listing that answers `{}` buries it.

    `slife2.libs.host.config._mapping` refuses the same shape at load, so answering empty
    here was this reader disagreeing with the one that decides whether the
    system starts — and disagreeing quietly, until the next start.
    """
    path = write(tmp_path, BROKEN_SECTION)

    with pytest.raises(ConfigError) as raised:
        read_section("mcp", path=path)
    assert "`mcp:`" in str(raised.value)

    assert read_section("a2a", path=path) == {}, "an absent section is still empty"


def test_an_entry_that_is_not_a_mapping_is_refused_not_dropped(tmp_path):
    path = write(tmp_path, BROKEN_ENTRY)

    with pytest.raises(ConfigError) as raised:
        read_section("mcp", path=path)
    assert "`mcp.chinese:`" in str(raised.value)


def test_a_write_will_not_replace_a_section_it_cannot_read(tmp_path):
    path = write(tmp_path, BROKEN_SECTION)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError):
        upsert("mcp", "new", {"command": "x"}, path=path)

    assert path.read_text(encoding="utf-8") == before, "the operator's value stays"


def test_the_switch_will_not_replace_an_entry_it_cannot_read(tmp_path):
    path = write(tmp_path, BROKEN_ENTRY)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError):
        set_enabled("mcp", "chinese", False, path=path)

    assert path.read_text(encoding="utf-8") == before


def test_an_update_will_not_replace_an_entry_it_cannot_read(tmp_path):
    path = write(tmp_path, BROKEN_ENTRY)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError):
        update_entry("mcp", "chinese", lambda current: {"url": "u"}, path=path)

    assert path.read_text(encoding="utf-8") == before


def test_a_mode_that_could_not_be_kept_is_said_rather_than_swallowed(
    tmp_path, monkeypatch, caplog
) -> None:
    """**SDD §11.8 states the swap as preserving the file's mode**, and the failure
    was `except OSError: pass`: the config came back at `mkstemp`'s `0600`,
    which tightens a file somebody had made group-readable — another user of
    the machine loses the config and nothing says why.
    """
    path = write(tmp_path)

    def refuse(*args, **kwargs):
        raise OSError("not permitted")

    monkeypatch.setattr(os, "chmod", refuse)
    with caplog.at_level("WARNING"):
        upsert("mcp", "new", {"command": "x"}, path=path)

    assert "the file's mode was not kept" in caplog.text
    assert load(path).mcp["new"].command == "x", "and the write still happened"
