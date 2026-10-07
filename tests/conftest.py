"""Shared test setup.

One fixture, and it is the difference between a test suite and a loaded gun:
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
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every test at its own data directory."""
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"
