"""slife2 — a terminal AI agent whose plugins are MCP servers.

The MCP servers are **shared infrastructure**.  ``slife2`` brings up the ones
its config needs and attaches to whatever is already running, so a second
instance is one command and no duplicate process appears.  They are daemons: a
client exiting never stops one.

``--agent NAME`` names the instance.  It is a label and nothing more — no
per-agent port, process, or config section.  Isolation, where it is needed,
belongs inside an MCP server, not in the process layout; the label is passed
through to the agent server for exactly that reason.

    slife2 [OPTIONS]          ensure the servers, then run the TUI
    slife2 status [OPTIONS]   what is running, and where
    slife2 down [OPTIONS]     stop the servers this config names

`--headless` runs that first one with **no terminal**: the same window, the same
connections, the same servers, the same turns, and nothing drawn and nothing
printed.  What it is for is a machine nobody is sitting in front of — the agent
stays reachable, because its window holds the mesh open for as long as this
process is up, so a peer can task it and be answered.  **Nothing else about the
agent changes**: an unattended agent is the same agent, and the only thing that
tells a *model* otherwise is its system prompt (`headless` in the template),
which is the lever for how it behaves.  `Ctrl+C` is the way out.

`--help` lists the options, and the flags may be written before or after the
command.  `--data-dir` is the one worth knowing about on its own: it decides
which config, which runtime state and which databases everything else reads.
"""

from __future__ import annotations

import argparse
import sys
import time

from slife2.libs.host.config import (
    DEFAULT_AGENT,
    Config,
    ConfigError,
    ModelSettings,
    find_config_path,
    load,
)
from slife2.libs.host.paths import (
    add_data_dir_argument,
    agents_dir,
    apply_data_dir,
    register_world,
)

__version__ = "0.1.0"

#: The subcommands, and what happens when none is named.
_RUN = "run"
_COMMANDS = (_RUN, "status", "down")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line.

    The subcommand is an ordinary optional positional, so argparse matches it
    wherever it appears: `slife2 status --data-dir D` and
    `slife2 --data-dir D status` are the same invocation.  It used to be pulled
    off the front before argparse saw the rest, which made the second spelling a
    usage error — and the flag is worth having on `status` and `down`, which are
    exactly the commands someone reaches for when the data directory is not the
    default one.

    Letting argparse own it also means an unknown command is a usage error
    naming the ones that exist, rather than a word silently treated as a flag.

    **And a flag is spelled the way it is written down**, which is
    `allow_abbrev=False` — argparse's default is the opposite, and it is worth
    knowing exactly what it buys and costs.  A prefix of a long option is taken
    for that option when it is unambiguous, so `--headles` (a missing `s`) was
    not a typo anybody was told about: it *was* `--headless`, and the stack came
    up as though it had been asked for.  The cost of refusing is that a spelling
    somebody has got used to stops working; what it buys is that a typo is a
    usage error naming the flags rather than a command that ran, and that an
    abbreviation which is unambiguous **today** cannot be broken by a longer flag
    added beside it later — `--keep` would be two flags the day anything else
    starts with those letters, and nobody would have been told to stop typing it.
    """
    rest = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(
        prog="slife2",
        # Deliberately does not enumerate the commands: the positional below
        # already lists them from `_COMMANDS`, and a second copy here is one
        # more place for the list to be wrong.
        description=(
            "Terminal AI agent. Starts the MCP servers it needs and shares any "
            "that are already running."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=_RUN,
        choices=_COMMANDS,
        help="what to do (default: run the TUI)",
    )
    # Shared with the servers' own parsers: the flag means the same thing in
    # both, and `slife2.libs.host.paths` is where the directory it names is defined.
    add_data_dir_argument(parser)
    parser.add_argument(
        "--agent",
        default=DEFAULT_AGENT,
        help=f"the name this instance runs under (default: {DEFAULT_AGENT})",
    )
    parser.add_argument(
        "--url", default=None, help="override the agent server's endpoint"
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "which model to start with, as provider/model "
            "(default: the config's `default`)"
        ),
    )
    parser.add_argument(
        "--keep-servers",
        action="store_true",
        help=(
            "leave the MCP servers running after this instance exits. By "
            "default the last instance to exit stops the ones it started"
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "run without a terminal: the same window, the same servers, the "
            "same turns, nothing drawn and nothing printed"
        ),
    )
    args = parser.parse_args(rest)
    apply_data_dir(args)
    return args


def _duration(seconds: float) -> str:
    """How long something took, in the unit a person reads it in.

    The same rule the transcript uses for a tool call (`slife2.tui.widgets`),
    and deliberately not the same code: that one is handed milliseconds and
    lives in a module that imports Textual, which this one must not — the CLI
    keeps `slife2.libs.host.config` and the servers importable without pulling in a UI.
    """
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def _report(
    prefix: str, name: str, url: str, detail: str, seconds: float = 0.0
) -> None:
    """One line of the startup report — and the time, when there is one.

    `seconds` is zero for the readings that are not a wait: `status` and `down`
    look at what a server is rather than making it anything, and a `0ms` on
    every line of those two would be a column of numbers about nothing.

    The time is right-aligned in a field of its own so the *numbers* line up on
    a start where every line is `started` or every line is `reusing` — which is
    all of them, on the two paths anyone reads this on.  That is what makes the
    outlier visible: the one that took twelve seconds is the one whose column
    does not match the other nine.
    """
    time_column = f"  {_duration(seconds):>6}" if seconds else ""
    print(f"  {name:<14} {url:<28} {prefix}{detail}{time_column}".rstrip())


def _ensure(config: Config, config_path) -> int:
    """Bring up what is missing, and report each server as it lands.

    **Printed per server, not at the end.**  `ensure_all` is a generator, and
    what that buys is not decoration: bringing one up blocks for a second or two
    and there are ten of them, so a report held back to the end is ten to twenty
    seconds of blank screen — and, when something is stuck, no way at all to see
    *which* server it is stuck on.  Printed as they land, the last line is that
    answer.

    Returns 0 when every server is usable, 2 otherwise — 2 rather than a
    traceback or a degraded TUI, because at this point nothing has been drawn
    and a two-line explanation is strictly more useful than a terminal that can
    never connect.
    """
    from slife2.libs.host.launcher import Status, ensure_all

    started = time.monotonic()
    failed = []
    count = 0
    for outcome in ensure_all(config, config_path=config_path):
        count += 1
        if outcome.status is Status.STARTED:
            _report(
                "starting -> ",
                outcome.spec.name,
                outcome.spec.url,
                "started",
                outcome.seconds,
            )
        elif outcome.ok:
            _report(
                "running  -> ",
                outcome.spec.name,
                outcome.spec.url,
                "reusing",
                outcome.seconds,
            )
        else:
            _report(
                "FAILED   -> ",
                outcome.spec.name,
                outcome.spec.url,
                # `.value` and not the member: `Status` is a plain `Enum`, so
                # `str` of it is `Status.CONFLICT` — which is what this line
                # said until the timing work put a reader next to it.  Its two
                # siblings below and in `_status` have always used the value.
                outcome.status.value,
                outcome.seconds,
            )
            failed.append(outcome)

    # The sum is not the same number as the parts: it includes the walking
    # between the servers and the locks, which is what a person waiting is
    # actually waiting for.
    plural = "" if count == 1 else "s"
    print(f"  {count} server{plural} in {_duration(time.monotonic() - started)}")

    if failed:
        print("\nslife2: could not start:", file=sys.stderr)
        for outcome in failed:
            print(f"  {outcome.spec.name}: {outcome.status.value}", file=sys.stderr)
            if outcome.detail:
                for line in outcome.detail.splitlines():
                    print(f"    {line}", file=sys.stderr)
        return 2
    return 0


def _open_world() -> None:
    """Make this data directory a world the whole machine can serve.

    **Two statements, and a fresh `--data-dir` is where both were missing.**  The
    directory is recorded, because the servers this machine is running are one
    set shared by every world (`slife2.libs.host.launcher._reuse`) and the only way
    a daemon can find the ledgers, logins and catalogues of a world nobody is
    sitting in front of is to be told which directories exist
    (`slife2.libs.host.paths.known_worlds`).  And its `agents/` is made, because a
    client that has started an agent here and left no trace of it is a directory
    that looks like a mistake — which is exactly what it looked like: a data
    directory holding a `runtime/` of its own and nothing else, while every file
    the agent wrote went to the world the servers happened to be started from.

    Not done for `status` and `down`: they read what is running rather than
    putting anything anywhere, and a world enrolled by somebody asking a question
    would be enrolled for good.
    """
    register_world()
    agents_dir()


def tui_url(config: Config, override: str | None = None) -> str:
    """Where the TUI connects: the agent server.

    **Not a model server.**  There are two kinds of endpoint in this system and
    they are one word apart in the config — `agent.server` and a provider's
    `server` — which is exactly how they get swapped.  Connecting to a model
    server fails with "not a slife2 agent server", because that server answers
    `stream_chat` and has never heard of `send_message`.

    Named and separate so the choice can be asserted on rather than read out of
    a long `main`.
    """
    return override or config.agent.server.url


def control_url(config: Config) -> str:
    """Where the window attaches to be handed messages from other channels.

    **And the naming is a trap worth walking into slowly**: `tui_url` above is the
    *agent* server this window connects to, and this is the `tui` plugin the
    window connects to as well — two addresses, two directions, and the word
    `tui` in both because in both cases it is the terminal that is at one end.

    It is a `servers:` entry rather than a section of its own
    (`slife2.plugins.tui.server`), so it is started, probed and stopped with everything
    else, which is what `_ensure` and `_down` get for free.
    """
    return config.server("tui").url


def mesh_url(config: Config) -> str:
    """Where the window puts its agent on the A2A mesh, or `""` for not on one.

    **`""` is the whole of what "not on the mesh" means here**, and it is the
    config's own switch rather than a second one (`slife2.libs.host.config.A2ASettings`: a
    machine with no `a2a:` section is a machine that is not on the mesh).  So the
    empty string is returned for an absent section and the window then holds
    nothing and draws nothing — rather than holding a call to a plugin that would
    answer "this machine is not on the A2A mesh" every two seconds, which reads
    as a fault rather than as a setting.

    **The key, and not the identity** — two names one word apart, and looking up
    the wrong one is a `ConfigError` before anything starts.  `a2a` is what
    `servers:` files this address under (`slife2.libs.host.config.LOCAL_SERVERS`, and the
    section's own name); `slife2-a2a` is what the *server* answers to, which is
    what `slife2.tui.client` proves it reached (`open_server(name=…)`) and has
    never been a key in that table.  `control_url` above is `"tui"` for the same
    reason.
    """
    return config.server("a2a").url if config.a2a.configured else ""


def tui_model(config: Config, reference: str = "") -> tuple[str, ModelSettings]:
    """Which model the window starts on, and what that model is.

    Two values out of one lookup, and the second is the reason this is a
    function.  Everything the status bar says about the model — how full the
    conversation is, whether it reasons, whether it can be shown a picture — is
    a field of the model's *own* config entry, and none of it was reaching the
    TUI: `SlifeApp` has taken a `context_window` since it was written and was
    never passed one, so the bar rendered `↑ 25,000` with no percentage while
    every widget test passed a window itself and so proved nothing about the
    wiring.

    Separate for the reason :func:`tui_url` is: assembled inside `main`, these
    are values nothing can assert on.  The reference is built here too rather
    than at the call site because `--model` may name a bare provider, and the
    wire wants `provider/model`.
    """
    name, _, model = config.resolve(reference)
    return f"{name}/{model.model}", model


def _status(config: Config) -> int:
    from slife2.libs.host.launcher import Status, statuses

    for outcome in statuses(config):
        record = outcome.record
        suffix = str(outcome.status.value)
        if outcome.status in (Status.RUNNING, Status.UNMANAGED) and record:
            suffix += f" (pid {record.pid}, since {record.started_at})"
        elif outcome.status is Status.UNMANAGED:
            suffix += " (not started by slife2)"
        if outcome.detail:
            suffix += f" - {outcome.detail}"
        _report("", outcome.spec.name, outcome.spec.url, suffix)
    return 0


def _down(config: Config) -> int:
    from slife2.libs.host.launcher import Status, stop

    outcomes = stop(config)
    anything = False
    for outcome in outcomes:
        if outcome.status is Status.STOPPED:
            anything = True
            _report(
                "", outcome.spec.name, outcome.spec.url, outcome.detail or "stopped"
            )
        elif outcome.status is not Status.NOT_RUNNING:
            _report(
                "",
                outcome.spec.name,
                outcome.spec.url,
                outcome.detail or str(outcome.status.value),
            )
    # **And the broker, which is the one daemon here the launcher does not
    # start.**  It has no module to launch and no MCP identity to check — the
    # a2a plugin brings one up when the mesh is wanted (`slife2.plugins.a2a.broker.ensure`)
    # — so it is not in `specs` and `stop` never sees it.  What it does have is a
    # record of what was started, written by whoever started it, which is what
    # makes stopping it here the same act as stopping every other daemon, and
    # what keeps a broker *slife2 did not start* out of it: no record, no
    # signalling.  A stack going away is exactly the moment it should, since the
    # alternative is a machine left holding a broker nobody is using.
    if config.a2a.configured:
        from slife2.plugins.a2a.broker import BROKER_NAME, log_url
        from slife2.plugins.a2a.broker import stop as stop_broker

        said = stop_broker(config.a2a)
        if said:
            anything = True
            _report("", BROKER_NAME, log_url(config.a2a), said)
    if not anything:
        print("nothing running")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `slife2` console script."""
    args = _parse_args(argv)

    try:
        config_path = find_config_path()
        config: Config = load()
        # The provider itself is not needed here: the TUI talks to the *agent*
        # server, which is the one that knows which model server to use.  What
        # is needed is the reference — because `--model` may be a bare provider
        # name and the wire wants `provider/model` — and the model's own entry,
        # which is where everything the status bar says about the model comes
        # from: its window, whether it reasons, whether it reads images.
        #
        # Resolved here rather than after the servers are up so that a `--model`
        # naming something that does not exist is one line and no processes.
        model_reference, model_settings = tui_model(config, args.model or "")
    except ConfigError as exc:
        # A config mistake is worth a one-line message rather than a traceback:
        # nothing has started yet, and the fix is in the file being named.
        print(f"slife2: {exc}")
        return 2

    if args.command == "status":
        return _status(config)
    if args.command == "down":
        return _down(config)

    # Imported here rather than at module scope so `slife2.libs.host.config` and the
    # servers stay importable without pulling in fastmcp or Textual.
    from slife2.libs.host.launcher import AgentInUse, claim_agent, others_running
    from slife2.tui.app import SlifeApp, configured_models

    code = 0
    claimed = False
    try:
        # The name is claimed for the lifetime of the TUI, and refused rather
        # than shared if another instance already answers to it: an agent name
        # is an identity, and two live claims on one identity is a contradiction
        # rather than a configuration.  Note what it does not do — the servers
        # stay shared with every other agent.
        with claim_agent(args.agent, headless=args.headless):
            claimed = True
            # Before the servers are ensured, so that a daemon starting now —
            # and the walks it does on the way up — already knows about this
            # world rather than learning about it one call later.
            _open_world()
            code = _ensure(config, config_path)
            if code == 0:
                app = SlifeApp(
                    tui_url(config, args.url),
                    agent=args.agent,
                    model=model_reference,
                    # What this model is, from its own config entry.  Passed at
                    # all is the point: the status bar has known how to render
                    # each of these since it was written, and showed none of
                    # them for as long as nothing handed them over.
                    context_window=model_settings.context_window,
                    thinking=model_settings.reasoning,
                    vision=model_settings.accepts_images,
                    # Every model the config defines, for the picker `Ctrl+S`
                    # opens.  Built here rather than by the window, because this
                    # read of the file is the one that already happened: the
                    # picker offers `providers:` as this start saw it, and a
                    # model the *model* has added since is offered from the next
                    # start — which is what keeps the window out of the file.
                    models=configured_models(config),
                    # Where this window attaches, so another channel can hand it
                    # a message (`slife2.plugins.tui.server`).  One address for every
                    # agent: what picks out this window is the agent name it
                    # attaches under, not a port of its own.
                    control_url=control_url(config),
                    # And where it holds its place on the A2A mesh, if this
                    # machine is on one: an agent's window is what puts it
                    # online, so the address has to reach the window rather
                    # than the plugin that was already started without it.
                    mesh_url=mesh_url(config),
                )
                # **The whole of `--headless`, and it is the window either
                # way.**  Nothing is passed in and nothing above changes: the
                # same three connections, the same queue, the same turns, the
                # same prompts — a message a channel or a peer hands this agent
                # becomes a turn here exactly as it does on a screen.  What the
                # flag decides is the *driver*, which writes nothing and reads
                # nothing, and what tells a model that nobody is there is the
                # claim it was written into (`claim_agent` above), which the
                # agent server renders into the system prompt.
                app.run(headless=args.headless)
    except AgentInUse as exc:
        print(f"slife2: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # **Ctrl+C, and the one path that reaches here is the headless one.**
        # A window reads Ctrl+C as a key (`SlifeApp.action_interrupt`: cancel
        # the turn, or quit when there is none), so the app returns by itself
        # and this branch is never taken.  With no terminal there is no input
        # reader and the signal reaches Python instead, which cancels the app
        # — Textual runs its shutdown on the way out, and *that* is what closes
        # the two held calls and takes the agent off the mesh.  All that is left
        # here is to not print a traceback for a stop somebody asked for: the
        # `finally` below still takes the stack down like any other exit.
        pass
    finally:
        # The last client out stops the servers, on every exit path — a normal
        # quit, Ctrl+C, or the TUI dying.
        #
        # This is the other half of "the servers are daemons".  They must
        # outlive *a* client, or sharing would be meaningless, but not the last
        # one: otherwise an ordinary exit leaves the servers running until the
        # next reboot.  `others_running` counts by pid liveness, so a client
        # that was killed — and so never deregistered — cannot keep them alive.
        if claimed and not args.keep_servers and not others_running():
            print("  last instance out; stopping the shared servers")
            _down(config)
    return code
