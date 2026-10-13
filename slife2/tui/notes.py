"""The words of every line the harness says to the person watching.

A transcript holds three kinds of line — what the person said, what the model
said, and what the *harness* did — and the third is the one nothing in the
conversation itself accounts for: history came back when the window opened, the
discriminator kept some turns and went to the log for others, a turn was
cancelled, a peer came online or went away.  v1 drew these from several places
and called the collection its activity feed; this is the one place their words
are written, so a reader who has learned `[restored 3 turns]` meets the same
shape everywhere and a producer cannot quietly invent a fourth spelling.

**The brackets are not here, and that is the point.**  `ChatView.add_note`
applies them, so the shape is a property of the widget rather than a convention
every caller has to remember — a line that skipped them would be a line nobody
could tell was the harness's.  What this module owns is the words *inside* them.

**A line that arrives from a plugin is not spelled here either.**  The A2A
plugin's presence lines (`⚡ jill online`) are written by the mesh that saw the
peer arrive (`slife2.a2a.mesh.format_presence_line`), for the same reason: the
producer is the only party that knows the fact.  What the window adds to those
is the bracket, and that is `add_note`'s.
"""

from __future__ import annotations

from slife2.tui.theme import GLYPHS


def as_turns(count: int) -> str:
    """`3 turns`, or `1 turn` — the phrase the counting notes are written in.

    Shared rather than spelled twice: the restored-history note and the
    discriminator's note are the same kind of line, and a reader who has learned
    to read one should not meet `1 turns` in the other.
    """
    return f"{count} turn" if count == 1 else f"{count} turns"


def restored(count: int) -> str:
    """The history a window drew when it opened."""
    return f"restored {as_turns(count)}"


def kept(kept: int, recalled: int) -> str:
    """What one turn's discriminator decided to run on."""
    return f"kept {as_turns(kept)}, recalled {recalled}"


def cancelled() -> str:
    """A turn the person stopped."""
    return "cancelled"


def not_on_the_mesh(reason: str) -> str:
    """The window could not put its agent on the A2A mesh, and why.

    The failure is the *window's* line rather than the mesh's — a mesh that
    refused to admit an agent has no way to say so to that agent's window, and a
    window that waited for one would report a successful join as a silence.
    """
    return f"{GLYPHS['failed']} not on the mesh: {reason}"
