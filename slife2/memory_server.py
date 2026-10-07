"""slife2-memory — turns, persisted, one database per client id.

A component with one job: keep what was said.  It does not summarise, does not
decide what mattered, does not put anything back into a conversation, and does
not decide *whether* a turn is worth keeping — the caller does that, and the
agent server is the caller that knows a worker's turns are not.  A turn is stored as it happened — with one
deliberate exception, an oversized tool result, which is kept as an announced
head-and-tail digest rather than in full (see `slife2.memory`) — so a question
this component cannot answer today can be asked of the same rows later without a
migration: search, embedding and summarising are each a table the schema has a
place for and nothing here builds yet.

**Client ids are isolated by file.**  `("jack", "")` reads and writes
`jack.turn.db`, and `("jack", "worker")` writes `jack@worker.turn.db`; there is no
query that can reach another id's turns, because there is no other id's turns in
the file.  One process serves every id — the same arrangement as the model
backends, where one process speaks one wire format for every provider — and the
`(agent, subagent)` pair picks the file.

It is shared infrastructure like the rest: started on demand, reused if already
running, never per-agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastmcp import FastMCP

from slife2.config import Config, find_config_path, load
from slife2.mcp_server import (
    configure_logging,
    describe,
    house_server,
    parse_serve_args,
    serve,
)
from slife2.memory import store_for
from slife2.paths import turns_dir

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-memory"

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "memory"

INSTRUCTIONS = (
    "Persisted turns, one database per client id. Call `remember` after a turn "
    "and `recent` to read back what was said, naming the same `(agent, subagent)` "
    "the turn was taken under. The store keeps no opinion about what matters: it "
    "writes what it is given and returns it in order."
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

    mcp: FastMCP = house_server(SERVER_NAME, instructions=INSTRUCTIONS)

    @mcp.tool
    async def remember(
        agent: str,
        messages: list[dict[str, Any]],
        subagent: str = "",
        token_count: int = 0,
        context_tokens: int = 0,
        who_helped: str = "",
        what_model: str = "",
        channel: str = "",
        created_at: str | None = None,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist one turn.

        A turn is one exchange: what the user said, and everything the agent did
        about it — assistant messages, tool calls and their results — in the
        order they happened.  Store it as it is; this is a record, not an
        interpretation.

        Args:
            agent: Whose memory.  With `subagent` it names the database file, so
                client ids are isolated from each other by construction.
            messages: The whole turn, as the agent loop returned it: the user's
                message first, then every assistant message, tool call and
                result.  Store it as it is.  What the user said is in there as
                the first entry, and an attached image's base64 payload rides
                along on it.
            subagent: Which of that agent's conversations; empty for its own.
            token_count: What the turn cost, summed over every model call in it.
            context_tokens: The last model call's prompt plus completion — how
                large the conversation had become, which is what the next
                request resends.  A different question from `token_count`: one
                is a bill, the other is what the context window has to hold.
            who_helped: The agent that answered.
            what_model: Which model answered, as `provider/model`.
            channel: Where the turn came in from — `human`, or a client id.
                Empty when the caller has nothing to say about it.
            created_at: When the user pressed enter.  Defaults to now, which is
                the same moment to within a hop for a caller on loopback.
            completed_at: When the assistant finished.  Defaults to now.

        Returns:
            `turn_id` of the stored turn, and the file it went into.
        """
        store = store_for(agent, subagent)
        turn_id = await _on_thread(
            store.save_turn,
            messages=messages,
            token_count=token_count,
            context_tokens=context_tokens,
            who_helped=who_helped,
            what_model=what_model,
            channel=channel,
            created_at=created_at,
            completed_at=completed_at,
        )
        logger.info("stored turn %s for %s", turn_id, describe((agent, subagent)))
        return {"turn_id": turn_id, "database": str(store.path)}

    @mcp.tool
    async def recent(
        agent: str, limit: int = 10, subagent: str = ""
    ) -> list[dict[str, Any]]:
        """The most recent turns, newest first.

        Retrieval is by time, which is the honest thing for a component that
        stores without judging.  Relevance is a question for whoever is reading,
        and adding an index is a change to this file rather than a change to
        what was kept.

        Args:
            agent: Whose memory.
            limit: How many turns at most.
            subagent: Which of that agent's conversations; empty for its own.
        """
        store = store_for(agent, subagent)
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
