"""Choosing which servers to run, and attaching to the ones already there.

`ensure` is driven with stubbed probes rather than real servers, so the three
outcomes — reuse, start, conflict — are assertions instead of timing.  The one
thing that does use a second process is the agent-name claim, because a Windows
mutex is recursive within a thread and an in-process test would prove nothing.

**And that one thing is what the marker has to say.**  `integration` is I/O,
network or subprocess, and the claim tests here spawn real interpreters and read
their standard output — so the module was mislabelled `unit`, which is the lane
documented as "no I/O, no subprocess", and put several seconds of interpreter
startup into the lane somebody runs precisely because it is the fast one.  The
tests that need none of that stay with the file: they are the tables this
launcher is built from (which server each protocol is, what waits for what) and
a lane is a fact about a file rather than a chance to grade its tests.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Context

from slife2 import _ensure
from slife2.clients.toolclient import ToolCounts
from slife2.libs.host import launcher, runtime
from slife2.libs.host.config import (
    LOCAL_SERVERS,
    PLUGINS,
    Config,
    default_config,
    load,
)
from slife2.libs.host.launcher import AgentInUse, ServerSpec, Status

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


# --- which servers are needed ------------------------------------------------


def test_there_is_one_plugin_per_job(tmp_path) -> None:
    """One agent loop, one context store, one hub,
    one process per wire protocol.

    The granularity is the point: a backend speaks one wire protocol and does
    nothing else, and a provider is a row in that backend's config rather than a
    process of its own.  Four providers on three protocols is three model
    processes, not four.  Note which way the list is ordered, too — it is
    `API_BACKENDS` order, then the local plugins.  That order is what a *reader*
    gets and what anything sequential would fall back on; it is no longer the
    order they start in, which comes from `launcher.NEEDS` — the two edges are
    asserted just below.
    """
    config = load(_config_with_every_protocol(tmp_path))
    names = [spec.name for spec in launcher.specs(config)]
    assert names == [
        "llm:openai-completions",
        "llm:anthropic-messages",
        "llm:openai-responses",
        "embeddings",
        "context",
        "skills-server",
        "cli-server",
        "mcp-tools",
        "restapi-tools",
        "wechat",
        "cabinet",
        "media",
        "file-tunnel",
        "tui",
        "toolhub",
        "agent",
        "agent-task",
        "schedule",
        "peer-agent",
        "subagent",
    ]
    modules = {spec.module for spec in launcher.specs(config)}
    assert modules == {
        "slife2.plugins.llm.openai_server",
        "slife2.plugins.llm.anthropic_server",
        "slife2.plugins.llm.openai_responses_server",
        "slife2.plugins.llm.embeddings_server",
        "slife2.plugins.context.server",
        "slife2.plugins.skills.server",
        "slife2.plugins.cli.server",
        "slife2.plugins.mcp_tools.server",
        "slife2.plugins.restapi.server",
        "slife2.plugins.wechat.server",
        "slife2.plugins.cabinet.server",
        "slife2.plugins.media.server",
        "slife2.plugins.file_tunnel.server",
        "slife2.plugins.tui.server",
        "slife2.plugins.toolhub.server",
        "slife2.plugins.agent.server",
        "slife2.plugins.a2a.local_server",
        "slife2.plugins.agent_task.server",
        "slife2.plugins.schedule.server",
        "slife2.plugins.a2a.server",
    }


def test_the_startup_graph_names_our_servers_and_has_no_cycles() -> None:
    """The table is what makes the waves, so it has to be a graph.

    A name that is not one of ours is a wait that never ends, and a cycle is the
    same thing said twice — which is why `ensure_all` raises rather than starting
    what it cannot order.  Asserted here rather than left to that raise, because
    a launcher that discovers this at run time has already refused to start.
    """
    known = {spec.name for spec in launcher.specs(default_config())}
    assert set(launcher.NEEDS) <= known, "a dependency nobody can be waiting for"
    for needs in launcher.NEEDS.values():
        assert set(needs) <= known

    on_path: set[str] = set()
    settled: set[str] = set()

    def walk(name: str) -> None:
        assert name not in on_path, f"{name} waits on itself"
        if name in settled:
            return
        on_path.add(name)
        for dependency in launcher.NEEDS.get(name, ()):
            walk(dependency)
        on_path.discard(name)
        settled.add(name)

    for name in launcher.NEEDS:
        walk(name)


def test_a_server_is_asked_for_only_once_what_it_needs_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edge, watched as the order the attempts are *made* in.

    `context` opens the embedder before it serves anything, and `open_server`
    refuses a peer that is not listening — so a context attempted in the same
    wave as an embeddings is a context that fails to start.  Watched on the
    attempt rather than on the outcome, because with `ensure` stubbed every
    outcome is the same.
    """
    attempted: list[str] = []

    def fake_ensure(spec, *, config_path=None):
        attempted.append(spec.name)
        return launcher.Outcome(spec, Status.STARTED)

    monkeypatch.setattr(launcher, "ensure", fake_ensure)

    started = list(launcher.ensure_all(default_config()))

    assert len(started) == len(launcher.specs(default_config()))
    assert attempted.index("embeddings") < attempted.index("context")
    assert attempted.index("context") < attempted.index("toolhub")
    # Everything the table says nothing about was in the first wave — which is
    # the whole reason for having a table rather than a sequence.
    first_wave = attempted[: attempted.index("context")]
    assert "agent" in first_wave and "skills-server" in first_wave


def _reaches(name: str, target: str) -> bool:
    """Whether *name* waits on *target*, directly or through the table."""
    seen: set[str] = set()
    pending = [name]
    while pending:
        for dependency in launcher.NEEDS.get(pending.pop(), ()):
            if dependency == target:
                return True
            if dependency not in seen:
                seen.add(dependency)
                pending.append(dependency)
    return False


def test_the_hub_waits_for_every_plugin_it_can() -> None:
    """The hub's entry is a rule, and the rule is the whole set minus the cycle.

    The hub asks *every* plugin for its tools — including the ones with nothing
    to offer the model, because what a server has is not knowable without asking
    — and `list_tools` refuses while any of them is not answering
    (`slife2.plugins.toolhub.server`), so a plugin that comes up after the hub is a first turn
    that cannot run.  What it may not wait for is the plugins that wait for
    *it*, and this is the assertion that keeps that the only exception: it fails
    for a plugin nobody has decided about, which is exactly what `a2a` was — the
    hub asks it for four tools, and the hand-written tuple said otherwise.
    """
    waited_for = set(launcher.NEEDS["toolhub"])
    assert waited_for <= set(LOCAL_SERVERS)
    assert "toolhub" not in waited_for

    for name in LOCAL_SERVERS:
        if name in waited_for or name == "toolhub":
            continue
        assert _reaches(name, "toolhub"), (
            f"{name} is not waited for and does not reach the hub"
        )
    # And the exclusion is not an excuse: each one really does wait on the hub.
    for name in launcher._AFTER_THE_HUB:
        assert name not in waited_for
        if name != "toolhub":
            assert _reaches(name, "toolhub")


def test_a_server_whose_dependency_failed_is_reported_and_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second failure describing the first one is noise.

    And it is worse than noise: a start that could not work spends its wait and
    leaves a process to clean up, to say nothing of the line it prints.  What the
    reader needs is the server that actually failed, plus a note of who was
    waiting on it.
    """
    attempted: list[str] = []

    def fake_ensure(spec, *, config_path=None):
        attempted.append(spec.name)
        if spec.name == "embeddings":
            return launcher.Outcome(spec, Status.FAILED, detail="no key")
        return launcher.Outcome(spec, Status.STARTED)

    monkeypatch.setattr(launcher, "ensure", fake_ensure)

    outcomes = {o.spec.name: o for o in launcher.ensure_all(default_config())}

    assert outcomes["context"].status is Status.FAILED
    assert "embeddings" in outcomes["context"].detail
    assert "context" in outcomes["toolhub"].detail, "the transitive edge is an edge"
    assert "context" not in attempted
    assert "toolhub" not in attempted
    # ...while a server that needed none of it is untouched.
    assert outcomes["agent"].ok


def test_the_servers_are_worked_on_at_once() -> None:
    """A barrier of exactly N: only overlapping calls can all pass it.

    Timed, because a serial implementation does not fail this test — it hangs on
    the first wait, and a hang is a worse failure than an assertion.  The barrier
    turns that into an error with a message.
    """
    batch = [
        replace(SPEC, name=f"server{n}", url=f"http://127.0.0.1:810{n}/mcp")
        for n in range(3)
    ]
    gate = threading.Barrier(
        len(batch),
        timeout=10,  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget

    def work(spec: ServerSpec) -> launcher.Outcome:
        gate.wait()
        return launcher.Outcome(spec, Status.RUNNING)

    outcomes = list(launcher._concurrently(work, batch))

    assert [o.spec.name for o in outcomes] != []
    assert len(outcomes) == len(batch)


def test_the_work_is_reported_in_the_order_it_finishes() -> None:
    """Which is the only order that is true once several are in flight.

    It is also what a caller printing as it goes is watching for: the line that
    appears last is the one that took the time.

    **The slow one is held by an event rather than by a sleep.**  What the test
    is about is *when* an outcome is handed over, and `time.sleep(0.3)` made that
    a question about the thread pool instead: on a loaded machine the "slow"
    worker can start late enough that both finish in `batch` order, and the
    assertion then fails at a test that is not wrong — while costing a third of a
    second in the `unit` lane on every run either way.  Held by an event the
    consumer sets, the order is not a race: the first outcome cannot be yielded
    until `fast` has returned, because `slow` is still waiting for a signal that
    only this loop sends — and a `_concurrently` that buffered its results
    reports that as the assertion inside the worker, bounded, rather than as a
    test that hangs.
    """
    batch = [
        replace(SPEC, name="slow", url="http://127.0.0.1:8201/mcp"),
        replace(SPEC, name="fast", url="http://127.0.0.1:8202/mcp"),
    ]
    released = threading.Event()

    def work(spec: ServerSpec) -> launcher.Outcome:
        if spec.name == "slow" and not released.wait(timeout=5):
            # The magnitude is a hang detector, not a budget: five seconds is
            # long past anything this needs to finish in.
            raise AssertionError("nothing was yielded until everything had finished")
        return launcher.Outcome(spec, Status.RUNNING)

    seen: list[str] = []
    for outcome in launcher._concurrently(work, batch):
        seen.append(outcome.spec.name)
        released.set()

    assert seen == ["fast", "slow"]


def test_the_servers_started_are_the_ones_the_registry_describes(tmp_path) -> None:
    """What a `status` line prints is `PLUGINS`, and nothing else adds to it.

    **This used to hold two lists together** — `config.LOCAL_SERVERS` and
    `launcher.SERVER_MODULES` — which is the test that stops existing when the
    two become one: there is no second list to disagree with the first, and a
    row is what a spec is built from (`launcher.spec_for`).  What is left worth
    asserting is that the launcher neither invents a server nor drops one, which
    is what a derived table can still get wrong.
    """
    config = load(_config_with_every_protocol(tmp_path))
    started = [spec.name for spec in launcher.specs(config)]
    described = [
        f"{launcher.BACKEND_PREFIX}{one.key}" if one.api else one.key for one in PLUGINS
    ]
    assert sorted(started) == sorted(described)
    assert set(LOCAL_SERVERS) == {one.key for one in PLUGINS if not one.api}


def test_the_hub_asks_exactly_what_the_launcher_starts(tmp_path) -> None:
    """The third thing that could drift, and the one with teeth.

    The hub reaches into our plugins for the model's tools, and it asks every
    one of them because it cannot know which have anything to offer.  So the set
    it asks has to be the set that exists: a plugin the launcher starts and
    the hub does not ask is a whole server's worth of tools nobody can see, and
    one the hub asks and nothing starts is a connection that can only fail —
    which, a plugin being required, fails the tool list of every turn.
    """
    config = load(_config_with_every_protocol(tmp_path))
    started = {
        spec.name.removeprefix(launcher.BACKEND_PREFIX)
        for spec in launcher.specs(config)
    }
    assert set(config.plugins()) == started


def test_one_server_per_wire_protocol(tmp_path) -> None:
    """Not one per provider — that is what this replaced.

    A process speaks one format, so every OpenAI-compatible provider shares it
    and `stream_chat(provider=...)` says whose credentials to use.  Two
    providers on one protocol is one process.
    """
    path = _config_with_every_protocol(tmp_path)
    names = [spec.name for spec in launcher.specs(load(path))]
    assert names == [
        "llm:openai-completions",
        "llm:anthropic-messages",
        "llm:openai-responses",
        "embeddings",
        "context",
        "skills-server",
        "cli-server",
        "mcp-tools",
        "restapi-tools",
        "wechat",
        "cabinet",
        "media",
        "file-tunnel",
        "tui",
        "toolhub",
        "agent",
        "agent-task",
        "schedule",
        "peer-agent",
        "subagent",
    ]


def _config_with_every_protocol(tmp_path) -> Path:
    """A config with one provider on each wire protocol, written into *tmp_path*.

    The test's own directory rather than `tempfile.mkdtemp()`: four tests read
    this, so the version that made a directory for itself left four of them in
    the system temporary directory on every run, and nothing ever removed them.
    """
    path = tmp_path / "all.yaml"
    path.write_text(
        """
providers:
  a: {api: openai-completions, base_url: 'https://example.test/v1', models: [{model: m}]}
  b: {api: openai-completions, base_url: 'https://example.test/v1', models: [{model: n}]}
  c: {api: anthropic-messages, base_url: 'https://example.test/v1', models: [{model: o}]}
  d: {api: openai-responses, base_url: 'https://example.test/v1', models: [{model: p}]}
default: a/m
""",
        encoding="utf-8",
    )
    return path


def test_the_api_chooses_the_module(tmp_path) -> None:
    modules = {
        spec.name: spec.module
        for spec in launcher.specs(load(_config_with_every_protocol(tmp_path)))
    }
    assert modules["llm:openai-completions"] == "slife2.plugins.llm.openai_server"
    assert modules["llm:anthropic-messages"] == "slife2.plugins.llm.anthropic_server"
    assert (
        modules["llm:openai-responses"] == "slife2.plugins.llm.openai_responses_server"
    )


def test_a_protocol_no_provider_uses_is_not_started(tmp_path) -> None:
    """A server for a protocol nobody uses holds nobody's key."""
    path = tmp_path / "one.yaml"
    path.write_text(
        "providers:\n  a: {api: openai-completions, base_url: 'https://example.test/v1', models: [{model: m}]}\n",
        encoding="utf-8",
    )
    names = [spec.name for spec in launcher.specs(load(path))]
    assert names == [
        "llm:openai-completions",
        "embeddings",
        "context",
        "skills-server",
        "cli-server",
        "mcp-tools",
        "restapi-tools",
        "wechat",
        "cabinet",
        "media",
        "file-tunnel",
        "tui",
        "toolhub",
        "agent",
        "agent-task",
        "schedule",
        "peer-agent",
        "subagent",
    ]


def test_the_agent_server_takes_no_provider() -> None:
    spec = next(s for s in launcher.specs(default_config()) if s.name == "agent")
    assert "--provider" not in launcher._argv(spec)


def test_every_peer_each_server_reaches_starts_before_it() -> None:
    """The plugins that *reach* a peer are ordered by which peer they reach.

    Starting them together races, and the failure is confusing rather than
    obvious: the plugin comes up healthy and every message — or every task — it
    hands over fails.  Which peer each one reaches is the table's business and
    the reasons are written down beside it, one entry at a time, so this is the
    place they are held to the code: an entry that outlives the call it
    described fails here, which is what `wechat`'s did — it was the agent server
    for a release after the delivery became the window's (SDD §9.1).
    """
    names = [spec.name for spec in launcher.specs(default_config())]
    assert names[-5:] == [
        "agent",
        "agent-task",
        "schedule",
        "peer-agent",
        "subagent",
    ]
    # WeChat delivers to the window and to nothing else: the terminal is the peer
    # that has to be there, and the agent server is not one it speaks to at all.
    assert set(launcher.NEEDS["wechat"]) == {"tui"}
    # A task reaches four: the hub its loop runs with, the two ways into a
    # conversation, and the store the task's own conversation is restored from.
    assert set(launcher.NEEDS["agent-task"]) == {"toolhub", "tui", "agent", "context"}
    # The clock reaches three peers, and asks the hub for nothing: it makes no
    # model call (SDD §9.4), so the terminal and the agent server behind it
    # are where a trigger goes, and `agent-task` is where a run's work is started
    # and asked after — the third edge is the one that lets a run close itself.
    assert set(launcher.NEEDS["schedule"]) == {"tui", "agent", "agent-task"}
    # The mesh reaches two, and it is the same pair for the same reason
    # (SDD §9.2): what arrives from a peer is a message handed to a
    # conversation, wherever the conversation can be reached.
    assert set(launcher.NEEDS["peer-agent"]) == {"tui", "agent"}
    # And the local network is the same pair: it is the same module serving
    # subagents rather than peers, and what it hands over is handed over the
    # same way.
    assert set(launcher.NEEDS["subagent"]) == {"tui", "agent"}
    # Everything else is a dependency of one of them, whatever kind it is.
    assert set(names[:-5]) == {
        "llm:openai-completions",
        "embeddings",
        "context",
        "skills-server",
        "cli-server",
        "mcp-tools",
        "restapi-tools",
        "wechat",
        "cabinet",
        "media",
        "file-tunnel",
        "tui",
        "toolhub",
    }


def test_specs_carry_where_each_server_listens() -> None:
    spec = next(
        s for s in launcher.specs(default_config()) if s.name.startswith("llm:")
    )
    assert (spec.host, spec.port, spec.expected_name, spec.expected_tool) == (
        "127.0.0.1",
        24001,
        "slife2-llm-openai",
        "stream_chat",
    )
    assert spec.url == "http://127.0.0.1:24001/mcp"


# --- ensure ------------------------------------------------------------------


SPEC = ServerSpec(
    name="llm:openai-completions",
    module="slife2.plugins.llm.openai_server",
    url="http://127.0.0.1:24001/mcp",
    host="127.0.0.1",
    port=24001,
    expected_name="slife2-llm-openai",
    expected_tool="stream_chat",
)


def _register(spec: ServerSpec, *, config: str = "") -> None:
    """Stand in for a server that has registered itself."""
    runtime.write_record(
        runtime.ServerRecord(
            name=spec.name, url=spec.url, pid=os.getpid(), config=config
        )
    )


@pytest.mark.asyncio
async def test_probe_refuses_to_run_inside_an_event_loop() -> None:
    """A programming error must not look like an absent server.

    `asyncio.run` raises when a loop is already running, and an
    `except Exception` around it turns "you called this wrong" into "nothing is
    running" — a wrong answer that looks exactly like a right one.  That is what
    this used to do, quietly, with a `coroutine was never awaited` warning as
    the only sign.
    """
    with pytest.raises(RuntimeError, match="cannot run inside an event loop"):
        launcher.probe(
            SPEC,
            timeout=0.2,  # timeouts: the value this test is built around, not a budget
        )  # timeouts: the value this test is built around, not a budget


@pytest.mark.asyncio
async def test_open_catalogue_refuses_to_run_inside_an_event_loop() -> None:
    """`probe`'s rule, for the other thing a client does before it has a loop.

    Reached the same way and for the same reason: `asyncio.run` raises when a loop
    is already running, and catching that turns "you called this wrong" into a
    start-up message about the hub — a wrong answer that reads like a right one.
    """
    with pytest.raises(RuntimeError, match="cannot run inside an event loop"):
        launcher.open_catalogue(default_config())


def test_open_catalogue_asks_the_hub_for_this_homes_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The ask is a turn's ask**, made once with this process's home on it.

    `agents/tools.db` is the hub's file — one writer, and the writer is the
    process that opened it — so a home's catalogue cannot be made by writing it
    here.  What makes it is asking the hub the question a turn asks
    (`list_tools`), which is what `open_catalogue` is: the same call, the same
    reader (`slife2.clients.toolclient.remote_catalogue`), and **the catalogue's
    count** rather than the length of the list that came back — the two are
    different numbers, and the status bar shows the first.

    **And the home is on it, not an agent.**  `Peer.call_tool` adds this process's
    home to every call it makes, and that is what the hub resolves the file
    against — so the test drives a real `Peer` and reads the `_meta` the far side
    was handed, rather than asserting on a stub that would have agreed with
    whatever this end sent.
    """
    from fastmcp import FastMCP

    from slife2.libs.host.paths import data_dir
    from slife2.libs.plugin.audience import home_of, request_meta
    from slife2.libs.plugin.protocol import Peer

    hub = FastMCP("slife2-toolhub")
    seen: dict[str, Any] = {}

    @hub.tool
    async def list_tools(ctx: Context, wait: bool = True) -> dict[str, Any]:
        seen["meta"] = dict(request_meta(ctx) or {})
        return {
            "tools": [
                {
                    "name": name,
                    "server": "toolhub",
                    "tool": name,
                    "description": "",
                    "parameters": {},
                }
                for name in ("tool_search", "func_tool_load")
            ],
            # Larger than the list on purpose: the loaded set is two tools and
            # the catalogue the window's bar reports holds more — skills, CLI
            # entries, the rest of the hub's own.
            "total": 17,
        }

    async def dial(url: str, **kwargs: Any) -> Any:
        seen["url"] = url
        client = Peer(hub)
        await client.__aenter__()
        return client

    monkeypatch.setattr(launcher, "open_server", dial)
    config = default_config()

    # Both numbers the hub answered: the two tools in its list, and the
    # catalogue they were chosen from.
    assert launcher.open_catalogue(config) == ToolCounts(loaded=2, total=17)
    assert seen["url"] == config.server("toolhub").url
    assert home_of(seen["meta"]) == str(data_dir())


def test_a_spawned_server_is_told_where_the_data_directory_is(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Always, not only when an environment variable happens to be set.

    The child is spawned with its working directory moved off the checkout, so
    one left to work it out for itself decides it is an installation, looks in
    `~/.slife2`, and finds no config there — while the parent, which read the
    config it was pointed at, believes otherwise.  The symptom is a server that
    refuses to start over a file its parent has just read:

        slife2-llm-anthropic: this config has no anthropic-messages provider

    **What is asserted is the value, not the module that wrote it**, and the two
    are not the same test: the expectation here used to be
    `str(launcher.data_dir())` — the very call `_argv` makes — so it held for any
    answer at all, including the checkout the process happened to be sitting in.
    The two halves are what the docstring claims: the child is handed *this*
    test's home when the variable names one, and an absolute path to the
    checkout when it does not.
    """
    spec = next(
        s for s in launcher.specs(default_config()) if s.name.startswith("llm:")
    )

    argv = launcher._argv(spec)
    assert "--data-dir" in argv
    given = argv[argv.index("--data-dir") + 1]
    assert given == str(isolated_runtime.resolve()), (
        "the child is told this home, and told where it really is"
    )

    # With no variable to read, `slife2.libs.host.paths` answers the checkout —
    # which is where a `slife2.yaml` beside the code is looked for, and so the
    # directory a server has to be told about to find the config this process
    # read.
    monkeypatch.delenv("SLIFE2_DATA_DIR", raising=False)
    argv = launcher._argv(spec)
    assert "--data-dir" in argv, "and it is told one with nothing to read"
    assert (Path(argv[argv.index("--data-dir") + 1]) / "pyproject.toml").is_file()


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


def test_each_server_is_reported_before_the_next_one_is_asked_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The startup report must not wait for the last server.

    `ensure` blocks until its server answers or gives up, which is a second or
    two each and ten of them — so a report held back to the end is a blank screen
    for the whole start, and, when one is stuck, no way at all to see *which* is
    what it exists to answer.  Asserted from *inside* the generator, because that
    is the only place the question can be asked: by the time the second server is
    asked for, the first line has to be on the screen.

    The line carries how long that server took and the summary how long they all
    did — and a failure says *which* status it failed with, in the words the
    config and `status` use rather than the name of an enum member.
    """
    printed_before_the_second: list[str] = []
    second = replace(SPEC, name="second", url="http://127.0.0.1:24002/mcp")
    third = replace(SPEC, name="third", url="http://127.0.0.1:24003/mcp")

    def fake_ensure_all(config, *, config_path=None):
        yield launcher.Outcome(SPEC, Status.STARTED, seconds=1.5)
        printed_before_the_second.append(capsys.readouterr().out)
        yield launcher.Outcome(second, Status.RUNNING, seconds=0.01)
        yield launcher.Outcome(third, Status.CONFLICT, seconds=0.3)

    monkeypatch.setattr(launcher, "ensure_all", fake_ensure_all)

    assert _ensure(default_config(), None) == 2, "a failed server is an exit code"

    (first_line,) = printed_before_the_second
    assert "starting -> " in first_line, "the first server was not reported"
    assert "1.5s" in first_line

    out = capsys.readouterr().out
    assert "running  -> " in out
    assert "10ms" in out, "a warm reuse is timed too, and 10ms is what it took"
    assert "port held by something else" in out, "not `Status.CONFLICT`"
    assert "3 servers in" in out


def test_status_reads_the_record_the_way_down_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server answering with no record of ours was started by hand.

    `down` refuses to stop it for exactly that reason, so `status` calling the
    same process merely "running" is two commands disagreeing about one
    process — and the "(not started by slife2)" branch in the CLI's output was
    unreachable, which is how the disagreement stayed invisible.
    """
    monkeypatch.setattr(launcher, "probe", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "specs", lambda config: [SPEC])

    (hand_started,) = launcher.statuses(default_config())
    assert hand_started.status is Status.UNMANAGED
    assert "not started by slife2" in hand_started.detail

    _register(SPEC)
    (ours,) = launcher.statuses(default_config())
    assert ours.status is Status.RUNNING


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
    assert "24001" in outcome.detail


def test_a_missing_server_is_spawned_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"spawned": False}

    def fake_probe(spec, **kwargs):
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
    argv = launcher._argv(SPEC)
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "slife2.plugins.llm.openai_server"]
    assert "--data-dir" in argv


# --- the agent name is exclusive ---------------------------------------------


def wait_until_free(
    name: str,
    *,
    timeout: float = 8.0,  # timeouts: the value this test is built around, not a budget
) -> None:  # timeouts: the value this test is built around, not a budget
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
            "from slife2.libs.host.launcher import claim_agent\n"
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
            "from slife2.libs.host.launcher import claim_agent\n"
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
    from slife2.libs.host import runtime

    with launcher.claim_agent("delta"):
        live = runtime.live_clients()
        assert [c.agent for c in live] == ["delta"]
        # A second instance would see us...
        assert [c.agent for c in launcher.others_running()] == []
        # ...and we are the only one, so the servers would be ours to stop.
    assert runtime.live_clients() == []


def test_others_running_is_only_about_whose_pid_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token half of the test is gone, because `live_clients` already made it.

    `live_clients` admits a record only when the pid is alive *and* the token
    matches (or is absent), so the only question left for `others_running` is
    whose pid each surviving record bears.  A record naming our own pid has
    therefore already been proved to be this process, and one naming any other
    pid is somebody else by definition; the removed "is this me" disjunct could
    not have changed the answer, which is why it was unreachable.
    """
    ours = runtime.ClientRecord(pid=os.getpid(), agent="me")
    theirs = runtime.ClientRecord(pid=os.getpid() + 1, agent="other", start_token="x")
    monkeypatch.setattr(launcher, "live_clients", lambda: [ours, theirs])

    assert launcher.others_running() == [theirs]


def test_a_killed_client_stops_counting() -> None:
    """A crash must not keep the servers alive forever."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\n"
            "from slife2.libs.host.launcher import claim_agent\n"
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
        deadline = (  # timeouts: the value this test is built around, not a budget
            time.monotonic() + 5
        )  # timeouts: the value this test is built around, not a budget
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
    from slife2.libs.host import runtime

    with launcher.claim_agent("eta"):
        assert runtime.read_claim("eta") is not None
    assert runtime.read_claim("eta") is None


def test_a_timeout_inside_the_claim_is_not_a_name_someone_holds() -> None:
    """**The claim's `except TimeoutError` used to wrap the caller's whole body.**

    The `try` opened before `with exclusive(...)` and closed after it, so the
    `yield` — every line of slife2's start-up and of `app.run()` — was inside it.
    A `TimeoutError` from anywhere in there (`toolclient.remote_tools` asks the
    hub under `work.hub`) came out as `AgentInUse("slife2", …) from None`, which
    is the one sentence a person would act on — "agent 'slife2' is already
    running (pid …)" — said about a name nothing held, with the real failure
    discarded by the `from None` on top of it.  The lock's own timeout is the
    only thing that can mean that, and the name is free.
    """
    with pytest.raises(TimeoutError):
        with launcher.claim_agent("a-name-nothing-holds"):
            raise TimeoutError("the hub did not answer in time")

    # And the claim is not left behind by the failure that went through it.
    assert "a-name-nothing-holds" not in launcher._HELD_NAMES
    with launcher.claim_agent("a-name-nothing-holds"):
        pass


# --- stop --------------------------------------------------------------------


def test_stop_leaves_a_reused_pid_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case that would otherwise kill whatever the user was running.

    The record names our own live pid but with a token from a process that no
    longer exists, which is exactly what a recycled pid looks like.
    """
    from slife2.libs.host import runtime

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


def test_stop_calls_a_record_whose_process_is_gone_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other reading of the same check, and the one a failed start leaves.

    A launcher that kills a server which never became ready leaves a record
    naming a pid that is not there any more.  Nothing was recycled, and the
    report says so instead of repeating the explanation next door.
    """
    runtime.write_record(
        runtime.ServerRecord(
            name=SPEC.name,
            url=SPEC.url,
            pid=999_999,
            start_token="a-token-nothing-is-holding",
        )
    )
    killed: list[int] = []
    monkeypatch.setattr(
        launcher, "terminate", lambda pid, **k: killed.append(pid) or True
    )

    outcomes = launcher.stop(_config_with_only_deepseek())
    assert killed == []
    assert any("the process is gone" in o.detail for o in outcomes)
    assert not any("pid reused" in o.detail for o in outcomes)
    assert runtime.read_record(SPEC.url) is None


def _config_with_only_deepseek() -> Config:
    return default_config()


def test_stop_keeps_the_record_of_a_process_that_did_not_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Clearing the record is how a surviving daemon is forgotten.**

    `terminate` answering False means the process is still there — the grace
    period ran out, or the kill was refused — and that is precisely when the
    record still has work to do: the pid and the start token in it are the whole
    of what lets the next `slife2 down` prove the process is ours and signal it
    again.  Clearing it forgot the daemon, so a second `down` found no record,
    answered `UNMANAGED` — "(not started by slife2)" — and never touched it
    again, while `status` said the same about a port that is still held.  The
    two cases the tree already covers both leave before this line (`same_process`
    is False for a reused pid and a gone one), so neither of them reached it.

    The record names *this* process with its real start token, so everything
    above the `terminate` call runs as it does in production and only that call
    is stubbed.
    """
    token = runtime.process_start_token(os.getpid())
    assert token, "the platform this runs on has no start token to write"
    runtime.write_record(
        runtime.ServerRecord(
            name=SPEC.name, url=SPEC.url, pid=os.getpid(), start_token=token
        )
    )
    monkeypatch.setattr(launcher, "terminate", lambda pid, **k: False)

    # Materialised once: `stop` is a generator, so a second `any` over it would
    # be asking the servers behind the first one.
    outcomes = list(launcher.stop(_config_with_only_deepseek()))

    assert any(outcome.status is Status.FAILED for outcome in outcomes)
    assert any("did not exit" in outcome.detail for outcome in outcomes)
    assert runtime.read_record(SPEC.url) is not None, (
        "the record is the only thing that can find that pid again"
    )
