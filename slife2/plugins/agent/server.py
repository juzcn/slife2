"""slife2-agent — the agent loop, as an MCP server that owns its conversations.

The middle of the chain.  It is a server to the TUI and a client to an LLM
server, and it holds nothing else: no provider SDK is importable here, no API key
is readable here, and the only thing it knows about the model behind it is a URL
and a model name from the config.

**This server keeps the conversations**, and it addresses each one by
`(agent, subagent)` — the same pair every other server in this system keys its
own state by.  `subagent=""` is the agent's own conversation; anything else is
one of its subagents, an agent it made and owns (`subagent_create`, SDD §3.5).  A
caller submits a user message under that key and gets
that turn's answer back.  The management surface a client uses is
`send_message`, `reset`, `transcript` (what a window that has just opened asks
for), `subagents` (the list a conversation switcher offers), and `watch` (the
live read of a conversation the caller is not running).  `_check_new_input` is
not part of that surface: it is the name a message arriving mid-turn is
delivered under, and it is declared here because the pair the harness writes
into a conversation has to name a tool the request carries.

**A key is created when it is first used, and it never expires.**  A name
cannot go stale, so no caller needs a "your loop is gone" path.  The idle sweep
still runs, but it only reclaims memory; nothing a caller can observe depends
on it.

**The key travels with the call.**  Everything this server asks of a peer is
asked on behalf of the same `(agent, subagent)` — the write to the db and the
model call both carry it — so a hop is never anonymous.  What the model servers do
*not* do with it is keep the conversation: see SDD §5.5 for why the history
has to live here, on the side of the hop that does not speak a wire protocol.

**The tools come from the toolhub**, and are asked for again before every model
call — the list goes out with each request, so it is read with each request.
That is why this server holds no tool registry of its own and why `loop.py`
still knows nothing about MCP: it is handed a coroutine, and where the registry
came from is nobody else's business.  Our own plugins included: one owner for
the model's whole tool list is worth one hop.  See SDD §7.1.

The consequences of owning the history are paid for here rather than avoided,
because they are the price of the state:

* **A per-key lock**, so two turns cannot race over one list.  `send_message`
  takes it; a second caller waits, and that wait *is* the inbox.  Distinct keys
  hold distinct locks, so two agents' conversations run at the same time.
* **Cut-in**, which is what waiting *for* means.  A message that arrives while a
  turn runs is handed to that turn at its next iteration boundary — as a call to
  `_check_new_input` and a result carrying the message's own words — so the model
  addresses it inside the turn it arrived in rather than one turn later.  It is
  always on, and it is the one place this server writes a message into a
  conversation that its own caller did not send.
* **Interruption keeps the work.**  A turn an interruption cuts short leaves a
  well-formed exchange: the loop completes the tool batch it was in —
  the calls that returned keep their answers, the rest say they were interrupted
  — and `run_turn_into` keeps the list rather than truncating it, inside the
  lock, so a later "continue" resumes from what was done.  Its row is written
  and its id claimed on the next turn (`settle_record`), so an interrupted turn
  is as addressable as any other.
* **An image is kept, not merely noted.**  When a turn is recorded every image
  part is replaced in the row by an `[Image:{…}]` reference to a cabinet copy
  (`record`'s `keep_images`); the live conversation keeps the part.  A cabinet
  refusal is a hard failure, like a store refusal: a row that names an image
  the cabinet does not hold is a marker that can never be attached again.
* **Repair on a failure.**  A turn that died at the provider keeps its messages —
  a message somebody typed must not vanish because a provider answered 500 — but
  not its pictures: an image the provider refuses is refused by every turn after
  it, so `send_message` drops them and says so in the error the caller reads.
  Such a row is note-only by design; the cabinet save is for turns that ran.

**The conversation store can grow without bound**, which is the price of
owning the history.  See SDD §14.2 — trimming is not solved here.

**This module is the assembly, and the machinery is beside it.**  `core` owns
the loops table, the turn (`send_message`, `run_turn_into`) and the
watch/transcript/reset reads; `recording` writes a turn; `model_tools` edits
the config a conversation resolves from; `membership` makes and unmakes a
subagent.  SDD §5.1 says what each owns.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from fastmcp import Client, FastMCP

from slife2.libs.host.config import AGENT_SERVER_NAME, Config, load_cached
from slife2.libs.model.base import LLMBackend
from slife2.libs.plugin.protocol import house_server, serve_plugin
from slife2.libs.turn.runtime import TurnRuntime
from slife2.libs.turn.tools import ATTACH_IMAGE, CHECK_NEW_INPUT

from . import membership, model_tools
from .core import SUBAGENT_CREATE, SUBAGENT_REMOVE, AgentCore, ProgressObserver
from .membership import _remove_tree
from .state import Pending

logger = logging.getLogger(__name__)

#: The names a reader (and the suite) reaches for through this module, which is
#: the one the plugin row and the console script name.
__all__ = [
    "ATTACH_IMAGE",
    "CHECK_NEW_INPUT",
    "SUBAGENT_CREATE",
    "SUBAGENT_REMOVE",
    "Pending",
    "ProgressObserver",
    "_remove_tree",
    "build_server",
    "main",
]

SERVER_NAME = AGENT_SERVER_NAME

#: This server's key in the config's `servers:` table, and so the name the
#: launcher starts it under.  **The same word as the `agent:` section and not a
#: coincidence**: that section is this plugin's settings (`AgentSettings`) and
#: the endpoint in `servers:` is where they are served from, so `PLUGINS` files
#: one row under the one key both halves are named by.  Declared here like every
#: other plugin's, because `serve_plugin` takes it and a literal at the call site
#: is the only other way to say it.
CONFIG_KEY = "agent"

INSTRUCTIONS = (
    "A conversational agent. Call `send_message` with an agent name and what was "
    "said; assistant output arrives as progress notifications on that call, and "
    "the result carries the final answer. The conversation is kept here and is "
    "addressed by `(agent, subagent)` — send only what is new, and use `reset` to "
    "start one over."
)


def build_server(
    config: Config,
    *,
    backend: LLMBackend | None = None,
    context_client: Client | None = None,
    hub_client: Client | None = None,
    cabinet_client: Client | None = None,
    window_client: Client | None = None,
    mesh_client: Client | None = None,
    source: Callable[[], Config] | None = None,
) -> FastMCP:
    """Build the agent MCP server.

    **The middle is the turn runtime's** (`slife2.libs.turn.runtime`): the model
    clients, their per-server connections, and the context and hub
    connections all live there, opened on first use and kept for the process.
    What is this server's is the *tool list* policy: the hub's connection is
    long-lived, and its list is asked for on every turn — see `AgentCore.turn_tools`
    for why those are not the same choice.

    `backend`, `context_client` and `hub_client` are injectable so the whole
    server — model call, tools, the restore, the rebuild, the write — can be
    exercised over the in-memory transport with no network at all.

    `cabinet_client` is the seam for the one peer this server reaches *directly*
    rather than through the hub — the cabinet, whose `dump_save` carries no
    audience mark and therefore has no row for the hub to route
    (`AgentCore.compress_for` says why that is the arrangement).

    `window_client` is the seam for the one thing this server *hands* rather
    than asks: a background call's result, on its way back into the conversation
    it came from (`slife2.libs.harness.handover`, which is where the window-is-the-only-road
    policy is argued).  `slife2.plugins.agent_task.server` takes it for the same reason.

    `mesh_client` is the seam for the other peer reached *directly*, and for a
    removal rather than a tool call: `subagent_remove` tells the subagent network
    that a member is gone (`membership.leave_the_mesh`), because a place on that network
    outlives the directory it was held for.  Opened per call and closed with it —
    once per removed subagent, not once per turn.
    """
    runtime = TurnRuntime(
        config,
        backend=backend,
        context_client=context_client,
        hub_client=hub_client,
        source=source,
    )
    core = AgentCore(
        runtime,
        cabinet_client=cabinet_client,
        window_client=window_client,
        backend_name=backend.name if backend is not None else None,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        try:
            yield {}
        finally:
            await core.close()

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )
    core.register_tools(mcp)
    model_tools.register_tools(mcp, core)
    membership.register_tools(mcp, core, mesh_client=mesh_client)
    return mcp


def main(argv: list[str] | None = None) -> int:
    """Serve the agent loop.  `slife2.libs.host.launcher` starts this.

    **The shared `main`.**  What is this server's own is the `note` — the model
    a *new* conversation starts on is the one fact about this start that no
    other line carries, and it is the thing a person checking "did my `--model`
    take" is looking for.

    The address comes from `config.server("agent")`, which is `config.agent.server`
    (`AgentSettings.server`): the agent's settings are the `agent:` section and
    its endpoint is the `servers:` entry of the same name, and `PLUGINS` files
    both under the second.
    """
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        # `load_cached` rather than the config: a new conversation reads the
        # file, so a model config edit — by a model or by hand — is live.
        build=lambda config: build_server(config, source=load_cached),
        logger=logger,
        note=lambda config: f"default model: {config.default}",
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
