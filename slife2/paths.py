"""Where slife2 keeps things.

**One data directory, and everything is under it.**  The config, the daemon's
bookkeeping and the databases are all data, and giving each its own root means
two knobs, two places to look when something is missing, and two answers to
"where did that go?" — so there is one root and the parts are subdirectories of
it.

    <data>/
      slife2.yaml   the config; absent means the built-in defaults
      runtime/      records, locks, logs, claims — a daemon's bookkeeping
      turns/        <agent>.turn.db — the turns, one file per agent

**Where that root is depends on what you are running.**  A checkout keeps it
beside itself, so the checked-in `slife2.yaml` is the one in use and the
databases are directories you can open; an installation keeps it at `~/.slife2`,
per-user and independent of wherever it was started from.  See `data_dir`.

The split that remains is the one that matters: `runtime/` is *reconstructible*
— delete it and the next start rebuilds whatever it needs — while `turns/` is
not.  Anything that would be a disaster to lose does not go in `runtime/`.

`SLIFE2_DATA_DIR` overrides the whole thing, which is what tests use to keep off
a developer's real state, and what `--data-dir` sets.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The environment variable naming the data directory.
DATA_ENV_VAR = "SLIFE2_DATA_DIR"


#: What a checkout of this project has at its root.  Its presence, naming this
#: project, is what "a development environment" means here.
_CHECKOUT_MARKER = "pyproject.toml"
_PROJECT_NAME = 'name = "slife2"'


def in_checkout(directory: Path | None = None) -> bool:
    """Whether this is a source checkout of slife2 rather than an installation.

    Deliberately a *specific* signal — a `pyproject.toml` that names this
    project — rather than "there is a `.git` here" or "there is a `slife2.yaml`
    here".  Both of those are true of other projects' directories and of a
    production folder somebody happened to put a config in, and being wrong
    about this decides where a database is written.
    """
    marker = (directory or Path.cwd()) / _CHECKOUT_MARKER
    try:
        return _PROJECT_NAME in marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def data_dir() -> Path:
    """The one directory slife2 keeps its files in.

    **A checkout keeps everything beside itself; an installation keeps it under
    the home directory.**  Working on slife2 means the config, the runtime state
    and the databases are the ones in front of you — the checked-in
    `slife2.yaml` is read, `turns/` is a directory you can open, and deleting
    the checkout deletes the lot.  An installation has no checkout to sit in, so
    it uses `~/.slife2`, which is per-user and survives whatever directory it
    happens to be started from.

    `SLIFE2_DATA_DIR` overrides both, and `--data-dir` sets that.
    """
    override = os.environ.get(DATA_ENV_VAR)
    path = Path(override) if override else (
        Path.cwd() if in_checkout() else Path.home() / ".slife2"
    )
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
