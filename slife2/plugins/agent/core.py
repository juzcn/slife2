"""The agent server's core: the conversations, and the turn that runs on one.

`AgentCore` is the state and machinery `slife2.plugins.agent.server` builds and
serves: the loops table (one conversation per key, created on first use), the
lock and inbox that make one turn at a time a fact rather than a rule, and the
turn machinery around them — `send_message`, `run_turn_into`, and the
watch/transcript reads.

**Three subsystems sit beside it, and they take this module's narrow surface**
(`loops`, `current()`, `this_home()`) rather than a share of one closure:
`model_tools` edits the config a conversation resolves from, `recording` writes
a turn and keeps the live list in step, and `membership` makes and unmakes a
subagent.  SDD §5.1 says what each owns.

**The removal claim is the loops table's, not each tool's.**  A name whose
directory is being deleted must not start a conversation — its first turn would
save into the directory the removal then deletes — so `Conversations` holds the
claim and refuses it on the path that creates a loop (`start`), and every
reader of it asks the same object.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, ItemsView, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

import slife2.helpers.logcontext as logcontext
import slife2.helpers.timeouts as _T
from slife2.clients.toolclient import (
    ToolListing,
    acquire_tool,
    remote_catalogue,
    tool_lookup,
)
from slife2.helpers.clock import now
from slife2.helpers.threads import off_loop
from slife2.libs.harness import dumps
from slife2.libs.harness.async_tasks import (
    CANCEL_ASYNC,
    CHECK_ASYNC,
    Deliver,
    cancel_async,
    check_async,
)
from slife2.libs.harness.dumps import Compress
from slife2.libs.harness.handover import DeliveryFailed, hand_over
from slife2.libs.host.config import CABINET_SERVER_NAME, Config
from slife2.libs.host.paths import home, subagent_file
from slife2.libs.host.subagents import subagent_spec
from slife2.libs.plugin.audience import (
    FOR_THE_MODEL,
    ConversationId,
    ConversationKey,
    conversation_key,
    conversation_meta,
    request_conversation,
    request_meta,
    turn_of,
)
from slife2.libs.plugin.protocol import (
    close_server,
    conversation_reader,
    describe,
    open_server,
    tool_payload,
    unreadable,
)
from slife2.libs.plugin.vocabulary import (
    NOTHING_WAITING,
    TRANSCRIPT,
)
from slife2.libs.turn import attachments, compact, plan
from slife2.libs.turn.context import (
    LIVE_REPLY,
    RECALL_REPLY,
    failure_text,
    turn_failed_note,
    turn_prompt_lead,
    with_lead,
    without_lead,
)
from slife2.libs.turn.events import (
    ToolApprovalRequested,
    ToolsCounted,
    TurnEvent,
    TurnObserver,
    emit,
    encode,
)
from slife2.libs.turn.loop import AgentLoop, AutoCall, TurnResult
from slife2.libs.turn.messages import Message, ToolCall, Usage
from slife2.libs.turn.prompt import (
    opening_messages,
)
from slife2.libs.turn.runtime import (
    ModelRef,
    TurnRuntime,
    measured_tokens,
)
from slife2.libs.turn.tools import (
    ATTACH_IMAGE,
    CHECK_NEW_INPUT,
    CONTEXT_REBUILD,
    CONTEXT_REBUILD_TEXT,
    TURN_COMPACT,
    TURN_COMPACT_TEXT,
    CatalogueAnswer,
    Tool,
    ToolFailed,
    ToolRegistry,
)
from slife2.libs.turn.watch import (
    Closed,
    Snapshot,
    TurnStarted,
    WatchItem,
)
from slife2.libs.turn.watch import (
    encode as encode_watch,
)
from slife2.libs.turn.watchhub import Tap, WatchHub

from .recording import Recorder
from .state import Loop, Outcome, Pending, PendingRecord

logger = logging.getLogger(__name__)

#: How many messages one loop may have waiting.  The client's per-call timeout
#: starts when the call is made, not when its turn starts, so an unbounded queue
#: is an unbounded wait — and a wait longer than the timeout closes the stream
#: and cancels the turn, which is the very way a message gets lost.  A bound is
#: what keeps "queued" from meaning "eventually dropped".
MAX_QUEUED = 32

#: How a conversation's results get fitted into it: whose conversation it is,
#: in return for the callback the loop asks.  A *builder* rather than the
#: callback, because the dump is written into that conversation's cabinet and
#: the key is what names which one.
Compressor = Callable[[ConversationId], Compress]

# The call the subagent network answers for a member that no longer exists is
# `slife2.libs.plugin.vocabulary.FORGET`, imported by `membership`: it is not a
# tool the model has, and its only caller is `subagent_remove`.

#: The two tools that make and unmake a subagent — the whole of what this adds to
#: the mesh's vocabulary, and two verbs rather than four (SDD §3.5): a subagent
#: is *made* and *unmade*, and everything else it can do, it does with the tools
#: an agent has.
#:
#: **`_create` rather than `_set` or `_add`**, because the act is the thing
#: itself: the call takes a free name, writes `subagent.yaml` under it, and that
#: directory is what makes the name a member.  `_set` names what a second call
#: does to one already there — its model and role — which is the smaller half of
#: the tool; `_add` names the maker's list rather than the subagent, and the list
#: is only how the directory is found.
SUBAGENT_CREATE = "subagent_create"
SUBAGENT_REMOVE = "subagent_remove"


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


def log_turn(
    loop: Loop,
    outcome: Outcome,
    turn_id: int | None,
    failure: BaseException | None,
    token: str = "",
) -> None:
    """One line per turn: what it ran on, what it cost, and how it ended.

    It is written on every ending, and the ending is in `stop=`: a model's own
    reason, `cancelled` for a turn that was interrupted, or `failed` with the
    exception's own words.  `turn=-` is a real value, not a missing one — the id
    does not exist until the record lands, and a cancelled turn often never
    reaches that point; the context plugin logs the id when the row does land,
    and the conversation and times are what join the two.

    **`token` is the turn's own id, minted before it started.**  It is the one
    value every process's lines for this turn carry (`logcontext.new_turn`), so
    it joins the model and context hops to this line; `turn` is the stored row's
    rowid, which exists only once the row lands.  A cancelled turn has a token
    and no rowid, which is why the two are reported side by side.

    **The whole bill, on one line.**  Prompt and completion are the two halves,
    and `cache_read`/`cache_creation` are their breakdown — the numbers a
    cache-hit ratio needs.  A cancelled turn reports the calls that completed
    before the cut; zero means no call came back, not that the turn was free.

    **No prompt or answer text.**  The transcript is where words live; this line
    is the operator's index into it, so it carries counts and times, never the
    material — the same rule `slife2.helpers.logcontext` applies to the prefix.
    """
    if not outcome.started_at:
        # A message folded into a running turn: it ran no turn of its own, and a
        # line saying one ran would be the log's version of a false positive.
        # Every other ending has a turn — including one that died before the
        # loop could open, whose clock `keep_opening` takes.
        return
    result = outcome.result
    # **A failure outranks a result.**  The refused-record path has an answer
    # in hand — the model did reply — but rolls the turn back and raises, so
    # the line must say `failed` with the refusal rather than the stop reason
    # of a turn the conversation no longer holds.
    if failure is not None:
        ending = (
            "cancelled" if isinstance(failure, asyncio.CancelledError) else "failed"
        )
    elif result is not None:
        ending = result.stop_reason or "stop"
    else:
        ending = "failed"
    # **From the outcome, not the result.**  A cancelled turn has no result but
    # did spend the model calls that completed before it was cut off, and the
    # loop keeps that total (`Outcome.usage`), so reporting zero here would drop
    # a cancellation's cost from both the log and the row.
    usage = outcome.usage
    logger.info(
        "turn_done agent=%s subagent=%s turn=%s token=%s model=%s stop=%s "
        "iterations=%d prompt_tokens=%d completion_tokens=%d cache_read=%d "
        "cache_creation=%d messages=%d elapsed=%.1fs%s",
        loop.agent,
        loop.subagent or "-",
        turn_id if turn_id is not None else "-",
        token or "-",
        outcome.model or loop.model or "-",
        ending,
        result.iterations if result is not None else 0,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.cache_read,
        usage.cache_creation,
        len(outcome.messages),
        outcome.elapsed,
        f" error={failure_text(failure)}" if failure is not None else "",
    )


@dataclass(frozen=True)
class StoredConversation:
    """What a window or a watcher opens with: the turns, and the plan as it stands.

    **Answered together because the two readers need one frame.**  The turns
    are the context store's and the plan is the conversation's own file
    (`slife2.libs.turn.plan`); `transcript` and `watch`'s snapshot must agree
    about both, or a window and a watcher opened beside it would draw one
    conversation two ways.
    """

    turns: list[dict[str, Any]]
    plan: list[plan.PlanItem]


class Conversations:
    """The loops this server holds, and the names whose directory is going.

    **The two are one table because they are one rule**: a name being removed
    must not start a conversation, or its first turn saves into the directory
    the removal is about to delete and recreates it without a record.  The
    claim is taken synchronously before a removal's first await, and it is
    refused here — on the path that creates a loop — so no tool has to
    remember it.
    """

    def __init__(self) -> None:
        self.loops: dict[ConversationKey, Loop] = {}
        self._removing: set[ConversationKey] = set()

    def get(self, key: ConversationKey) -> Loop | None:
        """The conversation under *key*, or None if nothing has used it."""
        return self.loops.get(key)

    def items(self) -> ItemsView[ConversationKey, Loop]:
        """Every held conversation, by key."""
        return self.loops.items()

    def pop(self, key: ConversationKey) -> Loop | None:
        """Drop a conversation from the table and answer what was there."""
        return self.loops.pop(key, None)

    def start(
        self,
        key: ConversationKey,
        *,
        subject: str,
        factory: Callable[[], Loop],
    ) -> Loop:
        """The conversation for *key*, created by *factory* if none is held.

        **A name being removed is not a conversation that can start.**  The
        check is on the synchronous path that creates the loop, and the
        removal's claim is taken synchronously before its first await, so the
        two cannot interleave into a turn whose save recreates the directory
        the removal just deleted.
        """
        self.refuse_removal(key, subject=subject, remake=True)
        loop = self.loops.get(key)
        if loop is None:
            loop = factory()
            self.loops[key] = loop
        return loop

    def refuse_removal(
        self, key: ConversationKey, *, subject: str, remake: bool = False
    ) -> None:
        """Refuse *key* if its directory is being deleted.

        One reader for every path that must not touch a name mid-removal: a
        message starting a conversation, and a `subagent_create` making it
        again.  `remake` adds the way back — a removed subagent can
        be made again — which only a caller that is *creating* one has a use
        for.
        """
        if key not in self._removing:
            return
        if remake:
            raise ToolError(
                f"{subject} is being removed; wait for the removal to finish, "
                f"or make it again with {SUBAGENT_CREATE}."
            )
        raise ToolError(f"{subject} is being removed; wait for the removal to finish.")

    @contextmanager
    def removal(self, key: ConversationKey, *, subject: str) -> Iterator[None]:
        """Claim *key* for a removal, and release it whatever the outcome.

        **Synchronous, before the first await.**  A message that arrives while
        the network notification or the delete is in flight checks this claim
        on the loop-creating path, so it cannot start a turn whose save
        recreates the directory.  One removal at a time per name: a second
        concurrent removal would walk a tree the first is deleting and report
        its vanished files as held open, and the first's `finally` would release
        the claim while the second still deletes.
        """
        if key in self._removing:
            raise ToolError(
                f"{subject} is already being removed; wait for that removal to finish."
            )
        self._removing.add(key)
        try:
            yield
        finally:
            self._removing.discard(key)


class AgentCore:
    """One agent server's conversations, and everything a turn of one needs.

    The surface the sibling modules take is deliberately narrow: `loops` (the
    table, by key), `current()` (the config the call in front of us is served
    from) and `this_home()` (the first half of a key).  The rest of this class
    is the turn machinery those three make possible, and the cabinet this
    server reaches directly rather than through the hub.
    """

    def __init__(
        self,
        runtime: TurnRuntime,
        *,
        cabinet_client: Client | None = None,
        window_client: Client | None = None,
        backend_name: str | None = None,
    ) -> None:
        #: **The middle of a turn**, shared with the agent-task server: the model
        #: clients, the context and hub connections, the restore, the rebuild and
        #: the trim.  What is this server's own is the store (`loops`), the inbox,
        #: the record and the delivery policy.
        self.runtime = runtime
        #: The peer a background result is *handed* to rather than asked of — the
        #: window (`slife2.libs.harness.handover`).  Injected only by a test: in
        #: production it is opened per hand-over, because that is once per finished
        #: call rather than once per turn.
        self.window_client = window_client
        self.conversations = Conversations()
        #: The turn in flight and every open `watch` call, keyed by conversation.
        #: **The mechanism is `slife2.libs.turn.watchhub`'s**, shared with the
        #: agent-task server: the key is this server's `(home, agent, subagent)`
        #: triple, and the table itself holds at most one turn per conversation,
        #: replaced at the next turn's start.
        self.watching: WatchHub[ConversationKey] = WatchHub()
        #: Writes detached from a cancelled turn.  Kept so they are not collected
        #: mid-flight, and drained on the way out.
        self.background: set[asyncio.Task[Any]] = set()
        #: The cabinet, on the same terms as the runtime's peers.  It is reached
        #: for a tool result too large for the conversation it was called in, and
        #: for a turn's images.
        self._cabinet_conn: Client | None = cabinet_client
        #: And whether *we* opened it, which the lifespan needs for the same
        #: reason it needs the runtime's: this client has no `clients` entry to be
        #: swept up with (it is a peer this server reaches itself, not a model
        #: server), so a connection left in it is one the next session is handed
        #: already shut.
        self._cabinet_owned = cabinet_client is None
        #: One opener at a time for the cabinet, the one peer this server still
        #: opens itself: two turns that both need it would otherwise build two
        #: clients for one server.
        self._cabinet_opening = asyncio.Lock()
        self._plan_conversation = conversation_reader("plan to write", "the plan")
        self.recorder = Recorder(
            memory=runtime.memory,
            cabinet=self.cabinet,
            backend_name=backend_name,
        )

    @property
    def loops(self) -> dict[ConversationKey, Loop]:
        """Every conversation, by key — the surface the sibling modules read."""
        return self.conversations.loops

    def current(self) -> Config:
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
        The runtime owns the rule; this is the name the server's own code uses.
        """
        return self.runtime.current()

    def this_home(self) -> str:
        """The home the call in front of us is for: the first half of a loop key.

        **Read inside a call, which is the only place it is the caller's.**
        `ServeTheCallersHome` (`slife2.libs.plugin.protocol`) has put the caller's
        directory around the whole message, so `conversation_key` answers with *their*
        home rather than this daemon's — and this process serves every home on the
        machine, so the difference decides whether an edit reaches a conversation
        somebody else is having.

        The empty identity beside it is deliberate: all that is wanted here is the
        home, and it is read from the one function that defines what a loop is
        filed under rather than from `paths.home` beside it — so a key and a
        filter can never disagree about where a conversation belongs.
        """
        return conversation_key("")[0]

    async def cabinet(self) -> Client:
        """The client for the cabinet, opened on first use.

        **A direct peer and not a hub route.**  The three cabinet tools this
        process needs are the harness's and carry no mark — `dump_save`,
        `image_save` and `image_source` — and the hub holds rows only for what
        the model may see, so the harness reaches the plugin itself, exactly as
        `slife2-context` reaches a model backend.  All three travel over this
        one client.  The model's own cabinet readers, `dump_read` and
        `dump_grep`, are marked and reach it through the hub.
        """
        if self._cabinet_conn is not None:
            return self._cabinet_conn
        async with self._cabinet_opening:
            if self._cabinet_conn is None:
                self._cabinet_conn = await open_server(
                    self.current().server("cabinet").url,
                    name=CABINET_SERVER_NAME,
                    fallback_tool="dump_read",
                )
            return self._cabinet_conn

    async def close(self) -> None:
        """Release what this server opened, and drain the writes it detached.

        The lifespan's whole teardown.  Closed *and* forgotten: this hook is not
        the process's lifetime over the in-memory transport, so a client left
        in place here is one the next session is handed already shut — every
        turn of it failing at its first hop.  `runtime.close` is what lets its
        peers open again; the cabinet is this server's own for the same reason.
        """
        # A detached write exists because a turn was cancelled; letting the
        # process exit on top of it would lose the record it was written for.
        # Bounded, because a store that has stopped answering must not hold up
        # the shutdown.
        if self.background:
            await asyncio.wait(list(self.background), timeout=_T.timeouts.grace.drain)
        await self.runtime.close()
        # The third client this server opens for itself, for the same rule:
        # `cabinet()` is reached by a turn whose result is past
        # `tool_result.bytes` and by a turn carrying images, and a cabinet left
        # open across a session boundary is handed to the next session already
        # shut — every oversized result then calling `dump_save` on a shut
        # client, every image `image_save`.  `compress_for` deliberately does
        # not catch a failed write ("nothing else here falls back"), so that is
        # not a lost dump: it is every such turn failing at its first hop.
        if self._cabinet_owned and self._cabinet_conn is not None:
            await close_server(self._cabinet_conn)
            self._cabinet_conn = None

        # `conversations` is deliberately **not** cleared here.  It is state the
        # process owns, and this hook is not the process's lifetime: over the
        # in-memory transport FastMCP runs the lifespan once per client
        # session, so clearing here would empty the registry every time a
        # caller disconnected — which is precisely what a loop is supposed to
        # survive.  The dictionary goes away with the process, which is the
        # only lifetime it has.

    async def attach_sources(
        self, conversation_id: ConversationId, wanted: list[str]
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
        cabinet, and both end as the same `image_url` part.  The policy is the
        library's (`slife2.libs.turn.attachments.resolve_sources`); what is this
        server's is the connection it asks through.
        """

        async def lookup(
            keys: list[tuple[str, str]],
        ) -> tuple[list[tuple[str, str]], list[str]]:
            try:
                client = await self.cabinet()
            except Exception as exc:  # noqa: BLE001 - an unreachable cabinet is each key's refusal
                return [], [
                    attachments.cabinet_unreachable(refusal, exc)
                    for _key, refusal in keys
                ]

            async def fetch(key: str) -> str | None:
                answer = await client.call_tool(
                    "image_source",
                    {"key": key},
                    meta=conversation_meta(*conversation_id),
                    timeout=_T.timeouts.work.hub,
                )
                payload = tool_payload(answer)
                if "url" not in payload:
                    raise unreadable("the cabinet", "return an image source", payload)
                url = payload.get("url")
                return url if isinstance(url, str) and url else None

            return await attachments.from_keys(keys, fetch)

        # A model's path is relative to its data directory — the working
        # directory its own tools run in — so a path a tool printed is one
        # `attach_image` can open.
        return await attachments.resolve_sources(wanted, base=home(), lookup=lookup)

    def compress_for(self, conversation_id: ConversationId) -> Compress:
        """How one conversation's tool results are fitted into it.

        **A fixed rule, not a model call** (SDD §5.2.4).  Under
        `tool_result.bytes` the result goes in as it came.  Over it the whole
        output is saved as a dump — a file, keyed by the call — and the
        conversation carries the head of it and the address: the `[DUMP:…]`
        marker, the size of the file, and the readers that open it.

        **The bound is a size, not a token count.**  One UTF-8 byte length
        decides this, the same number the cabinet's readers cap themselves with;
        no vocabulary is touched on this path.

        **Saving is the one step that must succeed.**  The marker promises a
        file; a conversation carrying a marker whose file is not there would be
        a bound with no way back, and it would look like success.  So a cabinet
        that cannot answer fails the turn.  There is no fallback: nothing here
        guesses what went wrong.
        """

        async def compress(call: ToolCall, text: str) -> str:
            window = self.current().tool_result.bytes
            if dumps.text_size(text) <= window:
                return text
            client = await self.cabinet()
            answer = await client.call_tool(
                "dump_save",
                {"call": call.id, "output": text, "tool": call.name},
                meta=conversation_meta(*conversation_id),
                timeout=_T.timeouts.work.hub,
            )
            payload = tool_payload(answer)
            where = payload.get("path")
            size = payload.get("size")
            if (
                not isinstance(where, str)
                or not where
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
            ):
                # The row was written, but the answer that names its file is
                # not this build's shape — a daemon from another build, which
                # is a harness failure and not a tool the model can correct.
                raise unreadable(
                    "the cabinet", "return the dump's path and size", payload
                )
            return dumps.window(
                call=call.id, text=text, where=where, size=size, budget=window
            )

        return compress

    async def turn_tools(self, conversation_id: ConversationId) -> ToolListing:
        """What the model may call, and how big the catalogue is, asked of the hub.

        Asked **before every model call**, not once per turn: the turn's tool
        list goes out with each request to the model, so each request asks
        afresh.  One loopback round trip on a path that already exists, and what
        it buys is a list that is live — a tool server that finished starting,
        or grew a tool, is in the next *call*'s list, and nothing here had to
        notice that it happened.

        The answer can be at most one call old, and nothing has to be kept in
        step to make that true: no `tools/list_changed` subscription, no shared
        catalog and no reconcile pass.

        It is also asked once when the loop is built, before the user's message
        is appended: a hub that is not there is a system that has come apart, and
        the moment to find that out is before the conversation has been touched.

        **The tools come back bound to `conversation_id`**, so a tool that reads the db
        reads *this* conversation's.  That is the one thing this process knows
        and the model does not, and it is why the binding happens here rather
        than in an argument — see `slife2.clients.toolclient`.

        The answer carries the catalogue's size as well as the list, because the
        start-up ask is the one that refreshes the declarations and therefore the
        only moment the two numbers are both current.  A turn's ask reads what
        that build produced and starts a background refresh; it must not wait for
        one, because a declarations refresh can outlast `work.hub` while external
        sources are connecting.  See `registry`, which is where the size is
        reported to a display.
        """
        return await remote_catalogue(
            await self.runtime.hub(), conversation_id, wait=False
        )

    async def registry(
        self, conversation_id: ConversationId, observer: TurnObserver | None = None
    ) -> ToolRegistry:
        """What the model may call: the hub's list, bound to this conversation.

        **One source.**  Every model tool — `check_async` and `cancel_async`
        included — is a catalogue row the hub advertises.  The two background
        tools are *served* here because the task table is this process's memory
        (`slife2.libs.harness.async_tasks`), but the hub catalogues them and
        `tool_load.autoload` keeps them loaded like any other model tool; there
        is no second list beside `found.tools`.

        **`observer` is where the catalogue's size goes**, and it is optional
        because this is also called to build a loop before a turn exists
        (`loop_for`) — there is no display to tell yet, and the window already
        has a count from the start-up ask (`slife2._open_catalogue`).  When the
        loop re-reads the list before a model call it passes its own observer,
        so the bar follows the hub rather than a snapshot from boot.

        **The count is the request's list, and the hub is its only source.**
        `loaded` is the hub's loaded set and `total` the catalogue it was chosen
        from: a clean pair (events.py says why).
        """

        async def catalogue_has(name: str) -> CatalogueAnswer:
            """What the catalogue says about a name outside this request's list.

            The registry's error path asks, so a name the budget unloaded is
            told to load, a name the machine does not have is told to search,
            and a name the catalogue is still being written for is told to retry
            — the three sentences `ToolRegistry._unknown` keeps apart.
            """
            return (
                await tool_lookup(await self.runtime.hub(), name, conversation_id)
            ).answer

        async def catalogue_acquire(
            name: str, arguments: dict[str, Any]
        ) -> Tool | None:
            """The same name, made runnable when the call fits its current schema.

            `acquire_tool` loads it — the tool is in the next request's list —
            and the call runs in this turn.  `None` hands the name back to the
            three sentences, which is where a call that no longer fits the row,
            or a name the hub will not load, belongs.
            """
            return await acquire_tool(
                await self.runtime.hub(), name, arguments, conversation_id
            )

        found = await self.turn_tools(conversation_id)
        if observer is not None:
            await emit(
                observer,
                ToolsCounted(total=found.total, loaded=len(found.tools)),
            )
        return ToolRegistry(
            found.tools, exists=catalogue_has, acquire=catalogue_acquire
        )

    async def loop_for(
        self,
        loop: Loop,
        conversation_id: ConversationId,
        approve: Callable[[ToolCall], Awaitable[bool]] | None = None,
        deliver: Deliver | None = None,
        compressor: Compressor | None = None,
        observer: TurnObserver | None = None,
    ) -> AgentLoop:
        """The agent loop for a conversation, bound to that conversation.

        **The middle is the turn runtime's** (`slife2.libs.turn.runtime`): the
        model backend, its per-server connection cache, and the key that names
        this conversation on every call.  What is this server's is the two
        registry builds — the first without the observer, because there is no
        display to tell yet, and the refresh with it, so the catalogue's size
        follows the hub rather than a snapshot from boot (`registry`).
        """
        return await self.runtime.make_loop(
            conversation_id,
            ModelRef(loop.model, loop.provider, loop.api, loop.model_id),
            partial(self.registry, conversation_id),
            refresh=partial(self.registry, conversation_id, observer),
            # The conversation's own, taken from the config it started on
            # (`Loop.max_iterations`): an iteration cap somebody raises
            # *because* a turn ran into it reaches the next conversation, which
            # is the rule the model follows too.
            max_iterations=loop.max_iterations,
            approve=approve,
            deliver=deliver,
            # Built here rather than handed in, because the conversation's key
            # is what names the cabinet the dump goes into.  None when the
            # caller did not ask for one, and a result then goes in as it came —
            # see `AgentLoop._fit`.
            compress=None if compressor is None else compressor(conversation_id),
            # **The projection and the state are both this loop's.**  A turn's
            # compaction hides messages from `loop.hidden`; the list the loop
            # advances, the record and the store never see that view.
            present=lambda messages: compact.view(messages, loop.hidden),
        )

    def reap(self) -> None:
        """Drop conversations nothing has asked anything of for a while.

        In-process state only.  Nothing a caller can observe depends on this — a
        key is recreated on the next message that uses it and restores what it
        had — which is the whole reason the sweep is allowed to be this casual.

        A conversation with a turn in flight is never idle, whatever its
        timestamp says: its own turn refreshes `last_used` when it finishes.
        """
        cutoff = time.monotonic() - _T.timeouts.pacing.loop_idle
        # The key is a `conversation_key` triple, so it is *not* unpacked: the words
        # that name a conversation come from the loop (`agent`, `subagent`),
        # which is also what keeps the home out of a log line.
        for key, loop in list(self.conversations.items()):
            if loop.lock.locked():
                continue
            if loop.last_used < cutoff:
                logger.info(
                    "reaping idle conversation %s",
                    describe((loop.agent, loop.subagent)),
                )
                self.conversations.pop(key)
                # The tap goes with it: its turn is in the store (a turn's
                # record is awaited before its lock is released), and a watcher
                # still attached reads what it needs from that store and then
                # from the live queue.  Keeping it would be memory for a
                # conversation nothing is running.
                self.watching.drop(key)

    def conversation(self, agent: str, subagent: str, model: str) -> Loop:
        """The conversation for this key, created on demand.

        Created on demand rather than opened by a separate call, because a key
        that cannot go stale is worth more than the round trip it costs: there is
        no handle to carry, no lifetime to observe, and no "not found" for a
        caller to handle.  The idle sweep may have dropped the state; the caller
        cannot tell, and does not have to.

        **A pair is not answered here.**  Whether to restore is a fact about the
        *loop*, not about *this call*: two callers arriving on one brand-new key
        are one starter and one not, and a per-call flag would leave the second
        running a turn on the empty list the restore was about to replace.
        `Loop.restored` is the claim, and it is made under the lock.
        """
        client = conversation_key(agent, subagent)
        loop = self.conversations.start(
            client,
            subject=describe((agent, subagent)),
            factory=lambda: self._start(agent, subagent, model),
        )
        loop.last_used = time.monotonic()
        return loop

    def _start(self, agent: str, subagent: str, model: str) -> Loop:
        """Build one new conversation's loop, from the config as it is now."""
        # **The record is the membership, and a message does not make one.**
        # A subagent is created with `subagent_create` (§4.5), which writes
        # `subagent.yaml`; a name with no record is a subagent that was
        # never made or one that was removed.  Starting its conversation
        # here would create `subagents/<name>/` — and the turn's save would
        # create `turns.db` inside it — with no record, which is the state
        # `subagent_create` refuses as "already a directory that is not a
        # subagent" and `subagent_remove` refuses as not one of yours: the
        # name is blocked for both until somebody cleans the filesystem by
        # hand.  The read path was fixed the same way
        # (`slife2.libs.store.db.read_store_for`); this is the write half.
        if subagent and not subagent_file(agent, subagent).is_file():
            raise ToolError(
                f"there is no subagent {subagent!r} of {agent}'s: it was "
                f"never made, or it was removed. Make it with {SUBAGENT_CREATE} "
                f"before messaging it."
            )
        live = self.current()
        # What this subagent's own file says it is — its model and its role
        # — read *here*, when its conversation starts, which is what makes a
        # `model_switch` in between reach it the way it reaches the maker's
        # own next turn.  An agent's own conversation has no such file and is
        # the defaults.  **Strictly**: a record this build cannot read must fail
        # the turn rather than silently run the subagent on the maker's model
        # and the plain role.
        spec = subagent_spec(agent, subagent, strict=True)
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
            prompt_window=settings.prompt_window,
            # A direct read before the first turn sees the model this
            # conversation was built on; `run_turn_into` refreshes them at
            # every turn start.
            turn_accepts_images=settings.accepts_images,
            turn_prompt_window=settings.prompt_window,
            turn_model=f"{name}/{settings.model}",
            max_iterations=live.agent.max_iterations,
            # The compaction threshold is a term of the conversation in the same
            # sense: an edit reaches the next conversation, not a turn already
            # running under the old number.
            compact_at=live.agent.compact_at,
            messages=messages,
            lock=asyncio.Lock(),
            last_used=time.monotonic(),
            # The head is what an empty list covers, so a conversation that
            # restores nothing carries nothing either.
            covered=len(messages),
        )
        logger.debug(
            "started %s on %s",
            describe((agent, subagent)),
            model or spec.model or "the default model",
        )
        return loop

    def detach(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
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
        self.background.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            self.background.discard(finished)
            if not finished.cancelled() and (exc := finished.exception()):
                logger.warning(
                    "recording a cancelled turn failed: %s", exc, exc_info=exc
                )

        task.add_done_callback(done)
        return task

    async def restore_into(self, loop: Loop) -> None:
        """Put a conversation back on the context it had when it last stopped.

        Called when a loop is *built* — a process that restarted, a conversation
        the idle sweep let go — and the stored list is what the conversation had
        when it stopped.

        **The store's answer is the turn runtime's** (`restore`), and adopting
        it is this server's step: a `Loop` is this server's conversation, and a
        context half the runtime's and half the loop's would not be either's.
        """
        restored = await self.runtime.restore(
            (loop.agent, loop.subagent), loop.messages
        )
        loop.messages = restored.messages
        loop.turn_ids = restored.turn_ids
        loop.covered = restored.covered
        # **The status bar's figure, taken from the turn it restored.**  A window
        # that has only restored has run no turn of its own, so `context_tokens`
        # would otherwise be 0 while the bar (which reads the same column,
        # `tui.app.context_of`) shows the last stored turn's size.  A size nobody
        # measured reads 0, the bar's own rule.
        loop.context_tokens = restored.context_tokens
        if loop.turn_ids:
            logger.info(
                "restored %d turn(s) for %s",
                len(loop.turn_ids),
                describe((loop.agent, loop.subagent)),
            )

    async def trim_tools(self, loop: Loop, observer: TurnObserver) -> None:
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

        **What went is written into the conversation by the turn runtime**
        (`slife2.libs.turn.runtime.trim`): the pair — `_func_tool_unload` called
        with no names — is one mechanism for both servers, and its two appends
        have no await between them.  The pair's own sentence is reported on this
        turn's stream by the same call, so the person sees it as a note rather
        than as a panel.  What is this server's is when to ask and
        how to report a skip: a turn boundary, because the list goes out with
        every model call, and a warning rather than a failed turn, because the
        turn is over and its next tool-list read is what surfaces a dead hub.

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
            unloaded = await self.runtime.trim(loop.messages, observer)
        except Exception as exc:  # noqa: BLE001 — bookkeeping does not fail a turn
            logger.warning(
                "%s: the tool list was not trimmed: %s",
                describe((loop.agent, loop.subagent)),
                exc,
                exc_info=exc,
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

    def take_waiting(self, conversation_id: ConversationId, turn: str = "") -> str:
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

        **`turn` is what keeps the two callers' turns apart.**  An agent task
        addresses this conversation's id too — it stands in for the same agent
        or subagent — so a call that names a turn other than the one running
        here is answered "nothing waiting" and the queue is left for the turn it
        belongs to (SDD §9.3).  A call that names no turn is the direct read
        (`turn_of` says why that is the caller's to omit).

        **The entry is claimed before it is returned**, into `loop.injected`, so
        that the turn this arrives in is the turn that answers for it: the
        caller parked on the lock reads its own entry's `result` and stops
        waiting — or, when the turn ends without one, reads `delivered` and is
        still answered as a cut-in rather than running the prompt again.  Taken
        in arrival order — the queue's first is the message that has waited
        longest.

        **`delivered` is not set here**, because being taken is not yet being in
        the conversation: the loop writes the pair after its own `_fit`, and an
        interruption or a failed fit in that window leaves this message in no
        record at all.  It is `folded_message` that marks the entry, at the
        point the pair is about to be appended with nothing awaited in between
        — so a take whose pair never lands still owes itself a turn.

        Returns `""` for nothing to deliver: no such conversation, an empty
        queue, or a head that carries images.  That last one is the single
        refusal, and it is `auto_invoke`'s too — a tool result carries text, so
        delivering one would drop what somebody attached, and the message keeps
        its place and its own turn instead.
        """
        loop = self.conversations.get(conversation_key(*conversation_id))
        if loop is None or not loop.inbox:
            return ""
        if turn and turn != loop.turn_token:
            # Another turn on the same id — an agent task of this conversation
            # is the live case — is not this loop's, and its take would steal a
            # message the running turn's own boundary is about to fold in.
            return ""
        waiting = loop.inbox[0]
        if waiting.images:
            return ""
        loop.inbox.popleft()
        loop.injected.append(waiting)
        return waiting.prompt

    async def run_turn_into(
        self, loop: Loop, item: Pending, observer: TurnObserver, outcome: Outcome
    ) -> None:
        """One turn, and the repair a cancellation needs.

        `outcome` is filled in a `finally`, *after* the repair, so it always
        holds what this turn is answerable for: the whole exchange, or the
        user's message alone when the turn was cut off.

        **The trim happens here, inside the lock.**  The pair it records has to
        be part of *this* turn — the saved record and the live history are one
        list until the snapshot is taken — and it has to be written before the
        next queued turn can append anything after it.  The price is that a
        queued turn waits for one loopback to the hub; the thing it buys is a
        transcript in which a tool the model lost is something it read in the
        turn that took it away, rather than a gap somebody has to explain.

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

        #: The loop opened for this turn, or None until it is.  Declared here
        #: because the boundary below reads its last call's bill, and assigned
        #: where the turn opens it; a failure before that is the one ending with
        #: no loop to read a bill from and no loop to trim for.
        agent_loop: AgentLoop | None = None

        async def auto_invoke() -> AutoCall | None:
            """The tool to call at this iteration boundary, or `None`.

            **The rebuild's decision is asked here, before the call.**  The
            selection the decision model returns is the `_context_rebuild`
            call's arguments, so the pair the loop writes carries them from the
            start — there is no empty call executed and rewritten afterwards.
            **Cheap when nothing is due**: the queue check and the window
            fraction are read first, and only a due rebuild or compaction
            reaches a model.  A failed decision skips the call (the turn keeps
            the context it had) and is not re-asked; the model can still call
            the tool itself with a selection of its own.

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

            **A message and a compaction can both be due, and the message goes
            first.**  A cut-in is a person waiting; a compaction can wait one
            iteration, and the message may be what makes the turn grow anyway.
            """
            if call := await self.runtime.rebuild_call(
                (loop.agent, loop.subagent),
                loop,
                agent_loop,
                loop.turn_model,
                context_tokens=measured_tokens(loop.live, loop.context_tokens),
                prompt_window=loop.turn_prompt_window,
            ):
                return call
            if loop.inbox:
                waiting = loop.inbox[0]
                if not waiting.images and (
                    agent_loop is not None and agent_loop.declares(CHECK_NEW_INPUT)
                ):
                    return AutoCall(
                        CHECK_NEW_INPUT,
                        channel=waiting.channel,
                        receipt=waiting.receipt,
                    )
            return await self.runtime.compact_call(
                (loop.agent, loop.subagent),
                loop,
                agent_loop,
                loop.turn_model,
                context_tokens=measured_tokens(loop.live, loop.context_tokens),
                prompt_window=loop.turn_prompt_window,
            )

        def injected_count() -> int:
            """How many messages this turn has taken, for the early cut-in."""
            return len(loop.injected)

        def folded_message() -> tuple[str, str] | None:
            """The channel and receipt of the message taken most recently.

            **A peek, not the commit.**  The loop asks it after the tool ran —
            `take_waiting` appends the entry after it pops it, so the last one
            is the message the call just took — and then writes the
            assistant/tool pair.  Marking `delivered` here marked the take
            before the pair existed: a later call in the same batch failing its
            `_fit` lost every answer while the person's message stayed delivered,
            and its caller was answered as folded with words the conversation
            never received.  `folded_commit` is the one place that marks, and
            the loop calls it only after the pair is in the list.

            A `_check_new_input` that answered "nothing waiting" must not wear
            the previous message's bubble, which is why the loop asks only when
            the count grew across that call.
            """
            if not loop.injected:
                return None
            item = loop.injected[-1]
            return item.channel, item.receipt

        def folded_commit(written: int) -> None:
            """The first `written` takes are in the conversation: they are delivered.

            **A count, because not every take is a delivery.**  A call
            interrupted before it answered writes `INTERRUPTED`, not the
            person's words, and its take must keep `delivered` false so its
            caller runs the turn it is owed.  The loop commits a prefix
            because takes append in arrival order.
            """
            for item in loop.injected[:written]:
                item.delivered = True

        # The monotonic clock is the duration's, and `started_at` the row's:
        # both are needed, and neither can be derived from the other without
        # reading the store's own timestamp format back (see `Outcome.elapsed`).
        started_mono = time.monotonic()
        # **The opening is the record's boundary, and a field because a rebuild
        # can move it.**  `_context_rebuild` replaces the historical prefix
        # under the running turn; the suffix from this index on is the turn's
        # own and is carried verbatim.
        loop.turn_opening = len(loop.messages)
        # **The turn's model facts are taken at the same boundary.**  A
        # `model_switch` arriving mid-turn moves the conversation for the next
        # one (`model_switch`'s own rule); this turn keeps the model — and
        # therefore the window and the image capability — it started on.
        loop.turn_prompt_window = loop.prompt_window
        loop.turn_accepts_images = loop.accepts_images
        loop.turn_model = loop.model
        # **The turn's view starts whole.**  A compaction is the turn's own: a
        # message it hid must not outlive it, and the next turn's rebuild is
        # what decides that turn's context.
        loop.hidden.clear()
        loop.compact_hold = False
        loop.rebuilt = False
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
        # **A local path is relative to the caller's data directory**, the same
        # three sources `attach_image` takes — and the same base the model's own
        # route uses.  Left to `Path`'s default, a relative path resolved
        # against the daemon's working directory (the runtime directory), so a
        # path that works in `file_save` named a different file here.
        # The encode reads and base64s up to `MAX_BYTES` per source — blocking
        # work in a process that serves every agent on the machine, so it hops.
        pairs, refusals = await off_loop(attachments.resolve, item.images, base=home())
        if pairs and not loop.turn_accepts_images:
            # **Extended, not replaced**: a caller who passed one unreadable
            # source and one the model cannot see is owed both reasons, and a
            # list that dropped the first to say the second would answer the
            # wrong question.
            refusals.extend(
                f"{source}: {loop.turn_model} cannot read images"
                for source, _url in pairs
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
        # **The turn's prompt.**  The context decision is the first boundary's
        # `_context_rebuild`, which runs after this marker is on the message and
        # reads the conversation exactly as the turn runs it — marker included,
        # which is what "the conversation is the input" means.  The marker is
        # removed in the `finally` below so the row, the next turn's rebuild and
        # the `[TURN:]` footnote never see it.  It is a prompt for this turn,
        # not a record of it.
        #
        # **The plan is read here because a rebuild cannot carry it.**  It is
        # the conversation's own file, not a turn the decision chose
        # between: this is the half of the prompt that survives whatever a
        # rebuild decides.  The context figure is not in the marker — it would
        # be the previous call's and the rebuild can change the list; the
        # turn's own estimate comes from `_context_rebuild`'s report.
        lead = turn_prompt_lead(
            plan.read(loop.agent, loop.subagent),
        )
        user = with_lead(user, lead)
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
            a client that never hears the request cannot answer it, so a request
            the stream would not take is a refusal of the call rather than a
            wait.  A client that hears it and does not answer is bounded by
            `deliver.approval`: a turn parked for ever on a future nobody sets
            is worse than a denial.  Both endings are logged and both leave the
            turn running — the call is what failed.
            """
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            loop.approvals[call.id] = future
            try:
                # **Emitted through the shared fault tolerance, not straight
                # to the observer.**  `emit` exists so that a UI which
                # throws cannot end a turn, and this event is raised from inside
                # a turn: a client whose progress stream is broken throws from
                # `on_event` (`ctx.report_progress` on a closed stream), and
                # letting that escape would kill the very turn that is waiting for
                # this answer — a tool call the model is entitled to make, refused
                # because a *display* failed, where the same observer failing on a
                # `TextDelta` would have been swallowed one layer down.  It is a
                # module function, so this closure — built before `agent_loop` —
                # asks it without depending on the instance.
                # **The answer, unlike every other event's, is read.**  Nobody
                # can answer a request that was not written, so that failure
                # refuses the call instead of parking the turn on it.
                if not await emit(
                    observer,
                    ToolApprovalRequested(call.id, call.name, call.arguments),
                ):
                    logger.error(
                        "the approval request for %s (%s) could not be written "
                        "to the progress stream — the call is refused",
                        call.id,
                        call.name,
                    )
                    return False
                return await asyncio.wait_for(
                    future, timeout=_T.timeouts.deliver.approval
                )
            except TimeoutError:
                logger.error(
                    "the approval request for %s (%s) was not answered in %.0f "
                    "seconds — the call is refused",
                    call.id,
                    call.name,
                    _T.timeouts.deliver.approval,
                )
                return False
            finally:
                loop.approvals.pop(call.id, None)

        async def deliver(text: str, channel: str) -> bool:
            """Hand a finished background call's result back to this conversation.

            **A result nobody comes back for is a result lost**, which is the one
            thing backgrounding must not do: the call was made for this
            conversation, and a model remembering to poll is not a road back.
            So it is handed to the window like every other piece of work
            this system does in the background (`slife2.libs.harness.handover`).

            **A failure here is `False`, and `False` is not "lost"**: the call
            keeps its result, `check_async` is what hands it over, and the model
            that asks for it is the model that has a window.  What is *not* done
            is running the delivery into a conversation nobody is watching:
            that is the "carry on without the window" this system does not do.

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
                    self.current(),
                    loop.agent,
                    text,
                    channel,
                    what=(
                        f"the result of a background call in "
                        f"{describe((loop.agent, loop.subagent))}"
                    ),
                    window_client=self.window_client,
                )
            except DeliveryFailed as exc:
                logger.warning(
                    "a background call's result was not delivered to %s: %s",
                    describe((loop.agent, loop.subagent)),
                    exc,
                )
                return False
            return True

        cancelled = False
        try:
            # **The loop is opened inside the turn's own `try`.**  It is the
            # turn's first step and it asks the hub for the tool list
            # (`loop_for` → `registry` → `turn_tools`), so a plugin that is not
            # answering fails *here* — before `run_turn` has appended the
            # caller's message.  Inside, the `finally` below keeps that message:
            # see it.
            agent_loop = await self.loop_for(
                loop,
                (loop.agent, loop.subagent),
                # Only a caller that said it can be asked is asked (see
                # `Pending.watched`).  With None the loop grants the request and
                # the call runs, which is the rule for a turn with nobody behind
                # it.
                ask if item.watched else None,
                deliver,
                # Every turn's results are fitted, not only the ones a window
                # has anything to say about: the callback's first act is to
                # measure, and a result that fits costs a comparison rather than
                # a call.
                self.compress_for,
                # So the tool list's size reaches the display on this turn's own
                # stream, each time the list is re-read before a model call.
                observer,
            )
            # **The running loop, for the one tool that reads its bill.**  A
            # `_turn_compact` call is answered with the last measured call, which
            # is the number the status bar is showing; the field is cleared where
            # `turn_token` is, so an idle conversation never keeps a loop.
            loop.live = agent_loop
            # The parts, built above and after the rebuild — the rebuild
            # replaces the list wholesale, so a message appended before it would
            # be destroyed, which is why `run_turn` appends this one itself.
            outcome.result = await agent_loop.run_turn(
                loop.messages,
                user,
                observer,
                auto=auto_invoke,
                folded=folded_message,
                folded_commit=folded_commit,
                injected=injected_count,
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
            # **The caller's message is this turn, and it stays in the list.**
            # `run_turn` appends it as its first act, so an unchanged list means
            # the turn died before the loop could open — a hub whose tool list
            # cannot be read is the failure this was written for, and it is the
            # one failure that used to leave nothing to record at all.  A message
            # handed to a conversation is kept however its turn ended
            # (`Recorder.remember_turn`).  Put in before the snapshot below, so the
            # record, the `[TURN:]` footnote and `roll_back` all see it as the
            # turn's own opening message.
            if len(loop.messages) == loop.turn_opening:
                loop.messages.append(Message(role="user", content=user))
            # **The trim is wrapped.**  It is a *post-turn* step, so it only runs
            # when the turn answered — but it suspends on a real call to the hub
            # (`unload_tools`), and a cancellation that lands in that suspension
            # is a cancellation of something the caller is no longer waiting for;
            # the turn is already done.  Left to unwind freely it would skip the
            # commit below, and `send_message` would then record the caller's
            # opening message and none of the work the turn did — a completed
            # turn whose answer has no row, which is the "conversation the
            # database has never heard of" SDD §5.6's rule exists to prevent.  So
            # the commit is put in a `finally` of its own: it holds no `await`,
            # so it completes even as a cancellation unwinds past it (the
            # property `slife2.plugins.agent_task.server`'s cancelled path
            # names), and the turn is settled however the trim ended.
            #
            # Skipped entirely on the cancelled path above, and not merely to
            # save a call: that `finally` is running *during* a cancellation,
            # where the next await is cancelled at its first checkpoint — so a
            # pair could be started and never finished, which is the one history
            # state that is a 400 from every provider.  `auto_invoke` refuses to
            # start for the same reason.  The turn is over; the budget
            # can wait a turn.  A loop that never opened is skipped for a
            # different reason: this turn loaded nothing, and the hub that could
            # not answer above is the one the trim would ask.
            try:
                if not cancelled and agent_loop is not None:
                    await self.trim_tools(loop, observer)
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
                        # **Only a fold the record holds gets the answer.**  A
                        # take whose pair was never written is not in this
                        # turn's messages, so handing it this result would tell
                        # its caller it was delivered and answered by a turn
                        # that never read it.  `folded_commit` marks the ones
                        # that were; the others run their own turn.
                        if not folded.delivered:
                            continue
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
                outcome.messages = list(loop.messages[loop.turn_opening :])
                outcome.completed_at = now()
                outcome.elapsed = time.monotonic() - started_mono
                # **The bill is taken from the loop, not from the result.**
                # `run_turn` returns its usage, but a cancellation has no
                # return — and the tokens a cancelled turn spent are as real as
                # any other's.  The loop updates these after each completed
                # model call and resets them when a turn begins
                # (`AgentLoop.usage`), so this reads *this* turn even on the
                # path where no result exists.
                # A loop that never opened spent nothing: the `Outcome` already
                # holds the zero bill, and the conversation's context figure is
                # the last call the conversation *did* make — resetting it to
                # zero would make a failed turn look like an empty one.
                if agent_loop is not None:
                    outcome.usage = agent_loop.usage
                    outcome.last_usage = agent_loop.last_usage
                # **The marker leaves as the turn ends**, before the record and
                # the footnote: it was prompt text for this turn only, and the
                # message it rode is the one `annotate_turn` appends `[TURN:]`
                # to after the save.  Leaving it would put the marker in the
                # row (and so in search and recall), and would make the live
                # message differ from the one a rebuild produces.
                if outcome.messages and outcome.messages[0].role == "user":
                    outcome.messages[0].content = without_lead(
                        outcome.messages[0].content, lead
                    )
                # The number the status bar will show, and the one the next
                # turn's marker reports — the last call this turn made, not a
                # sum over the turn (`TurnRecord.context_tokens`).  Only a loop
                # whose call actually completed made one: a loop that opened and
                # then failed or was cancelled before any call returned holds
                # `Usage()` (all zero), and writing that would make a failed
                # turn look like an empty conversation.  The last measured
                # figure stands instead.
                if agent_loop is not None and agent_loop.last_usage.total_tokens:
                    loop.context_tokens = agent_loop.last_usage.total_tokens

    def keep_opening(
        self, loop: Loop, item: Pending, outcome: Outcome, elapsed: float
    ) -> None:
        """The message that opened a turn stays in the conversation however it ended.

        **`run_turn` appends it, so an empty tail means the turn died before the
        loop could be opened** — a restore or a rebuild that could not be made,
        or the hub's tool list failing to arrive (`loop_for`), which is the case
        this was written for.  The failure still belongs to the caller:
        `repaired_error` says what it cost and the re-raise hands it back.  What
        this refuses is the second loss — the message was accepted into this
        conversation and then left no trace anywhere at all (SDD §5.6,
        Appendix A rule 10).

        **Put where `run_turn` would have put it**: the end of the live list, so
        the record writes it as the turn's own opening message.

        **The pictures are named, not read.**  A turn that dies here never
        resolved its sources, and an image part whose URL is a path is one a
        provider cannot fetch — so each source becomes the same `image_note` a
        failed turn's attachment leaves, and nothing touches a disk on the way
        out of a failure.  (`run_turn_into`'s own failure path is the other
        case: there the parts are resolved bytes, and `repaired_error` drops
        them.)

        The clock and the model are taken here for a turn that began before
        `run_turn_into` could take them; the row and the `[TURN:]` footnote are
        both derived from them, so the live and rebuilt spellings agree — the
        rule `annotate_turn` states for the ordinary path.
        """
        if outcome.messages:
            return
        opening = Message(
            role="user",
            content=(
                attachments.parts(item.prompt, list(item.images))
                if item.images
                else item.prompt
            ),
        )
        if item.images:
            attachments.drop([opening])
        loop.messages.append(opening)
        outcome.messages = [opening]
        outcome.started_at = outcome.started_at or now()
        outcome.completed_at = outcome.completed_at or now()
        outcome.elapsed = elapsed
        outcome.model = outcome.model or loop.model

    def repaired_error(self, loop: Loop, exc: BaseException) -> BaseException:
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

    async def send_message(
        self,
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
        iteration boundary, where it is folded in (see `take_waiting`).  Turns on one
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
                Change it with `model_switch`, which is the one edit that
                reaches a conversation already running.
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
        # **A message with neither words nor an image is nothing.**  It used to
        # enter the inbox, be taken by a running turn, and leave `take_waiting`
        # with an empty prompt: the tool answered "Nothing waiting" while the
        # entry was marked delivered, so the model was told no message arrived
        # and the caller was answered as folded with no words.  Refused at the
        # boundary instead.
        if not prompt.strip() and not images:
            raise ToolError(
                "a message needs some words or an image; this one carries "
                "neither, so there is nothing to send."
            )
        # Asked before anything is spent.  A context plugin that is not there is
        # a broken system rather than a degraded one, and the moment to find that
        # out is *before* the first model call has been paid for — not at the
        # write, when the answer exists and has nowhere to go.
        await self.runtime.memory()
        self.reap()
        loop = self.conversation(agent, subagent, model)
        # The key is what the watcher registry and the tap are filed under.
        # Taken from the one function that defines a conversation's identity, so
        # a watcher and the turn it is watching cannot disagree about it.
        key = conversation_key(agent, subagent)

        if len(loop.inbox) >= MAX_QUEUED:
            raise ToolError(
                f"{describe((agent, subagent))} already has {len(loop.inbox)} "
                f"messages waiting, which is the limit.  Wait for them, then send "
                f"this one."
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
        # **Both are read by the summary in `finally`, across a re-raise.**  The
        # failure is captured rather than left to `sys.exc_info()` there, because
        # a `finally` after an `except` that re-raises cannot be asked reliably
        # what it is unwinding; the id is initialised here because a turn that
        # never records (a refused write) has none to bind.
        failure: BaseException | None = None
        turn_id: int | None = None
        #: This turn's token and the handle that puts it back when the turn is
        #: over.  Empty and unset until a turn of this message actually starts:
        #: a message folded into a running turn runs no turn of its own and has
        #: no token to report.
        turn_token = ""
        turn_handle: contextvars.Token[str] | None = None
        try:
            async with loop.lock:
                # **A message queued behind a removal does not run.**  It
                # appended itself to this loop's inbox before waiting, so the
                # check has to come after the lock is in hand; the `finally`
                # below takes the entry out again.  Running it would save a turn
                # under a subagent whose directory has been deleted, recreating
                # it without a record.
                if loop.removed:
                    raise ToolError(
                        f"{describe((agent, subagent))} was removed; this message "
                        f"was not delivered."
                    )
                # **The handler holding the lock is the handle an interrupt
                # cancels** (`Loop.turn_task`), registered for the whole of the
                # locked region: a turn is the work under this lock, whatever
                # step it is on.  A message that turns out to be folded clears
                # it on the way out through the same identity guard.
                loop.turn_task = asyncio.current_task()
                # **The previous turn's record is taken up before this caller is
                # told anything.**  A message folded into that turn was answered
                # by it only if its row landed; a refused write is rolled back
                # here and un-answers the folded entries, so the check below
                # sees `None` and this message runs its own turn instead of
                # being handed an answer the conversation no longer holds.  It
                # also has to precede the restore and rebuild, which read
                # `turn_ids` and `covered` (`Recorder.settle_record`).
                await self.recorder.settle_record(loop)
                # **The turn that just released the lock may have taken this
                # message with it**, folding it in at one of its iteration
                # boundaries
                # (`take_waiting`).  Then there is no turn here to run: the model was
                # handed this message inside that one and its answer is the
                # answer to this, so the caller is given exactly what that turn's
                # own caller got.  Asked of the *entry* rather than inferred from
                # the queue, because a turn that produced no result leaves
                # `result` as `None` — and only `delivered` then says whether the
                # fold survived into the record or the message still owes itself
                # a turn, which is what the rest of this block is for.
                if item.result is not None or item.delivered:
                    return _injected_reply(item)
                # **Ours, and its turn is starting now.**  A message waiting in
                # the inbox is *not* in `messages`: `AgentLoop.run_turn`
                # re-reads the list every iteration, so an early append would be
                # seen by the running turn as steering nobody asked for.
                with contextlib.suppress(ValueError):
                    loop.inbox.remove(item)
                # **The turn's clock, for the one ending that never reaches
                # `run_turn_into`'s own** — a failure before the loop is built,
                # where `keep_opening` still has to give the row a duration.
                turn_began = time.monotonic()
                # **One token for one turn**, minted where the turn starts
                # rather than where the message arrived: a message folded into
                # a running turn returned above.  It is on every line this turn
                # writes — here, in the context store, in the model backend —
                # and `turn_done` carries it beside the rowid.
                turn_token, turn_handle = logcontext.begin_turn()
                loop.turn_token = turn_token
                # **The turn is opened for watchers here**, before its first
                # observable step: `_context_rebuild` is the first boundary's
                # harness pair, and a watcher that joined while it ran must find
                # that pair in the tap rather than before a turn it belongs to.
                # The message is the one frame a watcher never sent and so
                # cannot otherwise see.
                #
                # **Inside the `try`, because the restore is inside it too.**
                # From the moment this message left the inbox its turn is one to
                # keep, and a conversation that cannot be put back is the first
                # way that turn can fail (`keep_opening` in the handler says
                # why the message survives that).  `tap` is None until the
                # announcement, so the handler can tell a turn announced from one
                # that died before it could be.
                tap: Tap | None = None
                try:
                    # **Inside the lock, and here rather than when the loop is
                    # built.**  A restore replaces the message list wholesale, so
                    # it has to happen before the turn that runs on it — and it
                    # has to happen under the lock, because two callers can
                    # arrive on one brand-new key at once: one creates the loop
                    # and the other does not, and a restore awaited outside the
                    # lock lands *underneath* whichever of them got in first,
                    # reassigning the list a running turn is reading.  The claim
                    # is on the loop, for the reason `Loop.restored` gives.
                    if not loop.restored:
                        await self.restore_into(loop)
                        loop.restored = True
                    tap = self.watching.start_tap(
                        key, TurnStarted(item.prompt, item.channel)
                    )
                    observer = ProgressObserver(
                        ctx, publish=partial(self.watching.publish, key)
                    )
                    # **The rebuild is not here any more.**  It is the first
                    # boundary's `_context_rebuild`, a harness tool whose pair
                    # reports the decision to the model and the transcript.  A
                    # rebuild that cannot be made is a tool result the model
                    # reads, not a turn that never starts.
                    await self.run_turn_into(loop, item, observer, outcome)
                except BaseException as exc:
                    failure = exc
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
                    # **The turn's opening message is in the conversation before
                    # it is repaired or recorded.**  A turn can die before
                    # `run_turn` has appended it — the restore, the rebuild and
                    # the loop's opening are all steps that fail ahead of it —
                    # and `keep_opening` is what makes those failures a turn with
                    # a record instead of a message nobody kept.
                    #
                    # **Before `repaired_error` as well**, so a picture the
                    # caller sent is dropped into its note by the same repair
                    # every other failed turn's attachment goes through.
                    self.keep_opening(
                        loop, item, outcome, time.monotonic() - turn_began
                    )
                    # **A turn that cannot go on ends with the harness saying
                    # so.**  A tool's failure is text in the result the model
                    # reads and the turn carries on; an exception is the turn
                    # itself stopping, and a conversation that ended on an
                    # unanswered question with no account of why is one the
                    # model can only guess about — including after a restart,
                    # which is why this is a message and not a one-shot note.
                    # The marker keeps it in the harness's voice: capitals are
                    # the harness's (`turn_failed_note`), so the model does not
                    # read it as prose it wrote.  A cancellation is not an error
                    # — the caller stopped the turn — and there is nothing to
                    # report.
                    if isinstance(exc, Exception):
                        ended = Message(
                            role="assistant",
                            content=turn_failed_note(failure_text(exc)),
                        )
                        loop.messages.append(ended)
                        outcome.messages.append(ended)
                    # **Repaired before it is recorded**, so the row and the live
                    # conversation are one list and not two: the record writes
                    # `outcome.messages`, which holds this same list's own
                    # objects, and a write that serialised the refused bytes
                    # while the live list had already let them go is the
                    # disagreement `repaired_error` exists to prevent.
                    repaired = self.repaired_error(loop, exc)
                    written = self.detach(
                        self.recorder.record_into(tap, loop, item, outcome)
                    )
                    # **The id reaches the tap when the write does**, and
                    # that is what lets a watcher tell whether the snapshot
                    # it has just read already contains this turn: the same
                    # turn can be in the store and in the tap, and drawing
                    # both is the duplicate this field exists to prevent.
                    # `Tap.writing` is the half that closes the window between
                    # the store's commit and the field being set — a watcher
                    # waits it out instead of guessing.  A turn that died
                    # before its announcement has no tap to tell.
                    if tap is not None:
                        tap.writing = written
                    # An id backs a *prefix*, so it can only be claimed when
                    # this turn began on a wholly backed list — where it began
                    # being `began`, derived rather than kept because
                    # `keep_opening` may have put the opening message in here.
                    # The field is the opening's index, moved by a rebuild's
                    # prefix replacement.
                    began = loop.turn_opening
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
                                exc_info=refusal,
                            )
                            self.recorder.roll_back(loop, outcome)
                            if tap is not None:
                                self.watching.drop_if(key, tap)
                        else:
                            if turn_id is not None and loop.covered == began:
                                loop.covered += len(loop.messages) - began
                                self.recorder.address_turn(loop, outcome, item, turn_id)
                    elif loop.covered == began:
                        # **An interruption is claimed once its row lands.**
                        # The picture is in the cabinet, so an id no longer
                        # risks the retry image; a refused write is a hard
                        # failure and `settle_record` rolls the tail back.
                        loop.record_pending = PendingRecord(written, outcome, item)
                    if repaired is exc:
                        raise
                    raise repaired from exc
                else:
                    # **The write is a task before it is awaited**, so a
                    # cancellation landing in the save does not throw the turn
                    # away: the task outlives this scope and the next turn takes
                    # its id up (`record_pending` / `settle_record`).
                    written = self.detach(
                        self.recorder.record_into(tap, loop, item, outcome)
                    )
                    tap.writing = written
                    try:
                        turn_id = await asyncio.shield(written)
                    except asyncio.CancelledError:
                        # The turn itself succeeded; its messages stay and the
                        # row is on its way.  The next turn writes the footnote —
                        # but only a turn that began on a wholly backed list can
                        # have its id claimed (the same prefix rule as above).
                        if loop.covered == loop.turn_opening:
                            loop.record_pending = PendingRecord(written, outcome, item)
                        raise
                    except Exception as refusal:  # noqa: BLE001 - a refused write is a hard failure
                        # **A record that is refused fails the turn.**  Roll the
                        # messages back to the prefix the turn began on and
                        # un-answer the callers it had folded in; the refusal is
                        # what the caller sees.
                        #
                        # **The `failure` is what the turn's own log line reads.**
                        # This `raise` sits in the `else` of the try/except
                        # above, so the `except BaseException` that records a
                        # failure never sees it: without this line `log_turn`
                        # saw the successful result and wrote `stop=stop` about
                        # a turn that was rolled back and raised.
                        failure = refusal
                        logger.warning(
                            "the turn of %s was not recorded (%s); rolling it back",
                            describe((loop.agent, loop.subagent)),
                            refusal,
                            exc_info=refusal,
                        )
                        self.recorder.roll_back(loop, outcome)
                        self.watching.drop_if(key, tap)
                        raise
                    # The store has this turn now, and `record_into` has put its
                    # id on the tap — where a watcher's snapshot decision reads
                    # it.  The local `turn_id` is what `covered` and
                    # `address_turn` below are built from.
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
                    if turn_id is not None and loop.covered == loop.turn_opening:
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
                        loop.covered += len(loop.messages) - loop.turn_opening
                        # ...and it is addressable now, which is what the next
                        # turn's decision needs: a turn it cannot name is
                        # one a keep-list cannot keep.
                        self.recorder.address_turn(loop, outcome, item, turn_id)
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
            # **Only the task that registered the handle clears it.**  A
            # message queued behind this turn reaches its own `finally` with
            # nothing registered; clearing unconditionally would erase the
            # handle the *next* turn put there a moment later and leave an
            # interrupt with nothing to cancel.
            if loop.turn_task is asyncio.current_task():
                loop.turn_task = None
            # **The one line a session is traced by**, on every ending.  It is
            # here rather than at the success return because a failure and a
            # cancellation are turns too — and the log is the only place they
            # are otherwise given a start, a duration and a reason in one place.
            # The turn is over only after its own line is written: the record
            # this line describes is the last thing the turn does, and a line
            # without the token would be the one line of the turn that cannot
            # be joined to the rest.  The `finally` is what makes the reset
            # survive a handler that raises on the way out.
            try:
                log_turn(loop, outcome, turn_id, failure, token=turn_token)
            finally:
                # **The value guard, not a flag.**  A message queued behind this
                # turn may have taken the lock and set its own token in the gap
                # between the lock's release and this line; clearing
                # unconditionally would erase the token of a turn that is
                # running.  Only the token this turn put there is taken back.
                if turn_token and loop.turn_token == turn_token:
                    loop.turn_token = ""
                    loop.live = None
                if turn_handle is not None:
                    logcontext.end_turn(turn_handle)

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

    async def stored_conversation(
        self, agent: str, subagent: str
    ) -> StoredConversation:
        """The turns a conversation's store is made of, and its plan.

        **One read, two readers**: `transcript` gives it to a window that has
        just opened, and `watch` gives it to a watcher as its first frame.  They
        must agree about where history ends — a watcher that read a different
        list from the window would start its live stream at a different turn —
        so the read lives here rather than being written out twice.  The plan
        rides the same frame for the same reason: a window and a watcher opened
        beside it must draw the same plan, and the plan is the conversation's
        own file rather than the store's.

        The opening prompt is what a conversation that has never run would head
        with, so it is handed over rather than an empty list; the returned
        `messages` half is the loop's business and is not read here.
        """
        payload = tool_payload(
            await (await self.runtime.memory()).call_tool(
                "restore",
                {
                    "agent": agent,
                    "subagent": subagent,
                    "messages": [
                        m.to_wire()
                        for m in opening_messages(
                            agent,
                            subagent,
                            self.current(),
                            subagent_spec(agent, subagent),
                        )
                    ],
                },
            )
        )
        turns = payload.get("turns")
        if not isinstance(turns, list):
            raise unreadable("the context store", "return a turn list", payload)
        return StoredConversation(turns, plan.read(agent, subagent))

    async def transcript(self, agent: str, subagent: str = "") -> dict[str, Any]:
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
        same damage the runtime's `restore` refuses — would become `[]`, and a
        screen would
        show an empty conversation and say nothing (SDD Appendix C: a failure is
        not a verdict).

        Args:
            agent: Whose conversation.
            subagent: Which of that agent's conversations; empty for its own.

        Returns:
            `turns`: the stored turns this conversation's context is made of, in
                the list's own order, oldest first.  Empty for a conversation
                that has never run — the honest answer for a genuinely new
                name, and not an error.  `plan`: the conversation's current
                plan, empty when it has none.
        """
        stored = await self.stored_conversation(agent, subagent)
        return {"turns": stored.turns, "plan": stored.plan}

    async def watch(self, agent: str, subagent: str, ctx: Context) -> dict[str, Any]:
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
        which is the one way the same turn could be drawn twice — and a record
        that is still being written is waited out before that decision, because
        the snapshot can hold the row before the tap has its id.

        The person watching never slows the turn: the observer appends to the
        tap and `put_nowait`s for each watcher, and the reporting happens on this
        call's own task (see the hub's `Watcher` for why it must).

        Args:
            agent: Whose conversation.
            subagent: Which of that agent's conversations; empty for its own.
                Anything else is one of its subagents.

        Returns:
            `closed`: true, when the conversation stopped being watchable (its
                subagent was removed) and the last frame, `Closed`, has been
                sent.  A cancellation returns nothing — it is a detach.
        """
        key = conversation_key(agent, subagent)
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
            before = self.watching.tap(key)
            stored = await self.stored_conversation(agent, subagent)
            if self.watching.tap(key) is not before:
                continue
            if (
                before is not None
                and before.writing is not None
                and not before.writing.done()
            ):
                # **The row can be in the store before the tap has its id.**
                # `writing` is that write; a snapshot read in this window
                # already contains the turn while `recorded_turn` still says
                # None, and replaying the tap would draw it twice.  Waiting for
                # the write and then reading the store again is what makes the
                # two facts agree — and if the write fails, the field stays None
                # and the replay is the half the store is missing.
                await asyncio.wait({before.writing})
                continue
            break
        watcher = self.watching.watch(key)
        replay = self.watching.replay(key)
        if subagent and not subagent_file(agent, subagent).is_file():
            # **A watcher registered after the removal's `close` was never
            # told.**  The removal reads the store, closes every watcher and
            # drops the key; a watcher that joins after that synchronous block
            # waits on a queue nothing will ever write.  The record is the
            # membership, so its absence here is the answer the removal would
            # have given — and it is checked *after* registration, which is the
            # order that cannot be raced: a removal either closed us, or the
            # record is gone by this read.
            self.watching.close(
                key,
                f"{subagent} is not a subagent of {agent}'s — it was never made "
                f"or has been removed",
            )
            self.watching.drop(key)
        known = {
            turn["turn_id"]
            for turn in stored.turns
            if isinstance(turn, dict) and isinstance(turn.get("turn_id"), int)
        }
        count = 0

        async def send(item: WatchItem) -> None:
            nonlocal count
            count += 1
            # **Reported from this handler's own task**, which is what makes the
            # progress token this call's: see the hub's `Watcher`.
            await ctx.report_progress(count, None, encode_watch(item))

        try:
            await send(Snapshot(stored.turns, stored.plan))
            if replay is not None:
                # **The tap is the turn in flight, or the one that just ended.**
                # It is skipped only when the snapshot provably contains it —
                # the store answered with an id the snapshot has.  A turn whose
                # write has not answered has no id, and the read above cannot
                # have included it either, so replaying it is the missing half
                # rather than a duplicate.
                if replay.recorded_turn is None or replay.recorded_turn not in known:
                    await send(replay.started)
                    for event in replay.events:
                        await send(event)
            while True:
                item = await watcher.queue.get()
                await send(item)
                if isinstance(item, Closed):
                    # The conversation is gone; the last frame has been written
                    # and there is nothing after it to wait for.
                    return {"closed": True}
        finally:
            self.watching.unwatch(key, watcher)

    async def attach_image(self, ctx: Context, sources: list[str]) -> dict[str, Any]:
        """Add image URLs to the LLM call.

        Each source becomes an `image_url` content part on the last user
        message, so the next LLM call carries the image. Reserved to vision
        models; any other model is refused.

        Args:
            sources: A local path (absolute, or relative to your data
                directory), an `http(s)` URL that serves image bytes, a
                `data:` URI, or the `key` or `source` of an `[Image:…]`
                reference. Several in one call attach together.

        Returns:
            `attached` — the sources the model can now see, named exactly as
            this call named them — and `refused`, one entry per source that
            could not be read, each naming the source and the reason.
        """
        conversation_id = request_conversation(ctx)
        if (
            conversation_id is None
            or (loop := self.conversations.get(conversation_key(*conversation_id)))
            is None
        ):
            raise ToolError(
                "this call is not inside a running turn, so there is no request "
                "to attach an image to."
            )
        # **The token is what tells this conversation's live turn from another
        # one on the same id.**  An agent task stands in for the same agent or
        # subagent, so its calls carry that id too — but its list is its own and
        # this loop is not it.  Attaching to the maker's loop on the task's word
        # would tell the task it can see a picture its next call never carries.
        # A call that names no turn is the direct read (`turn_of` argues it).
        caller_turn = turn_of(request_meta(ctx))
        if caller_turn and caller_turn != loop.turn_token:
            raise ToolError(
                "this call belongs to a turn that is not this conversation's "
                "running one, so there is no request here to attach an image "
                "to; nothing was attached."
            )
        wanted = [str(source) for source in sources if str(source).strip()]
        if not wanted:
            raise ToolError(attachments.NO_SOURCE)

        if not loop.turn_accepts_images:
            raise ToolError(attachments.cannot_read_images(loop.turn_model))

        pairs, refusals = await self.attach_sources(conversation_id, wanted)
        if pairs and not attachments.inject(
            loop.messages, [url for _source, url in pairs]
        ):
            raise ToolError(attachments.NO_MESSAGE)
        logger.info(
            "attach_image %s: %d attached, %d refused",
            conversation_id,
            len(pairs),
            len(refusals),
        )
        # **A refusal is about one source, and it is answered as a value.**  The
        # batch rule is the module's; this is the half that hands it to the
        # model.  One unreadable source among five still attaches the other
        # four, and a call where every source failed answers the shape a call
        # where none did answers — because the fault is the file's, and a
        # refusal about the *call* would throw away the sentence naming which
        # file it was.
        return {
            "attached": [source for source, _url in pairs],
            "refused": refusals,
        }

    async def write_plan(
        self, ctx: Context, items: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Write the whole plan for this conversation, replacing the one before.

        **Send every item every time.**  The list replaces the previous plan, so
        an omitted item is a dropped item; an empty list clears the plan.  The
        plan is replayed to you at the start of every turn and drawn for the
        person watching, so keep it to the work in front of you: at most 24
        items, each one line of at most 200 characters.

        Args:
            items: Every step, in order.  Each is `{text, status}`, where
                `status` is `pending` (not started), `active` (being worked on
                now), `done` (finished), or `blocked` (cannot proceed — put the
                reason in the text).  No other fields.

        Returns:
            `items` as written, `done` (how many are `done`), and `total`.
        """
        agent, subagent = self._plan_conversation(ctx)
        try:
            written = plan.write(agent, subagent, items)
        except plan.PlanError as exc:
            raise ToolError(str(exc)) from exc
        done = sum(1 for item in written if item["status"] == plan.DONE)
        logger.info(
            "plan %s: %d item(s), %d done",
            describe((agent, subagent)),
            len(written),
            done,
        )
        return {"items": written, "done": done, "total": len(written)}

    async def check_new_input(self, ctx: Context) -> str:
        """The message that arrived while the turn was running, verbatim.

        The harness calls this at a turn's next iteration boundary and writes the
        call and its result into the conversation, so a message that arrives
        mid-turn reaches the turn it arrived in. It hands over the first message
        waiting for this conversation.
        """
        conversation_id = request_conversation(ctx)
        if not conversation_id:
            return NOTHING_WAITING
        # **`or`, because `""` is not an answer here.**  `take_waiting` returns
        # the empty string for the one thing it refuses — a head that carries
        # images, which cannot ride a tool result — and *this* is a tool result:
        # a blank one reads as a failure rather than as "nothing was handed
        # over", which is the whole reason `NOTHING_WAITING` is a sentence.
        return (
            self.take_waiting(conversation_id, turn_of(request_meta(ctx)))
            or NOTHING_WAITING
        )

    async def context_rebuild(
        self,
        ctx: Context,
        keep: Any = None,
        recall: Any = None,
    ) -> str:
        """Rebuild the conversation's live context — its historical turns.

        **The model-facing description is `CONTEXT_REBUILD_TEXT`** in
        `slife2.libs.turn.tools` — one text for the model in a turn and for the
        forced decision call.  This docstring is the code reader's.

        **`keep` and `recall` are `Any` at the signature on purpose.**  The
        automatic call carries the decision model's raw answer, and a failure is
        carried as its own words under `keep`; a typed signature would reject
        that at dispatch, before the body could turn it into the tool error the
        agent reads.  `rebuild_selection` is the one check, and it names the
        shape the fields take.

        The tool edits the turns the conversation is made of: it may drop some
        of them and recall others from the turn log. **The turn running is never
        touched** — it has no turn id yet, so it is carried verbatim while the
        history around it is rebuilt. The answer reports how many of the turns
        in hand it kept, how many turns outside the context it recalled, and an
        estimate of the context this turn runs on from here.

        **`keep` and `recall` are the two halves of the decision, and both are
        required.** `keep.action` is `"keep"` (every turn in hand), `"clear"`
        (none of them), or `"ids"` (exactly `keep.turn_ids`); `recall` is the
        query condition for the turn log, and `{}` is no recall.  The model's
        own call passes both; the harness's automatic call carries the selection
        its decision model returned.  A call missing either is refused — the
        decision model is asked by the harness before the call, never by an
        empty call's body.

        **A turn is named by the id its footnote carries.**  Every turn in the
        conversation opens with `[TURN:{"turn_id": …}]`, and a keep-list is
        those ids; kept and recalled turns are joined, so naming a turn on both
        sides is still one turn.
        """
        conversation_id = request_conversation(ctx)
        if not conversation_id:
            return "No live conversation to rebuild."
        loop = self.conversations.get(conversation_key(*conversation_id))
        caller = turn_of(request_meta(ctx))
        if loop is None or (caller and caller != loop.turn_token):
            return "No live turn to rebuild."
        try:
            applied = await self.runtime.apply_rebuild(
                (loop.agent, loop.subagent),
                loop,
                loop.turn_model,
                publish=True,
                # **The last LLM call's figure.**  A completed call of this turn
                # is newer than the previous turn's last call; before the first
                # call — where the automatic rebuild runs — the saved call
                # stands.
                context_tokens=measured_tokens(loop.live, loop.context_tokens),
                prompt_window=loop.turn_prompt_window,
                keep=keep,
                recall=recall,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        if applied is None:
            return "This turn has no opening message; nothing to rebuild."
        loop.turn_opening = applied.opening
        rebuilt = applied.rebuilt
        logger.info(
            "context_rebuilt %s changed=%s kept=%d in_hand=%d outside=%d "
            "recalled=%d rerank=%s decision=%s",
            describe((loop.agent, loop.subagent)),
            rebuilt.changed,
            rebuilt.kept,
            rebuilt.in_hand,
            rebuilt.outside,
            rebuilt.recalled,
            rebuilt.rerank or "-",
            rebuilt.decision or "-",
        )
        return applied.text

    async def turn_compact(self, ctx: Context, compact: Any = None) -> str:
        """Drop what this turn no longer needs from the view it is running on.

        **The model-facing description is `TURN_COMPACT_TEXT`** in
        `slife2.libs.turn.tools` — one text for the model in a turn and for the
        forced decision call.  This docstring is the code reader's.

        **The tool, not the decision.**  The selector is the context plugin's
        (`select_live`), and what this side owns is the live loop: the state a
        compaction hides (`Loop.hidden`), the list it must never touch, and the
        answer's voice in the transcript.  The harness calls this when a turn's
        measured context crosses `agent.compact_at`, and the model may call it
        itself — the call does the same thing either way.

        **The record is untouched, and the view is the whole list.**  A
        historical turn's messages are hidden the same transient way a spent
        tool result is; the candidates are the person's messages and the tool
        results, which are the ones carrying an id.  Hidden messages stay in
        `loop.messages`, the turn's saved row and the store's live-context list;
        only the list the model is sent shrinks (`slife2.libs.turn.compact`).
        The next turn starts from the full conversation; `_context_rebuild` is
        the tool that changes the durable live-context list.

        **The arguments are the selection, and a call must carry one.**  The
        model's own call passes `compact`; the harness's automatic call carries
        the ids its selector returned.  A call without `compact` is refused —
        the selector is asked by the harness before the call, never by an empty
        call's body.  An id is written exactly as its `[MESSAGE:{"id": …}]`
        annotation shows it; the schema states the fixed two and the exchange
        rule.  **A half-named exchange stays whole and the rest of the
        selection applies**; the answer names the pair that stayed.  A selection
        naming an id the view did not offer is refused with a tool error naming
        why, so it can be corrected while the view stays whole.
        """
        # The parameter shadows the module for this body; the view helpers are
        # this alias, and `compact` is the selection the model named.
        from slife2.libs.turn import compact as compact_view

        conversation_id = request_conversation(ctx)
        if not conversation_id:
            return "No live conversation to compact."
        loop = self.conversations.get(conversation_key(*conversation_id))
        caller = turn_of(request_meta(ctx))
        # **A token that is there has to be this turn's; one that is not is a
        # direct read.**  The same rule `take_waiting` follows for the cut-in:
        # the task that stands in for this conversation serves its own body, so
        # an untokened call cannot be the other live turn's.
        if loop is None or (caller and caller != loop.turn_token):
            return "No live turn to compact."
        at = compact_view.opening(loop.messages)
        if at is None:
            return "This turn has no opening message; nothing to compact."
        try:
            result = await self.runtime.apply_compaction(
                conversation_id,
                loop,
                loop.turn_model,
                selection=compact,
                context_tokens=measured_tokens(loop.live, loop.context_tokens),
                prompt_window=loop.turn_prompt_window,
            )
        except compact_view.Refusal as exc:
            raise ToolError(str(exc)) from None
        logger.info(
            "turn_compact %s seen=%d dropped=%d held=%d decision=%s hold=%s",
            describe((loop.agent, loop.subagent)),
            result.seen,
            result.dropped,
            len(result.held),
            result.decision or "ok",
            loop.compact_hold,
        )
        return compact_view.report(result)

    async def check_background_call(self, task_id: str, ctx: Context) -> dict[str, Any]:
        """Where one background call stands.

        `status` is `running`, `finished`, `failed` or `cancelled`; `tool` and
        `call_id` name the call, and `result` is the answer only when the
        conversation has not been handed it yet.  The result arrives on its own
        when the call ends; this asks where it stands, not what it said.

        `task_id` is the id the backgrounded call answered with.  A task that
        does not exist — or one started in another conversation — is refused.

        Args:
            task_id: The id the backgrounded call answered with.
        """
        try:
            return await check_async({"task_id": task_id}, scope=_background_scope(ctx))
        except ToolFailed as exc:
            raise ToolError(str(exc)) from exc

    async def cancel_background_call(
        self, task_id: str, ctx: Context
    ) -> dict[str, Any]:
        """Abandon a running background call, and answer where it stands.

        `status` is `cancelled` when this is what stopped it, and the call's own
        ending when it was already over — in which case its `result` is still
        handed over, not dropped.

        `task_id` is the id the backgrounded call answered with.  A task
        started in another conversation is refused, exactly as an unknown id.

        Args:
            task_id: The id the backgrounded call answered with.
        """
        try:
            return await cancel_async(
                {"task_id": task_id}, scope=_background_scope(ctx)
            )
        except ToolFailed as exc:
            raise ToolError(str(exc)) from exc

    async def answer_approval(
        self, agent: str, call_id: str, approved: bool, subagent: str = ""
    ) -> dict[str, Any]:
        """Answer one `_approve` request, and say whether anything was waiting.

        **This server's own surface, not a tool** — it sits beside
        `send_message` and `transcript`, and the model never reaches
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
        loop = self.conversations.get(conversation_key(agent, subagent))
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

    def register_tools(self, mcp: FastMCP) -> None:
        """Serve the conversation surface and the tools an agent calls on it."""
        for tool in (
            FunctionTool.from_function(
                self.attach_image, name=ATTACH_IMAGE, meta=FOR_THE_MODEL
            ),
            FunctionTool.from_function(
                self.write_plan, name=plan.PLAN_WRITE, meta=FOR_THE_MODEL
            ),
            FunctionTool.from_function(
                self.check_new_input, name=CHECK_NEW_INPUT, meta=FOR_THE_MODEL
            ),
            # **The selection schemas, not the signatures.**  The methods take
            # the fields so the call can carry them; the schema the model reads
            # is the decision reply's (`RECALL_REPLY`/`LIVE_REPLY`), so the
            # model's own call and the automatic decision answer to one shape.
            FunctionTool.from_function(
                self.context_rebuild,
                name=CONTEXT_REBUILD,
                description=CONTEXT_REBUILD_TEXT,
                meta=FOR_THE_MODEL,
            ).model_copy(update={"parameters": RECALL_REPLY}),
            FunctionTool.from_function(
                self.turn_compact,
                name=TURN_COMPACT,
                description=TURN_COMPACT_TEXT,
                meta=FOR_THE_MODEL,
            ).model_copy(update={"parameters": LIVE_REPLY}),
            FunctionTool.from_function(
                self.check_background_call, name=CHECK_ASYNC, meta=FOR_THE_MODEL
            ),
            FunctionTool.from_function(
                self.cancel_background_call, name=CANCEL_ASYNC, meta=FOR_THE_MODEL
            ),
            FunctionTool.from_function(self.answer_approval),
            FunctionTool.from_function(self.send_message),
            FunctionTool.from_function(self.transcript, name=TRANSCRIPT),
            FunctionTool.from_function(self.watch),
        ):
            mcp.add_tool(tool)


def _background_scope(ctx: Context) -> ConversationKey | None:
    """The conversation a `check_async`/`cancel_async` call is for.

    `None` when the call named none — the registry's own rule (`_may_read`)
    then decides, rather than this refusing a call that simply did not say.
    """
    conversation = request_conversation(ctx)
    return conversation_key(*conversation) if conversation else None


def _reply(
    result: TurnResult,
    *,
    model: str,
    injected: bool,
    refused: list[str],
) -> dict[str, Any]:
    """One turn's answer, in the one key set a caller is promised.

    **Built here rather than at each return.**  A stable key set is a promise,
    and one dict is the only way to keep it.

    `result` is never None, which is why there is no `if result else` anywhere
    below.  Every path that leaves a turn without a result has already gone —
    `_injected_reply` for a folded message, and the re-raises in `send_message`
    for a cancellation and a failure — so no branch is needed.

    **`model` is passed in rather than read off the loop**, and that is the
    second thing this signature is for.  The loop's reference is *not* the model
    that answered: `model_switch` moves it under a turn that is already running
    — that is the whole of what makes the tool the way out of a model that has
    stopped answering — so a reply built from `loop.model` here would name a
    model that never saw this message.  What is passed is the turn's own record
    of it (`Outcome.model`, taken at the boundary `loop_for` resolves the
    backend at), which is the same fact the row is written with
    (`Recorder.recorded_model`) and for the same reason.

    `injected` and `refused` are the only two things that differ between the two
    callers, and both are facts about *the message being answered* rather than
    about the turn that answered it: whether its own turn ran or it rode
    another's, and which of its own sources could not be attached.  `refused` is
    empty on every ordinary turn, and a source the caller passed that could not be
    read is named there because the alternative — an answer that silently had
    fewer pictures than the call asked for — is the one thing a caller cannot
    detect and a person cannot explain.
    """
    return _reply_fields(
        text=result.text,
        usage=result.usage,
        iterations=result.iterations,
        stop_reason=result.stop_reason,
        model=model,
        injected=injected,
        refused=refused,
    )


def _reply_fields(
    *,
    text: str,
    usage: Usage,
    iterations: int,
    stop_reason: str,
    model: str,
    injected: bool,
    refused: list[str],
) -> dict[str, Any]:
    """The one dict every reply is made of, whatever produced its values.

    **One literal, because the key set is the promise.**  `_reply` and
    `_injected_reply`'s no-answer case both build a reply, and a second
    literal — which the injected case used to carry — means adding a key to
    one leaves a hole in the other.
    """
    return {
        "text": text,
        "usage": usage.to_wire(),
        "iterations": iterations,
        "stop_reason": stop_reason,
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

    `usage` and `iterations` are that whole turn's, deliberately — they
    describe the work that answered this message, which is that turn's work and
    nothing else.
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
    # **A delivered cut-in with no answer has no answer to repeat, and still no
    # turn to run.**  Its absorbing turn was cancelled or failed after the fold,
    # and its row records the pair: the message is in the conversation already,
    # so an empty answer says the only thing that is true — it was delivered and
    # not answered.  A caller that ran it again would store it twice.
    if item.result is None:
        return _reply_fields(
            text="",
            usage=Usage(),
            iterations=0,
            stop_reason="",
            model=item.model,
            injected=True,
            refused=[],
        )
    # Written together with `result` and only by a turn that produced one
    # (`Pending.model`), so by the time this caller is answered both are there.
    return _reply(item.result, model=item.model, injected=True, refused=[])
