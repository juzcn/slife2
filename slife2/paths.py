"""Where slife2 keeps things.

**One data directory, and everything is under it.**  Daemon bookkeeping and the
databases are both data, and giving each its own root means two knobs, two
places to look when something is missing, and two answers to "where did that
go?" — so there is one root and the parts are subdirectories of it.

    <data>/
      runtime/   records, locks, logs, claims — a daemon's bookkeeping
      turns/     <agent>.turn.db — the turns, one file per agent

The split that remains is the one that matters: `runtime/` is *reconstructible*
— delete it and the next start rebuilds whatever it needs — while `turns/` is
not.  Anything that would be a disaster to lose does not go in `runtime/`.

`SLIFE2_DATA_DIR` moves the whole thing, which is what tests use to keep off a
developer's real state.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The environment variable naming the data directory.
DATA_ENV_VAR = "SLIFE2_DATA_DIR"


def data_dir() -> Path:
    """The one directory slife2 keeps its files in.

    The platform's data location rather than the repository: a wheel has no
    repository, two checkouts on one machine would otherwise fight over the same
    files, and a database inside a git working tree is one somebody eventually
    commits.
    """
    override = os.environ.get(DATA_ENV_VAR)
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        path = Path(base) / "slife2"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        path = Path(base) / "slife2"

    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_dir() -> Path:
    """Where a daemon records that it is running.  Safe to delete."""
    path = data_dir() / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def turns_dir() -> Path:
    """Where the per-agent databases live.  Not safe to delete."""
    path = data_dir() / "turns"
    path.mkdir(parents=True, exist_ok=True)
    return path
