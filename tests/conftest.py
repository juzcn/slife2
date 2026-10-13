"""Shared test setup.

Two autouse fixtures, each guarding something every test would otherwise have to
remember, and the first is the difference between a test suite and a loaded gun:
**every test runs against its own data directory.**

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
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slife2 import tokens

#: The real resolver, captured before anything patches it.  `monkeypatch`
#: replaces the module *attribute*, so holding the function object is what lets
#: `real_tokenizer` put it back.
_REAL_RESOLVER = tokens._openai_encoding


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test at its own data directory."""
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture(autouse=True)
def no_tokenizer_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite off the network, and off a vocabulary load per worker.

    `slife2.tokens` sizes a conversation with `tiktoken`'s `o200k_base` when it
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
