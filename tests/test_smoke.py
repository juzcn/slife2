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
from slife2 import _parse_args, main, mesh_url, tui_model, tui_url
from slife2.config import A2A_SERVER_NAME, A2ASettings, default_config, load

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
    `stream_chat` and has never heard of `send_message`.
    """
    config = default_config()
    assert tui_url(config) == config.agent.server.url
    assert tui_url(config) != config.url_for(config.default)


def test_the_tui_url_can_be_overridden() -> None:
    assert tui_url(default_config(), "http://elsewhere/mcp") == "http://elsewhere/mcp"


def test_the_mesh_url_is_the_key_the_address_is_filed_under() -> None:
    """`a2a`, and not `slife2-a2a` — two names one word apart.

    `servers:` is keyed by the short name every plugin's module calls its
    `CONFIG_KEY`, and `slife2-a2a` is what the *server* answers to (the identity
    `open_server(name=…)` checks, from `slife2.config.SERVER_MODULES`).  Looking
    up the identity in the key table is a `ConfigError` before the TUI starts,
    which is what this caught: every test of the window passed an address in by
    hand, so nothing exercised the line that reads one out of the file.
    """
    config = default_config()
    object.__setattr__(config, "a2a", A2ASettings(configured=True))

    assert mesh_url(config) == config.server("a2a").url
    assert mesh_url(config) != A2A_SERVER_NAME


def test_a_machine_with_no_mesh_section_has_no_mesh_url() -> None:
    """`""`, so the window holds nothing — rather than holding a call to be told
    every two seconds that this machine is not on the mesh."""
    assert mesh_url(default_config()) == ""
    assert not default_config().a2a.configured


def test_the_take_down_is_what_stops_the_broker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The last instance out takes mosquitto with it.

    The broker is the one daemon the launcher does not start — the a2a plugin
    brings one up when the mesh is wanted, and records what it started — so it is
    not in `specs` and `launcher.stop` never sees it.  This is where its lifetime
    is tied to the stack's instead, and it is the same place for both ways a
    stack goes away: `slife2 down`, and the last client exiting.

    The broker's own half is stubbed (`tests/test_a2a.py` has it: the record, and
    the refusal to signal anything the record does not prove is ours).  What is
    asserted here is that the take-down *asks*, and repeats the answer.
    """
    import slife2.a2a.broker as broker
    from slife2 import launcher

    config = default_config()
    object.__setattr__(config, "a2a", A2ASettings(configured=True))
    monkeypatch.setattr(launcher, "stop", lambda _config: iter(()))
    monkeypatch.setattr(
        broker, "stop", lambda _settings: "stopped mosquitto on localhost:1883"
    )

    assert slife2._down(config) == 0

    out = capsys.readouterr().out
    assert "a2a-broker" in out
    assert "stopped mosquitto" in out


def test_a_take_down_says_nothing_about_a_broker_there_is_none_of(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`""` from the broker is silence, not a row reading "left alone".

    A machine with no broker at all is the ordinary case for someone who does not
    use the mesh, and a take-down that announced the absence of a daemon nobody
    asked for would be reporting a non-event on every exit.
    """
    import slife2.a2a.broker as broker
    from slife2 import launcher

    config = default_config()
    object.__setattr__(config, "a2a", A2ASettings(configured=True))
    monkeypatch.setattr(launcher, "stop", lambda _config: iter(()))
    monkeypatch.setattr(broker, "stop", lambda _settings: "")

    assert slife2._down(config) == 0

    out = capsys.readouterr().out
    assert "a2a-broker" not in out
    assert "nothing running" in out, "and it still says the servers were not either"


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


# --- what the window starts on -------------------------------------------


def test_the_window_starts_on_the_configs_model() -> None:
    """The reference the TUI sends, and the model entry everything else comes from.

    Both come out of one model, and the model is what was missing: `SlifeApp`
    has taken a `context_window` since it was written and `main` never passed
    one, so the status bar rendered `↑ 25,000` and no percentage — while every
    widget test passed a window itself and so proved nothing about the wiring.
    This is the assertion that would have caught it.
    """
    config = default_config()
    reference, model = tui_model(config)
    assert reference == config.default
    assert model == config.resolve(config.default)[2]
    assert model.context_window > 0, "the shipped config gives its model a window"
    assert model.reasoning, "and says it reasons"
    assert model.accepts_images, "and that it reads images"


def test_the_flag_picks_that_models_own_window(tmp_path) -> None:
    """Two models, two windows, and the flag decides which one is in force.

    The percentage is against the model that answers, so a launch naming a
    different model must carry a different window — otherwise the bar would
    divide by the default model's, which is the kind of wrong that looks right.
    """
    path = tmp_path / "slife2.yaml"
    path.write_text(
        """
providers:
  p:
    api: openai-completions
    base_url: https://example.test/v1
    models:
      - model: small
        context_window: 1000
      - model: big
        context_window: 200000
default: p/small
""",
        encoding="utf-8",
    )
    config = load(path)
    assert tui_model(config, "p/big")[1].context_window == 200_000
    assert tui_model(config, "p")[1].context_window == 1000, "a bare provider"
    assert tui_model(config)[1].context_window == 1000, "the default"
    assert tui_model(config, "p/big")[0] == "p/big", "the wire form"


def test_a_model_with_no_window_reports_none(tmp_path) -> None:
    """Zero, not a guess — and the status bar reads that as "no percentage",
    the same way it reads `reasoning: false` as "no thinking badge" rather than
    inferring a capability from the model's name."""
    path = tmp_path / "slife2.yaml"
    path.write_text(
        """
providers:
  p:
    api: openai-completions
    base_url: https://example.test/v1
    models:
      - model: m
default: p/m
""",
        encoding="utf-8",
    )
    _, model = tui_model(load(path))
    assert model.context_window == 0
    assert not model.reasoning
    assert not model.accepts_images
