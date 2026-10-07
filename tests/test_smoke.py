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
from slife2 import _parse_args, main, tui_url
from slife2.config import default_config

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


def test_a_malformed_config_is_reported_not_raised(tmp_path) -> None:
    """A file the user wrote and got wrong is worth naming once.

    Note what is *not* here any more: a test for "the config file you named does
    not exist".  There is no such case — the command line names a folder, and a
    folder with no config in it is a fresh installation rather than a mistake.
    """
    (tmp_path / "slife2.yaml").write_text("agent: [unclosed\n", encoding="utf-8")
    code = main(["--data-dir", str(tmp_path)])
    assert code == 2


def test_help_exits_cleanly(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    # The help names the subcommands, since they are the part of the CLI nobody
    # guesses at.
    assert "status" in out
    assert "down" in out


def test_the_tui_connects_to_the_agent_server() -> None:
    """Not to a model server — the two are one word apart in the config.

    `agent.server` and a provider's `server` are both "the server", and swapping
    them fails at startup with "not slife2-agent": a model server answers
    `stream_chat` and has never heard of `run_turn`.
    """
    config = default_config()
    assert tui_url(config) == config.agent.server.url
    assert tui_url(config) != config.url_for(config.default)


def test_the_tui_url_can_be_overridden() -> None:
    assert tui_url(default_config(), "http://elsewhere/mcp") == "http://elsewhere/mcp"


def test_agent_defaults_to_slife2() -> None:
    assert _parse_args([]).agent == "slife2"
    assert _parse_args(["--agent", "jack"]).agent == "jack"


def test_a_subcommand_is_recognised_wherever_it_is_written() -> None:
    """`slife2 status` is a command; `slife2 --agent jack` is the TUI.

    The command is an ordinary positional, so the flags and the command can be
    written in either order.  It used to be taken only from the first position,
    which made `slife2 --data-dir D status` a usage error — and `--data-dir`
    matters most on `status` and `down`, which are what you run when the data
    directory is not the default one.
    """
    assert _parse_args(["status"]).command == "status"
    assert _parse_args(["down"]).command == "down"
    assert _parse_args([]).command == "run"
    assert _parse_args(["--agent", "jack"]).command == "run"
    assert _parse_args(["status", "--data-dir", "x"]).command == "status"
    assert _parse_args(["--data-dir", "x", "status"]).command == "status"
    assert _parse_args(["--data-dir", "x"]).command == "run"


def test_an_unknown_command_is_a_usage_error() -> None:
    """Rather than a word silently treated as a flag, or as the TUI.

    Argparse owns the choice, so it names the commands that do exist — which is
    the whole reason the subcommand is not picked out by hand any more.
    """
    with pytest.raises(SystemExit):
        _parse_args(["frobnicate"])
