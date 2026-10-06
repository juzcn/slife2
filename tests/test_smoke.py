"""The console entry point.

`main()` now starts a Textual app, so it cannot simply be called and inspected —
the app would take over the terminal.  What is worth testing is the part that
runs *before* the app: argument handling and config failure.  Both must produce
an exit code and a message rather than a traceback, because at that point
nothing has started yet and the fix is in the file being named.
"""

from __future__ import annotations

import pytest

import slife2
from slife2 import _parse_args, main

pytestmark = pytest.mark.unit


def test_version_is_exposed() -> None:
    assert slife2.__version__ == "0.1.0"


def test_package_import_does_not_pull_in_textual() -> None:
    """`slife2.config` and the servers must be importable without a TUI stack.

    The import is deferred inside `main` for exactly this reason: a server
    process should not pay for Textual, and CI installs the wheel with no
    optional extras.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import slife2, sys; print('textual' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


def test_a_missing_config_names_the_file(tmp_path, capsys) -> None:
    """A config the user asked for and did not get is an error, not a default."""
    code = main(["--config", str(tmp_path / "absent.yaml")])
    assert code == 2
    assert "absent.yaml" in capsys.readouterr().out


def test_a_malformed_config_is_reported_not_raised(tmp_path, capsys) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("agent: [unclosed\n", encoding="utf-8")
    code = main(["--config", str(path)])
    assert code == 2
    assert "broken.yaml" in capsys.readouterr().out


def test_help_exits_cleanly(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    # The help names the subcommands, since they are the part of the CLI nobody
    # guesses at.
    assert "status" in out
    assert "down" in out


def test_agent_defaults_to_slife2() -> None:
    assert _parse_args([]).agent == "slife2"
    assert _parse_args(["--agent", "jack"]).agent == "jack"


def test_a_leading_subcommand_is_taken_as_one() -> None:
    """`slife2 status` is a command; `slife2 --agent jack` is the TUI.

    The subcommand is only recognised in the first position, which is what lets
    the common invocation stay a plain flag call.
    """
    assert _parse_args(["status"]).command == "status"
    assert _parse_args(["down"]).command == "down"
    assert _parse_args([]).command == "run"
    assert _parse_args(["--agent", "jack"]).command == "run"
    assert _parse_args(["status", "--config", "x.yaml"]).command == "status"
