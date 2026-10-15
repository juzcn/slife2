"""Where slife2 keeps its files.

The rule is short and the consequences are not: it decides which config is
read and where a database is written, so both halves are asserted — including
that a directory which merely *looks* busy is not mistaken for a checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slife2.libs.host.paths import (
    DATA_ENV_VAR,
    RUNTIME_ENV_VAR,
    a2a_inbound_paths,
    agent_dir,
    agents_dir,
    catalogue_db,
    data_dir,
    in_checkout,
    in_world,
    known_worlds,
    register_world,
    runtime_dir,
    schedule_dbs,
    skills_dir,
    turn_dbs,
    world,
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


def test_the_worlds_parts_live_under_the_data_directory(
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


def test_the_runtime_is_the_machines_and_not_the_worlds(
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


def test_a_call_is_served_in_the_world_it_names(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`in_world` is what a shared server puts around somebody else's work.

    A server is one process for every world, so the directory an agent's file
    goes in cannot be its own — it is the caller's, for as long as that call
    lasts and no longer: a world that outlived its call would be one the next
    caller in this process silently inherited.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"

    assert world() == tmp_path, "no call, so this process's own directory"
    with in_world(theirs):
        assert world() == theirs
        assert agent_dir("jack") == theirs / "agents" / "jack"
        assert catalogue_db() == theirs / "agents" / "tools.db"
        with in_world(""):
            assert world() == tmp_path, "a call that named no world is this one's"
        assert world() == theirs, "and the outer one is back"
    assert world() == tmp_path, "and so is this process's"
    assert not mine.exists()


def test_a_world_is_found_by_the_ones_that_were_told_about_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`known_worlds`: this process's own, and every one a client recorded.

    A shared daemon has to be able to *name* the worlds it serves — the schedule
    plugin walks each one's ledgers, the WeChat plugin each one's logins — and
    nothing on the machine knows a data directory exists until somebody runs an
    agent in it.  So a client records its world, and the record outlives the
    client: a schedule still has to fire for a world nobody is sitting in.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path / "mine"))
    other = tmp_path / "other"
    (other / "agents" / "jack").mkdir(parents=True)

    assert known_worlds() == [(tmp_path / "mine").resolve()], "nothing told yet"

    register_world(other)

    assert known_worlds() == sorted([(tmp_path / "mine").resolve(), other.resolve()])
    # A world whose directory has gone is skipped rather than served.
    for path in sorted(other.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    other.rmdir()
    assert known_worlds() == [(tmp_path / "mine").resolve()]


def test_the_walks_span_every_world(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The three files a shared plugin finds its agents by, per world.

    Each is a walk of `agents/` because there is no registry of agents; with two
    worlds there are two of each, and a daemon that walked only its own would
    never fire the other's schedules, resume its logins or answer its peers.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path / "mine"))
    other = tmp_path / "other"
    for root in (tmp_path / "mine", other):
        jack = root / "agents" / "jack"
        jack.mkdir(parents=True)
        (jack / "turns.db").write_bytes(b"")
        (jack / "schedule.db").write_bytes(b"")
        (jack / "a2a.yaml").write_text("tasks: []\n", encoding="utf-8")
    register_world(other)

    assert [path.parent.parent.parent.name for path in turn_dbs()] == ["mine", "other"]
    assert [path.parent.parent.parent.name for path in schedule_dbs()] == [
        "mine",
        "other",
    ]
    assert [path.parent.parent.parent.name for path in a2a_inbound_paths()] == [
        "mine",
        "other",
    ]


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
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    jack = agents_dir() / "jack"
    jack.mkdir(parents=True)
    for name in ("turns.db", "worker.turns.db", "schedule.db"):
        (jack / name).write_bytes(b"")
    (jack / "wechat.yaml").write_text("token: x\n", encoding="utf-8")

    found = {path.name for path in turn_dbs()}

    assert found == {"turns.db", "worker.turns.db"}
    assert "schedule.db" not in found, "the scheduler's ledger is not a conversation"
