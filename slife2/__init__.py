"""slife2 — a terminal AI agent whose components are MCP servers.

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

`--help` lists the options, and the flags may be written before or after the
command.  `--data-dir` is the one worth knowing about on its own: it decides
which config, which runtime state and which databases everything else reads.
"""

from __future__ import annotations

import argparse
import sys

from slife2.config import (
    DEFAULT_AGENT,
    Config,
    ConfigError,
    find_config_path,
    load,
)
from slife2.paths import add_data_dir_argument, apply_data_dir

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
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=_RUN,
        choices=_COMMANDS,
        help="what to do (default: run the TUI)",
    )
    # Shared with the servers' own parsers: the flag means the same thing in
    # both, and `slife2.paths` is where the directory it names is defined.
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
    args = parser.parse_args(rest)
    apply_data_dir(args)
    return args


def _report(prefix: str, name: str, url: str, detail: str) -> None:
    print(f"  {name:<14} {url:<28} {prefix}{detail}".rstrip())


def _ensure(config: Config, config_path) -> int:
    """Bring up what is missing, and report what is already there.

    Returns 0 when every server is usable, 2 otherwise — 2 rather than a
    traceback or a degraded TUI, because at this point nothing has been drawn
    and a two-line explanation is strictly more useful than a terminal that can
    never connect.
    """
    from slife2.launcher import Status, ensure_all

    outcomes = ensure_all(config, config_path=config_path)

    failed = []
    for outcome in outcomes:
        if outcome.status is Status.STARTED:
            _report("starting -> ", outcome.spec.name, outcome.spec.url, "started")
        elif outcome.ok:
            _report("running  -> ", outcome.spec.name, outcome.spec.url, "reusing")
        else:
            _report(
                "FAILED   -> ", outcome.spec.name, outcome.spec.url, str(outcome.status)
            )
            failed.append(outcome)

    if failed:
        print("\nslife2: could not start:", file=sys.stderr)
        for outcome in failed:
            print(f"  {outcome.spec.name}: {outcome.status.value}", file=sys.stderr)
            if outcome.detail:
                for line in outcome.detail.splitlines():
                    print(f"    {line}", file=sys.stderr)
        return 2
    return 0


def tui_url(config: Config, override: str | None = None) -> str:
    """Where the TUI connects: the agent server.

    **Not a model server.**  There are two kinds of endpoint in this system and
    they are one word apart in the config — `agent.server` and a provider's
    `server` — which is exactly how they get swapped.  Connecting to a model
    server fails with "not a slife2 agent server", because that server answers
    `stream_chat` and has never heard of `run_turn`.

    Named and separate so the choice can be asserted on rather than read out of
    a long `main`.
    """
    return override or config.agent.server.url


def _status(config: Config) -> int:
    from slife2.launcher import Status, statuses

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
    from slife2.launcher import Status, stop

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
        # server, which is the one that knows which model server to use.
        provider_name, _, model = config.resolve(args.model)
    except ConfigError as exc:
        # A config mistake is worth a one-line message rather than a traceback:
        # nothing has started yet, and the fix is in the file being named.
        print(f"slife2: {exc}")
        return 2

    if args.command == "status":
        return _status(config)
    if args.command == "down":
        return _down(config)

    # Imported here rather than at module scope so `slife2.config` and the
    # servers stay importable without pulling in fastmcp or Textual.
    from slife2.launcher import AgentInUse, claim_agent, others_running
    from slife2.tui.app import SlifeApp

    code = 0
    claimed = False
    try:
        # The name is claimed for the lifetime of the TUI, and refused rather
        # than shared if another instance already answers to it: an agent name
        # is an identity, and two live claims on one identity is a contradiction
        # rather than a configuration.  Note what it does not do — the servers
        # stay shared with every other agent.
        with claim_agent(args.agent):
            claimed = True
            code = _ensure(config, config_path)
            if code == 0:
                # Resolved, because `--model` may be a bare provider name and
                # the wire wants `provider/model`.  Passed to the TUI at all is
                # the point: without it the flag reached the window title and
                # nothing else, and every turn ran on the config's default.
                app = SlifeApp(
                    tui_url(config, args.url),
                    agent=args.agent,
                    model=f"{provider_name}/{model.model}",
                )
                app.run()
    except AgentInUse as exc:
        print(f"slife2: {exc}", file=sys.stderr)
        return 2
    finally:
        # The last client out stops the servers, on every exit path — a normal
        # quit, Ctrl+C, or the TUI dying.
        #
        # This is the other half of "the servers are daemons".  They must
        # outlive *a* client, or sharing would be meaningless, but not the last
        # one: otherwise an ordinary exit leaves four processes running until
        # the next reboot.  `others_running` counts by pid liveness, so a client
        # that was killed — and so never deregistered — cannot keep them alive.
        if claimed and not args.keep_servers and not others_running():
            print("  last instance out; stopping the shared servers")
            _down(config)
    return code
