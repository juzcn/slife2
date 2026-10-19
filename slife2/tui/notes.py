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

**And the words are the interface's, so they are the language's.**  Every line
here goes through `t()` (`slife2.tui.i18n`), which is why a Chinese window reads
`[已恢复 3 轮]` where an English one reads `[restored 3 turns]`.  That the two
arrival lines are written by *daemons* rather than by the window is a wrinkle
worth knowing: a plugin composes its note in its own process, so `--lang` — a
flag on the window's own command line — does not reach it, and a daemon follows
the OS locale where a window follows the flag (`DESIGN.md` §7.5).
"""

from __future__ import annotations

from slife2.tui.i18n import t
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

    **The choice between the two spellings is made here**, in the one place that
    has the count: the table has no plural machinery and neither should it
    (`slife2.tui.i18n`), and a language whose noun does not inflect gives both
    keys the same value.
    """
    return t("turn_one", n=count) if count == 1 else t("turn_many", n=count)


def restored(count: int) -> str:
    """The history a window drew when it opened."""
    return t("restored", turns=as_turns(count))


def kept(kept: int, recalled: int) -> str:
    """What one turn's discriminator decided to run on."""
    return t("kept", turns=as_turns(kept), recalled=recalled)


def cancelled() -> str:
    """A turn the person stopped."""
    return t("cancelled")


def not_on_the_mesh(reason: str) -> str:
    """The window could not put its agent on the A2A mesh, and why.

    The failure is the *window's* line rather than the mesh's — a mesh that
    refused to admit an agent has no way to say so to that agent's window, and a
    window that waited for one would report a successful join as a silence.

    The reason is the transport's own sentence and is quoted rather than
    restated, so it stays in the language the library wrote it in.
    """
    return f"{GONE} {t('not_on_the_mesh', reason=reason)}"


def online(name: str) -> str:
    """`⚡ jill online` — a peer, or a channel's link, is up.

    `name` is whatever arrived: a peer's agent name (`slife2.plugins.a2a.mesh`) or the
    channel whose link came up (`slife2.plugins.wechat.server`, which says `wechat`).  One
    sentence for both, because to the person reading a transcript they are the
    same news — something this conversation can now reach.
    """
    return f"{ARRIVED} {t('online', name=name)}"


def offline(name: str) -> str:
    """`✗ jill offline` — and nothing reaches this conversation any more."""
    return f"{GONE} {t('offline', name=name)}"
