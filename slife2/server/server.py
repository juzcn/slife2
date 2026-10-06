"""slife2-agent — the agent loop, as a stateless MCP server.

The middle of the chain.  It is a server to the TUI and a client to an LLM
server, and it holds nothing else: no provider SDK is importable here, no API key
is readable here, and the only thing it knows about the model behind it is a URL
and a model name from the config.

**This server keeps no state.**  It has no conversation store, no session map,
and no lock.  A turn is a function: history in, history out.  The caller sends
what was said and gets back what to remember.

That is a deliberate reading of how MCP works now — `Context.session_id` is not
a usable identity (measured: it is a fresh uuid per request on both the
in-memory and the HTTP transport, because the client never sends
`mcp-session-id`), and building conversation memory on transport sessions means
the memory is only as stable as a header.  Making the caller own the history
removes the dependency entirely, and takes three things with it:

* a store that could grow without bound,
* a per-conversation lock, because two turns can no longer race over one list,
* the repair-on-cancellation logic, which existed only to stop an interrupted
  turn leaving a corrupt shared history.  There is no shared history to corrupt:
  a cancelled turn simply returns nothing, and the caller's history is whatever
  it already had.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import Context, FastMCP

from slife2.config import DEFAULT_AGENT, Config, ServerSettings, load
from slife2.events import TurnEvent, encode
from slife2.llm.base import LLMBackend
from slife2.llm.client import close_backend, open_backend
from slife2.llm.server_common import configure_logging, parse_serve_args, serve
from slife2.loop import AgentLoop
from slife2.messages import Message
from slife2.tools import ToolRegistry, builtin_tools

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-agent"

INSTRUCTIONS = (
    "A conversational agent. Call `run_turn` with the conversation so far and "
    "a new prompt; assistant output arrives as progress notifications on the "
    "same request, and the result carries the final answer plus the messages "
    "to append to the conversation. This server keeps no state: the caller "
    "owns the history."
)


class ProgressObserver:
    """Reports turn events as MCP progress notifications.

    `progress` is a monotonic counter and `total` stays None: how many deltas a
    turn will produce is genuinely unknown, and inventing a total would make
    clients render a percentage that means nothing.

    This class is also the seam for coalescing.  A fast local model can produce
    hundreds of notifications a second, and if that ever matters the fix is a
    small buffer here — one place, not threaded through the loop.
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._count = 0

    async def on_event(self, event: TurnEvent) -> None:
        self._count += 1
        # A no-op when the caller sent no progress token.  That is intended and
        # it is silent: `report_progress` checks for the token itself, so a
        # client that does not want a stream simply does not get one, and still
        # gets a correct result at the end.
        await self._ctx.report_progress(self._count, None, encode(event))


def build_server(config: Config, *, backend: LLMBackend | None = None) -> FastMCP:
    """Build the agent MCP server.

    `backend` is injectable so the whole server can be exercised over the
    in-memory transport with no LLM server and no network.  When it is not
    given, the real one is built from the config and connected in the server's
    lifespan — **once for the process, not once per turn**, because reconnecting
    per turn would pay a handshake for every step of every turn.
    """
    loop_holder: dict[str, AgentLoop] = {}

    def make_loop(active: LLMBackend) -> AgentLoop:
        return AgentLoop(
            active,
            ToolRegistry(builtin_tools()),
            max_steps=config.agent.max_steps,
        )

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        if backend is not None:
            loop_holder["loop"] = make_loop(backend)
            yield {}
            return

        provider_name, provider, model = config.resolve()
        logger.info(
            "provider %s at %s, model %s",
            provider_name,
            provider.server.url,
            model.model,
        )
        client, mcp_backend = await open_backend(
            provider.server.url, model.model, name=f"{provider_name}/{model.model}"
        )
        try:
            loop_holder["loop"] = make_loop(mcp_backend)
            yield {}
        finally:
            await close_backend(client)

    mcp: FastMCP = FastMCP(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        # Provider failures should reach the caller intact: "model not found" is
        # actionable and FastMCP's default is to replace it with a generic
        # message.  This listens on loopback and serves its own operator.
        mask_error_details=False,
    )

    @mcp.tool
    async def run_turn(
        messages: list[dict[str, Any]],
        prompt: str,
        ctx: Context,
        agent: str = DEFAULT_AGENT,
    ) -> dict[str, Any]:
        """Run one agent turn.

        Assistant output streams back as `notifications/progress` on this
        request, one notification per chunk.  The returned `text` is the final
        answer and is authoritative — a client that ignores the progress stream
        still gets the whole thing.

        The conversation lives with the caller.  Send back whatever the previous
        result's `new_messages` contained, in order; the server remembers
        nothing between calls.

        Args:
            messages: The conversation so far, as previously returned.  Treat
                these as opaque — pass back what you were given.
            prompt: What the user just said.
            agent: Who is asking.  This server treats it as opaque — it is
                recorded in the log and is the designated place for per-agent
                behaviour if any ever appears, because isolation between agents
                belongs inside an MCP server rather than in the process layout.

        Returns:
            `text` (the final answer), `new_messages` (append these to the
            conversation you sent), `usage`, `steps` and `stop_reason`.
        """
        loop = loop_holder.get("loop")
        if loop is None:  # pragma: no cover - the lifespan always sets this
            raise RuntimeError("the agent server is not initialised")

        logger.debug("turn from agent %s", agent)
        working = [Message.from_wire(m) for m in messages]

        # The system prompt comes from this server's config rather than the
        # caller's history, so it is applied afresh each turn and can be changed
        # by editing the config.  Skipped when the caller already supplied one,
        # so this cannot produce two.
        if config.agent.system_prompt and not (working and working[0].role == "system"):
            working.insert(
                0, Message(role="system", content=config.agent.system_prompt)
            )

        # Everything from here on is what the caller has to remember.
        offset = len(working)
        result = await loop.run_turn(working, prompt, ProgressObserver(ctx))

        return {
            "text": result.text,
            "new_messages": [m.to_wire() for m in working[offset:]],
            "usage": result.usage.to_wire(),
            "steps": result.steps,
            "stop_reason": result.stop_reason,
        }

    return mcp


def resolve_settings(config: Config) -> ServerSettings:
    """Where this server listens, per the config."""
    return config.agent.server


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config = load(args.config)
    settings = config.agent.server

    provider_name, provider, model = config.resolve()
    logger.info(
        "serving %s on http://%s:%d%s (model: %s/%s at %s)",
        SERVER_NAME,
        args.host or settings.host,
        args.port or settings.port,
        settings.path,
        provider_name,
        model.model,
        provider.server.url,
    )
    serve(build_server(config), settings, args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
