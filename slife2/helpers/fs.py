"""The one containment rule for a path a model wrote.

`skill_set`, `media_dir` and the cabinet's `resolve` all take a relative name
from a model and turn it into a path on the operator's machine.  The rule is the
same one every time: resolve first, then require the result to be the root or
beneath it — a string check passes `scripts/../../..`, and a symlink only
escapes once it has been resolved.  What was being named differs, so each caller
writes its own refusal; the decision is not theirs to write three times.
"""

from __future__ import annotations

from pathlib import Path


def confined(root: Path, relative: str) -> Path | None:
    """`root / relative` resolved, or `None` when it leaves *root*.

    `None` rather than an exception because the callers refuse in their own
    vocabulary — a `ValueError` for the cabinet and a skill, a
    `MediaAdapterError` for an artifact — and with a sentence naming what the
    path was for.  The root is resolved once, so a caller reached through a
    symlink compares the same directory it will open.
    """
    root = root.resolve()
    candidate = (root / relative).resolve()
    return candidate if candidate == root or root in candidate.parents else None
