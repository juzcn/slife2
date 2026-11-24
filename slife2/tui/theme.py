"""The palette, defined once.

The TUI design: a dark, low-chrome look with warm amber accents, GitHub-dark
values.  The colours live here and nowhere else.

The alternative is to write each hex value twice — once in `app.tcss` and once
in the Python that builds styled text — so the two can drift, and one of them is
not checked by anything.  Textual lets an app publish custom CSS variables
(:meth:`App.get_css_variables`), so the stylesheet reads `$slife-bg` and the
widgets read :data:`PALETTE`, and a colour can only be wrong in one place.

The names describe the *role*, not the hue.  `amber` is the accent today; if it
ever becomes another colour, `slife-amber` in the CSS is the only rename.
"""

from __future__ import annotations

#: The glyphs moved to the shared vocabulary once two *daemons* began composing
#: a line the window draws: a plugin must not import the terminal package for a
#: mark (`slife2.libs.harness.glyphs`).  Re-exported here — the explicit alias is
#: what says so to `ruff` and `pyright` — because every widget reads its visual
#: vocabulary, colours and marks, from this one module.
from slife2.libs.harness.glyphs import GLYPHS as GLYPHS

#: GitHub-dark, with warm amber accents.  Keys are CSS variable names without
#: the `slife-` prefix.
PALETTE: dict[str, str] = {
    # Surfaces, darkest first.
    "bg": "#0d1117",
    "panel": "#161b22",
    # Lines.
    "border": "#30363d",
    "border-dim": "#484f58",
    # Text, brightest first.  Five steps, because the transcript ranks what it
    # prints: what you said, what a tool returned, what the system did, what is
    # merely there, and what is only a separator.
    "text": "#e6edf3",
    "text-secondary": "#c9d1d9",
    "muted": "#8b949e",
    "dim": "#6e7681",
    "dimmest": "#484f58",
    # Accents.
    "amber": "#d29922",
    "amber-bold": "#d97706",
    "green": "#3fb950",
    "red": "#f85149",
}


def css_variables() -> dict[str, str]:
    """The palette as Textual CSS variables, for the stylesheet."""
    return {f"slife-{name}": value for name, value in PALETTE.items()}
