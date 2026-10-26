"""Where slife2 keeps its files.

The rule is short and the consequences are not: it decides which config is
read and where a database is written, so both halves are asserted — including
that a directory which merely *looks* busy is not mistaken for a checkout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from slife2.libs.host.paths import (
    DATA_ENV_VAR,
    RUNTIME_ENV_VAR,
    agent_dir,
    agents_dir,
    catalogue_db,
    data_dir,
    home,
    homes_dir,
    in_checkout,
    in_home,
    known_homes,
    register_home,
    runtime_dir,
    schedule_dbs,
    skills_dir,
    subagent_file,
    turn_dbs,
    work_dir,
)

pytestmark = pytest.mark.unit


def test_a_checkout_keeps_its_data_beside_itself(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """That is what makes the checked-in `slife2.yaml` the one in use."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "slife2"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DATA_ENV_VAR, raising=False)

    assert in_checkout() is True
    assert data_dir() == tmp_path


def test_a_subdirectory_of_a_checkout_finds_the_checkout_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A checkout is entered from a subdirectory too.**  Reading only the
    working directory sent `slife2/libs/...`'s data to `~/.slife2` with nothing
    said; the marker is looked for in every ancestor, and the data directory is
    the root, not the subdirectory."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "slife2"\n', encoding="utf-8"
    )
    nested = tmp_path / "slife2" / "libs" / "turn"
    nested.mkdir(parents=True)
    # A nearer manifest for another project does not hide the checkout.
    (tmp_path / "slife2" / "pyproject.toml").write_text(
        '[project]\nname = "a-nested-project"\n', encoding="utf-8"
    )
    monkeypatch.chdir(nested)
    monkeypatch.delenv(DATA_ENV_VAR, raising=False)

    assert in_checkout() is True
    assert data_dir() == tmp_path


def test_an_installation_keeps_its_data_under_home(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No checkout to sit in, and it must not depend on where it was started."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DATA_ENV_VAR, raising=False)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))

    assert in_checkout() is False
    assert data_dir() == tmp_path / "home" / ".slife2"


def test_another_projects_pyproject_is_not_a_checkout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker names *this* project.

    Mistaking a neighbouring project for ours would write a database into it.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "something-else"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert in_checkout() is False


def test_a_config_alone_does_not_make_a_checkout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A production folder somebody put a `slife2.yaml` in is still production."""
    (tmp_path / "slife2.yaml").write_text("default: ''\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert in_checkout() is False


def test_a_marker_that_cannot_be_read_is_not_a_missing_marker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A path that cannot be read is not a path that is not a checkout.**

    `in_checkout` swallowed every `OSError` into False, so a `pyproject.toml`
    that was there but unreadable — permissions, a racing delete — read as "not
    a checkout" and sent the whole data directory to `~/.slife2` with nothing
    said.  Only "no such file" now answers False.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "slife2"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    real_read_text = Path.read_text

    def denied(self: Path, *args, **kwargs):
        if self.name == "pyproject.toml":
            raise PermissionError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        in_checkout()


def test_the_environment_overrides_both(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One knob, and it wins — which is what tests rely on."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "slife2"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(DATA_ENV_VAR, str(elsewhere))

    assert data_dir() == elsewhere


def test_the_homes_parts_live_under_the_data_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config's neighbours: what a person wrote, and what an agent wrote."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    assert agents_dir() == tmp_path / "agents"
    assert skills_dir() == tmp_path / "skills"
    assert catalogue_db() == tmp_path / "agents" / "tools.db"
    # ...and are made on demand, so a first run has somewhere to write.
    assert agents_dir().is_dir()
    assert skills_dir().is_dir()


def test_the_runtime_is_the_machines_and_not_the_homes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The split, and it is the whole of why a second data directory works.**

    The daemons are one set per machine, so the record naming a pid, the lock two
    launchers race on and the claim on an agent name are facts about the machine.
    Under the data directory they were facts about *a directory* — so a second
    `--data-dir` reported a running stack it could not see, two launchers could
    cold-start one port, and a claim was lost to whoever looked in the other
    folder.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    monkeypatch.delenv(RUNTIME_ENV_VAR, raising=False)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))

    assert runtime_dir() == tmp_path / "home" / ".slife2" / "runtime"
    assert runtime_dir().is_dir()
    assert not (tmp_path / "runtime").exists(), "not under the data directory"


def test_a_call_is_served_in_the_home_it_names(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`in_home` is what a shared server puts around somebody else's work.

    A server is one process for every home, so the directory an agent's file
    goes in cannot be its own — it is the caller's, for as long as that call
    lasts and no longer: a home that outlived its call would be one the next
    caller in this process silently inherited.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"

    assert home() == tmp_path, "no call, so this process's own directory"
    with in_home(theirs):
        assert home() == theirs
        assert agent_dir("jack") == theirs / "agents" / "jack"
        assert catalogue_db() == theirs / "agents" / "tools.db"
        with in_home(""):
            assert home() == tmp_path, "a call that named no home is this one's"
        assert home() == theirs, "and the outer one is back"
    assert home() == tmp_path, "and so is this process's"
    assert not mine.exists()


def test_a_subagents_directory_is_inside_its_parents(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One directory is one agent's everything, and a subagent's is one level in.

    **The nesting is the relationship.**  A subagent belongs to whoever made it
    by being *underneath* it, so nothing has to keep a second record of who made
    whom — the directory says it, removing a subagent is removing a directory,
    and removing an agent takes its subagents with it.

    **And it is one container in, not one name in**: `subagents/` is where a
    subagent lives, so a subagent called `cabinet` is `subagents/cabinet` rather
    than the agent's own cabinet.  That is the layout's job and the reason the
    name needs no check against the names the agent already uses.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    jack = agent_dir("jack")
    researcher = agent_dir("jack", "researcher")

    assert researcher == jack / "subagents" / "researcher"
    assert researcher.is_dir(), "made on demand, like an agent's own"
    assert agent_dir("jack") == jack, "and the agent's own does not move"
    # The one name that would have collided: an agent's own cabinet.
    assert agent_dir("jack", "cabinet") != jack / "cabinet"


def test_a_subagent_stays_inside_its_parent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves of the id are sanitised, and `..` is a directory too.

    The second half comes out of a model's tool call rather than off a command
    line, and it decides a *path*: what is asserted is the invariant rather than
    a spelling — whatever the name reduces to, it lands inside `subagents/`, or it
    is refused for reducing to nothing.  The container is what makes that
    invariant cheap to hold: the name is the *last* segment of a path whose other
    segments are this build's, so a name cannot reach anything of the agent's own.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    jack = agent_dir("jack")

    for name in ("..", "./x", "../elsewhere", "a/b", "researcher"):
        try:
            made = agent_dir("jack", name)
        except ValueError:
            continue  # a name with nothing left in it is refused
        assert made.parent == jack / "subagents", f"{name!r} landed outside its parent"

    for name in ("..", ".", "/"):
        with pytest.raises(ValueError):
            agent_dir("jack", name)

    assert not (tmp_path / "agents" / "elsewhere").exists()


def test_a_fork_shares_its_makers_work_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-fork subagent owns its work; a fork is a copy and shares.

    The cabinet and media are work products, so where they land follows the
    conversation rather than the process: a persistent subagent has its own
    directory, a fork has its maker's.  The record inside the subagent's
    directory is what says which it is.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    jack = agent_dir("jack")
    researcher = agent_dir("jack", "researcher")
    record = subagent_file("jack", "researcher")

    record.write_text("fork: false\n", encoding="utf-8")
    assert work_dir("jack", "researcher") == researcher
    assert work_dir("jack") == jack

    record.write_text("fork: true\n", encoding="utf-8")
    assert work_dir("jack", "researcher") == jack

    record.unlink()
    assert work_dir("jack", "researcher") == researcher, (
        "a missing record is not a fork"
    )


def test_a_damaged_fork_record_is_named_not_silently_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A record that cannot be read is not a record that says "not a fork".**

    The directory is the subagent's own either way, which is the safe answer
    for a live subagent; what the warning adds is that a fork whose record was
    lost would otherwise write into its own directory with nothing said.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    researcher = agent_dir("jack", "researcher")
    subagent_file("jack", "researcher").write_text(
        "fork: [unclosed\n", encoding="utf-8"
    )

    with caplog.at_level("WARNING"):
        assert work_dir("jack", "researcher") == researcher

    assert any("not a fork" in one.getMessage() for one in caplog.records)


def test_an_empty_fork_record_is_named_too(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A file that parsed to nothing is damage, not absence.**  A zero-byte
    record loads as `None` and the old mapping check read it as "not a fork"
    without a word — the one arm of the read that did not name what it found.
    The answer stays the safe one; the warning is the trace."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    researcher = agent_dir("jack", "researcher")
    subagent_file("jack", "researcher").write_text("", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert work_dir("jack", "researcher") == researcher

    assert any("not a mapping" in one.getMessage() for one in caplog.records)


def test_a_home_is_found_by_the_ones_that_were_told_about_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`known_homes`: this process's own, and every one a client recorded.

    A shared daemon has to be able to *name* the homes it serves — the schedule
    plugin walks each one's ledgers, the WeChat plugin each one's logins — and
    nothing on the machine knows a data directory exists until somebody runs an
    agent in it.  So a client records its home, and the record outlives the
    client: a schedule still has to fire for a home nobody is sitting in.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path / "mine"))
    other = tmp_path / "other"
    (other / "agents" / "jack").mkdir(parents=True)

    assert known_homes() == [(tmp_path / "mine").resolve()], "nothing told yet"

    register_home(other)

    assert known_homes() == sorted([(tmp_path / "mine").resolve(), other.resolve()])
    # A home whose directory has gone is skipped rather than served.
    for path in sorted(other.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    other.rmdir()
    assert known_homes() == [(tmp_path / "mine").resolve()]


def test_a_home_is_registered_without_ever_truncating_the_record(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A client writes this file on every start, and daemons read it while up.**

    `register_home` rewrote the record in place — `write_text` empties the file
    first and the JSON arrives afterwards — and `known_homes` lets a `ValueError`
    out by design, because a record it cannot read is a file to look at and not a
    home that is not there.  So a daemon walking the registry in that window —
    the context store's lifespan pass, the scheduler's poll, the mesh's announce
    — read an empty file and failed its own *start-up* pass: the launcher
    reported that server FAILED and `slife2` exited 2, on a first start, over a
    file that was never damaged, only in flight.  Every other bookkeeping file
    here is written beside-and-renamed for exactly this reason
    (`slife2.libs.host.runtime._write_json_atomic`).

    What is asserted is the window itself, from the only two sides a reader can
    meet: the record is never opened for writing in place, and at the moment it
    is replaced it still holds the whole of what was there.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path / "mine"))
    other = tmp_path / "other"
    other.mkdir()
    register_home(other)
    record = next(homes_dir().glob("*.json"))
    before = record.read_text(encoding="utf-8")

    seen: list[str] = []
    real_open, real_replace = Path.open, os.replace

    def watching_open(self: Path, *args, **kwargs):
        mode = kwargs.get("mode", args[0] if args else "r")
        handle = real_open(self, *args, **kwargs)
        if self == record and "w" in mode:
            seen.append(self.read_text(encoding="utf-8"))
        return handle

    def watching_replace(src, dst):
        if Path(dst) == record:
            seen.append(Path(dst).read_text(encoding="utf-8"))
        return real_replace(src, dst)

    monkeypatch.setattr(Path, "open", watching_open)
    monkeypatch.setattr(os, "replace", watching_replace)
    register_home(other)

    assert seen == [before], (
        "the record was caught half-written, which is what a daemon reading the "
        f"registry at that moment sees: {seen!r}"
    )
    assert json.loads(record.read_text(encoding="utf-8"))["home"] == str(
        other.resolve()
    )


def test_the_walks_span_every_home(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two files a shared plugin finds its agents by, per home.

    Each is a walk of `agents/` because there is no registry of agents; with two
    homes there are two of each, and a daemon that walked only its own would
    never fire the other's schedules or resume its logins.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path / "mine"))
    other = tmp_path / "other"
    for root in (tmp_path / "mine", other):
        jack = root / "agents" / "jack"
        jack.mkdir(parents=True)
        (jack / "turns.db").write_bytes(b"")
        (jack / "schedule.db").write_bytes(b"")
    register_home(other)

    assert [path.parent.parent.parent.name for path in turn_dbs()] == ["mine", "other"]
    # A ledger comes with the home it is in, because the walker is the only
    # thing that knows it: the file name is the same in every home, and a
    # scheduler that has to fire one must do it *in* that home.
    found = schedule_dbs()
    assert [one.path.parent.parent.parent.name for one in found] == ["mine", "other"]
    assert [one.home.name for one in found] == ["mine", "other"]


def test_turn_dbs_finds_the_conversations_and_not_the_ledgers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A suffix is not a description**, and that is the whole of the test.

    `slife2-context`'s startup pass used to enumerate `agents/*/*.db` and hand
    every hit to `TurnStore`, which writes the turn schema into whatever it
    opens.  `schedule.db` sits in the same directory and matches that pattern, so
    the scheduler's ledger was being opened as a conversation store — two plugins
    on one SQLite file, against the one-writer rule the layout exists to keep.
    What is wanted is "a conversation", and only the middle of the name says so.

    **The two places the layout has, and no further.**  A subagent's
    conversation is inside the container (`agents/jack/subagents/researcher/`)
    and is found; a `turns.db` deeper than that — inside a subagent's cabinet,
    say — is not, because the walk enumerates the places the layout has rather
    than recursing into everything an agent keeps.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    jack = agents_dir() / "jack"
    jack.mkdir(parents=True)
    for name in ("turns.db", "schedule.db"):
        (jack / name).write_bytes(b"")
    (jack / "wechat.yaml").write_text("token: x\n", encoding="utf-8")
    researcher = jack / "subagents" / "researcher"
    (researcher / "cabinet").mkdir(parents=True)
    (researcher / "turns.db").write_bytes(b"")
    (researcher / "schedule.db").write_bytes(b"")
    (researcher / "cabinet" / "turns.db").write_bytes(b"")

    found = {path.relative_to(agents_dir()).as_posix() for path in turn_dbs()}

    assert found == {
        "jack/turns.db",
        "jack/subagents/researcher/turns.db",
    }
    assert "jack/schedule.db" not in found, "a ledger is not a conversation"
    assert "jack/subagents/researcher/cabinet/turns.db" not in found, (
        "and neither is a name"
    )
