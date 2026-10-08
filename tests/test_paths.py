"""Where slife2 keeps its files.

The rule is short and the consequences are not: it decides which config is
read and where a database is written, so both halves are asserted — including
that a directory which merely *looks* busy is not mistaken for a checkout.
"""

from __future__ import annotations

import pytest

from slife2.paths import DATA_ENV_VAR, data_dir, db_dir, in_checkout, runtime_dir

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
    assert db_dir() == tmp_path / "slife2.db"
    # ...and are made on demand, so a first run has somewhere to write.
    assert runtime_dir().is_dir()
    assert db_dir().is_dir()
