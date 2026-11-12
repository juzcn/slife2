"""The e2e lane's own watchdog, and the stack every scenario starts from.

The first half of this file is about a timer, the second about the arrangement
of processes a test is handed.  Both are here rather than in a test module
because every module in the lane shares them, and a fixture copied into a
second module is a fixture the two modules eventually disagree about.

`faulthandler_timeout` (180 s, `pyproject.toml`) is sized for the fast lane,
where the slowest test is ~7 s.  This lane is a different shape: every test's
fixture starts nineteen daemons, and the launcher's own readiness budget for
*one* of them is `timeouts.ready.plugin_start` — 300 s.  Under the fast lane's
bound a server that genuinely never comes up is killed by the watchdog before
`_wait_ready` can answer with its name and log tail, and the worker is reported
as "crashed while running …", which names no server at all.  So the lane
replaces the timer with the launcher's budget plus a margin, and a real start
failure arrives as the diagnosis it already is.

**A hook rather than `-o` on the command line**, because the lane is entered
from several places (`pyproject.toml`'s comment, both READMEs, and CI's step)
and a number repeated in each is a number one of them will lose.
`pytest_runtest_setup` is where the timer can be replaced: pytest's faulthandler
plugin arms the global one around the whole protocol, before the fixture setup
this lane is about, and the first test hook to run is this one.
"""

from __future__ import annotations

import faulthandler
import os
import socket
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import pytest
import pytest_asyncio
from _pytest.faulthandler import (
    fault_handler_stderr_fd_key,
    get_exit_on_timeout_config_value,
)
from fastmcp import Client

import slife2.helpers.timeouts as _T
from slife2.libs.host import launcher, statefile
from slife2.libs.host.config import LOCAL_SERVERS, load
from slife2.libs.host.paths import DATA_ENV_VAR
from tests.e2e.edges import (
    MODEL,
    WECHAT_AGENT,
    WINDOW,
    IlinkStub,
    MqttBroker,
    StubProvider,
)
from tests.fakes import a_home, wait_until_settled

#: Two minutes for the teardown that follows a start that failed.  A magnitude
#: of the kind this suite's timeout gate leaves to tests — "long enough" rather
#: than a daemon's budget, which is the registry's and is read at call time.
E2E_MARGIN = 120.0

#: The key the config names.  Not a `${VAR}`: a literal is resolved to itself,
#: and this way the test does not have to own an environment variable that a
#: stray `os.environ` inherited from the developer's shell could shadow.
API_KEY = "e2e-not-a-real-key"


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Re-arm the watchdog above the launcher's own readiness budget."""
    # The file descriptor pytest's plugin armed the global timer with: the one
    # dup'd at configure, because a worker's streams may be wrapped by the time
    # a test runs and not be files `faulthandler` can write to.  Its absence
    # means the plugin is off (`-p no:faulthandler`), and then there is no
    # timer to replace and no finally to cancel one armed here.
    stderr = item.config.stash.get(fault_handler_stderr_fd_key, None)
    if stderr is None:
        return
    faulthandler.cancel_dump_traceback_later()
    faulthandler.dump_traceback_later(
        _T.timeouts.ready.plugin_start + E2E_MARGIN,
        file=stderr,
        exit=get_exit_on_timeout_config_value(item.config),
    )


# ── the stack ───────────────────────────────────────────────────────────────

#: Where this worker's ports come from.  **A band per xdist worker, because the
#: obvious allocator is racy.**  Binding port 0, reading the number and closing
#: it hands back a port that belongs to nobody until somebody binds it, so two
#: workers drawing at the same moment can be given the same one — measured on
#: this machine at about one run in fifty with three workers.  The daemon that
#: loses the race cannot bind, and everything that depends on it is then
#: reported "not attempted: … did not come up", which names neither a port nor a
#: collision.  Bands cannot overlap, so the race is gone rather than unlikely.
_PORT_BASE = 20_000
_BAND_PORTS = 250


def _worker_band() -> int:
    """Which xdist worker this is.  A run with no xdist is band 0."""
    name = os.environ.get("PYTEST_XDIST_WORKER", "")
    return int(name[2:]) if name[:2] == "gw" and name[2:].isdigit() else 0


def _free_ports(names: Iterable[str]) -> dict[str, int]:
    """One port per name, all of them from this worker's own band.

    The config cannot say `port: 0` (`slife2.libs.host.config._server` refuses
    it, for the reason a port is how a launcher finds a server again), so a test
    that must not collide with a developer's running stack picks them itself.

    **Every socket is held until the whole set has been chosen**, so nothing
    else — another worker, or the next port this loop asks for — can be handed
    one of them.  Releasing each as it is found would leave the same race one
    call smaller.
    """
    start = _PORT_BASE + (_worker_band() % 32) * _BAND_PORTS
    end = start + _BAND_PORTS
    chosen: dict[str, int] = {}
    held: list[socket.socket] = []
    port = start
    try:
        for name in names:
            while True:
                if port >= end:
                    raise RuntimeError(
                        f"no free port left in this worker's band "
                        f"({start}-{end}); something else here is holding them"
                    )
                probe = socket.socket()
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    probe.close()
                    port += 1
                    continue
                held.append(probe)
                chosen[name] = port
                port += 1
                break
    finally:
        for probe in held:
            probe.close()
    return chosen


def _config_text(provider_url: str, ports: dict[str, int], broker_port: int) -> str:
    servers = "\n".join(f"  {name}: {{port: {port}}}" for name, port in ports.items())
    return f"""\
# Written by the e2e test: no sections a turn does not need, and every address
# on a port this process picked.
servers:
{servers}

# **Deliberately not the default.**  One number bounds both sides of the
# arrangement — the harness shortening an output too large for a conversation,
# and a tool of ours handing text back — and a test that read it from the
# default would prove nothing about the config file every child reads.
tool_result:
  tokens: {WINDOW}

# `note_write` is the tool the turn test asks the model for, so it is in the
# list the model is offered.  Every other cabinet tool is on demand, which is
# what a plugin's tools are now; the first eight are what the system works by.
# The three after them are the unattended channels' own send tools: a channel
# scenario is a model *answering* on that channel, and a model can only choose
# a tool it was offered.
tool_load:
  autoload:
    - tool_search
    - tool_list
    - func_tool_load
    - _func_tool_unload
    - _check_new_input
    - skill_use
    - check_async
    - cancel_async
    - note_write
    - wechat_send_message
    - peer_agent_task_response
    - subagent_send_message

providers:
  stub:
    api: openai-completions
    base_url: {provider_url}
    api_key: {API_KEY}
    models:
      - model: small
        name: Stub
        context_window: 8000
        max_tokens: 1000

embeddings:
  providers:
    stub:
      base_url: {provider_url}
      model: {MODEL}
      api_key: {API_KEY}

# Both networks meet on the broker this process serves, because a machine meets
# its own subagents somewhere and a remote mesh that says nothing meets there.
# The edges are stubs — the broker and the bridge are the two networks whose
# far end is not slife2 — and everything between a message and an answer is the
# real stack.
peer_agent:
  broker: {{host: 127.0.0.1, port: {broker_port}}}

subagent:
  broker: {{host: 127.0.0.1, port: {broker_port}}}

default: stub/small
"""


def _seed_wechat_login(data: Path, base_url: str) -> None:
    """Write the login a person would have scanned, pointed at the stub bridge.

    The wechat plugin resumes saved logins from `agents/*/wechat.yaml` at start,
    so this file *is* the scan — there is no seam to fake and no tool call that
    could stand in for one.  `base_url` is the one field that makes the stub
    reachable: a saved login carries the node its token belongs to, which is
    how a real bridge redirects a scan to another host.
    """
    statefile.write(
        data / "agents" / WECHAT_AGENT / "wechat.yaml",
        {
            "agent": WECHAT_AGENT,
            "bot_token": "e2e-bot-token",
            "base_url": base_url,
            "ilink_user_id": "e2e-owner",
            "ilink_bot_id": "e2e-bot",
            "saved_at": time.time(),
            "get_updates_buf": "0",
        },
    )


@pytest_asyncio.fixture(loop_scope="function")
async def stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_dialer
) -> AsyncIterator[dict]:
    """The whole stack, up: the three edges, a config, every daemon, and a client.

    **One stack shape for every scenario**, with the network sections and a
    seeded WeChat login always present.  A test that needed a different config
    would be a test whose subject was the config; what the lane proves is that
    a channel's message crosses every process boundary, and each channel's
    section is turned on by the same file the daemons read.

    Torn down in the reverse order it came up, and *stopped* rather than left to
    the last-client rule: these daemons belong to a data directory under
    `tmp_path`, and a test that leaked them would leave eighteen processes
    holding ports for the rest of the session.
    """
    # **The launcher's own probe is a real dial, and the guard has to be told.**
    # `no_live_stack` replaces `open_server` in every module that dials, the
    # launcher among them, so a test cannot reach this machine's running stack.
    # `_wait_ready` probes through exactly that name — and every server it is
    # waiting for is one *this fixture just started*, on a port this test
    # picked, so the danger the guard exists for is not here.  Without this,
    # every probe of a daemon that is already serving is refused with the
    # guard's `RuntimeError`, `probe` reports it as "the probe itself failed",
    # and the stack never finishes starting: the lane's watchdog kills the
    # worker and the lane reports a crash that names no server at all.
    real_dialer("slife2.libs.host.launcher")
    provider = StubProvider()
    provider.start()
    ilink = IlinkStub()
    ilink.start()
    broker = MqttBroker()
    broker.start()
    config = None
    try:
        monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
        _seed_wechat_login(tmp_path, ilink.base_url)
        # **Derived from `LOCAL_SERVERS`, not written out.**  A hand-kept list is
        # a list that goes stale, and this one had: `subagent` was missing, so it
        # started on the built-in default port instead of a picked one — and
        # since these tests run concurrently, every worker's stack raced for that
        # one address.  The first to bind it won; the others found it answering,
        # called it "running (external)", and their teardown asserted on a server
        # they had never started.  The lane was red on every run and the
        # assertion below said nothing about ports.  Every plugin this config
        # names now gets a port nobody else is on, which is what `_config_text`'s
        # own comment promises.
        ports = _free_ports((*LOCAL_SERVERS, "openai-completions"))
        (tmp_path / "slife2.yaml").write_text(
            _config_text(provider.base_url, ports, broker.port), encoding="utf-8"
        )
        config = load()
        outcomes = list(launcher.ensure_all(config))
        failed = [one for one in outcomes if not one.ok]
        assert not failed, [
            (one.spec.name, one.status.value, one.detail) for one in failed
        ]
        # **The launcher's readiness is per process; the hub's links are the
        # hub's own.**  `agent-task`, `schedule`, `peer-agent` and `subagent`
        # start *after* the hub — they reach it, so it cannot wait for them — so
        # its first attempts fail while they come up.  A turn's `list_tools`
        # bounds its wait at `ready.list_settle` and is entitled to refuse with
        # "a plugin is not answering"; on a loaded runner that is what the
        # first turn after `ensure_all` gets.  The stack handed to a test is
        # therefore one the hub has linked to, not merely one whose processes
        # are serving.
        async with Client(config.server("toolhub").url) as hub:
            await wait_until_settled(hub)
        yield {
            "config": config,
            "provider": provider,
            "data": tmp_path,
            "ilink": ilink,
            "broker": broker,
        }
    finally:
        # **The daemons go first.**  They hold connections to the three edges,
        # and stopping an edge under a running child turns a clean take-down
        # into a log full of refused connections — which is not this fixture's
        # subject and would be the first thing a failing run's log showed.
        try:
            if config is not None:
                # **Consumed, not merely called.**  `stop` is a generator for
                # `ensure_all`'s reason — "nothing here runs until the first
                # `next`, so a caller that drops the iterator has started
                # nothing at all" — and dropping it stops nothing at all
                # either: this fixture leaked eighteen daemons on every run
                # while both tests went on passing, and the only trace was
                # eighteen python processes per run holding ports whose data
                # directory pytest had already deleted.  The assertion is the
                # half that makes the next leak a failure instead of a mess.
                stopped = list(launcher.stop(config))
                assert all(one.status is launcher.Status.STOPPED for one in stopped), [
                    (one.spec.name, one.status.value, one.detail) for one in stopped
                ]
        finally:
            broker.stop()
            ilink.stop()
            provider.stop()
