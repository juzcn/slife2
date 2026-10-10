"""Writing `slife2.yaml`: the four properties an edit has to have.

It is the first thing in slife2 that writes the config, so each of them is a new
promise rather than a regression guard: comments and the rest of the file
survive, a field left out keeps its value, an edit that changes nothing does not
touch the file, and a file the loader would refuse is not left on disk.
"""

from __future__ import annotations

import pytest

from slife2.config import ConfigError, load
from slife2.configfile import config_path, remove, set_enabled, upsert
from slife2.paths import DATA_ENV_VAR

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
default: local/big

# ── Other people's servers ──────────────────────────────────────────────────
tools:
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


def added(path, name: str, section: str = "tools") -> str:
    """The lines a new entry occupies, so a test can read the entry it wrote.

    Split out because the interesting assertions are about what is *not* in the
    block — an empty `url`, an `enabled` the caller never mentioned — and a
    substring search over the whole file would find the next entry's.
    """
    text = path.read_text(encoding="utf-8")
    block = text.split(f"  {name}:", 1)[1]
    return block.split("\n# ", 1)[0]


def write(tmp_path, text: str = CONFIG):
    """A config file at *tmp_path*, creating the directory it goes in.

    The `mkdir` is for the test that writes into the data directory, which the
    autouse fixture only *names*: it is `slife2.paths.data_dir` that creates one,
    and a test that writes before anything has resolved it would otherwise be
    the thing that creates it — with a traceback instead of a config.
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

    upsert("tools", "newcomer", {"url": "https://new.test/mcp"}, path=path)

    after = path.read_text(encoding="utf-8")
    original = before.splitlines()
    remaining = iter(after.splitlines())
    assert all(line in remaining for line in original), (
        "a line of the file the edit had nothing to do with was rewritten"
    )
    assert "  newcomer:\n    url: https://new.test/mcp\n" in after
    assert after.count("description: A very long single line") == 1
    assert "高德地图官方 MCP Server，提供地理编码" in after, "not \\uXXXX escaped"


def test_an_update_moves_only_what_it_was_given(tmp_path) -> None:
    """Merge and not replace, which is what makes an upsert an update.

    `slow-one` is written again with no `description` and no `enabled`; both are
    facts the caller did not mention, and an entry that lost them would turn
    "set this field" into "replace this entry" without saying so.
    """
    path = write(tmp_path)

    upsert("tools", "slow-one", {"command": "uvx", "args": ["other-server"]}, path=path)

    entry = load(path).tools["slow-one"]
    assert entry.command == "uvx"
    assert entry.args == ("other-server",)
    assert entry.description.startswith("A very long single line")
    assert entry.enabled is False, "the switch the caller never mentioned"


def test_a_field_that_says_nothing_is_not_written(tmp_path) -> None:
    """An empty value is four spellings of "no value", and none is a field.

    `url: ''` in particular reads as a claim that there *is* a URL, and the
    loader refuses an entry that names both transports — so an upsert that wrote
    its empty arguments through would make the next entry unloadable.
    """
    path = write(tmp_path)

    upsert(
        "tools",
        "newcomer",
        {"command": "npx", "args": [], "env": {}, "url": "", "description": None},
        path=path,
    )

    block = added(path, "newcomer")
    assert "command: npx" in block
    assert "url" not in block, "an entry that names no url must not claim one"
    assert "env" not in block
    assert load(path).tools["newcomer"].args == ()


def test_enabled_is_written_only_when_it_is_false(tmp_path) -> None:
    """Off is a decision and on is the default, so only one is a line.

    Which is the file's own convention — `enabled: false` appears four times in
    the checked-in config and `enabled: true` never — and it is what makes the
    switch's presence in a diff mean something.
    """
    path = write(tmp_path)

    upsert("tools", "newcomer", {"url": "https://new.test/mcp"}, path=path)
    upsert("tools", "newcomer", {"description": "d", "enabled": True}, path=path)
    assert "enabled" not in added(path, "newcomer")

    upsert("tools", "newcomer", {"description": "d", "enabled": False}, path=path)
    assert "enabled: false" in added(path, "newcomer")


# --- removal and the switch --------------------------------------------------


def test_a_removal_takes_the_entry_and_says_whether_it_was_there(tmp_path) -> None:
    path = write(tmp_path)

    assert remove("tools", "chinese", path=path) is True
    assert "chinese" not in path.read_text(encoding="utf-8")
    assert "slow-one" in path.read_text(encoding="utf-8")
    assert remove("tools", "chinese", path=path) is False


def test_an_edit_that_changes_nothing_does_not_touch_the_file(tmp_path) -> None:
    """A reader who sees the mtime move should be able to believe something did.

    The removal of an entry that is not there is the case that makes this worth
    asserting: it is a normal answer to a normal request — two callers removing
    the same server — and it must not read as a write.
    """
    path = write(tmp_path)
    before = path.read_text(encoding="utf-8")
    stamp = path.stat().st_mtime_ns

    assert remove("tools", "nothing-here", path=path) is False
    assert set_enabled("tools", "nothing-here", False, path=path) is False

    assert path.read_text(encoding="utf-8") == before
    assert path.stat().st_mtime_ns == stamp


def test_removing_the_last_entry_empties_the_section_and_keeps_its_explanation(
    tmp_path,
) -> None:
    """**The section is a place, and emptying it does not move it.**

    Found by a removal that failed every time the section had a comment inside
    it: ruamel renders a block mapping that has become empty with a comment still
    hanging off it as the key, that comment, then `{}` at column zero — which is
    not YAML, so the write was refused and rolled back.  And the obvious fix —
    drop the section — is the wrong one here, because `slife2.yaml` explains each
    section in a comment block *above* its key: a `tools:` that disappeared would
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
        "# ── REST APIs ──\n"
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


def test_the_switch_is_written_where_the_entry_is(tmp_path) -> None:
    """`slife2.config` reads `enabled` off the entry, so that is where it goes."""
    path = write(tmp_path)

    assert set_enabled("tools", "chinese", False, path=path) is True
    assert load(path).tools["chinese"].enabled is False

    assert set_enabled("tools", "chinese", True, path=path) is True
    assert load(path).tools["chinese"].enabled is True
    assert "enabled" not in added(path, "chinese")


# --- the two refusals --------------------------------------------------------


def test_a_file_the_loader_refuses_is_not_left_on_disk(tmp_path) -> None:
    """**The write is validated by the reader, and rolled back if it fails.**

    An entry with neither `command` nor `url` is one `slife2.config` refuses —
    so the alternative to this test is an edit that reports success and takes
    the system down at the *next* start, in a process that never saw the edit.
    What must survive is not only "the call raised" but "the file is the file it
    was", because a half-applied config is the one outcome worse than neither.
    """
    path = write(tmp_path)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigError) as raised:
        upsert("tools", "broken", {"description": "neither transport"}, path=path)

    assert "needs `command`" in str(raised.value)
    assert path.read_text(encoding="utf-8") == before


def test_a_section_edit_refuses_when_there_is_no_config_to_edit(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A missing file is refused, not created.**

    slife2 runs with no config at all — one built-in provider — so a write that
    created the file would replace that fallback with a file holding only the
    section being edited: the next start would have no models, over an edit that
    reported success.  The message has to say what to do instead.
    """
    missing = isolated_runtime / "slife2.yaml"
    assert not missing.exists()

    with pytest.raises(ConfigError) as raised:
        upsert("tools", "newcomer", {"url": "https://new.test/mcp"})

    assert "no slife2.yaml" in str(raised.value)
    assert not missing.exists(), "the refusal must not be a file it created"


def test_the_path_is_the_one_the_loader_reads(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`config_path` and `slife2.config` must not disagree about the file.

    `--data-dir` is the knob for where an installation lives, and a writer that
    resolved it differently would edit a file nothing reads.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(isolated_runtime))
    write(isolated_runtime)

    assert config_path() == isolated_runtime / "slife2.yaml"
