"""slife2-mcp-tools — the `tools:` section, and the servers it names.

Other people's MCP servers, each one an entry the operator wrote down: a command
to start or a URL to reach, with whatever credential it needs.  This is the
process that connects to them now, and the reason it is a process rather than a
paragraph of the hub is the same one `skills-server` and `cli-server` have — a
config section belongs to the thing that owns it, and the hub's job is the tool
*set*, not twenty links.

**What it serves is `list_sources` and `call_source`**, and nothing else: the
model never sees this server.  Each entry is declared as a source, the hub merges
its rows and enforces the naming rule, and a call comes back here to be run.  The
whole arrangement is `slife2.toolfamily`'s, which is also `restapi-tools`'s — the
two differ in which section they read and nothing else, which is the honest
consequence of REST being an MCP server behind a proxy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastmcp import FastMCP

from slife2.config import Config, ToolServerSettings, find_config_path, load
from slife2.gateway import ClientFactory
from slife2.mcp_server import configure_logging, parse_serve_args, serve
from slife2.toolfamily import build_family_server

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-mcp-tools"

#: This server's key in the config's `servers:` table, and the name the launcher
#: starts it under.  It is `mcp-tools` rather than `tools` because the catalogue
#: source of each entry is that entry's own name: a plugin cannot hold a source
#: under its own name, or its verdict would be written across rows that have no
#: connection behind them.
CONFIG_KEY = "mcp-tools"

#: The catalogue category its sources are filed under — the db's word for
#: "somebody else's MCP server", and the one thing that decides a row is
#: callable and the model may be given it.
CATEGORY = "mcp"

INSTRUCTIONS = (
    "The MCP servers the config's `tools:` section names. This server is called "
    "by the toolhub, not by a model: it holds the connections, and the tools "
    "themselves reach the model through the hub."
)


def build_server(
    config: Config,
    *,
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
) -> FastMCP:
    """Build the server over the `tools:` section, in file order."""
    return build_family_server(
        config,
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        category=CATEGORY,
        entries=config.tools.values(),
        transports=transports,
        client_factory=client_factory,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config_path = find_config_path()
    config = load()

    address = config.server(CONFIG_KEY)
    logger.info(
        "serving %s on http://%s:%d%s",
        SERVER_NAME,
        args.host or address.host,
        args.port or address.port,
        address.path,
    )
    serve(
        build_server(config),
        address,
        args,
        name=SERVER_NAME,
        config_path=config_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
