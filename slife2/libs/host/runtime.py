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
import json
import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import slife2.helpers.timeouts as _T
from slife2.helpers.clock import now
from slife2.libs.host.paths import digest, runtime_dir

logger = logging.getLogger(__name__)

#: How large a daemon's log may grow before the next start truncates it.  Logs
#: here are diagnostics nobody is expected to read in full, so the ceiling is a
#: "do not fill the disk" bound rather than a retention policy.
_LOG_KEEP_BYTES = 4 * 1024 * 1024


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


def endpoint_key(url: str) -> str:
    """The short digest that names this endpoint's files."""
    return digest(normalize_url(url))


def agent_key(name: str) -> str:
    """The digest naming one agent's files.

    Prefixed so an agent called ``http://127.0.0.1:8000/mcp`` cannot collide
    with the server at that address.
    """
    return digest(f"agent:{name}")


def config_key(path: Path) -> str:
    """The digest naming one config file's write lock.

    Digested rather than the file's name, and for the reason the two above are:
    a key reaches the OS as a file name (POSIX) or a mutex name (Windows), and a
    path is neither.  Prefixed so a config file called like an endpoint cannot
    share a lock with it — the same collision `agent_key` is spelled out against.
    """
    return digest(f"config:{path}")


def log_path(url: str) -> Path:
    return _sub("logs") / f"{endpoint_key(url)}.log"


def record_path(url: str) -> Path:
    return _sub("servers") / f"{endpoint_key(url)}.json"


def _lock_path(key: str) -> Path:
    return _sub("locks") / f"{key}.lock"


def agent_claim_path(name: str) -> Path:
    """The file holding one agent name's claim — **the name, and nothing else**.

    **A name decides everything, and two agents may not share one.**  So this is
    the machine's, not a home's: one live instance per name on this machine,
    whatever data directory it was started in.  A second `jack` — in another
    home, in another terminal — is refused, and the refusal is the same fact the
    mesh states when it will not put a second `jack` online
    (`slife2.plugins.a2a.server.join`): whatever an agent's name picks out, it
    picks out once.

    I had this keyed by `(home, name)` for a while, on the reasoning that two
    data directories hold two agents; the designer settled it the other way, and
    the code is the smaller for it.  Which home a name was started in is where
    its files are, not part of who it is.
    """
    return _sub("agents") / f"{agent_key(name)}.json"


def _sub(name: str) -> Path:
    path = runtime_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def tail_log(url: str, *, lines: int = 20) -> str:
    """The end of a server's log, for putting a real error in a real message.

    **A log that cannot be read is not a log that is empty.**  The caller pastes
    this into a `StartFailed` text, so answering "" would say the server printed
    nothing — a confident negative read off a file that could not be opened,
    which is the deformation DESIGN.md §10.1 names.  The failure is named in the
    text instead, so "it printed nothing" and "the log could not be read" do not
    arrive as one message.
    """
    try:
        content = log_path(url).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(the log at {log_path(url)} could not be read: {exc})"
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
            started_at=now(),
        )


def _read_json_dataclass[RecordT](
    path: Path, factory: Callable[[dict[str, Any]], RecordT]
) -> RecordT | None:
    """Read the JSON object at `path` and hand it to `factory`, or None.

    **A file that is missing, truncated or malformed reads as absent, not as an
    error.**  Both files this reads are bookkeeping — a record names a pid, a
    claim names a holder — and refusing to start because a crash truncated one
    would be a worse failure than losing the number it held.  `factory` is the
    half each caller answers for itself: which fields it needs, and which a
    release older than this one may have written without.

    **This is a registered fallback, and its cost is real**: a record whose
    damage is read as absence makes a running server look record-less, and
    `slife2 down` then has no pid to stop.  *Gives up:* the pid a damaged record
    held.  *Does not cover:* nothing — this is the floor, and a caller that
    knows the file must exist (`read_claim`, `read_record`) still only learns
    that it does not.  *Failure goes:* nowhere, which is why the process that
    wrote the file is the one to read the warning `_write_json_atomic` logs;
    flag for `DESIGN.md` §10.2.

    The broad `except` is the point rather than a shortcut, and each arm is a
    different way the file fails to be one: `read_text` raises `OSError` when it
    is not there, `json` raises `ValueError` when the text is not JSON, and
    `factory` raises `KeyError`/`TypeError`/`ValueError` when the object is not
    the shape it expects.  All four mean the same thing to a reader here.
    """
    try:
        return factory(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_json_atomic(path: Path, obj: Any) -> None:
    """Serialise `obj`'s dataclass fields to `path`, atomically and quietly.

    **Written to a temporary file and renamed**, so a reader running
    concurrently — every `status`, or another instance's claim check — can never
    catch a half-written file.  The rename is atomic on both platforms this runs
    on, so a reader sees the whole of the old file or the whole of the new one;
    `write_claim` argues what catching the middle would cost, and it is why that
    file of all of them has to come through here.

    **The write is still allowed to fail, but not silently** — a registered
    fallback.  Raising would turn a full disk into a `status` that crashes and a
    start that refuses to run, so the caller is not asked to fail over; what it
    must not do is vanish, because a server that started but whose record never
    landed is invisible to `stop` and is then orphaned.  *Gives up:* the record,
    at the moment it was written.  *Does not cover:* nothing — the write is
    either complete or not attempted.  *Failure goes:* a warning naming the
    path, so the orphan is findable in the log; flag for `DESIGN.md` §10.2.
    """
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(asdict(obj), indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning(
            "could not write %s (%s); whatever it records is now invisible to "
            "`stop` and may be orphaned",
            path,
            exc,
        )


def read_record(url: str) -> ServerRecord | None:
    """Read a record, or None when it is missing, unreadable or corrupt.

    The reading rules are `_read_json_dataclass`'s; what belongs to a record is
    its shape — which fields it must carry, and which an older release may have
    left out.
    """
    return _read_json_dataclass(
        record_path(url),
        lambda raw: ServerRecord(
            name=str(raw["name"]),
            url=str(raw["url"]),
            pid=int(raw["pid"]),
            start_token=str(raw.get("start_token") or ""),
            config=str(raw.get("config") or ""),
            version=str(raw.get("version") or ""),
            started_at=str(raw.get("started_at") or ""),
        ),
    )


def write_record(record: ServerRecord) -> None:
    """Record a running server, atomically — `_write_json_atomic` is the how."""
    _write_json_atomic(record_path(record.url), record)


def _remove(path: Path) -> None:
    """Delete a bookkeeping file, tolerating one that is already gone.

    **A delete of a file that is not there is not a failure** — a second `stop`,
    or a record `live_clients` already cleaned up on the way past, would
    otherwise make an idempotent teardown raise.  That tolerance is the whole of
    the fallback, and it is registered: *what it does* is treat an absent file
    as already removed; *it gives up* the distinction between a file that was
    there and one that was not, so a caller cannot tell it removed anything;
    *it does not cover* an unlink that fails for another reason — permissions, a
    directory in the way — which is logged; *the failure goes* to that warning,
    naming the path (DESIGN.md §10.2).  Only the failed unlink is reported,
    because otherwise the file the caller asked to remove would survive with
    nothing said.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "could not remove %s (%s); it survives the delete that asked for it",
            path,
            exc,
        )


def clear_record(url: str) -> None:
    """Remove a server's record; the tolerance for an absent one is `_remove`'s."""
    _remove(record_path(url))


@dataclass(frozen=True)
class AgentClaim:
    """Who is running under an agent name right now.

    Almost only used to *describe* a conflict.  Whether the name is taken is
    decided by the kernel lock, never by this file, so a stale claim produces a
    slightly wrong pid in an error message and nothing worse — and the one
    exception is the one field something *acts* on rather than reports
    (`headless`), where the same staleness costs a model a question nobody can
    answer.  That is the price of a prompt being rendered by a process that was
    not there when the flag was given.
    """

    name: str
    pid: int
    start_token: str = ""
    started_at: str = ""
    #: Whether the instance holding the name is running **without a terminal**
    #: (`slife2 --headless`).  Not a fact about the conflict this record mostly
    #: describes, but the only place it is written down: the claim already
    #: answers "which instance is this", and one instance per name is what makes
    #: the answer worth having.  The agent server is the reader — a system prompt
    #: is where a model is told who it is talking to, and that process never saw
    #: the command line (`slife2.plugins.agent.opening_messages`).
    headless: bool = False

    @classmethod
    def now(cls, name: str, *, headless: bool = False) -> AgentClaim:
        pid = os.getpid()
        return cls(
            name=name,
            pid=pid,
            start_token=process_start_token(pid) or "",
            started_at=now(),
            headless=headless,
        )


def read_claim(name: str) -> AgentClaim | None:
    """Read an agent's claim, or None when there is not a readable one."""
    return _read_json_dataclass(
        agent_claim_path(name),
        lambda raw: AgentClaim(
            name=str(raw["name"]),
            pid=int(raw["pid"]),
            start_token=str(raw.get("start_token") or ""),
            started_at=str(raw.get("started_at") or ""),
            # `.get`, like the two above it: a claim written by a release that
            # did not have this field is a claim from an instance that had a
            # terminal, which is what an absent field means.
            headless=bool(raw.get("headless")),
        ),
    )


def write_claim(claim: AgentClaim) -> None:
    """Claim an agent name, atomically.

    `_write_json_atomic` is the mechanic; what has to stay said here is *why
    this file of all of them* needs it.  This is the one two instances race
    over — `--agent` is exclusive, and the loser is decided by what it reads
    here — so a reader that catches a half-written claim does not compare
    against a name, it fails to parse one and takes the name for free.  The
    window is small and the failure is exactly the one this file exists to
    prevent.
    """
    _write_json_atomic(agent_claim_path(claim.name), claim)


def clear_claim(name: str) -> None:
    """Release an agent's claim; the tolerance for an absent one is `_remove`'s."""
    _remove(agent_claim_path(name))


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
        """The file naming this client, with the agent name digested.

        Not the name itself, which arrives from `--agent` on a command line and
        is only conservative *by the db's rule* — `slife2.libs.store.db` replaces what it
        cannot put in a filename.  A name this property did not sanitise would
        be a record `live_clients`' `*.json` glob never returns: on Windows the
        colon in `a:b` makes the write an alternate data stream, on POSIX a
        slash makes it fail outright, and `register_client` suppresses the
        `OSError` either way.  The instance would then be uncounted, and the
        last client out would stop the shared servers underneath it.  The name
        is in the record's own body, which is where every reader takes it from.
        """
        return _clients_dir() / f"{self.pid}-{agent_key(self.agent)}.json"


def _clients_dir() -> Path:
    return _sub("clients")


def register_client(agent: str) -> ClientRecord:
    """Record that this process is using the shared servers.

    Written through `_write_json_atomic`, like the record and the claim: it is
    the third bookkeeping file and the one that decides whether the last client
    out stops the servers, so a reader catching a half-written file would count
    a live client as absent.
    """
    record = ClientRecord.now(agent)
    _write_json_atomic(record.path, record)
    return record


def unregister_client(record: ClientRecord) -> None:
    """Stop counting a client; the tolerance for an absent record is `_remove`'s."""
    _remove(record.path)


def _pid_in_client_name(path: Path) -> int | None:
    """The pid `ClientRecord.path` wrote into a client record's file name.

    The name is `{pid}-{agent_key}.json`, so a record whose body is damaged
    still says whose it is — which is the half `live_clients` needs to tell a
    live client it cannot read from a dead client's leftover.
    """
    head, _, _ = path.name.partition("-")
    try:
        return int(head)
    except ValueError:
        return None


def live_clients() -> list[ClientRecord]:
    """Every registered client that is still actually running.

    Records left by killed clients are removed on the way past, so a crash does
    not keep the servers alive forever.

    **A record that cannot be parsed is not a client that is gone.**  Deleting
    one makes a live client uncounted, and the count is what "the last one out
    stops the shared servers" rests on, so a file whose body is damaged is kept
    and counted whenever the pid in its *name* is alive (DESIGN.md §10.1).  Only
    a dead pid is a crash to clean up, and only then is the file removed — by
    `_remove`, so a sweep whose delete failed is logged rather than silent.
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
        except (OSError, ValueError, KeyError, TypeError) as exc:
            pid = _pid_in_client_name(path)
            if pid is not None and pid_alive(pid):
                # The name says whose it is; the body is unreadable.  Count it
                # with an empty token, which the rule below already takes at its
                # word — dropping it would stop the servers under a live client.
                logger.warning(
                    "client record %s cannot be read (%s); counting pid %d as a "
                    "live client rather than deleting its record",
                    path,
                    exc,
                    pid,
                )
                alive.append(ClientRecord(pid=pid))
                continue
            _remove(path)
            continue

        # Lenient where `same_process` is strict, and deliberately: the two
        # ask different questions.  This one asks "may the servers stay up for
        # this client?", and the wrong answer is stopping them under somebody
        # who is still working — so a record with no token to compare takes the
        # live pid at its word, where `same_process` would refuse to prove
        # anything.  That is the price of a token source being unavailable on
        # this host: a record left by a killed client whose pid was then reused
        # keeps the servers alive a little longer than it should, which costs a
        # daemon nobody is using rather than a session somebody is in.
        if pid_alive(record.pid) and (
            not record.start_token
            or process_start_token(record.pid) == record.start_token
        ):
            alive.append(record)
        else:
            _remove(path)
    return alive


def _zombie_linux(pid: int) -> bool:
    """Field 3 of `/proc/<pid>/stat`, which is `Z` for a process that has exited."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    # Field 2 is the command name, in brackets, and may hold spaces or brackets
    # of its own, so the split happens after the *last* `)` — the field after it
    # is field 3.  `_start_token_linux` splits the same way, for the same reason.
    try:
        state = stat.rsplit(")", 1)[1].split()[0]
    except IndexError:
        return False
    return state.startswith("Z")


def _zombie_ps(pid: int) -> bool:
    """`ps -o state=`, for macOS and the BSDs, which have no `/proc`.

    The letter is `Z` for a process that has exited and has not been waited for;
    every other letter is a process that is doing something.  The same call as
    `_start_token_ps` with another column, and the same cost.

    **A `ps` that could not say is not a zombie** — no output, no `ps`, a
    deadline — which is the direction that leaves a daemon reported as running
    rather than talked about as gone (DESIGN.md §10.1).
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_T.timeouts.ready.probe,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    return result.stdout.strip().startswith("Z")


#: One zombie check per platform, and split out for `_START_TOKEN_SOURCES`'
#: reason: each is callable directly, which is the only way the macOS path can be
#: tested on the other two thirds of the CI matrix.
_ZOMBIE_SOURCES = {
    "linux": _zombie_linux,
}


def _is_zombie(pid: int) -> bool:
    """Whether `pid` names a process that has exited and not been waited for."""
    source = _ZOMBIE_SOURCES.get(sys.platform)
    if source is not None:
        return source(pid)
    return _zombie_ps(pid)  # macOS, the BSDs, anything else


def pid_alive(pid: int) -> bool:
    """Whether a *live* process with this pid currently exists.

    **`os.kill(pid, 0)` must not be used here.**  On Windows, Python's `os.kill`
    does not send signals: any value other than the two console events calls
    `TerminateProcess` unconditionally.  So the usual POSIX idiom would not
    merely fail to check the process — it would *kill* it, which is a memorable
    way to lose the server you were asking about.

    Windows therefore opens the process and waits on it with a zero timeout;
    POSIX uses the signal-0 form, where it does mean what it looks like.

    **And a pid that exists is not a process that is running.**  On POSIX a child
    that has exited keeps its place in the process table — the pid, the state, an
    exit status nobody has read — until whoever spawned it waits, and `os.kill`
    on that zombie succeeds exactly as it does on a live process.  Reading it as
    alive is what made `launcher.stop` report `pid N did not exit` for every
    daemon the end-to-end lane had just stopped, on Linux and macOS — the two
    platforms that have zombies — while Windows, which has none, passed; and it
    is the count `live_clients` decides "am I the last one out?" by, where a
    killed client that stays a zombie is a client keeping the servers up forever.

    The state is therefore read as well, and **a state that could not be read is
    not a zombie**: an unanswerable question leaves the process reported as
    running, because the other direction claims a daemon is gone that may still
    be holding its port.
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
        # Somebody else's process, and the state is still readable for it (`ps`
        # for everyone, `/proc` for anyone who can see the directory), so the
        # question below is asked rather than answered here.
        pass
    return not _is_zombie(pid)


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
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(wintypes.DWORD()),
            ctypes.byref(wintypes.DWORD()),
        )
        if not ok:
            # ctypes does not raise on a Win32 call that failed, and a FILETIME
            # left at zero is the constant "0000000000000000" — which two
            # different failed lookups would share, so `same_process` would
            # "prove" a match between unrelated pids.  `None` is the honest
            # answer, and it is the unsafe direction that is being avoided.
            return None
        if not created.dwHighDateTime and not created.dwLowDateTime:
            return None
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
            timeout=_T.timeouts.ready.probe,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    stamp = result.stdout.strip()
    return digest(stamp) if stamp else None


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


def tcp_listening(host: str, port: int, *, timeout: float | None = None) -> bool | None:
    """Whether anything accepts a connection here, or `None` when it cannot say.

    A cheap gate in front of the MCP probe, so the common "nothing is there"
    case costs a quarter second instead of a full probe timeout.  It answers
    "is something there", never "is it ours" — the probe does that.

    **A probe that could not be made is not a port that is closed.**  A name
    that does not resolve means the probe never asked the question it was meant
    to ask, and that is `None`; `open_server` names it rather than stating as
    fact "nothing is listening" for a check it did not make (DESIGN.md §10.1).
    Every *other* failure — a refusal, a timeout, a socket that could not be
    created — means a connection was attempted and nothing answered within the
    budget, which is precisely what this gate exists to ask, so it is `False`
    and the reason is logged at debug.  The line is drawn at the failed *phase*
    rather than at the exception, because whether a closed port reports as a
    refusal or as a timeout depends on the host's own stack and on how long this
    probe waits — measured here, the same closed port refuses after two seconds
    and times out at this probe's quarter — and an answer that shifts with the
    machine is the one thing a probe must not give.

    `timeout` is None by default and resolved here, not in the signature: a
    def-time default would capture the registry's value at import
    (`slife2.helpers.timeouts` states the rule).
    """
    timeout = _T.timeouts.ready.probe_local if timeout is None else timeout
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except socket.gaierror as exc:
        logger.warning("%s:%d: the address did not resolve (%s)", host, port, exc)
        return None
    except OSError as exc:
        logger.debug("%s:%d: nothing answered the port probe (%s)", host, port, exc)
        return False


@contextlib.contextmanager
def exclusive(key: str, *, timeout: float | None = None) -> Generator[None]:
    """Hold a named lock, released by the OS if the holder dies.

    The primitive behind both the per-server start lock and the per-agent name
    claim.  Because the kernel owns it, there is no stale-lock protocol to get
    wrong: no "is the holder still alive", no timeout that races a crash, and
    no window where the answer is wrong.

    Raises:
        TimeoutError: If someone else holds it for longer than `timeout`.

    `timeout` is None by default and resolved here rather than in the signature
    (`slife2.helpers.timeouts` states why a def-time default is forbidden).
    """
    timeout = _T.timeouts.grace.lock if timeout is None else timeout
    if sys.platform == "win32":
        with _windows_mutex(key, timeout):
            yield
        return

    with _posix_flock(key, timeout):
        yield


@contextlib.contextmanager
def start_lock(url: str, *, timeout: float | None = None) -> Generator[None]:
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
                time.sleep(_T.timeouts.pacing.lock_poll)
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
      that job, silently defeating the whole point.  Breakaway takes it out; a
      job that forbids breakaway is retried without it, and the retry is
      **logged at warning, naming the consequence**, because the process then
      does not survive its enclosing job.  The retry is kept rather than failing
      the start — a machine where breakaway is forbidden at least gets a server
      for this session — and it is a registered fallback; see `DESIGN.md` §10.2.

    Also `cwd` is set away from the repository: a daemon must not hold a working
    directory open.  Which config the child reads is decided by the absolute
    `--data-dir` its caller passes, since that is the argument that actually
    reaches a server — and it has to be absolute for exactly this reason:
    `slife2.libs.host.launcher._argv` resolves it, and a relative one would be re-resolved
    against the `cwd` set here.
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
            except OSError as exc:
                # The surrounding job forbids breakaway (JOB_OBJECT_LIMIT_
                # BREAKAWAY_OK is not set).  Losing the escape hatch beats
                # failing to start, but it is not silent: the daemon does not
                # outlive the job that would kill it.
                logger.warning(
                    "could not start %s with CREATE_BREAKAWAY_FROM_JOB (%s); "
                    "starting without it, so this server will not outlive an "
                    "enclosing kill-on-close job",
                    argv[0],
                    exc,
                )
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
    """Keep a daemon's log from growing without bound across restarts.

    **A truncation that did not happen is not one that did.**  The stat and the
    unlink were both suppressed, so a log that could not be read or removed grew
    past the ceiling with nothing said.  The unlink is still best-effort — a log
    that is already gone is fine — but a failure is now logged naming the file,
    because the ceiling this function exists to hold is otherwise quietly
    exceeded (DESIGN.md §10.1).
    """
    try:
        if path.exists() and path.stat().st_size > _LOG_KEEP_BYTES:
            path.unlink()
    except OSError as exc:
        logger.warning(
            "could not truncate %s (%s); it may grow past the log ceiling", path, exc
        )


def _signal_tree(pid: int, sig: int) -> None:
    """Send `sig` to a process, and to what it spawned, as far as allowed.

    **The group, not only the process**, so that what a server spawned in turn
    goes with it — `spawn` sets `start_new_session=True`, which is what makes the
    child a group leader.  Checked rather than assumed: a server started by hand
    writes its own record, and a pid that is not a group leader makes `killpg`
    raise ESRCH (suppressed, so `down` would report a failure while the daemon
    kept running) — or, worse, signal whatever unrelated group happens to own
    that number.

    **A pid that vanished between the caller's last check and here is not an
    error.**  `os.getpgid` raises `ProcessLookupError` for a process that is
    already gone, which is the state the caller was aiming for, so it is read as
    "there is nothing left to signal" rather than allowed to escape.  That is
    why the lookup is inside a function and guarded rather than inlined: a
    plugin that exits during the grace poll — or between the deadline and the
    force — would otherwise turn a successful stop into a `ProcessLookupError`
    out of `terminate`, and `slife2 down` would report a crash for a daemon that
    stopped exactly as asked.
    """
    if sys.platform == "win32":
        # Windows stops a tree with `taskkill /T` inside `terminate` and never
        # reaches here.  The branch is named so the two POSIX-only calls below
        # are provably POSIX-only to a type checker — `os.killpg` is absent from
        # `os` on Windows, which is where this project is developed.
        return
    try:
        leader = os.getpgid(pid) == pid
    except ProcessLookupError:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if leader:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)


def terminate(pid: int, *, grace: float | None = None) -> bool:
    """Stop a process and its children, returning whether it is gone.

    `grace` is None by default and resolved here rather than in the signature
    (`slife2.helpers.timeouts` states why a def-time default is forbidden).

    Deliberately blunt.  These processes have no console to receive a graceful
    signal and hold no state worth flushing — they are stateless by design — so
    a kind shutdown would only be ceremony.

    **A `taskkill` that failed to run does not fail the stop, and that is a
    registered fallback.**  The Windows branch suppresses `OSError` and
    `SubprocessError` and then polls the pid, so a `taskkill` that could not be
    launched raises nothing out of `stop`.  *Gives up:* why the force failed —
    the caller learns only that the process is still alive, not that `taskkill`
    was unavailable.  *Does not cover:* a process that survives the poll, which
    this reports as `False` and `stop` reports as a server it could not stop.
    *Failure goes:* the still-alive pid the caller polls (DESIGN.md §10.2).
    """
    if not pid_alive(pid):
        return True

    grace = _T.timeouts.grace.kill if grace is None else grace
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=grace + _T.timeouts.ready.reap,
            )
    else:
        import signal

        _signal_tree(pid, signal.SIGTERM)

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(_T.timeouts.pacing.lock_poll)

    if sys.platform != "win32":
        import signal

        # **The same branch the polite signal takes, and it has to be.**  A pid
        # that is not a group leader — a server started by hand, which
        # `_signal_tree` names — has no group to signal, so an unconditional
        # `killpg` is suppressed ESRCH and this returns False with the daemon
        # still running: the one case where the force is needed is the one where
        # it was never sent.
        _signal_tree(pid, signal.SIGKILL)
        time.sleep(_T.timeouts.pacing.lock_poll)

    return not pid_alive(pid)
