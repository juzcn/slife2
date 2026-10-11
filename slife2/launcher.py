"""Bringing up the shared MCP servers, and attaching to the ones already there.

Every server in this system is shared infrastructure.  `slife2` ensures the ones
its config needs are running and attaches to whatever it finds, so a second
instance is one command and no duplicate process appears.  Nothing here is
per-agent: an agent name is a label, and isolation, where it exists at all, is
an MCP server's own business.

The decision that everything else follows from: **a probe is the only authority
on whether a server is running.**  A listening port proves something is there; a
record proves something was there once.  Only a server that answers *as itself*
proves that *our* server is up — its advertised name, from the handshake, or a
tool it certainly serves when it reports no name (`slife2.mcp_server.identifies`)
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

from slife2.config import (
    AGENT_SERVER_NAME,
    API_BACKENDS,
    API_SERVER_NAMES,
    CLI_SERVER_NAME,
    CONTEXT_SERVER_NAME,
    EMBEDDINGS_SERVER_NAME,
    LOCAL_SERVERS,
    MCP_TOOLS_SERVER_NAME,
    RESTAPI_TOOLS_SERVER_NAME,
    SKILLS_SERVER_NAME,
    TOOLHUB_SERVER_NAME,
    Config,
)
from slife2.mcp_server import identifies
from slife2.paths import data_dir
from slife2.runtime import (
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
    same_client,
    same_process,
    spawn,
    start_lock,
    tail_log,
    tcp_listening,
    terminate,
    unregister_client,
    write_claim,
)

#: The plugins that are not model backends, keyed by the name the config's
#: `servers:` section uses for each: the module that serves it, the MCP name
#: that module advertises, and the tool that identifies it for a server that
#: reports no name at all.  See `slife2.mcp_server.identifies` for why both are
#: needed.
#:
#: In code rather than in the config.  Letting an operator name an arbitrary
#: command would be a capability nobody asked for and one more thing to get
#: wrong.  The model backends need no entry here — their module *and* their
#: advertised name both come from the `api` they speak, which is already a
#: closed set.
#:
#: The *order* these start in is `slife2.config.LOCAL_SERVERS`, so that what a
#: plugin is and when it starts are each said once; `tests/test_launcher.py`
#: holds the two together.
AGENT_SERVER = ("slife2.server.server", AGENT_SERVER_NAME, "send_message")
#: A conversation's turn log, and the decision about which of its turns a turn
#: runs on.  It identifies itself by `remember`, which is the write everything
#: else about a conversation follows from.
CONTEXT_SERVER = ("slife2.context_server", CONTEXT_SERVER_NAME, "remember")
#: The two plugins whose families are not tools: a folder of playbooks and a
#: list of programs already installed.  Their identifying tool is what the
#: fallback check looks for when a server reports no name of its own, so it has
#: to be one they certainly serve — `cli-server` offers the model nothing at
#: all, and `list_sources` is the one tool it has.
SKILLS_SERVER = ("slife2.skills_server", SKILLS_SERVER_NAME, "skill_use")
CLI_SERVER = ("slife2.cli_server", CLI_SERVER_NAME, "list_sources")
#: The two that hold somebody else's servers, and identify themselves by the
#: tool they hold them *with*: neither offers the model anything.
MCP_TOOLS = ("slife2.mcp_tools", MCP_TOOLS_SERVER_NAME, "list_sources")
RESTAPI_TOOLS = ("slife2.restapi_tools", RESTAPI_TOOLS_SERVER_NAME, "list_sources")
TOOLHUB_SERVER = ("slife2.toolhub", TOOLHUB_SERVER_NAME, "list_tools")
#: Not a chat backend, though it lives beside them: it speaks the OpenAI
#: protocol's `/embeddings` and nothing else, is configured under `embeddings:`
#: rather than under `providers:`, and is identified by `describe` — the tool
#: the db calls before it can build an index.  See `slife2.llm.embeddings_server`.
EMBEDDINGS_SERVER = ("slife2.llm.embeddings_server", EMBEDDINGS_SERVER_NAME, "describe")

SERVER_MODULES: dict[str, tuple[str, str, str]] = {
    "embeddings": EMBEDDINGS_SERVER,
    "context": CONTEXT_SERVER,
    "skills-server": SKILLS_SERVER,
    "cli-server": CLI_SERVER,
    "mcp-tools": MCP_TOOLS,
    "restapi-tools": RESTAPI_TOOLS,
    "toolhub": TOOLHUB_SERVER,
    "agent": AGENT_SERVER,
}

#: The tool every model server answers to, and how they are named.  One tool
#: and one prefix for all of them, because a backend is a backend whatever
#: protocol it speaks — adding one changes a dict in the config, not this file.
MODEL_TOOL = "stream_chat"
BACKEND_PREFIX = "llm:"

#: How long a server may take to answer after being spawned.  Generous enough
#: for a cold import of a provider SDK, short enough to be a deadline.
#:
#: **Ninety, and the number was raised from thirty by measurement.**  Every
#: server here imports the MCP stack before it can serve anything, and that
#: import is most of the budget: `import slife2.toolhub` costs 5.8s in a process
#: of its own and 15-17s each when nine of them are started at once, on a
#: four-core machine with nothing else running.  Thirty was below that floor, so
#: the deadline was being decided by how busy the launcher had made the machine
#: rather than by whether a server was coming up — and the one that lost was
#: `toolhub`, which starts last (it waits on seven plugins; see `NEEDS`) and does
#: the most before it serves (a catalogue index, then ten plugin asks): starved
#: past 26s before it logged its first line, killed about a second short.  A
#: deadline has to clear the floor before it means anything.
READY_TIMEOUT_SECONDS = 90.0

#: How long to wait for one `tools/list`.  Long enough that a slow but real
#: server is not misread as absent.
PROBE_TIMEOUT_SECONDS = 2.0

#: How long to wait for another instance to release an agent name.  Short on
#: purpose: unlike starting a server, there is nothing to wait for — a name is
#: either free or it is not — so a long wait would only delay a refusal.
AGENT_CLAIM_TIMEOUT_SECONDS = 0.2


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
    #: `slife2.mcp_server.identifies`.
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
    result: list[ServerSpec] = []
    for api in config.apis_in_use():
        address = config.server(api)
        result.append(
            ServerSpec(
                name=f"{BACKEND_PREFIX}{api}",
                module=API_BACKENDS[api],
                url=address.url,
                host=address.host,
                port=address.port,
                expected_name=API_SERVER_NAMES[api],
                expected_tool=MODEL_TOOL,
            )
        )

    for name in LOCAL_SERVERS:
        module, server_name, tool = SERVER_MODULES[name]
        address = config.server(name)
        result.append(
            ServerSpec(
                name=name,
                module=module,
                url=address.url,
                host=address.host,
                port=address.port,
                expected_name=server_name,
                expected_tool=tool,
            )
        )
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


def probe(spec: ServerSpec, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Whether the server at `spec.url` is up *and* is the one we expect.

    Identity comes from the handshake, which is what makes this better than a
    connection test: it catches the port being held by a different MCP server,
    which a connect would wave through and which would then fail in the middle
    of a turn.  See `slife2.mcp_server.identifies`.

    Takes the whole spec rather than a URL and a name so the two cannot be
    paired up wrongly at a call site — there are five of them, and a mismatched
    pair would report a perfectly good server as absent.

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
        return asyncio.run(_probe_async(spec, timeout))
    except Exception:  # noqa: BLE001 - a probe answers yes or no; a peer that threw is not up
        return False


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
    a launcher that only watched the clock would sit out the full thirty seconds
    and then blame a timeout.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
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
                proc.wait(timeout=5.0)
            raise StartFailed(
                f"{spec.name} did not answer within {READY_TIMEOUT_SECONDS:.0f}s",
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
    """
    if probe(spec):
        return _reuse(spec, config_path=config_path)

    if tcp_listening(spec.host, spec.port):
        # Something is there and it is not us.  Spawning would fail to bind and
        # surface as a traceback in a log nobody is reading.
        return Outcome(spec, Status.CONFLICT, detail=_conflict_detail(spec))

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
def claim_agent(name: str) -> Generator[None]:
    """Hold the exclusive right to run under `name` for the life of the client.

    The kernel owns the lock, so an instance that crashes — or is killed —
    releases the name immediately, with no stale-claim cleanup and no window
    where a dead process still owns a name.  A client record is registered
    alongside it so other instances can tell whether anyone else is still using
    the shared servers; see :func:`others_running`.

    Raises:
        AgentInUse: If another live instance already answers to this name.
    """
    if name in _HELD_NAMES:
        raise AgentInUse(name, read_claim(name))

    try:
        with exclusive(agent_key(name), timeout=AGENT_CLAIM_TIMEOUT_SECONDS):
            _HELD_NAMES.add(name)
            try:
                # Everything after the `add` is inside the `try`, including the
                # bookkeeping.  A name that is added and then not released
                # because something between the two raised is a name this
                # process can never claim again — and "released however the
                # body exits" is the whole point of holding it.
                write_claim(AgentClaim.now(name))
                client = register_client(name)
                try:
                    yield
                finally:
                    unregister_client(client)
            finally:
                clear_claim(name)
                _HELD_NAMES.discard(name)
    except TimeoutError:
        raise AgentInUse(name, read_claim(name)) from None


def others_running() -> list[ClientRecord]:
    """Clients other than this process that are still using the servers.

    Read from pid liveness rather than any counter, so a client that was killed
    — and therefore never got to deregister — does not keep the servers alive
    forever.  The start token guards against a recycled pid counting as a live
    client.
    """
    return [
        record
        for record in live_clients()
        if record.pid != os.getpid() or not same_client(record)
    ]


#: What a server needs up before it can come up itself.
#:
#: **These are edges, and everything not named here is a preference.**  The rest
#: of `slife2.config.LOCAL_SERVERS`' order says only that a model server
#: answering first is what keeps a first turn from failing and retrying, and a
#: preference is not a reason to serialise a launcher — so servers with no entry
#: here all start at once.
#:
#: `context` needs `embeddings` and that one is hard: its lifespan opens the
#: embedder *before it serves anything at all*, and `open_server` refuses a peer
#: that is not listening — so a context started in the same breath as an
#: embeddings is a context that fails to start, not one that starts slowly.
#:
#: `toolhub` needs every plugin it asks for tools — including `context`, whose
#: `turn_list` and `turn_read` are the model's.  That edge is soft, and it is
#: kept anyway: `settle`'s bounded wait means a late plugin costs the hub
#: catalogue *rows* rather than a crash, which is exactly the failure that would
#: go unnoticed — a first turn answered with tools missing.
#:
#: `tests/test_launcher.py` holds this table together: every name is one of ours
#: and nothing here points at itself in a circle.
NEEDS: dict[str, tuple[str, ...]] = {
    "context": ("embeddings",),
    "toolhub": (
        "embeddings",
        "context",
        "skills-server",
        "cli-server",
        "mcp-tools",
        "restapi-tools",
    ),
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
    clear_record(spec.url)
    return Outcome(
        spec,
        Status.STOPPED if gone else Status.FAILED,
        detail="" if gone else f"pid {record.pid} did not exit",
    )
