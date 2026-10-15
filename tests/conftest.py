"""Shared test setup.

Three autouse fixtures, each guarding something every test would otherwise have
to remember, and the first is the difference between a test suite and a loaded
gun: **every test runs against its own data directory.**

The reason is the last act of `slife2.main`.  When the last client exits it stops
the shared MCP servers, and "the last client" is decided by pid liveness against
whatever data directory is in force.  A test that reaches that path with the
default data directory therefore stops a developer's real daemons — and under a
checkout `data_dir()` resolves to *the checkout itself*, so the default is
exactly the directory they are running against.  The symptom is a server that
keeps disappearing rather than a failing test, which is the worst way for it to
show up.

Autouse, and here rather than in the two modules that first needed it, because
the tests it protects are the ones that never mention the data directory — and
because a hazard guarded in two files out of eight is a hazard that comes back
the next time somebody adds a file.  `test_smoke.py` already calls `main()`
directly from a module that had no such guard.

The second is about a dependency rather than a directory, and it is here for the
same reason: the token estimator will fetch a few megabytes of vocabulary over
the network the first time it is asked for one, and a suite that claims to need
no network must not acquire one because a test happened to size a turn.  Its
docstring below says what it pins and how a test opts back in.

The third is **the same gun as the first, pointed at the addresses**.  The two
that guard a directory cover `/agents`; nothing covered the ports, and a port is
what a hand-over needs.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from slife2.libs.turn import tokens

#: Every module that dials one of *our* servers, and the names it does it
#: through.  Named as strings because importing the plugin stack — `fastmcp`,
#: the loop, the model clients — is not something a conftest should make every
#: worker do at collection time; the fixture resolves them on first use.
#:
#: The list is the whole guard, so it is worth saying what each dial reaches:
#:
#:   * `handover` — the window, and the agent server as the fallback.  Both put
#:     words into somebody's conversation.
#:   * `agent_task_server` — the hub, the context store and the model for a
#:     task's own turn.  A forgotten `backend=` is a live, paid call.
#:   * `schedule_server` — the agent-task plugin it dispatches a run to.
#:   * `server.server` — the same three, for a turn of the agent's own, which is
#:     the one leak that would *write* to a developer's real conversation.
#:   * `context_server` — the model its discriminator needs to choose a context.
#:   * `llm.client` — `open_backend`, which is how anything above reaches a
#:     model server at all.
#:   * `embedder` — the embeddings endpoint, the other paid address.
#:
#: `slife2.tui.client` is deliberately **not** here: that one is how a test
#: reaches a server it started itself on an ephemeral port (`test_a2a`,
#: `test_tui_control`), and refusing it would take away the only way to exercise
#: a stream end to end.
_DIALERS: dict[str, tuple[str, ...]] = {
    "slife2.libs.harness.handover": ("open_server",),
    "slife2.plugins.agent_task.server": ("open_server", "open_backend"),
    "slife2.plugins.schedule.server": ("open_server",),
    "slife2.plugins.agent.server": ("open_server", "open_backend"),
    "slife2.plugins.context.server": ("open_server",),
    "slife2.clients.model": ("open_server",),
    "slife2.clients.embedder": ("open_server",),
}


@pytest.fixture(autouse=True)
def no_live_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put this machine's running slife2 out of every test's reach.

    **A config's addresses are the machine's, and a test's are nobody's.**  The
    data-directory guard above stops a test from stopping real daemons; this one
    stops it from *talking* to them, which is worse because it cannot be told
    apart from success.  A plugin takes its peers as arguments —
    `window_client`, `hub_client`, `context_client`,
    `backend`, `tasks_client` — and `None` means "the real address in the
    config", which in a test is the developer's stack listening on the standard
    ports.  So a test that forgot one does not fail: it types into a live
    conversation, or hands a live conversation a trigger for a task whose ledger
    is in a `tmp_path` the live process has never heard of.

    That is not hypothetical.  It is what a full run of this suite did to a
    running `jack`: a `daily` schedule set in `test_schedule.py` came due against
    the real clock and was handed to the real window, which ran a turn asking
    about a task its own ledger has never held — and every `test_agent_task.py`
    answer whose test passed no `window=` arrived in the same conversation, one
    per test, each one naming a task in a registry only the test process has.
    The model on the other end spent a turn on each, correctly reporting that
    none of it was its own.

    **`open_server` is the seam because every one of those dials goes through
    it**, and patching it in the *dialling module* is what this codebase already
    does when a test wants to say what a peer answered (`test_wechat`,
    `test_runtime`, `test_server`).  A test that wants a real one still gets it by
    patching the name itself — its `monkeypatch` is this same object and undoes
    first — and a test that merely *left a peer out* now fails with the sentence
    that names what to pass instead, which is the whole point: the leak is loud
    rather than silent.
    """

    def refuse(url: str, *args: Any, **kwargs: Any) -> Any:
        """Stand in for the connection, and say which peer was left out.

        The arguments are only read to name it — every dialler passes a
        different pair (`name=` for a plugin, `server_name=` and a model for a
        backend) — because the sentence is a diagnosis, and "hand over the
        window" is worth more than "hand over the peer".
        """
        left_out = ", ".join(
            str(part)
            for part in (kwargs.get("name"), kwargs.get("server_name"), *args)
            if part
        )
        raise RuntimeError(
            f"a test tried to open a real connection to {url}"
            + (f" ({left_out})" if left_out else "")
            + ". This machine's slife2 may be running, and a test that reaches it "
            "types into a live conversation. Hand the plugin the peer instead: "
            "`window_client=`, `hub_client=`, `context_client=`, "
            "`backend=`, `tasks_client=` — or patch this name in the module under "
            "test if the connection is the thing being tested."
        )

    for name, attributes in _DIALERS.items():
        module = importlib.import_module(name)
        for attribute in attributes:
            if hasattr(module, attribute):
                monkeypatch.setattr(module, attribute, refuse)


@pytest.fixture
def real_dialer(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Put a real `open_server` back in one module, and say so at the call site.

    **The escape `no_live_stack` promises, made askable.**  Two tests are *about*
    the dial rather than tripped up by it — one probes a port nothing is
    listening on to assert the sentence `open_server` writes for that, and one
    proves a dispatch that cannot reach its peer still closes the run — and both
    would pass with the refusal in place while testing nothing they claim to.
    Handing them the real function is the same move `real_tokenizer` makes for
    the vocabulary, and it is deliberately a *fixture* rather than each test
    patching the name itself: reaching for the original means importing where the
    original lives, and a test that has to know that is a test that has stopped
    being about its own subject.
    """

    def restore(module: str) -> None:
        real = importlib.import_module("slife2.libs.plugin.protocol").open_server
        monkeypatch.setattr(importlib.import_module(module), "open_server", real)

    return restore


#: The real resolver, captured before anything patches it.  `monkeypatch`
#: replaces the module *attribute*, so holding the function object is what lets
#: `real_tokenizer` put it back.
_REAL_RESOLVER = tokens._openai_encoding


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test at its own data directory — **and its own runtime**.

    Two roots, because they are two kinds of fact (`slife2.libs.host.paths`): the
    data directory is the home a test works in, and the runtime directory is
    where the *machine's* daemons record themselves.  The second one defaults to
    `~/.slife2/runtime`, which on a developer's machine is a directory their own
    running stack is using — so a suite that did not redirect it would write
    records, claims and locks into the real one, and a test that passed here
    would be one that fails on a machine with nothing running.
    """
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SLIFE2_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path / "data"


@pytest.fixture(autouse=True)
def no_tokenizer_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite off the network, and off a vocabulary load per worker.

    `slife2.libs.turn.tokens` sizes a conversation with `tiktoken`'s `o200k_base` when it
    can get it, and getting it means a few megabytes fetched over the network
    the first time anybody asks — once per *process*, which under `-n auto` is
    once per worker, from a cold cache: a dozen simultaneous downloads to answer
    a question about a turn's size.

    The suite's own rule is that nothing in it needs the network (README,
    "Tests"), and a test run is not the place to discover whether this machine
    has one.  So the resolver is pinned to "no vocabulary", which makes every
    test that sizes a turn use the script fallback — a branch that has to work
    anyway, since a machine with no network is supported rather than broken.
    `tests/test_tokens.py` puts the real one back by asking for the
    `real_tokenizer` fixture, and skips where there is genuinely none.
    """
    monkeypatch.setattr(tokens, "_openai_encoding", lambda: None)


@pytest.fixture
def tokenizer_resolver():
    """The real vocabulary resolver, for a test that wants to drive the load itself.

    Handed out rather than patched back in place, because what such a test wants
    is usually to decide *how* the load goes — `test_tokens.py` makes it raise,
    to check that a failure is paid for once rather than once per message.
    """
    return _REAL_RESOLVER


@pytest.fixture
def real_tokenizer(monkeypatch: pytest.MonkeyPatch, tokenizer_resolver) -> None:
    """Undo `no_tokenizer_download`, for the tests that are *about* the tokenizer.

    Skips rather than fails when the vocabulary cannot be had: the point of the
    fallback is that this machine is supported, so a machine without `tiktoken`'s
    vocabulary is a machine that runs the rest of the suite, not a red one.
    """
    monkeypatch.setattr(tokens, "_openai_encoding", tokenizer_resolver)
    if tokenizer_resolver() is None:
        pytest.skip("no o200k_base vocabulary on this machine")
