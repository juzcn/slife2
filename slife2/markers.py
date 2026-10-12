"""`[Name:{…}]` — the marker a plugin puts in front of a message it delivers, and
how a screen takes one back off.

A marker is **content**: it goes into the turn's row along with the message and
comes back out of it verbatim, which is what its word-shaped spelling says and
`[TURN:…]`'s capitals do not (DESIGN.md §1.2).  Its reader is the model — for
WeChat it carries the two ids a reply needs, and for an agent task it says which
task an answer came from — and a screen has the channel already, as the bubble's
own prefix, so it removes one before drawing.

**This module holds the shape and no name.**  Which prefixes exist is each
writer's own fact (`slife2.wechat.MARKER_PREFIX`,
`slife2.agent_task_server.MARKER_PREFIX`) and the caller hands them in.  A table
of markers here would be the second place a marker is spelled, and the comment
in `slife2.tui.widgets` says why that is the failure: the module that writes a
marker is the one that knows what it looks like, and a copy drifts into printing
the raw marker at somebody.

Nothing here parses a marker, compares two of them, or reads the case — the
case is a convention about *where* a marker lives and no code has ever read it
(DESIGN.md §1.2).  This is a prefix and a function for removing one.
"""

from __future__ import annotations


def strip(content: str, *prefixes: str) -> str:
    """*content* as a person reads it: one of *prefixes* taken back off.

    **A leader, and a leader that closes.**  A marker is a prefix by
    construction, so text that merely *mentions* one is left alone — and so is a
    bracket that never closes, which is a torn row or a hand-edited file: a
    display that eats the rest of a message is worse than one that shows a
    marker it could not read.

    A prefix that is not there changes nothing, so a caller may hand in every
    marker it knows about without first asking which one it has — which is what
    the window does, and what keeps adding a third from being a change here.
    """
    for prefix in prefixes:
        if not content.startswith(prefix):
            continue
        end = content.find("]", len(prefix))
        if end != -1:
            return content[end + 1 :].lstrip()
    return content
