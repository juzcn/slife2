"""The middle of a turn: its peers, its model, and the context it runs on.

The agent server and the agent-task server each own the *edges* of a turn.
One keeps conversations — a registry, an inbox, a tap and a record; the other
keeps one detached loop and a hand-over.  What both were building is this
middle: the model clients, the context and hub connections, `restore`,
`rebuild`, the tool-list trim, and `AgentLoop`'s construction.  It was
maintained twice, so a change to the rebuild or the trim could reach one server
and not the other; here it is once.

**A runtime builds a turn; it does not run one.**  The order `restore` →
`rebuild` → `make_loop` → `run_turn` → `trim` is the caller's, because the
edges decide it: the agent server holds its conversation lock across the first
two and records the last; the task runs them as a clone — it publishes no
rebuild — and hands the answer over.  What this supplies is the I/O every such
turn makes, and the strict reading of the context store's answers.

**The store is the server's.**  `restore` and `rebuild` take the
conversation's messages, turn ids and covered count and answer with
replacements; adopting them is the server's step, because the object that holds
them is the server's — the agent's `Loop`, or a task's local list.  Nothing
here knows how a conversation is keyed beyond the `(agent, subagent)` every hop
carries, and nothing here records or delivers: those are the policies the two
callers argue for themselves.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from fastmcp import Client

import slife2.helpers.timeouts as _T
from slife2.clients.model import MCPBackend, open_backend
from slife2.clients.toolclient import (
    FUNC_TOOL_UNLOAD,
    trim_answer,
    unload_tools,
)
from slife2.libs.harness.async_tasks import Deliver
from slife2.libs.harness.dumps import Compress
from slife2.libs.host.config import (
    API_SERVER_NAMES,
    CABINET_SERVER_NAME,
    CONTEXT_SERVER_NAME,
    TOOLHUB_SERVER_NAME,
    Config,
    for_the_call,
)
from slife2.libs.model.base import LLMBackend
from slife2.libs.plugin.audience import ConversationId, conversation_key
from slife2.libs.plugin.protocol import (
    UnreadableAnswer,
    close_server,
    open_server,
    tool_payload,
    unreadable,
)
from slife2.libs.turn import compact
from slife2.libs.turn import context as decisions
from slife2.libs.turn.events import (
    NULL_OBSERVER,
    ToolCallFinished,
    ToolCallStarted,
    TurnObserver,
    emit,
    preview,
)
from slife2.libs.turn.loop import AgentLoop, AutoCall, harness_call
from slife2.libs.turn.messages import Message, ToolCall
from slife2.libs.turn.tokens import estimate_turn_tokens
from slife2.libs.turn.tools import (
    CONTEXT_REBUILD,
    TURN_COMPACT,
    ToolRegistry,
    is_harness_call,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelRef:
    """A model reference and the three facts needed to reach it.

    `reference` is `provider/model` — the cache key and the name a turn's record
    carries.  The other three are what `open_backend` is built with, resolved
    by the caller because a conversation resolves them once and keeps them; a
    task resolves them from the live config each time it runs, because it has
    no conversation to keep them on.
    """

    reference: str
    provider: str
    api: str
    model_id: str


@dataclass(frozen=True)
class Restored:
    """A conversation put back on the context it had when it last stopped.

    `covered` is every message, because a restore carries nothing in front of
    it: an unbacked tail is a fact about the list the caller held, not about
    the store's answer.  `context_tokens` is the last restored turn's own
    figure — the number a status bar would show — or 0 when no size was
    measured.
    """

    messages: list[Message]
    turn_ids: list[int]
    covered: int
    context_tokens: int


@dataclass(frozen=True)
class Rebuilt:
    """What a turn runs on after the decision was made.

    `covered` counts the rebuilt turns; what follows them is the caller's
    carried tail, which the store re-appended verbatim.  The decision's own
    counts and statuses travel here rather than as an event: the call is a
    harness tool's, and the pair it writes is what reports it.
    """

    messages: list[Message]
    turn_ids: list[int]
    covered: int
    changed: bool
    #: How many of the caller's turns the decision kept, and how many the
    #: caller held before it.  The pair is the report's "kept 0/5": `kept`
    #: alone cannot tell a clear from a history that was empty.
    kept: int
    in_hand: int
    #: The turns in the log beyond the context — the whole set a recall
    #: condition filters.  The changed report's "recalled 0/90" is read from this
    #: figure.
    outside: int
    recalled: int
    rerank: str
    #: Why there was no decision: `""` when the model answered, otherwise the
    #: provider's failure text (`failure_text`) or the parser's refusal.  The
    #: words ride the pair's report, so a person reads the same reason the log
    #: carries.
    decision: str
    #: How many turns the prompt budget cut from the selection's oldest end, or
    #: 0.  The harness's edit, named by the report rather than folded into
    #: `kept`: the window's bound is not a selection the model made.
    fitted: int = 0
    #: Which no-op the answer was, when nothing changed: `"asked"` (the decision
    #: wants exactly what is in hand), `"recalled"` (a recall ran and found
    #: nothing), `"unknown"` (a keep-list naming no turn in hand) or
    #: `"unfetchable"`.  `""` on a changed answer.  The report names it so four
    #: different facts are not one "unchanged" sentence.
    reason: str = ""


@dataclass(frozen=True)
class Applied:
    """A rebuild already applied to a live view, and the answer it owes.

    `opening` is the running turn's first message in the *new* list — the index
    the agent server keeps as `turn_opening`, and the one an agent task does not
    need.  `text` is the model-facing report, built from the list as it stands.
    """

    rebuilt: Rebuilt
    opening: int
    text: str


class LiveView(Protocol):
    """The mutable state a live context edit touches, on either server.

    `Loop` (an agent's conversation) and `Turn` (an agent task's clone) spell the
    same state; the two tool bodies had a copy of every edit, which is how "a
    rebuild resets the view" comes to be true on one server and stale on the
    other.
    """

    messages: list[Message]
    turn_ids: list[int]
    covered: int
    hidden: set[str]
    compact_hold: bool
    compact_at: float
    rebuilt: bool


@dataclass(frozen=True)
class LiveChoice:
    """One live selector's raw answer, before `_turn_compact` checks it.

    `arguments` is the forced call's raw argument text (or the mapping an
    injected selector answered with); `decision` is `""` for an answer,
    otherwise why there is none.  The caller builds the call's `compact`
    argument from it; `compact_turn` checks the selection.
    """

    arguments: Any = ""
    decision: str = ""


@dataclass(frozen=True)
class Decided:
    """The decision model's raw answer, before a tool executes it.

    **The arguments the automatic call will carry.**  `arguments` is the forced
    call's raw argument text (or the mapping an injected model answered with);
    it is not parsed here.  `decision` is `""` for an answer, otherwise why
    there is none.  The caller turns the text into a non-empty selection for the
    tool call — the tool checks it and reports a failure if it is wrong.
    """

    arguments: Any = ""
    decision: str = ""


def _message_list(payload: dict[str, Any]) -> list[Message]:
    """The messages an answer carries, refused unless every one is a mapping.

    **A list of something else is not a short list.**  `Message.from_wire`
    reads a mapping; a string or a number in its place used to raise
    `AttributeError` out of the restore, which is an internal error rather than
    the sentence a build mismatch earns.  The whole list is checked before any
    of it is adopted, so a damaged answer cannot half-replace a conversation
    (`slife2.libs.turn.context.consistent` says the same for a stored one).
    """
    raw = payload.get("messages")
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise unreadable("the context store", "return a message list", payload)
    try:
        return [Message.from_wire(item) for item in raw]
    except (AttributeError, KeyError, TypeError) as exc:
        # A mapping whose own fields are the wrong shape is the same damage
        # one level down: the type name is a traceback, not an answer.
        raise unreadable("the context store", "return a message list", payload) from exc


def _context_message(message: Message) -> dict[str, Any]:
    """One message as the context plugin's `rebuild` hop carries it.

    **The wire plus the two derived fields a rebuild must hand back unchanged.**
    `channel` is the display metadata a cut-in pair's tool message carries and a
    rebuilt transcript draws its bubble from; `_mid` is the live id the view
    annotates and a selection names.  Neither is stored and neither goes to a
    provider, so `Message.to_wire` omits them and `to_record` keeps only the
    first.  The rebuild's no-op answer echoes the caller's own messages and its
    changed one re-appends the carried tail, so a hop that dropped them stripped
    the live list of its history's ids — a compaction could no longer name a
    historical message — and of a folded cut-in's channel, which its saved row
    then lost.
    """
    payload = message.to_record()
    if message.mid:
        payload["_mid"] = message.mid
    return payload


def _readable_selection(call: ToolCall) -> bool:
    """Whether a harness call's arguments are the current selection shape.

    **Only the schema decides what an example is.**  A valid past
    `_context_rebuild` / `_turn_compact` pair is a real selection the model
    made and may be kept; a failure pair carries a string under the selection
    field and is not one.  The check is the tool's own (`rebuild_selection` for
    the rebuild, the list shape for the compaction), so the filter and the tool
    agree about what "readable" means.
    """
    if call.name == CONTEXT_REBUILD:
        try:
            decisions.rebuild_selection(
                call.arguments.get("keep"), call.arguments.get("recall")
            )
        except ValueError:
            return False
        return True
    if call.name == TURN_COMPACT:
        return isinstance(call.arguments.get("compact"), list)
    return True


def _decision_messages(messages: list[Message]) -> list[Message]:
    """The live context a decision model reads, without harness failure pairs.

    **A failed decision's pair is not an example.**  The harness writes
    `_context_rebuild` / `_turn_compact` with a failure sentence under the
    selection field, and the tool answers with its refusal.  The agent must
    read that pair, so it stays in the conversation — but the next decision
    model is about to fill the same tool, and a model that copies the pair's
    shape reproduces the failure.  The filter is for the decision call only:
    the turn's own list, the record and its `[MESSAGE:…]` ids are untouched.
    A valid past pair is kept; it is a real selection and a useful example.

    A tool result whose call was dropped goes with it; leaving it behind would
    show the decision model a result with no call, which is not a conversation
    any provider accepts.
    """
    failed: set[str] = set()
    kept: list[Message] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            bad = {
                call.id
                for call in message.tool_calls
                if is_harness_call(call.id)
                and call.name in (CONTEXT_REBUILD, TURN_COMPACT)
                and not _readable_selection(call)
            }
            if bad:
                # The assistant message goes whole: dropping only the failed
                # call would leave a call whose result no longer follows it.
                failed.update(bad)
                continue
        if message.role == "tool" and message.tool_call_id in failed:
            continue
        kept.append(message)
    return kept


def _turn_id_list(payload: dict[str, Any]) -> list[int]:
    """The turn ids an answer carries: whole ints, never bools or text.

    **A damaged id is not a turn.**  `true` is an `int` to `isinstance` and
    would name turn 1, a string is coerced by `int()`, and a float truncates —
    each becomes a real id in `Loop.turn_ids`, and the next rebuild that
    publishes anything writes that id into the stored live-context list.  The
    two other readers of a store-written id (`Recorder.remember_turn`,
    `TurnRuntime.select_live`) already refuse these; a restore is the same
    question asked earlier, so it is answered the same way (SDD Appendix C).
    """
    raw = payload.get("turn_ids")
    if not isinstance(raw, list):
        raise unreadable("the context store", "return a turn-id list", payload)
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            raise unreadable("the context store", "return a turn-id list", payload)
    return list(raw)


def _count(payload: dict[str, Any], key: str) -> int:
    """One rebuild count, refused unless it is the int the store writes.

    `True` would read as 1 and `"2"` as 2; both would put a figure nobody
    reported on the `kept …` line, which is the report's whole content.
    """
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise unreadable("the context store", "report the rebuild's counts", payload)
    return value


def _compact_value(arguments: Any) -> Any:
    """The `compact` value in a raw selector answer, **without checking it**.

    A transport conversion, not a legality check: a JSON-object text has its
    `compact` field taken out, and anything else is handed on as it came.
    `parse_live` is the one check, and it runs on the result of this for both
    the selector's answer and the model's own call.
    """
    raw = arguments
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw
    if isinstance(raw, dict):
        return raw.get("compact", raw)
    return raw


def selection_from_arguments(arguments: Any, field: str) -> dict[str, Any]:
    """A decision's raw arguments as a **non-empty** tool-call argument mapping.

    A transport conversion, not a legality check.  A JSON-object text is passed
    as the object; anything else is wrapped in `field`, so the tool sees the
    value the decision model gave and answers with its own refusal.  The result
    is never `{}`: the tool call must carry arguments, and a failure is one of
    them.
    """
    if isinstance(arguments, dict):
        return (
            arguments if arguments else {field: "the decision returned an empty object"}
        )
    if isinstance(arguments, str) and arguments:
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed:
            return parsed
        if isinstance(parsed, dict):
            return {field: "the decision returned an empty object"}
        return {field: arguments}
    return {field: "the decision returned no arguments"}


def measured_tokens(live: AgentLoop | None, fallback: int) -> int:
    """The newest measured call's size, or the last saved figure when none measured.

    **One expression for both tools and both servers.**  `AgentLoop.last_usage`
    is reset to zero when a turn opens, so a turn that has not completed a call
    yet — or one whose provider reported no usage — has no figure of its own.
    `fallback` is the conversation's last saved measurement, which is what the
    status bar shows until a call replaces it.  A decision that showed `0` there
    would be reading a context the conversation does not have.
    """
    if live is not None and live.last_usage.total_tokens:
        return live.last_usage.total_tokens
    return fallback


def _rebuild_answer(
    messages: list[Message], rebuilt: Rebuilt, prompt_window: int
) -> str:
    """The model-facing report after a rebuild, sized from the list it left."""
    tokens = decisions.context_usage(
        estimate_turn_tokens([message.to_record() for message in messages]),
        prompt_window,
    )[0]
    return decisions.rebuild_report(
        kept=rebuilt.kept,
        in_hand=rebuilt.in_hand,
        outside=rebuilt.outside,
        recalled=rebuilt.recalled,
        rerank=rebuilt.rerank,
        fitted=rebuilt.fitted,
        tokens=tokens,
    )


class TurnRuntime:
    """The peers, the model and the context calls one turn is built on."""

    def __init__(
        self,
        config: Config,
        *,
        backend: LLMBackend | None = None,
        context_client: Client | None = None,
        hub_client: Client | None = None,
        cabinet_client: Client | None = None,
        source: Callable[[], Config] | None = None,
    ) -> None:
        self._config = config
        self._source = source
        #: An injected backend is a test's, and it is returned as it is: only
        #: `MCPBackend` can name a conversation to a model server, and an
        #: injected one has no model server to name.
        self._backend = backend
        #: The injected peers and the ones this runtime opened are kept apart
        #: so `close` knows what it owns.  An injected client belongs to
        #: whoever handed it in.
        self._context_client = context_client
        self._hub_client = hub_client
        #: The cabinet is the agent-task turn's: the agent server keeps its own
        #: connection (it writes dumps and kept images through it as well), and a
        #: task reaches it only to resolve an `[Image:…]` key its own
        #: `attach_image` was handed.
        self._cabinet_client = cabinet_client
        #: One connection per **endpoint**, not one per runtime.  The config is
        #: read per call (a shared server serves several homes, and each names
        #: its own `servers:` block), so two homes may point these peers at two
        #: different daemons — and one cached client would file the second
        #: home's turns under the first one's store.  Keyed by URL for the same
        #: reason `model()` keys its clients that way.
        self._contexts: dict[str, Client] = {}
        self._hubs: dict[str, Client] = {}
        self._cabinets: dict[str, Client] = {}
        #: One client per model *server* — a server speaks one wire format for
        #: every provider that uses it — and one backend per model reference,
        #: because the reference is what the backend must match.
        self._clients: dict[str, Client] = {}
        self._backends: dict[tuple[str, str], MCPBackend] = {}
        #: One opener at a time for the lazily-built connections and backends,
        #: so two turns starting together do not build two clients for one
        #: server and leak the loser.
        self._opening = asyncio.Lock()

    def current(self) -> Config:
        """The config the call in front of us is served from.

        `for_the_call` is the rule: a server built with an injected config keeps
        it, and a server wired to `load_cached` reads the file again.  The
        latter is what makes a model edit reach the next turn without a
        restart, and the former is what lets every test run with no file on
        disk.
        """
        return for_the_call(self._source, self._config)

    # ── the peers ────────────────────────────────────────────────────────

    async def _open_peer(
        self,
        injected: Client | None,
        cache: dict[str, Client],
        server: str,
        *,
        name: str,
        fallback_tool: str,
        timeout: float | None = None,
    ) -> Client:
        """One lazily-opened peer: injected, cached by address, or opened once.

        **The three peers are one mechanism**, and this is it.  An injected
        client belongs to whoever handed it in and is returned as it is.  The
        address is read per call, from the config the call is served from
        (`current`), because this process may serve several homes and each may
        name a different daemon.  The connection is kept per address, so the
        common one-home case still opens once.  And the lock makes two turns
        starting together build one client for one server rather than two and
        leak the loser.

        `timeout` is the connection's request deadline where the peer wants
        one; None leaves every request's patience to the call that makes it.
        """
        if injected is not None:
            return injected
        url = self.current().server(server).url
        held = cache.get(url)
        if held is not None:
            return held
        async with self._opening:
            held = cache.get(url)
            if held is None:
                held = await open_server(
                    url, name=name, fallback_tool=fallback_tool, timeout=timeout
                )
                cache[url] = held
            return held

    async def memory(self) -> Client:
        """The context plugin, opened on first use.

        Opened once per endpoint and kept for the process, like every peer
        here: a handshake per turn would be a handshake per turn.  A context
        plugin that is not there **raises** — a turn whose context cannot be
        restored is a turn that should not run — and this is the peer whose
        absence is felt soonest.

        The address is read per call and the connection kept per address
        (`_open_peer`).
        """
        return await self._open_peer(
            self._context_client,
            self._contexts,
            "context",
            name=CONTEXT_SERVER_NAME,
            fallback_tool="remember",
            timeout=_T.timeouts.work.context,
        )

    async def hub(self) -> Client:
        """The toolhub, opened on first use.

        **Never optional.**  Every plugin's tools are in the hub rather than in
        the server that runs the turn, so a system whose toolhub is missing is
        not a system with no tools — it is a system that has come apart, and it
        fails the turn like any other missing peer.  A hub that is there and
        whose *upstreams* are not is the operator's configuration and is not an
        error at all.

        The address is read per call and the connection kept per address
        (`_open_peer`).

        **No request deadline on the connection**: `timeout=` on the
        connection is a clock on every request that crosses it, so a listing's
        patience would become the deadline for a tool call.  The asks made of
        the hub state their own patience where they are made.
        """
        return await self._open_peer(
            self._hub_client,
            self._hubs,
            "toolhub",
            name=TOOLHUB_SERVER_NAME,
            fallback_tool="list_tools",
        )

    async def cabinet(self) -> Client:
        """The cabinet, opened on first use.

        **Only a task opens one here.**  The agent server keeps its own
        connection, because it writes through it as well (`dump_save`,
        `image_save`); a task reaches it for exactly one thing — turning an
        `[Image:…]` key its restored context carries back into a URL the task's
        own `attach_image` can inject.  Opened lazily for that reason: a task
        with no picture in it must not depend on the cabinet at all.

        The address is read per call and the connection kept per address
        (`_open_peer`).
        """
        return await self._open_peer(
            self._cabinet_client,
            self._cabinets,
            "cabinet",
            name=CABINET_SERVER_NAME,
            fallback_tool="dump_read",
        )

    async def model(self, identity: ConversationId, model: ModelRef) -> LLMBackend:
        """A backend bound to one conversation, opened on first use.

        **Keyed by the model reference and the server URL**, because the
        `MCPBackend` carries the model id it was built with and a cache keyed by
        provider would hand the second of two conversations on one provider the
        first one's model.  The URL is half of the key because one process serves
        every home: two homes may use the same `provider/model` reference while
        their configs point `servers.<api>.url` at different daemons — and a
        cache keyed by the reference alone sent one home's history to the
        other's endpoint (or kept an edited URL's old backend for ever).  The
        conversation key is not cached: `with_key` hands back a view of the
        shared backend carrying this conversation's `(agent, subagent)`, so it
        reaches the model server on every call without splitting the cache.
        """
        if self._backend is not None:
            return self._backend
        url = self.current().server(model.api).url
        held = self._backends.get((model.reference, url))
        if held is None:
            async with self._opening:
                held = self._backends.get((model.reference, url))
                if held is None:
                    client = self._clients.get(url)
                    if client is None:
                        client, _ = await open_backend(
                            url,
                            model.model_id,
                            provider=model.provider,
                            name=model.reference,
                            # Checked against the name the protocol's server
                            # advertises, so a URL pointed at the wrong
                            # backend is caught here rather than at the first
                            # turn.
                            server_name=API_SERVER_NAMES[model.api],
                        )
                        self._clients[url] = client
                    held = MCPBackend(
                        client,
                        model.model_id,
                        provider=model.provider,
                        name=model.reference,
                    )
                    self._backends[(model.reference, url)] = held
                    logger.info("model %s via %s", model.reference, url)
        return held.with_key(*identity)

    # ── the context ──────────────────────────────────────────────────────

    async def restore(
        self, identity: ConversationId, messages: list[Message]
    ) -> Restored:
        """Put a conversation back on the context it had when it last stopped.

        Called once, when the loop is built.  The answer replaces the list
        wholesale, because a context half this runtime's and half the store's is
        not a context either of them knows — and the *caller* does the
        replacing, because the list is the caller's.

        **Every half of the answer is read strictly.**  `messages` and
        `turn_ids` are one context, so an answer carrying messages and no ids
        is a list whose turns nothing can name; `turns` is part of the same
        answer's shape, written even when it is empty, and a hole there is
        another build.  Reading any of them as absent would make an unreadable
        answer indistinguishable from a decision (SDD Appendix C).
        """
        payload = tool_payload(
            await (await self.memory()).call_tool(
                "restore",
                {
                    "agent": identity[0],
                    "subagent": identity[1],
                    "messages": [message.to_wire() for message in messages],
                },
            )
        )
        restored = _message_list(payload)
        turn_ids = _turn_id_list(payload)
        turns = payload.get("turns")
        if not isinstance(turns, list):
            raise unreadable("the context store", "return the stored turns", payload)
        # **A conversation is not built on a hole.**  The store reports a row
        # with no turn, or a turn whose body cannot be read, rather than
        # dropping it or reading it as empty; the live context refuses both.
        # The window's own transcript reader draws the damage instead, which is
        # why the report is a value here rather than an error out of the store.
        missing = payload.get("missing") or []
        unreadable_rows = payload.get("unreadable") or []
        if not isinstance(missing, list) or not isinstance(unreadable_rows, list):
            raise unreadable("the context store", "report the restore's damage", payload)
        if missing or unreadable_rows:
            raise RuntimeError(
                "the stored conversation cannot be restored: "
                + (
                    f"turn(s) {missing} in the live context have no row; "
                    if missing
                    else ""
                )
                + (
                    f"the stored body of turn(s) {unreadable_rows} cannot be read; "
                    if unreadable_rows
                    else ""
                )
                + "repair the turn store (restore a backup, or delete it and "
                "start afresh)"
            )
        # **The status bar's figure, taken from the turn it restored.**  A
        # conversation that has only restored has run no turn of its own, so
        # this is the size the bar shows until one does.  A size nobody
        # measured reads 0 — a value of another type is a stored row's fact and
        # is read the same way, rather than failing a restore over a display
        # fact.
        context_tokens = 0
        if turns:
            last = turns[-1]
            if isinstance(last, dict):
                found = last.get("context_tokens")
                if isinstance(found, int) and not isinstance(found, bool):
                    context_tokens = found
        return Restored(
            messages=restored,
            turn_ids=turn_ids,
            covered=len(restored),
            context_tokens=context_tokens,
        )

    async def decide_context(
        self,
        identity: ConversationId,
        messages: list[Message],
        model: str,
        *,
        context_tokens: int = 0,
        prompt_window: int = 0,
    ) -> Decided:
        """Ask the decision model for a selection without applying it.

        **The first half of the automatic rebuild.**  The agent server's
        boundary calls this, then calls `_context_rebuild` with the selection
        it returned; that call carries the decision's arguments from the start,
        so the pair is never an empty call.  No store is read and nothing is
        written here.

        `context_tokens` and `prompt_window` are the figure the instruction
        prints — the same one the status bar shows.  An answer this build cannot
        read is the context store's refusal (`unreadable`), not a decision.

        **A harness failure pair is not handed on.**  The conversation keeps the
        pair the failed rebuild wrote; the decision model must not see it as an
        example of the tool it is about to fill (`_decision_messages`).
        """
        payload = tool_payload(
            await (await self.memory()).call_tool(
                "decide",
                {
                    "agent": identity[0],
                    "subagent": identity[1],
                    "messages": [
                        _context_message(message)
                        for message in _decision_messages(messages)
                    ],
                    "model": model,
                    "context_tokens": context_tokens,
                    "prompt_window": prompt_window,
                },
            )
        )
        arguments = payload.get("arguments")
        if not isinstance(arguments, (str, dict)):
            raise unreadable(
                "the context store", "return the decision's arguments", payload
            )
        decision = payload.get("decision")
        if not isinstance(decision, str):
            raise unreadable(
                "the context store", "report the decision's status", payload
            )
        return Decided(arguments=arguments, decision=decision)

    async def rebuild_arguments(
        self,
        identity: ConversationId,
        messages: list[Message],
        model: str,
        *,
        context_tokens: int = 0,
        prompt_window: int = 0,
    ) -> dict[str, Any]:
        """The first boundary's `_context_rebuild` arguments — asked once.

        **The decision is asked before the tool is called**, so the pair the
        loop writes carries the selection from the start (SDD §5.3).  A
        provider failure is carried into the arguments as the failure's own
        words (`failure_text`), so the tool refuses them and the agent reads a
        tool error naming what went wrong rather than an empty call.  The
        sentence is `failure_selection`: it also says the context in hand was
        kept, so the pair cannot be read as a malformed model answer — and the
        next decision's input filters it out (`_decision_messages`) rather than
        offering it as an example.

        **The pair the loop writes carries the tool's own argument names.**  A
        decision answer in another shape — a historical `context`/`mode` call the
        model copied, or a half it left out — is carried as the failure
        selection, so the tool call is a `ToolError` the agent reads rather than
        a dispatch validation error the harness cannot turn into a result.  The
        tool body still makes the one legality check; this is the transport's
        guard that the call reaches it.

        **One rule for the agent, its subagents and an agent task.**  The two
        servers call this instead of each shaping the failure's sentence; a
        second copy is how one of them ends up saying something the other does
        not.  A payload this build cannot read is not a provider failure — it is
        a context store from another build, and it fails the turn
        (`UnreadableAnswer`, SDD Appendix C).
        """
        try:
            decided = await self.decide_context(
                identity,
                messages,
                model,
                context_tokens=context_tokens,
                prompt_window=prompt_window,
            )
        except UnreadableAnswer:
            raise
        except Exception as exc:  # noqa: BLE001 - a fallback, not a failure mode
            logger.info(
                "context_rebuild_skipped reason=%s", decisions.failure_text(exc)
            )
            return {
                "keep": decisions.failure_selection(
                    "the decision call failed: " + decisions.failure_text(exc)
                ),
                "recall": {},
            }
        if decided.decision:
            return {
                "keep": decisions.failure_selection(
                    "the decision model did not answer: " + decided.decision
                ),
                "recall": {},
            }
        selection = selection_from_arguments(decided.arguments, "keep")
        try:
            decisions.rebuild_selection(selection.get("keep"), selection.get("recall"))
        except ValueError as exc:
            logger.info("context_rebuild_unreadable reason=%s", exc)
            return {
                "keep": decisions.failure_selection(
                    "the selection could not be read: " + str(exc)
                ),
                "recall": {},
            }
        return {"keep": selection.get("keep"), "recall": selection.get("recall")}

    async def rebuild(
        self,
        identity: ConversationId,
        messages: list[Message],
        turn_ids: list[int],
        covered: int,
        model: str,
        *,
        publish: bool = True,
        context_tokens: int = 0,
        prompt_window: int = 0,
        selection: dict[str, Any] | None = None,
    ) -> Rebuilt:
        """Decide what this turn runs on: keep ∪ recall.

        **Called after the turn's message is in the list** — at the first
        boundary, or mid-turn after a cut-in — so there is no prompt to pass
        beside it: the conversation is the input, and the current turn is the
        `carried` tail.

        `context_tokens` and `prompt_window` are the figure the status bar
        shows — the last LLM call's size against the space the prompt has — and
        are carried so the decision's instruction prints the same one.

        `carried` is what the message list holds beyond the turns in
        `turn_ids`: the **unbacked run at its end** — a cancelled turn's
        repair, a failed turn, a turn the store would not record.  It is
        exactly `len(messages) - covered` because `covered` counts a prefix.
        The store puts those messages back verbatim, so a rebuild replaces the
        *turns* without destroying something that has no turn to be replaced by.

        **`publish=False` is the clone an agent task runs on.**  The decision is
        that turn's own and is not written back: a task records nothing, so its
        context selection must not move the conversation it stands in for.  The
        store is not asked for its list and nothing is written.

        **All of the answer is read before the caller adopts any of it.**  An
        answer that replaced the list but omitted the counts would report
        `kept 0/0, recalled 0/0` — a decision nobody made; a rerank status and the
        reason no decision was made are part of the same shape, and an
        unrecognised one still reaches the window, which is where its words live.
        """
        carried = max(len(messages) - covered, 0)
        arguments: dict[str, Any] = {
            "agent": identity[0],
            "subagent": identity[1],
            "messages": [_context_message(message) for message in messages],
            "turn_ids": list(turn_ids),
            "model": model,
            "carried": carried,
            "publish": publish,
            "context_tokens": context_tokens,
            "prompt_window": prompt_window,
            # `None` is the decision ("choose for me"); a mapping is the
            # selection the caller already made (the model's own tool call).
            "selection": selection,
        }
        payload = tool_payload(
            await (await self.memory()).call_tool("rebuild", arguments)
        )
        rebuilt = _message_list(payload)
        rebuilt_ids = _turn_id_list(payload)
        changed = payload.get("changed")
        if not isinstance(changed, bool):
            raise unreadable(
                "the context store", "report whether the context changed", payload
            )
        kept = _count(payload, "kept")
        in_hand = _count(payload, "in_hand")
        outside = _count(payload, "outside")
        recalled = _count(payload, "recalled")
        fitted = _count(payload, "fitted")
        rerank = payload.get("rerank")
        if not isinstance(rerank, str):
            raise unreadable(
                "the context store", "report the recall's rerank status", payload
            )
        decision = payload.get("decision")
        if not isinstance(decision, str):
            raise unreadable(
                "the context store",
                "report the decision's status",
                payload,
            )
        reason = payload.get("reason")
        if not isinstance(reason, str):
            raise unreadable(
                "the context store", "report which no-op it chose", payload
            )
        return Rebuilt(
            messages=rebuilt,
            turn_ids=rebuilt_ids,
            covered=len(rebuilt) - carried,
            changed=changed,
            kept=kept,
            in_hand=in_hand,
            outside=outside,
            recalled=recalled,
            rerank=rerank,
            decision=decision,
            fitted=fitted,
            reason=reason,
        )

    # ── the turn's own compaction ────────────────────────────────────────

    async def select_live(
        self,
        identity: ConversationId,
        messages: list[Message],
        selectable: list[str],
        model: str,
        prompt_window: int,
        context_tokens: int = 0,
    ) -> LiveChoice:
        """Ask the live selector which of this turn's messages it no longer needs.

        **A harness call, not a model tool**: the model reaches compaction
        through `_turn_compact`, and this is the call that asks the selector.
        The answer is the forced tool's raw argument text, not a parsed list:
        `_turn_compact` (here, `compact_turn`) checks it.  Only the payload's
        own shapes are refused — `arguments` that is neither text nor a mapping,
        or a status that is not text — as a build this one cannot read
        (`slife2.libs.plugin.protocol.unreadable`).

        The ids are the *live view's* ids — the `[MESSAGE:{"id": …}]` the caller
        wrote onto each message, not positions: hiding one message does not
        renumber the rest, and the same id addresses the same message across the
        turn.  `context_tokens` and `prompt_window` are the figure the status
        bar shows, carried so the selector's instruction can print the same one.

        **A harness failure pair is not handed on**, for `decide_context`'s
        reason: the view still holds the failed `_turn_compact` pair, and a
        selector that copied its string shape would write the same failure.  The
        view the tool itself applies a selection to is untouched.
        """
        payload = tool_payload(
            await (await self.memory()).call_tool(
                "select_live",
                {
                    "agent": identity[0],
                    "subagent": identity[1],
                    "messages": [
                        message.to_wire() for message in _decision_messages(messages)
                    ],
                    "selectable": list(selectable),
                    "model": model,
                    "context_tokens": context_tokens,
                    "prompt_window": prompt_window,
                },
            )
        )
        arguments = payload.get("arguments")
        if not isinstance(arguments, (str, dict)):
            raise unreadable(
                "the context store", "return the selector's arguments", payload
            )
        decision = payload.get("decision")
        if not isinstance(decision, str):
            raise unreadable(
                "the context store", "report the selector's status", payload
            )
        return LiveChoice(arguments=arguments, decision=decision)

    async def compact_arguments(
        self,
        identity: ConversationId,
        view: list[Message],
        selectable: list[str],
        model: str,
        *,
        context_tokens: int = 0,
        prompt_window: int = 0,
    ) -> dict[str, Any]:
        """The automatic `_turn_compact` arguments — asked once.

        **The selector is asked before the tool is called**, for
        `rebuild_arguments`' reason: the pair carries the selection from the
        start, and a failure is carried as its own words so the tool refuses it
        and the agent reads a tool error naming what went wrong.  A payload this
        build cannot read raises `UnreadableAnswer` and fails the turn.

        `view` is the projection the caller built with `compact.due`, and
        `selectable` is its candidate set — the ids the selector may name.
        """
        try:
            choice = await self.select_live(
                identity,
                view,
                selectable,
                model,
                prompt_window,
                context_tokens=context_tokens,
            )
        except UnreadableAnswer:
            raise
        except Exception as exc:  # noqa: BLE001 - a fallback, not a failure mode
            logger.info("turn_compact_skipped reason=%s", decisions.failure_text(exc))
            return {
                "compact": "the selector call failed: " + decisions.failure_text(exc)
            }
        if choice.decision:
            return {"compact": "the selector did not answer: " + choice.decision}
        return selection_from_arguments(choice.arguments, "compact")

    async def compact_turn(
        self,
        identity: ConversationId,
        messages: list[Message],
        hidden: set[str],
        model: str,
        prompt_window: int,
        context_tokens: int = 0,
        selection: Any = None,
    ) -> compact.Compacted:
        """Run one live compaction over a selection, hiding what it names.

        **The selection is the caller's; the harness only checks it.**  Both
        servers ask the selector before the call and pass its answer here (a
        failure arrives as its text, `compact_arguments`).  A direct caller may
        pass `None`, and then the selector is asked here — the same seam the
        context plugin's `rebuild` keeps for a caller that has no selection of
        its own.

        **A selection is read before it is applied, and the exchange rule after
        that.**  The model's own call writes the same `compact` list the
        selector writes, so it is read by the same parser
        (`context.parse_live`); a value of another shape is a refusal the model
        can correct, not a list to guess at — a string is iterable, so `"12.3"`
        would otherwise be read as four ids.  An id the view did not offer is
        refused whole rather than filtered out, because the rest would be a
        selection the model did not make.  **A tool exchange named in part is
        kept whole and the rest of the selection is applied**
        (`compact.split_exchanges`): the pair is the unit the model has to name
        whole, and that one unstated decision does not discard the ids it did
        state.  The kept groups are reported in `Compacted.held`.

        `hidden` is the turn's own state and is mutated here; the caller's
        `present` callback reads it, and the turn's saved row never does.
        """
        at = compact.opening(messages)
        if at is None:
            return compact.Compacted(seen=0, dropped=0)
        view = compact.view(messages, hidden)
        at_view = compact.opening(view)
        opening_mid = view[at_view].mid if at_view is not None else ""
        candidates = compact.selectable(view, opening_mid)
        if not candidates:
            return compact.Compacted(seen=len(view), dropped=0)
        if selection is None:
            choice = await self.select_live(
                identity,
                view,
                candidates,
                model,
                prompt_window,
                context_tokens=context_tokens,
            )
            if choice.decision:
                return compact.Compacted(
                    seen=len(view), dropped=0, decision=choice.decision
                )
            selection = _compact_value(choice.arguments)
        # **One parser for both callers.**  The selector's raw answer and the
        # model's own call go through the same `parse_live`; the tool reports a
        # refusal as its error result, and no repair is attempted.
        drop, refusal = decisions.parse_live(
            json.dumps({"compact": selection}, ensure_ascii=False)
        )
        if drop is None:
            return compact.Compacted(
                seen=len(view),
                dropped=0,
                refused=f"the selection could not be read: {refusal}",
            )
        unknown = compact.unknown_ids(candidates, drop, hidden)
        if unknown:
            return compact.Compacted(
                seen=len(view),
                dropped=0,
                refused=(
                    "the selection named "
                    + ", ".join(repr(mid) for mid in unknown)
                    + ", which this turn's view does not offer — the system "
                    "prompt and the opening message are fixed, and every other "
                    "id is the one its [MESSAGE:…] annotation shows"
                ),
            )
        # **The pair is filtered, not the selection.**  A half-named exchange is
        # the one thing the model left unstated; the ids it did name are its
        # decision and are applied.  Refusing the whole list here would make one
        # missed member discard every valid drop in the same answer.
        held = compact.split_exchanges(view, drop)
        if held:
            kept = {mid for group in held for mid in group}
            drop = [mid for mid in drop if mid not in kept]
        applied = [mid for mid in drop if mid not in hidden]
        hidden.update(applied)
        return compact.Compacted(seen=len(view), dropped=len(applied), held=held)

    async def rebuild_call(
        self,
        identity: ConversationId,
        view: LiveView,
        live: AgentLoop | None,
        model: str,
        *,
        context_tokens: int,
        prompt_window: int,
    ) -> AutoCall | None:
        """The once-per-turn `_context_rebuild` call, or `None` when not due.

        **The decision is asked here, before the call**, so the pair the loop
        writes carries its selection from the start rather than starting empty
        and being rewritten.  A hub that does not advertise the tool is a build
        that predates it; the turn then runs on the context it has instead of
        writing an error pair at every boundary, and the attempt is not repeated
        (`rebuilt` is set before the declaration is read).
        """
        if view.rebuilt or live is None:
            return None
        view.rebuilt = True
        if not live.declares(CONTEXT_REBUILD):
            return None
        selection = await self.rebuild_arguments(
            identity,
            view.messages,
            model,
            context_tokens=context_tokens,
            prompt_window=prompt_window,
        )
        return AutoCall(CONTEXT_REBUILD, arguments=selection)

    async def compact_call(
        self,
        identity: ConversationId,
        view: LiveView,
        live: AgentLoop | None,
        model: str,
        *,
        context_tokens: int,
        prompt_window: int,
    ) -> AutoCall | None:
        """The `_turn_compact` call when the threshold is due, or `None`.

        **One threshold rule and one decision call for both servers** — a task
        runs one turn and has no next turn to recover on, so the same rule is
        what keeps it alive.  The guard on the tool being declared keeps a hub
        from a build that predates it from paying a decision call at every
        boundary.
        """
        asked = (
            compact.due(
                view.messages,
                view.hidden,
                last_tokens=live.last_usage.total_tokens,
                prompt_window=prompt_window,
                compact_at=view.compact_at,
                held=view.compact_hold,
            )
            if live is not None and live.declares(TURN_COMPACT)
            else None
        )
        if asked is None:
            return None
        shown, selectable = asked
        selection = await self.compact_arguments(
            identity,
            shown,
            selectable,
            model,
            context_tokens=context_tokens,
            prompt_window=prompt_window,
        )
        return AutoCall(TURN_COMPACT, arguments=selection)

    async def apply_compaction(
        self,
        identity: ConversationId,
        view: LiveView,
        model: str,
        *,
        selection: Any,
        context_tokens: int,
        prompt_window: int,
    ) -> compact.Compacted:
        """The shared body of `_turn_compact`: refuse, apply, and hold the pass.

        **The arguments are the selection, and a call must carry one.**  The
        servers word the refusal in their own exception type; `compact.Refusal`
        is how this hands over the sentence without importing either.  A string
        is the harness's failure selection and an unreadable one is a decision
        about the whole view, so both stop the automatic pass; a decision to
        keep everything stops it for the same reason — re-asking would pay for
        an answer already given — and a drop leaves it live.

        **A half-named exchange is not a refusal**: the pair stays in the view
        and the other ids the model named are applied, so one missed member does
        not discard the rest of its answer.  The report names the pair, and the
        automatic pass stays live when something else dropped.
        """
        if selection is None:
            raise compact.Refusal(compact.NO_SELECTION)
        if isinstance(selection, str):
            view.compact_hold = True
            raise compact.Refusal(compact.unreadable_selection(selection))
        result = await self.compact_turn(
            identity,
            view.messages,
            view.hidden,
            model,
            context_tokens=context_tokens,
            prompt_window=prompt_window,
            selection=selection,
        )
        if result.refused:
            view.compact_hold = True
            raise compact.Refusal(result.refused)
        view.compact_hold = result.dropped == 0 or bool(result.decision)
        return result

    async def apply_rebuild(
        self,
        identity: ConversationId,
        view: LiveView,
        model: str,
        *,
        publish: bool,
        context_tokens: int,
        prompt_window: int,
        keep: Any,
        recall: Any,
    ) -> Applied | None:
        """The shared body of `_context_rebuild`; `None` for a view with no opening.

        **`keep` and `recall` are the decision's two halves**, parsed by
        `rebuild_selection` here because both servers must refuse the same
        shapes.  The list the view is made of moves in place, the opening's new
        index is returned for the agent server to keep, and the compaction view
        starts over — a history edit is the moment a per-turn projection ends.
        """
        at = compact.opening(view.messages)
        if at is None:
            return None
        selection = decisions.rebuild_selection(keep, recall)
        rebuilt = await self.rebuild(
            identity,
            view.messages,
            view.turn_ids,
            view.covered,
            model,
            publish=publish,
            context_tokens=context_tokens,
            prompt_window=prompt_window,
            selection=selection,
        )
        # **The prefix moves; the suffix does not.**  `carried` is everything
        # from the opening on, and its length is what the new index preserves.
        carried = len(view.messages) - at
        view.messages[:] = rebuilt.messages
        view.turn_ids = rebuilt.turn_ids
        view.covered = rebuilt.covered
        view.hidden.clear()
        view.compact_hold = False
        return Applied(
            rebuilt=rebuilt,
            opening=len(rebuilt.messages) - carried,
            text=_rebuild_answer(view.messages, rebuilt, prompt_window),
        )

    # ── the turn boundary ────────────────────────────────────────────────

    async def trim(
        self,
        messages: list[Message],
        observer: TurnObserver = NULL_OBSERVER,
    ) -> list[str]:
        """Bring the model's tool list back within its budget — and say so in
        the turn, rather than doing it behind the model's back.

        **A turn boundary, and the harness's own call.**  The tools a model has
        loaded go out with every request, so a turn that loaded several has
        left the list longer than the config allows — and it is trimmed here
        rather than by the gate because this is the moment nothing is in
        flight: the list is rebuilt before every *model call*, so dropping the
        excess mid-turn would take away a tool the model had just loaded and
        was about to use.

        **What went is written into the conversation** as the harness pair
        `_func_tool_unload` called with no names, so a reader of the record
        reads that the list shrank and which tools left, rather than reaching
        for one it still believes it has.  Nothing is written when nothing
        moved: an empty pair would be a record of something that did not
        happen.  The two appends have no await between them, so an
        interruption cannot leave an assistant message whose call is never
        answered.

        **The pair's answer is also a note on the caller's stream.**  It is the
        tool's answer — the five lists the model reads — drawn as a harness line
        rather than a panel (`slife2.tui.draw`, which turns them into the names
        that left), so a person sees which tools the model lost without reading
        the call.  The events are emitted here because this is the one place
        that knows the pair; a caller with nobody watching passes the default
        observer, which drops them.

        **A failure is raised, not swallowed.**  The turn is over and has been
        answered, and how to report bookkeeping is each caller's: the agent
        server warns and trusts its next tool-list read to surface a dead hub;
        a task says it at error because it has no next turn to surface it on.
        """
        found = await unload_tools(await self.hub())
        unloaded, text = trim_answer(found)
        if not unloaded:
            return []
        call = harness_call(FUNC_TOOL_UNLOAD)
        messages.append(Message(role="assistant", content=None, tool_calls=[call]))
        messages.append(Message(role="tool", content=text, tool_call_id=call.id))
        await emit(observer, ToolCallStarted(call.id, call.name, call.arguments))
        await emit(
            observer,
            ToolCallFinished(
                call_id=call.id,
                name=call.name,
                ok=True,
                result_preview=preview(text),
                result_chars=len(text),
                # Nobody measured it: zero is the display's "nobody said".
                elapsed_ms=0,
            ),
        )
        return unloaded

    async def make_loop(
        self,
        identity: ConversationId,
        model: ModelRef,
        tools: Callable[[], Awaitable[ToolRegistry]],
        *,
        refresh: Callable[[], Awaitable[ToolRegistry]] | None = None,
        max_iterations: int,
        approve: Callable[[ToolCall], Awaitable[bool]] | None = None,
        deliver: Deliver | None = None,
        compress: Compress | None = None,
        present: Callable[[list[Message]], list[Message]] | None = None,
    ) -> AgentLoop:
        """Build the loop a turn runs in.

        **The backend is opened before the tool list is asked for**, which is
        the order both servers use: a model that cannot be reached fails the
        turn before the hub is bothered.

        `tools` builds the first registry; `refresh` builds the one asked for
        before every model call.  They are two callables rather than one
        because the agent server deliberately does not hand its observer to the
        first build — a display has no turn to report on yet — and a change to
        either must reach the same place.  A task passes the same factory for
        both.  `deliver` stays None for a task: a task is one turn, so there is
        no later turn for a background result to arrive in.  The result is kept
        for `check_async` instead, and the startup sentence says so
        (`slife2.libs.harness.async_tasks.started`).
        """
        backend = await self.model(identity, model)
        return AgentLoop(
            backend,
            await tools(),
            max_iterations=max_iterations,
            refresh=refresh,
            approve=approve,
            deliver=deliver,
            compress=compress,
            present=present,
            # **The conversation, for the two async tools.**  A task id is not
            # a capability: a model that learns another conversation's id must
            # not be able to poll or cancel that call (`Task.scope`).  Taken
            # here, where the identity and the caller's home are both in hand.
            background_scope=conversation_key(*identity),
        )

    async def close(self) -> None:
        """Close what this runtime opened, and forget it so the next call
        reopens.

        **Only what it opened.**  An injected client belongs to whoever handed
        it in.  Closing is not enough without clearing: a lifespan is not the
        process's lifetime over the in-memory transport, so a client left here
        after being closed is one the next session is handed already shut, and
        every turn of that session fails at its first hop.  Clearing is what
        lets `memory()` and `hub()` open again.

        No lock is taken: a close racing an open is the lifespan's own
        boundary, and a cancellation arriving here must still win.
        """
        for client in self._clients.values():
            await close_server(client)
        self._clients.clear()
        self._backends.clear()
        for client in self._contexts.values():
            await close_server(client)
        self._contexts.clear()
        for client in self._hubs.values():
            await close_server(client)
        self._hubs.clear()
        for client in self._cabinets.values():
            await close_server(client)
        self._cabinets.clear()
