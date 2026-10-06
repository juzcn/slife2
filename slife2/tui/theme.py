"""The palette, defined once.

This is slife v1's TUI design ported: a dark, low-chrome look with warm amber
accents, GitHub-dark values.  The one thing changed in the porting is that the
colours live here and nowhere else.

slife v1 writes each hex value twice — once in `slife.tcss` and once in the
Python that builds styled text — so the two can drift, and one of them is not
checked by anything.  Textual lets an app publish custom CSS variables
(:meth:`App.get_css_variables`), so the stylesheet reads `$slife-bg` and the
widgets read :data:`PALETTE`, and a colour can only be wrong in one place.

The names describe the *role*, not the hue.  `amber` is the accent today; if it
ever becomes another colour, `slife-amber` in the CSS is the only rename.
"""

from __future__ import annotations

#: GitHub-dark, with warm amber accents.  Keys are CSS variable names without
#: the `slife-` prefix.
PALETTE: dict[str, str] = {
    # Surfaces, darkest first.
    "bg": "#0d1117",
    "panel": "#161b22",
    "panel-focus": "#1c2128",
    # Lines.
    "border": "#30363d",
    "border-dim": "#484f58",
    # Text, brightest first.  Four steps, because the transcript uses all four
    # to mean different things: what you said, what the model said, what the
    # system did, and what is merely there.
    "text": "#e6edf3",
    "text-secondary": "#c9d1d9",
    "muted": "#8b949e",
    "dim": "#6e7681",
    "dimmest": "#484f58",
    # Accents.
    "amber": "#d29922",
    "amber-bold": "#d97706",
    "amber-focus": "#f0c040",
    "green": "#3fb950",
    "red": "#f85149",
    "blue": "#58a6ff",
}


def css(name: str) -> str:
    """The value of a palette entry, for building markup in Python."""
    return PALETTE[name]


def css_variables() -> dict[str, str]:
    """The palette as Textual CSS variables, for the stylesheet."""
    return {f"slife-{name}": value for name, value in PALETTE.items()}


#: Glyphs, kept with the palette because they are the same kind of thing: the
#: visual vocabulary, spelled once so a widget and its tests cannot disagree.
#:
#: All of these are outside ASCII, and the house rule is that anything reaching
#: a *console* must be ASCII.  These never do — Textual owns the terminal and
#: writes UTF-8 — but it is why the server logs and the CLI stay plain.
GLYPHS: dict[str, str] = {
    "collapsed": "▸",  # right-pointing small triangle
    "expanded": "▾",  # down-pointing small triangle
    "running": "◌",  # dotted circle
    "done": "●",  # filled circle
    "error": "●",
    "warning": "⚠",
    "ok": "✓",
    "failed": "✗",
    "up": "↑",
    "separator": "│",
    "ellipsis": "…",
}
