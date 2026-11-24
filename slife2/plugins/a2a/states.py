"""The A2A vocabulary both sides read, spelled once.

The profile's terminal words and the two non-terminal ones are the same facts
in `task_store`, `inbound_store`, the responder and the mesh.  A copy in each
module is a copy that drifts: the responder translates the wire's
``input-required``/``auth-required`` into two of these, and the mesh compares a
record's state against a word another module spelled.

The same rule holds for the two wire facts more than one module reads: which
characters are control characters (`CONTROL_RE` — the mesh scrubs them for
display, the group gates refuse a name carrying one), and how an MQTT v5 user
property is read (`user_property` — the mesh's presence and the responder's
context check ask the same question).  Nothing here imports a store or a
transport; it is the vocabulary both sides read.
"""

from __future__ import annotations

import re

#: A task that has not been handed to a round yet.
PENDING = "pending"
#: A round is live in the responder.
WORKING = "working"
#: The peer paused the task; a continuation can resume it.
INTERRUPTED = "interrupted"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
REJECTED = "rejected"

#: A task cannot leave these.  One tuple so a guard, the prune order and the
#: display cannot drift from one another.
TERMINAL_STATUSES = (COMPLETED, FAILED, CANCELLED, REJECTED)

#: Control characters a remote peer could use to break out of a single display
#: line in a handed-over message, the TUI, or logs (newlines, tabs, ESC, …).
#: One pattern for the two answers to it: the mesh's `safe_name` replaces them
#: for display, and a group name carrying one is refused.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def user_property(props, key: str) -> str:
    """One MQTT v5 user property value, or ``""``.

    The properties carry a list of ``(k, v)`` pairs; the first pair with this
    key is the value, and a message that carries none reads as empty.  The
    mesh's presence and the responder's context check ask the same question,
    so they read it through one function rather than two copies of the rule.
    """
    for name, value in getattr(props, "UserProperty", None) or []:
        if name == key:
            return str(value)
    return ""
