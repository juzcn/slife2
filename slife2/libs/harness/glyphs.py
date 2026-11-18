"""The visual vocabulary: every glyph the interface prints, spelled once.

Kept with the rest of the shared vocabulary rather than in the window's theme,
because two of them are written by *daemons*: a plugin composes an arrival line
in its own process and the window draws it, so the mark has to have a home both
sides can import from.  The window's colours stay in `slife2.tui.theme`; a colour
is a palette the terminal owns and no daemon renders.

All of these are outside ASCII, and the house rule is that anything reaching a
*console* must be ASCII.  These never do — Textual owns the terminal and writes
UTF-8 — but it is why the server logs and the CLI stay plain.
"""

from __future__ import annotations

GLYPHS: dict[str, str] = {
    "collapsed": "▸",  # right-pointing small triangle
    "expanded": "▾",  # down-pointing small triangle
    "running": "◌",  # dotted circle
    "done": "●",  # filled circle
    "error": "●",
    # The fourth state of a call's panel: the turn it belonged to ended while the
    # window was still waiting on it.  A glyph of its own rather than a second use
    # of one of the two above, because it answers neither of their questions —
    # nothing is running and nothing came back (`slife2.tui.widgets._status`).
    "interrupted": "⊘",  # circled division slash
    "failed": "✗",
    "check": "✓",  # the other half of `failed`
    # A plan item nobody has started.  Distinct from every call state above:
    # the plan's four statuses are the model's claims, and a panel's are the
    # harness's observations, so the two vocabularies do not share a mark.
    "todo": "○",
    "up": "↑",
    "down": "↓",
    "separator": "│",
    "ellipsis": "…",
    "thinking": "⟐",
    "vision": "◉",
    # The model picker's two, named apart from `collapsed` for the reason every
    # one of these is named apart: a cursor and a folded panel look alike and
    # mean nothing alike, so one name for both is a name that says neither.
    "cursor": "▸",
    "current": "★",
}
