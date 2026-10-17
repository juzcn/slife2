"""slife2 — a terminal AI agent whose plugins are MCP servers.

The MCP servers are **shared infrastructure**.  ``slife2`` brings up the ones
its config needs and attaches to whatever is already running, so a second
instance is one command and no duplicate process appears.  They are daemons: a
client exiting never stops one.

``--agent NAME`` names the instance, and the name is an **identity**: it is
claimed once on the machine, so two live instances may not share one, and the
claim is by the name rather than by the data directory.  It buys no per-agent
port, process or config section — the servers are shared, and isolation, where it
is needed, belongs inside an MCP server rather than in the process layout.  The
name is passed through to the agent server for exactly that reason.

    slife2 [OPTIONS]          ensure the servers, then run the TUI
    slife2 status [OPTIONS]   what is running, and where
    slife2 down [OPTIONS]     stop the servers this config names

Starting in a data directory **makes that home** (`slife2._open_home`,
`slife2._open_catalogue`): the directory is recorded, its `agents/` and `skills/`
are made, and the tool catalogue it is configured for is built — `agents/tools.db`,
by the hub, in one ask.  So a `--data-dir` an agent has been started in is a home
rather than a directory with a config in it, and one nobody has sent a message in
still has the tool set its `slife2.yaml` names.

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

`--lang` is the window's language, and **the window's only** (`slife2.tui.i18n`):
the model never reads a translated string, the logs stay English whatever the
flag says, and the report this command prints as the servers come up is the
terminal's rather than the window's.  It overrides the language the OS reports,
which is what it is for — a Chinese window on a machine whose interface is
English.
"""

from __future__ import annotations

#: **The version, and it is the first thing this module defines.**  `slife2`'s own
#: package root is imported by everything under it, so a name here that something
#: deeper wants is an edge back into this file — and `slife2.libs.plugin.protocol`
#: wants exactly one: `serve` writes it into the record that says which build a
#: daemon is.  Defined after the imports below, that read only worked because
#: nothing imported while this module's body ran ever reached `protocol.py` —
#: break that (add one import here, above this line) and the daemon start-up dies
#: with "cannot import name `__version__`", in a file nobody was editing.
#: Defined first, there is no order to get wrong.
__version__ = "0.1.0"

import argparse
import sys
import time

from slife2.libs.host.config import (
    A2A_SERVER_NAME,
    DEFAULT_AGENT,
    Config,
    ConfigError,
    ModelSettings,
    add_a2a_broker_argument,
    apply_a2a_broker,
    find_config_path,
    load,
)
from slife2.libs.host.paths import (
    add_data_dir_argument,
    agents_dir,
    apply_data_dir,
    register_home,
    skills_dir,
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
    # And the one flag that overrides a *config* value, for the one setting whose
    # useful case is somebody else's: a broker to join for a session rather than
    # a file to edit for every session (`slife2.libs.host.config.A2ASettings`).
    add_a2a_broker_argument(parser)
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
        "--lang",
        default=None,
        choices=("en", "zh"),
        help=(
            "the window's language, overriding the one the OS reports "
            "(default: detected)"
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
    apply_a2a_broker(args)
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


def _broker_not_honoured(config: Config) -> int:
    """`--a2a-broker` named a broker this command cannot put the mesh on.

    **A failure rather than a line, because the other outcome is a mesh silently
    on the wrong broker.**  A broker is where a process's MQTT sessions point,
    and the process holding them is the a2a server: one that is already running
    was started with some address, and this command did not choose it.  Carrying
    on would hand a model `a2a_*` tools that answer, peers that answer, and a
    broker nobody asked for — the "feature that quietly does nothing" this
    project refuses everywhere else, and the one way this flag can fail that
    looks like success.

    Two rather than a traceback, for `_ensure`'s reason: nothing has been drawn
    yet, and the fix is one of two words in a shell.
    """
    print(
        f"slife2: --a2a-broker {config.peer_agent.address} cannot be honoured: the a2a "
        f"server is already running, and a mesh is on the broker it was started "
        f"with.\n"
        f"  Stop it with `slife2 down` and start again, or drop the flag to use "
        f"the running one.",
        file=sys.stderr,
    )
    return 2


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
    #: Whether the mesh is answering from a process *this command* started.  A
    #: broker is a property of a process's connection and not of a call, so a
    #: server that was already up is on whatever broker it was started with —
    #: and `--a2a-broker` can only be honoured by the command that starts one.
    mesh_is_ours = True
    for outcome in ensure_all(config, config_path=config_path):
        count += 1
        if (
            outcome.spec.expected_name == A2A_SERVER_NAME
            and outcome.ok
            and outcome.status is not Status.STARTED
        ):
            mesh_is_ours = False
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

    if config.peer_agent.overridden and not mesh_is_ours:
        return _broker_not_honoured(config)

    if failed:
        print("\nslife2: could not start:", file=sys.stderr)
        for outcome in failed:
            print(f"  {outcome.spec.name}: {outcome.status.value}", file=sys.stderr)
            if outcome.detail:
                for line in outcome.detail.splitlines():
                    print(f"    {line}", file=sys.stderr)
        return 2
    return 0


def _open_catalogue(config: Config) -> int:
    """Make this home's tool catalogue, and report it as one line of the start.

    **A home is formed when an agent starts in it**, and `agents/tools.db` is the
    part of that this process cannot write itself: the hub is the file's one
    writer, so the ask that makes it is `list_tools` — the call a turn makes —
    sent once before the window opens
    (`slife2.libs.host.launcher.open_catalogue`).  What a `--data-dir` used to get
    instead was nothing at all until a model turn happened to ask, so a directory
    an agent had been started in held its config, the `agents/` and `skills/`
    `_open_home` makes, and no tool set.

    Printed for `_ensure`'s reason: this is a step that takes a second, and a
    start-up report that goes quiet while it runs is a report that cannot say
    which step is slow.  `_report`'s columns are the servers' and this line is
    not one, which is what the empty prefix says.

    **Failure is not degraded**, and it is the same answer as a server that would
    not come up: a hub that cannot say what the model may call is this system come
    apart, and a window opened against it fails its first turn instead — later,
    and with less to go on.  Two rather than a traceback, for `_ensure`'s reason.
    Only a `ConnectionError` is caught, so a programming error (this being called
    with a loop already running) is still a traceback rather than a start-up
    message about the catalogue.
    """
    from slife2.libs.host.launcher import open_catalogue
    from slife2.libs.host.paths import catalogue_db

    started = time.monotonic()
    try:
        count = open_catalogue(config)
    except ConnectionError as exc:
        print(f"slife2: the tool catalogue could not be built: {exc}", file=sys.stderr)
        return 2
    _report(
        "",
        "catalogue",
        str(catalogue_db()),
        f"{count} tool(s)",
        time.monotonic() - started,
    )
    return 0


def _open_home() -> None:
    """Make this data directory a home the whole machine can serve.

    **Two statements, and a fresh `--data-dir` is where both were missing.**  The
    directory is recorded, because the servers this machine is running are one
    set shared by every home (`slife2.libs.host.launcher._reuse`) and the only way
    a daemon can find the ledgers, logins and catalogues of a home nobody is
    sitting in front of is to be told which directories exist
    (`slife2.libs.host.paths.known_homes`).  And **the home's own shape is
    made** — `agents/` and `skills/`, the two parts of it that are not the config
    — because a client that has started an agent here and left no trace of it is
    a directory that looks like a mistake: exactly what it looked like, a data
    directory holding a `runtime/` of its own and nothing else, while every file
    the agent wrote went to the home the servers happened to be started from.

    One shape for a home and not half of one: `agents/` starts empty because no
    agent by a name this client did not use has anything in it, and `skills/`
    starts empty because the playbooks are a *person's* — copied in beside
    `slife2.yaml`, exactly as the config is, and generated by no run.  An empty
    directory is that answer rather than a fault, and it is the same answer
    `skills_dir` gives a fresh installation.

    Not done for `status` and `down`: they read what is running rather than
    putting anything anywhere, and a home enrolled by somebody asking a question
    would be enrolled for good.
    """
    register_home()
    agents_dir()
    skills_dir()


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
    machine with no `peer_agent:` section is a machine that is not on the mesh).  So the
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
    return config.server("peer_agent").url if config.peer_agent.configured else ""


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
    if config.peer_agent.configured:
        from slife2.plugins.a2a.broker import BROKER_NAME, log_url
        from slife2.plugins.a2a.broker import stop as stop_broker

        said = stop_broker(config.peer_agent)
        if said:
            anything = True
            _report("", BROKER_NAME, log_url(config.peer_agent), said)
    if not anything:
        print("nothing running")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `slife2` console script."""
    args = _parse_args(argv)
    if args.lang:
        # **Set before anything is built**, because every string the window draws
        # is resolved at the moment it is composed — and before the servers are
        # ensured, so that a window is never opened in one language and finished
        # in another.  The import is here rather than at module scope for this
        # module's usual reason: `slife2.libs.host.config` and the servers stay
        # importable without a UI, and this module is free of Textual only by
        # accident of where it lives (`slife2/tui/__init__.py` imports nothing,
        # and `i18n` needs nothing but the stdlib).
        from slife2.tui.i18n import set_language

        set_language(args.lang)

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
            # home rather than learning about it one call later.
            _open_home()
            code = _ensure(config, config_path)
            if code == 0:
                # **And the home is made whole before the window opens.**  One
                # call to the hub, and it is the call a turn would have made:
                # `agents/tools.db` is the hub's file, so asking is the only way
                # to make it exist, and a `--data-dir` an agent is started in has
                # its tool set from the first moment rather than from the first
                # message.  See `_open_catalogue`.
                code = _open_catalogue(config)
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
