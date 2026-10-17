"""The small YAML file a plugin keeps beside an agent's own state.

Two write one: `slife2-wechat` its login (`<agent>/wechat.yaml`) and `slife2-a2a`
the inbound tasks a restart orphaned (`<agent>/inbound.yaml`).  Both had worked out
the same two things for themselves, and the second said so — its `_yaml`
docstring read "set up the way `slife2.plugins.wechat.server`'s is", which is a
cross-reference standing in for a call.

**A plain document, not an edited one.**  `slife2.libs.host.configfile` loads
`slife2.yaml` in round-trip mode to keep an operator's comments and quote styles;
these files are machine-written, so what they want is the opposite: a plain YAML
writer in block style, because the file is a document a person may open and read
(a login, a list of peers still owed a reply).

**The mechanics are shared; the policy is not.**  What a missing or unreadable
file means is each caller's own question and each answers differently — a corrupt
login is a cache miss that is logged and ignored, a corrupt ledger is a refusal
to start from nothing — so this hands back the reader and the writer, and the
store decides.  Reading is one line either way; writing is not, which is where
the duplication was.

**A write is a rename.**  A temporary file in the same directory and
`os.replace`, so a reader running concurrently — the poll loop rewrites a login
every time the cursor moves — sees the whole of the old file or the whole of the
new one and never half of either.  That is the guarantee
`slife2.libs.host.runtime` gives its records for the same reason; what is
different here is only the format.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


def reader() -> YAML:
    """A reader and writer for a state file, in block style.

    Block style rather than flow: an entry in one of these is a fact somebody
    may want to read, and `{a: 1, b: 2}` is a shape only a machine likes.
    """
    yaml = YAML()
    yaml.default_flow_style = False
    return yaml


def write(path: Path, payload: Mapping[str, Any]) -> None:
    """Write one state file, atomically.

    **Flushed and fsynced before the rename**, which is the half a plain
    `write_text` misses: `os.replace` publishes whatever the file descriptor
    holds, and a window that renamed before the bytes reached the disk is a
    crash that leaves a state file naming itself as complete and empty.  These
    files are how a restart knows what a previous process was holding, so that
    is the one failure they cannot have.

    Raises whatever the filesystem raises — an unwritable directory, a full
    disk — because what to do about it is the caller's: one of the two callers
    here logs it and carries on, and the other one is writing a login that the
    caller asked for by name.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as opened:
            reader().dump(dict(payload), opened)
            opened.flush()
            os.fsync(opened.fileno())
        os.replace(tmp, path)
    except BaseException:
        # **The temporary file does not outlive the attempt**: a write that
        # failed leaves the old file standing and nothing else beside it, which
        # is what someone looking at an agent's directory should find.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
