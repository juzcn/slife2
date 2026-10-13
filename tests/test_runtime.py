"""Platform primitives: endpoint keys, records, pid liveness, and the lock.

The lock tests deliberately use **two processes**.  A Windows named mutex is
recursive for the thread that owns it and a POSIX `flock` belongs to the open
file description, so an in-process test would acquire the same lock twice and
pass while proving nothing at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from slife2 import handover, runtime

pytestmark = pytest.mark.unit

REPO = str(Path(__file__).resolve().parents[1])


# The data directory is isolated for every test in `conftest.py`, not here —
# see the note there for why it is not this module's business alone.


def run_helper(code: str, *, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start a child that can import slife2, without `-I` closing sys.path."""
    return subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": REPO, **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


# --- endpoint identity -------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("http://127.0.0.1:8001/mcp", "http://127.0.0.1:8001/mcp"),
        ("http://localhost:8001/mcp", "http://127.0.0.1:8001/mcp"),
        ("http://LOCALHOST:8001/mcp", "http://127.0.0.1:8001/mcp"),
        ("http://127.0.0.1:8001/mcp/", "http://127.0.0.1:8001/mcp"),
        ("http://127.0.0.1:80/mcp", "http://127.0.0.1/mcp"),
    ],
)
def test_urls_that_are_the_same_server_share_a_key(left: str, right: str) -> None:
    """Records are keyed by endpoint, so two spellings must not be two servers."""
    assert runtime.endpoint_key(left) == runtime.endpoint_key(right)


def test_different_ports_are_different_servers() -> None:
    assert runtime.endpoint_key("http://127.0.0.1:8001/mcp") != runtime.endpoint_key(
        "http://127.0.0.1:8002/mcp"
    )


def test_an_agent_name_cannot_collide_with_a_server_address() -> None:
    """The prefix is why `--agent http://...` is harmless."""
    url = "http://127.0.0.1:8000/mcp"
    assert runtime.agent_key(url) != runtime.endpoint_key(url)


# --- records -----------------------------------------------------------------


def test_record_round_trips() -> None:
    record = runtime.ServerRecord.now(
        name="agent",
        url="http://127.0.0.1:8000/mcp",
        pid=1234,
        config="c.yaml",
        version="1",
    )
    runtime.write_record(record)
    assert runtime.read_record(record.url) == record


def test_a_missing_record_is_absent_not_an_error() -> None:
    assert runtime.read_record("http://127.0.0.1:9999/mcp") is None


def test_a_corrupt_record_reads_as_absent() -> None:
    """Bookkeeping must never block a start."""
    url = "http://127.0.0.1:8000/mcp"
    path = runtime.record_path(url)
    path.write_text("{ not json", encoding="utf-8")
    assert runtime.read_record(url) is None


def test_a_record_that_is_not_a_record_reads_as_absent() -> None:
    """The half `read_record` owns, now that the shared reader is `_read_json_dataclass`.

    Parsing is the shared helper's; the *shape* is the record's, and a JSON
    object that parses but is not a record is the `KeyError` the factory raises
    — the arm a caller's own fields are the only way to reach.
    """
    url = "http://127.0.0.1:8000/mcp"
    runtime.record_path(url).write_text('{"url": "x"}', encoding="utf-8")
    assert runtime.read_record(url) is None


def test_a_claim_round_trips_and_a_corrupt_one_reads_as_absent() -> None:
    """The claim is where the shared reader and writer prove they serve both.

    `read_record`/`write_record` and `read_claim`/`write_claim` are one
    `_read_json_dataclass` and one `_write_json_atomic` now, so a claim is the
    other call site of each: the same atomic write on the way in, the same
    "absent, not an error" read on the way out.
    """
    claim = runtime.AgentClaim.now("alpha")
    runtime.write_claim(claim)
    assert runtime.read_claim("alpha") == claim
    assert runtime.read_claim("nobody") is None

    runtime.agent_claim_path("alpha").write_text("{ not json", encoding="utf-8")
    assert runtime.read_claim("alpha") is None


# --- pid liveness ------------------------------------------------------------


def test_pid_alive_is_true_for_this_process() -> None:
    assert runtime.pid_alive(os.getpid()) is True


def test_pid_alive_is_false_for_a_pid_that_cannot_exist() -> None:
    assert runtime.pid_alive(999_999) is False


def test_pid_alive_does_not_kill_the_process_it_checks() -> None:
    """The whole reason this is not `os.kill(pid, 0)`.

    On Windows that idiom calls `TerminateProcess`, so a liveness check written
    the POSIX way would terminate the server it was asking about.
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert runtime.pid_alive(child.pid) is True
        time.sleep(0.2)
        assert child.poll() is None, "the liveness check killed the process"
    finally:
        child.kill()
        child.wait()


#: Platforms with a start-token source.  The CI matrix covers all three; the
#: guard is here so an exotic platform fails honestly rather than pretending
#: `down` is safe there.
_HAS_START_TOKEN = (
    sys.platform == "win32"
    or sys.platform.startswith("linux")
    or sys.platform == "darwin"
)


@pytest.mark.skipif(not _HAS_START_TOKEN, reason="no start-token source here")
def test_start_token_distinguishes_process_instances() -> None:
    """The defence against pid reuse: same number, different instance."""
    mine = runtime.process_start_token(os.getpid())
    assert mine is not None, "this platform should expose a start token"
    assert runtime.process_start_token(999_999) is None


@pytest.mark.skipif(not _HAS_START_TOKEN, reason="no start-token source here")
def test_the_start_token_is_stable_for_one_process() -> None:
    """It identifies an instance, so it must not drift while it runs."""
    assert runtime.process_start_token(os.getpid()) == runtime.process_start_token(
        os.getpid()
    )


class FakePs:
    """Stands in for `subprocess.run(["ps", ...])`."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return self


def test_the_ps_token_comes_from_lstart(monkeypatch: pytest.MonkeyPatch) -> None:
    """The macOS path, exercised where it cannot run.

    macOS has no /proc, so its token comes from `ps -o lstart=`.  Calling the
    source directly is the only way this is covered on the Linux and Windows CI
    jobs — otherwise a typo here would surface on one third of the matrix.
    """
    fake = FakePs("Mon Oct  6 11:20:03 2026\n")
    monkeypatch.setattr(runtime.subprocess, "run", fake)

    token = runtime._start_token_ps(4321)
    assert token, "a stamp should produce a token"
    assert fake.calls[0][0] == "ps"
    assert "lstart=" in fake.calls[0]
    assert fake.calls[0][-1] == "4321"

    # Stable for the same stamp...
    assert runtime._start_token_ps(4321) == token
    # ...and different for a process that started a second later, which is the
    # whole point: a recycled pid gets a different value.
    monkeypatch.setattr(runtime.subprocess, "run", FakePs("Mon Oct  6 11:20:04 2026\n"))
    assert runtime._start_token_ps(4321) != token


def test_the_ps_token_is_absent_when_ps_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No output means no answer, not an empty-string token that would match."""
    monkeypatch.setattr(runtime.subprocess, "run", FakePs(""))
    assert runtime._start_token_ps(4321) is None


def test_the_ps_token_survives_ps_being_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A machine without `ps` degrades to "cannot prove", which is safe."""

    def explode(*args, **kwargs):
        raise FileNotFoundError("no ps here")

    monkeypatch.setattr(runtime.subprocess, "run", explode)
    assert runtime._start_token_ps(4321) is None


def test_every_platform_has_a_token_source() -> None:
    """A platform with no source silently disables `down`, so name them."""
    assert set(runtime._START_TOKEN_SOURCES) == {"win32", "linux"}
    # Everything else falls through to `ps`, which macOS and the BSDs have.
    assert runtime._start_token_ps is not None


def test_same_process_rejects_a_record_with_no_token() -> None:
    """An unprovable match is treated as no match, so `down` leaves it alone."""
    record = runtime.ServerRecord(
        name="agent", url="http://127.0.0.1:8000/mcp", pid=os.getpid(), start_token=""
    )
    assert runtime.same_process(record) is False


def test_same_process_matches_our_own_pid_and_token() -> None:
    pid = os.getpid()
    record = runtime.ServerRecord(
        name="agent",
        url="http://127.0.0.1:8000/mcp",
        pid=pid,
        start_token=runtime.process_start_token(pid) or "",
    )
    assert runtime.same_process(record) is True


def test_same_process_rejects_a_reused_pid() -> None:
    """A live pid whose token says it is not ours must never be signalled."""
    record = runtime.ServerRecord(
        name="agent",
        url="http://127.0.0.1:8000/mcp",
        pid=os.getpid(),
        start_token="not-the-token-this-process-was-born-with",
    )
    assert runtime.same_process(record) is False


# --- the lock ----------------------------------------------------------------


def test_exclusive_lock_is_released_on_exit() -> None:
    with runtime.exclusive("k"):
        pass
    with runtime.exclusive(
        "k",
        timeout=1,  # timeouts: the value this test is built around, not a budget
    ):  # timeouts: the value this test is built around, not a budget
        pass  # acquiring again proves the first was released


def test_exclusive_lock_is_held_by_another_process() -> None:
    """Two processes, because in one process a recursive mutex proves nothing."""
    holder = run_helper(
        "import time\n"
        "from slife2 import runtime\n"
        "with runtime.exclusive('shared', timeout=5):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(3)\n"
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(TimeoutError):
            with runtime.exclusive(
                "shared",
                timeout=0.3,  # timeouts: the value this test is built around, not a budget
            ):  # timeouts: the value this test is built around, not a budget
                pass
    finally:
        holder.kill()
        holder.wait()


def test_exclusive_lock_survives_nothing_when_the_holder_is_killed() -> None:
    """The property a lockfile cannot offer: the kernel releases it on death."""
    holder = run_helper(
        "import time\n"
        "from slife2 import runtime\n"
        "with runtime.exclusive('shared', timeout=5):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(30)\n"
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        holder.kill()
        holder.wait()
        # No stale-lock protocol, no pid probing, no timeout: it is just free.
        with runtime.exclusive(
            "shared",
            timeout=2,  # timeouts: the value this test is built around, not a budget
        ):  # timeouts: the value this test is built around, not a budget
            pass
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


# --- the TCP gate ------------------------------------------------------------


def test_tcp_listening_is_false_for_a_closed_port() -> None:
    assert runtime.tcp_listening("127.0.0.1", 9) is False


def test_tcp_listening_is_true_for_an_open_one() -> None:
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert runtime.tcp_listening("127.0.0.1", port) is True
    finally:
        server.close()


# --- span (the daemon requirement) -------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32", reason="console windows are a Windows concern"
)
def test_a_spawned_server_gets_no_console_window(tmp_path) -> None:
    """A daemon that flashes a window on every start is not a daemon.

    This asserts through the real `spawn()` because the plausible flag is the
    wrong one: `DETACHED_PROCESS` reads as "no console" and actually means "does
    not *inherit* the parent's console", leaving the child with a window of its
    own.  Measured, `DETACHED_PROCESS` yields a live handle (~723768) and
    `CREATE_NO_WINDOW` yields 0 — and passing both is accepted, with the window
    coming back, so getting this wrong is silent apart from the flashing.
    """
    result = tmp_path / "console.txt"
    probe = (
        "import ctypes, pathlib\n"
        f"pathlib.Path(r'{result}').write_text(\n"
        "    str(ctypes.windll.kernel32.GetConsoleWindow()))\n"
    )
    proc = runtime.spawn([sys.executable, "-c", probe], url="http://127.0.0.1:8077/mcp")
    proc.wait(timeout=30)  # timeouts: the value this test is built around, not a budget

    assert result.exists(), "the probe did not run"
    assert result.read_text().strip() == "0", "the daemon opened a console window"


def test_a_spawned_server_outlives_its_spawner() -> None:
    """The one property this whole layer exists for.

    The helper spawns a sleeper through the real `spawn()` and exits
    immediately; if the child dies with it, nothing here is a daemon.
    """
    holder = run_helper(
        "import sys\n"
        "from slife2 import runtime\n"
        "p = runtime.spawn([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                  url='http://127.0.0.1:8000/mcp')\n"
        "print(p.pid, flush=True)\n"
    )
    try:
        assert holder.stdout is not None
        pid = int(holder.stdout.readline().strip())
        holder.wait(
            timeout=20  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget
        assert holder.returncode == 0

        time.sleep(0.5)
        assert runtime.pid_alive(pid) is True, "the child died with its spawner"
        assert runtime.terminate(pid) is True
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


# --- stopping a process that vanished mid-stop -------------------------------


def test_terminate_reads_a_process_that_vanished_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plugin that exits between the liveness check and the signal.

    `terminate` asks `pid_alive` once and then, on POSIX, calls `os.getpgid` —
    and a process that exits in between makes that call raise
    `ProcessLookupError`.  `os.getpgid` sits outside the `suppress` the signal
    itself has, so the exception escaped `terminate`: `slife2 down` reported a
    crash for a daemon that stopped exactly as asked.  The POSIX-only branch is
    driven here by naming the platform, since a pid cannot be made to vanish on
    cue on this machine.

    With the lookup guarded, the vanished pid reads as gone — which is what the
    return value is for — and nothing is signalled.
    """
    calls = {"n": 0}

    def alive(pid: int) -> bool:
        calls["n"] += 1
        return calls["n"] == 1  # alive for the first look, gone by the next

    def vanished(pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime, "pid_alive", alive)
    monkeypatch.setattr(runtime.os, "getpgid", vanished, raising=False)

    assert runtime.terminate(4242, grace=0.05) is True


# --- the close a cancellation cannot take away -------------------------------
# `slife2.handover` hands a message to a window (or, as the fallback, to the
# agent server) and closes the connection it opened on the way out.  That close
# lives in one helper there, `_peer`, and has to finish even when the caller is
# cancelled — which is the case the `finally` exists for.  Exercised from here
# because it is a lifecycle primitive and the hand-over module has no test
# module of its own.


@pytest.mark.asyncio
async def test_the_close_survives_the_cancellation_it_exists_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `CancelledError` is not an `Exception`, so the plain close let it through.

    The `finally` runs on exactly the path where the caller was cancelled —
    that is why a close belongs there — and a cancellation delivered while the
    transport is shutting down passes straight through `contextlib.suppress`
    and leaves the connection open, the one outcome the close exists to
    prevent.  `_peer` shields the close, so it runs in a task of its own and
    finishes whatever happens to the caller, and the cancellation still
    propagates so the caller's task is cancelled as it expected.
    """
    entered = asyncio.Event()
    closed = asyncio.Event()

    class SlowClient:
        async def __aexit__(self, *exc: object) -> None:
            entered.set()
            await asyncio.sleep(0.05)
            closed.set()

    async def fake_open(
        url: str,
        *,
        name: str = "",
        fallback_tool: str = "",
        timeout: float | None = None,
    ) -> SlowClient:
        return SlowClient()

    monkeypatch.setattr(handover, "open_server", fake_open)

    async def body() -> None:
        async with handover._peer(
            "http://127.0.0.1:9/mcp",
            name="x",
            fallback_tool="t",
            timeout=1.0,
            client=None,
        ):
            pass  # the body ends at once, so it is the close that is running

    task = asyncio.create_task(body())
    await entered.wait()  # we are now inside `__aexit__`
    task.cancel()  # the cancellation lands on the close itself
    with contextlib.suppress(asyncio.CancelledError):
        await task

    await asyncio.wait_for(closed.wait(), timeout=1)
    assert closed.is_set()
