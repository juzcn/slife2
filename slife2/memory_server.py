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

from fastmcp import Context, FastMCP

from slife2.audience import FOR_THE_MODEL, request_client
from slife2.config import Config, find_config_path, load
from slife2.mcp_server import (
    configure_logging,
    describe,
    house_server,
    parse_serve_args,
    serve,
)
from slife2.memory import PREVIEW_CHARS, store_for
from slife2.paths import turns_dir

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-memory"

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "memory"

INSTRUCTIONS = (
    "Persisted turns, one database per client id. Call `remember` after a turn, "
    "naming the `(agent, subagent)` the turn was taken under. The store keeps no "
    "opinion about what matters: it writes what it is given and returns it in "
    "order. Reading is by time — `turn_list` browses and pages, `turn_read` "
    "returns one turn whole — which is the honest thing for a component that "
    "stores without judging. Both of those are the model's, and neither names an "
    "agent: they answer about the conversation the call came from."
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
                the first entry, and so is an attached image — which is the one
                thing this does not keep: the bytes become a note saying they
                were there, and the file is still named in the prompt.
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

    def _caller(ctx: Context) -> tuple[str, str]:
        """Whose memory a model's call is about — read from the request.

        **There is no `agent` argument, and that is the whole point.**  A model
        that could name a database could read somebody else's memory, and the
        only thing standing between it and that would be a sentence in its own
        system prompt — which is an instruction, not a boundary.  The
        conversation is a fact the caller's side holds (`slife2.toolclient`
        attaches it to the call) and this side reads; the model neither sees it
        nor can write it.

        Raises:
            ValueError: If the call arrived without one.  Every real caller is
                the hub, which forwards what the agent gave it, so this is a
                caller that reached the memory server directly — a test, or a
                component that is not the agent — and it is better told than
                quietly served whatever it named.
        """
        found = request_client(ctx)
        if found is None:
            raise ValueError(
                "this tool reads one conversation's memory and the call did not "
                "say whose; it is called through the toolhub, which forwards the "
                "caller's identity (`slife2.audience`)"
            )
        return found

    @mcp.tool(meta=FOR_THE_MODEL)
    async def turn_list(
        ctx: Context,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Browse what was said before, newest first, one line per turn.

        The way back into a conversation you were not in, or were in long enough
        ago to have lost: when each turn was, what was asked, what was answered,
        and the id to read the whole of it with `turn_read`.  Both messages come
        back cut short — enough to tell whether this is the turn you wanted, not
        enough to be the turn itself.

        Your own history, and only your own: there is no argument naming whose
        memory to read, because a model that could name one could read somebody
        else's.

        Args:
            since: Lower bound on when the turn was written — an ISO date or
                datetime, or one of: today, yesterday, tomorrow, now,
                last|this week|month|quarter|year, or '<N> day(s)|week(s)|month(s)|year(s) ago'.
                Omit for no lower bound.
            until: Upper bound on the same grammar.  A date means the whole day.
            limit: How many turns this page may hold.
            offset: Skip this many turns — that is how you page back.

        Returns:
            `entries` — `turn_id`, `created_at`, `user_message`,
            `assistant_message` and `token_count` per turn — `total`, how many
            turns the window holds, so `offset + len(entries) < total` says
            whether there is more — and the `limit` and `offset` that produced
            this page.
        """
        store = store_for(*_caller(ctx))
        records, total = await _on_thread(
            store.turns, since=since, until=until, limit=limit, offset=offset
        )
        return {
            "entries": [record.to_listing(PREVIEW_CHARS) for record in records],
            "total": total,
            "limit": limit,
            "offset": max(0, offset),
        }

    @mcp.tool(meta=FOR_THE_MODEL)
    async def turn_read(ctx: Context, turn_id: int) -> dict[str, Any]:
        """One turn in full, by the id `turn_list` gave you.

        Everything that happened in it, in the order it happened: what the user
        said, every assistant message, the tool calls and what they answered.
        Your own history, like `turn_list` — an id only resolves inside it.

        Args:
            turn_id: The turn to read, as `turn_list` reported it.
        """
        agent, subagent = _caller(ctx)
        store = store_for(agent, subagent)
        record = await _on_thread(store.turn, turn_id)
        if record is None:
            raise ValueError(
                f"no turn {turn_id} in {describe((agent, subagent))}"
            )
        return record.to_wire()

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
