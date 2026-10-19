"""slife2-agent — the agent loop, as an MCP server that owns its conversations.

The middle of the chain.  It is a server to the TUI and a client to an LLM
server, and it holds nothing else: no provider SDK is importable here, no API key
is readable here, and the only thing it knows about the model behind it is a URL
and a model name from the config.

**This server keeps the conversations**, and it addresses each one by
`(agent, subagent)` — the same pair every other server in this system keys its
own state by.  `subagent=""` is the agent's own conversation; anything else is
one of its subagents, an agent it made and owns (`subagent_set`, SDD §3.5).  A
caller submits a user message under that key and gets
that turn's answer back.  The management surface a client uses is
`send_message`, `reset`, `transcript` (what a window that has just opened asks
for), `subagents` (the list a conversation switcher offers), and `watch` (the
live read of a conversation the caller is not running).  `_check_new_input` is
not part of that surface: it is the name a message arriving mid-turn is
delivered under, and it is declared here because the pair the harness writes
into a conversation has to name a tool the request carries.

**A key is created when it is first used, and it never expires.**  That is the
difference from the handle this server used to mint: an id could go stale — a
daemon restart, an idle window — and every caller then had to carry a "your loop
is gone" path.  A name cannot go stale.  The idle sweep still runs, but it only
reclaims memory; nothing a caller can observe depends on it.

**The key travels with the call.**  Everything this server asks of a peer is
asked on behalf of the same `(agent, subagent)` — the write to the db and the
model call both carry it — so a hop is never anonymous.  What the model servers do
*not* do with it is keep the conversation: see DESIGN.md §2.2 for why the history
has to live here, on the side of the hop that does not speak a wire protocol.

**The tools come from the toolhub**, and are asked for again before every model
call — the list goes out with each request, so it is read with each request.
That is why this server holds no tool registry of its own and why `loop.py`
still knows nothing about MCP: it is handed a coroutine, and where the registry
came from is nobody else's business.  Our own plugins included: one owner for
the model's whole tool list is worth one hop.  See DESIGN.md §4.1.

Two consequences of owning the history are paid for here rather than avoided,
because they are the price of the state:

* **A per-key lock**, so two turns cannot race over one list.  `send_message`
  takes it; a second caller waits, and that wait *is* the inbox.  Distinct keys
  hold distinct locks, so two agents' conversations run at the same time.
* **Cut-in**, which is what waiting *for* means.  A message that arrives while a
  turn runs is handed to that turn at its next iteration boundary — as a call to
  `_check_new_input` and a result carrying the message's own words — so the model
  addresses it inside the turn it arrived in rather than one turn later.  v1's
  mode, always on, and the one place this server writes a message into a
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

What statelessness bought and is now given up deliberately: a conversation store
that can grow without bound.  See DESIGN.md §10 — trimming is the next thing, not
a thing this cut pretends to have solved.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from collections import deque
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Coroutine,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

import slife2.helpers.timeouts as _T
from slife2.clients.gateway import flatten
from slife2.clients.model import MCPBackend, open_backend
from slife2.clients.toolclient import (
    FUNC_TOOL_UNLOAD,
    remote_tools,
    trim_answer,
    unload_tools,
)
from slife2.helpers.clock import now
from slife2.libs.harness import dumps
from slife2.libs.harness.async_tasks import Deliver, async_tools
from slife2.libs.harness.dumps import Compress
from slife2.libs.harness.handover import DeliveryFailed, hand_over
from slife2.libs.host import configfile
from slife2.libs.host.config import (
    AGENT_SERVER_NAME,
    API_BACKENDS,
    API_SERVER_NAMES,
    CABINET_SERVER_NAME,
    CONTEXT_SERVER_NAME,
    LOCAL,
    TOOLHUB_SERVER_NAME,
    Config,
    ConfigError,
    for_the_call,
    load_cached,
)
from slife2.libs.host.paths import (
    agent_dir,
    in_home,
    known_homes,
    subagent_dirs,
    subagent_file,
    subagent_path,
)
from slife2.libs.host.runtime import read_claim
from slife2.libs.model.base import LLMBackend
from slife2.libs.plugin.audience import (
    FOR_THE_MODEL,
    client_key,
    client_meta,
    request_client,
    unaddressed,
)
from slife2.libs.plugin.protocol import (
    ClientId,
    close_server,
    describe,
    house_server,
    open_server,
    serve_plugin,
    tool_payload,
    unreadable,
)
from slife2.libs.turn import attachments
from slife2.libs.turn.context import turn_note, with_note
from slife2.libs.turn.events import (
    ContextChosen,
    ToolApprovalRequested,
    TurnEvent,
    TurnObserver,
    encode,
)
from slife2.libs.turn.loop import AgentLoop, AutoCall, TurnResult, harness_call
from slife2.libs.turn.messages import Message, ToolCall
from slife2.libs.turn.prompt import render as render_system_prompt
from slife2.libs.turn.tokens import estimate_text_tokens
from slife2.libs.turn.tools import CHECK_NEW_INPUT, Tool, ToolRegistry
from slife2.libs.turn.watch import (
    Closed,
    Snapshot,
    TurnStarted,
    WatchItem,
)
from slife2.libs.turn.watch import (
    encode as encode_watch,
)

logger = logging.getLogger(__name__)

SERVER_NAME = AGENT_SERVER_NAME

#: This server's key in the config's `servers:` table, and so the name the
#: launcher starts it under.  **The same word as the `agent:` section and not a
#: coincidence**: that section is this plugin's settings (`AgentSettings`) and
#: the endpoint in `servers:` is where they are served from, so `PLUGINS` files
#: one row under the one key both halves are named by.  Declared here like every
#: other plugin's, because `serve_plugin` takes it and a literal at the call site
#: is the only other way to say it.
CONFIG_KEY = "agent"

#: How many messages one loop may have waiting.  The client's per-call timeout
#: starts when the call is made, not when its turn starts, so an unbounded queue
#: is an unbounded wait — and a wait longer than the timeout closes the stream
#: and cancels the turn, which is the very way a message gets lost.  A bound is
#: what keeps "queued" from meaning "eventually dropped".
MAX_QUEUED = 32

#: How a conversation's results get fitted into it: the backend the turn is
#: running on and whose conversation it is, in return for the callback the loop
#: asks.  A *builder* rather than the callback, because half of what the callback
#: needs — the backend — is decided inside `make_loop`.
Compressor = Callable[[LLMBackend, ClientId], Compress]

#: The call the subagent network answers for a member that no longer exists —
#: `slife2.plugins.a2a.server`'s tool of that name, and **spelled here too**: a
#: name crossing a process boundary is written in two places, which is what
#: `JOIN`'s own comment says about the window's call.  It is not a tool the model
#: has; its only caller is `subagent_remove`, through `leave_the_mesh`.
FORGET = "forget"

#: The two tools that make and unmake a subagent — the whole of what this adds to
#: the mesh's vocabulary, and two verbs rather than four (SDD §3.5): a subagent
#: is *made* and *unmade*, and everything else it can do, it does with the tools
#: an agent has.
#:
#: **`_set` and `_remove` rather than `_add`**, because that is what this system
#: calls membership: `mcp_set`/`rest_api_set`/`skill_set` make a thing the agent
#: holds and `*_remove` ends it, in every family there is.  A second spelling for
#: the same act would be a vocabulary a model has to learn twice.
SUBAGENT_SET = "subagent_set"
SUBAGENT_REMOVE = "subagent_remove"

#: What a subagent is when the call that made it named no role, and the word the
#: shipped `subagent.j2` opens with.  A **role** is what a subagent is *for* —
#: the one part of what it is that its maker decides and not the system — and it
#: is kept in the subagent's own file, which is where everything about one
#: subagent lives (`subagent_spec`).
DEFAULT_ROLE = "subagent"

#: The system prompt a subagent's conversation opens with, shipped and rendered
#: per conversation (`slife2.libs.turn.prompt`).  **Not the template an agent's
#: own conversation uses** (`agent.system_prompt`): the two have different things
#: to say — this one says what a subagent is, whose it is, and where its work
#: comes from — and an operator's prompt
#: for their own agent cannot say any of that.  A bare name, so it is looked up
#: among the templates shipped beside `system.j2`.
#:
#: **One template for every role, for now.**  `role` reaches it as a word ("You
#: are scout, one of jill's subagents — your role is code reviewer") and not yet
#: as a choice of *file*: a per-role
#: template is the step this design leaves open, and it is left open on purpose
#: rather than guessed at — a role that selects `prompts/<role>.j2` needs an
#: answer for a role with no file, and that answer is a design decision.
SUBAGENT_PROMPT = "subagent.j2"

#: What this process holds one conversation's state under: `(home, agent,
#: subagent)`, built by `slife2.libs.plugin.audience.client_key`.  Not `ClientId`,
#: which is what travels on the wire — a home rides in the call's `_meta` rather
#: than in an argument, so the two are built from different places and only one
#: of them is a key.
ConversationKey = tuple[str, str, str]


async def _asked(
    backend: LLMBackend,
    call: ToolCall,
    text: str,
    messages: Sequence[Message],
    *,
    window: int,
) -> dumps.Decision | dumps.Rejected:
    """One discriminator call.  **A reply is what comes back.**

    `REJECTED` is a reply this build cannot read, which is an ordinary thing for
    a model to produce and the one failure this mechanism is designed around.  A
    call that *did not complete* is not that, and is not caught: see below.
    """
    instruction = dumps.instruction(
        tool=call.name,
        body=text,
        budget=window,
        user_input=_current_input(messages),
    )
    # No tools, and no observer: this is one question and one answer, and it runs
    # on the backend the turn already has — the conversation's own model, called
    # the way the conversation is called.  **A call that does not complete is not
    # caught here**: a model server that has stopped answering is a system that
    # has come apart, and the turn it is serving is about to fail on its next
    # request anyway — so absorbing it would buy a worse trace, not a turn.
    stream = backend.stream([Message(role="user", content=instruction)], [])
    # **Drained, though nothing here displays a chunk**, and that is what puts a
    # clock on this call.  The stall watchdog lives in the drain
    # (`slife2.clients.model.MCPBackend._drain`) and is armed by *iterating* the
    # chunks, so awaiting `result` alone waits on a connection with no deadline
    # of any kind: `open_backend` passes no timeout — the watchdog is the bound,
    # for the reason the loop gets away with it — and a provider that took the
    # call and then went quiet would hang this step for ever with the
    # conversation's lock held.  The same silence fails a turn in the loop in
    # `timeouts.work.stall` seconds.  The chunks are discarded because this is
    # one question and one answer: `read_reply` reads the result, and a step of
    # the discriminator's own is not something the window draws.
    async for _ in stream.chunks:
        pass
    reply = (await stream.result).text
    return dumps.read_reply(reply)


async def _first_page(client: Client, call: str, client_id: ClientId) -> str:
    """The designed fallback: the reader's own first window over the same output.

    **The same function a model would have called**, which is the whole point of
    it: a conversation whose discriminator could not answer gets what a
    conversation whose model asked for the beginning would have got — the same
    numbers, the same mark on the same lines, and the same sentence about where
    the rest is.  A second renderer here would be a second set of addresses for
    one output, and the one a model copied out of the conversation would reach
    different text.

    **A read that fails is left to fail the turn**, and this is deliberately a
    bare call rather than a wrapped one.  The row was written a moment ago by
    this same process, so a window that cannot be read back is not a case to
    design for — it is the cabinet having stopped answering, which the register
    (SDD Appendix C) puts on the side of the steps that must succeed.  It used to catch
    every exception, log a warning and answer `""`, which is an empty tool result
    entering the conversation: the fallback `compress_for` forbids one paragraph
    up ("nothing else here falls back"), and the mask covered exactly the failure
    this step would then be unable to report.
    """
    answer = await client.call_tool(
        "dump_read",
        {"call": call, "offset": 1},
        meta=client_meta(*client_id),
        timeout=_T.timeouts.work.hub,
    )
    return flatten(answer.content)


def _current_input(messages: Sequence[Message]) -> str:
    """The input this turn is answering: the newest user message, as words.

    Read off the list rather than threaded down, because the loop appends the
    user's message itself before it calls anything — so the newest `user` in the
    list *is* the current input, and a message that cut into the middle of the
    turn is the current input too, which is the honest answer to a question asked
    while the turn was running.
    """
    for message in reversed(messages):
        if message.role == "user" and isinstance(message.content, str):
            return message.content
    return ""


#: The harness tool a message that arrived mid-turn is delivered under — v1's
#: *cut-in*, and the name is v1's for the same reason its pair shape is.
#:
#: **It is a tool because a pair has to name one.**  The Responses and Messages
#: backends reject a tool call in history whose name is not in the request's
#: declared tool list, so an injected message cannot be a fabrication of the
#: history layer: it is a call to a real tool, declared like any other, that the
#: *harness* makes on the model's behalf.  The model can call it too and gets a
#: sentence saying there is nothing to fetch — only the harness pulls.
#:
#: Being declared is not enough for it to survive: `slife2.libs.store.db` admits
#: `_`-prefixed names to the model's list only from its own set, and
#: `slife2.plugins.toolhub.server.ALWAYS_LOADED` is what stops the model unloading a name its
#: own history refers to.  Both spellings are this string, and the string itself
#: lives in `slife2.libs.turn.tools.CHECK_NEW_INPUT` because the display that
#: rebuilds a transcript reads it too.

#: The tool an image comes in by.  **The model calls it**, which is the whole
#: mechanism: somebody writes a path in a sentence and the model attaches it.  v1
#: also let its harness call this for `@path` markers in the text, and that half
#: is not ported — it saves one model call and costs a second entry point into
#: one behaviour, a marker syntax every user has to be taught, and an argument
#: only the harness ever omits.  What is kept is the half that carries the
#: feature.
#:
#: **An ordinary tool, not a harness tool.**  Nothing this process's machinery
#: writes refers to it by name — no pair, no prompt — so it is offered to the
#: model the way any plugin's tool is, and whether it is in the list from the
#: first turn is the budget's business (`tool_load:`), not this file's.
#:
#: It injects content parts into the **user message** — where v1's tool put them
#: too, and the only place a wire here can carry an image: a tool result is text
#: on every backend this system speaks.  Those parts are live-session state and
#: nothing more; what is stored is the text plus a note naming every file
#: (`slife2.libs.turn.attachments.image_note`), which is also what the repair below leaves
#: when a provider refuses the picture.
ATTACH_IMAGE = "attach_image"

#: What that tool answers when there is nothing to hand over — an empty queue, or
#: a head that carries images and so cannot ride a tool result.  A sentence and
#: not an empty string: it lands in a tool result, and a blank one reads as a
#: failure rather than as "nothing had arrived".
NOTHING_WAITING = "Nothing waiting: no message has arrived since the last boundary."

INSTRUCTIONS = (
    "A conversational agent. Call `send_message` with an agent name and what was "
    "said; assistant output arrives as progress notifications on that call, and "
    "the result carries the final answer. The conversation is kept here and is "
    "addressed by `(agent, subagent)` — send only what is new, and use `reset` to "
    "start one over."
)


class ProgressObserver:
    """Reports turn events as MCP progress notifications.

    `progress` is a monotonic counter and `total` stays None: how many deltas a
    turn will produce is genuinely unknown, and inventing a total would make
    clients render a percentage that means nothing.

    **`publish` is the second observer a turn has**: a watcher is not the caller
    of the turn, so its copy cannot go down this call's stream.  The callback is
    synchronous and must stay so — it appends to the conversation's tap and
    enqueues for each watcher, and a display that is slow must never be a
    `send_message` that waits (`slife2.libs.turn.watch` says what a watcher is
    handed).  The caller's own notification is sent after it, so a broken
    watcher cannot cost the caller an event.

    This class is also the seam for coalescing.  A fast local model can produce
    hundreds of notifications a second, and if that ever matters the fix is a
    small buffer here — one place, not threaded through the loop.
    """

    def __init__(
        self,
        ctx: Context,
        *,
        publish: Callable[[TurnEvent], None] | None = None,
    ) -> None:
        self._ctx = ctx
        self._publish = publish
        self._count = 0

    async def on_event(self, event: TurnEvent) -> None:
        if self._publish is not None:
            self._publish(event)
        self._count += 1
        # A no-op when the caller sent no progress token.  That is intended and
        # it is silent: `report_progress` checks for the token itself, so a
        # client that does not want a stream simply does not get one, and still
        # gets a correct result at the end.
        await self._ctx.report_progress(self._count, None, encode(event))


@dataclass(eq=False)
class Pending:
    """A message submitted to a loop, waiting for its own turn — or for one.

    It is held here, and *not* appended to the loop's messages as a user message,
    until its turn begins.  What may happen to it in between is the running
    turn **folding it in** (`inject`): the message goes to the model at that
    turn's next iteration boundary as a harness tool pair, and this entry's `result`
    is set to that turn's answer.  It is never appended as a user message
    mid-turn, which is what would be steering nobody asked for — the model reads
    a folded message as something that arrived, which is what it is.

    `eq=False` because the inbox is a queue of *submissions* and not of values.
    `deque.remove` compares by equality, so the generated `__eq__` — which
    compares `(prompt, attachments, channel)` — would make two callers who sent
    the same text one entry: the first turn takes its own message out of the queue,
    and its closing `remove` then takes the *other* caller's place in it, since
    that entry is equal.  The count `MAX_QUEUED` guards would then fall below
    the number of callers actually waiting, which is the bound's whole job.
    """

    prompt: str
    #: What this message arrived with, as **sources** rather than as encoded
    #: bytes: a path on this machine, an `http(s)` URL, or a `data:` URI.  They
    #: are turned into content parts by `slife2.libs.turn.attachments` — the same encoder
    #: `attach_image` uses, so a caller that holds a path and a model that is
    #: given one cannot come to different conclusions about the same file.
    images: list[str]
    channel: str
    #: Whether this caller can be asked a question mid-turn and will answer it.
    #:
    #: **It is the caller's to say, and nobody else can.**  v1 answered the same
    #: question with "is a handler installed", which worked because one process
    #: was one host — here every turn arrives at the same server, and whether
    #: anybody is watching is a fact about *this* call rather than about the
    #: process: the TUI is watched, a WeChat message is not, and the two can
    #: address one conversation an hour apart.  False is the safe default for
    #: the same reason it is v1's rule: a caller that cannot answer must never
    #: be asked, or the model's request for a check becomes a turn that hangs.
    watched: bool = False
    #: Set by the turn that folded this message into itself, and **only when that
    #: turn produced a result**.  `None` is therefore the honest "nothing folded
    #: this in — run your own turn", and one field carries both the branch and
    #: the answer rather than a second slot that could disagree with it.
    #:
    #: A turn that produced no result — cancelled *or* failed — leaves this alone
    #: on purpose: the message was not answered, so it still owes itself a turn,
    #: and the caller is already parked on the lock with this very object in hand.
    #: Restoring it to the inbox instead would race a caller that had been
    #: cancelled in the meantime and leave an orphan the next turn would fold in
    #: with nobody waiting for it.
    result: TurnResult | None = None
    #: The model that answered this message, written **with** `result` and only
    #: by the turn that wrote it.  Kept on the entry rather than read off the
    #: loop when the caller is finally answered, because the two can differ: the
    #: reply names what answered, and a `model_switch` arriving while that turn
    #: was running moves the loop's reference without moving the answer
    #: (`slife2.plugins.agent.server._reply` is where that is argued).  Nothing
    #: reads this unless `result` is set, so its emptiness is not a branch —
    #: `result` is still the one field that says whether anything folded this in.
    model: str = ""
    #: The submitting client's own id for this message, echoed on the
    #: `MessageCutIn` event when a running turn takes it.
    #:
    #: **Opaque here and load-bearing there.**  A window that submits a message
    #: while a turn is running has to know whether that message was folded in or
    #: ran its own turn — its call produces no events in the first case — and
    #: the receipt is what makes that an identity rather than a guess at the
    #: words.  The server never reads it.
    receipt: str = ""


@dataclass
class Tap:
    """The last or current turn of one conversation, kept for a watcher.

    **A watcher arrives mid-turn by definition** — that is when somebody wants
    to look — and the store has nothing for the turn in flight: a turn is written
    when it ends (`record`).  So the turn's own display events are kept here from
    the moment it opens, and a watcher that joins is handed them before it joins
    the live queue.  The alternative was showing a turn's tail without its head,
    which is the one thing the main window never does.

    `recorded_turn` is the store id the turn was given, once it has one.  A
    watcher uses it to tell whether the snapshot it has just read already
    contains this turn: the same turn may be both in the store and here, and
    drawing it twice is the duplicate this field exists to prevent.

    **One per conversation, replaced when the next turn starts.**  A turn's
    record is awaited before its lock is released (or picked up by the next
    turn's `settle_record`), so by the time a replacement happens the store has
    the old one — which is why nothing here outlives the turn it describes by
    more than one.
    """

    started: TurnStarted
    events: list[TurnEvent] = field(default_factory=list)
    recorded_turn: int | None = None


@dataclass(eq=False)
class Watcher:
    """One open `watch` call, and the queue its frames are passed through.

    **The queue is the Context seam**, and it is not an optimisation.  A
    watcher's frames must be reported from the watcher's own handler task:
    `Context.report_progress` resolves its progress token from the request that
    is current when it is called, so an observer reporting from a *turn's* task
    would push into the turn caller's stream (`slife2.plugins.tui.server._push`
    is the same arrangement).  The handler owns the reporting; everything else
    only `put_nowait`s.

    Unbounded because every entry is a display frame for one connected client,
    and the client is a local terminal reading as fast as it is written.  A
    watcher that genuinely falls behind is the client's problem to fix by
    closing the view; the turn it is watching never waits for it.

    **`eq=False` because this is a session rather than a value**: two readers
    are two watchers however alike their queues look, and the set they are kept
    in is a set of readers — the generated equality would make the second one
    the first, and a removal would take the wrong one out.
    """

    queue: asyncio.Queue[WatchItem] = field(default_factory=asyncio.Queue)


@dataclass
class Outcome:
    """What one turn did, filled in even when the turn was cancelled.

    `messages` is a copy taken after any repair, so it is the slice this turn is
    answerable for: the whole exchange, or as much of it as the turn had
    completed when it was interrupted.
    """

    messages: list[Message] = field(default_factory=list)
    result: TurnResult | None = None
    #: Sources this turn was handed that could not be attached, in the caller's
    #: words.  On the outcome rather than on the loop, because it is a fact about
    #: *this* turn — the next one attaches nothing of the sort — and it is what
    #: the caller is told, since a call whose pictures were fewer than it asked
    #: for is a call that cannot tell.
    refused: list[str] = field(default_factory=list)
    #: The model this turn *ran on*, taken when it started.  Held because the
    #: loop's own reference can move while a turn is in flight — `model_switch`
    #: moves every conversation it is holding — and what that turn's record must
    #: name is the model that answered it, which is the one it began on.
    model: str = ""
    started_at: str = ""
    completed_at: str = ""
    #: The inbox entries this turn folded in, kept so a record that is refused
    #: can **un-answer** them (`unanswer_folded`): a caller whose message is
    #: rolled back with the turn must run its own turn, not be told an answer
    #: that is no longer in the conversation.
    folded: list[Pending] = field(default_factory=list)


@dataclass
class PendingRecord:
    """A turn's detached write, and the turn its id will make addressable.

    **The id is not knowable when the caller needs it.**  The write is detached
    — `detach` says why — when the turn was interrupted (the caller is gone and
    cannot wait) or when a caller was cancelled during the save of a turn that
    had already finished.  What it answers with arrives later, and the turn is
    not addressable until `settle_record` waits for it.  The outcome and the
    item ride with the task because they are what the footnote is made of: the
    timestamps the row was written with and the channel it carries.
    """

    written: asyncio.Task[int | None]
    outcome: Outcome
    item: Pending


@dataclass
class Loop:
    """One conversation, and everything that makes it a thing.

    `subagent` is part of the identity rather than a label on it: two keys differ
    exactly when their pairs differ, and the pair is what the lock, the inbox and
    the write to the db are each scoped to.
    """

    agent: str
    subagent: str
    model: str
    #: **The iteration cap this conversation started on**, read where the model is
    #: read — at the start, from the config as it was then — and not per turn.
    #: `make_loop` runs on every turn, so taking it from the live config would
    #: let an operator's edit change the terms of a turn already in flight; the
    #: boot config was the other half of the same mistake, keeping a cap the
    #: conversation that *should* have read the edit never saw.
    max_iterations: int
    messages: list[Message]
    lock: asyncio.Lock
    last_used: float
    inbox: deque[Pending] = field(default_factory=deque)
    #: Whether this conversation has been put back on the context it had when it
    #: last stopped.  A property of the *loop* rather than of the call that
    #: created it, which is the fix rather than a detail: two callers can arrive
    #: on one brand-new key at the same time, and only one of them is the caller
    #: that created it — so a per-call flag left the other running a turn on the
    #: empty list the restore was about to replace.  Set under the lock, and only
    #: once the restore has actually answered, so a store that failed is tried
    #: again by the next turn rather than leaving the conversation blank for the
    #: life of the process.
    restored: bool = False
    #: The entries this turn has folded in, in the order it took them.  Held for
    #: exactly one turn: it is how the turn's own end reaches them (their `result`
    #: is set there), and it is cleared by the same `finally`, so a later turn can
    #: never answer for one this one took.
    injected: list[Pending] = field(default_factory=list)
    #: Which turns of the log `messages` is made of, in order — the agent's copy
    #: of the live-context list the store persists.  Held here rather than read
    #: per turn because it *is* the in-memory state: `rebuild` is handed it and
    #: answers with its replacement, and the two must be the same list or a
    #: keep-list would name turns the messages do not have.
    turn_ids: list[int] = field(default_factory=list)
    #: How many of `messages` those ids account for — the system prompt, then
    #: each turn in the list, in order.  What follows is the **carried tail**:
    #: messages this conversation holds that no turn in the list covers, which a
    #: rebuild takes back verbatim rather than reconstructing.
    #:
    #: **The ids have to describe a *prefix*, and that is the invariant the whole
    #: count rests on.**  `rebuild_into` hands `len(messages) - covered` to the
    #: store as "how many messages at the end no turn accounts for", and the store
    #: re-appends `messages[-carried:]` — so an unbacked run that is not at the
    #: end would be read off the wrong end of the list entirely.
    #:
    #: A turn is unbacked only while its write is *pending*: it was interrupted
    #: (the caller is gone) or its caller was cancelled during the save of a turn
    #: that had already finished.  `settle_record` takes the id up on the next
    #: turn and the messages become addressable.  **A write that is refused is a
    #: hard failure**: the turn's messages are rolled back to this prefix, so a
    #: silent unaddressable run cannot be left behind.  A fork has no id at all
    #: and never rebuilds, so its carried run is never read.  v1 re-derived the
    #: same run by grouping messages into turns; a count is enough here only
    #: because of that rule, and it is what lets a rebuild replace the turns
    #: without destroying a message that has no turn to be replaced by.
    covered: int = 0
    #: The write of a turn whose caller could not wait for it, kept until the
    #: turn that follows can take up the id it answered with — see
    #: `settle_record`.
    #:
    #: **The id is what makes the run backed, and the write is where it is.**  The
    #: write is detached because an interrupted caller cannot await and a
    #: cancellation would cancel an awaited one at its next checkpoint, so by the
    #: time the turn ends it has not answered.  Left unclaimed for good, the
    #: turn's messages would stay in the carried run — and once one turn has left
    #: a run there, the guard below (`covered == before`) is never true again,
    #: because a rebuild re-appends the run at the end of a list `covered` does
    #: not count.  Every later turn would then be unaddressable *and*
    #: recallable: its id never joins `turn_ids`, so `annotate_turn` never writes
    #: a footnote the model can name it by, while the row is in the log for a
    #: query recall to find — and the store would render that turn from its row
    #: and re-append the same messages verbatim, so the exchange appears twice in
    #: the list the turn runs on.
    #:
    #: Set only when the turn began on a wholly backed list (`covered == began`),
    #: because that is what makes its id describe the whole of what follows that
    #: prefix.  A refused write does not leave the run: `settle_record` rolls it
    #: back (a hard failure).
    #:
    #: **The outcome and item travel with the task** so that `settle_record` can
    #: write the turn's footnote in the same step that claims its id
    #: (`PendingRecord` says why the two cannot be separated).
    record_pending: PendingRecord | None = None
    #: The rest of how this conversation reaches its model, resolved once when it
    #: started and kept beside the reference.  Held rather than re-resolved so a
    #: conversation whose model is later edited out of the config still runs: the
    #: backend it talks to is already open, and only a *new* conversation reads
    #: the file.
    provider: str = ""
    api: str = ""
    model_id: str = ""
    accepts_images: bool = True
    #: The `_approve` calls this conversation is holding for an answer, by call
    #: id.  On the loop because a turn is what fills and empties it, and the
    #: answer arrives on a *different* call (`answer_approval`) that must not
    #: wait for the lock the waiting turn is holding — so this is the one piece
    #: of a loop's state that is read and written while its lock is held, and
    #: that is why it is a dict of futures rather than a queue.
    approvals: dict[str, asyncio.Future[bool]] = field(default_factory=dict)

    #: There was a `records` property here — `not self.subagent` — and it is gone
    #: with the thing it was about.  Its reason was written down: *a subagent is a
    #: means to an end inside one turn of its parent's conversation*, so its round
    #: trips were not written, and the file its store named was a schema and
    #: nothing more.  A subagent *owns* its conversation now (SDD §3.5) — it has a
    #: directory, a cabinet and a memory of its own, and `subagent_remove` deletes
    #: them — so every conversation this server holds is recorded, in the store its
    #: own key names.  The worker the old rule was for was never built: nothing in
    #: this tree ever created a conversation with a non-empty `subagent`.
    #:
    #: **A `fork` is the one conversation that is not recorded**, and it is not
    #: the old rule coming back: the old one asked *who is asking*, and this one
    #: asks *what this conversation is made of*.  A fork is made of its maker's
    #: conversation — copied out of it, and then its own — so writing it down
    #: would file one agent's work under another's history and put a second copy
    #: of that history in the store.  Nothing about it is written: no restore,
    #: no rebuild, no turn record (`adopt_maker` is all three).
    #:
    #: **And its session is its whole life.**  The only file a fork leaves is
    #: its `subagent.yaml`, and `forget_forks` removes that when the server
    #: starts: a record with no conversation behind it would name a new fork of
    #: the maker's current conversation, which is a different subagent.
    fork: bool = False


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

    `backend`, `context_client` and `hub_client` are injectable so the whole
    server — model call, tools, the restore, the rebuild, the write — can be
    exercised over the in-memory transport with no network at all.

    `cabinet_client` is the seam for the one peer this server reaches *directly*
    rather than through the hub — the cabinet, whose `dump_save` carries no
    audience mark and therefore has no row for the hub to route
    (`compress_for` says why that is the arrangement).

    `window_client` is the seam for the one thing this server *hands* rather
    than asks: a background call's result, on its way back into the conversation
    it came from (`slife2.libs.harness.handover`, which is where the window-is-the-only-road
    policy is argued).  `slife2.plugins.agent_task.server` takes it for the same reason.

    `mesh_client` is the seam for the other peer reached *directly*, and for a
    removal rather than a tool call: `subagent_remove` tells the subagent network
    that a member is gone (`leave_the_mesh`), because a place on that network
    outlives the directory it was held for.  Opened per call and closed with it —
    once per removed subagent, not once per turn.

    Otherwise a connection is opened **per model server, on first use, and kept
    for the process**.  Not per turn — that would pay a handshake for every
    iteration of every turn — and not once at startup either, because which model
    is wanted is a property of the *loop*: a caller names one when it opens one,
    and
    this server serves every caller.  A model nobody asks for is a connection
    nobody opens.

    The toolhub is the same arrangement with a different lifetime question
    answered: its *connection* is kept for the process, and its *tool list* is
    asked for on every turn.  See `turn_tools` for why those are not the same
    choice.
    """
    #: Cached per model *name*, because the connection behind one is expensive
    #: and a conversation's reference changes rarely and only between its turns.
    #: A `model_switch` therefore needs no cache handling at all: the reference
    #: it writes is the key, so the backend it wants is either open already or
    #: opened by `loop_for` the next time that conversation runs a turn.
    #: `MCPBackend` rather than the protocol, because only this one can carry a
    #: key — an injected backend is a test's, and has no model server to name a
    #: conversation to.
    model_backends: dict[str, MCPBackend] = {}
    clients: dict[str, Client] = {}
    #: The context client once we have one — injected, or opened on first use.
    context_conn: Client | None = context_client
    #: Whether *we* opened it, and so whether we should close it.  An injected
    #: client belongs to whoever made it.
    context_owned = context_client is None
    #: The toolhub client, on the same terms.
    hub_conn: Client | None = hub_client
    hub_owned = hub_client is None
    #: The cabinet, on the same terms — though it is opened on one path only: a
    #: tool result too large for the conversation it was called in.
    cabinet_conn: Client | None = cabinet_client
    #: And whether *we* opened it, which the lifespan needs for the same reason
    #: it needs the other two: this client has no `clients` entry to be swept up
    #: with (it is a peer this server reaches itself, not a model server), so a
    #: connection left in it is one the next session is handed already shut —
    #: `lifespan`'s own comment is where that failure is named.
    cabinet_owned = cabinet_client is None
    #: The peer a background result is *handed* to rather than asked of — the
    #: window (`slife2.libs.harness.handover`).  Injected only by a test: in production it is
    #: opened per hand-over, because that is once per finished call rather than
    #: once per turn.
    window_conn: Client | None = window_client

    def current() -> Config:
        """The config a *new* conversation is built from.

        **Live in production, frozen in tests.**  `main` passes `load_cached`,
        so a model config edit — or a hand edit of `slife2.yaml` — is picked up
        when the next conversation starts, with no restart.  A server built with
        a config injected (every test in `test_server.py`) has no source and
        simply keeps it, which is what lets those tests run with no file on
        disk.

        **Only what a conversation *starts* from is read here**, which is
        deliberate: a conversation's model, its prompt and the backend it talks
        to are properties of the conversation, so an edit of the file does not
        reach one already running.  `model_switch` is the one edit that does,
        and it does not come through here — it moves the loops it is holding,
        because a reference is not a prompt and a conversation can be pointed at
        another model without being rebuilt.

        `for_the_call` is the rule itself, and the half of it that matters here
        is `source()`: `load_cached` answers per home, so a process serving two
        data directories reads the file of whichever one the call came from.
        """
        return for_the_call(source, config)

    #: Every conversation, by key.  One entry per conversation that has been
    #: used, which is what makes isolation a property of the key rather than a
    #: rule somebody has to remember to apply.
    #:
    #: **The key is `client_key`'s triple, and the home in it is load-bearing.**
    #: This process is shared by every home on the machine, so `jack` names two
    #: conversations the moment two homes have an agent by that name — and a
    #: table keyed by the name alone does not answer the second caller with a
    #: stale loop, it answers it with *the other home's history*: its prompt, its
    #: turns, and a `remember` that writes there.
    loops: dict[ConversationKey, Loop] = {}

    #: Serialises the two lazy caches below.  Without it, two turns that start
    #: together both miss and both open a connection, and the loser is
    #: overwritten in `clients` — which makes it unreachable to the lifespan's
    #: cleanup and leaks it along with its task group.  Two loops on one default
    #: model is the ordinary case now, so this is not a hypothetical race.
    opening = asyncio.Lock()

    #: Writes detached from a cancelled turn.  Kept so they are not collected
    #: mid-flight, and drained on the way out.
    background: set[asyncio.Task[None]] = set()

    #: The last or current turn of each conversation, for a watcher that joins
    #: while it is running — see `Tap`.  Replaced at the next turn's start, so
    #: the table holds at most one turn per conversation that has ever run.
    taps: dict[ConversationKey, Tap] = {}

    #: Every open `watch` call, by the conversation it is watching.  **A set**,
    #: because watching is a read and two readers are ordinary: the window is one
    #: client of this server, but nothing makes it the only one.
    watchers: dict[ConversationKey, set[Watcher]] = {}

    def publish(key: ConversationKey, event: TurnEvent) -> None:
        """Offer one turn event to the tap, and to whoever is watching.

        **Synchronous by contract, not by accident.**  It is called from the
        turn's observer, between model calls, and `put_nowait` is the whole of
        the cost a watcher may add: a display that cannot keep up is dropped by
        its own client, never waited for here.

        The tap is appended before the watchers are served, so a watcher that
        registers between the two sees the event in the tap rather than not at
        all — there is no await between the append and the enqueues, and the
        registration is one synchronous step too (`watch`).
        """
        tap = taps.get(key)
        if tap is not None:
            tap.events.append(event)
        for watcher in tuple(watchers.get(key, ())):
            watcher.queue.put_nowait(event)

    def announce(key: ConversationKey, item: WatchItem) -> None:
        """Hand a watcher-only frame to every watcher of a conversation.

        `TurnStarted` and `Closed` are not turn events — one is a fact the turn's
        own caller already has, the other is the end of the read — so they go to
        the watchers directly and never into the tap's event list.
        """
        for watcher in tuple(watchers.get(key, ())):
            watcher.queue.put_nowait(item)

    def start_tap(key: ConversationKey, started: TurnStarted) -> Tap:
        """Open a turn for watchers, and replace the conversation's last one.

        Called immediately before the turn's first observable step
        (`rebuild_into`, which may emit `ContextChosen`), so the tap is the turn
        and not the one before it.  The replacement is what bounds the table: a
        turn's record has been awaited (or taken up by `settle_record`) before
        another turn can start, so the old tap is already in the store.
        """
        tap = Tap(started=started)
        taps[key] = tap
        # A watcher of an idle conversation learns a turn has begun from this,
        # not from its first delta: the message itself is the half of a turn a
        # watcher never sent and cannot otherwise see.
        announce(key, started)
        return tap

    def close_watchers(key: ConversationKey, reason: str) -> None:
        """End every watch of a conversation, with the reason as its last frame.

        **The frame rather than a cancelled call**, because the watcher is owed a
        sentence: a stream that simply stops is indistinguishable from one that
        is quiet between turns, and the two mean opposite things.  The watcher's
        handler sends this and returns, which is what makes the far end's call
        come back normally.
        """
        for watcher in watchers.pop(key, set()):
            watcher.queue.put_nowait(Closed(reason))

    async def memory() -> Client:
        """The client for the context plugin, opened on first use.

        Opened once and kept for the process, the same arrangement as the model
        backends and for the same reason: a handshake per turn is a handshake
        per turn.

        A context plugin that is not there **raises**, like every other peer in
        this system.  `slife2.libs.plugin.protocol.open_server` is where that rule lives
        and why; what matters here is that this plugin is not the exception
        to it — and it is the peer whose absence is felt soonest, since a turn
        whose context cannot be restored is a turn that should not be run.
        """
        nonlocal context_conn
        # Fast path outside the lock: once a loop has been opened there is no
        # await between the check and the use, and the event loop is
        # single-threaded, so reading it unlocked is sound.
        if context_conn is not None:
            return context_conn
        async with opening:
            if context_conn is None:
                context_conn = await open_server(
                    config.server("context").url,
                    name=CONTEXT_SERVER_NAME,
                    fallback_tool="remember",
                    timeout=_T.timeouts.work.context,
                )
            assert context_conn is not None  # open_server returns one or raises
            return context_conn

    async def hub() -> Client:
        """The client for the toolhub, opened on first use.

        **Never optional.**  There used to be a branch here for a config with no
        tools at all, and it was wrong for the reason every plugin's tools are in
        the hub rather than here: the model's tool list has one owner, so a system whose
        toolhub is missing is not a system with no tools — it is a system that
        has come apart, and it fails the turn like any other missing peer.

        A toolhub that is there and whose *upstreams* are not is an entirely
        different thing, and is not an error at all: that is the operator's
        configuration and somebody else's process (`slife2.plugins.toolhub.server`).
        """
        nonlocal hub_conn
        if hub_conn is not None:
            return hub_conn
        async with opening:
            if hub_conn is None:
                hub_conn = await open_server(
                    config.server("toolhub").url,
                    name=TOOLHUB_SERVER_NAME,
                    fallback_tool="list_tools",
                    # **No request deadline on the connection**, and this is the
                    # correction that matters most in this file: `timeout=` here
                    # is a clock on *every* request that travels over the link,
                    # and the number it used to hold (30 s, `list_tools`'
                    # patience) therefore became the deadline for whatever tool
                    # the model happened to call — a listing's number killing a
                    # render.  The asks this process makes of the hub state their
                    # own patience where they are made
                    # (`slife2.clients.toolclient.HUB_TIMEOUT_SECONDS`), and a *tool
                    # call* is bounded by the loop and by nobody else.
                )
            assert hub_conn is not None
            return hub_conn

    async def cabinet() -> Client:
        """The client for the cabinet, opened on first use.

        **A direct peer and not a hub route.**  The two tools this process needs
        sit on opposite sides of the audience line — `dump_save` is the harness's
        and carries no mark, `dump_read` is the model's and does — and the hub
        holds rows only for what the model may see, so the harness reaches the
        plugin itself, exactly as `slife2-context` reaches a model backend.  Both
        tools travel over this one client: one implementation, two callers.
        """
        nonlocal cabinet_conn
        if cabinet_conn is not None:
            return cabinet_conn
        async with opening:
            if cabinet_conn is None:
                cabinet_conn = await open_server(
                    current().server("cabinet").url,
                    name=CABINET_SERVER_NAME,
                    fallback_tool="dump_read",
                )
            return cabinet_conn

    async def attach_sources(
        client_id: ClientId, wanted: list[str]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Resolve what a model named into `(source, url)` pairs, keys included.

        **A `[Image:…]` reference names a cabinet key, and a key is not a path.**
        A source the attachment encoder refuses as a file is asked of the
        cabinet (`image_source`), which answers a `data:` URL for stored bytes
        or the original URL for a remote image.  If the cabinet does not know it
        either, the encoder's own sentence is the refusal — a path that is not
        there still says "no such file" rather than "no such image".  One bad
        source costs only itself; the rest of the batch attaches.

        This is the one place the two routes meet: a person's natural-language
        path goes through the encoder, and a marker's key goes through the
        cabinet, and both end as the same `image_url` part.
        """
        pairs: list[tuple[str, str]] = []
        refusals: list[str] = []
        keys: list[tuple[str, str]] = []
        seen: set[str] = set()
        for source in wanted:
            if source in seen:
                continue
            seen.add(source)
            encoded, refused = attachments.resolve([source])
            if encoded:
                pairs.extend(encoded)
            else:
                # **The encoder's sentence is kept** for the cabinet's failure:
                # a path that is not there says "no such file", and only the
                # cabinet can say whether the same string is one of its keys.
                keys.append((source, refused[0]))
        if not keys:
            return pairs, refusals
        try:
            client = await cabinet()
        except Exception as exc:  # noqa: BLE001 - an unreachable cabinet is each key's refusal
            # **A missing path is not a cabinet failure.**  The lookup is only
            # how a string that failed the encoder is checked against the
            # cabinet's keys, so a cabinet that cannot be reached leaves each
            # such source with the encoder's own sentence — plus why the second
            # check could not be made, because a key that never got its turn is
            # a different fact from a path that is genuinely absent.
            refusals.extend(
                f"{refusal} (the cabinet could not be reached: {exc})"
                for _key, refusal in keys
            )
            return pairs, refusals
        for key, refusal in keys:
            try:
                answer = await client.call_tool(
                    "image_source",
                    {"key": key},
                    meta=client_meta(*client_id),
                    timeout=_T.timeouts.work.hub,
                )
                payload = tool_payload(answer)
                url = payload.get("url") if isinstance(payload, dict) else None
            except Exception:  # noqa: BLE001 - one source's refusal, not the batch's
                refusals.append(refusal)
                continue
            if not isinstance(url, str) or not url:
                refusals.append(refusal)
                continue
            pairs.append((key, url))
        return pairs, refusals

    async def leave_the_mesh(
        agent: str, subagent: str, *, client: Client | None = None
    ) -> None:
        """Tell the subagent network that this member is gone.

        **A removal is one directory and three processes hold something of it.**
        This server holds the conversation (`loops`), the context store holds the
        list it would restore from, and the subagent network holds the member's
        *place*: an MQTT connection subscribed to its topic, answering whoever
        sends to it, with a card on the broker saying it is here.  A directory
        that has been deleted cannot unsubscribe any of that, so the network is
        told — the same act the other two get, in the one vocabulary this system
        has for it (`forget`, which is what `slife2-context`'s tool of the same
        name means and what `slife2.plugins.a2a.server`'s does).

        **Called before anything is taken away, and that is the order.**  A peer
        that is not there is a broken system rather than a degraded one
        (`slife2.libs.plugin.protocol.open_server`), this one is dialled on every
        removal, and a notification that cannot be made must leave the subagent
        exactly as it was — which it does, because by then nothing has been
        deleted.  A machine that configures that section has its plugin running
        (`slife2.libs.host.launcher` starts every one of them), so the only way
        this cannot be reached is the whole stack being wrong.

        A machine with no `subagent:` section has no such network and nothing to
        tell: `configured` is that section's presence, and it is the one case this
        returns without a call.

        `client` is the seam for a test — a recorder answers what the plugin would
        — and an injected client is used as it is and left open.

        **The call's patience is `work.management`**, the registry's number for a
        management call answered out of a plugin's memory: this one reads a table
        and publishes an offline card, and it must not be the call that keeps a
        removal waiting.
        """
        if not current().subagent.network.configured:
            return
        opened = client is None
        try:
            peer = client or await open_server(
                current().server(LOCAL.key).url,
                name=LOCAL.server_name,
                fallback_tool=FORGET,
                timeout=None,
            )
        except Exception as exc:  # noqa: BLE001 - every way this peer can be missing means the same thing here, and the caller is a removal that has not started
            raise ToolError(
                f"{describe((agent, subagent))} was not removed: the subagent "
                f"network could not be reached, so its place on it could not be "
                f"given up ({exc}).  Nothing has been deleted."
            ) from exc
        try:
            await peer.call_tool(
                FORGET,
                {"agent": agent, "subagent": subagent},
                timeout=_T.timeouts.work.management,
            )
        finally:
            if opened:
                # `shield`, for `slife2.libs.harness.handover._peer`'s reason: a
                # cancellation arriving while the close is awaiting would pass
                # straight through it and leave the connection open.
                await asyncio.shield(close_server(peer))

    def compress_for(backend: LLMBackend, client_id: ClientId) -> Compress:
        """How one conversation's tool results are fitted into it.

        **The second discriminator, and the shape of the first** (DESIGN.md §5.4):
        one model call, given the result, the conversation in hand and the
        current input, answering with **line numbers rather than text** — so the
        harness cuts the original, and what enters the conversation is the tool's
        own words or it is nothing.

        **One fallback, for the one step whose failure is expected.**  A model is
        asked for a decision and can answer badly or about something else, so an
        unusable reply is an ordinary event with a designed answer: the reader's
        own first window, which is what a model that had asked for the beginning
        would have got.  Nothing else here falls back — keeping the output whole,
        writing it, reading a window back are steps that must succeed, and a
        fallback for one of them would be a hidden bug rather than a design: a
        conversation carrying an output nobody can read back has given up the
        bound *and* broken the address, and it would look like success.

        The model is the conversation's own, through the backend its turn is
        already using: a selection is judged *under* the conversation, and the
        model that will read it is the one that knows what it is doing.
        """

        async def compress(
            call: ToolCall, text: str, messages: Sequence[Message]
        ) -> str:
            window = current().tool_result.tokens
            if estimate_text_tokens(text) <= window:
                return text
            # **The question comes first, and the dump is written once.**
            # `mime` is what the cabinet's `dump_read` refuses to open a row by
            # (`slife2/plugins/cabinet/server.py`, the `not startswith("text/")`
            # arm), and the one party that can say an output has no lines in it
            # is the model being asked here: a payload is a `str` either way, so
            # nothing else in this process — this server, the loop, the tool that
            # produced it — knows the difference.  Writing the row before the
            # answer would therefore leave it claiming to be readable, and the
            # `binary` window says in the conversation that `dump_read` will not
            # open it — a promise the same turn's row would then break.  The row
            # is written under one key, so a second write to correct it is a
            # second write to keep in step; asking first is what keeps it one.
            decision = await _asked(backend, call, text, messages, window=window)
            client = await cabinet()
            await client.call_tool(
                "dump_save",
                {
                    "call": call.id,
                    "output": text,
                    "tool": call.name,
                    # `NOT_TEXT` rather than a media type, and empty rather than
                    # a guess: the store's own answer for a row nobody has a type
                    # for is `""`, and what the model says when it *is* not text
                    # is the word this module already uses for that judgement.
                    "mime": (
                        dumps.NOT_TEXT
                        if not isinstance(decision, dumps.Rejected) and decision.binary
                        else ""
                    ),
                },
                meta=client_meta(*client_id),
                timeout=_T.timeouts.work.hub,
            )
            if isinstance(decision, dumps.Rejected):
                logger.info("compact_discriminator_unparsed call=%s", call.id)
                return await _first_page(client, call.id, client_id)
            lines = dumps.lines_of(text)
            return dumps.window(
                call=call.id,
                lines=lines,
                kept=dumps.cut(lines, decision, window),
                # Whether the model *chose* nothing, which is an answer, as
                # against choosing lines too long to fit, which is not — the two
                # are told apart because they read differently to the model that
                # gets one.
                chosen=bool(decision.lines),
                binary=decision.binary,
                budget=window,
            )

        return compress

    async def turn_tools(client_id: ClientId) -> list[Tool]:
        """What the model may call, asked of the hub.

        Asked **before every model call**, not once per turn: the turn's tool
        list goes out with each request to the model, so each request asks
        afresh.  One loopback round trip on a path that already exists, and what
        it buys is a list that is live — a tool server that finished starting,
        or grew a tool, is in the next *call*'s list, and nothing here had to
        notice that it happened.

        v1 needed a `tools/list_changed` subscription, a shared catalog and a
        reconcile pass to arrive at the same place.  Here the answer can be at
        most one call old, and nothing has to be kept in step to make that true.

        It is also asked once when the loop is built, before the user's message
        is appended: a hub that is not there is a system that has come apart, and
        the moment to find that out is before the conversation has been touched.

        **The tools come back bound to `client_id`**, so a tool that reads the db
        reads *this* conversation's.  That is the one thing this process knows
        and the model does not, and it is why the binding happens here rather
        than in an argument — see `slife2.clients.toolclient`.
        """
        return await remote_tools(await hub(), client_id)

    async def registry(client_id: ClientId) -> ToolRegistry:
        """What the model may call: the hub's tools, plus the mechanism's own.

        **Two tools are added here rather than served by the hub**, and they are
        the only ones: `check_async` and `cancel_async` are the vocabulary of
        `_async: true`, and the work they describe is this process's — the
        background tasks a turn started live in `slife2.libs.harness.async_tasks`, not in the
        hub, which knows nothing about whose call it was.  They are therefore
        outside the catalogue: the budget cannot unload them and `tool_search`
        cannot find them, which is what a tool the mechanism itself needs
        deserves (v1's system tools sat in every registry the same way).
        """
        return ToolRegistry(await turn_tools(client_id) + async_tools())

    def turn_annotation(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, str | None]:
        """The annotation a rowid-less `turn_summarize` call carries.

        The model annotates the in-flight turn by calling `turn_summarize` with
        no `turn_id`; the tool answers `captured` and writes nothing.  The
        values are applied here, at the one save point, so they land on exactly
        the turn being saved — no "latest row" race, no pending state, and a
        turn whose save fails applies nothing.  A call with an explicit id is
        ignored: the tool wrote that one already.
        """
        summary: str | None = None
        tags: str | None = None
        for message in messages:
            for call in message.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                function = function if isinstance(function, dict) else {}
                if str(function.get("name") or "").split("__")[-1] != "turn_summarize":
                    continue
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(arguments, dict):
                    continue
                if arguments.get("turn_id") is not None:
                    continue
                found = arguments.get("summary")
                if isinstance(found, str) and found.strip():
                    summary = found.strip()
                found = arguments.get("tags")
                if isinstance(found, str) and found.strip():
                    tags = found.strip()
        return summary, tags

    async def remember_turn(
        agent: str,
        subagent: str,
        messages: list[dict],
        result: TurnResult | None,
        *,
        model: str,
        channel: str,
        created_at: str,
        completed_at: str,
    ) -> int | None:
        """Persist a turn, and answer with the id it was given.

        **This is called for every turn of every conversation**, cancelled ones
        included — an agent's own and each of its subagents' — and `result` is
        None exactly when the turn did not finish.  The rule is deliberately not
        "remember to record the cancel path": a write that is conditional on how
        a turn ended is a write somebody can forget to make, and the failure it
        produces is a conversation in the transcript that the database has never
        heard of.

        **A refusal is a failure of the turn, not a `None`.**  A store that is
        gone fails the turn (`slife2.libs.plugin.protocol.open_server` is where a
        missing peer is decided to be a broken system), and so does a store
        that answered and refused *this* request — an agent name that cannot be
        a filename, say — or answered without an id.  The caller rolls the
        unrecorded turn back and raises; there is no silent "recorded nowhere"
        path, which would leave a message the transcript shows and no later turn
        can address.

        Two token counts go over, and they are not interchangeable.  `usage` is
        the turn's total across however many model calls it took — the bill.
        `last_usage` is the final call's own, which is how much conversation
        existed when the turn ended: the number the *next* request would resend.
        A cancelled turn has neither, and reports zero rather than a guess.
        """
        summary, tags = turn_annotation(messages)
        client = await memory()

        payload = tool_payload(
            await client.call_tool(
                "remember",
                {
                    "agent": agent,
                    "subagent": subagent,
                    "messages": messages,
                    "token_count": result.usage.total_tokens if result else 0,
                    "context_tokens": result.last_usage.total_tokens if result else 0,
                    "who_helped": agent,
                    # What the loop was opened on, not the reference as typed:
                    # a caller may name a bare provider or nothing at all, and
                    # neither says which model wrote the answer.
                    "what_model": model,
                    "channel": channel,
                    "summary": summary or "",
                    "tags": tags or "",
                    "created_at": created_at,
                    "completed_at": completed_at,
                },
            )
        )
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, int):
            raise unreadable("the context store", "return a turn id", payload)
        return turn_id

    async def make_loop(
        active: LLMBackend,
        client_id: ClientId,
        max_iterations: int,
        approve: Callable[[ToolCall], Awaitable[bool]] | None = None,
        deliver: Deliver | None = None,
        compressor: Compressor | None = None,
    ) -> AgentLoop:
        return AgentLoop(
            active,
            await registry(client_id),
            # The conversation's own, taken from the config it started on
            # (`Loop.max_iterations`): an iteration cap somebody raises
            # *because* a turn ran into it reaches the next conversation, which
            # is the rule the model follows too.
            max_iterations=max_iterations,
            # Handed to the loop so the list is re-read before each model call;
            # see `turn_tools`.  Bound to the key for the same reason the first
            # call is: every refresh has to hand back tools for *this*
            # conversation, and a bare `registry` would forget whose it was.
            refresh=partial(registry, client_id),
            # None when this turn's caller cannot be asked — see `Pending.watched`
            # and `ask`, which is the other half of this.
            approve=approve,
            # And None when there is no conversation to hand a background
            # call's result back to — see `hand_back`, which is the other half
            # of that.
            deliver=deliver,
            # Built here rather than handed in, because half of what it needs is
            # decided in this function: the backend the turn runs on.  None when
            # the caller did not ask for one, and a result then goes in as it
            # came — see `AgentLoop._fit`.
            compress=None if compressor is None else compressor(active, client_id),
        )

    async def loop_for(
        loop: Loop,
        client_id: ClientId,
        approve: Callable[[ToolCall], Awaitable[bool]] | None = None,
        deliver: Deliver | None = None,
        compressor: Compressor | None = None,
    ) -> AgentLoop:
        """The agent loop for a conversation, bound to that conversation.

        **Keyed by the model reference, and that is a fix rather than a
        detail.**  The `MCPBackend` carries the model id it was built with, so a
        cache keyed by *provider* would hand the second of two conversations on
        one provider the first one's model — silently, because the name a caller
        passes is only looked up when the backend is built.  The reference is
        the loop's own `model`, which is exactly the thing the backend must
        match.

        What is *not* cached is the key.  `with_key` hands back a view of the
        shared backend carrying this conversation's `(agent, subagent)`, so the
        key reaches the model server on every call without splitting the cache
        per conversation and without `loop.py` ever learning that keys exist.
        """
        if backend is not None:
            return await make_loop(
                backend, client_id, loop.max_iterations, approve, deliver, compressor
            )

        reference = loop.model
        async with opening:
            if reference not in model_backends:
                url = current().server(loop.api).url
                if url not in clients:
                    # One client per *server*, not per provider: a server speaks
                    # one wire format for every provider that uses it.
                    client, _ = await open_backend(
                        url,
                        loop.model_id,
                        provider=loop.provider,
                        name=reference,
                        # Checked against the name the protocol's server
                        # advertises, so a URL pointed at the wrong backend is
                        # caught here rather than at the first turn.
                        server_name=API_SERVER_NAMES[loop.api],
                    )
                    clients[url] = client
                model_backends[reference] = MCPBackend(
                    clients[url],
                    loop.model_id,
                    provider=loop.provider,
                    name=reference,
                )
                logger.info("model %s via %s", reference, url)
        return await make_loop(
            model_backends[reference].with_key(*client_id),
            client_id,
            loop.max_iterations,
            approve,
            deliver,
            compressor,
        )

    # --- the registry --------------------------------------------------------

    def opening_messages(
        agent: str, subagent: str, live: Config, spec: SubagentSpec
    ) -> list[Message]:
        """What a conversation starts with: its system prompt, and nothing else.

        Rendered when the conversation starts rather than once at startup,
        because the agent name is a property of the *key* — the server is shared,
        so two agents are two names asking one process — and it is rendered from
        the config a conversation *starts* from, so a prompt edited since boot
        reaches the next one.

        **A subagent is told its own name, its agent's, and where its work comes
        from.**  Each follows from what it is: the name in the prompt is the one
        it answers to (its agent's alone would be a model told it is somebody
        else), the agent's name is whose subagent it is — and with it the member
        it answers — and a subagent has no window at all, not a terminal and not
        a headless one, so what reaches it comes from another member rather than
        from a person at its own keyboard.

        **And it is not told how to answer, and that is the schema's half.**
        What a reply is — the send call, with the task's id for a
        `task_response` — is what the tool's own description says; a prompt that
        repeated the arguments would be a second schema kept in step by hand.
        The prompt's job is the part no tool can state: what this conversation
        is.

        **And it is told what it is, from its own file** (`subagent_spec`): the
        `role` the agent named it with is the one thing from that file the prompt
        uses.  A fork is not told that it is one — the sentence was longer than
        the fact was worth (the designer's call, 2026-10-17).  Its prompt is
        `subagent.j2` and not the config's (`SUBAGENT_PROMPT`), because the two
        say different things.

        **And an agent's own prompt is rendered with whether anybody is
        watching**, read off the claim the client under that name wrote
        (`AgentClaim.headless`).  A prompt is where a model is told who it is
        talking to, and an unattended agent has nobody: a tool call put up for
        approval is granted without being read, so a model that asks is spending
        the turn it was given.  Two ways for nobody to be there, and they are one
        answer: the instance holding the name has no terminal (`slife2
        --headless`), or **no instance is holding it at all** — a peer that asks
        something of an agent nobody is running is told so rather than served
        (`slife2.libs.harness.handover`), and a turn that runs here anyway is
        unattended for the same reason.  A subagent is not one of those cases:
        it is always unattended, so its prompt has no claim to read.
        """
        if subagent:
            system = render_system_prompt(
                SUBAGENT_PROMPT,
                agent_name=subagent,
                agent=agent,
                role=spec.role,
            )
        else:
            claim = read_claim(agent)
            system = render_system_prompt(
                live.agent.system_prompt,
                agent_name=agent,
                headless=claim is None or claim.headless,
            )
        return [Message(role="system", content=system)] if system else []

    def reap() -> None:
        """Drop conversations nothing has asked anything of for a while.

        In-process state only.  Nothing a caller can observe depends on this — a
        key is recreated on the next message that uses it and restores what it
        had — which is the whole reason the sweep is allowed to be this casual.

        **A fork is the exception, and it is not idle state.**  What a restore
        gives every other conversation back, a fork has nowhere else: its own
        turns are the only copy, and letting them go would not reclaim a cache
        but lose the conversation — the next message would start again from the
        maker as it stood then.  So a fork is held for the session, which is the
        whole of its life (`forget_forks` ends it at the next start).

        A conversation with a turn in flight is never idle, whatever its
        timestamp says: its own turn refreshes `last_used` when it finishes.
        """
        cutoff = time.monotonic() - _T.timeouts.pacing.loop_idle
        # The key is a `client_key` triple, so it is *not* unpacked: the words
        # that name a conversation come from the loop (`agent`, `subagent`),
        # which is also what keeps the home out of a log line.
        for key, loop in list(loops.items()):
            if loop.fork:
                continue
            if loop.lock.locked():
                continue
            if loop.last_used < cutoff:
                logger.info(
                    "reaping idle conversation %s",
                    describe((loop.agent, loop.subagent)),
                )
                loops.pop(key, None)
                # The tap goes with it: its turn is in the store (a turn's
                # record is awaited before its lock is released), and a watcher
                # still attached reads what it needs from that store and then
                # from the live queue.  Keeping it would be memory for a
                # conversation nothing is running.
                taps.pop(key, None)

    def conversation(agent: str, subagent: str, model: str) -> Loop:
        """The conversation for this key, created on demand.

        Created on demand rather than opened by a separate call, because a key
        that cannot go stale is worth more than the round trip it costs: there is
        no handle to carry, no lifetime to observe, and no "not found" for a
        caller to handle.  The idle sweep may have dropped the state; the caller
        cannot tell, and does not have to.

        **A pair is not answered here, and the reason is the fix rather than a
        tidying.**  This used to return `(loop, started)`, and the caller used
        `started` to decide whether to restore — which is a fact about *this
        call*, while the restore is a fact about the *loop*: two callers arriving
        on one brand-new key are one starter and one not, and the second one
        would have run a turn on the empty list the restore was about to replace.
        `Loop.restored` is the claim, and it is made under the lock.
        """
        client = client_key(agent, subagent)
        loop = loops.get(client)
        if loop is None:
            live = current()
            # What this subagent's own file says it is — its model, its role,
            # whether it is a fork — read *here*, when its conversation starts,
            # which is what makes a `model_switch` in between reach it the way it
            # reaches the maker's own next turn.  An agent's own conversation has
            # no such file and is the defaults.
            spec = subagent_spec(agent, subagent)
            name, provider, settings = live.resolve(model or spec.model)
            messages = opening_messages(agent, subagent, live, spec)
            loop = Loop(
                agent=agent,
                subagent=subagent,
                model=f"{name}/{settings.model}",
                provider=name,
                api=provider.api,
                model_id=settings.model,
                accepts_images=settings.accepts_images,
                max_iterations=live.agent.max_iterations,
                messages=messages,
                lock=asyncio.Lock(),
                last_used=time.monotonic(),
                # The head is what an empty list covers, so a conversation that
                # restores nothing carries nothing either.
                covered=len(messages),
                fork=spec.fork,
            )
            loops[client] = loop
            logger.debug(
                "started %s on %s",
                describe((agent, subagent)),
                model or spec.model or "the default model",
            )
        loop.last_used = time.monotonic()
        return loop

    def this_home() -> str:
        """The home the call in front of us is for: the first half of a loop key.

        **Read inside a call, which is the only place it is the caller's.**
        `ServeTheCallersHome` (`slife2.libs.plugin.protocol`) has put the caller's
        directory around the whole message, so `client_key` answers with *their*
        home rather than this daemon's — and this process serves every home on the
        machine, so the difference decides whether an edit reaches a conversation
        somebody else is having.

        The empty identity beside it is deliberate: all that is wanted here is the
        home, and it is read from the one function that defines what a loop is
        filed under rather than from `paths.home` beside it — so a key and a
        filter can never disagree about where a conversation belongs.
        """
        return client_key("")[0]

    def detach(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Run a write that must outlive the cancellation that prompted it.

        Measured against the real Streamable HTTP transport: from a cancelled
        handler a plain `await` is cancelled again at its next checkpoint, so a
        record written that way is simply lost.  A task created here is outside
        the cancelled scope and does land.  We cannot wait for it, so its
        failures are logged rather than dropped on the floor.

        **Handed back rather than dropped**, so a caller that *can* wait for it
        later has something to wait on: a turn's record answers with its id, and
        the turn after a failed one is where that id can still be claimed
        (`Loop.record_pending`).
        """
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        background.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            background.discard(finished)
            if not finished.cancelled() and (exc := finished.exception()):
                logger.warning("recording a cancelled turn failed: %s", exc)

        task.add_done_callback(done)
        return task

    def recorded_model(loop: Loop, outcome: Outcome) -> str:
        """What to write in the `what_model` column.

        The model the turn *started* on, except when a backend was injected — in
        which case the thing that ran genuinely is not anything the config
        names.  Both are the same mistake written twice, recording a model that
        never answered, which is why this is one function rather than a
        condition at each call site — and it is the loop's *record* of the turn
        rather than the loop's reference now, because `model_switch` moves the
        reference under a turn that is already running.
        """
        return backend.name if backend is not None else outcome.model or loop.model

    async def keep_images(
        agent: str, subagent: str, messages: list[dict[str, Any]]
    ) -> None:
        """Replace every image part in *messages* with its cabinet reference.

        **The record copies only, and new lists.**  `to_record` shares a
        message's content list with the live `Message`, so replacing a part in
        place would put the marker into the conversation the model is still
        running on.  Each changed message gets a fresh list; the live objects
        are untouched.

        **A cabinet refusal is a failure of the turn.**  It raises, and the
        caller rolls the unrecorded turn back — there is no fallback to the note
        on a turn whose image was meant to be kept.  A *provider* refusal is the
        other road: `repaired_error` has already replaced those parts with the
        note before `record` is reached.

        The pair travels on every call so a subagent's images land in the
        subagent's own cabinet, exactly as its turns land in its own store.
        """
        client: Client | None = None
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            parts: list[dict[str, Any]] = []
            changed = False
            for part in content:
                source = _image_url(part)
                if source is None:
                    parts.append(part)
                    continue
                if client is None:
                    # **Opened only when there is an image to keep.**  A turn
                    # with no pictures must not depend on the cabinet at all.
                    client = await cabinet()
                answer = await client.call_tool(
                    "image_save",
                    {"url": source},
                    meta=client_meta(agent, subagent),
                    timeout=_T.timeouts.work.hub,
                )
                kept = tool_payload(answer)
                key = kept.get("key") if isinstance(kept, dict) else None
                remote = kept.get("source") if isinstance(kept, dict) else None
                if not (isinstance(key, str) and key) and not (
                    isinstance(remote, str) and remote
                ):
                    raise unreadable("the cabinet", "return an image reference", kept)
                parts.append(
                    {
                        "type": "text",
                        "text": attachments.image_reference(
                            key=str(key or ""),
                            source=str(remote or ""),
                            mime=str(kept.get("mime") or ""),
                            size=int(kept.get("size") or 0),
                        ),
                    }
                )
                changed = True
            if changed:
                message["content"] = parts

    async def record(loop: Loop, item: Pending, outcome: Outcome) -> int | None:
        """Write this turn to the log if this loop has any, and answer with its id.

        **Called inside the lock, which reverses an earlier decision.**  It used
        to run after the lock was released, on the argument that a queued turn
        waiting on a network call is a wait with no reason behind it — and that
        argument held while the write was a store's alone.  It does not hold now
        that the *live-context list* is what the write maintains: a rebuild reads
        that list, so a queued turn that started before the write landed would be
        handed a context missing the turn it is following up on, and would drop
        it.  The price is one loopback before a queued turn starts; the thing it
        buys is that `turn_ids` and `messages` are never out of step.

        **Images are kept before the row is written.**  The record copy is
        handed to `keep_images`; a cabinet refusal raises and the caller treats
        the turn as unrecorded, because a row that names an image the cabinet
        does not hold is a marker that can never be attached again.
        """
        if not outcome.messages:
            # A caller cancelled while queued, in which case its turn never
            # started and there is nothing to record.
            return None
        if loop.fork:
            # **A fork is not written down, and the answer it owes is not an
            # id.**  Its turns are its own copy of its maker's conversation plus
            # what it has done since, held in memory and gone with the loop — so
            # there is no row to name and nothing for a caller to point at.
            # `None` is what says so: the caller then leaves this turn's messages
            # unbacked and unannotated, which is what a conversation with no log
            # wants (`annotate_turn` would otherwise write a turn id into the one
            # conversation that can never look one up).
            return None
        messages = [message.to_record() for message in outcome.messages]
        await keep_images(loop.agent, loop.subagent, messages)
        return await remember_turn(
            loop.agent,
            loop.subagent,
            messages,
            outcome.result,
            model=recorded_model(loop, outcome),
            channel=item.channel,
            created_at=outcome.started_at or outcome.completed_at,
            completed_at=outcome.completed_at,
        )

    # --- the two decisions, and the one place they are asked for -------------
    #
    #  Both are the context plugin's, and this server is the caller — it is the
    #  party that owns the conversation, so it is the party that knows when one
    #  has started and when a turn is about to run.  What it does with each
    #  answer is the same: adopt it wholesale, because a context that is half
    #  this server's and half the store's is not a context either of them knows.

    def _adopt(loop: Loop, payload: dict[str, Any], *, carried: int) -> None:
        """Take a rebuilt context as this conversation's, or refuse the answer.

        Refused rather than ignored, and that is the load-bearing half: a peer
        that answers with a shape this build cannot read is a system that has
        come apart — the same rule `slife2.clients.toolclient.remote_tools` applies to a
        tool list — and quietly keeping the context we had would make an
        unreadable answer indistinguishable from a decision to keep it.

        **`turn_ids` is read as strictly as `messages`**, and it did not used to
        be: `payload.get("turn_ids") or []` adopted an answer that carried
        messages and no ids as a list whose turns the loop could not name, so
        the loop's `turn_ids` and the store's disagreed from that point on and
        the next write persisted the wrong list.  A missing or null `turn_ids`
        is the same damage as a non-list `messages` and is refused the same way
        (DESIGN.md §11.1: a value that cannot be read is not a value that is
        absent).
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise unreadable("the context store", "return a message list", payload)
        turn_ids = payload.get("turn_ids")
        if not isinstance(turn_ids, list):
            raise unreadable("the context store", "return a turn-id list", payload)
        loop.messages = [Message.from_wire(raw) for raw in messages]
        loop.turn_ids = [int(turn_id) for turn_id in turn_ids]
        loop.covered = len(loop.messages) - carried

    def unanswer_folded(outcome: Outcome) -> None:
        """Take back what a turn told the messages it folded in.

        A record that is refused rolls the turn's messages back, so an answer
        that was already handed to a parked caller would name a turn the
        conversation no longer holds.  ``None`` sends that caller on to its own
        turn, which is the only honest answer left.
        """
        for folded in outcome.folded:
            folded.result = None
            folded.model = ""

    def roll_back(loop: Loop, outcome: Outcome) -> None:
        """Drop an unrecorded turn's messages, leaving the backed prefix.

        **The tail is the turn's own messages** — `record_pending` is set only
        when the turn began on a wholly backed list, and nothing appends between
        the turn and the settle — but the identity is checked rather than
        assumed, because deleting the wrong slice of a conversation is not a
        mistake a later rebuild can repair.
        """
        count = len(outcome.messages)
        if count == 0:
            return
        tail = loop.messages[-count:]
        if len(tail) != count or any(
            kept is not mine for kept, mine in zip(tail, outcome.messages, strict=True)
        ):
            logger.error(
                "the unrecorded turn is not the tail of %s; not rolling it back",
                describe((loop.agent, loop.subagent)),
            )
            unanswer_folded(outcome)
            return
        del loop.messages[-count:]
        unanswer_folded(outcome)

    async def settle_record(loop: Loop) -> None:
        """Take up the id of a turn whose write the caller could not wait for.

        **The step that turns an unbacked run back into a turn**, and it is here
        — at the start of the next turn, inside the lock — because the record of
        an interrupted turn is detached (`send_message`'s handler says why), so
        its id is only knowable once somebody can wait for that write.  A loop
        that never takes it up is a loop whose `covered` can never advance
        again: the turn's messages stay in the carried run, and the rebuild that
        re-appends that run leaves every later turn's id unclaimed too, since
        the ids have to describe a prefix of `messages` and a carried run in
        front of them is not one.

        Nothing appends to `messages` between the turn and this, so the id
        covers the whole of what follows `covered`.  **A refused write is a hard
        failure**: the turn was not recorded, so its messages are rolled back to
        the backed prefix and any caller it had already answered is un-answered
        (`roll_back`).  That is the same rule `record`'s callers apply when they
        are still there to see the refusal.

        **And it writes the footnote the interrupted turn could not have.**  The
        turn ended before its id existed, so `annotate_turn` had nothing to
        write with; this is the first moment it does, and `address_turn` writes
        the id and the footnote together — the two are one act, and
        `PendingRecord` says why.
        """
        pending = loop.record_pending
        if pending is None:
            return
        try:
            turn_id = await pending.written
        except Exception as exc:  # noqa: BLE001 - a refused write is a hard failure
            logger.warning(
                "the turn of %s was not recorded (%s); rolling it back",
                describe((loop.agent, loop.subagent)),
                exc,
            )
            turn_id = None
        # **Cleared only after the await.**  A cancellation while waiting leaves
        # the field for the next turn; the task itself was created outside this
        # scope, so it still completes.
        loop.record_pending = None
        if turn_id is None:
            roll_back(loop, pending.outcome)
            return
        address_turn(loop, pending.outcome, pending.item, turn_id)
        loop.covered = len(loop.messages)

    async def restore_into(loop: Loop) -> None:
        """Put a conversation back on the context it had when it last stopped.

        Called when a loop is *built* — a process that restarted, a conversation
        the idle sweep let go — which is why `reset` clears the stored list
        rather than only dropping the loop: forgetting a conversation and then
        having it restore itself is not forgetting it.
        """
        payload = tool_payload(
            await (await memory()).call_tool(
                "restore",
                {
                    "agent": loop.agent,
                    "subagent": loop.subagent,
                    "messages": [message.to_wire() for message in loop.messages],
                },
            )
        )
        _adopt(loop, payload, carried=0)
        if loop.turn_ids:
            logger.info(
                "restored %d turn(s) for %s",
                len(loop.turn_ids),
                describe((loop.agent, loop.subagent)),
            )

    async def adopt_maker(loop: Loop) -> None:
        """A fork's context: a copy of its maker's conversation, taken now.

        **This is what a fork starts from instead of a store**, and it is the
        whole of what `fork: true` buys: *share everything with the agent that
        made you*, answered with the one thing a conversation is — the context
        it is made of.  Copied rather than shared, because the two part company
        from here: what the fork does with it is a turn in the fork's
        conversation, and nothing the maker says afterwards reaches it.

        **The maker's conversation is read the way its own next turn would read
        it** — out of memory when it is loaded, and out of the store when it is
        not, which is the same step (`restore_into`) a turn of the maker's takes.
        That is what makes a fork work from anywhere a maker can call from: a
        turn of its own, an agent task working in its name, a scheduled fire.

        **The copy ends at the last exchange that finished** (`settled_messages`),
        and that is not tidiness.  A maker calls `subagent_set` *as* a tool call,
        so the last thing in its list is an assistant message whose call has no
        result yet — and a history that ends that way is the one state every
        provider refuses.  So the fork is handed the conversation as it stood
        when its maker asked for it, which is the reading a person would give
        "what we had said so far" too.

        **And the head is replaced rather than copied.**  The maker's system
        prompt names the maker and says whether anybody is watching; a fork is
        told what it is instead — its own name, its role, and whose subagent it
        is.  `opening_messages` is where those words are, and this is why they
        have to be *words*: a model reading somebody else's conversation under
        its own name with nothing said is a model answering as somebody it is
        not.

        **The maker's list is read under the maker's lock when it still has to be
        restored**, which is the rule `Loop.restored` states and not a precaution.
        `restore_into` replaces the maker's messages wholesale, and the maker's
        own `send_message` may be running that same restore under that same lock
        at this moment — it is the state where the maker's loop is in `loops` and
        not yet restored, which is every fresh conversation and every one the
        idle sweep has just let go.  Two restores of one list is one answer
        landing on top of the other: the fork's, arriving late, would replace a
        list the maker has already appended its user message to — the turn then
        advances the orphaned list while `loop.messages` holds one that never saw
        it, and `covered`/`turn_ids` are computed against a conversation the turn
        is not in.

        **And only when it has to be restored, because the lock is held for a
        whole turn.**  `send_message` takes this lock for the length of its turn —
        not for its restore — so a fork that waited on it unconditionally waited
        out the maker's *entire* turn, which is for ever when the maker is parked
        on `_approve` with nobody answering yet.  That is not a hypothetical: it
        is `test_a_fork_is_taken_from_a_maker_that_is_mid_turn`, which hangs.  A
        maker that is already restored has had the only wholesale writer of its
        list run, so what is there is a conversation and a turn in flight on it is
        not the race this rule is about — the copy ends at the last exchange that
        finished, which is what a fork is for.  The claim is re-read *inside* the
        lock, so a maker that restores while this waits is not restored twice.
        """
        maker = conversation(loop.agent, "", "")
        if maker.restored:
            body = settled_messages(maker.messages)
        else:
            async with maker.lock:
                if not maker.restored:
                    await restore_into(maker)
                    maker.restored = True
                body = settled_messages(maker.messages)
        # Copied on the way out either way, for the reason the copy is a copy at
        # all (below): two loops must not share a message object, and a message
        # the maker's turn rewrites while it is being read here would be copied
        # from one state and stored in another.
        body = [Message.from_wire(message.to_wire()) for message in body]
        if body and body[0].role == "system":
            body = body[1:]
        # Copied, not aliased: the two loops must not share a message object.
        # `repaired_error` drops an attachment by *rewriting* the messages that
        # carry it (`attachments.drop`), so a fork sharing objects with its maker
        # would have its own copy rewritten when the maker's turn failed.  `_adopt`
        # copies for the same reason, the same way — a message list handed to two
        # conversations is a message list with two owners.
        loop.messages = loop.messages + body
        # Nothing in this list is backed by a turn this conversation can name —
        # it has no log — so the carried tail is empty and this is the whole of
        # it.  `rebuild`'s `carried` is the only reader, and a fork never runs a
        # rebuild; the number is kept in step anyway rather than left to say
        # something untrue.
        loop.covered = len(loop.messages)
        logger.info(
            "forked %s from %s: %d message(s)",
            describe((loop.agent, loop.subagent)),
            describe((loop.agent, "")),
            len(loop.messages) - 1,
        )

    async def rebuild_into(loop: Loop, prompt: str) -> ContextChosen:
        """Ask what this turn runs on, take the answer, and report the counts.

        **Before the user's message is appended**, because the answer replaces
        the list and an appended message would be destroyed by it — and the
        message therefore travels as `prompt` rather than being read back out of
        the list, where it is not yet.

        `carried` is what the message list holds beyond the turns in `turn_ids`:
        the **unbacked run at its end** — a cancelled turn's repair, a failed
        turn, a turn the store would not record — and it is exactly
        `len(messages) - covered` because `covered` counts a prefix (`Loop.covered`
        states the invariant).  The store puts those messages back verbatim, so a
        rebuild replaces the *turns* without destroying something that has no turn
        to be replaced by.

        Returns the counts as the event that reports them, because the
        discriminator is the most expensive thing a turn does and the two
        numbers are the only sight of it anybody gets — how much of what was in
        hand it kept, and how much it went back to the log for.  Handing back the
        event rather than the counts keeps the vocabulary in one module: this
        knows *what happened*, `slife2.libs.turn.events` knows how it is said.

        **An answer that carried no counts is refused**, like a shape `_adopt`
        cannot read: a reply that replaced the context list but omitted `kept`
        and `recalled` used to draw `[kept 0, recalled 0]`, a note reporting a
        decision nobody made (DESIGN.md §11.1: a failure is not a verdict).
        """
        carried = max(len(loop.messages) - loop.covered, 0)
        payload = tool_payload(
            await (await memory()).call_tool(
                "rebuild",
                {
                    "agent": loop.agent,
                    "subagent": loop.subagent,
                    "messages": [message.to_wire() for message in loop.messages],
                    "turn_ids": list(loop.turn_ids),
                    "prompt": prompt,
                    "model": loop.model,
                    "carried": carried,
                },
            )
        )
        _adopt(loop, payload, carried=carried)
        kept, recalled = payload.get("kept"), payload.get("recalled")
        if kept is None or recalled is None:
            raise unreadable(
                "the context store", "report the rebuild's counts", payload
            )
        return ContextChosen(kept=int(kept), recalled=int(recalled))

    async def forget_context(agent: str, subagent: str) -> None:
        """Clear a conversation's stored live context.

        A store that is gone **fails the reset**, which is this system's rule and
        not an exception made for tidiness: a reset that quietly did not happen
        is a conversation that comes back on the next message, which is the one
        outcome the caller was trying to prevent.
        """
        await (await memory()).call_tool(
            "forget", {"agent": agent, "subagent": subagent}
        )

    async def trim_tools(loop: Loop) -> None:
        """Bring the model's tool list back within its budget — and say so in the
        turn, rather than doing it behind the model's back.

        **A turn boundary, and the harness's own call.**  The tools a model has
        loaded go out with every request, so a turn that loaded several has left
        the list longer than the config allows — and it is trimmed here rather
        than by the gate because this is the moment nothing is in flight: the
        list is rebuilt before every *model call*, so dropping the excess
        mid-turn would take away a tool the model had just loaded and was about
        to use.  The count and the threshold are the catalogue's (`evict`); what
        this side decides is *when* to ask, which is the half a provider cannot
        do for us.

        **What went is written into the conversation.**  The trim is recorded as
        a tool pair — `_func_tool_unload` called with no names — so the model
        reads that its list shrank and which tools left, instead of reaching for
        a tool it still believes it has.  That is v1's *harness tool-pair*
        (DESIGN.md §4.3 there), and it is why the tool is in the model's list at
        all: v1's rule is that such a pair has to name a **declared** tool,
        because the Responses and Messages backends reject a tool call in
        history that is not in the request's tool list.  A pair invented in the
        history layer would be exactly that.

        Nothing is written when nothing moved: no tools unloaded, no pair.  An
        empty pair every turn would be a record of something that did not
        happen, and a model reading it would learn to distrust the mechanism.

        Best-effort by construction, and registered as a fallback in SDD Appendix C.  It
        *skips the trim* when the hub cannot be reached or answers with a shape
        this build cannot read — the turn is over and has been answered, and
        bookkeeping that runs after it must not turn a good turn into a failed
        one.  It *gives up* the trim for that turn, so the loaded set stays over
        budget.  It *does not cover* the next turn, whose tool list is asked for
        before the conversation is touched and fails loudly on a hub that is
        really gone; and because both halves are read before either is appended,
        it never leaves a half-written pair.  The *failure goes* to the log as a
        warning naming the conversation.
        """
        try:
            found = await unload_tools(await hub())
            unloaded, text = trim_answer(found)
        except Exception as exc:  # noqa: BLE001 — bookkeeping does not fail a turn
            logger.warning(
                "%s: the tool list was not trimmed: %s",
                describe((loop.agent, loop.subagent)),
                exc,
            )
            return
        if not unloaded:
            return
        logger.info(
            "%s: %d tool(s) unloaded to stay within the budget: %s",
            describe((loop.agent, loop.subagent)),
            len(unloaded),
            ", ".join(unloaded),
        )
        call = harness_call(FUNC_TOOL_UNLOAD)
        # **Both halves with nothing awaited between them.**  An interruption in
        # that gap leaves an assistant message whose call is never answered, and
        # that is the one history state every provider rejects — the same reason
        # the caller refuses to start a trim on a cancelled turn.  Two appends
        # with no suspension point between them cannot be split.  (`harness_call`
        # is the one builder of these pairs: the loop writes the cut-in's the same
        # way, and two writers inventing ids would eventually invent one twice.)
        loop.messages.append(Message(role="assistant", content=None, tool_calls=[call]))
        loop.messages.append(
            Message(
                role="tool",
                content=text,
                tool_call_id=call.id,
            )
        )

    def take_waiting(client_id: ClientId) -> str:
        """The first message waiting for one conversation, taken for delivery.

        **The delivery itself, and the reason `_check_new_input` is a real tool
        rather than a stub beside a fabricated pair.**  What the harness writes
        into the conversation is this call's answer, exactly as `trim_tools`
        writes `_func_tool_unload`'s — so the tool does the work, and the pair
        cannot say something the tool would not say.

        **The model can reach this too, and that is not the design's intent —
        only its constraint.**  The tool has to be declared (a pair names it),
        being declared puts it in the model's list, and a call from there cannot
        be refused: a provider hands us the call and the only answer available is
        this one.  So the same function serves both, and what makes that
        acceptable is that the model calling it is the boundary's own move made
        early rather than something new — the message would have arrived at the
        next iteration regardless.  A tool that could not say that could not be a
        harness tool at all (`slife2.libs.turn.tools`).

        **The entry is claimed before it is returned**, into `loop.injected`, so
        that the turn this arrives in is the turn that answers for it: the
        caller parked on the lock reads its own entry's `result` and stops
        waiting.  Taken in arrival order — the queue's first is the message that
        has waited longest.

        Returns `""` for nothing to deliver: no such conversation, an empty
        queue, or a head that carries images.  That last one is the single
        refusal, and it is `auto_invoke`'s too — a tool result carries text, so
        delivering one would drop what somebody attached, and the message keeps
        its place and its own turn instead.
        """
        loop = loops.get(client_key(*client_id))
        if loop is None or not loop.inbox:
            return ""
        waiting = loop.inbox[0]
        if waiting.images:
            return ""
        loop.inbox.popleft()
        loop.injected.append(waiting)
        return waiting.prompt

    def address_turn(loop: Loop, outcome: Outcome, item: Pending, turn_id: int) -> None:
        """Make one recorded turn addressable — its id and its footnote as one act.

        **They are one act, and separating them fails in both directions.**  An
        id in `turn_ids` that no message carries is a turn the discriminator
        cannot name: the ids it writes in a keep-list are the ones it read, and
        it reads them in `[TURN:…]`.  A footnote whose id is not in `turn_ids`
        is worse: the keep-list contains it, and `rebuild`'s intersection drops
        it as an id it cannot see (`slife2.plugins.context.server.rebuild`).
        The success path and `settle_record` are the two moments a turn becomes
        addressable, and this is the one place both halves are written so they
        cannot drift apart.
        """
        loop.turn_ids.append(turn_id)
        annotate_turn(outcome, item, turn_id)

    def annotate_turn(outcome: Outcome, item: Pending, turn_id: int) -> None:
        """Write a turn's id and span onto the message that opened it.

        **The footnote is how a turn id reaches the model at all**, and
        therefore how a keep-list is expressible: the ids a model writes back
        are the ids it read.  `messages_from_turns` puts one on every turn a
        *rebuild* builds, which covers everything that came out of the store —
        and this is the other half, and v1's: the turn that has just run is in
        memory and in no rebuilt list yet, so without this the newest turns are
        the only ones the model cannot name, and a keep-list silently drops
        exactly those.

        Called **after** the save, and never written into it.  The row holds the
        user's own words and the footnote is derived from the row when a list is
        built; storing it as well would put two of them in one message on the
        next rebuild.  That is also why the timestamps are the ones the row was
        written with — the two spellings have to agree to the character.

        The message is `outcome.messages[0]`, which is the *same object* the
        loop is holding: the snapshot is a shallow copy, so there is no index to
        carry and the live list is annotated by annotating this.  A fork's turn
        never reaches here: it is written to no log, so no id is claimed for it
        and there is nothing to annotate it with.
        """
        opening = outcome.messages[0] if outcome.messages else None
        if opening is None or opening.role != "user":
            return
        opening.content = with_note(
            opening.content,
            turn_note(
                turn_id,
                outcome.started_at,
                outcome.completed_at,
                # The channel the row is written with, from the same field — a
                # footnote that disagreed with the record would be the one thing
                # the model has no way to check.
                item.channel,
            ),
        )

    async def run_turn_into(
        loop: Loop, item: Pending, observer: TurnObserver, outcome: Outcome
    ) -> None:
        """One turn, and the repair a cancellation needs.

        `outcome` is filled in a `finally`, *after* the repair, so it always
        holds what this turn is answerable for: the whole exchange, or the
        user's message alone when the turn was cut off.

        **The trim happens here, inside the lock, and no longer after it.**  The
        pair it records has to be part of *this* turn — the saved record and the
        live history are one list until the snapshot is taken — and it has to be
        written before the next queued turn can append anything after it.  The
        price is that a queued turn waits for one loopback to the hub; the thing
        it buys is a transcript in which a tool the model lost is something it
        read in the turn that took it away, rather than a gap somebody has to
        explain.

        **A cancellation that lands in that trim still finds the turn settled.**
        The trim is post-turn work — it only runs once the turn has answered —
        and it suspends on a real hub call, so a client whose stream closes
        during it cancels something nothing is waiting for any more.  The commit
        below therefore sits in a `finally` of its own, so it runs even as that
        cancellation unwinds past it; otherwise the caller's handler would find
        an empty `outcome.messages`, record nothing, and re-raise — a completed
        turn with no row, and the loop's `covered`/`turn_ids` out of step with a
        list the model is still sent.  The `finally` comments say the rest.
        """

        def auto_invoke() -> AutoCall | None:
            """The tool to call at this iteration boundary, or `None` — v1's cut-in.

            **Cheap, and it decides only *whether*.**  The name it returns is
            handed to the loop, which makes the call through the same registry
            the model's calls go through, so this half never touches a tool and
            never builds a message.  What it answers is one question — has
            anything arrived for this conversation — and the answer is a queue
            that is almost always empty.

            **The head's channel and receipt go with the name** because the loop
            is what writes the pair and raises the event, and neither field is
            recoverable later: the channel is the line a rebuilt transcript
            needs, and the receipt is how the window that submitted the message
            stops waiting for a turn of its own.  Nothing is popped here, so a
            boundary that delivers nothing leaves no pair behind.

            **A message with images is not folded in**, and this is the one
            refusal: a tool result carries text, so delivering one would drop
            what somebody attached, and this system announces what it cannot keep
            rather than keeping it silently.  The message keeps its place and
            runs its own turn, which costs the messages behind it one wait and
            loses nobody anything.  `take_waiting` refuses the same way, so the
            two agree about what is deliverable — this one without popping, so
            that a boundary which delivers nothing leaves no pair behind.
            """
            if not loop.inbox:
                return None
            waiting = loop.inbox[0]
            if waiting.images:
                return None
            return AutoCall(
                CHECK_NEW_INPUT,
                channel=waiting.channel,
                receipt=waiting.receipt,
            )

        snapshot = len(loop.messages)
        outcome.started_at = now()
        # **The model is taken here, and that is a turn boundary.**  `loop_for`
        # below resolves the backend once for this turn, so this is what will
        # answer it; a `model_switch` arriving mid-turn moves the conversation
        # for the next one and must not rewrite what this one's record says.
        outcome.model = loop.model
        # **The caller's own sources are resolved here**, before the turn: the
        # message is built with its parts rather than having them injected later,
        # because there is no tool call to inject them — this is the route for a
        # caller that holds bytes, and the model's route is `attach_image`.
        # Resolved through the same encoder for the reason that tool exists, and
        # gated on the same question: a model whose config lists only `text` gets
        # the text and a refusal, not parts its provider would reject.
        pairs, refusals = attachments.resolve(item.images)
        if pairs and not loop.accepts_images:
            # **Extended, not replaced**: a caller who passed one unreadable
            # source and one the model cannot see is owed both reasons, and a
            # list that dropped the first to say the second would answer the
            # wrong question.
            refusals.extend(
                f"{source}: {loop.model} cannot read images" for source, _url in pairs
            )
            pairs = []
        # **On the outcome, so the caller is finally told.**  This is the route a
        # caller with bytes of its own takes, and a call whose pictures were fewer
        # than it asked for cannot tell — the reply's `refused` is the only place
        # that says so.  (The model's own route reports through `attach_image`'s
        # answer, which is the model's to read.)
        outcome.refused = refusals
        user = (
            attachments.parts(item.prompt, [url for _source, url in pairs])
            if pairs
            else item.prompt
        )
        for refusal in refusals:
            # **Reported, never dropped in silence**: to the caller in the reply
            # below, because a call whose pictures were fewer than it asked for
            # cannot tell.  (The model's own route reports through the tool's
            # answer, which is the model's to read.)
            logger.warning("attachment not attached: %s", refusal)

        async def ask(call: ToolCall) -> bool:
            """Hand one `_approve` request to whoever is watching, and wait.

            **The wait is on a future in the loop's own `approvals`**, because
            the answer does not come back on this call: the observer's stream
            runs one way, so the watching client answers with a call of its own
            (`answer_approval`).  That is also why the entry is dropped in a
            `finally` — a cancelled turn, an answered prompt and a client that
            died all leave through here, and an entry left behind is a future
            nobody will ever resolve.

            Emitting the event *before* awaiting is the whole of the handshake:
            a client that never hears the request cannot answer it, and the case
            that matters is the client that hears it and does not answer — which
            is what the turn's own cancellation is for, and why this method does
            not invent a bound of its own.
            """
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            loop.approvals[call.id] = future
            try:
                # **Emitted through the loop's own fault tolerance, not straight
                # to the observer.**  `AgentLoop._emit` exists so that a UI which
                # throws cannot end a turn, and this event is raised from inside
                # a turn: a client whose progress stream is broken throws from
                # `on_event` (`ctx.report_progress` on a closed stream), and
                # letting that escape would kill the very turn that is waiting for
                # this answer — a tool call the model is entitled to make, refused
                # because a *display* failed, where the same observer failing on a
                # `TextDelta` would have been swallowed one layer down.  It is a
                # staticmethod, so the class is asked rather than an instance: this
                # closure is built before `agent_loop` and must not depend on it.
                await AgentLoop._emit(
                    observer,
                    ToolApprovalRequested(call.id, call.name, call.arguments),
                )
                return await future
            finally:
                loop.approvals.pop(call.id, None)

        async def deliver(text: str, channel: str) -> bool:
            """Hand a finished background call's result back to this conversation.

            **A result nobody comes back for is a result lost**, which is the one
            thing backgrounding must not do: the call was made for this
            conversation, and v1's only road back was the model remembering to
            poll.  So it is handed to the window like every other piece of work
            this system does in the background (`slife2.libs.harness.handover`).

            **A failure here is `False`, and `False` is not "lost"**: the call
            keeps its result, `check_async` is what hands it over, and the model
            that asks for it is the model that has a window.  What is *not* done
            is running the delivery into a conversation nobody is watching —
            that road used to exist and it is exactly the "carry on without the
            window" this system dropped.

            It goes to the **agent's** conversation and not the subagent's,
            because `send_message` addresses a conversation by `(agent,
            subagent)` and hand-over names only the agent: a worker's background
            call is reported where the person is, which is the only place a
            result can be read by somebody.

            **The channel is the caller's**, not this function's: which tool the
            call was is `Task.tool`'s fact and lives in the registry that ran it
            (`slife2.libs.harness.async_tasks.channel_for`), and this end knows only whose
            conversation the result is going into.
            """
            try:
                await hand_over(
                    current(),
                    loop.agent,
                    text,
                    channel,
                    what=(
                        f"the result of a background call in "
                        f"{describe((loop.agent, loop.subagent))}"
                    ),
                    window_client=window_conn,
                )
            except DeliveryFailed as exc:
                logger.warning(
                    "a background call's result was not delivered to %s: %s",
                    describe((loop.agent, loop.subagent)),
                    exc,
                )
                return False
            return True

        agent_loop = await loop_for(
            loop,
            (loop.agent, loop.subagent),
            # Only a caller that said it can be asked is asked (see
            # `Pending.watched`).  With None the loop grants the request and the
            # call runs, which is v1's rule for a turn with nobody behind it.
            ask if item.watched else None,
            deliver,
            # Every turn's results are fitted, not only the ones a window has
            # anything to say about: the callback's first act is to measure, and
            # a result that fits costs a comparison rather than a call.
            compress_for,
        )
        cancelled = False
        try:
            # The parts, built above and after the rebuild — the rebuild
            # replaces the list wholesale, so a message appended before it would
            # be destroyed, which is why `run_turn` appends this one itself.
            outcome.result = await agent_loop.run_turn(
                loop.messages, user, observer, auto=auto_invoke
            )
        except asyncio.CancelledError:
            # **The interruption is repaired in the loop.**  A batch the
            # cancellation landed in is completed before the exception leaves
            # (`AgentLoop.run_turn`): the calls that returned keep their answers
            # and the rest say they were interrupted.  So the list here is
            # already the well-formed thing to keep — and keeping it is what
            # makes a follow-up "continue" possible — where truncating back to
            # the user's message would throw away work nothing else can
            # reconstruct.
            #
            # `cancelled` still skips the trim below: a cancelled handler cannot
            # await the hub, and a trim is bookkeeping for a turn that ran to
            # its end.
            cancelled = True
            raise
        finally:
            # **The trim is wrapped, and the wrapping is the fix rather than
            # tidiness.**  It is a *post-turn* step, so it only runs when the
            # turn answered — but it suspends on a real call to the hub
            # (`unload_tools`), and a cancellation that lands in that suspension
            # is a cancellation of something the caller is no longer waiting for;
            # the turn is already done.  Left to unwind freely it would skip the
            # commit below, and `send_message`'s handler would then find an empty
            # `outcome.messages`, record nothing and re-raise — a completed turn
            # with no row, which is the "conversation the database has never
            # heard of" DESIGN.md §2.5's rule exists to prevent.  So the commit is put in a
            # `finally` of its own: it holds no `await`, so it completes even as a
            # cancellation unwinds past it (the property
            # `slife2.plugins.agent_task.server`'s cancelled path names), and the turn is
            # settled however the trim ended.
            #
            # Skipped entirely on the cancelled path above, and not merely to
            # save a call: that `finally` is running *during* a cancellation,
            # where the next await is cancelled at its first checkpoint — so a
            # pair could be started and never finished, which is the one history
            # state that is a 400 from every provider.  v1's `_auto_invoke`
            # refuses to start for the same reason.  The turn is over; the budget
            # can wait a turn.
            try:
                if not cancelled:
                    await trim_tools(loop)
            finally:
                # **What a folded message is owed, settled where the turn
                # ends.**  A turn that produced a result has answered for
                # everything it was handed, so each entry it took is given that
                # answer and the caller waiting on it stops waiting.  A turn that
                # produced *none* — the cancelled path above, or any failure —
                # has answered nothing: the entries keep `result` as `None`, and
                # their callers, who are already parked on the lock holding these
                # same objects, run their own turns.
                #
                # The list is one turn's and is emptied either way.  A later
                # turn answering for a message it never saw is the failure that
                # would come of leaving it.
                #
                # **A copy is kept on the outcome** so a record that is refused
                # can un-answer them: the turn's messages are rolled back, and
                # a caller still parked on the lock must run its own turn rather
                # than be handed an answer that is no longer in the conversation.
                outcome.folded = list(loop.injected)
                if outcome.result is not None:
                    for folded in loop.injected:
                        folded.result = outcome.result
                        # The absorbing turn's own record of what answered, and
                        # the model its folded callers are told — taken here
                        # because it is what `_reply` needs and because the loop
                        # may be moved onto another model before they read it.
                        folded.model = outcome.model
                loop.injected.clear()
                # **After the trim**, so its pair — written into the turn it
                # followed — rides this turn's record.  It is a cancelled trim
                # that reaches here too, when it appended nothing (its two
                # appends have no await between them, so neither can be split
                # from the other); then this snapshot is the whole completed
                # turn, which is exactly what the record needs.
                outcome.messages = list(loop.messages[snapshot:])
                outcome.completed_at = now()

    def repaired_error(loop: Loop, exc: BaseException) -> BaseException:
        """What a turn that died at the provider leaves behind, and what it says.

        **A failed turn stays in the conversation.**  That is `remember_turn`'s
        rule — a message somebody typed must not vanish because a provider
        answered 500 — and it is not what this changes.  What it changes is the
        one part of a failed turn that can make every *later* turn fail the same
        way: an attached image the provider refuses is carried by every request
        that follows, because a failed turn's messages are left unbacked and ride
        the next rebuild's carried tail verbatim.  There is then no way forward
        at all — not a retry, not a new terminal, because the loop lives in this
        process — and that is the state this repairs.

        **The pixels go and the text stays**, which is what a restore would
        already have left: storage never keeps an image (`db.strip_images`), so
        the live list is brought to the shape the conversation comes back in, and
        the note left where the picture was still names its size.  What makes
        that affordable is that no pixel is the whole of anything: the sentence
        that named the file is still there, and a caller that held bytes still
        holds them — so sending it again costs one sentence or one call.

        **Returned rather than raised** so `send_message` keeps one `raise` and
        the original exception stays the chain's cause: the provider's own words
        are the diagnosis, and a repair that swallowed them would leave a person
        with a note about images and no reason the turn failed.

        **A cancellation is not this.**  That path keeps the user's message *for*
        the retry — the `+ 1` in `run_turn_into` is the same decision — and its
        picture is what the retry is for, so it is returned untouched.  The test
        is `Exception` rather than `CancelledError` for the same reason and one
        more: an exit is not a failure either, and both derive straight from
        `BaseException` (`slife2.libs.turn.loop` relies on that for the same purpose).

        **Every other failure drops**, not only a provider's own error — a tool
        hub that could not be reached would take the picture with it, and the
        picture was never the problem.  The trade is deliberate and it is the
        cheap one: what is lost is one attachment the person can send again in a
        sentence, where the alternative is a conversation that cannot run at all
        until somebody is told to kill the server.  The repairs that could tell
        the difference are worth making the day a transient failure can be shown
        to cost somebody their picture.
        """
        if not isinstance(exc, Exception):
            return exc
        dropped = attachments.drop(loop.messages)
        if not dropped:
            return exc
        logger.warning(
            "turn failed and %d attached image(s) were dropped for %s: %s",
            dropped,
            loop.agent,
            exc,
        )
        return ToolError(f"{exc}\n{attachments.DROPPED_IMAGES}")

    #: Whether this process has already cleared the forks the previous session
    #: left behind.  **Once per server, not once per lifespan**: over the
    #: in-memory transport FastMCP runs the lifespan once per client session, and
    #: the loops deliberately outlive those sessions — a sweep on every one
    #: would delete a fork another client had just made.
    session_started = False

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        nonlocal context_conn, hub_conn, cabinet_conn, session_started
        if not session_started:
            session_started = True
            # **The start of a session is the end of the last one's forks.**
            # A fork's conversation lives in memory and nothing about it is
            # written down, so the record a restart finds is the only part of it
            # left — and a record with no conversation behind it is a name that
            # would answer as a new fork of wherever its maker stands now.
            # `forget_forks` says why that is a different subagent and not the
            # same one resumed, and why the sweep is every home this process
            # serves rather than its own.
            forget_forks()
        try:
            yield {}
        finally:
            # A detached write exists because a turn was cancelled; letting the
            # process exit on top of it would lose the record it was written
            # for.  Bounded, because a store that has stopped answering must not
            # hold up the shutdown.
            if background:
                await asyncio.wait(list(background), timeout=_T.timeouts.grace.drain)
            for client in clients.values():
                await close_server(client)
            clients.clear()
            model_backends.clear()
            # Closed *and* forgotten, which is what `clients.clear()` above is
            # for as well: this hook is not the process's lifetime over the
            # in-memory transport, so a client left in place here is one the
            # next session is handed already shut — every turn of it failing at
            # its first hop.  Clearing is what lets `memory()` and `hub()` open
            # again.
            if context_owned and context_conn is not None:
                await close_server(context_conn)
                context_conn = None
            if hub_owned and hub_conn is not None:
                await close_server(hub_conn)
                hub_conn = None
            # The third client this server opens for itself, and it is one for
            # the same rule and not one more than for symmetry: `cabinet()` is
            # reached only by a turn with a result past `tool_result.tokens` —
            # and a session that ran one of those used to end with the cabinet
            # still open and still held, so the *next* session's oversized result
            # called `dump_save` on a shut client.  `compress_for` deliberately
            # does not catch a failed write ("nothing else here falls back"), so
            # that was not a lost dump: it was every such turn failing at its
            # first hop.
            if cabinet_owned and cabinet_conn is not None:
                await close_server(cabinet_conn)
                cabinet_conn = None

            # `loops` is deliberately **not** cleared here.  It is state the
            # process owns, and this hook is not the process's lifetime: over
            # the in-memory transport FastMCP runs the lifespan once per client
            # session, so clearing here would empty the registry every time a
            # caller disconnected — which is precisely what a loop is supposed
            # to survive.  The dictionary goes away with the process, which is
            # the only lifetime it has.

    mcp: FastMCP = house_server(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    @mcp.tool(name=ATTACH_IMAGE, meta=FOR_THE_MODEL)
    async def attach_image(ctx: Context, sources: list[str]) -> dict[str, Any]:
        """Add the image parts a vision model reads to the request.

        Call this when a message names a picture — a path on this machine, a
        URL, a `data:` URI — or when the conversation carries an `[Image:…]`
        reference naming a `key` or `source`; pass that value as a source and
        the cabinet resolves it.

        Args:
            sources: Absolute paths on this machine, `http(s)` URLs that serve
                image bytes, `data:` URIs, or a cabinet key from an `[Image:…]`
                reference — as many as you need in one call.

        Returns:
            `attached` — the sources the model can now see, named exactly as
            this call named them — and `refused`, one entry per source that
            could not be read, each naming the source and the reason.
        """
        client_id = request_client(ctx)
        if client_id is None or (loop := loops.get(client_key(*client_id))) is None:
            raise ToolError(
                "this call is not inside a running turn, so there is no request "
                "to attach an image to."
            )
        wanted = [str(source) for source in sources if str(source).strip()]
        if not wanted:
            raise ToolError(
                "this call named no source; pass the paths, URLs or `data:` "
                "URIs of the images to attach."
            )

        if not loop.accepts_images:
            # v1 refused to encode rather than letting a provider drop them, and
            # this is the better half of its version: the refusal is the tool
            # result the model reads, so it can tell the person — where v1 had
            # the harness write an assistant message on the model's behalf.
            raise ToolError(
                f"{loop.model} cannot read images, so nothing was attached — "
                f"move to a model whose config lists `image` under `input`."
            )

        pairs, refusals = await attach_sources(client_id, wanted)
        if pairs and not attachments.inject(
            loop.messages, [url for _source, url in pairs]
        ):
            raise ToolError("there is no message here to attach an image to.")
        logger.info(
            "attach_image %s: %d attached, %d refused",
            client_id,
            len(pairs),
            len(refusals),
        )
        # **A refusal is about one source, and it is answered as a value.**  The
        # batch rule is the module's; this is the half that hands it to the
        # model.  One unreadable source among five still attaches the other
        # four, and a call where every source failed answers the shape a call
        # where none did answers — because the fault is the file's, and a
        # refusal about the *call* would throw away the sentence naming which
        # file it was (the mistake this used to make: "there is no message to
        # attach it to", said of a path that is not there).
        return {
            "attached": [source for source, _url in pairs],
            "refused": refusals,
        }

    @mcp.tool(name=CHECK_NEW_INPUT, meta=FOR_THE_MODEL)
    async def check_new_input(ctx: Context) -> str:
        """The message that arrived while the turn was running, verbatim.

        The harness calls this at a turn's next iteration boundary and writes the
        call and its result into the conversation, so a message that arrives
        mid-turn reaches the turn it arrived in. It hands over the first message
        waiting for this conversation.
        """
        client_id = request_client(ctx)
        if not client_id:
            return NOTHING_WAITING
        # **`or`, because `""` is not an answer here.**  `take_waiting` returns
        # the empty string for the one thing it refuses — a head that carries
        # images, which cannot ride a tool result — and *this* is a tool result:
        # a blank one reads as a failure rather than as "nothing was handed
        # over", which is the whole reason `NOTHING_WAITING` is a sentence.  Its
        # own comment has said it covers that case from the beginning; the code
        # did not.
        return take_waiting(client_id) or NOTHING_WAITING

    @mcp.tool
    async def answer_approval(
        agent: str, call_id: str, approved: bool, subagent: str = ""
    ) -> dict[str, Any]:
        """Answer one `_approve` request, and say whether anything was waiting.

        **This server's own surface, not a tool** — it sits beside
        `send_message`, `reset` and `transcript`, and the model never reaches
        this process at all: a model reads a prompt and writes arguments, and
        the thing that answers a person's question is the person.  Whoever draws
        the prompt is therefore the one caller it has (`slife2.tui.client`).

        **A separate call, because the turn's channel runs one way.**  The turn
        that is waiting already told whoever is watching *what* it is waiting for
        (`ToolApprovalRequested`, on the progress stream), and the answer cannot
        travel back up that stream — a progress notification is a report, not a
        question.  So it arrives here instead, on the same server and **not
        behind the conversation's lock**: that lock is held by the very turn
        that is waiting, and a prompt whose answer had to wait for the turn to
        end would be a prompt that could never be answered.

        **Nothing waiting is an answer and not an error.**  A prompt that was
        drawn and then overtaken — the turn was cancelled, or somebody answered
        twice — leaves this call with no future to resolve.  That is a race
        rather than a broken client, so it is reported (`answered: false`) rather
        than raised, and the client that lost the race says so and stops.

        Args:
            agent: The conversation the waiting turn belongs to.
            call_id: The id from the `ToolApprovalRequested` that was drawn.
            approved: True to let the call run, False to refuse it.
            subagent: The other half of the conversation's key.

        Returns:
            `answered` — whether a waiting turn took this answer — and `text`,
            which is what to tell the reader when it did not.
        """
        loop = loops.get(client_key(agent, subagent))
        waiting = loop.approvals.get(call_id) if loop is not None else None
        if waiting is None or waiting.done():
            return {
                "answered": False,
                "text": (
                    f"Nothing is waiting for an answer about {call_id!r}: the "
                    f"call already ran, or that turn has ended."
                ),
            }
        waiting.set_result(bool(approved))
        logger.info(
            "tool_approval answered=%s call=%s %s",
            bool(approved),
            call_id,
            describe((agent, subagent)),
        )
        return {"answered": True, "text": ""}

    # ── The model config tools ───────────────────────────────────────────
    # v1's `model_*` set, on the process that *resolves* the model rather than
    # in a family plugin of its own: `default:` and `providers:` are what this
    # server turns a conversation's model into, so the edit and the read that
    # honours it are in one process — "哪个 plugin 用，哪个 plugin 管".  The hub
    # reaches them because this server is one of `Config.plugins()`, the same
    # way it reaches `_check_new_input`.
    #
    # **Everything here reads the file, never `Config.providers`.**  A held
    # provider has been through `resolve_secret`, so a listing built from one
    # would print the operator's live key into the conversation and the
    # transcript; `read_section` is the `${VAR}` the file holds, and none of
    # these answers shows even that.

    def _providers() -> dict[str, dict[str, Any]]:
        """The `providers:` section as the file has it — secrets unresolved."""
        return configfile.read_section("providers")

    def _active_default(providers: dict[str, dict[str, Any]]) -> str:
        """The model reference the system runs on, read off the file.

        The `default:` scalar when the file has one, else the first model of the
        first provider — `slife2.libs.host.config._first_reference`'s rule, read here so
        that it reflects an edit this process has just made rather than the
        config it booted with.
        """
        explicit = configfile.read_scalar("default")
        if explicit:
            return explicit
        for name, entry in providers.items():
            models = entry.get("models")
            if isinstance(models, list) and models and isinstance(models[0], dict):
                return f"{name}/{models[0].get('model', '')}"
        return ""

    async def _ensure_backend(api: str) -> dict[str, Any]:
        """Start the model server for an api nothing was serving.

        **The one case a re-read cannot cover.**  The agent and the backends
        follow the file, but a protocol with no provider at boot has no process
        to follow it — so the first provider added for one is a server that has
        to exist before the model can be reached.  `launcher.ensure` is the same
        call the launcher makes, so the process is started, recorded and reused
        exactly as one from a cold start.

        Off the event loop, because `probe` refuses to run inside one by design
        (`slife2.libs.host.launcher`); and on the common path `ensure` probes first, so a
        model added beside a provider whose backend is already up costs one
        round trip and starts nothing.

        Answers `{"api", "up", "detail"}` — the protocol, whether it has a
        backend now, and the reason when it has not.  **A start that failed is a
        value and not an exception**, so the model is told its model was written
        and its backend was not: the edit stands either way, and a restart is
        what retries.
        """
        from slife2.libs.host import launcher

        try:
            outcome = await asyncio.to_thread(
                launcher.ensure,
                launcher.spec_for_api(current(), api),
                config_path=configfile.config_path(),
            )
        except Exception as exc:  # noqa: BLE001 - a start that raised is not a crash
            return {
                "api": api,
                "up": False,
                "detail": f"its server could not be started ({exc}); a restart "
                f"will retry",
            }
        if outcome.ok:
            return {"api": api, "up": True, "detail": ""}
        return {
            "api": api,
            "up": False,
            "detail": f"it did not come up — "
            f"{outcome.detail or 'it did not answer'}; a restart will retry",
        }

    @mcp.tool(name="model_list", meta=FOR_THE_MODEL)
    async def model_list() -> dict[str, Any]:
        """List the models this config defines.

        Reads the config file, so it is what the next conversation would use.

        Returns:
            `models`, one row per model: `provider` and `model`, which name it —
            `provider/model` is the reference the rest of these tools take —
            `name`, whether it is the `default`, and whether it reasons or reads
            images, under `marks`; and `context_window` and `max_tokens`, both
            `0` when the config does not say.  Plus `total`, how many models
            there are, and `default`, the reference the system runs on — `null`
            when the config names none.
        """
        try:
            providers = _providers()
            active = _active_default(providers)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

        # **No `limit`/`offset`, and the bound is what decides it.**  Every other
        # listing here answers a log — turns, runs — which grows for as long as
        # the system is used, so it pages.  This one answers the config file: a
        # handful of models an operator wrote down, read whole in a fraction of
        # the token budget, where a page would be two arguments the model has to
        # reason about for a list it can hold in one look.
        models: list[dict[str, Any]] = []
        for provider, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            specs = entry.get("models")
            for spec in specs if isinstance(specs, list) else []:
                if not isinstance(spec, dict):
                    continue
                model = str(spec.get("model") or "")
                marks = []
                if f"{provider}/{model}" == active:
                    marks.append("default")
                if spec.get("reasoning"):
                    marks.append("reasoning")
                if "image" in (spec.get("input") or []):
                    marks.append("image")
                models.append(
                    {
                        "provider": provider,
                        "model": model,
                        "name": str(spec.get("name") or model),
                        "marks": marks,
                        "context_window": spec.get("context_window") or 0,
                        "max_tokens": spec.get("max_tokens") or 0,
                    }
                )
        return {"models": models, "total": len(models), "default": active or None}

    @mcp.tool(name="model_set", meta=FOR_THE_MODEL)
    async def model_set(
        ref: str,
        name: str,
        api: str = "",
        base_url: str = "",
        api_key: str = "",
        reasoning: bool = False,
        input: list[str] | None = None,
        context_window: int = 0,
        max_tokens: int = 0,
        temperature: float | None = None,
        top_p: float | None = None,
        compat: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add or update one model, creating its provider.

        This is the model's whole entry: a field left out is dropped, so restate
        everything it should have. The next conversation can name it.

        Args:
            ref: The model, as `provider/model` — the same reference its
                siblings take. The provider is created when it is new.
            name: The display name.
            api: The wire protocol — `openai-completions`,
                `anthropic-messages` or `openai-responses`. Empty uses the
                provider's own, or openai-completions for a new one.
            base_url: The endpoint. A new provider needs it.
            api_key: A `${VAR}` reference, so the key stays out of the file. A
                new provider needs it.
            reasoning: The model thinks natively, and its reasoning is kept on
                the turn.
            input: What it reads — `["text"]`, or `["text", "image"]`.
            context_window: Its context window, in tokens.
            max_tokens: Its output limit, in tokens.
            temperature: Sampling temperature.
            top_p: Nucleus sampling.
            compat: Wire quirks the endpoint needs, e.g. `{"thinking": "omit"}`.

        Returns:
            `ref`, and what the write did: `created` when this call added a model
            the file did not have rather than replacing one it did, and
            `provider_created` when it made the provider too. `backend` is the
            protocol's backend when this call had to start one — `{"api", "up",
            "detail"}`, where `up: false` carries the reason — and `null` when a
            provider for that protocol was already configured.
        """
        provider, _slash, model = ref.strip().partition("/")
        provider, model, name = provider.strip(), model.strip(), name.strip()
        if not provider or not model:
            raise ToolError(f"`{ref}` is not a model reference; use `provider/model`.")
        if not name:
            raise ToolError(
                "a model needs a display `name` — it is what the model is called "
                "in a conversation."
            )
        if api and api not in API_BACKENDS:
            raise ToolError(
                f"{api!r} is not a wire protocol; known: {', '.join(API_BACKENDS)}."
            )
        try:
            providers = _providers()
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

        existing = providers.get(provider)
        creating = not isinstance(existing, dict)
        if creating and not (base_url and api_key):
            raise ToolError(
                f"`{provider}` is a new provider, so it needs `base_url` and "
                f"`api_key` — the key as a `${{VAR}}` reference, so it stays out "
                f"of the file."
            )
        known = set()
        for entry in providers.values():
            if isinstance(entry, dict) and entry.get("api"):
                known.add(str(entry["api"]))
        entry_api = api or (existing or {}).get("api") or "openai-completions"
        before = [
            one
            for one in ((existing or {}).get("models") or [])
            if isinstance(one, dict)
        ]
        had_model = any(one.get("model") == model for one in before)

        def change(current: dict[str, Any]) -> dict[str, Any]:
            entry = dict(current)
            if creating or api:
                entry["api"] = entry_api
            if base_url:
                entry["base_url"] = base_url
            if api_key:
                entry["api_key"] = api_key
            spec: dict[str, Any] = {"model": model, "name": name}
            for key, value in (
                ("reasoning", reasoning or None),
                ("input", input),
                ("context_window", context_window or None),
                ("max_tokens", max_tokens or None),
                ("temperature", temperature),
                ("top_p", top_p),
                ("compat", compat),
            ):
                if value is not None:
                    spec[key] = value
            # The changed model goes first and its siblings keep their order, so
            # a `set` on one model of a provider leaves the rest of the file
            # exactly as it was.
            entry["models"] = [
                spec,
                *(one for one in before if one.get("model") != model),
            ]
            return entry

        try:
            configfile.update_entry("providers", provider, change)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

        return {
            "ref": f"{provider}/{model}",
            "created": not had_model,
            "provider_created": creating,
            # Only when this call is what made the protocol reachable: a provider
            # for an api that was already configured is served by a backend that
            # has been up since boot, and reporting on it here would be this tool
            # answering about a process it never touched.
            "backend": await _ensure_backend(entry_api)
            if entry_api not in known
            else None,
        }

    @mcp.tool(name="model_remove", meta=FOR_THE_MODEL)
    async def model_remove(ref: str) -> dict[str, Any]:
        """Remove one model, named `provider/model`.

        The model stays at its provider, and the provider goes too when that was
        its last model. Refused for the default model — switch to another first
        — and for one a running conversation is on, whose model is fixed when it
        starts.

        Args:
            ref: The model, as `provider/model`.

        Returns:
            `ref`, and `provider_removed`: whether the provider's whole entry
            went with it, which happens when this was its last model.
        """
        ref = ref.strip()
        if "/" not in ref:
            raise ToolError(f"`{ref}` is not a model reference; use `provider/model`.")
        provider, model = ref.split("/", 1)
        try:
            providers = _providers()
            active = _active_default(providers)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        entry = providers.get(provider)
        specs = (entry or {}).get("models")
        specs = specs if isinstance(specs, list) else []
        if not any(
            isinstance(one, dict) and one.get("model") == model for one in specs
        ):
            raise ToolError(f"`{ref}` is not a model this config defines.")
        # **The config-wide guard comes first, deliberately.**  With one model
        # the fallback makes it the default too, so both refusals are true — and
        # "it is the last model" is the deeper reason, the one that says what
        # would actually go wrong.
        total = sum(
            len([one for one in entry.get("models") or [] if isinstance(one, dict)])
            for entry in providers.values()
            if isinstance(entry, dict)
        )
        if total == 1:
            raise ToolError(
                f"`{ref}` is the last model in the config; without it slife2 "
                f"would fall back to its built-in default. `model_set` another "
                f"first."
            )
        if ref == active:
            raise ToolError(
                f"`{ref}` is the default model; `model_switch` to another first."
            )
        # **This home's conversations, and this home's only.**  The loop table is
        # keyed by `(home, agent, subagent)` because one process serves several
        # homes, and `current()` above read the *caller's* file — so a loop filed
        # under another home is a conversation this call knows nothing about, and
        # refusing on its behalf would be a false refusal that also printed
        # somebody else's conversation names into an error the model reads.
        here = this_home()
        holders = [
            describe((one.agent, one.subagent))
            for key, one in loops.items()
            if key[0] == here and one.model == ref
        ]
        if holders:
            raise ToolError(
                f"`{ref}` is the model a conversation is running on "
                f"({', '.join(holders)}); `model_switch` to another one first — "
                f"it moves the conversations running on this one with it."
            )

        def change(current: dict[str, Any]) -> dict[str, Any] | None:
            kept = [
                one
                for one in (current.get("models") or [])
                if not (isinstance(one, dict) and one.get("model") == model)
            ]
            if not kept:
                # A provider with no models is one the loader refuses, so its
                # entry goes with its last model rather than being left empty.
                return None
            return {**current, "models": kept}

        try:
            configfile.update_entry("providers", provider, change)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        return {"ref": ref, "provider_removed": len(specs) == 1}

    @mcp.tool(name="model_switch", meta=FOR_THE_MODEL)
    async def model_switch(ref: str) -> dict[str, Any]:
        """Make `provider/model` the model this system runs on, at once.

        Writes the top-level `default:` in the config file, and moves every
        conversation this server is holding onto it: each keeps its history and
        runs on the new model from its next turn. That is what makes it the way
        out of a model that has stopped answering. The model must be one the
        config defines.

        Args:
            ref: The model, as `provider/model`.

        Returns:
            `default` — the reference the system now runs on — `previous`, what
            `default:` said before, and `moved`, how many conversations that were
            already running this server moved onto it from their next turn.
        """
        ref = ref.strip()
        if "/" not in ref:
            raise ToolError(f"`{ref}` is not a model reference; use `provider/model`.")
        try:
            providers = _providers()
            old = _active_default(providers)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        provider, model = ref.split("/", 1)
        entry = providers.get(provider)
        specs = (entry or {}).get("models")
        specs = specs if isinstance(specs, list) else []
        if not any(
            isinstance(one, dict) and one.get("model") == model for one in specs
        ):
            raise ToolError(f"`{ref}` is not a model this config defines.")

        # What the conversations below are moved *onto*, and it is not the same
        # question as the judge inside the writer.  `default:` is a scalar the
        # model's own settings do not depend on, so resolving the reference a
        # moment before the write reads the same entry the write leaves behind —
        # which is what lets the move be made from an answer `resolve` gave
        # rather than from the file's raw dicts, whose rules about `input:` and
        # about a bare provider name are `Config.resolve`'s and are not worth a
        # second spelling here.
        try:
            name, endpoint, settings = current().resolve(ref)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

        def resolves(config: Config) -> None:
            """The writer's judge: `load` accepts any string, `resolve` does not."""
            config.resolve(ref)

        try:
            configfile.set_scalar("default", ref, check=resolves)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc

        # **The conversations move with it, and this is the half that makes the
        # tool worth having.**  Writing `default:` alone changes what the *next*
        # conversation starts on — so switching away from a model that has
        # stopped answering, which is the whole reason to do this, would leave
        # the window being answered (or not) by the model it was trying to
        # leave.  v1's `switch_model` reloaded the loop for exactly that reason,
        # and it is done here rather than by the caller because this process is
        # the one holding the loops: the edit and the read that honours it are
        # one process, which is the argument that puts these four tools on the
        # agent server at all.
        #
        # From the next turn, never mid-turn.  `loop_for` resolves the backend
        # once, when a turn starts, so a turn already in flight finishes on the
        # model it began on — and the record of what answered it was taken at
        # that same boundary (`Outcome.model`).  Everything the conversation
        # reaches its model with moves, images included: `accepts_images` is
        # what `attach_image` refuses against.
        # **Only this home's conversations move, and that is a boundary rather
        # than a filter.**  The table is keyed by `(home, agent, subagent)`
        # because one process serves every home on the machine, and the
        # reference just resolved came out of the *caller's* `slife2.yaml` — so
        # moving another home's loop would point it at a model its own config may
        # not even name, and its store would then record a foreign model.  The
        # home is read here, inside the call, which is the only place it is the
        # caller's (`this_home`).
        here = this_home()
        mine = [loop for key, loop in loops.items() if key[0] == here]
        reference = f"{name}/{settings.model}"
        for loop in mine:
            loop.model = reference
            loop.provider = name
            loop.api = endpoint.api
            loop.model_id = settings.model
            loop.accepts_images = settings.accepts_images

        return {"default": reference, "previous": old or None, "moved": len(mine)}

    @mcp.tool
    async def send_message(
        agent: str,
        prompt: str,
        ctx: Context,
        subagent: str = "",
        model: str = "",
        images: list[str] | None = None,
        channel: str = "",
        watched: bool = False,
        receipt: str = "",
    ) -> dict[str, Any]:
        """Say something to a conversation and get that turn's answer.

        The conversation is addressed by `(agent, subagent)` and **starts on the
        first message sent to a key** — there is nothing to open and no id to
        carry.  Sending to a key that has been idle for a long time simply starts
        it again; that is not an error and a caller cannot tell it happened.

        Assistant output streams back as `notifications/progress` on this
        request, one notification per chunk.  The returned `text` is the final
        answer and is authoritative — a client that ignores the progress stream
        still gets the whole thing.

        **A message sent to a busy conversation waits rather than displacing
        anything** — and what it waits *for* is the running turn's next
        iteration boundary, where it is folded in (see `inject`).  Turns on one
        key run one at a time, in the order they were submitted; a message the
        running turn did not reach before it ended becomes a turn of its own
        when its place comes up.  Nothing is dropped and nothing is cancelled —
        up to a bound,
        because a client's call timeout does not know it is waiting.  Different
        keys do not wait for each other at all.

        A caller whose message was folded in is answered by the turn that took it:
        the same `text` that turn's own caller got, with `injected` true.  The
        model was given both messages, so its answer is the answer to both — and
        a caller told nothing would have no way to tell that from a turn that
        happened to say the same thing.

        Args:
            agent: Whose conversation.  It renders the system prompt and is the
                name its turns are recorded under.
            prompt: What the user just said.
            subagent: Which of that agent's conversations.  Empty — the default —
                is the agent's own, the one a person is watching.  Anything else
                is one of its subagents: a separate conversation, with its own
                history, its own inbox and its own directory, whose turns are
                written to *its* store — the one `subagent_remove` deletes.
            model: Which model this conversation runs on, as `provider/model`.
                Left out, the config's `default` is used.  Read when the
                conversation starts — a later message naming a different model is
                ignored, because the model, the system prompt and the recorded
                name are all properties of the conversation, and a caller that
                could rename the model per message could rename who answered.
                Change it by resetting the conversation, or for everything at
                once with `model_switch`, which is the one edit that reaches a
                conversation already running.
            images: Images to send with it — a local path, an `http(s)` URL or a
                `data:` URI, the same three `attach_image` takes.  Refused unless
                the model's config lists `image` under `input`; silently
                dropping an attachment somebody made is worse than saying the
                model cannot read it.
            channel: Where this turn came in from — `tui`, or the key of
                whoever sent it.  Recorded with the turn and used for nothing
                else; the caller is the only party that knows, which is why it is
                a parameter rather than something this server infers.  It is also
                what a turn's footnote carries, so a model reading a conversation
                can tell a person's turn from a subagent's.
            watched: Whether **this caller can be asked a question mid-turn and
                will answer it**.  A turn whose model asks to be checked
                (`_approve`) sends the request to whoever is watching and waits
                for the answer, which arrives as a call of its own
                (`answer_approval`) — so a caller that will not make that call
                must say so here, or the turn waits for an answer nobody will
                send.  False means the request is granted and the call runs,
                which is the right answer for a channel with nobody behind it
                (WeChat, a scheduled task, another agent) and for every client
                that has not been taught to draw a prompt.
            receipt: The caller's own opaque id for this submission.  It is
                echoed on the `MessageCutIn` event whenever a running turn takes
                the message, so a caller that can draw one (the window) can tell
                a folded message from one that ran its own turn.  Never read by
                this server, and absent for every caller that does not ask.

        Returns:
            `text` (the final answer), `usage`, `iterations`, `stop_reason`, the
            `model` that answered — which a bare provider or an empty reference
            does not otherwise reveal — `injected`, true when this message was
            folded into a turn that was already running rather than run as its
            own, and `refused` (empty on an ordinary turn), naming any source
            this call passed that could not be attached.  The key set is the
            same whichever path answered — `_reply` builds it in one place — so
            a caller reads them without having to know which one it got.
        """
        # Asked before anything is spent.  A context plugin that is not there is
        # a broken system rather than a degraded one, and the moment to find that
        # out is *before* the first model call has been paid for — not at the
        # write, when the answer exists and has nowhere to go.
        await memory()
        reap()
        loop = conversation(agent, subagent, model)
        # The key is what the watcher registry and the tap are filed under.
        # Taken from the one function that defines a conversation's identity, so
        # a watcher and the turn it is watching cannot disagree about it.
        key = client_key(agent, subagent)

        if len(loop.inbox) >= MAX_QUEUED:
            raise ToolError(
                f"{describe((agent, subagent))} already has {len(loop.inbox)} "
                f"messages waiting, which is the limit.  Wait for them, or reset "
                f"the conversation."
            )

        item = Pending(
            prompt=prompt,
            images=list(images or []),
            channel=channel,
            watched=watched,
            receipt=receipt,
        )
        loop.inbox.append(item)
        outcome = Outcome()
        try:
            async with loop.lock:
                # **The previous turn's record is taken up before this caller is
                # told anything.**  A message folded into that turn was answered
                # by it only if its row landed; a refused write is rolled back
                # here and un-answers the folded entries, so the check below
                # sees `None` and this message runs its own turn instead of
                # being handed an answer the conversation no longer holds.  It
                # also has to precede the restore and rebuild, which read
                # `turn_ids` and `covered` (`settle_record`).
                await settle_record(loop)
                # **The turn that just released the lock may have taken this
                # message with it**, folding it in at one of its iteration
                # boundaries
                # (`inject`).  Then there is no turn here to run: the model was
                # handed this message inside that one and its answer is the
                # answer to this, so the caller is given exactly what that turn's
                # own caller got.  Asked of the *entry* rather than inferred from
                # the queue, because a turn that produced no result leaves it as
                # `None` — and then this message still owes itself a turn, which
                # is what the rest of this block is for.
                if item.result is not None:
                    return _injected_reply(item)
                # **Ours, and its turn is starting now.**  A message waiting in
                # the inbox is *not* in `messages`: `AgentLoop.run_turn`
                # re-reads the list every iteration, so an early append would be
                # seen by the running turn as steering nobody asked for.
                with contextlib.suppress(ValueError):
                    loop.inbox.remove(item)
                # **Inside the lock, and here rather than when the loop is
                # built.**  Both halves are the same correction.  A restore
                # replaces the message list wholesale, so it has to happen before
                # the turn that runs on it — and it has to happen under the lock,
                # because two callers can arrive on one brand-new key at once:
                # one creates the loop and the other does not, and a restore
                # awaited outside the lock lands *underneath* whichever of them
                # got in first, reassigning the list a running turn is reading.
                # The claim is on the loop for the same reason a per-call flag
                # was not enough.
                if not loop.restored:
                    # **A fork is put back on its maker's conversation, not on a
                    # stored one.**  It has no store — nothing about it is
                    # written down — so this is its own step, and it is taken
                    # here for the reason the restore is: this is where a
                    # conversation begins.  Which includes beginning again: a
                    # fork the idle sweep let go starts afresh from its maker as
                    # it stands then, and that is the same rule and not a
                    # fallback — a conversation nobody wrote down is a
                    # conversation whose next start is a new copy.
                    if loop.fork:
                        await adopt_maker(loop)
                    else:
                        await restore_into(loop)
                    loop.restored = True
                # **The turn is opened for watchers here**, before its first
                # observable step: `rebuild_into` can emit `ContextChosen`, and
                # a watcher that joined while it ran must find that event in the
                # tap rather than before a turn it belongs to.  The message is
                # the one frame a watcher never sent and so cannot otherwise
                # see.  Outside the `try` as well, so a failure below is a
                # failure of a turn that was announced rather than an unbound
                # name in the handler.
                tap = start_tap(key, TurnStarted(item.prompt, item.channel))
                observer = ProgressObserver(ctx, publish=partial(publish, key))
                try:
                    # **The rebuild is inside this `try` too**, and not for
                    # tidiness: when the discriminator is asked a question it
                    # makes a model call carrying these same messages
                    # (`slife2.plugins.context.server._ask_model`), so the very failure
                    # this block repairs can arrive from here rather than from the
                    # turn.  Anything that reaches a provider on this
                    # conversation's behalf belongs on the repaired side of the
                    # line.
                    #
                    # **Before the turn.**  It replaces the message list
                    # wholesale — anything appended first, which includes this
                    # very prompt, would be destroyed by it — and it reads the
                    # list the previous turn's write maintains, so it has to wait
                    # for that write the way the next turn does.
                    #
                    # **And a fork is not rebuilt at all.**  There is no log to
                    # go back to and no keep-list worth deciding, because what a
                    # fork runs on is the copy it was handed plus its own turns.
                    # Asked of the loop rather than of the store: a discriminator
                    # call spent deciding what a fork already knows the answer to
                    # would be a model call whose only outcome is "keep what you
                    # have".
                    if not loop.fork:
                        chosen = await rebuild_into(loop, item.prompt)
                        # **Reported before the turn**, because it is a fact about
                        # what the turn runs on and not about how it went — and
                        # because the answer that follows is the thing it is about.
                        # The discriminator is one model call the caller never sees,
                        # so this is the only place its decision is visible at all.
                        #
                        # **Through the loop's own fault tolerance**, like every
                        # other event this conversation raises.  A broken progress
                        # stream throws from `on_event` (`ctx.report_progress` on a
                        # closed stream), and a throw from *here* lands before the
                        # prompt has been appended or recorded at all — so the
                        # caller's message is lost outright, where the same observer
                        # failing on a `TextDelta` is swallowed one layer down
                        # (`AgentLoop._emit`).  Asked for by class rather than instance
                        # because the `AgentLoop` is not built until the turn below.
                        await AgentLoop._emit(observer, chosen)
                    # How long the list was when this turn began.  What the write
                    # below covers is what the turn *appends*, and the difference
                    # is the whole of it — see `covered` in the success branch.
                    before = len(loop.messages)
                    await run_turn_into(loop, item, observer, outcome)
                except BaseException as exc:
                    # A turn that happened is recorded however it ended, and this
                    # is deliberately one clause rather than a handler per ending
                    # — see `remember_turn` for why the rule is written that way.
                    # A provider that answered with a 500 is the case that made it
                    # matter: the same turn that leaves the user's message in the
                    # transcript must not leave it missing from the record, which
                    # is the failure a write conditional on *how* a turn ended
                    # produces.
                    #
                    # **The write is a task in both cases, and what is done with
                    # it depends on who is still here.**  A failure can wait for
                    # its own record: the caller is present, so a refusal is
                    # known now rather than left for a later turn.  An
                    # interruption cannot — its caller is gone — so the next
                    # turn takes the id up (`settle_record`).  `detach` is what
                    # makes either task outlive this scope.
                    #
                    # **Repaired before it is recorded**, so the row and the live
                    # conversation are one list and not two: the record writes
                    # `outcome.messages`, which holds this same list's own
                    # objects, and a write that serialised the refused bytes
                    # while the live list had already let them go is the
                    # disagreement `repaired_error` exists to prevent.
                    repaired = repaired_error(loop, exc)
                    if outcome.messages:
                        written = detach(record(loop, item, outcome))
                        # **The id reaches the tap when the write does**, and
                        # that is what lets a watcher tell whether the snapshot
                        # it has just read already contains this turn: the same
                        # turn can be in the store and in the tap, and drawing
                        # both is the duplicate this field exists to prevent.
                        written.add_done_callback(partial(_recorded_into, tap))
                        # An id backs a *prefix*, so it can only be claimed when
                        # this turn began on a wholly backed list — where it
                        # began being `began`, re-derived rather than read
                        # because a failure inside the rebuild reaches this
                        # handler with the name unbound (and with no messages of
                        # its own, which is what the `if` above says).
                        began = len(loop.messages) - len(outcome.messages)
                        if isinstance(exc, Exception):
                            # **A failure waits for its own row.**  A store or
                            # cabinet refusal is a hard failure: the turn is
                            # unrecorded, so its messages and the answers it gave
                            # folded callers are taken back, and the original
                            # failure is what the caller sees.  A cancellation
                            # during this await keeps the task for the next turn,
                            # which applies the same rule.
                            try:
                                turn_id = await asyncio.shield(written)
                            except asyncio.CancelledError:
                                if loop.covered == began:
                                    loop.record_pending = PendingRecord(
                                        written, outcome, item
                                    )
                                raise
                            except Exception as refusal:  # noqa: BLE001 - a refused write is a hard failure
                                logger.warning(
                                    "the failed turn of %s was not recorded "
                                    "(%s); rolling it back",
                                    describe((loop.agent, loop.subagent)),
                                    refusal,
                                )
                                roll_back(loop, outcome)
                                if taps.get(key) is tap:
                                    taps.pop(key, None)
                            else:
                                if turn_id is not None and loop.covered == began:
                                    loop.covered += len(loop.messages) - began
                                    address_turn(loop, outcome, item, turn_id)
                        elif loop.covered == began:
                            # **An interruption is claimed once its row lands.**
                            # The picture is in the cabinet, so an id no longer
                            # risks the retry image; a refused write is a hard
                            # failure and `settle_record` rolls the tail back.
                            loop.record_pending = PendingRecord(written, outcome, item)
                    else:
                        # **A turn that never started is not a turn to watch.**
                        # It has no messages and no events, so a tap left here
                        # would replay a question with no answer after it.  The
                        # watchers are not closed: the conversation is still
                        # watchable, and the next turn replaces the tap anyway.
                        if taps.get(key) is tap:
                            taps.pop(key, None)
                    if repaired is exc:
                        raise
                    raise repaired from exc
                else:
                    # **The write is a task before it is awaited**, so a
                    # cancellation landing in the save does not throw the turn
                    # away: the task outlives this scope and the next turn takes
                    # its id up (`record_pending` / `settle_record`).
                    written = detach(record(loop, item, outcome))
                    written.add_done_callback(partial(_recorded_into, tap))
                    try:
                        turn_id = await asyncio.shield(written)
                    except asyncio.CancelledError:
                        # The turn itself succeeded; its messages stay and the
                        # row is on its way.  The next turn writes the footnote —
                        # but only a turn that began on a wholly backed list can
                        # have its id claimed (the same prefix rule as above).
                        if loop.covered == before:
                            loop.record_pending = PendingRecord(written, outcome, item)
                        raise
                    except Exception as refusal:  # noqa: BLE001 - a refused write is a hard failure
                        # **A record that is refused fails the turn.**  Roll the
                        # messages back to the prefix the turn began on and
                        # un-answer the callers it had folded in; the refusal is
                        # what the caller sees.
                        logger.warning(
                            "the turn of %s was not recorded (%s); rolling it back",
                            describe((loop.agent, loop.subagent)),
                            refusal,
                        )
                        roll_back(loop, outcome)
                        if taps.get(key) is tap:
                            taps.pop(key, None)
                        raise
                    # The store has this turn now (or it never will: a fork has
                    # no row).  Recorded on the tap so a watcher's snapshot can
                    # decide whether replaying the tap would duplicate it.
                    tap.recorded_turn = turn_id
                    # **A turn joins `turn_ids` only when the list it follows is
                    # wholly accounted for**, and this is the second half of what
                    # `covered` means.  `covered` is a count from the *start*, so
                    # the ids it stands for have to describe a prefix of
                    # `messages` — and an unbacked run in front of this turn (a
                    # cancelled or failed turn, or one the store refused to
                    # record) makes that impossible: the id would be counted as
                    # covering messages that are not its own, and the next
                    # rebuild's `carried = len(messages) - covered` would then be
                    # read off the wrong end of the list — silently dropping the
                    # unbacked messages *and* re-appending this turn's own tail a
                    # second time.
                    #
                    # Such a turn's messages stay in the carried run instead:
                    # verbatim, in order, and not addressable (a keep-list cannot
                    # name a turn the loop cannot name either).  The row is still
                    # written — a turn that happened is recorded however it ended
                    # (`remember_turn`) — and the rebuild that next changes the
                    # context carries the whole run back in beside the turns it
                    # keeps, which is what makes this a delay rather than a loss.
                    if turn_id is not None and loop.covered == before:
                        # **Only what this turn appended is covered.**  Setting
                        # `covered` to the whole list counted the carried tail
                        # with it — a cancelled turn's unbacked messages, which
                        # no id here accounts for — and the next rebuild reads
                        # `carried = len(messages) - covered`, so its tail came
                        # out zero and the user's own message was dropped from
                        # the conversation.  The turn's own messages are exactly
                        # the ones appended since `before`, and they are the whole
                        # of the run because the guard above says nothing was
                        # carried into this turn.
                        loop.covered += len(loop.messages) - before
                        # ...and it is addressable now, which is what the next
                        # turn's discriminator needs: a turn it cannot name is
                        # one a keep-list cannot keep.
                        address_turn(loop, outcome, item, turn_id)
        finally:
            # On every path, including a cancellation while queued: the turn
            # never started, so there is nothing to record, but the inbox entry
            # must not be left behind.  A no-op when the running turn folded this
            # message in — that is exactly the case the entry is already gone —
            # and the suppression is what says so without a branch: there is no
            # difference to report between "I took it out" and "it was taken".
            loop.last_used = time.monotonic()
            with contextlib.suppress(ValueError):
                loop.inbox.remove(item)

        # **One answer, built by `_reply`.**  No `if result` here: every path
        # that leaves a turn with no result has already left — `_injected_reply`
        # for a folded message, and the re-raises in the handler above for a
        # cancellation and a failure — so by this line the turn answered, and the
        # assert says so to a reader and to the checker rather than leaving
        # branches that can never be taken.
        assert outcome.result is not None
        return _reply(
            outcome.result,
            # What answered this message, not what the loop's reference says by
            # now — `_reply` is where that is argued.
            model=outcome.model or loop.model,
            injected=False,
            refused=outcome.refused,
        )

    async def stored_turns(agent: str, subagent: str) -> list[dict[str, Any]]:
        """The turns a conversation's store is made of, oldest first.

        **One read, two readers**: `transcript` gives it to a window that has
        just opened, and `watch` gives it to a watcher as its first frame.  They
        must agree about where history ends — a watcher that read a different
        list from the window would start its live stream at a different turn —
        so the read lives here rather than being written out twice.

        The opening prompt is what a conversation that has never run would head
        with, so it is handed over rather than an empty list; the returned
        `messages` half is the loop's business and is not read here.
        """
        payload = tool_payload(
            await (await memory()).call_tool(
                "restore",
                {
                    "agent": agent,
                    "subagent": subagent,
                    "messages": [
                        m.to_wire()
                        for m in opening_messages(
                            agent, subagent, current(), subagent_spec(agent, subagent)
                        )
                    ],
                },
            )
        )
        turns = payload.get("turns")
        if not isinstance(turns, list):
            raise unreadable("the context store", "return a turn list", payload)
        return turns

    @mcp.tool
    async def transcript(agent: str, subagent: str = "") -> dict[str, Any]:
        """What a conversation is made of, for a screen that has just opened.

        The restoring read's second reader.  `restore` exists because a
        conversation that has just been *built* has to be put back on its
        context before it can run; a terminal that has just been opened asks the
        opposite question — the context is fine, and what it needs is to show
        the conversation the previous terminal was showing.  One read answers
        both, and this is it: the same list, answered with the turns it was
        built from rather than with the message list.

        **A read, deliberately.**  Opening a window must not start, end or
        rebuild anything, so this touches neither the loops nor the list — which
        is also why it is the *stored* list rather than the in-memory one: a
        window opened against a server that has been up for a week still shows
        the conversation, and so does one opened against a server that has just
        started.

        **A `turns` this build cannot read is refused, not drawn as none.**  A
        context plugin answering with a shape this build does not know — the
        same damage `_adopt` refuses — used to become `[]`, so a screen showed
        an empty conversation and said nothing (DESIGN.md §11.1: a failure is
        not a verdict).

        Args:
            agent: Whose conversation.
            subagent: Which of that agent's conversations; empty for its own.

        Returns:
            `turns`: the stored turns this conversation's context is made of, in
                the list's own order, oldest first.  Empty for a conversation
                that has never run — the honest answer for a genuinely new
                name, and not an error.
        """
        return {"turns": await stored_turns(agent, subagent)}

    @mcp.tool
    async def subagents(agent: str) -> dict[str, Any]:
        """The subagents an agent has made, for a screen that offers them.

        **The filesystem is the list**, by the rule the subagent directory
        states: what makes a directory a subagent's is its `subagent.yaml`, and
        nothing keeps a registry to be kept in step with a directory that is
        deleted without notice (`slife2.libs.host.paths.subagent_dirs`).  A watch
        list that asked the *network* instead would be a different question:
        `subagent_list` is the model's roster, it connects and wakes members to
        answer, and a screen that merely offers names must not do either.

        A damaged record reads as the defaults, exactly as it does everywhere
        else that reads one (`read_subagent`): losing a role to a half-written
        file is smaller than refusing to list the conversation that exists.

        Args:
            agent: Whose subagents.

        Returns:
            `subagents`: one entry per directory, sorted by name, each with
                `name`, `model` (empty for the maker's own), `role`, and `fork`.
                Empty is the honest answer for an agent that has made none.
        """
        found: list[dict[str, Any]] = []
        for directory in sorted(subagent_dirs(agent), key=lambda one: one.name):
            spec = subagent_spec(agent, directory.name)
            found.append(
                {
                    "name": directory.name,
                    "model": spec.model,
                    "role": spec.role,
                    "fork": spec.fork,
                }
            )
        return {"subagents": found}

    @mcp.tool
    async def watch(agent: str, subagent: str, ctx: Context) -> dict[str, Any]:
        """Watch a conversation's turns as they happen — read-only, until closed.

        **Hold this call open for as long as the view is up.**  It does not
        return: the first progress notification is a `Snapshot` of the turns the
        store already holds, and every notification after it is a turn event
        (`slife2.libs.turn.watch` is the vocabulary).  A cancelled call is the
        detach, and there is no `unwatch` to send — the same lifetime the
        window's own inbox (`slife2.plugins.tui.server`) and an agent's place on
        the mesh (`slife2.plugins.a2a.server`) have.

        **Watching is not running.**  Nothing here builds a loop, chooses a
        context or spends a model call: a subagent still runs when something
        sends it a message, and this is a second reader of the events that turn
        already produces.  That is also why a watched turn is still
        `watched=False` in the loop's sense — a tool call it puts up for approval
        is granted rather than handed to the person watching, because the person
        is not the one the turn is answering.

        **A turn already in flight is replayed from its start.**  The store only
        has a turn once it ends, so the turn in flight is kept in the
        conversation's tap (`Tap`) and handed over before the live stream; a
        watcher that joined mid-turn would otherwise see an answer to a question
        it never saw.  A tap whose record is already in the snapshot is skipped,
        which is the one way the same turn could be drawn twice.

        The person watching never slows the turn: the observer appends to the
        tap and `put_nowait`s for each watcher, and the reporting happens on this
        call's own task (see `Watcher` for why it must).

        Args:
            agent: Whose conversation.
            subagent: Which of that agent's conversations; empty for its own.
                Anything else is one of its subagents.

        Returns:
            `closed`: true, when the conversation stopped being watchable (its
                subagent was removed) and the last frame, `Closed`, has been
                sent.  A cancellation returns nothing — it is a detach.
        """
        key = client_key(agent, subagent)
        # **The history is read before the watcher is registered, and the tap
        # is taken in the same synchronous step as the registration.**  That
        # order is the whole race: an event during the read is kept by the tap
        # (it is not replaced until the next turn starts), and an event after
        # the step is queued for this watcher.
        #
        # **A turn *starting* during the read is the one thing the tap cannot
        # cover**, because starting a turn replaces the tap and the turn it
        # replaced may not have reached the store yet.  The tap's identity is
        # therefore re-checked after each read, and a read a turn boundary
        # crossed is taken again — the turn that ended is in the store by the
        # next one (its record is awaited before its lock is released), so the
        # retry is a snapshot that is whole.  A turn that merely *ends* leaves
        # the tap in place; `recorded_turn` decides whether the snapshot already
        # has it, which is the same check the replay makes.
        while True:
            before = taps.get(key)
            turns = await stored_turns(agent, subagent)
            if taps.get(key) is before:
                break
        watcher = Watcher()
        watchers.setdefault(key, set()).add(watcher)
        tap = taps.get(key)
        replay = (
            None if tap is None else (tap.started, list(tap.events), tap.recorded_turn)
        )
        known = {
            turn["turn_id"]
            for turn in turns
            if isinstance(turn, dict) and isinstance(turn.get("turn_id"), int)
        }
        count = 0

        async def send(item: WatchItem) -> None:
            nonlocal count
            count += 1
            # **Reported from this handler's own task**, which is what makes the
            # progress token this call's: see `Watcher`.
            await ctx.report_progress(count, None, encode_watch(item))

        try:
            await send(Snapshot(turns))
            if replay is not None:
                started, events, recorded = replay
                # **The tap is the turn in flight, or the one that just ended.**
                # It is skipped only when the snapshot provably contains it —
                # the store answered with an id the snapshot has.  A turn whose
                # write has not answered has no id, and the read above cannot
                # have included it either, so replaying it is the missing half
                # rather than a duplicate.
                if recorded is None or recorded not in known:
                    await send(started)
                    for event in events:
                        await send(event)
            while True:
                item = await watcher.queue.get()
                await send(item)
                if isinstance(item, Closed):
                    # The conversation is gone; the last frame has been written
                    # and there is nothing after it to wait for.
                    return {"closed": True}
        finally:
            found = watchers.get(key)
            if found is not None:
                found.discard(watcher)
                if not found:
                    watchers.pop(key, None)

    @mcp.tool
    async def reset(agent: str, subagent: str = "") -> dict[str, Any]:
        """Forget a conversation, and its context with it.

        Idempotent, and deliberately not an error when there was nothing there:
        the caller's intent — that this conversation should not continue — is
        satisfied either way, and a caller that had nothing to forget is in
        exactly the state it asked for.

        **The stored live-context list is cleared too**, and that is a change:
        dropping the in-memory loop used to be the whole of it, and it cannot be
        any more, because a key that has just been dropped is a key whose next
        message *restores*.  Forgetting a conversation and having it come back on
        the next prompt is not forgetting it.

        Only the *conversation* is forgotten.  The turns it produced are in the
        log — storage rather than state — and clearing those is a different
        request with a different blast radius: `turn_list` still finds every one
        of them, they are simply no longer what this conversation is made of.

        Args:
            agent: Whose conversation.
            subagent: Which of that agent's conversations; empty for its own.

        Returns:
            `reset`: whether there was a conversation there to forget.
        """
        await forget_context(agent, subagent)
        forgotten = loops.pop(client_key(agent, subagent), None) is not None
        logger.debug(
            "reset: %s %s",
            describe((agent, subagent)),
            "forgotten" if forgotten else "was not running",
        )
        return {"reset": forgotten}

    @mcp.tool(name=SUBAGENT_SET, meta=FOR_THE_MODEL)
    async def subagent_set(
        ctx: Context,
        name: str,
        model: str = "",
        role: str = DEFAULT_ROLE,
        fork: bool = False,
    ) -> dict[str, Any]:
        """Make a subagent: an agent of your own, named, with its own memory.

        A subagent is an agent that belongs to you — the same tools, the same way
        of working, its own conversation, and its own directory underneath
        yours (`agents/<you>/subagents/<name>/`).  It has no window and no
        process: it runs when something sends it a message, and from then on you
        address it by name.  It
        is *made* here and *unmade* by `subagent_remove`; everything else it does,
        it does with the tools you have.

        **A subagent you already have is the same subagent** — `set` rather than
        `add`, so naming one twice is safe.  What a second call changes is its
        model and its role; when it was made, and whether it was made as a fork,
        are what they were — a fork's conversation is a copy already taken, and
        what it is made of is not something a later call can re-decide.

        Args:
            name: What to call it.  A name is one subagent and one directory
                inside yours, so it is also the address you send to.
            model: Which model it runs on, as `provider/model`.  Empty — the
                default — is *your* model, read when the subagent first runs
                rather than now, so a `model_switch` you make in between reaches
                it the way it reaches your own next turn.
            role: What it is for, as a word: it goes into the prompt the subagent
                starts with, which opens `You are <name>, one of <agent>'s
                subagents — your role is <role>`.  The default is the plain
                one — `subagent` — and anything else is worth saying only when
                it changes what the agent would do, the way "code reviewer"
                does.
            fork: Whether it starts from a **copy of your own conversation**
                instead of from nothing — a temporary stand-in for you.  A fork
                is handed everything you have said so far, as it stood at the
                moment you made it, and nothing it does is written down: it
                remembers its own turns for as long as this session runs, and a
                restart forgets it — the name is free for a new one.  Ask one a
                question *about* what you are in the middle of when you want the
                answer to that question to be the only thing that enters your
                history.

        Returns:
            `name` (made safe to be a directory), `model`, `role`, `fork`,
            `created_at`, and `created`: whether this call was the one that made
            it.
        """
        who = request_client(ctx)
        if who is None or not who[0]:
            raise ToolError(unaddressed("agent to make a subagent for", "a subagent"))
        maker, mine = who
        if mine:
            # The tree, and it stops at one level — not because a second level is
            # unthinkable, but because nothing would hold a place for it: the
            # local network's membership is the maker's *own* subagents
            # (`subagent_dirs`, under the agent's own `subagents/`), and a place is
            # what makes a member
            # reachable at all (`slife2.plugins.a2a.server.place_for`).  SDD §4.5.3
            # holds the rule this depends on.
            raise ToolError(
                "a subagent cannot make a subagent: the local network holds a "
                "place for one level in — the subagents of the agent you belong "
                "to — so a subagent of yours would have nothing listening for "
                "it. Ask your own maker for what you need."
            )
        held = subagent_dirs(maker)
        directory = subagent_path(maker, name)
        created = directory not in held
        if created and directory.exists():
            raise ToolError(
                f"{directory.name!r} is already a directory inside your "
                f"`subagents/` that is not a subagent — a half-removed one, or one "
                f"made by hand. Pick a name that is free, or remove that "
                f"directory yourself."
            )
        limit = current().subagent.max_running
        if created and len(held) >= limit:
            raise ToolError(
                f"{maker} already has {len(held)} subagents (the `subagent: "
                f"max_running` cap); remove one with {SUBAGENT_REMOVE}, or raise "
                f"it in the config."
            )
        made_at = now()
        if created:
            agent_dir(maker, directory.name)
        record = read_subagent(maker, directory.name)
        # What a second call does not get to rewrite, and both are the same
        # reason: whether it is a fork is settled by the copy that was taken when
        # it was made, and when it was made is not a question a later call is
        # answering.  A `set` says what this subagent runs on and what it is for.
        made_fork = fork if created else record.get("fork") is True
        _write_subagent(
            maker,
            directory.name,
            {
                "name": directory.name,
                "model": model,
                "role": role or DEFAULT_ROLE,
                "fork": made_fork,
                "created_at": record.get("created_at") or made_at,
            },
        )
        logger.info(
            "subagent %s %s (model %s, role %s%s)",
            describe((maker, directory.name)),
            "made" if created else "set",
            model or "the maker's",
            role or DEFAULT_ROLE,
            ", fork" if made_fork else "",
        )
        return {
            "name": directory.name,
            "model": model,
            "role": role or DEFAULT_ROLE,
            "fork": made_fork,
            "created_at": record.get("created_at") or made_at,
            "created": created,
        }

    @mcp.tool(name=SUBAGENT_REMOVE, meta=FOR_THE_MODEL)
    async def subagent_remove(ctx: Context, name: str) -> dict[str, Any]:
        """Unmake a subagent, and take everything it wrote with it.

        The whole of one subagent: its conversation is forgotten (the loop, and
        the list it would restore from), and its directory — turns, cabinet,
        ledger and the record of what it was made with — is deleted.  A subagent
        is a directory, so this is one operation rather than a list somebody has
        to keep complete.

        **Nothing outside `subagents/` can be removed by it, and that is the
        layout rather than a check.**  The name is a name inside the container
        (`subagent_path`), so the agent's own `cabinet/`, `media/` and `turns.db`
        are not reachable from here at all — and inside it, what makes a directory
        a subagent's is the record the directory carries (`subagent.yaml`).  A
        directory there without the record is refused rather than emptied; a name
        that is not there at all is not an error, because the caller wanted it
        gone and it is gone.

        Args:
            name: Which of your subagents.  The same name you made it with.

        Returns:
            `name`, and `removed`: whether there was one here to remove.
        """
        who = request_client(ctx)
        if who is None or not who[0]:
            raise ToolError(unaddressed("agent to remove a subagent of", "a subagent"))
        maker, mine = who
        if mine:
            raise ToolError(
                f"a subagent cannot remove a subagent: {describe(who)} owns no "
                f"subagents — its maker does. Ask your own maker to remove it."
            )
        directory = subagent_path(maker, name)
        if directory not in subagent_dirs(maker):
            if directory.exists():
                raise ToolError(
                    f"{directory.name!r} is not a subagent of {maker}'s — it is "
                    f"not yours to remove, and this call will not delete it."
                )
            return {"name": directory.name, "removed": False}
        # **The network first, and before anything is taken away.**  A place on
        # the subagent network outlives the directory it was held for: the plugin
        # has a responder subscribed to this name, so a removal that only stopped
        # the loop and deleted the files would leave something still answering for
        # a subagent that no longer exists — and a notification that cannot be
        # made has to leave the subagent exactly as it was, which is why it is
        # this first (`leave_the_mesh`).
        await leave_the_mesh(maker, directory.name, client=mesh_client)
        # The loop next — in-process, and before the files: a loop left behind
        # writes its store again on the next message to a name that is supposed
        # to have stopped existing.
        #
        # **And no `forget` against the store, which is the difference from
        # `reset`.**  What `reset` clears is the *stored* live-context list, and
        # that list is a table in `turns.db` — the file this is about to delete.
        # Asking the store first would be asking it to clear a file that is on
        # its way out, and on Windows the asking is not free: opening a store
        # that does not exist *creates* it, and the handle that created it can
        # hold the file against the `rmtree` that follows.
        loops.pop(client_key(maker, directory.name), None)
        _remove_tree(directory)
        # **A watcher is told, not left on a stream that will never write
        # again**, and it is told after the directory is gone: a delete that
        # failed leaves the conversation there (`_remove_tree` raises rather
        # than half-removing it), and a watcher told it was gone would have been
        # told something untrue.  A stream that simply stopped, on the other
        # hand, would be indistinguishable from an idle conversation, so the
        # last frame says why and the call returns after it (`close_watchers`).
        close_watchers(
            client_key(maker, directory.name),
            f"{directory.name} was removed",
        )
        taps.pop(client_key(maker, directory.name), None)
        logger.info("subagent %s removed", describe((maker, directory.name)))
        return {"name": directory.name, "removed": True}

    return mcp


def _remove_tree(directory: Path) -> None:
    """Take a subagent's whole directory away, waiting out a file still in use.

    **Windows will not delete a file that is open**, and the one thing in there
    that can be is the subagent's own conversation store: `remember_turn` detaches
    the record of a turn, so a write can outlive the answer that produced it, and
    until `slife2.libs.store.db._connect` closed its connections the store also
    held the file from *any* earlier turn until the collector ran.  That last one
    is fixed; this is the belt for the braces, because a write genuinely in flight
    is not something the caller can wait for — it belongs to a turn that has
    already been answered, in another process.

    The wait is `storage.sqlite_busy`, which is the registry's number for exactly
    this ("how long a writer waits for the writer holding the database"): a
    holder that is a write in flight lets go in milliseconds, and one that
    outlasts this is not a write.

    Raises:
        ToolError: With the file named and nothing else.  A `WinError 32` — which
            is what this used to raise, in the OS's own words and in the OS's own
            *language* — is not an answer a model can act on; "it is open, try
            again" is.  Refused rather than half-done: the directory is one
            subagent, and a removal that deleted most of it would leave something
            that is neither a subagent nor a directory anybody can finish
            removing.
    """
    deadline = time.monotonic() + _T.timeouts.storage.sqlite_busy
    while True:
        try:
            shutil.rmtree(directory)
            return
        except PermissionError as e:
            if time.monotonic() >= deadline:
                raise ToolError(
                    f"{directory.name!r} could not be removed: {e.filename} is "
                    f"open in another process — its conversation may still be "
                    f"being written. Ask again in a moment."
                ) from e
            time.sleep(_T.timeouts.pacing.file_release)


@dataclass(frozen=True)
class SubagentSpec:
    """What a subagent's own file says it is: what to run it on, and what it is.

    **A subagent's configuration is a file inside the subagent** — beside its
    conversation and its work, the way a WeChat login is
    (`slife2.plugins.wechat.server.SESSION_LOGIN`) — and it is *found* by looking
    rather than listed anywhere (`slife2.libs.host.paths.subagent_dirs`).  That
    is what makes the config as short-lived as the thing it configures: a
    subagent made today and removed tomorrow takes its settings with it, and
    nothing in `slife2.yaml` has to be kept in step with a directory that is no
    longer there.

    So this is read where it is used and never cached, which is the same rule the
    config file itself gets: a subagent that has been named twice runs on what
    the second call said, and a `model_switch` reaches one that has not started
    yet.
    """

    #: `provider/model`, or empty for the maker's own — which is not a fallback
    #: but the default the note asked for: a subagent is an agent the maker owns,
    #: so it runs where the maker runs unless it was told otherwise.
    model: str = ""
    #: What it is for, in the words its prompt uses (`DEFAULT_ROLE`).
    role: str = DEFAULT_ROLE
    #: Whether it was made as a copy of its maker's conversation (`Loop.fork`).
    fork: bool = False


def subagent_spec(agent: str, subagent: str) -> SubagentSpec:
    """One conversation's own settings, or the defaults for the maker's own.

    The maker's own conversation has no such file and is not supposed to: what
    makes a directory a subagent's is the record inside it, and an agent's own
    conversation is not one (`slife2.libs.host.paths.subagent_file`).  So `""`
    answers the defaults rather than being looked up, which keeps
    `agent_dir(agent, "")` — the agent's own directory — out of a question about
    a file that lives under the subagent's own directory.
    """
    if not subagent:
        return SubagentSpec()
    record = read_subagent(agent, subagent)
    return SubagentSpec(
        model=str(record.get("model") or ""),
        role=str(record.get("role") or DEFAULT_ROLE),
        # `is True` rather than `bool(…)`: this file is written by `subagent_set`
        # and YAML spells a boolean as one, so a quoted "false" is a hand-edit
        # that means nothing rather than a second spelling of False — and reading
        # it as True would make a subagent a fork on the strength of a typo.
        fork=record.get("fork") is True,
    )


def read_subagent(agent: str, subagent: str) -> dict[str, Any]:
    """What a subagent was made with, or `{}` when there is none to read.

    **A record that cannot be read is read as absent, and that is a decision
    rather than a shrug.**  What makes a directory a subagent's is that this file
    *exists* (`slife2.libs.host.paths.subagent_dirs`), so a torn write cannot
    widen what `subagent_remove` deletes — the property that matters is carried by
    the file's existence, not by its contents.  What the contents add is the model
    and when it was made, and losing a `created_at` to a half-written file is a
    smaller harm than a removal that refuses to happen; the log line is what says
    a file wants looking at.

    So the shape is the one `slife2.plugins.wechat.server` keeps for its login and
    for its reason: a per-agent file whose absence is a normal state, whose
    damage is reported where a person can read it, and which never turns into a
    refusal of the thing the caller actually asked for.
    """
    path = subagent_file(agent, subagent)
    if not path.is_file():
        return {}
    try:
        loaded = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, YAMLError) as exc:
        logger.warning("the record of %s could not be read: %s", path, exc)
        return {}
    if not isinstance(loaded, dict):
        logger.warning("the record of %s is not a mapping", path)
        return {}
    return loaded


def forget_forks() -> list[Path]:
    """Remove every fork a previous session left behind — at the start of one.

    **A fork is session-lived: its lifetime is the process that made it.**  Its
    conversation is held in memory and nothing it says is written down, so the
    only thing a restart could find is the `subagent.yaml` record — a record of
    a conversation that no longer exists.  Left in place, the name would answer
    as a *new* fork of wherever its maker stands now: a different subagent
    wearing the old one's name, role and model.  So the record goes when the
    conversation it described does, and this is where: the start of a session is
    the end of the last one's forks.

    **The walk is every home this process serves** (`known_homes`), because a
    shared daemon answers for all of them.  It is `turn_dbs`' shape one level in
    and for its reason: what belongs to an agent is found by looking under its
    directory, and nothing keeps a list to be kept in step.

    **A record that cannot be read leaves its directory alone.**  `read_subagent`
    reads a damaged record as absent, so `subagent_spec` answers "not a fork"
    for it, and a read failure does not widen a deletion — the same rule that
    stops a torn record widening `subagent_remove`'s blast radius.

    Raises:
        ToolError: A fork directory that could not be removed, naming the path
            and the reason.  **This is a refusal to start rather than a warning**
            because the session would otherwise begin with a fork of the last
            one still holding its name — the state this function exists to
            prevent.  The caller is the lifespan, so the server does not serve.
    """
    removed: list[Path] = []
    for root in known_homes():
        agents = root / "agents"
        if not agents.is_dir():
            continue
        with in_home(root):
            for agent in sorted(agents.iterdir()):
                if not agent.is_dir():
                    continue
                for directory in subagent_dirs(agent.name):
                    if not subagent_spec(agent.name, directory.name).fork:
                        continue
                    try:
                        _remove_tree(directory)
                    except ToolError as e:
                        raise ToolError(
                            f"the fork {directory} left by the previous session "
                            f"could not be removed: {e}"
                        ) from e
                    removed.append(directory)
                    logger.info(
                        "forgot the fork %s: a fork is session-lived",
                        describe((agent.name, directory.name)),
                    )
    return removed


def settled_messages(messages: list[Message]) -> list[Message]:
    """A conversation cut back to the last exchange that finished.

    **A history a provider will accept, which a live list is not always.**  A
    turn is `[head, user, assistant(calls), results, assistant, …]`, and a list
    caught mid-turn ends on one of two shapes that cannot be sent: an assistant
    message whose calls have no results yet, or a batch of results that does not
    answer every call above it — an iteration runs its calls in order, so a turn
    parked on the second of two has answered the first.  Every other ending is a
    history, and is left exactly as it is: a user message, an answer the model
    gave, an exchange that finished.  Cutting those would be the copy losing
    things its maker knows, which is the one thing a fork is for.

    It is here for the fork (`adopt_maker`), which is taken while its maker is
    *in* a turn — the maker calls `subagent_set` as a tool call, so its own list
    necessarily ends mid-exchange — and a copy of that list is a conversation
    nobody can be sent.  What it costs is the exchange the maker had not
    finished: a tool round nobody has spoken about yet, which is the part of the
    conversation that is still in motion.
    """
    if not messages:
        return messages
    results = 0
    while results < len(messages) and messages[-1 - results].role == "tool":
        results += 1
    above = messages[-1 - results] if results < len(messages) else None
    if above is None or above.role != "assistant" or not above.tool_calls:
        # The results answer nothing, or there are none: the list ends on a
        # message that stands on its own.
        return messages
    if len(above.tool_calls) == results:
        return messages
    return messages[: -1 - results]


def _write_subagent(agent: str, subagent: str, record: dict[str, Any]) -> None:
    """Write a subagent's record — the file that makes its directory one.

    It holds the whole of what a subagent *is*, and it is the whole of that: the
    name it answers to, what it runs on, the role it was made for, whether it is a
    fork, and when it was made (`SubagentSpec` reads three of those back).  A
    second place holding any of it — a section in `slife2.yaml`, a table in a db —
    would be a thing to keep in step with a directory that is deleted without
    notice, and this file is deleted with it.

    Written whole every time, because it is one small mapping and a merge would
    be a second rule about which keys survive a `set`.  Block style, like the two
    neighbours this file sits beside (`wechat.yaml`, `inbound.yaml`): somebody opening
    an agent's directory should not have to read three formats to see three
    facts.
    """
    yaml = YAML()
    yaml.default_flow_style = False
    path = subagent_file(agent, subagent)
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(record, handle)


def _image_url(part: Any) -> str | None:
    """The URL of one `image_url` content part, or `None` for any other part.

    One reader of the wire shape, because two places ask: `keep_images` when it
    records a turn and `attach_image` when it resolves a marker.  A part that is
    not the shape is left alone rather than guessed at — an unreadable part is
    the store's note's business, not this function's.
    """
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return None
    image = part.get("image_url")
    url = image.get("url") if isinstance(image, dict) else None
    return url if isinstance(url, str) and url else None


def _recorded_into(tap: Tap, task: asyncio.Task[Any]) -> None:
    """Take up a detached record's id for the tap that turn left behind.

    Only an `int` is a turn the store can be asked about; `None` is the store
    having refused or a fork (which has no row), and leaving the tap's field
    alone is the honest answer for both — a watcher then replays the tap rather
    than trusting an id the snapshot cannot contain.

    A cancelled or failed write leaves it alone too.  `detach` already says that
    failure in the log; this callback is not the place to say it a second time.
    """
    if task.cancelled():
        return
    try:
        found = task.result()
    except Exception:  # noqa: BLE001 — the write's failure is `detach`'s to report
        return
    if isinstance(found, int):
        tap.recorded_turn = found


def _reply(
    result: TurnResult,
    *,
    model: str,
    injected: bool,
    refused: list[str],
) -> dict[str, Any]:
    """One turn's answer, in the one key set a caller is promised.

    **Built here rather than at each return, and that is the fix rather than a
    tidy-up.**  The reply used to be written out twice — once at the end of
    `send_message` and once in `_injected_reply` — and the two had already
    drifted: the injected dict had no `refused`, so a caller that read
    `reply["refused"]` got a `KeyError` on exactly the folded-in path, which is
    the one path a caller cannot recognise by anything else in the answer.  A
    stable key set is a promise, and one dict is the only way to keep it.

    `result` is never None, which is why there is no `if result else` anywhere
    below.  Every path that leaves a turn without a result has already gone —
    `_injected_reply` for a folded message, and the re-raises in `send_message`
    for a cancellation and a failure — so those branches were dead.

    **`model` is passed in rather than read off the loop**, and that is the
    second thing this signature is for.  The loop's reference is *not* the model
    that answered: `model_switch` moves it under a turn that is already running
    — that is the whole of what makes the tool the way out of a model that has
    stopped answering — so a reply built from `loop.model` here would name a
    model that never saw this message.  What is passed is the turn's own record
    of it (`Outcome.model`, taken at the boundary `loop_for` resolves the
    backend at), which is the same fact the row is written with
    (`recorded_model`) and for the same reason.

    `injected` and `refused` are the only two things that differ between the two
    callers, and both are facts about *the message being answered* rather than
    about the turn that answered it: whether its own turn ran or it rode
    another's, and which of its own sources could not be attached.  `refused` is
    empty on every ordinary turn, and a source the caller passed that could not be
    read is named there because the alternative — an answer that silently had
    fewer pictures than the call asked for — is the one thing a caller cannot
    detect and a person cannot explain.
    """
    return {
        "text": result.text,
        "usage": result.usage.to_wire(),
        "iterations": result.iterations,
        "stop_reason": result.stop_reason,
        "model": model,
        "injected": injected,
        "refused": refused,
    }


def _injected_reply(item: Pending) -> dict[str, Any]:
    """What a caller is told when a running turn folded its message in.

    **The answer that turn's own caller got, said the same way**, because there
    is only one answer to give: the model was handed both messages in one turn,
    and what it said was said to both — so `item.model`, the absorbing turn's own
    record of what answered, is the model this reply names too.  Reading the
    loop's reference here instead would let a `model_switch` during that turn
    answer this caller as a model that never saw the message.

    `usage` and `iterations` are that whole turn's, deliberately — they describe
    the
    work that answered this message, which is that turn's work and nothing else.
    `injected` is the one thing added, and it is not decoration: without it a
    caller cannot tell "my message rode in another turn" from "my turn happened
    to say the same thing", and the two call for different things to be shown.

    **`refused` is empty, and empty for a reason rather than by omission.**  Only
    a message carrying **no images** is ever folded in — both `auto_invoke` here
    and `take_waiting` refuse an image-bearing head — so there is no source of
    this caller's that could have been refused.  The key is present all the same:
    `_reply` builds one dict, and a caller must not have to know which path
    handed it the answer in order to read a field of it.
    """
    # Written together with `result` and only by a turn that produced one
    # (`Pending.model`), so by the time this caller is answered both are there.
    assert item.result is not None
    return _reply(item.result, model=item.model, injected=True, refused=[])


def main(argv: list[str] | None = None) -> int:
    """Serve the agent loop.  `slife2.libs.host.launcher` starts this.

    **The shared `main`, and this server is the reason there is one.**  The
    eleven lines below used to be written out here, which is how they came to be
    written out in `slife2.plugins.llm.embeddings_server` too and how the other
    sixteen came to share a function that two of eighteen did not call.  What is
    this server's own is the `note` — the model a *new* conversation starts on is
    the one fact about this start that no other line carries, and it is the
    thing a person checking "did my `--model` take" is looking for.

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
