"""Platform primitives: endpoint keys, records, pid liveness, and the lock.

The lock tests deliberately use **two processes**.  A Windows named mutex is
recursive for the thread that owns it and a POSIX `flock` belongs to the open
file description, so an in-process test would acquire the same lock twice and
pass while proving nothing at all.

**Which is the lane this module is in.**  `integration` is I/O, network or
subprocess, and there are five real interpreters here — the lock holder, the
holder that gets killed, the child that outlives its spawner, and the two pid
liveness checks that need a process nobody is waiting for — so this is not a
file that can be run somewhere processes cannot be spawned.  The module used to
say `unit`, which put six seconds of interpreter startup into a lane documented
as "no I/O, no subprocess"; the tests that need none of it are about the same
subject (the record and claim files, the start token) and stay with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from slife2.libs.harness import handover
from slife2.libs.host import runtime

pytestmark = pytest.mark.integration

#: The checkout, found by the file that names it rather than by counting
#: directories up: a depth is a number that goes stale in silence.
REPO = str(
    next(
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "pyproject.toml").is_file()
    )
)


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
        ("http://127.0.0.1:24001/mcp", "http://127.0.0.1:24001/mcp"),
        ("http://localhost:24001/mcp", "http://127.0.0.1:24001/mcp"),
        ("http://LOCALHOST:24001/mcp", "http://127.0.0.1:24001/mcp"),
        ("http://127.0.0.1:24001/mcp/", "http://127.0.0.1:24001/mcp"),
        ("http://127.0.0.1:80/mcp", "http://127.0.0.1/mcp"),
    ],
)
def test_urls_that_are_the_same_server_share_a_key(left: str, right: str) -> None:
    """Records are keyed by endpoint, so two spellings must not be two servers."""
    assert runtime.endpoint_key(left) == runtime.endpoint_key(right)


def test_different_ports_are_different_servers() -> None:
    assert runtime.endpoint_key("http://127.0.0.1:24001/mcp") != runtime.endpoint_key(
        "http://127.0.0.1:24002/mcp"
    )


def test_an_agent_name_cannot_collide_with_a_server_address() -> None:
    """The prefix is why `--agent http://...` is harmless."""
    url = "http://127.0.0.1:24000/mcp"
    assert runtime.agent_key(url) != runtime.endpoint_key(url)


# --- records -----------------------------------------------------------------


def test_record_round_trips() -> None:
    record = runtime.ServerRecord.now(
        name="agent",
        url="http://127.0.0.1:24000/mcp",
        pid=1234,
        config="c.yaml",
        version="1",
    )
    runtime.write_record(record)
    assert runtime.read_record(record.url) == record


def test_a_missing_record_is_absent_not_an_error() -> None:
    assert runtime.read_record("http://127.0.0.1:9999/mcp") is None


def test_a_corrupt_record_reads_as_absent() -> None:
    """Bookkeeping must never block a start.

    This is the registered tolerance of `_read_json_dataclass` (`DESIGN.md`
    SDD Appendix C): damage reads as absence, which is accepted for a *recorded pid* but
    is why `live_clients` does not delete a damaged record while its pid is
    alive — there the cost is a live client nobody counts.
    """
    url = "http://127.0.0.1:24000/mcp"
    path = runtime.record_path(url)
    path.write_text("{ not json", encoding="utf-8")
    assert runtime.read_record(url) is None


def test_a_record_that_is_not_a_record_reads_as_absent() -> None:
    """The half `read_record` owns, now that the shared reader is `_read_json_dataclass`.

    Parsing is the shared helper's; the *shape* is the record's, and a JSON
    object that parses but is not a record is the `KeyError` the factory raises
    — the arm a caller's own fields are the only way to reach.
    """
    url = "http://127.0.0.1:24000/mcp"
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


# --- the client count the shared servers rest on ------------------------------


def test_a_client_record_that_cannot_be_parsed_but_is_alive_is_kept() -> None:
    """**A record that cannot be parsed is not a client that is gone.**

    Deleting a damaged record made a live client uncounted, and the count is
    what "the last one out stops the shared servers" rests on.  The pid is in
    the file's *name*, so a live client's record is kept even when its body is
    unreadable — only a dead pid is a crash to clean up.
    """
    path = runtime.register_client("alpha").path
    path.write_text("{ not json", encoding="utf-8")

    live = runtime.live_clients()

    assert any(record.pid == os.getpid() for record in live), (
        "the live client was uncounted"
    )
    assert path.exists(), "a live client's record was deleted"


def test_a_client_record_that_cannot_be_parsed_and_is_dead_is_cleaned_up() -> None:
    """Only a dead pid is a crash to clean up, and then the file goes."""
    path = runtime._clients_dir() / "999999-deadbeef.json"
    path.write_text("{ not json", encoding="utf-8")

    assert runtime.live_clients() == []
    assert not path.exists()


def test_a_sweep_whose_delete_failed_is_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A delete that failed is not a delete that happened** — here too.

    `live_clients` removes a dead client's leftover under the same `_remove` the
    three clear functions use, so a sweep whose delete failed is logged rather
    than leaving the record to survive silently (SDD Appendix C).
    """
    path = runtime._clients_dir() / "999999-deadbeef.json"
    path.write_text("{ not json", encoding="utf-8")

    def deny(self: Path, *, missing_ok: bool = False) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", deny)
    with caplog.at_level(logging.WARNING):
        runtime.live_clients()

    assert str(path) in caplog.text


def test_registering_a_client_writes_it_atomically() -> None:
    """The third bookkeeping file goes through `_write_json_atomic` too, so a
    concurrent reader never catches it half-written — the same race the record
    and the claim are written for."""
    record = runtime.register_client("beta")
    assert runtime.live_clients() == [record]
    assert not record.path.with_suffix(".tmp").exists()


def test_a_bookkeeping_write_that_failed_is_reported(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A write that failed is not a write that happened.**

    The atomic write suppressed `OSError`, so a server that started but whose
    record never landed was invisible to `stop` and got orphaned.  The behaviour
    is kept — the caller is not prepared to fail over — but the path is logged,
    so the orphan is findable (SDD Appendix C).
    """
    target = tmp_path / "record.json"

    def deny(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runtime.os, "replace", deny)
    with caplog.at_level(logging.WARNING):
        runtime._write_json_atomic(target, runtime.AgentClaim.now("alpha"))

    assert str(target) in caplog.text


def test_a_delete_that_failed_is_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A delete that failed is not a delete that happened.**

    `clear_record` — and `clear_claim` and `unregister_client` — suppressed a
    failed unlink, so a record the code asked to remove survived with nothing
    said.  An absent file is still tolerated; a failed unlink is logged naming
    the path (SDD Appendix C).
    """
    url = "http://127.0.0.1:24000/mcp"
    path = runtime.record_path(url)

    def deny(self: Path, *, missing_ok: bool = False) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", deny)
    with caplog.at_level(logging.WARNING):
        runtime.clear_record(url)

    assert str(path) in caplog.text


def test_a_delete_of_a_record_that_is_already_gone_is_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The registered tolerance: removing an absent record is not a failure."""
    with caplog.at_level(logging.WARNING):
        runtime.clear_record("http://127.0.0.1:9999/mcp")

    assert caplog.text == ""


def test_a_log_that_cannot_be_read_is_named_not_empty() -> None:
    """**An unreadable log is not a log with nothing in it.**

    `tail_log` is pasted into a `StartFailed` message, so answering "" said the
    server printed nothing when the file could not be opened.  The failure is
    named in the text instead (`DESIGN.md` §11.1).
    """
    url = "http://127.0.0.1:24000/mcp"
    path = runtime.log_path(url)
    path.mkdir()  # a directory where the log should be: reading it is an OSError

    text = runtime.tail_log(url)

    assert text != ""
    assert str(path) in text


def test_a_log_that_cannot_be_truncated_is_reported(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A truncation that did not happen is not one that did.**

    `_truncate_if_huge` suppressed the stat and the unlink, so a log that could
    not be unlinked grew past the ceiling with nothing said.  The failure is now
    logged naming the file (`DESIGN.md` §11.1).
    """
    path = tmp_path / "daemon.log"
    path.write_text("x", encoding="utf-8")

    def deny(self: Path, *, missing_ok: bool = False) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", deny)
    monkeypatch.setattr(runtime, "_LOG_KEEP_BYTES", 0)
    with caplog.at_level(logging.WARNING):
        runtime._truncate_if_huge(path)

    assert str(path) in caplog.text


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


def test_a_child_that_exited_unwaited_is_not_a_live_process() -> None:
    """**A pid that exists is not a process that is still running.**

    On POSIX a child that has exited keeps its place in the process table — the
    pid, the state, an exit status nobody has read — until whoever spawned it
    waits, and `os.kill` on that zombie succeeds exactly as it does on a live
    process.  This is the state `spawn` leaves every daemon in: it drops the
    `Popen` it made, so nothing here ever waits on a server this process started
    (which is `_wait_ready`'s lesson, and the reason it reaps by hand).

    The failure it produced is the end-to-end lane's teardown, and it was a
    platform split: `launcher.stop` asked after seventeen daemons it had just
    killed, was told every one of them was still there, and reported `pid N did
    not exit` for each — on Linux and macOS, the two platforms that have
    zombies, while Windows, which does not, passed.

    The wait is deliberately *not* done here.  Reaping the child would answer
    the question by destroying it, and `stop` cannot do that: it is holding a
    pid out of a record, and the process that spawned the daemon may be long
    gone.
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert runtime.pid_alive(child.pid) is True
        child.kill()
        time.sleep(0.5)
        assert runtime.pid_alive(child.pid) is False
        # And the same fact through the caller that reports it: a stopped
        # daemon, not `did not exit`.
        assert runtime.terminate(child.pid) is True
    finally:
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


def test_a_platform_with_no_source_of_its_own_falls_back_to_ps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform with no source silently disables `down`, so name them.

    The table holds the two platforms with a cheap and exact answer; everything
    else — macOS, the BSDs, whatever comes next — has to reach `ps`, and reaching
    nothing answers "cannot prove" for every pid, which is a `down` that stops
    nothing and says why to nobody.

    **The fall-through is exercised rather than pointed at.**  What stood here
    was `assert runtime._start_token_ps is not None` on an unconditional
    module-level `def`, which is true of any process that imported this file and
    would have gone on being true if `process_start_token` stopped calling it.
    Emptying the table is the platform nobody has a source for, simulated
    honestly: the call has to arrive at `ps`.
    """
    assert set(runtime._START_TOKEN_SOURCES) == {"win32", "linux"}

    monkeypatch.setattr(runtime, "_START_TOKEN_SOURCES", {})
    asked: list[int] = []
    monkeypatch.setattr(
        runtime, "_start_token_ps", lambda pid: asked.append(pid) or "a-token"
    )

    assert runtime.process_start_token(4321) == "a-token"
    assert asked == [4321]


# --- a process that has exited and was never waited for ----------------------
# The state of a pid, which is `pid_alive`'s second question and the one a
# signal-0 test cannot ask.  Both sources are called directly, so the macOS half
# is covered on the other two thirds of the CI matrix.


def test_the_linux_zombie_check_reads_the_state_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Linux path, driven by a `/proc/<pid>/stat` line.

    Field 2 is the command name, which may hold spaces and brackets of its own,
    so the state is the field after the *last* `)` — the same split
    `_start_token_linux` makes.
    """

    def reads(state: str) -> bool:
        monkeypatch.setattr(
            runtime.Path,
            "read_text",
            lambda self, **kw: f"4321 (a ) name) {state} 1 4321 1234\n",
        )
        return runtime._zombie_linux(4321)

    assert reads("Z") is True
    assert reads("S") is False
    assert reads("R") is False


def test_the_linux_zombie_check_answers_false_when_proc_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A state that could not be read is not a zombie.**

    The direction matters: answering "zombie" from a read that never happened
    would report a live daemon as stopped, and a daemon reported as stopped is
    one nobody goes looking for.  An unanswerable question leaves it running
    (`DESIGN.md` §11.1).
    """

    def deny(self: Path, **kw: object) -> str:
        raise OSError("no such file or directory")

    monkeypatch.setattr(runtime.Path, "read_text", deny)
    assert runtime._zombie_linux(4321) is False


def test_the_ps_zombie_check_reads_the_state_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The macOS path, exercised where it cannot run — `_start_token_ps`'s reason.

    macOS has no `/proc`, so the state comes from `ps -o state=`, whose first
    character is the run state and `Z` is the one a process that has exited has.
    The letters a live process reports are here too, because the failure that
    matters is the other direction: a daemon that is running and is reported
    gone.
    """
    for output, expected in (
        ("Z\n", True),
        ("Z+\n", True),
        ("S\n", False),
        ("R\n", False),
        ("", False),
    ):
        fake = FakePs(output)
        monkeypatch.setattr(runtime.subprocess, "run", fake)
        assert runtime._zombie_ps(4321) is expected, output

    assert fake.calls[0][0] == "ps"
    assert "state=" in fake.calls[0]
    assert fake.calls[0][-1] == "4321"


def test_the_ps_zombie_check_answers_false_when_ps_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `ps` on this machine is not a process that has exited."""

    def missing(*args: object, **kw: object):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(runtime.subprocess, "run", missing)
    assert runtime._zombie_ps(4321) is False


def test_only_linux_reads_proc_for_a_zombie() -> None:
    """The table names Linux, and everything else POSIX falls through to `ps`.

    Windows is deliberately absent: it has no zombies — a process object is
    signalled the moment the process exits, whether or not anybody waited — and
    `pid_alive` answers from that handle before this table is ever read.
    """
    assert set(runtime._ZOMBIE_SOURCES) == {"linux"}


def test_same_process_rejects_a_record_with_no_token() -> None:
    """An unprovable match is treated as no match, so `down` leaves it alone."""
    record = runtime.ServerRecord(
        name="agent", url="http://127.0.0.1:24000/mcp", pid=os.getpid(), start_token=""
    )
    assert runtime.same_process(record) is False


def test_same_process_matches_our_own_pid_and_token() -> None:
    pid = os.getpid()
    record = runtime.ServerRecord(
        name="agent",
        url="http://127.0.0.1:24000/mcp",
        pid=pid,
        start_token=runtime.process_start_token(pid) or "",
    )
    assert runtime.same_process(record) is True


def test_same_process_rejects_a_reused_pid() -> None:
    """A live pid whose token says it is not ours must never be signalled."""
    record = runtime.ServerRecord(
        name="agent",
        url="http://127.0.0.1:24000/mcp",
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
        "from slife2.libs.host import runtime\n"
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
        "from slife2.libs.host import runtime\n"
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


def test_tcp_listening_that_cannot_resolve_is_no_answer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A probe that could not be made is not a port that is closed.**

    A name that does not resolve answered `False`, so `open_server` stated as
    fact "nothing is listening" for a check it never got to make.  The third
    answer is `None`, and the reason is logged (`DESIGN.md` §11.1).
    """
    import socket

    def unresolved(*args, **kwargs):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(runtime.socket, "create_connection", unresolved)
    with caplog.at_level(logging.WARNING):
        assert runtime.tcp_listening("nowhere.invalid", 9) is None

    assert "nowhere.invalid" in caplog.text


def test_tcp_listening_that_answered_nothing_is_false_and_logs_why(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refusal and a timeout are both "nothing answered the probe", which is
    the gate's question — the reason is named at debug, and neither is read as
    an address that could not be asked about (`DESIGN.md` §11.1)."""

    def timed_out(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(runtime.socket, "create_connection", timed_out)
    with caplog.at_level(logging.DEBUG):
        assert runtime.tcp_listening("127.0.0.1", 9) is False

    assert "127.0.0.1" in caplog.text


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


@pytest.mark.skipif(
    sys.platform != "win32", reason="breakaway is a Windows job-object flag"
)
def test_a_start_without_the_job_escape_is_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**A weaker path is not a substitute for a missing capability.**

    When the enclosing job forbids `CREATE_BREAKAWAY_FROM_JOB`, the process is
    retried without it — which is defensible, because losing the escape hatch
    beats failing to start, but *not* silently: the server then dies with the
    job, and that consequence is named at warning (SDD Appendix C).
    """
    calls: list[int] = []

    class FakeProc:
        pid = 4321

    def fake_popen(argv, **kwargs):
        calls.append(kwargs.get("creationflags", 0))
        if len(calls) == 1:
            raise OSError("breakaway forbidden")
        return FakeProc()

    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)
    with caplog.at_level(logging.WARNING):
        proc = runtime.spawn(["true"], url="http://127.0.0.1:8079/mcp")

    assert isinstance(proc, FakeProc)
    assert len(calls) == 2, "the retry without the flag did not happen"
    assert calls[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert not calls[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert "job" in caplog.text.lower()


def test_a_spawned_server_outlives_its_spawner() -> None:
    """The one property this whole layer exists for.

    The helper spawns a sleeper through the real `spawn()` and exits
    immediately; if the child dies with it, nothing here is a daemon.
    """
    holder = run_helper(
        "import sys\n"
        "from slife2.libs.host import runtime\n"
        "p = runtime.spawn([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                  url='http://127.0.0.1:24000/mcp')\n"
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
# `slife2.libs.harness.handover` hands a message to a window — one road, and a
# delivery that finds none is a hard failure — and closes the connection it opened
# on the way out.  That close
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
