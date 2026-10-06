"""slife2 — a terminal AI agent whose components are MCP servers.

The MCP servers are **shared infrastructure**.  ``slife2`` brings up the ones
its config needs and attaches to whatever is already running, so a second
instance is one command and no duplicate process appears.  They are daemons: a
client exiting never stops one.

``--agent NAME`` names the instance.  It is a label and nothing more — no
per-agent port, process, or config section.  Isolation, where it is needed,
belongs inside an MCP server, not in the process layout; the label is passed
through to the agent server for exactly that reason.

    slife2 [--agent NAME]     ensure the servers, then run the TUI
    slife2 status             what is running, and where
    slife2 down               stop the servers this config names
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

__version__ = "0.1.0"

#: argv[0] values that select a subcommand instead of the TUI.  Checked before
#: argparse sees them, so `slife2 --agent jack` — the thing people actually
#: type — stays a plain flag invocation.
_COMMANDS = ("status", "down")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    rest = list(sys.argv[1:] if argv is None else argv)
    command = "run"
    if rest and rest[0] in _COMMANDS:
        command = rest.pop(0)

    parser = argparse.ArgumentParser(
        prog="slife2",
        description=(
            "Terminal AI agent. Starts the MCP servers it needs and shares any "
            "that are already running. Commands: status, down."
        ),
    )
    parser.add_argument("--config", default=None, help="path to slife2.yaml")
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
    args.command = command
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
        config: Config = load(args.config)
        provider_name, provider, model = config.resolve(args.model)
        tui_url = args.url or provider.server.url
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
            code = _ensure(config, find_config_path(args.config))
            if code == 0:
                app = SlifeApp(
                    tui_url,
                    agent=args.agent,
                    model_label=f"{provider_name}/{model.model}",
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
