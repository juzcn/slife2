"""Where slife2 keeps its files.

The rule is short and the consequences are not: it decides which config is
read and where a database is written, so both halves are asserted — including
that a directory which merely *looks* busy is not mistaken for a checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slife2.paths import (
    DATA_ENV_VAR,
    agents_dir,
    data_dir,
    in_checkout,
    runtime_dir,
    skills_dir,
    turn_dbs,
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


def test_the_parts_live_under_the_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    assert runtime_dir() == tmp_path / "runtime"
    assert agents_dir() == tmp_path / "agents"
    assert skills_dir() == tmp_path / "skills"
    # ...and are made on demand, so a first run has somewhere to write.
    assert runtime_dir().is_dir()
    assert agents_dir().is_dir()
    assert skills_dir().is_dir()


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
