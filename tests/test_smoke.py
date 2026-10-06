"""Scaffold smoke test.

This tests no behaviour.  It exists so that `uv run pytest` fails loudly if the
build backend, the flat-layout package declaration or the dev dependency group
is misconfigured — the wiring is the thing under test.
"""

import slife2


def test_version_is_exposed() -> None:
    assert slife2.__version__ == "0.1.0"


def test_main_prints_and_returns_zero(capsys) -> None:
    assert slife2.main() == 0
    assert "slife2" in capsys.readouterr().out
