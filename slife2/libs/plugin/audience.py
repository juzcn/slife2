"""Who a call is for: the tool's audience, and the caller's own name.

Both travel out of band — beside the model rather than in it — and there are two
of them because a tool call has two parties.  **The tool says who may call it**
(`slife2/audience`, read from `tools/list`), and **the caller says who it is**
(the other key in this module, attached to the call), and neither is an argument
a model writes.  The first section below is the tool's half; `CLIENT` and its
readers are the caller's.

Who a tool is for, said on the tool itself.

Every server in this system offers tools to one of two callers, and they are not
interchangeable.  A **plugin** — the db, the agent, a model backend — is called
by our own code at a moment the code already knows: `remember` after a turn,
`stream_chat` when the loop wants an answer.  A **tool server** offers tools to a
language model, which picks one by reading its description and calls it with
arguments it invented.

The toolhub is the one place that sees both — every plugin's own tools arrive
there by `tools/list`, and so does every row a plugin declares for a server
somebody else runs — and it has to tell them apart,
because handing a model `remember` is handing it a write into any agent's
database, and handing it `send_message` is handing it another conversation.

**Said on the tool, not in its name and not in a list in the config.**  A name is
something the home rewrites: providers accept only letters, digits, underscore
and hyphen, and `slife2.clients.gateway.sanitise` exists to make a name legal, so policy
hung on a name is policy hung on a sanitiser.  A list in the config is a second
registry kept in step by hand, which is the thing the hub exists to avoid.

**Opt in, and the default is the safe half.**  A plugin's tool reaches the model
only if the tool says so; everything else — a tool that forgot, a tool written
before this module existed, a tool on a server nobody has thought about yet —
stays where it was.  The other convention, marking the internal tools and
exposing the rest, fails the other way: one forgotten mark puts `remember` in
the model's hands and nothing anywhere says so.  An entry under `mcp:` needs no
mark at all, because the operator already opted in by writing it.

The word is the protocol's, not this module's.  MCP annotates *content* with an
audience — `["user", "assistant"]` — and a tool meant for the model is the same
idea with a different subject, so it travels in `_meta` under a namespaced key
rather than in a field invented here.  Measured against FastMCP 4.0.11, a
`meta=` given to `@mcp.tool` arrives unchanged on the far side of `tools/list`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from slife2.libs.host import paths

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


#: The `_meta` key a *call* carries to say whose behalf it is on.  The db
#: server is shared — one process serves every client id, the way one model
#: server serves every provider — so the identity has to be said, and this is
#: where.  A process per agent would let the connection be the identity; this
#: server cannot.
CLIENT = "slife2/client"

#: The key inside that mapping naming the *home* the call is for: the data
#: directory of the process that made it.
#:
#: **In here rather than in a key of its own**, because it travels exactly as the
#: identity does and for the same reason: the servers are shared, so a plugin
#: writing an agent's turn file is not the process that was started with
#: `--data-dir`, and only the caller knows which directory the work belongs in.
#: `forwarded_client` already carries this whole mapping on to the next hop, so a
#: home reaches the far end of a chain of proxies without a second mechanism.
#:
#: **Not an argument, for the reason the conversation is not one**: a model that
#: could name a directory could name somebody else's.
DATA_DIR = "data_dir"


def client_meta(agent: str, subagent: str = "", home: str = "") -> dict[str, Any]:
    """What a harness-side caller attaches to a tool call it makes for one
    conversation.

    **The home defaults to this process's**, which is right at every call site
    there is: a client, a launcher and a test are one home from start to finish,
    and a plugin making a call on somebody else's behalf is *inside* a call of
    its own, where `slife2.libs.host.paths.home` already answers with the caller's
    (`slife2.libs.plugin.protocol.serve` is what put it there).

    An explicit argument is for the case the ambient answer is the wrong one —
    a caller acting for a home it is not itself in — and there is no such caller
    today.  It travels exactly as written, because a caller that names a
    directory is stating something about a home this process is not in, and
    rewriting it would be this module overruling the one party that knows.

    What the *default* holds is `_for_the_wire`'s answer rather than the
    directory as this process happens to spell it: the home is read at the other
    end, in a process started somewhere else, so a relative one there is a
    different directory.
    """
    return {
        CLIENT: {
            "agent": agent,
            "subagent": subagent,
            "data_dir": home or _for_the_wire(paths.home()),
        }
    }


def _for_the_wire(root: Path) -> str:
    """A home as it leaves this process: **absolute**, however it was spelt.

    A relative one is the hazard this exists for.  The daemon that receives it
    resolves it against *its own* working directory, and every daemon here is
    spawned with that set to the machine's runtime directory
    (`slife2.libs.host.launcher._argv`), so `--data-dir rel/chk` arrives meaning
    `<runtime>/rel/chk`: the client reads its config from the directory it
    named, the daemon writes `agents/jack` under the runtime directory, and
    nothing errors — the work is simply somewhere else, which is the one shape
    of failure a file layout can have that no message reports.  `_argv` resolves
    the directory it puts in a child's argv for exactly this reason; the `_meta`
    hop is the same crossing by another road.

    An absolute path travels as it was named.  It already means one directory in
    every process, whatever that process's working directory is, so there is
    nothing to fix — and resolving it would rewrite a home reached through a
    symlink, which is a spelling the operator chose.
    """
    return str(root if root.is_absolute() else root.resolve())


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


def client_key(agent: str, subagent: str = "") -> tuple[str, str, str]:
    """The key a shared daemon holds one conversation's state under.

    **(home, agent, subagent)** — and the first element is the one this exists
    for.  Every plugin here is one process serving every agent, which is why it
    keys its state by an agent name at all; the moment there are two homes there
    are two agents called `jack`, and a table keyed by the name alone answers the
    second one with the first one's conversation.  That is not a stale value but
    the wrong conversation — a turn written into somebody else's history, or a
    tool list built for a config that is not the caller's.

    Read from `slife2.libs.host.paths.home` rather than passed, and every caller is
    inside a call where that is already the caller's home
    (`slife2.libs.plugin.protocol.serve`).  A caller *outside* a call — an idle
    sweep — must not build a key at all: it iterates the ones already there.
    """
    return (str(paths.home()), agent, subagent)


def home_of(meta: Mapping[str, Any] | None) -> str:
    """The home a call named, or `""` when it named none.

    Empty is the honest answer for a call that said nothing — a `status`, a
    model server's listing — and both readers of it treat it as "the directory
    this process is in", which is what every server did before there were
    homes.  A home that is not a string, or is blank, is the same as no home:
    the field arrives off a wire, and a caller that sent something unusable is
    not a caller that named a directory.
    """
    found = (meta or {}).get(CLIENT)
    if not isinstance(found, Mapping):
        return ""
    named = found.get(DATA_DIR)
    return named if isinstance(named, str) else ""


#: The refusal for a home spelled on another platform.  Two machines can share a
#: loopback — Windows and WSL under mirrored networking are the measured case —
#: and then a call arrives naming its home in the other OS's spelling.
FOREIGN_HOME = (
    "This call says its home is {home!r}, which is not an absolute path on "
    "this machine — a server shared across machines can only serve a home in "
    "its own filesystem. Give this instance its own servers rather than "
    "reusing ones started by another machine."
)


def foreign_home(home: str) -> str:
    """*home* when it is not a path this machine can resolve, else `""`.

    A home travels as an absolute path in the **caller's** spelling
    (`_for_the_wire`), and the far end resolves it in its own filesystem.  A
    Windows path (`D:\\...`) is not absolute on Linux, nor a POSIX one on
    Windows; treated as a relative name it would create a directory literally
    called `D:\\...` under the daemon's working directory and write the caller's
    work there — a file layout failure no message reports.
    """
    return home if home and not Path(home).is_absolute() else ""


def with_home(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """`meta`, with this process's home in it unless it already names one.

    **What every outgoing call to one of our own servers goes through**, so that
    a home reaches the far end of a chain rather than only the first hop:
    `slife2.libs.plugin.protocol.open_server` hands back a client that does this
    for every call it makes, and the hub's own proxy adds it to the identity it
    forwards for the same reason.

    A meta that already names a home is left alone, because a forwarded home is
    the *caller's* — rewriting it with this process's would move somebody else's
    work into whichever directory happens to be in front of us.  A meta that says
    nothing gets a blob with a home and no agent, and that is not the same thing
    as a nameless client: `client_of` answers `None` for it, so no plugin reads
    it as somebody's, and `home_of` answers with the directory the work belongs
    in, which is the whole of what that call needed to say.
    """
    found = dict(meta or {})
    blob = found.get(CLIENT)
    named = home_of(found)
    if named:
        return found
    merged = dict(blob) if isinstance(blob, Mapping) else {}
    merged[DATA_DIR] = _for_the_wire(paths.home())
    found[CLIENT] = merged
    return found


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
    import graph of everything that reads a mark — `slife2.plugins.skills.server`
    included.
    `request_context` is `None` outside a session, which is why the chain stops
    at the first missing link rather than assuming one.
    """
    request = getattr(ctx, "request_context", None)
    meta = getattr(request, "meta", None)
    return meta if isinstance(meta, Mapping) else None


def request_client(ctx: Any) -> tuple[str, str] | None:
    """The `(agent, subagent)` a tool call is on behalf of."""
    return client_of(request_meta(ctx))


def request_home(ctx: Any) -> str:
    """The home a tool call named, or `""`.

    The reader for the few places that need the *name* rather than the ambient
    answer — a key into a table of per-home state is the one that exists — and
    for a test asserting what a call carried.  Everything that resolves a file
    wants `slife2.libs.host.paths.home` instead, which the server has already
    arranged to answer with this.
    """
    return home_of(request_meta(ctx))


#: The sentence a call that did not say whose it is is refused with.  **One
#: sentence with two holes**, because five plugins say it — a task, a schedule, a
#: cabinet, a login, a mesh — and the only words that change are the thing that is
#: missing and the thing that is the agent's.  That is the same arrangement
#: `slife2.libs.plugin.family.FamilyWords` exists for, and for the same reason: two copies
#: of a sentence are two chances for a model to be taught two vocabularies for
#: one mechanism.
#:
#: `{why}` is a hole rather than the fixed tail the other four use, because the
#: mesh's reason is genuinely different — a peer sees an *identity*, not a piece
#: of somebody's state — and a template that could not say so would be a template
#: the fifth plugin silently ignored.
UNADDRESSED = (
    "This call did not say which conversation it is for, so there is no {what} — {why}"
)

#: The tail four of the five use: the shared process cannot guess, and the thing
#: asked for belongs to the agent the call did not name.
AGENTS_OWN = "one process serves every agent, and {own} is the agent's"


def unaddressed(what: str, own: str, *, why: str = "") -> str:
    """The refusal, with this plugin's words in it.

    `what` is the missing thing as a reader meets it — "agent whose task it could
    be", "cabinet to act on" — and `own` is what is the agent's: "a task", "the
    cabinet".  `why` overrides the shared tail for the one plugin whose reason is
    its own.
    """
    return UNADDRESSED.format(what=what, why=why or AGENTS_OWN.format(own=own))
