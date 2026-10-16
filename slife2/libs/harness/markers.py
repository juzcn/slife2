"""`[Name:{…}]` — the marker a plugin puts in front of a message it delivers, and
how a screen takes one back off.

A marker is **content**: it goes into the turn's row along with the message and
comes back out of it verbatim, which is what its word-shaped spelling says and
`[TURN:…]`'s capitals do not (DESIGN.md §1.2).  Its reader is the model — for
WeChat it carries the two ids a reply needs, and for an agent task it says which
task an answer came from — and a screen has the channel already, as the bubble's
own prefix, so it removes one before drawing.

**This module holds the shape and no name.**  Which prefixes exist is each
writer's own fact (`slife2.plugins.wechat.server.MARKER_PREFIX`,
`slife2.plugins.agent_task.server.MARKER_PREFIX`) and the caller hands them in.  A table
of markers here would be the second place a marker is spelled, and the comment
in `slife2.tui.widgets` says why that is the failure: the module that writes a
marker is the one that knows what it looks like, and a copy drifts into printing
the raw marker at somebody.

Nothing here parses a marker, compares two of them, or reads the case — the
case is a convention about *where* a marker lives and no code has ever read it
(DESIGN.md §1.2).  This is a prefix and a function for removing one.

**Where a marker ends is a shape question, and it is asked as one.**  The end is
the `]` that closes the payload's own braces, found by a scan of quoting and
brace depth — `[Name:{…}]` is the shape both writers build, and a `]` inside a
JSON string is a character of somebody's words rather than the end of anything
(see `_marker_end`).  Nothing inside is read: no field is looked up and no value
is interpreted, which is all the old "nothing here parses a marker" ever meant.
"""

from __future__ import annotations

#: The characters the shape is made of: a brace-delimited payload between the
#: brackets, and the two that quote and escape a string inside it.  Named because
#: the scan below is about *this* shape rather than about brackets in general.
_PAYLOAD_OPEN = "{"
_PAYLOAD_CLOSE = "}"
_MARKER_END = "]"
_QUOTE = '"'
_ESCAPE = "\\"


def _marker_end(content: str, start: int) -> int:
    """The index just past a marker's closing bracket, or -1 if it is torn.

    **A payload's own brackets must not end the marker.**  The end used to be the
    first `]` after the prefix, and both writers put a `json.dumps` object there:
    an agent task's name is model-chosen free text and `json.dumps` does not
    escape `]`, so a task named `parse [draft] config` produced a marker that
    stopped inside its own payload and the raw JSON tail was drawn over the
    message.  The same line broke the promise the docstring above makes — a `]`
    inside the *message body* ended a torn marker early and ate the first words
    the person wrote.  Both callers are display paths, so neither is data loss;
    both are a wrong line, which is the kind nothing reports.

    So the end is found by the shape rather than by the next bracket: the payload
    is one brace-delimited object, a `"` opens a string that may hold any bracket
    at all, and `\\` escapes the character after it.  A marker whose braces never
    balance — a torn row, a hand-edited file — answers -1, and the caller shows
    it whole, which is what the docstring above promises.
    """
    if start >= len(content) or content[start] != _PAYLOAD_OPEN:
        return -1
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(content)):
        character = content[index]
        if escaped:
            escaped = False
        elif quoted:
            if character == _ESCAPE:
                escaped = True
            elif character == _QUOTE:
                quoted = False
        elif character == _QUOTE:
            quoted = True
        elif character == _PAYLOAD_OPEN:
            depth += 1
        elif character == _PAYLOAD_CLOSE:
            depth -= 1
        elif character == _MARKER_END and depth == 0:
            return index + 1
    return -1


def strip(content: str, *prefixes: str) -> str:
    """*content* as a person reads it: one of *prefixes* taken back off.

    **A leader, and a leader that closes.**  A marker is a prefix by
    construction, so text that merely *mentions* one is left alone — and so is a
    bracket that never closes, which is a torn row or a hand-edited file: a
    display that eats the rest of a message is worse than one that shows a
    marker it could not read.

    **A prefix with a payload that is not the shape is left alone too**, for the
    same reason: a row nobody's writer built is one this function cannot say the
    end of, and showing it whole is the mistake that costs a reader least.

    A prefix that is not there changes nothing, so a caller may hand in every
    marker it knows about without first asking which one it has — which is what
    the window does, and what keeps adding a third from being a change here.
    """
    for prefix in prefixes:
        if not content.startswith(prefix):
            continue
        end = _marker_end(content, len(prefix))
        if end != -1:
            return content[end:].lstrip()
    return content
