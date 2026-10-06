"""Platform primitives for managing daemonised MCP servers.

Two ideas run through this module:

**A probe decides whether a server is alive, never a pid.**  Only an MCP
`tools/list` can tell *our* server from any other process holding the port, so
nothing here is on the critical path for correctness.  What lives here is what a
probe cannot answer: which pid to stop, and a lock that stops two launchers
starting the same server at once.

**A lock is a kernel object, not a file we agree to respect.**  A Windows named
mutex and a POSIX `flock` are released by the operating system when the holder
dies, however it dies — a hard kill included.  That removes an entire class of
bug that an `O_EXCL` lockfile has to solve by hand: no stale-lock protocol, no
"is the holder still alive", no window where the answer is wrong.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

#: How long to wait for another launcher to finish starting a server.
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.1

#: A record older than this whose process is gone is simply deleted.
_LOG_KEEP_BYTES = 4 * 1024 * 1024


def runtime_dir() -> Path:
    """Per-user directory for records, locks and logs.

    Deliberately not inside the repository.  A wheel install has no repository,
    two checkouts on one machine would otherwise fight over the same state, and
    a state file that lands in a git working tree is one somebody will
    eventually commit.

    One directory serves every instance on the machine, because the servers are
    shared: two instances that name different config files still mean the same
    servers when those configs name the same ports, and each has to be able to
    see what the other started.
    """
    override = os.environ.get("SLIFE2_RUNTIME_DIR")
    if override:
        return Path(override)

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    else:
        # POSIX prefers XDG_RUNTIME_DIR, which is already private and per-user.
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
        if xdg_runtime:
            return Path(xdg_runtime) / "slife2"
        base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")

    path = Path(base) / "slife2" / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_url(url: str) -> str:
    """Canonical form of an endpoint, so two spellings are one server.

    Records are keyed by this.  Without it ``http://localhost:8001/mcp`` and
    ``http://127.0.0.1:8001/mcp`` would be two servers sharing one port.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "localhost":
        # A convention, not a DNS lookup: for a server on this machine the two
        # spellings mean the same endpoint, and treating them as different
        # would give one port two records — one reporting "running" and the
        # other "held by something else", which is a confusing way to say
        # "these are the same server".
        host = "127.0.0.1"
    port = parts.port
    if port and not (
        (parts.scheme == "http" and port == 80)
        or (parts.scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme.lower()}://{host}{path}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def endpoint_key(url: str) -> str:
    """The short digest that names this endpoint's files."""
    return _digest(normalize_url(url))


def agent_key(name: str) -> str:
    """The digest naming one agent's files.

    Prefixed so an agent called ``http://127.0.0.1:8000/mcp`` cannot collide
    with the server at that address.
    """
    return _digest(f"agent:{name}")


def log_path(url: str) -> Path:
    return _sub("logs") / f"{endpoint_key(url)}.log"


def record_path(url: str) -> Path:
    return _sub("servers") / f"{endpoint_key(url)}.json"


def _lock_path(key: str) -> Path:
    return _sub("locks") / f"{key}.lock"


def agent_claim_path(name: str) -> Path:
    return _sub("agents") / f"{agent_key(name)}.json"


def _sub(name: str) -> Path:
    path = runtime_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def tail_log(url: str, *, lines: int = 20) -> str:
    """The end of a server's log, for putting a real error in a real message."""
    try:
        content = log_path(url).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


@dataclass(frozen=True)
class ServerRecord:
    """What a launcher recorded when it started a server.

    `start_token` is the important field and the reason this is not just a pid.
    Windows reuses pids, so by the time `down` runs, the number in this file may
    belong to somebody else's process.  The token identifies the *instance*.
    """

    name: str
    url: str
    pid: int
    #: Opaque identifier for the process instance, or "" when the platform
    #: offers none.  Empty means "cannot prove this pid is ours", which makes
    #: `down` refuse to signal it.
    start_token: str = ""
    #: Absolute path of the config the launcher used, so a second config on the
    #: same port is detected rather than silently shared.
    config: str = ""
    version: str = ""
    started_at: str = ""

    @classmethod
    def now(
        cls, *, name: str, url: str, pid: int, config: str, version: str
    ) -> ServerRecord:
        return cls(
            name=name,
            url=url,
            pid=pid,
            start_token=process_start_token(pid) or "",
            config=config,
            version=version,
            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )


def read_record(url: str) -> ServerRecord | None:
    """Read a record, or None when it is missing or unreadable.

    A corrupt record is treated as absent rather than raised on: it is
    bookkeeping, and refusing to start because a crash truncated a bookkeeping
    file would be a worse failure than losing a pid.
    """
    try:
        raw = json.loads(record_path(url).read_text(encoding="utf-8"))
        return ServerRecord(
            name=str(raw["name"]),
            url=str(raw["url"]),
            pid=int(raw["pid"]),
            start_token=str(raw.get("start_token") or ""),
            config=str(raw.get("config") or ""),
            version=str(raw.get("version") or ""),
            started_at=str(raw.get("started_at") or ""),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_record(record: ServerRecord) -> None:
    """Record a running server, atomically.

    Written to a temporary file and renamed, so a `status` running concurrently
    can never read a half-written record.
    """
    path = record_path(record.url)
    tmp = path.with_suffix(".tmp")
    with contextlib.suppress(OSError):
        tmp.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        os.replace(tmp, path)


def clear_record(url: str) -> None:
    with contextlib.suppress(OSError):
        record_path(url).unlink(missing_ok=True)


def all_records() -> list[ServerRecord]:
    """Every record in the runtime directory, for `status --all`."""
    records = []
    for path in sorted(_sub("servers").glob("*.json")):
        record = read_record_for_file(path)
        if record is not None:
            records.append(record)
    return records


def read_record_for_file(path: Path) -> ServerRecord | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ServerRecord(
            name=str(raw["name"]),
            url=str(raw["url"]),
            pid=int(raw["pid"]),
            start_token=str(raw.get("start_token") or ""),
            config=str(raw.get("config") or ""),
            version=str(raw.get("version") or ""),
            started_at=str(raw.get("started_at") or ""),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


@dataclass(frozen=True)
class AgentClaim:
    """Who is running under an agent name right now.

    Only ever used to *describe* a conflict.  Whether the name is taken is
    decided by the kernel lock, never by this file, so a stale claim produces a
    slightly wrong pid in an error message and nothing worse.
    """

    name: str
    pid: int
    start_token: str = ""
    started_at: str = ""

    @classmethod
    def now(cls, name: str) -> AgentClaim:
        pid = os.getpid()
        return cls(
            name=name,
            pid=pid,
            start_token=process_start_token(pid) or "",
            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )


def read_claim(name: str) -> AgentClaim | None:
    """Read an agent's claim, or None when there is not a readable one."""
    try:
        raw = json.loads(agent_claim_path(name).read_text(encoding="utf-8"))
        return AgentClaim(
            name=str(raw["name"]),
            pid=int(raw["pid"]),
            start_token=str(raw.get("start_token") or ""),
            started_at=str(raw.get("started_at") or ""),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_claim(claim: AgentClaim) -> None:
    with contextlib.suppress(OSError):
        agent_claim_path(claim.name).write_text(
            json.dumps(asdict(claim), indent=2), encoding="utf-8"
        )


def clear_claim(name: str) -> None:
    with contextlib.suppress(OSError):
        agent_claim_path(name).unlink(missing_ok=True)


@dataclass(frozen=True)
class ClientRecord:
    """A live client, so "am I the last one out?" is answerable.

    Liveness is decided by the pid *and* its start token, so a recycled pid does
    not read as a client that is still here.
    """

    pid: int
    agent: str = ""
    start_token: str = ""

    @classmethod
    def now(cls, agent: str) -> ClientRecord:
        pid = os.getpid()
        return cls(pid=pid, agent=agent, start_token=process_start_token(pid) or "")

    @property
    def path(self) -> Path:
        return _clients_dir() / f"{self.pid}-{self.agent or 'client'}.json"


def _clients_dir() -> Path:
    return _sub("clients")


def register_client(agent: str) -> ClientRecord:
    """Record that this process is using the shared servers."""
    record = ClientRecord.now(agent)
    with contextlib.suppress(OSError):
        record.path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
    return record


def unregister_client(record: ClientRecord) -> None:
    with contextlib.suppress(OSError):
        record.path.unlink(missing_ok=True)


def live_clients() -> list[ClientRecord]:
    """Every registered client that is still actually running.

    Records left by killed clients are removed on the way past, so a crash does
    not keep the servers alive forever.
    """
    alive: list[ClientRecord] = []
    for path in _clients_dir().glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            record = ClientRecord(
                pid=int(raw["pid"]),
                agent=str(raw.get("agent") or ""),
                start_token=str(raw.get("start_token") or ""),
            )
        except (OSError, ValueError, KeyError, TypeError):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            continue

        if pid_alive(record.pid) and (
            not record.start_token
            or process_start_token(record.pid) == record.start_token
        ):
            alive.append(record)
        else:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
    return alive


def same_client(record: ClientRecord) -> bool:
    """Whether a client record refers to this very process."""
    if record.pid != os.getpid():
        return False
    return not record.start_token or record.start_token == process_start_token(
        record.pid
    )


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid currently exists.

    **`os.kill(pid, 0)` must not be used here.**  On Windows, Python's `os.kill`
    does not send signals: any value other than the two console events calls
    `TerminateProcess` unconditionally.  So the usual POSIX idiom would not
    merely fail to check the process — it would *kill* it, which is a memorable
    way to lose the server you were asking about.

    Windows therefore opens the process and waits on it with a zero timeout;
    POSIX uses the signal-0 form, where it does mean what it looks like.
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _start_token_windows(pid: int) -> str | None:
    """The kernel's creation time for a process, as a FILETIME pair."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(wintypes.DWORD()),
            ctypes.byref(wintypes.DWORD()),
        )
        return f"{created.dwHighDateTime:08x}{created.dwLowDateTime:08x}"
    except OSError:
        return None
    finally:
        kernel32.CloseHandle(handle)


def _start_token_linux(pid: int) -> str | None:
    """Field 22 of `/proc/<pid>/stat`, the process start time in clock ticks."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # Field 2 is the command name and may contain spaces or brackets, so
        # the split happens after the last ')' rather than on whitespace.
        after_name = stat.rsplit(")", 1)[1].split()
        return after_name[19]
    except (OSError, IndexError, ValueError):
        return None


def _start_token_ps(pid: int) -> str | None:
    """`ps -o lstart=`, for macOS and the BSDs, which have no /proc.

    Second-resolution, and worth being precise about what that costs: a false
    match needs a recycled pid belonging to a process that started in the
    *same second* as the daemon we recorded.  The impostor necessarily started
    after our daemon exited, so that requires our daemon to have lived under a
    second — and these are servers, not one-shots.

    The alternative is reading `kinfo_proc` through ctypes, whose layout is
    exactly the thing that changes between macOS releases.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    stamp = result.stdout.strip()
    return _digest(stamp) if stamp else None


#: One token source per platform.  Split out rather than inlined into a single
#: branchy function so each is callable directly, which is the only way the
#: macOS path can be tested on the other two thirds of the CI matrix.
_START_TOKEN_SOURCES = {
    "win32": _start_token_windows,
    "linux": _start_token_linux,
}


def process_start_token(pid: int) -> str | None:
    """An identifier for *this instance* of a pid, or None if unavailable.

    The kernel records when each process started, and a recycled pid gets a new
    value, so comparing tokens distinguishes "the server we started" from "an
    unrelated process that inherited the number".  It is the difference between
    `down` stopping a daemon and `down` killing whatever the user happened to be
    running.

    Returning None is a normal outcome, not a failure: the caller treats it as
    "cannot prove", which is the safe direction.
    """
    if pid <= 0:
        return None
    source = _START_TOKEN_SOURCES.get(sys.platform)
    if source is not None:
        return source(pid)
    return _start_token_ps(pid)  # macOS, the BSDs, anything else


def same_process(record: ServerRecord) -> bool:
    """Whether the pid in a record still refers to the process we started.

    False when the token is missing on either side: an unprovable match is
    treated as no match, so `down` errs towards leaving a process alone.
    """
    if not record.start_token:
        return False
    if not pid_alive(record.pid):
        return False
    current = process_start_token(record.pid)
    return current is not None and current == record.start_token


def tcp_listening(host: str, port: int, *, timeout: float = 0.25) -> bool:
    """Whether anything accepts a connection here.

    A cheap gate in front of the MCP probe, so the common "nothing is there"
    case costs a quarter second instead of a full probe timeout.  It answers
    "is something there", never "is it ours" — the probe does that.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def exclusive(key: str, *, timeout: float = LOCK_TIMEOUT_SECONDS) -> Generator[None]:
    """Hold a named lock, released by the OS if the holder dies.

    The primitive behind both the per-server start lock and the per-agent name
    claim.  Because the kernel owns it, there is no stale-lock protocol to get
    wrong: no "is the holder still alive", no timeout that races a crash, and
    no window where the answer is wrong.

    Raises:
        TimeoutError: If someone else holds it for longer than `timeout`.
    """
    if sys.platform == "win32":
        with _windows_mutex(key, timeout):
            yield
        return

    with _posix_flock(key, timeout):
        yield


@contextlib.contextmanager
def start_lock(url: str, *, timeout: float = LOCK_TIMEOUT_SECONDS) -> Generator[None]:
    """Hold the exclusive right to start the server at `url`.

    Probe, spawn and wait-for-ready all happen inside this, so two launchers
    racing on a cold start cannot both decide the server is missing.  The one
    that waits re-probes once it gets in and finds the server already up, which
    is why `ensure` probes again inside the lock rather than trusting its first
    look.
    """
    with exclusive(endpoint_key(url), timeout=timeout):
        yield


@contextlib.contextmanager
def _windows_mutex(key: str, timeout: float):
    import ctypes

    WAIT_OBJECT_0 = 0x00000000
    WAIT_ABANDONED = 0x00000080
    WAIT_TIMEOUT = 0x00000102

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    # `Local\` rather than `Global\`: a global name needs SeCreateGlobalPrivilege,
    # which a standard user does not have.  The trade is that two Windows
    # *sessions* (an RDP session and the console) could both acquire it; the TCP
    # port remains the backstop for that case, and it is rare enough not to buy
    # a privilege for.
    name = f"Local\\slife2-{key}"
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise OSError("could not create the start mutex")

    acquired = False
    try:
        result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
        if result == WAIT_TIMEOUT:
            raise TimeoutError(f"timed out waiting for {key!r} to be released")
        # WAIT_ABANDONED means the previous holder died mid-start.  The OS
        # released the mutex on our behalf, so this is a success, not an error.
        acquired = result in (WAIT_OBJECT_0, WAIT_ABANDONED)
        if not acquired:
            raise OSError(f"could not acquire the start mutex (result {result})")
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


@contextlib.contextmanager
def _posix_flock(key: str, timeout: float):
    # Imported here so the module still imports on Windows.
    import fcntl

    path = _lock_path(key)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                # `fcntl` has no stubs on Windows, which is where this project
                # is developed — hence the ignores on a branch that cannot run
                # there.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for {key!r} to be released"
                    ) from None
                time.sleep(LOCK_POLL_SECONDS)
        yield
    finally:
        os.close(fd)  # also releases the flock


def spawn(argv: list[str], *, url: str) -> subprocess.Popen:
    """Start a server as a daemon that outlives this process.

    Two Windows flags, and both were chosen by measurement rather than by
    reading, because the plausible-looking choice is wrong here:

    * **`CREATE_NO_WINDOW`, not `DETACHED_PROCESS`.**  `DETACHED_PROCESS` sounds
      like "no console", and is not: it means the child does not *inherit* the
      parent's console, and a console-subsystem program still ends up with one.
      Measured with `GetConsoleWindow()` in the child, `DETACHED_PROCESS` gives a
      live window handle (~723768) and `CREATE_NO_WINDOW` gives 0.  The two are
      not even mutually exclusive — passing both is accepted, and the window
      comes back, so `DETACHED_PROCESS` wins the console allocation.  A daemon
      that flashes a window on every start is not a daemon.
    * **`CREATE_BREAKAWAY_FROM_JOB`.**  A child inherits its parent's job object
      unless it asks not to.  If the client is itself inside a kill-on-close job
      — a CI runner, some terminal and IDE task hosts — then the child dies with
      that job, silently defeating the whole point.  Breakaway takes it out; the
      `except` below covers a job that forbids breakaway, because losing the
      escape hatch beats failing to start.

    Also `cwd` is set away from the repository: a daemon must not hold a working
    directory open, and `--config` is absolute, so moving it cannot change which
    config the child reads.
    """
    log = log_path(url)
    _truncate_if_huge(log)
    sink = open(log, "ab", buffering=0)  # noqa: SIM115 - the child inherits it
    try:
        common: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": sink,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
            "cwd": str(runtime_dir()),
        }
        if sys.platform == "win32":
            base = subprocess.CREATE_NO_WINDOW
            try:
                return subprocess.Popen(
                    argv,
                    creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                    **common,  # type: ignore[arg-type]
                )
            except OSError:
                # The surrounding job forbids breakaway.  Losing the escape
                # hatch is better than failing to start at all.
                return subprocess.Popen(
                    argv,
                    creationflags=base,
                    **common,  # type: ignore[arg-type]
                )
        return subprocess.Popen(
            argv,
            start_new_session=True,
            **common,  # type: ignore[arg-type]
        )
    finally:
        sink.close()


def _truncate_if_huge(path: Path) -> None:
    """Keep a daemon's log from growing without bound across restarts."""
    with contextlib.suppress(OSError):
        if path.exists() and path.stat().st_size > _LOG_KEEP_BYTES:
            path.unlink()


def terminate(pid: int, *, grace: float = 5.0) -> bool:
    """Stop a process and its children, returning whether it is gone.

    Deliberately blunt.  These processes have no console to receive a graceful
    signal and hold no state worth flushing — they are stateless by design — so
    a kind shutdown would only be ceremony.
    """
    if not pid_alive(pid):
        return True

    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=grace + 5,
            )
    else:
        import signal

        with contextlib.suppress(ProcessLookupError, PermissionError):
            # `start_new_session=True` made the child a process-group leader, so
            # the group reaches anything it spawned in turn.
            os.killpg(pid, signal.SIGTERM)

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)

    if sys.platform != "win32":
        import signal

        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
        time.sleep(0.2)

    return not pid_alive(pid)
