"""The names one process writes and another reads.

**A name is one fact, and both ends import it.**  A channel's marker, a
window's tools, an agent task's states, a peer's `join`: each is written by one
process and read by another, and the reader often has no business loading the
writer.  Spelled in the server module, every reader takes the server's whole
dependency graph with it — FastMCP, MQTT, a tunnel SDK — for a string, and a
rename inside the server breaks its readers at import rather than at the
contract.  Spelled again at the reader, the two copies drift, and the miss is
silent.

So the names live here, and both sides import them from here.  A plugin keeps
its module-level constant as the name it exports (`CONFIG_KEY`,
`MARKER_PREFIX`, the tool names) by importing this module's constant; its
readers do the same.

**Nothing here imports a plugin, FastMCP or a transport.**  The one function
that must know an MCP error's type imports it at the call, on the failure path,
so a display can import this module without a server stack.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# ═══════════════════════════════════════════════════════════════════════
# The window (`slife2-tui`)
# ═══════════════════════════════════════════════════════════════════════

#: The four tools the window's plugin serves, and none of them is the model's:
#: `submit` is a channel's, `note` is the harness's, `attach` is a window's, and
#: `attached` answers whether one is there.  Each crosses a process boundary,
#: so each is spelled once.
ATTACH = "attach"
SUBMIT = "submit"
NOTE = "note"
ATTACHED = "attached"

#: What a hand-over asks the window to do with the words, and the one branch
#: between them: `TURN` is run as a turn of the conversation, `NOTE` is drawn as
#: a harness line and nothing else.  `NOTE` is its own tool name because the two
#: coincide; `TURN` is not `SUBMIT` because what the window does with a submitted
#: message is *run it*, which is the fact the window branches on.
TURN = "turn"
KINDS: tuple[str, ...] = (TURN, NOTE)

#: What `submit` says when there is nothing to hand a message to.  A constant
#: because a *caller* has to be able to recognise it: see `refused`.
NO_WINDOW = "no window is showing"

#: The channel this window's own turns are sent on, and therefore the one whose
#: messages are the person's own words.  It is also the tui plugin's key in the
#: config's `servers:` table: an address is the whole of what is written down
#: about it.
WINDOW_CHANNEL = "tui"


@dataclass(frozen=True)
class Handover:
    """One hand-over on its way to a window: the words, and where they came in.

    `ticket` exists so that a caller and the push that carries it can agree on
    *which* hand-over is being acknowledged — a number rather than the words,
    because the same words twice is an ordinary thing for somebody to send.

    `kind` is what the window does with it, and there are two: a **turn** is put
    in the queue the person's own typing goes into and run as one (`submit`), and
    a **note** is drawn as a line and nothing else (`note`).  A note needs no
    channel — it is not on any channel, it is the harness talking — and carries
    `""` in its place so that one dataclass and one wire shape serve both.
    """

    ticket: int
    text: str
    channel: str
    kind: str = TURN


def encode(handover: Handover) -> str:
    """Encode a hand-over for an MCP progress notification's `message` field.

    Same convention as a turn's events (`slife2.libs.turn.events.encode`), and
    the same reasons: the field is a plain string, so the payload crosses as
    compact JSON, and `ensure_ascii=True` keeps it representable on a console
    whose codepage is not UTF-8.  The encode and the decode live beside each
    other so one round-trip test covers the contract.

    It is deliberately **not** one of `slife2.libs.turn.events`' variants: those
    are the events a *turn* produces, and this is a control message that causes
    one — or a line drawn without one.
    """
    return json.dumps(
        {
            "t": "handover",
            "id": handover.ticket,
            "d": handover.text,
            "c": handover.channel,
            "k": handover.kind,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def decode(message: str) -> Handover | None:
    """Decode a progress notification's `message`, or None if it is not ours.

    None rather than an exception for `slife2.libs.turn.events.decode`'s reason:
    a notification this build has no word for is a line not read, and the caller
    is a display that should stay up rather than a turn that should die.

    **And a `k` that is not one of `KINDS` is such a line.**  An absent or
    unknown kind used to read as `TURN`, argued from the older-plugin direction
    — a hand-over before there were two kinds was always a turn.  But the
    direction that costs something is the other one: a *newer* plugin's third
    kind would be executed as a conversation turn rather than drawn as a line,
    and a line run as a turn is a model call nobody asked for.  So the safer
    branch is the refusal (SDD Appendix C: a weaker path is not a substitute
    for a missing capability).
    """
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(payload, dict) or payload.get("t") != "handover":
        return None
    text = payload.get("d")
    channel = payload.get("c")
    if not isinstance(text, str) or not isinstance(channel, str):
        return None
    kind = payload.get("k")
    if kind not in KINDS:
        return None
    # **Checked like the two fields above it**, because this is the reader for
    # the stream a window holds open: `int("x")` and `int([1])` raise, and a
    # raise here ends the progress callback that delivers every hand-over for
    # that window — one malformed push from a peer on another build, or a torn
    # write, and the delivery path is gone for the life of the attachment.  A
    # `bool` is not a ticket either.
    ticket = payload.get("id")
    if not isinstance(ticket, int) or isinstance(ticket, bool):
        return None
    return Handover(
        ticket=ticket,
        text=text,
        channel=channel,
        kind=kind,
    )


def refused(error: Exception) -> bool:
    """Whether a `submit` failed because no window is showing that agent.

    **The one failure a caller may safely repeat**, and the reason this is a
    function rather than a `ToolError` check at the call site.  Everything a tool
    handler raises arrives at the far end as a `ToolError` (`serve` pins
    `mask_error_details=False`, which is what keeps the text) — so the *type*
    cannot tell a refusal from a stream that dropped, and a caller that read it
    as one would hand over words that had already been written.

    What makes this one decidable is that `submit` looks the session up **before
    it posts anything**: nothing was written, so trying somewhere else cannot
    duplicate anything.  The module that writes the message is the one that
    answers what it looks like — the same arrangement `MARKER_PREFIX` makes for a
    marker, and `tests/plugins/agent_task/test_agent_task.py` holds the pair
    together.

    The FastMCP import is at the call because this module is on the import path
    of a display, and `refused` is reached only after a call has already failed.
    """
    from fastmcp.exceptions import ToolError

    return isinstance(error, ToolError) and NO_WINDOW in str(error)


# ═══════════════════════════════════════════════════════════════════════
# An agent task (`agent-task`)
# ═══════════════════════════════════════════════════════════════════════

#: The four tools, named once.  They are the whole surface, and the vocabulary
#: they share is one: `agent_task_cancel` answers with the same `status` field
#: `agent_task_status` does, so stopping a task needs no second set of outcomes.
AGENT_TASK_START = "agent_task_start"
AGENT_TASK_STATUS = "agent_task_status"
AGENT_TASK_LIST = "agent_task_list"
AGENT_TASK_CANCEL = "agent_task_cancel"

#: Every state a task can be in.  `unknown` is not one of them — it is what a
#: name the registry does not hold answers, and it includes a task the cap has
#: forgotten, which is honest because this process can no longer tell.
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled"
UNKNOWN = "unknown"

#: The channel an agent task's answer comes back on, and the first half of the
#: key its own state is filed under.  A channel is the key a plugin runs under
#: (`wechat`, `tui`, and this) and a task's answer also wears the task's name —
#: `agent-task:research` — because a hand-over carries the channel and the words
#: and nothing else, and the window draws the bubble from the channel alone
#: (`slife2.tui.widgets.channel_prefix`).
AGENT_TASK_CHANNEL = "agent-task"

#: What the answer wears, so the model can read the task's id back out of the
#: row.  It is **content**, not an annotation this build derives: it goes into
#: the stored message verbatim, which by `SDD` §5.5's convention is what
#: the word-shaped spelling says and `[TURN:…]`'s capitals do not.
#:
#: **`AgentTaskResult`, because that is what the line is**: an agent task's
#: result, read by the model beside `slife2.libs.harness.async_tasks`'s
#: `[AgentToolResult:{…}]` — two results, one from a task and one from a tool
#: call, named so that neither can be taken for the other.  The *bubble* a window
#: draws is the shorter `AsyncTask(name)> ` (`slife2.tui.widgets.channel_prefix`):
#: a person is being told which task answered.
AGENT_TASK_MARKER = "[AgentTaskResult:"


# ═══════════════════════════════════════════════════════════════════════
# WeChat (`wechat`)
# ═══════════════════════════════════════════════════════════════════════

#: This server's key in the config's `servers:` table, and the channel an
#: incoming message becomes a turn on.  It owns no section of its own: the only
#: thing written down about it is an address, and a login is scanned rather than
#: configured (`slife2.plugins.wechat.ilink`).
WECHAT_CHANNEL = "wechat"

#: What the marker opens with.  A constant because the marker is *read* as well
#: as written: stored text carries it into a transcript, and the window takes it
#: back off by this string (`slife2.libs.harness.markers.strip`) rather than by a
#: second copy of the spelling that could drift from this one.
WECHAT_MARKER = "[Wechat:"


# ═══════════════════════════════════════════════════════════════════════
# The peer network (`peer-agent`)
# ═══════════════════════════════════════════════════════════════════════

#: The tool that puts an agent *on* the mesh — and it is not the model's.  A
#: window calls it and holds it open; see `slife2.plugins.a2a.server.join` for
#: why the mesh's membership is a call rather than a state the plugin keeps.
JOIN = "join"

#: The other call that is not the model's: **give up a member's place on the
#: mesh** (`forget`).  Its caller is the agent server, in the middle of
#: `subagent_remove` — a member can stop existing while this process is serving
#: it, and a directory that is gone cannot unsubscribe anything.
FORGET = "forget"


# ═══════════════════════════════════════════════════════════════════════
# The agent (`agent`)
# ═══════════════════════════════════════════════════════════════════════

#: The read a window that has just opened makes of its conversation.  One read,
#: two readers: the terminal's restore and the TUI probe
#: (`slife2.clients.tui`), which is why the name is here rather than in either.
TRANSCRIPT = "transcript"
