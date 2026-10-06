"""Choosing which servers to run, and attaching to the ones already there.

`ensure` is driven with stubbed probes rather than real servers, so the three
outcomes — reuse, start, conflict — are assertions instead of timing.  The one
thing that does use a second process is the agent-name claim, because a Windows
mutex is recursive within a thread and an in-process test would prove nothing.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from slife2 import launcher, runtime
from slife2.config import (
    Config,
    default_config,
    load,
)
from slife2.launcher import AgentInUse, ServerSpec, Status

pytestmark = pytest.mark.unit

REPO = str(Path(__file__).resolve().parents[1])


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path / "data"))


# --- which servers are needed ------------------------------------------------


def test_there_are_exactly_three_components() -> None:
    """One agent loop, two model backends — each with one job.

    The granularity is the point: a backend speaks one wire protocol and does
    nothing else, and a provider is a row in that backend's config rather than a
    process of its own.  Three providers on two protocols is three processes,
    not five.
    """
    config = load(_config_with_both_protocols())
    names = [spec.name for spec in launcher.specs(config)]
    assert names == [
        "llm:openai-completions",
        "llm:anthropic-messages",
        "memory",
        "agent",
    ]
    modules = {spec.module for spec in launcher.specs(config)}
    assert modules == {
        "slife2.llm.openai_server",
        "slife2.llm.anthropic_server",
        "slife2.memory_server",
        "slife2.server.server",
    }


def test_one_server_per_wire_protocol() -> None:
    """Not one per provider — that is what this replaced.

    A process speaks one format, so every OpenAI-compatible provider shares it
    and `stream_chat(provider=...)` says whose credentials to use.  Three
    providers on two protocols is two processes, not three.
    """
    path = _config_with_both_protocols()
    names = [spec.name for spec in launcher.specs(load(path))]
    assert names == [
        "llm:openai-completions",
        "llm:anthropic-messages",
        "memory",
        "agent",
    ]


def _config_with_both_protocols():
    import tempfile

    path = Path(tempfile.mkdtemp()) / "two.yaml"
    path.write_text(
        """
providers:
  a: {api: openai-completions, models: [{model: m}]}
  b: {api: openai-completions, models: [{model: n}]}
  c: {api: anthropic-messages, models: [{model: o}]}
default: a/m
""",
        encoding="utf-8",
    )
    return path


def test_the_api_chooses_the_module(tmp_path) -> None:
    modules = {
        spec.name: spec.module
        for spec in launcher.specs(load(_config_with_both_protocols()))
    }
    assert modules["llm:openai-completions"] == "slife2.llm.openai_server"
    assert modules["llm:anthropic-messages"] == "slife2.llm.anthropic_server"


def test_a_protocol_no_provider_uses_is_not_started(tmp_path) -> None:
    """A server for a protocol nobody uses holds nobody's key."""
    path = tmp_path / "one.yaml"
    path.write_text(
        "providers:\n  a: {api: openai-completions, models: [{model: m}]}\n",
        encoding="utf-8",
    )
    names = [spec.name for spec in launcher.specs(load(path))]
    assert names == ["llm:openai-completions", "memory", "agent"]


def test_the_agent_server_takes_no_provider() -> None:
    spec = next(s for s in launcher.specs(default_config()) if s.name == "agent")
    assert "--provider" not in launcher._argv(spec, Path("slife2.yaml"))


def test_model_servers_start_before_the_agent_server() -> None:
    """The agent server connects to its model in its lifespan.

    Starting them together races, and the failure is confusing rather than
    obvious: the agent server comes up healthy and every turn fails.
    """
    names = [spec.name for spec in launcher.specs(default_config())]
    assert names[-1] == "agent"
    # Everything else is a dependency of it, whatever kind it is.
    assert set(names[:-1]) == {"llm:openai-completions", "memory"}


def test_specs_carry_where_each_server_listens() -> None:
    spec = next(
        s for s in launcher.specs(default_config()) if s.name.startswith("llm:")
    )
    assert (spec.host, spec.port, spec.expected_tool) == (
        "127.0.0.1",
        8001,
        "stream_chat",
    )
    assert spec.url == "http://127.0.0.1:8001/mcp"


# --- ensure ------------------------------------------------------------------


SPEC = ServerSpec(
    name="llm:openai-completions",
    module="slife2.llm.openai_server",
    url="http://127.0.0.1:8001/mcp",
    host="127.0.0.1",
    port=8001,
    expected_tool="stream_chat",
)


def _register(spec: ServerSpec, *, config: str = "") -> None:
    """Stand in for a server that has registered itself."""
    runtime.write_record(
        runtime.ServerRecord(
            name=spec.name, url=spec.url, pid=os.getpid(), config=config
        )
    )


def test_a_registered_server_is_reused_and_nothing_is_spawned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(SPEC)
    monkeypatch.setattr(launcher, "probe", lambda *a, **k: True)

    def explode(*a, **k):
        raise AssertionError("should not have spawned anything")

    monkeypatch.setattr(launcher, "spawn", explode)
    outcome = launcher.ensure(SPEC)
    assert outcome.status is Status.RUNNING
    assert outcome.ok


def test_a_server_started_by_another_config_is_still_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The servers are shared, full stop.**

    Which config started one does not decide whether another may use it.  Two
    instances naming the same file are simply using the same configuration;
    two naming different files still mean the same servers when those configs
    name the same ports, and sharing them is the point.

    The record still says where it came from, so a port two configurations are
    competing for is reported rather than left as a mystery when the model
    turns out not to be the one expected.
    """
    _register(SPEC, config=str(Path("somewhere/else/slife2.yaml")))
    monkeypatch.setattr(launcher, "probe", lambda *a, **k: True)

    def explode(*a, **k):
        raise AssertionError("should not spawn onto a port that answers")

    monkeypatch.setattr(launcher, "spawn", explode)
    outcome = launcher.ensure(SPEC, config_path=Path("this/one/slife2.yaml"))
    assert outcome.status is Status.RUNNING
    assert outcome.ok
    # Reported, not refused: the detail names the other config, and comparing
    # paths rather than strings keeps it honest on Windows, where the same path
    # spells itself with backslashes.
    assert "else" in outcome.detail and "slife2.yaml" in outcome.detail


def test_a_held_port_that_is_not_ours_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not "running" and not "not running" — a distinct, refused outcome.

    Spawning here would fail to bind and surface as a traceback in a log nobody
    is reading, which is a worse way to learn the port is taken.
    """
    monkeypatch.setattr(launcher, "probe", lambda *a, **k: False)
    monkeypatch.setattr(launcher, "tcp_listening", lambda *a, **k: True)

    def explode(*a, **k):
        raise AssertionError("should not have spawned onto a held port")

    monkeypatch.setattr(launcher, "spawn", explode)
    outcome = launcher.ensure(SPEC)
    assert outcome.status is Status.CONFLICT
    assert not outcome.ok
    assert "8001" in outcome.detail


def test_a_missing_server_is_spawned_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"spawned": False}

    def fake_probe(url, tool, **kwargs):
        return state["spawned"]  # not ready until after the spawn

    class FakeProc:
        pid = os.getpid()

        def poll(self):
            return None

    def fake_spawn(argv, *, url):
        state["spawned"] = True
        state["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(launcher, "probe", fake_probe)
    monkeypatch.setattr(launcher, "tcp_listening", lambda *a, **k: False)
    monkeypatch.setattr(launcher, "spawn", fake_spawn)

    outcome = launcher.ensure(SPEC, config_path=Path("slife2.yaml"))
    assert outcome.status is Status.STARTED
    # The record is written by the server, not by the launcher: only the server
    # knows which config it read.  So there is none until it has started.
    assert outcome.record is None

    # And a second call reuses it rather than spawning again, once the server
    # has registered.
    _register(SPEC)
    again = launcher.ensure(SPEC, config_path=Path("slife2.yaml"))
    assert again.status is Status.RUNNING


def test_the_spawn_command_uses_this_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sys.executable -m`, not the console script: same venv, no PATH needed."""
    argv = launcher._argv(SPEC, Path("D:/x/slife2.yaml"))
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "slife2.llm.openai_server"]
    assert "--data-dir" in argv


# --- the agent name is exclusive ---------------------------------------------


def wait_until_free(name: str, *, timeout: float = 8.0) -> None:
    """Block until nobody holds `name`.

    A process killed by a test releases its mutex at teardown, which is not
    instantaneous, so a test that killed the previous holder can lose a race it
    ought to win.  Waiting for the name to come free is the honest fix; the
    alternative is asserting on a timing.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(AgentInUse):
            with launcher.claim_agent(name):
                return
        time.sleep(0.1)


def test_two_instances_cannot_share_an_agent_name() -> None:
    """Proved across processes: a same-process test would prove nothing."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\n"
            "from slife2.launcher import claim_agent\n"
            "with claim_agent('alpha'):\n"
            "    print('claimed', flush=True)\n"
            "    time.sleep(5)\n",
        ],
        env={**os.environ, "PYTHONPATH": REPO},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "claimed"
        with pytest.raises(AgentInUse, match="alpha"):
            with launcher.claim_agent("alpha"):
                pass
    finally:
        holder.kill()
        holder.wait()


def test_the_name_is_released_when_its_holder_is_killed() -> None:
    """A crashed instance must not own a name forever."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\n"
            "from slife2.launcher import claim_agent\n"
            "with claim_agent('beta'):\n"
            "    print('claimed', flush=True)\n"
            "    time.sleep(30)\n",
        ],
        env={**os.environ, "PYTHONPATH": REPO},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "claimed"
        holder.kill()
        holder.wait()
        wait_until_free("beta")
        with launcher.claim_agent("beta"):
            pass
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_the_same_process_cannot_claim_a_name_twice() -> None:
    """The kernel lock alone does not give this.

    A Windows named mutex is recursive for its owning thread and a POSIX `flock`
    belongs to the open file description, so a second claim inside one process
    would quietly succeed — the lock is a cross-process guard and says nothing
    about re-entry.
    """
    with launcher.claim_agent("gamma"):
        with pytest.raises(AgentInUse, match="gamma"):
            with launcher.claim_agent("gamma"):
                pass


def test_a_live_client_is_visible_to_others() -> None:
    """This is what makes "the last one out" answerable at all."""
    from slife2 import runtime

    with launcher.claim_agent("delta"):
        live = runtime.live_clients()
        assert [c.agent for c in live] == ["delta"]
        # A second instance would see us...
        assert [c.agent for c in launcher.others_running()] == []
        # ...and we are the only one, so the servers would be ours to stop.
    assert runtime.live_clients() == []


def test_a_killed_client_stops_counting() -> None:
    """A crash must not keep the servers alive forever."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\n"
            "from slife2.launcher import claim_agent\n"
            "with claim_agent('ghost'):\n"
            "    print('claimed', flush=True)\n"
            "    time.sleep(30)\n",
        ],
        env={**os.environ, "PYTHONPATH": REPO},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "claimed"
        assert len(launcher.others_running()) == 1

        holder.kill()
        holder.wait()
        # Process teardown is not instantaneous — the pid can read as alive for
        # a moment after `wait()` returns.  Production has the same window, and
        # it fails in the safe direction: a lingerer means the servers stay up
        # until the next exit or a `slife2 down`.
        deadline = time.monotonic() + 5
        while launcher.others_running() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert launcher.others_running() == []
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_different_names_do_not_collide() -> None:
    """The servers are shared; only the label is exclusive."""
    with launcher.claim_agent("epsilon"):
        with launcher.claim_agent("zeta"):
            pass


def test_the_claim_is_cleared_on_the_way_out() -> None:
    from slife2 import runtime

    with launcher.claim_agent("eta"):
        assert runtime.read_claim("eta") is not None
    assert runtime.read_claim("eta") is None


# --- stop --------------------------------------------------------------------


def test_stop_leaves_a_reused_pid_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case that would otherwise kill whatever the user was running.

    The record names our own live pid but with a token from a process that no
    longer exists, which is exactly what a recycled pid looks like.
    """
    from slife2 import runtime

    runtime.write_record(
        runtime.ServerRecord(
            name=SPEC.name,
            url=SPEC.url,
            pid=os.getpid(),
            start_token="a-token-from-a-process-that-is-gone",
        )
    )
    killed: list[int] = []
    monkeypatch.setattr(
        launcher, "terminate", lambda pid, **k: killed.append(pid) or True
    )

    outcomes = launcher.stop(_config_with_only_deepseek())
    assert killed == [], "it terminated a pid it could not prove was ours"
    assert any("pid reused" in o.detail for o in outcomes)
    assert runtime.read_record(SPEC.url) is None  # the stale record is gone


def _config_with_only_deepseek() -> Config:
    return default_config()
