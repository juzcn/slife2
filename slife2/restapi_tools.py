"""slife2-restapi-tools — the `rest-api:` section, held like any other server.

A REST API is written down as a spec, a base URL and a key, and the config layer
expands it into the `uvx mcp-openapi-proxy` command that serves it — so what
arrives here is an ordinary stdio MCP server like every entry under `tools:`, and
this plugin holds it the same way.  That is the whole of the difference between
the two: the section, and the shape an entry is written in.

**Why it is a plugin of its own anyway.**  Because the sections are separate, and
an operator's mental model is the section: `rest-api:` entries are a shorter way
to say the same thing, not a second mechanism, and a family that owns its config
is a family whose next change has somewhere to land.  The machinery is shared —
see `slife2.toolfamily` — so this costs a module and a port, not a second
implementation.
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

SERVER_NAME = "slife2-restapi-tools"

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "restapi-tools"

#: The catalogue category its sources are filed under.  The db keeps `rest`
#: apart from `mcp` for the reason the sections are apart: it is the answer to
#: the first question anyone debugging asks, which is why a server they never
#: wrote a `command:` for is running `uvx`.
CATEGORY = "rest"

INSTRUCTIONS = (
    "The REST APIs the config's `rest-api:` section expands into MCP servers. "
    "This server is called by the toolhub, not by a model: it holds the "
    "connections, and the tools themselves reach the model through the hub."
)


def build_server(
    config: Config,
    *,
    transports: Mapping[str, Callable[[ToolServerSettings], Any]] | None = None,
    client_factory: ClientFactory | None = None,
) -> FastMCP:
    """Build the server over the `rest-api:` section, in file order."""
    return build_family_server(
        config,
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        category=CATEGORY,
        entries=config.rest_apis.values(),
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
