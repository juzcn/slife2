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

**Who a channel saw arrive is the producer's; how an arrival is spelled is not.**
`⚡ jill online` comes from the peer's name, which only the mesh that saw the card
has — and `⚡ wechat online`, which is the same sentence about a *link* rather
than a peer.  Both go through the two functions below, so a channel cannot
announce itself in a vocabulary of its own: what it supplies is the name.
"""

from __future__ import annotations

from slife2.tui.theme import GLYPHS

#: The two glyphs every arrival and departure wears, named here because two
#: channels write these lines (`slife2.plugins.a2a.mesh`, `slife2.plugins.wechat.server`) and a glyph
#: spelled twice is a glyph that drifts once.
ARRIVED = "⚡"
GONE = GLYPHS["failed"]


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
    return f"{GONE} not on the mesh: {reason}"


def online(name: str) -> str:
    """`⚡ jill online` — a peer, or a channel's link, is up.

    `name` is whatever arrived: a peer's agent name (`slife2.plugins.a2a.mesh`) or the
    channel whose link came up (`slife2.plugins.wechat.server`, which says `wechat`).  One
    sentence for both, because to the person reading a transcript they are the
    same news — something this conversation can now reach.
    """
    return f"{ARRIVED} {name} online"


def offline(name: str) -> str:
    """`✗ jill offline` — and nothing reaches this conversation any more."""
    return f"{GONE} {name} offline"
