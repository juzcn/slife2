"""Bringing up the shared MCP servers, and attaching to the ones already there.

Every server in this system is shared infrastructure.  `slife2` ensures the ones
its config needs are running and attaches to whatever it finds, so a second
instance is one command and no duplicate process appears.  Nothing here is
per-agent: the name is an identity and is claimed as one (`claim_agent`), which
buys no port and no process, and isolation, where it exists at all, is an MCP
server's own business.

The decision that everything else follows from: **a probe is the only authority
on whether a server is running.**  A listening port proves something is there; a
record proves something was there once.  Only a server that answers *as itself*
proves that *our* server is up — its advertised name, from the handshake, or a
tool it certainly serves when it reports no name (`slife2.libs.plugin.protocol.identifies`)
— which is why the record is never consulted for liveness and a stale one can
never wedge a start.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path

from fastmcp import Client

import slife2.helpers.timeouts as _T
from slife2.clients.toolclient import LIST_TOOLS, ToolCounts, remote_catalogue
from slife2.libs.host.config import (
    LOCAL,
    LOCAL_SERVERS,
    PLUGINS,
    REMOTE,
    TOOLHUB_SERVER_NAME,
    Config,
    Plugin,
    plugin,
)
from slife2.libs.host.paths import data_dir
from slife2.libs.host.runtime import (
    AgentClaim,
    ClientRecord,
    ServerRecord,
    agent_key,
    clear_claim,
    clear_record,
    exclusive,
    live_clients,
    pid_alive,
    read_claim,
    read_record,
    register_client,
    same_process,
    spawn,
    start_lock,
    tail_log,
    tcp_listening,
    terminate,
    unregister_client,
    write_claim,
)
from slife2.libs.plugin.protocol import close_server, identifies, open_server

#: The tool every model server answers to, and how they are named.  One tool
#: and one prefix for all of them, because a backend is a backend whatever
#: protocol it speaks — adding one changes a row of `slife2.libs.host.config.PLUGINS`
#: and nothing in this file.
BACKEND_PREFIX = "llm:"


class Status(enum.Enum):
    """What a server turned out to be."""

    RUNNING = "running"
    #: Answering correctly, but started by hand — there is no record of it.
    #: Reused with a warning, because refusing would be pedantry.
    UNMANAGED = "running (external)"
    #: The port is held by something that is not this server.
    CONFLICT = "port held by something else"
    STARTED = "started"
    STOPPED = "stopped"
    FAILED = "failed"
    NOT_RUNNING = "not running"


class AgentInUse(Exception):
    """Another instance is already running under this agent name.

    An agent name is an identity, and two live instances claiming one identity
    is a contradiction rather than a configuration — so it is refused instead of
    resolved.  Note what this is *not*: it does not partition anything.  The
    servers stay shared; only the name is exclusive.
    """

    def __init__(self, name: str, holder: AgentClaim | None) -> None:
        where = ""
        if holder is not None and pid_alive(holder.pid):
            where = f" (pid {holder.pid}"
            where += f", since {holder.started_at})" if holder.started_at else ")"
        super().__init__(f"agent {name!r} is already running{where}")
        self.name = name
        self.holder = holder


class StartFailed(Exception):
    """A server was spawned and did not come up.

    Carries the child's log tail, because the alternative — "did not become
    ready" with nothing else — sends the reader hunting for a log file at the
    exact moment they are least inclined to.
    """

    def __init__(self, message: str, log_tail: str = "") -> None:
        super().__init__(message)
        self.log_tail = log_tail


@dataclass(frozen=True)
class ServerSpec:
    """One server, as the launcher sees it."""

    name: str
    module: str
    url: str
    host: str
    port: int
    #: The MCP name the server advertises, which is what proves it is ours.
    expected_name: str
    #: The tool that identifies it when it advertises no name — see
    #: `slife2.libs.plugin.protocol.identifies`.
    expected_tool: str


@dataclass(frozen=True)
class Outcome:
    """What happened to one server."""

    spec: ServerSpec
    status: Status
    record: ServerRecord | None = None
    detail: str = ""
    #: How long making this server usable took, when anybody measured.  Zero
    #: everywhere else, and zero is not a claim that it was instant — `status`
    #: and `stop` read what a server *is* rather than making it anything, and
    #: there is no wait there to report.  `ensure` is the one that times itself,
    #: and it times the whole call: reusing a server is a way of making it
    #: usable too, and a probe that took 4 seconds is worth seeing.
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in (Status.RUNNING, Status.UNMANAGED, Status.STARTED)


def spec_for(config: Config, one: Plugin) -> ServerSpec:
    """One row of `PLUGINS`, in the launcher's own terms.

    **Everything a spec is comes out of that row**, which is the point of it: the
    module to run, the name to expect, the tool to probe with and the port to
    fall back on are all facts about the server, and a second place to spell any
    of them is a second place for it to be wrong.  What is *not* in the row is
    where this config puts it, which is `config.server(key)` — a `servers:` block
    may move any endpoint, and the launcher's job is to start what the file says.

    The one thing the row cannot answer is what the *spec* is called: a backend
    is named for its protocol with a prefix (`llm:openai-completions`) because
    that is what a person reads in a `status` line, and every other server is
    named for its own key.
    """
    address = config.server(one.key)
    return ServerSpec(
        name=f"{BACKEND_PREFIX}{one.key}" if one.api else one.key,
        module=one.module,
        url=address.url,
        host=address.host,
        port=address.port,
        expected_name=one.name,
        expected_tool=one.probe,
    )


def spec_for_api(config: Config, api: str) -> ServerSpec:
    """The one model server that serves *api*, in the launcher's own terms.

    `specs` builds the whole list and this is one row of it, so a caller that
    needs a single backend — the model config tools, which start one when a
    provider is added for a protocol nothing is serving — builds the same spec
    the launcher would rather than a second spelling of it.
    """
    return spec_for(config, plugin(api))


def specs(config: Config) -> list[ServerSpec]:
    """The servers this config needs, in the order they must be started.

    **One per wire protocol, not one per provider.**  A process speaks one
    format, so every OpenAI-compatible provider is served by the same process
    and `stream_chat(provider=..., model=...)` says whose credentials to use.
    A provider with its own process would mean three processes for three
    providers, which is what this replaced.

    Which families are needed is derived from the provider table: a protocol no
    provider uses has nothing to serve, and starting it would be a process
    holding nobody's key.

    **Order matters.**  The agent server connects to a model server when a turn
    needs it, but a model server answering first is what makes the first turn
    work rather than fail and retry.
    """
    result = [spec_for_api(config, api) for api in config.apis_in_use()]
    result += [spec_for(config, one) for one in PLUGINS if not one.api]
    return result


async def _probe_async(spec: ServerSpec, timeout: float) -> bool:
    client: Client = Client(spec.url, timeout=timeout)
    await client.__aenter__()
    try:
        return await identifies(
            client, spec.expected_name, fallback_tool=spec.expected_tool
        )
    finally:
        await client.__aexit__(None, None, None)


def probe(spec: ServerSpec, *, timeout: float | None = None) -> bool:
    """Whether the server at `spec.url` is up *and* is the one we expect.

    Identity comes from the handshake, which is what makes this better than a
    connection test: it catches the port being held by a different MCP server,
    which a connect would wave through and which would then fail in the middle
    of a turn.  See `slife2.libs.plugin.protocol.identifies`.

    Takes the whole spec rather than a URL and a name so the two cannot be
    paired up wrongly at a call site — there are five of them, and a mismatched
    pair would report a perfectly good server as absent.

    `timeout` is None by default, resolved here rather than in the signature:
    a def-time default captures the registry's value at import, which is the one
    thing the gate forbids (`slife2.helpers.timeouts`).

    Blocks, and so cannot be called from inside a running event loop.  It never
    is in production — the launcher runs before the TUI starts its loop — and
    calling it from one anyway is a **programming error, not an absent server**.
    That distinction has to be made here rather than caught below: `asyncio.run`
    raises when a loop is already running, and an `except Exception` around it
    turns "you called this wrong" into "nothing is running" — a wrong answer
    that looks like a right one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no loop of our own to conflict with, which is the normal case
    else:
        raise RuntimeError(
            "probe() is synchronous and cannot run inside an event loop; "
            "call the launcher before starting one, or from a thread"
        )

    try:
        return asyncio.run(
            _probe_async(spec, _T.timeouts.ready.probe if timeout is None else timeout)
        )
    except Exception:  # noqa: BLE001 - a probe answers yes or no; a peer that threw is not up
        return False


async def _open_catalogue_async(url: str) -> ToolCounts:
    """Ask the hub for this home's tool list, which is what builds its catalogue.

    `remote_catalogue` reads the answer the way the agent server does —
    strictly, so a hub that answered with something this build cannot read is a
    failure rather than an empty tool set — and the tools it hands back are
    dropped here: what this call is *for* is the hub opening the calling home's
    `agents/tools.db` and writing the machine's tool set into it.

    **Both numbers, and they are different questions.**  The loaded half is the
    hub's loaded set and the total is the catalogue's size; the window's status
    bar starts on `loaded/total`, and this is the ask that can hand it both
    before a turn exists.  The first model call replaces the loaded half with
    the request's own list, which also holds the agent process's mechanism
    tools — not catalogue rows, and not something this ask can see.
    """
    client = await open_server(url, name=TOOLHUB_SERVER_NAME, fallback_tool=LIST_TOOLS)
    try:
        # The cold budget: this is the ask that builds the file, so it waits for
        # plugins that are still starting and for the rows to be embedded.  A
        # turn's ask stays `work.hub` (`slife2.clients.toolclient`).
        listing = await remote_catalogue(client, timeout=_T.timeouts.work.catalogue)
        return ToolCounts(loaded=len(listing.tools), total=listing.total)
    finally:
        await close_server(client)


def open_catalogue(config: Config) -> ToolCounts:
    """Make this home's tool catalogue, and answer what it holds and offers.

    **Both numbers**: `loaded` is the rows the hub has loaded and `total` counts
    every row the file has, skills and CLI entries included and a source that is
    down among them (`slife2.libs.store.catalogue.ToolStore.count`).  It is the
    pair the window starts its status bar on; a turn's own report replaces the
    loaded half with the request's list, which also carries the agent process's
    two mechanism tools (not catalogue rows, so not in this ask).

    **A home is formed when an agent starts in it**, and the catalogue is the
    part of that this process cannot write itself: `agents/tools.db` belongs to
    the *hub*, which is its one writer (`slife2.plugins.toolhub.server.Catalogue`),
    so the way to make it exist is to ask the hub the question a turn asks —
    `list_tools` — once, before the window opens.  Until this existed the file
    appeared only if a model turn happened to ask, so a data directory an agent
    had been started in and not yet used held its `slife2.yaml`, the `agents/`
    and `skills/` `slife2._open_home` makes, and no catalogue at all.

    **And the ask names the home and nothing else.**  `Peer.call_tool` puts this
    process's home on every call it makes (`slife2.libs.plugin.audience.with_home`),
    which is what the hub resolves `agents/tools.db` against.  The catalogue is
    the *home's* and not an agent's — one directory may hold several agents and
    they share it — so no agent is named here, and the hub needs none.

    Raises whatever `open_server` or `remote_catalogue` raises, which is a
    `ConnectionError` naming what could not be reached or what could not be read.
    **Deliberately not caught**: a hub that cannot say what the model may call is
    this system come apart, and the caller's job is to say so rather than to start
    a window that cannot run a turn.

    Blocks, for `probe`'s reason and under the same rule — see there for why the
    loop check is made *before* `asyncio.run` rather than caught below it.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no loop of our own to conflict with, which is the normal case
    else:
        raise RuntimeError(
            "open_catalogue() is synchronous and cannot run inside an event "
            "loop; call it before starting one, or from a thread"
        )

    return asyncio.run(_open_catalogue_async(config.server("toolhub").url))


def _argv(spec: ServerSpec) -> list[str]:
    """The command that starts a server.

    `sys.executable -m` rather than the console script: it guarantees the daemon
    runs in the same interpreter and virtual environment as the client that
    spawned it, and it does not depend on the scripts directory being on PATH —
    which it is not, under `uv run`.

    Host and port are pinned to what this client already decided, so editing the
    config between load and spawn cannot move an endpoint out from under a
    server that was started for it.
    """
    argv = [
        sys.executable,
        "-m",
        spec.module,
        "--host",
        spec.host,
        "--port",
        str(spec.port),
    ]
    # The *resolved* directory, not the environment variable.  A child is
    # spawned with its working directory set away from the checkout, so one left
    # to work it out for itself would decide it is an installation, look in
    # `~/.slife2`, find no config there, and run on the built-in defaults — while
    # this process, which read the config it was pointed at, believes otherwise.
    # The symptom is a server that starts fine and immediately reports a config
    # it does not have.
    # `.resolve()` is the load-bearing half of "the resolved directory".  The
    # child is spawned with its working directory set to `<data>/runtime`, so a
    # relative `--data-dir rel/chk` — from the flag or from `SLIFE2_DATA_DIR` —
    # would be re-resolved *underneath that*, pointing the child at a directory
    # that has no config in it while this process reads the one it was given.
    argv += ["--data-dir", str(data_dir().resolve())]
    return argv


def _wait_ready(spec: ServerSpec, proc: subprocess.Popen) -> None:
    """Block until the spawned server answers, or explain why it never did.

    The child is checked on **every** poll, not only at the deadline.  A server
    that dies at import — a bad config, a missing SDK, a port something grabbed
    in the meantime — reports that in about fifty milliseconds with its log, where
    a launcher that only watched the clock would sit out the whole of
    `_T.timeouts.ready.plugin_start` and then blame a timeout.
    """
    deadline = time.monotonic() + _T.timeouts.ready.plugin_start
    delay = 0.05

    while True:
        if proc.poll() is not None:
            raise StartFailed(
                f"{spec.name} exited with code {proc.returncode} before it was ready",
                tail_log(spec.url),
            )
        if probe(spec):
            return
        if time.monotonic() >= deadline:
            # We own a process that has never served anything, so cleaning it up
            # is unambiguous.  A server that *had* served would be someone's.
            terminate(proc.pid)
            # Reaped, because nothing else will: `Popen` holds the child's exit
            # status until somebody waits, and on POSIX a process nobody waits
            # for is a zombie for the life of the waiter — which here is the
            # whole session, since the launcher runs until the TUI exits.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_T.timeouts.ready.reap)
            raise StartFailed(
                f"{spec.name} did not answer within {_T.timeouts.ready.plugin_start:.0f}s",
                tail_log(spec.url),
            )
        time.sleep(delay)
        delay = min(delay * 2, 0.5)


def ensure(spec: ServerSpec, *, config_path: Path | None = None) -> Outcome:
    """Make sure a server is up, reusing one that already is — and time it.

    The bracket is the whole call, one level above every path in `_bring_up`,
    because every path is a way of making the server usable and the question the
    number answers is how long *this* line cost.  Reusing is one of those paths:
    almost every launch finds everything already running, and a warm start that
    says `12ms` per server is the honest report of a start that did nothing.
    """
    started = time.monotonic()
    outcome = _bring_up(spec, config_path=config_path)
    return replace(outcome, seconds=time.monotonic() - started)


def _bring_up(spec: ServerSpec, *, config_path: Path | None = None) -> Outcome:
    """`ensure`'s body, untimed — and the reason it is a function.

    Probed before the lock as well as inside it.  The unlocked probe is the fast
    path — almost every launch finds everything already running and never
    contends — and the probe inside the lock is what makes the wait meaningful:
    whoever held it has just started the server, so the answer changes.

    **The port is asked about inside the lock and not before it**, which is a
    correctness fix rather than a tidying.  A port that is bound and not yet
    answering is what a *concurrent cold start* looks like from here — two
    instances launched together, this one probing while the other's child is
    between `bind` and `serve` — and answering `CONFLICT` there refused to start
    against our own server coming up.  Inside the lock the question means what it
    says: anyone else starting this server holds that lock, so by the time this
    runs, bound-and-silent is somebody else's process and not ours mid-boot.
    """
    if probe(spec):
        return _reuse(spec, config_path=config_path)

    try:
        with start_lock(spec.url):
            if probe(spec):
                return _reuse(spec, config_path=config_path)
            if tcp_listening(spec.host, spec.port):
                return Outcome(spec, Status.CONFLICT, detail=_conflict_detail(spec))

            proc = spawn(_argv(spec), url=spec.url)
            _wait_ready(spec, proc)
            # The record is written by the server itself, not here: it is the
            # only party that knows which config it read, and that is what
            # decides whether another instance may reuse it.
            return Outcome(spec, Status.STARTED, read_record(spec.url))
    except StartFailed as exc:
        return Outcome(spec, Status.FAILED, detail=f"{exc}\n{exc.log_tail}".strip())
    except (TimeoutError, OSError, subprocess.SubprocessError) as exc:
        # `spawn` can fail before there is anything to probe: the log file
        # cannot be opened, or both `Popen` attempts are refused.  Every other
        # path here reports a server's outcome rather than raising, and this
        # was the one that did not — so a start failure arrived as a traceback
        # out of `slife2 run` instead of the line naming the server and why.
        return Outcome(spec, Status.FAILED, detail=f"{type(exc).__name__}: {exc}")


def _reuse(spec: ServerSpec, *, config_path: Path | None = None) -> Outcome:
    """Attach to a server that is already answering.

    **The servers are shared, full stop.**  Which config started one does not
    decide whether another may use it — two instances that happen to name the
    same file are simply using the same configuration, and two that name
    different files still share the servers, because sharing them is the point.

    The record's `config` is still worth reading: a server on this port started
    from a *different* config means two configurations are competing for one
    endpoint, which is worth saying out loud rather than leaving as a mystery
    when the model is not the one expected.
    """
    record = read_record(spec.url)
    detail = ""
    if record is not None and record.config and config_path is not None:
        if Path(record.config) != Path(config_path):
            detail = f"started from {record.config}"
    return Outcome(spec, Status.RUNNING, record, detail)


def _conflict_detail(spec: ServerSpec) -> str:
    return (
        f"port {spec.port} is held by something that is not {spec.name} "
        f"(nothing calling itself {spec.expected_name!r} at {spec.url})"
    )


#: Names claimed in *this* process.
#:
#: The kernel lock alone is not enough.  A Windows named mutex is recursive for
#: the thread that holds it, and a POSIX `flock` belongs to the open file
#: description, so a second claim inside one process quietly succeeds — the lock
#: is a cross-process guard and says nothing about re-entry.  This set supplies
#: the missing half, so "a name is claimed once" is true regardless.
_HELD_NAMES: set[str] = set()


@contextlib.contextmanager
def claim_agent(name: str, *, headless: bool = False) -> Generator[None]:
    """Hold the exclusive right to run under `name` for the life of the client.

    The kernel owns the lock, so an instance that crashes — or is killed —
    releases the name immediately, with no stale-claim cleanup and no window
    where a dead process still owns a name.  A client record is registered
    alongside it so other instances can tell whether anyone else is still using
    the shared servers; see :func:`others_running`.

    `headless` is written into the claim rather than used here, and the claim is
    the only place it can go: the *servers* are what render a system prompt, and
    a prompt that says "nobody is watching" has to be able to find that out from
    a process that was not the one asked (`slife2.libs.host.runtime.AgentClaim.headless`).

    Raises:
        AgentInUse: If another live instance already answers to this name.
    """
    if name in _HELD_NAMES:
        raise AgentInUse(name, read_claim(name))

    # **The acquire is translated on its own, and the caller's body is not
    # inside it.**  This used to be one `try:` around the whole `with
    # exclusive(...)`, `yield` and all, so the `except TimeoutError` covered
    # everything the window ever did: a `TimeoutError` raised anywhere in
    # slife2's start-up or in `app.run()` — `toolclient.remote_tools` asks the
    # hub under `work.hub`, and an ask that runs out of time raises exactly this
    # — came back out as `AgentInUse(name, …) from None`, which says "agent
    # 'slife2' is already running (pid …)" about a name nothing was holding and
    # throws the real failure away.  The docstring above is the contract, and it
    # is about the *claim*: the only thing that can say "another live instance
    # already answers to this name" is `exclusive`'s own timeout.
    held = contextlib.ExitStack()
    try:
        held.enter_context(
            exclusive(agent_key(name), timeout=_T.timeouts.ready.agent_claim)
        )
    except TimeoutError:
        raise AgentInUse(name, read_claim(name)) from None

    with held:
        _HELD_NAMES.add(name)
        try:
            # Everything after the `add` is inside the `try`, including the
            # bookkeeping.  A name that is added and then not released
            # because something between the two raised is a name this
            # process can never claim again — and "released however the
            # body exits" is the whole point of holding it.
            write_claim(AgentClaim.now(name, headless=headless))
            client = register_client(name)
            try:
                yield
            finally:
                unregister_client(client)
        finally:
            clear_claim(name)
            _HELD_NAMES.discard(name)


def others_running() -> list[ClientRecord]:
    """Clients other than this process that are still using the servers.

    Read from pid liveness rather than any counter, so a client that was killed
    — and therefore never got to deregister — does not keep the servers alive
    forever.

    **The pid is the whole of the test, and the start token is not needed here.**
    A record that reached this far already came through `live_clients`, which
    admits one only when the pid is alive *and* the token matches (or is absent)
    — so a record bearing our pid has
    necessarily proved itself to be this process, and one bearing any other pid
    is somebody else by definition.  The token still does its work: it is what
    stops a recycled pid counting as a live *other* client inside `live_clients`.
    """
    return [record for record in live_clients() if record.pid != os.getpid()]


#: The plugins the hub cannot be made to wait for, because each of them waits on
#: the hub: itself, `agent-task` (a task's loop *runs on* the hub's tool list) and
#: `schedule` (its runs are handed to `agent-task`).  Named once, so that the
#: entry below can be the rule rather than a list of names.
_AFTER_THE_HUB: tuple[str, ...] = ("toolhub", "agent-task", "schedule")


#: What a server needs up before it can come up itself.
#:
#: **These are edges, and everything not named here is a preference.**  The rest
#: of `slife2.libs.host.config.LOCAL_SERVERS`' order says only that a model server
#: answering first is what keeps a first turn from failing and retrying, and a
#: preference is not a reason to serialise a launcher — so servers with no entry
#: here all start at once.
#:
#: `context` needs `embeddings` and that one is hard: its lifespan opens the
#: embedder *before it serves anything at all*, and `open_server` refuses a peer
#: that is not listening — so a context started in the same breath as an
#: embeddings is a context that fails to start, not one that starts slowly.
#:
#: **The hub's entry is every plugin it asks, minus the three that reach it**,
#: and it is a comprehension rather than thirteen names because the list did
#: drift: `a2a` landed, the hub asks it for its tools, and the tuple nobody had a
#: reason to revisit said otherwise.  What the edge is for is not
#: politeness — `list_tools` *refuses* while any plugin is not answering
#: (`slife2.plugins.toolhub.server`: a missing hub is a plugin gone and fails the turn), so a
#: plugin that comes up after the hub is a first turn that cannot run.  The hub
#: asks the plugins with nothing to offer the model too, which is why this is the
#: whole set and not the ones with tools.
#:
#: **`agent` is a peer the agent server reaches and this table cannot say.**  Its
#: tool list and its turn log both come from a peer, and the hub is one of them —
#: but the hub asks *it*, so that edge closes a cycle and `ensure_all` would
#: refuse to start.  What makes leaving it out safe is the other half of the same
#: fact: the agent server is inert until a caller arrives, and a caller arrives
#: either from a window, which exists only once every wave is done, or from a
#: plugin that is itself waiting on the hub.  Its peers are up by construction
#: rather than by an edge, which is the weaker guarantee and the honest one.
#:
#: `tests/test_launcher.py` holds this table together: every name is one of ours,
#: nothing here points at itself in a circle, and the hub waits for every plugin
#: it can.
NEEDS: dict[str, tuple[str, ...]] = {
    "context": ("embeddings",),
    # **The window, and not the agent server** (SDD §9.1).  WeChat's poll
    # loop hands a message to the conversation the window is showing, which is
    # the one inbox everything this system takes goes into — and the agent server
    # was the peer named here until that changed, an edge that outlived the code
    # by a release.  Ordering rather than correctness, which is what every edge
    # here is: a login resumed before its peer is up would be a session watching
    # a bridge for messages it cannot deliver, and its own call is what notices.
    "wechat": ("tui",),
    # The cabinet embeds through the embeddings server, like the context store:
    # a write places its vector in the same call, so a cabinet that cannot embed
    # stores nothing.
    "cabinet": ("embeddings",),
    # The background tasks reach four peers rather than being reached by them:
    # the hub for the tools its loop runs with, a model server for the loop
    # itself, the terminal — or, when no window is showing the conversation, the
    # agent server — to hand the answer over, and the context store a task's
    # conversation is restored from, which is the one a task cannot run without
    # at all.  Ordering rather than correctness, like every edge here: a task
    # started before one of these is up fails its own first hop, and that failure
    # looks like the feature not working rather than like a daemon that was still
    # starting.
    "agent-task": ("toolhub", "tui", "agent", "context"),
    # The clock reaches three peers, and needs neither the hub nor a model.  A
    # fire puts a due task in front of the agent through the terminal, or through
    # the agent server when no window is showing that conversation; the turn it
    # opens then calls back into *this* plugin, which hands the work to
    # `agent-task` as a task of the agent's own and asks that peer what became of
    # it.  So the third edge is where the run can actually be closed.  Ordering
    # rather than correctness, like every edge here — a fire that cannot be handed
    # over is a run marked failed, which looks like the feature not working rather
    # than like a daemon that was still starting.
    "schedule": ("tui", "agent", "agent-task"),
    # The mesh reaches two peers and needs neither the hub nor a model: what
    # arrives from a peer is handed to the conversation — the terminal showing
    # it, or the agent server when no window is — through the same `hand_over`
    # every other out-of-band message takes (`slife2.libs.harness.handover`).  Ordering
    # rather than correctness, like every edge here: a message that cannot be
    # handed over is logged and dropped, which looks like the mesh not working
    # rather than like a daemon that was still starting.
    REMOTE.plugin_key: ("tui", "agent"),
    # The local network, for the line above's reason: it hands a member's answer
    # to the conversation the same way, and its start resumes the ledgers it
    # finds.  The two networks are one module, so the edge is one edge — and it
    # is written twice because a *server* is what the launcher orders, and these
    # are two of them.
    LOCAL.key: ("tui", "agent"),
    # The rule and not a list: see `_AFTER_THE_HUB` for the three that cannot be
    # in here.  The model backends are asked too and are deliberately absent —
    # nothing waits on a backend and a backend waits on nothing, so it is in the
    # first wave whatever this table says, which is the preference `config.py`
    # states about them and not a dependency.
    "toolhub": tuple(name for name in LOCAL_SERVERS if name not in _AFTER_THE_HUB),
}


def _concurrently(
    work: Callable[[ServerSpec], Outcome], batch: Sequence[ServerSpec]
) -> Iterator[Outcome]:
    """Run one blocking call per server, and yield each as it finishes.

    **Threads, not tasks.**  Every call this is handed blocks: `probe` builds an
    MCP client and runs its own event loop — it refuses to be called on one that
    is already running — `ensure` spawns a subprocess and waits on it, and
    `terminate` sleeps out a grace period.  There is nothing to await, and a
    thread each is the only way to have them in flight together.

    **Completion order, not `batch` order.**  The caller prints as it goes, so
    what it wants is the slow one appearing while it is still slow; and once
    several are in flight at once, "who finished first" is the only ordering that
    is true.
    """
    with ThreadPoolExecutor(max_workers=max(len(batch), 1)) as pool:
        futures = [pool.submit(work, spec) for spec in batch]
        for future in as_completed(futures):
            yield future.result()


def ensure_all(config: Config, *, config_path: Path | None = None) -> Iterator[Outcome]:
    """Bring up everything this config needs, a wave at a time.

    **A generator, and that is the point.**  Each `ensure` blocks until its
    server answers or gives up, which is a second or two per server and tens of
    seconds together; a caller handed the list at the end has nothing to show
    while it waits, and no way to tell a slow start from one that is stuck on the
    server it is waiting for.  Yielded, the last outcome a caller has *is* the
    answer to "which one is it on" — and it is the one that took the time.

    **The waves come from `NEEDS`.**  Everything a wave does not have to wait for
    starts at once, which for this system is nine of the ten; the one thing worth
    serialising is the two real edges, and a table is how they stop being prose.
    A server whose dependency *failed* is not attempted at all — reported, with
    the name of what it was waiting for, because a second failure describing the
    first one is noise and a start that could not work is a process to clean up.

    **Consuming it is what starts the servers**: nothing here runs until the
    first `next`, so a caller that drops the iterator has started nothing at all.
    That is the price of the streaming, and it is why this is not a lazy
    convenience around an eager function — there is no eager function.
    """
    pending = list(specs(config))
    present = {spec.name for spec in pending}
    done: dict[str, Outcome] = {}

    while pending:
        batch: list[ServerSpec] = []
        for spec in list(pending):
            needs = [dep for dep in NEEDS.get(spec.name, ()) if dep in present]
            failed = [dep for dep in needs if dep in done and not done[dep].ok]
            if failed:
                pending.remove(spec)
                outcome = Outcome(
                    spec,
                    Status.FAILED,
                    detail=f"not attempted: {', '.join(failed)} did not come up",
                )
                done[spec.name] = outcome
                yield outcome
            elif all(dep in done for dep in needs):
                pending.remove(spec)
                batch.append(spec)
        # `pending` and not `batch` being empty is the question: a wave in which
        # every server was *skipped* for a failed dependency has nothing to start
        # and nothing left over, and that is the loop ending rather than a
        # failure.  What cannot happen is work left that nothing will ever
        # release — a name waiting on itself, directly or through others.
        if pending and not batch:
            # The table is asserted acyclic in the tests, so this is a bug rather
            # than a state, and starting them anyway would mean `context` racing
            # the embedder it cannot start without: that fails worse and says
            # less than naming the servers involved.
            raise RuntimeError(
                "the startup graph has a cycle: " + ", ".join(s.name for s in pending)
            )
        for outcome in _concurrently(
            lambda spec: ensure(spec, config_path=config_path), batch
        ):
            done[outcome.spec.name] = outcome
            yield outcome


def statuses(config: Config) -> Iterator[Outcome]:
    """What each server this config needs is doing right now.

    One probe each, all at once.  There is no ordering to keep — a status reads
    what a server *is* and changes nothing — and the cost of a probe is a whole
    MCP handshake, so a server that is hung spends its timeout here: serial, ten
    of those add up, and at once the slowest one sets the wall clock.

    Which makes the rows come out in the order they *answered* rather than the
    config's.  That is the price of the same trade the startup report makes, and
    it is the one worth paying: a hung server is invisible in a listing that
    waits for it, and the ten that answered are already known.
    """
    yield from _concurrently(_status_of, specs(config))


def _status_of(spec: ServerSpec) -> Outcome:
    """One server's status, which is the record's reading and the port's.

    The record is read exactly the way `stop` reads it, and it has to be: a
    server answering with no record of ours was started by hand, so `status`
    calling it merely "running" while `down` refuses to stop it is two commands
    disagreeing about one process.  The `UNMANAGED` branch in `slife2 status`'s
    output — "(not started by slife2)" — existed and could never be reached
    without this.
    """
    if probe(spec):
        record = read_record(spec.url)
        if record is None:
            return Outcome(
                spec, Status.UNMANAGED, detail="no record; not started by slife2"
            )
        return _reuse(spec)
    if tcp_listening(spec.host, spec.port):
        return Outcome(spec, Status.CONFLICT, detail=_conflict_detail(spec))
    return Outcome(spec, Status.NOT_RUNNING)


def stop(config: Config) -> Iterator[Outcome]:
    """Stop the servers this config names, and only ones we can prove are ours.

    "Prove" is doing real work in that sentence.  A record holds a pid, and
    Windows reuses pids, so by the time this runs the number in the file may
    belong to something else entirely.  The start token is what distinguishes
    them; without a match the process is left alone and reported, because
    killing an innocent process is a much worse failure than leaving a daemon
    running.

    All at once, and it is the cleanest case of the three: a stop has no
    ordering to keep at all.  Yielded as each lands for `ensure_all`'s reason —
    the one that is stuck in `terminate` for five seconds should appear while it
    is stuck, which is the opposite of what a list assembled in silence shows.
    """
    yield from _concurrently(_stop_one, specs(config))


def _stop_one(spec: ServerSpec) -> Outcome:
    """One server's stop — and there is nothing here to order.

    Nothing to flush (see `terminate`: these processes are stateless by design
    and are not asked to shut down kindly) and nothing shared between two of
    them: each was spawned into its own process group, so killing one cannot
    reach another, and each record is its own file.  What is *slow* here is
    `terminate`'s grace period, five seconds of it when a process will not go —
    which is why this is worth doing ten at a time rather than ten in a row.
    """
    record = read_record(spec.url)
    if record is None:
        status = Status.UNMANAGED if probe(spec) else Status.NOT_RUNNING
        return Outcome(spec, status, detail="no record; not started by slife2")

    if not same_process(record):
        clear_record(spec.url)
        # `same_process` is False for two situations, and they are not the same
        # sentence.  A pid that is simply gone is the common one — a launcher
        # that killed a server which never became ready leaves exactly this
        # record, and so does a machine that was rebooted — while a pid that
        # belongs to somebody else is the one the start token exists to catch.
        # Calling the first "pid reused" asserts something nobody checked, in
        # the one line a reader gets when a server did not stop.
        why = "the process is gone" if not pid_alive(record.pid) else "pid reused"
        return Outcome(
            spec,
            Status.STOPPED,
            detail=(
                f"record points at pid {record.pid}, which is no longer "
                f"{spec.name} ({why}); left alone"
            ),
        )

    gone = terminate(record.pid)
    if not gone:
        # **The record is the only thing that says the pid is ours, so it stays
        # while the pid does.**  Clearing it here forgot a live process: the next
        # `slife2 down` found no record, answered `UNMANAGED` — "(not started by
        # slife2)" — and never signalled the daemon again, and `slife2 status`
        # said the same about a server that is still listening.  A server that
        # did not exit is precisely the one whose record is still needed; it is
        # cleared when the process really did go, or by the `same_process` branch
        # above once the pid is no longer the one we started.
        return Outcome(
            spec,
            Status.FAILED,
            detail=(
                f"pid {record.pid} did not exit; the record is kept, so "
                f"`down` can find it again"
            ),
        )
    clear_record(spec.url)
    return Outcome(spec, Status.STOPPED)
