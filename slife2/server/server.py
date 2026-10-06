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

import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

from slife2.config import (
    DEFAULT_AGENT,
    Config,
    ServerSettings,
    find_config_path,
    load,
)
from slife2.events import TurnEvent, encode
from slife2.llm.base import LLMBackend
from slife2.llm.client import MCPBackend, close_backend, open_backend
from slife2.llm.server_common import configure_logging, parse_serve_args, serve
from slife2.loop import AgentLoop
from slife2.messages import Message
from slife2.prompt import render as render_system_prompt
from slife2.runtime import tcp_listening
from slife2.tools import ToolRegistry, builtin_tools

logger = logging.getLogger(__name__)

SERVER_NAME = "slife2-agent"

#: How long to wait on the memory server.  Short: remembering is not worth
#: holding an answer for, and the write is best-effort anyway.
MEMORY_TIMEOUT_SECONDS = 10.0

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


def build_server(
    config: Config,
    *,
    backend: LLMBackend | None = None,
    memory_client: Client | None = None,
) -> FastMCP:
    """Build the agent MCP server.

    `backend` and `memory_client` are injectable so the whole server — model
    call, tools, memory write — can be exercised over the in-memory transport
    with no network at all.

    Otherwise a connection is opened **per model server, on first use, and kept
    for the process**.  Not per turn — that would pay a handshake for every step
    of every turn — and not once at startup either, because which model is
    wanted is a property of the *request*: the caller names one, and this server
    serves every caller.  A model nobody asks for is a connection nobody opens.
    """
    model_backends: dict[str, LLMBackend] = {}
    clients: dict[str, Client] = {}
    #: The memory client once we have one — injected, or opened on first use.
    memory_conn: Client | None = memory_client
    #: Whether *we* opened it, and so whether we should close it.  An injected
    #: client belongs to whoever made it.
    memory_owned = memory_client is None
    #: Set once the memory server has been found absent, so the attempt is not
    #: repeated.  See `memory`.
    memory_off = False

    async def memory() -> Client | None:
        """The client for the memory server, or None once we know there isn't one.

        **A server that is not there is remembered as not being there.**  The
        write is best-effort, so a missing memory server costs nothing but the
        attempt — and the attempt is not free: it is a connection that has to
        time out, paid on every turn, for a component whose whole contribution
        is a record nobody is waiting for.  Trying once and giving up turns an
        unbounded tax into a single one.

        The cost is that a memory server started *later* is not picked up until
        this process restarts.  That is the trade, and it is the right way
        round: `slife2` starts its components together, so the case is a
        deliberate `slife2 down` and not something to wait for.
        """
        nonlocal memory_conn, memory_off
        if memory_off:
            return None
        if memory_conn is None:
            address = config.server("memory")
            # Ask the port before asking the protocol.  A refused TCP connect
            # comes back at once, where building an MCP client against nothing
            # spends a couple of seconds in the transport's own retries — and
            # that cost lands on the first answer of the session, which is the
            # one somebody is watching for.
            if not tcp_listening(address.host, address.port):
                memory_off = True
                logger.warning(
                    "no memory server on %s:%d; turns will not be recorded",
                    address.host,
                    address.port,
                )
                return None

            client: Client = Client(address.url, timeout=MEMORY_TIMEOUT_SECONDS)
            try:
                await client.__aenter__()
            except Exception:  # noqa: BLE001 - absent is an expected state
                memory_off = True
                logger.warning(
                    "no memory server at %s; turns will not be recorded",
                    address.url,
                )
                return None
            memory_conn = client
        return memory_conn

    async def remember_turn(
        agent: str, prompt: str, model: str, result, new_messages: list[dict]
    ) -> None:
        """Persist a turn, and never let that decision cost the turn.

        Memory is an enhancement, not part of correctness: a store that is down,
        a disk that is full, a name that cannot be a filename — none of them is
        a reason for a conversation that just succeeded to be reported as
        failed.  So every failure is logged and swallowed, and the caller gets
        its answer either way.

        A `ToolError` is *not* a reason to stop trying.  It means the memory
        server answered and refused this one request — an agent name that cannot
        be a filename, say — and that is one caller's problem rather than
        everyone's.  Anything else is the transport, and the transport going
        away is what `memory` remembers.
        """
        nonlocal memory_off
        client = await memory()
        if client is None:
            return

        try:
            await client.call_tool(
                "remember",
                {
                    "agent": agent,
                    "prompt": prompt,
                    "messages": new_messages,
                    "model": model,
                    "usage": result.usage.to_wire(),
                    "steps": result.steps,
                },
            )
        except ToolError as exc:
            logger.warning("memory refused the turn for %s: %s", agent, exc)
        except Exception:  # noqa: BLE001 - see the docstring
            memory_off = True
            logger.warning(
                "memory stopped answering; turns will no longer be recorded",
                exc_info=True,
            )

    def make_loop(active: LLMBackend) -> AgentLoop:
        return AgentLoop(
            active,
            ToolRegistry(builtin_tools()),
            max_steps=config.agent.max_steps,
        )

    async def loop_for(reference: str) -> AgentLoop:
        """The loop that talks to whatever `provider/model` names."""
        if backend is not None:
            return make_loop(backend)

        name, provider, model = config.resolve(reference)
        if name not in model_backends:
            url = config.server(provider.api).url
            if url not in clients:
                # One client per *server*, not per provider: a server speaks one
                # wire format for every provider that uses it.
                client, _ = await open_backend(
                    url, model.model, provider=name, name=f"{name}/{model.model}"
                )
                clients[url] = client
            client = clients[url]
            model_backends[name] = MCPBackend(
                client, model.model, provider=name, name=f"{name}/{model.model}"
            )
            logger.info("model %s via %s", reference, config.server(provider.api).url)
        return make_loop(model_backends[name])

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        try:
            yield {}
        finally:
            for client in clients.values():
                await close_backend(client)
            clients.clear()
            model_backends.clear()
            if memory_owned and memory_conn is not None:
                with contextlib.suppress(Exception):
                    await memory_conn.__aexit__(None, None, None)

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
        model: str = "",
        images: list[str] | None = None,
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
            agent: Who is asking.  This server treats it as opaque beyond the
                system prompt it renders — it is the designated place for
                per-agent behaviour, because isolation between agents belongs
                inside an MCP server rather than in the process layout.
            model: Which model to use, as `provider/model`.  Left out, the
                config's `default` is used.  Per request rather than per server
                because one agent server serves every caller, and two instances
                may well want different models.
            images: Images to send with the prompt, each a `data:` URL.  Refused
                unless the model's config lists `image` under `input` — silently
                dropping an attachment somebody made is worse than saying the
                model cannot read it.

        Returns:
            `text` (the final answer), `new_messages` (append these to the
            conversation you sent), `usage`, `steps` and `stop_reason`.
        """
        loop = await loop_for(model)
        logger.debug("turn from agent %s on %s", agent, model or "the default")
        working = [Message.from_wire(m) for m in messages]
        user = _with_images(prompt, images or [], config, model)

        # The system prompt comes from this server's config rather than the
        # caller's history, so it is applied afresh each turn and can be changed
        # by editing the config.  Skipped when the caller already supplied one,
        # so this cannot produce two.
        #
        # Rendered here rather than at startup because the agent name arrives
        # *with the request*: this server is shared, so two instances are two
        # names asking one process, and a prompt rendered once would give the
        # first caller's name to everybody.
        if not (working and working[0].role == "system"):
            # Imported as a function rather than as the module: the tool's
            # own parameter is called `prompt`, and `prompt.render(...)` would
            # be asking a string to render itself.
            system = render_system_prompt(config.agent.system_prompt, agent_name=agent)
            if system:
                working.insert(0, Message(role="system", content=system))

        # Everything from here on is what the caller has to remember.
        offset = len(working)
        result = await loop.run_turn(working, user, ProgressObserver(ctx))
        new_messages = [m.to_wire() for m in working[offset:]]

        await remember_turn(agent, prompt, model, result, new_messages)

        return {
            "text": result.text,
            "new_messages": new_messages,
            "usage": result.usage.to_wire(),
            "steps": result.steps,
            "stop_reason": result.stop_reason,
        }

    return mcp


def _with_images(
    prompt: str, images: list[str], config: Config, model: str
) -> str | list[dict[str, Any]]:
    """The user's message: text, or text and images.

    The model has to be *able* to read them.  A config that does not list
    `image` under a model's `input` is the config saying so, and the alternative
    to refusing is worse than it looks: the images would be dropped somewhere
    along the way and the user would be left wondering why the model ignored
    what they attached.
    """
    if not images:
        return prompt

    settings = config.resolve(model)[2]
    if not settings.accepts_images:
        raise ValueError(
            f"{settings.model} cannot read images "
            f"(its config lists input: {', '.join(settings.input)})"
        )

    parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    parts += [{"type": "image_url", "image_url": {"url": url}} for url in images]
    return parts


def resolve_settings(config: Config) -> ServerSettings:
    """Where this server listens, per the config."""
    return config.agent.server


def main(argv: list[str] | None = None) -> int:
    args = parse_serve_args(argv, SERVER_NAME)
    configure_logging()
    config_path = find_config_path()
    config = load()
    settings = config.agent.server

    logger.info(
        "serving %s on http://%s:%d%s (default model: %s)",
        SERVER_NAME,
        args.host or settings.host,
        args.port or settings.port,
        settings.path,
        config.default,
    )
    serve(
        build_server(config),
        settings,
        args,
        name=SERVER_NAME,
        config_path=config_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
