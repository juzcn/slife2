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
two and records the last; the task holds its own lock and hands the answer
over.  What this supplies is the I/O every such turn makes, and the strict
reading of the context store's answers.

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
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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
    close_server,
    open_server,
    tool_payload,
    unreadable,
)
from slife2.libs.turn import compact
from slife2.libs.turn.events import (
    NULL_OBSERVER,
    ContextChosen,
    ToolCallFinished,
    ToolCallStarted,
    TurnObserver,
    emit,
    preview,
)
from slife2.libs.turn.loop import AgentLoop, harness_call
from slife2.libs.turn.messages import Message, ToolCall
from slife2.libs.turn.tools import ToolRegistry

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
    """What a turn runs on after the discriminator chose.

    `covered` counts the rebuilt turns; what follows them is the caller's
    carried tail, which the store re-appended verbatim.  `chosen` is the
    decision as the event that reports it.
    """

    messages: list[Message]
    turn_ids: list[int]
    covered: int
    chosen: ContextChosen


@dataclass(frozen=True)
class LiveChoice:
    """One live selector's answer: the view ids to drop, and why not if none.

    `drop` is empty for both "nothing worth dropping" and "no decision", and
    `decision` tells the two apart the way `ContextChosen.discriminator` does
    for the rebuild.
    """

    drop: list[int]
    decision: str = ""


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
        raw = payload.get("messages")
        if not isinstance(raw, list):
            raise unreadable("the context store", "return a message list", payload)
        turn_ids = payload.get("turn_ids")
        if not isinstance(turn_ids, list):
            raise unreadable("the context store", "return a turn-id list", payload)
        turns = payload.get("turns")
        if not isinstance(turns, list):
            raise unreadable("the context store", "return the stored turns", payload)
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
        restored = [Message.from_wire(item) for item in raw]
        return Restored(
            messages=restored,
            turn_ids=[int(turn_id) for turn_id in turn_ids],
            covered=len(restored),
            context_tokens=context_tokens,
        )

    async def rebuild(
        self,
        identity: ConversationId,
        messages: list[Message],
        turn_ids: list[int],
        covered: int,
        prompt: str,
        model: str,
    ) -> Rebuilt:
        """Decide what this turn runs on: keep ∪ recall.

        **Before the user's message is appended**, because the answer replaces
        the list and an appended message would be destroyed by it.  This is why
        what the person just said travels as `prompt` rather than being read
        back out of the list, where it is not yet.

        `carried` is what the message list holds beyond the turns in
        `turn_ids`: the **unbacked run at its end** — a cancelled turn's
        repair, a failed turn, a turn the store would not record.  It is
        exactly `len(messages) - covered` because `covered` counts a prefix.
        The store puts those messages back verbatim, so a rebuild replaces the
        *turns* without destroying something that has no turn to be replaced by.

        `expected` is the list this turn's snapshot came from.  A rebuild is
        usually a model call, and another process may run the same
        conversation's pre-turn work — an agent task of the same agent does —
        so a turn saved while the decision ran must not be erased by this
        write; the store's compare-and-set is what keeps that from happening.

        **All of the answer is read before the caller adopts any of it.**  An
        answer that replaced the list but omitted the counts would draw
        `[kept 0, recalled 0]`, a note reporting a decision nobody made; a
        rerank status and the reason no decision was made are part of the same
        shape, and an unrecognised one still reaches the window, which is where
        its words live.
        """
        carried = max(len(messages) - covered, 0)
        payload = tool_payload(
            await (await self.memory()).call_tool(
                "rebuild",
                {
                    "agent": identity[0],
                    "subagent": identity[1],
                    "messages": [message.to_wire() for message in messages],
                    "turn_ids": list(turn_ids),
                    "expected": list(turn_ids),
                    "prompt": prompt,
                    "model": model,
                    "carried": carried,
                },
            )
        )
        raw = payload.get("messages")
        if not isinstance(raw, list):
            raise unreadable("the context store", "return a message list", payload)
        rebuilt_ids = payload.get("turn_ids")
        if not isinstance(rebuilt_ids, list):
            raise unreadable("the context store", "return a turn-id list", payload)
        kept, recalled = payload.get("kept"), payload.get("recalled")
        if kept is None or recalled is None:
            raise unreadable(
                "the context store", "report the rebuild's counts", payload
            )
        rerank = payload.get("rerank")
        if not isinstance(rerank, str):
            raise unreadable(
                "the context store", "report the recall's rerank status", payload
            )
        discriminator = payload.get("discriminator")
        if not isinstance(discriminator, str):
            raise unreadable(
                "the context store",
                "report the discriminator's status",
                payload,
            )
        rebuilt = [Message.from_wire(item) for item in raw]
        return Rebuilt(
            messages=rebuilt,
            turn_ids=[int(turn_id) for turn_id in rebuilt_ids],
            covered=len(rebuilt) - carried,
            chosen=ContextChosen(
                kept=int(kept),
                recalled=int(recalled),
                rerank=rerank,
                discriminator=discriminator,
            ),
        )

    # ── the turn's own compaction ────────────────────────────────────────

    async def select_live(
        self,
        identity: ConversationId,
        messages: list[Message],
        selectable: list[int],
        model: str,
        context_tokens: int = 0,
        context_window: int = 0,
    ) -> LiveChoice:
        """Ask the live selector which of this turn's messages it no longer needs.

        **A harness call, not a model tool**: the model reaches compaction
        through `_turn_compact`, and this is the other half of that tool's body.
        The answer is read strictly — a drop that is not a whole id, or a status
        that is not text, is a build this one cannot read rather than an empty
        selection (`slife2.libs.plugin.protocol.unreadable`).

        The ids index the *view* the caller sent, which is the list the model
        will run on: the selector has never seen the record, and an id means
        nothing outside this call.  `context_tokens` and `context_window` are
        the figure the status bar shows, carried so the selector's instruction
        can print the same one.
        """
        payload = tool_payload(
            await (await self.memory()).call_tool(
                "select_live",
                {
                    "agent": identity[0],
                    "subagent": identity[1],
                    "messages": [message.to_wire() for message in messages],
                    "selectable": list(selectable),
                    "model": model,
                    "context_tokens": context_tokens,
                    "context_window": context_window,
                },
            )
        )
        drop = payload.get("compact")
        if not isinstance(drop, list):
            raise unreadable("the context store", "return a live drop list", payload)
        decision = payload.get("decision")
        if not isinstance(decision, str):
            raise unreadable(
                "the context store", "report the selector's status", payload
            )
        ids: list[int] = []
        for value in drop:
            if isinstance(value, bool) or not isinstance(value, int):
                raise unreadable(
                    "the context store", "return whole message ids", payload
                )
            ids.append(value)
        return LiveChoice(drop=ids, decision=decision)

    async def compact_turn(
        self,
        identity: ConversationId,
        messages: list[Message],
        hidden: list[Message],
        model: str,
        context_tokens: int = 0,
        context_window: int = 0,
    ) -> compact.Compacted:
        """Run one live compaction: ask, then hide what the answer names.

        **The answer is applied as it came.**  The prompt asks the model to drop
        a tool exchange as a whole; the harness does not close a split one,
        because repairing the answer would be the harness making a decision the
        model was asked to make.  A model that splits a pair writes a history
        the next call refuses — one failed turn, with the record intact and the
        next turn on the full list.

        `hidden` is the turn's own state and is mutated here; the caller's
        `present` callback reads it, and the turn's saved row never does.
        """
        at = compact.opening(messages)
        if at is None:
            return compact.Compacted(seen=0, dropped=0)
        view = compact.view(messages, hidden)
        candidates = compact.selectable(view, messages[at])
        if not candidates:
            return compact.Compacted(seen=len(view), dropped=0)
        choice = await self.select_live(
            identity, view, candidates, model, context_tokens, context_window
        )
        if choice.decision:
            return compact.Compacted(
                seen=len(view), dropped=0, decision=choice.decision
            )
        allowed = set(candidates)
        seen = {id(message) for message in hidden}
        added: list[Message] = []
        for index in choice.drop:
            if index not in allowed:
                continue
            message = view[index]
            if id(message) in seen:
                continue
            seen.add(id(message))
            added.append(message)
        hidden.extend(added)
        return compact.Compacted(seen=len(view), dropped=len(added))

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

        **The pair's own sentence is also a note on the caller's stream.**  It
        is the tool's answer — the same text the pair carries — drawn as a
        harness line rather than a panel (`slife2.tui.draw`), so a person sees
        which tools the model lost without reading the call.  The events are
        emitted here because this is the one place that knows the pair; a caller
        with nobody watching passes the default observer, which drops them.

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
