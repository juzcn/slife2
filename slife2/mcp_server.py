"""The MCP layer every plugin shares: what it takes to be one of our servers.

**A plugin is an MCP server over Streamable HTTP — an implementation of the
protocol, not a protocol of its own.**  Nothing here adds a method, a frame or a
transport: a plugin answers the same JSON-RPC as any MCP server does, and the
`arxiv` entry under `mcp:` speaks the same wire.  Two things separate the
words.  A *plugin* is started by slife2 and shared by every instance, which is
what makes it required — the hub refuses to list anything when one is missing
(DESIGN.md §8) — while an entry under `mcp:` is somebody else's process that
may be slow, paid or down.  And a plugin honours the contract below, which is
the part that is ours: what follows is not the protocol, it is what this project
puts on top of it.

Every one of our servers is an MCP server — the agent loop, the hub, the two
families that declare rows, the two that hold somebody else's servers, and one
process per wire protocol — and they agree about more than they disagree about.  What they agree on lives here rather than in whichever server
was written first: how a server is started, how it records that it is here, and
the two options that are pinned because flipping either is *silent*.

It also holds the client half of the same question — :func:`identifies` and
:func:`open_server` — because "is this server the one I think it is", "is it
there at all", and "what does it mean to be one of our servers" are the same
piece of protocol knowledge, and splitting them is how the halves drift.

Nothing here is LLM-specific.  That is the point of the module: the db
server and the agent server used to reach into `slife2.llm` for this, which put
the serving scaffold of a non-LLM plugin inside the LLM package.

## The contract: a server keys its state by client id

**Every server here keys whatever state it has by a client id, and no client
carries state on a server's behalf.**  The id is `(agent, subagent)` — which
agent is talking, and which of that agent's conversations, empty for the one a
person is watching and a name for a worker it is running — and it travels with
every call, at every hop, including the ones that key nothing on it.

Three things follow, and each is a property the whole system has rather than a
rule each server remembers:

* **State is created when a key is first used, and a key never goes stale.**
  There is nothing to open, no handle to carry and no lifetime for a caller to
  observe, so there is no "not found" for one to handle either.  An idle sweep
  may drop the state underneath; the next call simply starts it again, and the
  caller cannot tell.
* **Isolation is the key.**  Two agents cannot reach each other's state because
  their ids differ, not because a query remembered a `WHERE`.
* **A hop is never anonymous.**  Even a server with no state to key receives the
  id, so any log line or future accounting can say whose call it served.

This is the shape SEP-2567 asks for once sessions are gone — state addressed by
an explicit argument rather than by the connection — with one deliberate
difference: the id is a **name the caller already has**, not an opaque handle
the server minted.  An opaque handle has to be stored, and a stored handle is
one more thing that can be lost, expired, or wrong after a restart.  See
DESIGN.md §3 for the argument and for what this costs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
from fastmcp import Client, FastMCP
from fastmcp.client.transports.base import ClientTransport, TransportOptions
from fastmcp.exceptions import ToolError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

import slife2.timeouts as _T
from slife2 import __version__
from slife2.audience import request_client, unaddressed
from slife2.config import Config, ServerSettings, find_config_path, load
from slife2.paths import add_data_dir_argument, apply_data_dir
from slife2.runtime import ServerRecord, clear_record, tcp_listening, write_record

logger = logging.getLogger(__name__)

#: The client id every server keys its state by: which agent is talking, and
#: which of that agent's conversations.  See the module docstring for the
#: contract; this is only its shape.
ClientId = tuple[str, str]

#: The tool a plugin serves when it holds **sources** of its own — the ones it
#: connects to on the operator's behalf, and the ones that are not tools at all.
#: The plugin answers with the whole of what it holds and the hub merges it,
#: which is what keeps one writer of the tool table, one place where "a name is a
#: row's identity" is decided, and one process holding a connection to the db.
#:
#: **A source is a name, and everything about it that is not its rows**: which
#: category it is, whether the operator switched it off, whether it is answering
#: now, what it is for, and how it is reached.  Those are the facts the hub used
#: to read off a connection of its own, and they are what `servers()` reports —
#: so the plugin that holds the connection is the one that has to say them.
#:
#: **It is not a tool for the model**, and the hub enforces what follows from
#: that rather than trusting it: only a plugin slife2 starts may declare, and the
#: category `plugin` — *the servers slife2 starts* — is refused, so that this
#: channel cannot be used to offer the model a tool the audience mark never saw.
LIST_SOURCES = "list_sources"

#: How the hub runs a tool of a source it does not hold a connection to.
#:
#: A declared source's rows are merged by the hub and its calls are run by the
#: plugin that declared it — which is what keeps the hub the only thing that
#: knows the *set*, without making it the only thing that can reach a server.
#: The far end's own tool name travels with the call (`remote_name`), and the
#: caller's identity is forwarded on through, exactly as the hub forwards it.
CALL_SOURCE = "call_source"

#: The two source names the *documents* are filed under — a playbook's rows and
#: a command's.  A source is a name and not a process, so these are not the
#: plugins' own names (`skills-server` serves, `skills` is what its rows are
#: filed under), and they are contract rather than any one plugin's business
#: for the same reason `LIST_SOURCES` is: the hub merges under them, the two
#: declaring plugins write them, and the family plugins that hold *other*
#: people's servers refuse a name that would collide with one.  Three copies of
#: `"cli"` and a test to hold them together is what this replaces.
SKILLS_SOURCE = "skills"
CLI_SOURCE = "cli"


def describe(client: ClientId) -> str:
    """A client id as one readable token, for a log line.

    `jack` rather than `jack/` for an agent's own conversation, because that is
    what a person calls it — and the worker form keeps the slash, so the two are
    never confusable in a log.
    """
    agent, subagent = client
    return f"{agent}/{subagent}" if subagent else agent


def agent_reader(what: str, own: str, *, why: str = "") -> Callable[[Any], str]:
    """The `agent_of(ctx)` a plugin binds its own refusal words to.

    **A factory rather than a function, because every caller already had the
    call shape it wants.**  Three plugins wrote the same four lines — read the
    client id, refuse when it is absent, answer with it — and every one of them
    called the result `agent_of(ctx)` from a dozen tool bodies.  Handing them a
    helper that takes the words as arguments would have rewritten all of those
    calls to repeat two strings; this hands back the same one-argument function
    they had, so what each plugin declares is exactly the part that is its own:
    the two words its refusal varies by (`slife2.audience.UNADDRESSED`), named
    once, at the top of the plugin that says them.

    That is `slife2.toolfamily.FamilyWords`'s arrangement one level down: the
    sentence lives where the mechanism lives and the vocabulary lives with the
    plugin, so a wording change is one edit rather than five.

    A server that answers with a refusal *sentence* rather than an error — the
    cabinet, the login — calls `slife2.audience.unaddressed` directly.  The
    sentence is one place either way, which is the half that matters.
    """

    def agent_of(ctx: Any) -> str:
        """The agent this call is on behalf of, refused rather than guessed."""
        found = request_client(ctx)
        if found is None or not found[0]:
            raise ToolError(unaddressed(what, own, why=why))
        return found[0]

    return agent_of


def house_server(
    name: str,
    *,
    instructions: str = "",
    lifespan: Callable[[FastMCP], AbstractAsyncContextManager[dict[str, object]]]
    | None = None,
) -> FastMCP:
    """A FastMCP server with this project's conventions applied.

    One convention so far, and it is worth stating rather than defaulting:

    * ``mask_error_details=False`` — FastMCP's default replaces an exception's
      message with a generic one.  Every server here listens on loopback and
      serves its own operator, so the detail is worth more than the tidiness:
      "model not found" and "the API key did not resolve" are the two things a
      user actually has to act on, and both arrive as exceptions.

    ``stateless_http`` and ``json_response`` are *not* here: they are
    ``mcp.run(...)`` options rather than constructor options, and :func:`serve`
    is where they are pinned.

    `instructions` is the MCP field for global, always-read guidance.  Keep it
    short and do not restate what the tools' own descriptions already say —
    which, as the spec's guidance puts it, is why no instructions is better than
    poorly written ones.
    """
    return FastMCP(
        name,
        instructions=instructions or None,
        lifespan=lifespan,
        mask_error_details=False,
    )


async def identifies(client: Client, expected_name: str, *, fallback_tool: str) -> bool:
    """Whether a connected client is talking to the server we meant.

    The first check reads the identity the server advertised during the
    handshake.  **It costs nothing**: the client performs `server/discover` as
    part of connecting (it is a MUST-implement method in the 2026-07-28
    revision), so a name is already in hand by the time anyone can ask.  Measured
    here against the installed client, `Client.server_info` is populated from the
    `DiscoverResult`, and the name is the one passed to `FastMCP(...)`.

    The fallback exists because the name is not guaranteed.  A client pinned to
    an exact modern protocol version gets a *synthesized* identity with an empty
    name, and a server from another implementation may report none at all — so
    when there is no name to compare, this falls back to asking whether the
    expected tool is present.  That check is weaker, and it is the one this
    system used exclusively until the handshake was found to answer the same
    question for free.

    Note what neither check is: a *liveness* probe in the sense the removed
    protocol-level `ping` was.  The spec names no replacement for `ping`, so
    this is a choice rather than an instruction — and it earns its place by
    catching a URL pointed at the wrong MCP server, which a bare connection test
    waves through and which would otherwise fail in the middle of a turn.
    """
    info = client.server_info
    if info is not None and info.name:
        return info.name == expected_name

    names = {tool.name for tool in await client.list_tools()}
    return fallback_tool in names


class _Ours(ClientTransport):
    """One connection to one of *our* servers, with no bound on a single event.

    **The 1 MiB ceiling on an SSE event is not ours to apply.**  It is the MCP
    SDK's default (`mcp.client.streamable_http.DEFAULT_MAX_SSE_EVENT_SIZE`), the
    *client* enforces it, and what it is for is keeping a peer from making this
    process buffer an unbounded message — which is exactly the right guard for a
    server nobody here started, and no guard at all on loopback to a daemon we
    did.  So `slife2.gateway`, which dials the entries under `mcp:` and
    `rest-api:`, keeps it; every hop that goes through `open_server` does not.
    That split is the point of this class rather than a global setting: the
    question is *who is on the other end*, and the two paths are the two answers.

    **And this system needs it off.**  Whole conversations travel in single
    messages here by design — `rebuild` sends the context a turn runs on,
    `remember` sends a turn, `transcript` sends a conversation to a screen — so
    1 MiB is a ceiling on how long a conversation may be.  It was reached in a
    real session, as `send_message` failing with "Server-sent event exceeded the
    1048576 byte limit".

    `None` rather than a large number, because a number is the same wall further
    away and there is no size at which one of our own conversations is too big.
    What is given up is the *reporting*: an overrun is now the machine's memory
    instead of a named `SSEError`.  That is the trade, and it is only acceptable
    because the peer is a process this system started.

    **Why this is a transport of its own rather than an argument**: FastMCP
    4.0.11 does not pass `max_sse_event_size` to `streamable_http_client`
    (`fastmcp/client/transports/http.py`), and its own `StreamableHttpTransport`
    takes no such parameter — so `Client(url)` cannot reach the knob at all.
    Everything else here is what that transport does: the same http client from
    the same helper, the same timeout, and the caller's session class.
    """

    def __init__(self, url: str) -> None:
        self.url = url

    @asynccontextmanager
    async def connect_session(
        self,
        *,
        transport_options: TransportOptions | None = None,
        **session_kwargs: Any,
    ) -> AsyncGenerator[ClientSession]:
        options = transport_options or TransportOptions()
        # The request deadline arrives as a *session* argument rather than as a
        # transport one — `Client(url, timeout=…)` turns it into
        # `read_timeout_seconds` and carries it in `session_kwargs` — so it is
        # read from there and turned into the http client's own clocks, exactly
        # as FastMCP's transport does it.
        #
        # **Read and write are None, and that is the whole of this hop's own
        # opinion.**  v1's registry spelled it `read: None # DELEGATED — owned by
        # the loop's tool budget`, and the reason survives the port: a bound here
        # applies to *every* message that crosses this socket, so a number meant
        # for one layer becomes the deadline for whatever the far end is doing —
        # and a tool that takes twenty minutes (a render) is a tool doing its job,
        # not a link to give up on.  A request that stated a deadline keeps it
        # (`read_timeout_seconds`); one that did not has none.
        #
        # Connect is not a duration anybody waits *through*: it is the bound that
        # keeps a port that hangs instead of refusing from holding a turn open.
        read_timeout_seconds = session_kwargs.get("read_timeout_seconds")
        timeout = httpx2.Timeout(
            connect=_T.timeouts.ready.connect_ours,
            read=read_timeout_seconds,
            write=None,
            pool=None,
        )
        async with create_mcp_http_client(timeout=timeout) as http_client:
            async with streamable_http_client(
                self.url, http_client=http_client, max_sse_event_size=None
            ) as (read_stream, write_stream):
                async with options.session_class(
                    read_stream, write_stream, **session_kwargs
                ) as session:
                    yield session


async def open_server(
    url: str,
    *,
    name: str = "",
    fallback_tool: str = "",
    timeout: float | None = None,
) -> Client:
    """Connect to one of our servers, or raise naming what could not be reached.

    **A server here is either there, or the system has come apart.**  `slife2`
    starts every plugin together and refuses to start at all if one of them
    will not come up — before it draws anything, so the failure is two lines
    rather than a terminal that can never connect.  A peer that goes missing
    later is the same situation arriving late, and it takes the same answer:
    fail where it is used, rather than run on quietly with a piece gone.

    That convention lives here, in one function, because it had four
    implementations and one of them was its own opposite.  The LLM backend and
    the TUI each connected and probed and raised; the agent server's db
    client swallowed the failure and latched itself off, so that the same
    situation was fatal at startup and silent at runtime.

    `name` is the MCP name the server should be advertising.  Empty skips the
    check, which is what a caller that does not know it wants — a test, or a URL
    nobody configured.  :func:`identifies` explains why the handshake's own
    identity is asked first and the tool list second.

    The client is *not* closed on success; the caller owns its lifetime, and
    :func:`close_server` is the other half of this pair.
    """
    # Ask the port before asking the protocol.  A refused TCP connect comes back
    # at once, where building an MCP client against nothing spends a couple of
    # seconds inside the transport's own retries — measured here at 0.27s
    # against 2.29s.  It used to be worth this only for the db server, whose
    # absence was the one that could be discovered on the first turn of a
    # session; now that any missing peer fails the turn, it is worth it for all
    # of them, and it costs a quarter of a second only when something is
    # genuinely wrong.
    parts = urlsplit(url)
    if parts.hostname and parts.port and not tcp_listening(parts.hostname, parts.port):
        raise ConnectionError(
            f"{url}: nothing is listening on {parts.hostname}:{parts.port}"
        )

    client: Client = Client(_Ours(str(url)), timeout=timeout)
    try:
        await client.__aenter__()
        if name and not await identifies(client, name, fallback_tool=fallback_tool):
            names = sorted(tool.name for tool in await client.list_tools())
            raise ConnectionError(f"not {name} (tools: {names or 'none'})")
    except asyncio.CancelledError:
        # **A cancellation is a `BaseException`, so it walks straight past the
        # `except Exception` below.**  This function is awaited inside tasks that
        # a person cancels — Esc, `asyncio.wait_for`, a turn given up on — and a
        # cancellation during `__aenter__` would otherwise skip the close
        # entirely, leaving a half-entered client holding an open HTTP connection
        # or a spawned stdio child for the GC.  `slife2.gateway.Connection` gets
        # this right on its own connect path; this is the same branch.
        await close_server(client)
        raise
    except Exception as exc:
        # Closed on the way out because there is no client to hand back, and a
        # half-open one would hold a connection nobody owns.
        await close_server(client)
        raise ConnectionError(f"{url}: {exc}") from exc
    return client


async def close_server(client: Client) -> None:
    """Let go of a client nobody is keeping — the close that must not raise.

    Two callers, and one copy of the three lines they share: a caller that has
    finished with a client `open_server` handed back, and `open_server` itself
    for a client it could not hand back — a connection that was not the server we
    meant, or an enter a cancellation interrupted.  It used to be two copies, and
    the one that *lacked* the cancellation branch is how a half-entered client
    was leaked.

    Suppressed, because every one of those is a way out: a shutdown, an error
    path, a cancellation.  A transport that is already gone has nothing left to
    fail at, and a close that raises would replace whatever the caller was
    actually doing with a message about the shutdown.  `CancelledError` is a
    `BaseException` and so is deliberately not suppressed here — a cancel that
    arrives *during* the close must still win.
    """
    with contextlib.suppress(Exception):
        await client.__aexit__(None, None, None)


def tool_payload(result: Any) -> dict[str, Any]:
    """The mapping one of our own tool calls answered with.

    Reads the structured payload the SDK has already deserialized, and falls
    back to the text block — which is the same JSON, since a structured result
    is also sent as text.  A server reached over a transport that kept only the
    text is still readable, and one that answered with neither is a caller that
    gets an empty mapping and says so itself.

    Here rather than in a caller, because it is about reading a *tool result*
    and this is the module that owns what being one of our servers means.  Two
    plugins hop to a peer now — the toolhub and the db — and a second copy of
    this is how the two would come to disagree about the shape of an answer.
    """
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    if isinstance(data, str) and data.strip():
        try:
            decoded = json.loads(data)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def parse_serve_args(argv: list[str] | None, description: str) -> argparse.Namespace:
    """The flags every server here accepts.

    `--data-dir` comes from :mod:`slife2.paths`, which is also where the CLI's
    own parser gets it — the flag means the same thing in both, and two copies
    of its help text is how that stops being true.

    `allow_abbrev=False` for the CLI's reason, which is the same flag's rule
    twice: `--data-d` is not a spelling of anything, and a server started by a
    process manager should fail loudly on a typo rather than come up on the
    wrong directory.
    """
    parser = argparse.ArgumentParser(
        prog=description, description=description, allow_abbrev=False
    )
    add_data_dir_argument(parser)
    parser.add_argument("--host", default=None, help="override the listen address")
    parser.add_argument("--port", default=None, type=int, help="override the port")
    args = parser.parse_args(argv)
    apply_data_dir(args)
    return args


def serve(
    mcp: FastMCP,
    server: ServerSettings,
    args: argparse.Namespace,
    *,
    name: str,
    config_path: Any = None,
) -> None:
    """Run a server over streamable HTTP, recording that it is here.

    A server registers itself rather than being registered by whoever started
    it, because *it* is the only party that knows which config it read — and
    that is exactly what decides whether another instance may reuse it.  A
    server started by hand is indistinguishable from one started by a launcher
    once it does this, which is the point: `slife2` pointed at the same config
    reuses it either way, and an instance pointed at a *different* config sees a
    record that is not its own and refuses rather than talking to a server
    holding somebody else's key.

    Two options are pinned explicitly even where they match the default, because
    flipping either is *silent* and a future refactor could do it without
    noticing:

    * ``json_response=False`` — with it True the response body is buffered and
      returned as one JSON document.  Progress notifications cannot be
      interleaved into a buffered body, so every stream in this system would
      stop streaming while still returning correct results.
    * ``stateless_http=True`` — no request here depends on a session surviving
      between two of them.  The agent server does keep its loops, but it names
      them with an ordinary tool argument rather than a transport session, so it
      is stateless in the sense this flag means.  See `slife2.server.server`.
    """
    url = ServerSettings(
        host=args.host or server.host,
        port=args.port or server.port,
        path=server.path,
    ).url
    record = ServerRecord.now(
        name=name,
        url=url,
        pid=os.getpid(),
        config=str(config_path) if config_path else "",
        version=__version__,
    )
    write_record(record)
    try:
        mcp.run(
            transport="http",
            host=args.host or server.host,
            port=args.port or server.port,
            path=server.path,
            json_response=False,
            stateless_http=True,
        )
    finally:
        clear_record(url)


def configure_logging() -> None:
    """Log to stderr, never stdout.

    stdout is not free here: on a stdio transport it *is* the protocol channel,
    and even over HTTP the house rule is that model output must never be
    printed to a console whose codepage may not be UTF-8 — a `UnicodeEncodeError`
    mid-turn is a worse failure than a log line nobody reads.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def serve_plugin(
    argv: list[str] | None,
    *,
    server_name: str,
    config_key: str,
    build: Callable[[Config], FastMCP],
    logger: logging.Logger,
    note: Callable[[Config], str] | None = None,
) -> int:
    """The `main` every plugin that owns a config section shares.

    Same shape as `slife2.llm.server_common.serve_backend`, and its sibling: one
    process per plugin, so the only thing that differs between any two of them
    is which config key their address is filed under and which server they
    build.  Seven servers each spelled out the same eleven lines, which is the
    kind of duplication that drifts one branch at a time.

    `note` is the one thing a server wants to add to its start-up line — "turns
    in <dir>", "N plugin(s) to ask" — and it is a *callable* because the config
    it reports on is only loaded in here.  It returns the text **inside** the
    parentheses; the parentheses are this function's, so no caller can produce
    a half-formatted line.  `logger` is the caller's, so a line still lands
    under the name of the server that wrote it rather than this module's.
    """
    args = parse_serve_args(argv, server_name)
    configure_logging()
    config_path: Path | None = find_config_path()
    config = load()

    address = config.server(config_key)
    logger.info(
        "serving %s on http://%s:%d%s%s",
        server_name,
        args.host or address.host,
        args.port or address.port,
        address.path,
        f" ({note(config)})" if note is not None else "",
    )
    serve(
        build(config),
        address,
        args,
        name=server_name,
        config_path=config_path,
    )
    return 0
