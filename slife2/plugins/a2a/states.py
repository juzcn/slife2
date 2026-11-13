"""The A2A task states, spelled once.

The profile's terminal words and the two non-terminal ones are the same facts
in `task_store`, `inbound_store`, the responder and the mesh.  A copy in each
module is a copy that drifts: the responder translates the wire's
``input-required``/``auth-required`` into two of these, and the mesh compares a
record's state against a word another module spelled.  Nothing here imports a
store or a transport; it is the vocabulary both sides read.
"""

from __future__ import annotations

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
