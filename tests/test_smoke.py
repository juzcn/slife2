"""The console entry point.

`main()` now starts a Textual app, so it cannot simply be called and inspected —
the app would take over the terminal.  What is worth testing is the part that
runs *before* the app: argument handling and config failure.  Both must produce
an exit code and a message rather than a traceback, because at that point
nothing has started yet and the fix is in the file being named.
"""

from __future__ import annotations

from typing import Any

import pytest

import slife2
from slife2 import _parse_args, main, mesh_url, tui_model, tui_url
from slife2.libs.host.config import A2A_SERVER_NAME, A2ASettings, default_config, load

pytestmark = pytest.mark.unit


def test_version_is_exposed() -> None:
    assert slife2.__version__ == "0.1.0"


def test_package_import_does_not_pull_in_textual() -> None:
    """`slife2.libs.host.config` and the servers must be importable without a TUI stack.

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
    `open_server(name=…)` checks, from `slife2.libs.host.config.SERVER_MODULES`).  Looking
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
    import slife2.plugins.a2a.broker as broker
    from slife2.libs.host import launcher

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
    import slife2.plugins.a2a.broker as broker
    from slife2.libs.host import launcher

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


def test_headless_is_off_unless_it_is_asked_for() -> None:
    assert _parse_args([]).headless is False
    assert _parse_args(["--headless"]).headless is True
    assert _parse_args(["--headless", "--agent", "jack"]).headless is True


def test_headless_reaches_the_driver_and_the_claim(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One flag, two readers, and the second is the one that is easy to lose.

    Textual's driver is the obvious one — that is what writes nothing and draws
    nothing — and it is the *whole* of what `--headless` does to the window:
    nothing is passed in, because an unattended agent is the same agent.  The
    other reader is the servers: they render the system prompt
    (`slife2.plugins.agent.opening_messages`), they are a different process from the one
    that was told `--headless`, and a prompt is exactly where a model finds out
    that there is nobody to ask.  That is why the flag is written into the claim:
    `AgentClaim.headless` is the shared record of who is running under a name,
    and one instance per name is what makes reading it worth anything.

    The claim is read *from inside the run*, because that is when it is true —
    it is cleared on the way out, exactly as a window's is.

    **Under a name of its own**, because the claim is a *machine-wide* lock on
    the name rather than one scoped to the data directory: `slife2` running in
    somebody's terminal — the ordinary case while this feature is being used —
    holds that mutex, and a test that claimed the default name would fail as
    `agent 'slife2' is already running` for a reason that has nothing to do with
    what it asserts.  `tests/test_launcher.py` names its claims `alpha`, `beta`
    and so on for exactly this.
    """
    from uuid import uuid4

    from slife2.libs.host.runtime import read_claim
    from slife2.tui import app as app_module

    agent = f"smoke-{uuid4().hex[:8]}"
    seen: dict[str, Any] = {}

    class RecordingApp:
        """The window, stood in for: what it was handed, and how it was run."""

        def __init__(self, url: str, **kwargs: object) -> None:
            seen["url"] = url
            seen.update(kwargs)

        def run(self, *, headless: bool = False) -> None:
            seen["ran"] = headless
            seen["claim"] = read_claim(agent)

    monkeypatch.setattr(slife2, "_ensure", lambda config, config_path: 0)
    # And the catalogue step, which is one call to the hub.  `_ensure` is stubbed
    # here, so no hub is up to answer it — and the conftest guard would refuse the
    # dial rather than let this reach a live one.  What it does, and where it sits,
    # is `test_the_home_is_made_before_the_window_opens`.
    monkeypatch.setattr(slife2, "_open_catalogue", lambda config: 0)
    monkeypatch.setattr(app_module, "SlifeApp", RecordingApp)

    # `--keep-servers`, so the last-instance-out take-down does not run against
    # a data directory with nothing in it.
    code = main(
        ["--data-dir", str(tmp_path), "--agent", agent, "--headless", "--keep-servers"]
    )

    assert code == 0
    assert seen["ran"] is True, "the driver was not told to write nothing"
    assert "headless" not in seen, "the window is the same window"
    claim = seen["claim"]
    assert claim is not None, "no claim was written for the running instance"
    assert claim.headless is True, "the servers cannot read the flag"


def test_the_home_is_made_before_the_window_opens(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three steps, one order, and the catalogue on the right side of the window.

    **A home is made when an agent starts in it**, and these three statements are
    the whole of that: the directory is recorded and shaped (`_open_home`) — before
    the servers, so a daemon coming up already knows about this home — the servers
    come up (`_ensure`), and the tool catalogue is built (`_open_catalogue`).  That
    last one has to come *after* the servers, because `agents/tools.db` is the
    hub's file and the hub is what writes it, and it has to come *before* the
    window, because a window is a conversation and the file is what its first turn
    is served from.
    """
    from slife2.tui import app as app_module

    order: list[str] = []

    def step(name: str, code: int = 0):
        """One of the start-up steps, and a note that it ran."""

        def run(*args: object, **kwargs: object) -> int:
            order.append(name)
            return code

        return run

    class RecordingApp:
        def __init__(self, url: str, **kwargs: object) -> None:
            order.append("window")

        def run(self, *, headless: bool = False) -> None:
            pass

    monkeypatch.setattr(slife2, "_open_home", step("home"))
    monkeypatch.setattr(slife2, "_ensure", step("servers"))
    monkeypatch.setattr(slife2, "_open_catalogue", step("catalogue"))
    monkeypatch.setattr(app_module, "SlifeApp", RecordingApp)

    # `--keep-servers`, so the last-instance-out take-down does not run.
    code = main(
        [
            "--data-dir",
            str(tmp_path),
            "--agent",
            f"smoke-order-{tmp_path.name}",
            "--keep-servers",
        ]
    )

    assert code == 0
    assert order == ["home", "servers", "catalogue", "window"]


def test_a_catalogue_that_cannot_be_built_does_not_open_a_window(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**A hub that cannot say what the model may call is a system come apart.**

    So the start says so and stops, like a server that would not come up — one
    line and exit 2 — rather than opening a terminal that would fail its first
    turn instead: later, and with less to go on.  This is the same rule as
    `_ensure`'s and deliberately not a degraded start, which would be a window
    nobody can use reported as success.
    """
    from slife2.libs.host import launcher
    from slife2.tui import app as app_module

    opened: list[str] = []

    def refuse(config: object) -> int:
        raise ConnectionError("slife2-toolhub is not answering (no answer)")

    class RecordingApp:
        def __init__(self, url: str, **kwargs: object) -> None:
            opened.append(url)

        def run(self, *, headless: bool = False) -> None:
            pass

    monkeypatch.setattr(slife2, "_open_home", lambda: None)
    monkeypatch.setattr(slife2, "_ensure", lambda config, config_path: 0)
    # The dial, not the step: `slife2._open_catalogue` is what turns this into a
    # start-up message and exit 2, so patching it instead would skip the whole
    # thing under test.
    monkeypatch.setattr(launcher, "open_catalogue", refuse)
    monkeypatch.setattr(app_module, "SlifeApp", RecordingApp)

    code = main(
        [
            "--data-dir",
            str(tmp_path),
            "--agent",
            f"smoke-refused-{tmp_path.name}",
            "--keep-servers",
        ]
    )

    assert code == 2
    assert not opened, "a window was opened against a hub that cannot list"
    assert "slife2-toolhub is not answering" in capsys.readouterr().err


def test_an_abbreviated_flag_is_a_usage_error() -> None:
    """`--headles` is a typo, and the mistake was not being told so.

    argparse abbreviates long options by default: any *unambiguous prefix* is
    taken for the flag it could become, so a missing `s` was not a misspelling
    of anything — it was `--headless`, and the stack came up as though it had
    been asked for.  The other half of the same default is worse than a typo:
    an abbreviation that works today stops working the day a longer flag appears
    beside it, and nobody who had learned to type it would be told.
    """
    with pytest.raises(SystemExit):
        _parse_args(["--headles"])
    with pytest.raises(SystemExit):
        _parse_args(["--keep"])
    assert _parse_args(["--headless"]).headless is True
    assert _parse_args(["--keep-servers"]).keep_servers is True


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
