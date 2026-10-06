"""slife2-memory — turns, persisted, one database per agent.

A component with one job: keep what was said.  It does not summarise, does not
decide what mattered, and does not put anything back into a conversation — the
caller does that, if it ever does.  Everything stored is the message list as it
arrived, so a question this component cannot answer today can be asked of the
same rows later without a migration.

**Agents are isolated by file.**  `agent="jack"` reads and writes
`jack.turn.db`; there is no query that can reach another agent's turns, because
there is no other agent's turns in the file.  One process serves every agent —
the same arrangement as the model backends, where one process speaks one wire
format for every provider — and the `agent` argument picks the file.

It is shared infrastructure like the rest: started on demand, reused if already
running, never per-agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastmcp import FastMCP

from slife2.config import Config, find_config_path, load
from slife2.llm.server_common import configure_logging, parse_serve_args, serve
from slife2.memory import store_for
from slife2.paths import turns_dir

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-memory"

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "memory"

INSTRUCTIONS = (
    "Persisted turns, one database per agent. Call `remember` after a turn and "
    "`recent` to read back what was said. The store keeps no opinion about what "
    "matters: it writes what it is given and returns it in order."
)


def build_server(config: Config) -> FastMCP:
    """Build the memory MCP server."""

    async def _on_thread(function, *args, **kwargs):
        """Run a blocking store call off the event loop.

        SQLite is synchronous, and a tool that blocks the loop blocks every
        other call this server is handling.  A connection per call is what makes
        the thread hop safe — a connection is not shareable across threads, so
        there is none to share.
        """
        return await asyncio.to_thread(function, *args, **kwargs)

    mcp: FastMCP = FastMCP(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        mask_error_details=False,
    )

    @mcp.tool
    async def remember(
        agent: str,
        prompt: str,
        messages: list[dict[str, Any]],
        model: str = "",
        usage: dict[str, Any] | None = None,
        steps: int = 0,
    ) -> dict[str, Any]:
        """Persist one turn.

        A turn is one exchange: the user's prompt and everything the loop did
        about it — assistant messages, tool calls and their results — in the
        order they happened.  Store them as they are; this is a record, not an
        interpretation.

        Args:
            agent: Whose memory.  It names the database file, so agents are
                isolated from each other by construction.
            prompt: What the user said.
            messages: The turn's messages, as returned by the agent loop.
            model: Which model answered, as `provider/model`.
            usage: Token counts, if known.
            steps: How many model calls the turn took.

        Returns:
            `id` of the stored turn and the file it went into.
        """
        store = store_for(agent)
        turn_id = await _on_thread(
            store.remember,
            prompt=prompt,
            messages=messages,
            model=model,
            usage=usage,
            steps=steps,
        )
        logger.info("stored turn %s for %s", turn_id, agent)
        return {"id": turn_id, "database": str(store.path)}

    @mcp.tool
    async def recent(agent: str, limit: int = 10) -> list[dict[str, Any]]:
        """The most recent turns, newest first.

        Retrieval is by time, which is the honest thing for a component that
        stores without judging.  Relevance is a question for whoever is reading,
        and adding an index is a change to this file rather than a change to
        what was kept.

        Args:
            agent: Whose memory.
            limit: How many turns at most.
        """
        store = store_for(agent)
        records = await _on_thread(store.recent, limit)
        return [record.to_wire() for record in records]

    return mcp


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config_path = find_config_path()
    config = load()

    address = config.server(CONFIG_KEY)
    logger.info(
        "serving %s on http://%s:%d%s (databases in %s)",
        SERVER_NAME,
        args.host or address.host,
        args.port or address.port,
        address.path,
        turns_dir(),
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
