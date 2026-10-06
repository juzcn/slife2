"""slife2 — a terminal-based AI agent, designed from a clean slate.

This is the v2 scaffold.  The package is deliberately near-empty: it exists so
the uv workspace, the build backend and the console script are wired end to end
before any agent code lands.  The design comes next, into a project that is
already sound.
"""

__version__ = "0.1.0"


def main() -> int:
    """Entry point for the `slife2` console script.

    ASCII only: this is the first thing a user sees, and a Windows console
    whose codepage is not UTF-8 renders anything else as mojibake.
    """
    print(f"slife2 {__version__} - scaffold only, no agent loop yet")
    return 0
