"""Who a call is for: the tool's audience, and the caller's own name.

Both travel out of band — beside the model rather than in it — and there are two
of them because a tool call has two parties.  **The tool says who may call it**
(`slife2/audience`, read from `tools/list`), and **the caller says who it is**
(`slife2/client`, attached to the call), and neither is an argument a model
writes.  The first section below is the tool's half; `CLIENT` and its readers
are the caller's.

Who a tool is for, said on the tool itself.

Every server in this system offers tools to one of two callers, and they are not
interchangeable.  A **plugin** — memory, the agent, a model backend — is called
by our own code at a moment the code already knows: `remember` after a turn,
`stream_chat` when the loop wants an answer.  A **tool server** offers tools to a
language model, which picks one by reading its description and calls it with
arguments it invented.

The toolhub is the one place that sees both — it reaches into the plugins we
start and out to somebody else's arxiv server — and it has to tell them apart,
because handing a model `remember` is handing it a write into any agent's
database, and handing it `send_message` is handing it another conversation.

**Said on the tool, not in its name and not in a list in the config.**  A name is
something the world rewrites: providers accept only letters, digits, underscore
and hyphen, and `slife2.toolhub.sanitise` exists to make a name legal, so policy
hung on a name is policy hung on a sanitiser.  A list in the config is a second
registry kept in step by hand, which is the thing the hub exists to avoid.

**Opt in, and the default is the safe half.**  A plugin's tool reaches the model
only if the tool says so; everything else — a tool that forgot, a tool written
before this module existed, a tool on a server nobody has thought about yet —
stays where it was.  The other convention, marking the internal tools and
exposing the rest, fails the other way: one forgotten mark puts `remember` in
the model's hands and nothing anywhere says so.  An entry under `tools:` needs no
mark at all, because the operator already opted in by writing it.

The word is the protocol's, not this module's.  MCP annotates *content* with an
audience — `["user", "assistant"]` — and a tool meant for the model is the same
idea with a different subject, so it travels in `_meta` under a namespaced key
rather than in a field invented here.  Measured against FastMCP 4.0.11, a
`meta=` given to `@mcp.tool` arrives unchanged on the far side of `tools/list`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The `_meta` key a tool declares its audience under.  Namespaced because
#: `_meta` is shared with the server, with FastMCP and with every other client
#: that reads it — the protocol's own keys are reverse-DNS for the same reason.
AUDIENCE = "slife2/audience"

#: Who the model is, in this vocabulary.  Not `"model"`: the protocol says
#: `assistant`, and a second word for one party is a translation somebody has to
#: remember to make.
MODEL = "assistant"

#: What a tool decorates itself with to say the model may call it:
#:
#:     @mcp.tool(meta=FOR_THE_MODEL)
#:     def now() -> str: ...
#:
#: A mapping rather than a function so the decorator reads as what it is — a
#: statement about the tool — and so a test can compare against it.
FOR_THE_MODEL: dict[str, Any] = {AUDIENCE: MODEL}


def for_the_model(meta: Mapping[str, Any] | None) -> bool:
    """Whether a listed tool may be offered to the model.

    Takes the tool's `_meta` rather than the tool, so this stays a leaf nothing
    has to import a server or a client library to use.

    Absence is the answer "no", and it is the important half: a tool that says
    nothing is one nobody has decided about yet, and the caller that finds it
    is the hub, whose only safe reading of silence is to leave it alone.
    """
    declared = (meta or {}).get(AUDIENCE)
    if isinstance(declared, str):
        declared = [declared]
    return MODEL in (declared or ())


#: The `_meta` key a *call* carries to say whose behalf it is on.  V1's memory
#: server had no such key and needed none: it ran one process per agent, so the
#: connection **was** the identity.  slife2's db server is shared — one
#: process serves every client id, the way one model server serves every
#: provider — so the identity has to be said, and this is where.
CLIENT = "slife2/client"


def client_meta(agent: str, subagent: str = "") -> dict[str, Any]:
    """What a harness-side caller attaches to a tool call it makes for one
    conversation."""
    return {CLIENT: {"agent": agent, "subagent": subagent}}


def client_of(meta: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """The `(agent, subagent)` a call is on behalf of, or `None` if it says.

    `None` is not an error here — most calls are not on anyone's behalf, and a
    model backend has no idea what a client id is.  It is the *reader* that
    decides whether it can proceed without one, and a tool that needs it says so.
    """
    found = (meta or {}).get(CLIENT)
    if not isinstance(found, Mapping):
        return None
    agent = found.get("agent")
    if not isinstance(agent, str) or not agent:
        return None
    subagent = found.get("subagent")
    return agent, subagent if isinstance(subagent, str) else ""


def forwarded_client(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """Just the caller's identity, as `_meta` for the next hop.

    Not the whole `_meta`, which carries the protocol's own keys as well —
    `progressToken` names *this* request's progress stream, and passing it on
    would point the far server's progress at a caller that never asked for it.
    A hub is a proxy, and a proxy forwarding its caller's identity forwards that
    and nothing else.
    """
    found = (meta or {}).get(CLIENT)
    return {CLIENT: found} if found is not None else {}


def request_meta(ctx: Any) -> Mapping[str, Any] | None:
    """The `_meta` a tool call arrived with, read from its context.

    Duck-typed on purpose, so this module stays a leaf: `ctx` is a FastMCP
    `Context`, and importing that here would put a server framework in the
    import graph of everything that reads a mark — `slife2.builtins` included.
    `request_context` is `None` outside a session, which is why the chain stops
    at the first missing link rather than assuming one.
    """
    request = getattr(ctx, "request_context", None)
    meta = getattr(request, "meta", None)
    return meta if isinstance(meta, Mapping) else None


def request_client(ctx: Any) -> tuple[str, str] | None:
    """The `(agent, subagent)` a tool call is on behalf of."""
    return client_of(request_meta(ctx))
