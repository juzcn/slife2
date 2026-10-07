"""Bringing up the shared MCP servers, and attaching to the ones already there.

Every server in this system is shared infrastructure.  `slife2` ensures the ones
its config needs are running and attaches to whatever it finds, so a second
instance is one command and no duplicate process appears.  Nothing here is
per-agent: an agent name is a label, and isolation, where it exists at all, is
an MCP server's own business.

The decision that everything else follows from: **a probe is the only authority
on whether a server is running.**  A listening port proves something is there; a
record proves something was there once.  Only `tools/list` answering with the
tool we expect proves that *our* server is up, which is why the record is never
consulted for liveness and a stale one can never wedge a start.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import subprocess
import sys
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from fastmcp import Client

from slife2.config import (
    AGENT_SERVER_NAME,
    API_BACKENDS,
    API_SERVER_NAMES,
    MEMORY_SERVER_NAME,
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

#: The components that are always present: a module, the MCP name that module's
#: server advertises, and the tool that identifies it for a server that reports
#: no name at all.  See `slife2.mcp_server.identifies` for why both are needed.
#:
#: In code rather than in the config.  Letting an operator name an arbitrary
#: command would be a capability nobody asked for and one more thing to get
#: wrong.  The model backends need no entry here — their module *and* their
#: advertised name both come from the `api` they speak, which is already a
#: closed set.
AGENT_SERVER = ("slife2.server.server", AGENT_SERVER_NAME, "send_message")
MEMORY_SERVER = ("slife2.memory_server", MEMORY_SERVER_NAME, "remember")

#: The tool every model server answers to, and how they are named.  One tool
#: and one prefix for all of them, because a backend is a backend whatever
#: protocol it speaks — adding one changes a dict in the config, not this file.
MODEL_TOOL = "stream_chat"
BACKEND_PREFIX = "llm:"

#: How long a server may take to answer after being spawned.  Generous enough
#: for a cold import of a provider SDK, short enough to be a deadline.
READY_TIMEOUT_SECONDS = 30.0

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

    for name, (module, server_name, tool) in (
        ("memory", MEMORY_SERVER),
        ("agent", AGENT_SERVER),
    ):
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
    except Exception:
        return False


def _argv(spec: ServerSpec, config_path: Path | None) -> list[str]:
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
    argv += ["--data-dir", str(data_dir())]
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
            raise StartFailed(
                f"{spec.name} did not answer within {READY_TIMEOUT_SECONDS:.0f}s",
                tail_log(spec.url),
            )
        time.sleep(delay)
        delay = min(delay * 2, 0.5)


def ensure(spec: ServerSpec, *, config_path: Path | None = None) -> Outcome:
    """Make sure a server is up, reusing one that already is.

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

            proc = spawn(_argv(spec, config_path), url=spec.url)
            _wait_ready(spec, proc)
            # The record is written by the server itself, not here: it is the
            # only party that knows which config it read, and that is what
            # decides whether another instance may reuse it.
            return Outcome(spec, Status.STARTED, read_record(spec.url))
    except StartFailed as exc:
        return Outcome(spec, Status.FAILED, detail=f"{exc}\n{exc.log_tail}".strip())
    except TimeoutError as exc:
        return Outcome(spec, Status.FAILED, detail=str(exc))


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
        f"(no {spec.expected_tool!r} tool at {spec.url})"
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


def ensure_all(config: Config, *, config_path: Path | None = None) -> list[Outcome]:
    """Bring up everything this config needs, LLM servers first."""
    return [ensure(spec, config_path=config_path) for spec in specs(config)]


def statuses(config: Config) -> list[Outcome]:
    """What each server this config needs is doing right now."""
    results = []
    for spec in specs(config):
        if probe(spec):
            results.append(_reuse(spec))
        elif tcp_listening(spec.host, spec.port):
            results.append(
                Outcome(spec, Status.CONFLICT, detail=_conflict_detail(spec))
            )
        else:
            results.append(Outcome(spec, Status.NOT_RUNNING))
    return results


def stop(config: Config) -> list[Outcome]:
    """Stop the servers this config names, and only ones we can prove are ours.

    "Prove" is doing real work in that sentence.  A record holds a pid, and
    Windows reuses pids, so by the time this runs the number in the file may
    belong to something else entirely.  The start token is what distinguishes
    them; without a match the process is left alone and reported, because
    killing an innocent process is a much worse failure than leaving a daemon
    running.
    """
    results = []
    for spec in specs(config):
        record = read_record(spec.url)
        if record is None:
            status = Status.UNMANAGED if probe(spec) else Status.NOT_RUNNING
            results.append(
                Outcome(spec, status, detail="no record; not started by slife2")
            )
            continue

        if not same_process(record):
            clear_record(spec.url)
            results.append(
                Outcome(
                    spec,
                    Status.STOPPED,
                    detail=(
                        f"record points at pid {record.pid}, which is no longer "
                        f"{spec.name} (pid reused); left alone"
                    ),
                )
            )
            continue

        gone = terminate(record.pid)
        clear_record(spec.url)
        results.append(
            Outcome(
                spec,
                Status.STOPPED if gone else Status.FAILED,
                detail="" if gone else f"pid {record.pid} did not exit",
            )
        )
    return results
