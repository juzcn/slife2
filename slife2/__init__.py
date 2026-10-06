"""slife2 — a terminal AI agent whose components are MCP servers.

The system is four processes:

* ``slife2``            the Textual TUI, an MCP client
* ``slife2-agent``      the agent loop, an MCP server and a client
* ``slife2-llm-openai`` an OpenAI-compatible model, as an MCP server
* ``slife2-llm-anthropic`` the Anthropic Messages API, as an MCP server

Two things fall out of that arrangement and are the reason for it: a provider's
API key exists only inside the LLM server process that needs it, and the agent
loop imports no provider SDK at all — switching providers is changing a URL.

This module is the console entry point, which starts the TUI.  The servers have
their own entry points, one per component, so a process manager can start any of
them without passing an argument.
"""

from __future__ import annotations

import argparse

from slife2.config import Config, ConfigError, load

__version__ = "0.1.0"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="slife2",
        description="Terminal AI agent. Run `slife2-agent`, `slife2-llm-openai` "
        "and `slife2-llm-anthropic` alongside it.",
    )
    parser.add_argument("--config", default=None, help="path to slife2.yaml")
    parser.add_argument(
        "--url",
        default=None,
        help="the agent server's MCP endpoint (default: from the config)",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="which configured provider the agent should use",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `slife2` console script: run the TUI."""
    args = _parse_args(argv)

    try:
        config: Config = load(args.config)
        provider = config.agent.provider(args.provider)
    except ConfigError as exc:
        # A config mistake is worth a one-line message rather than a traceback:
        # nothing has started yet, and the fix is in the file being named.
        print(f"slife2: {exc}")
        return 2

    # Imported here rather than at module scope so `slife2.config` and the
    # servers stay importable without pulling in Textual.
    from slife2.tui.app import SlifeApp

    app = SlifeApp(
        args.url or config.tui_url,
        model_label=f"{args.provider or config.agent.default}/{provider.model}",
    )
    app.run()
    return 0
