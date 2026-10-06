"""Platform primitives: endpoint keys, records, pid liveness, and the lock.

The lock tests deliberately use **two processes**.  A Windows named mutex is
recursive for the thread that owns it and a POSIX `flock` belongs to the open
file description, so an in-process test would acquire the same lock twice and
pass while proving nothing at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from slife2 import runtime

pytestmark = pytest.mark.unit

REPO = str(Path(__file__).resolve().parents[1])


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test at its own runtime directory.

    Without this a test could read — or `down` — a developer's real servers.
    """
    monkeypatch.setenv("SLIFE2_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path / "runtime"


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


def test_start_token_distinguishes_process_instances() -> None:
    """The defence against pid reuse: same number, different instance."""
    mine = runtime.process_start_token(os.getpid())
    assert mine is not None, "this platform should expose a start token"
    assert runtime.process_start_token(999_999) is None


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
    with runtime.exclusive("k", timeout=1):
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
            with runtime.exclusive("shared", timeout=0.3):
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
        with runtime.exclusive("shared", timeout=2):
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
        holder.wait(timeout=20)
        assert holder.returncode == 0

        time.sleep(0.5)
        assert runtime.pid_alive(pid) is True, "the child died with its spawner"
        assert runtime.terminate(pid) is True
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
