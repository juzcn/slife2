"""slife2-agent-task — the agent task: one loop of the agent's own, run on a
description, answered by a message.

An **agent task** is a background loop.  The model starts one with a name and a
description; the loop runs as the calling agent — the same system prompt, the
same model, the same tools — and its last message is handed back to that
conversation when it stops.  Nothing waits for it: the call that starts one
answers at once, and the answer arrives later as a message on the `agent-task`
channel, wearing the marker that says which task it was (SDD §9.3).

**It is a plugin because it holds loops**, which is what `slife2-agent` holds and
why that one is a plugin.  What it does *not* do is reach for anything the agent
*server* owns: the turn it runs is `slife2.libs.turn.loop.AgentLoop` **called with nothing
turned off** — `observer` already defaults to `NULL_OBSERVER` and `auto` to
`None`, which are exactly a task's two answers, nobody is watching and there is
no inbox to cut in from.  A behaviour this turn needs and the loop does not have
belongs in `AgentLoop` behind a parameter that defaults off, never in a copy of
the loop.

**A task runs on the agent's own context, under the agent's own key.**  Its
single turn is set up exactly as a turn of the agent's is — `restore` when the
loop is built, `rebuild` once before the turn, `_func_tool_unload` at the turn's
end — and the key every hop carries is `(agent, "")`, the agent's own
conversation.  SDD §5.5's *the context is chosen, not accumulated* has no exception
for a task: one that ran on whatever happened to be in the list would be the one
turn in this system whose context nobody decided.  `restore` and `rebuild` are
`slife2-context`'s and the trim is `slife2-toolhub`'s; the order they are called
in is `slife2.libs.turn.runtime`'s, one runtime shared with the agent server.

**Nothing is recorded, and that is a decision about tasks.**  A task's result
reaches the conversation as a message and *that* message is what the agent server
records, so writing the task's own round trip as well would file the same answer
twice under a description that is a rendered template rather than anything the person
said.  A task reads the agent's context and adds nothing to it.

Two things it deliberately does not have, each because nothing needs it rather
than because it was left out:

* **No cancellation repair of its own.**  The loop completes an interrupted
  batch for every caller (`slife2.libs.turn.loop`), so the list it leaves is well
  formed; and a task's list is local and dies with it, so there is no next
  request to poison either way.
* **No filtering of the tool set.**  The catalogue is one list for the whole
  system (`slife2-toolhub`), so a task may start a task and the registry here is
  **flat** — no parents, no depth.  `agent-task: max_running` is the bound
  instead; SDD §14.2 holds the one it does not bound.

The answer goes back through `slife2-tui`'s `submit`, which is this system's one
path for "something outside the conversation became a turn in it" (SDD §5.4).  A
window that is not showing that agent is a **failed delivery**: the work
happened, the answer reached no conversation, and the log says so — a task is
over either way, and running a turn for nobody to make the two agree is the
"carry on without the window" this system refuses.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.logcontext as logcontext
from slife2.clients.toolclient import acquire_tool, remote_tools, tool_lookup
from slife2.helpers.clock import now
from slife2.libs.harness.handover import DeliveryFailed, hand_over
from slife2.libs.host.config import (
    AGENT_TASK_SERVER_NAME,
    Config,
    load_cached,
)
from slife2.libs.host.paths import home_key
from slife2.libs.model.base import LLMBackend
from slife2.libs.plugin.audience import (
    FOR_THE_MODEL,
    ConversationId,
    conversation_key,
)
from slife2.libs.plugin.protocol import agent_reader, house_server, serve_plugin
from slife2.libs.plugin.vocabulary import (
    AGENT_TASK_CANCEL,
    AGENT_TASK_LIST,
    AGENT_TASK_START,
    AGENT_TASK_STATUS,
    CANCELLED,
    FAILED,
    FINISHED,
    RUNNING,
    UNKNOWN,
)
from slife2.libs.plugin.vocabulary import (
    AGENT_TASK_CHANNEL as CHANNEL,
)
from slife2.libs.plugin.vocabulary import (
    AGENT_TASK_MARKER as MARKER_PREFIX,
)
from slife2.libs.turn.context import turn_prompt_lead, with_lead
from slife2.libs.turn.loop import TurnResult
from slife2.libs.turn.messages import Message, ToolSpec
from slife2.libs.turn.prompt import render_instruction, render_system_prompt
from slife2.libs.turn.runtime import ModelRef, TurnRuntime
from slife2.libs.turn.tools import (
    ATTACH_IMAGE,
    CATALOGUE_MISSING,
    CHECK_NEW_INPUT,
    CatalogueAnswer,
    Tool,
    ToolFailed,
    ToolRegistry,
)

logger = logging.getLogger(__name__)

SERVER_NAME = AGENT_TASK_SERVER_NAME

#: This server's key in the config's `servers:` table, and the name of the
#: section that holds its one number.  It is the channel too: the key a plugin
#: runs under is the channel its answers come back on.
CONFIG_KEY = CHANNEL

#: **The two tools a task's model must not have.**  They act on a *live*
#: conversation: `_check_new_input` pops a queued message out of the running
#: loop's inbox and hands it to whoever called, and `attach_image` injects parts
#: into that loop's last user message.  A task shares the agent's identity by
#: design — same context, same store, same tools — but it is not that live loop,
#: and the shared identity is exactly what makes the two indistinguishable at
#: the agent server.  So they are withheld here, and a by-name call is answered
#: "not here" instead of stealing the main conversation's message or image.
LIVE_CONVERSATION_TOOLS = frozenset({CHECK_NEW_INPUT, ATTACH_IMAGE})

# The tools, the states, the channel and the marker are
# `slife2.libs.plugin.vocabulary`'s: each crosses a process boundary — the
# scheduler starts a task, the window draws its answer, the probe reads the
# result — so the readers import the names without loading this server.

#: The template a task is opened with.  Asked for by bare name, so it is the
#: shipped one — this is machinery, the way this system tells a loop that nobody
#: is listening, rather than something an operator writes.
OPENING_TEMPLATE = "agent_task.j2"

#: How many *ended* tasks the registry keeps, so that asking about one that has
#: finished still has an answer.  Bounded because a model can start tasks for as
#: long as somebody keeps asking it to, and forgotten from the tail.
RETIRED = 32

#: How much of a description a list row shows.  A row is a glance's worth: the
#: name is what the model addresses a task by, and the description is there so
#: that a name it chose a while ago still means something.
DESCRIPTION_CHARS = 120


@dataclass
class AgentTask:
    """One background loop, and everything this process knows about it.

    The five fields `report` answers with are a task's *whole* description:
    there is no message list here, no transcript and no result beyond the one
    being delivered, because none of those outlive the coroutine that produced
    them (SDD §9.3).  `status` is the delivery-gated fact: `finished` means the
    answer reached a window; a task whose answer could not be delivered is
    `failed`, however much work it did.
    """

    agent: str
    name: str
    description: str
    started_at: str
    #: The home the task was started in, **kept rather than looked up**, and
    #: that is not a detail: this object outlives the call that made it.  Its loop
    #: runs on after the request returns — so a `key` that asked
    #: `slife2.libs.host.paths.home` would be answered by whatever directory the
    #: *daemon* is in, and the task would be filed under a key that nothing else
    #: can find it by.  A home is a fact about where a thing was started.
    home: str = ""
    status: str = RUNNING
    #: Empty until the task stops, and the one field whose emptiness is a fact:
    #: a model asking about a task that is still running reads it as such.
    finished_at: str = ""
    #: The answer, held only from the moment the loop returns until it is handed
    #: over.  Never served to the model — asking is a question about *state* and
    #: the answer travels by message, so that a result cannot enter the
    #: conversation twice (SDD §9.3).
    text: str = ""
    #: Whether the answer reached the conversation.  Kept because "was it handed
    #: over" is the one fact about a task that nothing else can reconstruct — the
    #: delivery log is the process's, and the conversation cannot be asked
    #: whether something arrived.  `finished` implies `True`; a `failed` task may
    #: have delivered its failure message or may have delivered nothing.
    delivered: bool = False
    #: The running coroutine, kept so it is not collected mid-flight and so that
    #: `agent_task_cancel` has something to cancel.
    handle: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def key(self) -> tuple[str, str, str]:
        """`(home, agent, name)` — what this process's registry is keyed by.

        Keyed by the agent and not by the name, because this server is shared:
        two agents may each have a task called `research`, and neither may see
        the other's, stop it, or be capped by it.  And keyed by the home for the
        same reason one level up: two homes may each be running an agent called
        `jack`, whose tasks are as separate as their conversations.

        Read off the object rather than from the ambient home, because a task's
        loop outlives the call that started it — see the field.
        """
        return (self.home, self.agent, self.name)

    @property
    def task_id(self) -> str:
        """The task, named for a model to read.

        Built from the channel and the name rather than minted, for the reason
        SDD §3.2 gives for `(agent, subagent)`: a name cannot go stale, and a handle a
        caller has to keep is a thing that can be lost.  It also says where the
        answer will come from.

        `channel_for` is the spelling, and this is the name the *key* goes by in
        a report: one string with two readers, rather than two `f"{CHANNEL}:…"`
        that a later change to the format would have to find.
        """
        return channel_for(self)

    @property
    def identity(self) -> ConversationId:
        """`(agent, "")` — **the agent's own conversation, which is the point.**

        A task is the main agent's task, not a conversation beside it: the same
        prompt, the same model, the same tools, the same context store, and the
        same key.  So every hop it makes carries the agent's own conversation id
        — the tool list the hub hands back is bound to it, the model call goes
        out under it, and `restore` and `rebuild` are asked about *this agent's*
        context, which is what "it runs on the current agent's context" means at
        the only place that is observable.  A `turn_read` from inside a task
        answers from the log the conversation is also written to, because that
        is the log a task is working out of.

        The empty second half is `subagent`'s "this agent's own conversation",
        which is exactly what a task is running on (SDD §3.2).
        """
        return (self.agent, "")

    def report(self) -> dict[str, Any]:
        """The five fields, in one shape for three readers.

        The marker on the answer, every tool's reply and a row of
        `agent_task_list` all read this, so a model never has to translate
        between two spellings of one fact — which is the same reason the marker
        keys are named after the arguments and fields the tools use
        (SDD §5.5).
        """
        return {
            "task_id": self.task_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def unknown_report(name: str) -> dict[str, Any]:
    """The report for a name this process does not hold.

    `AgentTask.report`'s shape with the three facts that are not there left
    empty, because the two tools that ask about a task by name — the status and
    the cancel — answer a name they have never heard of the same way, and a model
    reading both should read one shape.  The id is still the key the task *would*
    have: it is derived from the name rather than minted, so a name that is not
    held is still named the way the answer would have named it.
    """
    return {
        "task_id": f"{CHANNEL}:{name}",
        "name": name,
        "status": UNKNOWN,
        "started_at": "",
        "finished_at": "",
    }


def channel_for(task: AgentTask) -> str:
    """The channel a task's answer comes back on: the key, and the task's name.

    The name is part of it because the window draws the bubble from the channel
    and nothing else (`slife2.tui.widgets.channel_prefix`), so `AsyncTask(name)> `
    is a fact the channel has to carry rather than one the window could guess.
    The *recorded* channel carries it too, which is what the `[TURN:{…}]`
    footnote hands the model (SDD §5.5) — so a result says which task it came from
    outside the marker as well as inside it.
    """
    return f"{CHANNEL}:{task.name}"


def marker(task: AgentTask, *, status: str = "") -> str:
    """What a task's answer wears, so the model knows which task it came from.

    The space between a marker and the words beside it is the joiner's to add,
    exactly as `slife2-wechat` adds its own (SDD §5.5).

    `status` is what the answer *is*, which is not always the object's status at
    the moment it is built: a successful loop is still `running` while its answer
    is being handed over (a task is `finished` only when it lands), and the
    marker has to say what the answer will be if it lands.
    """
    payload = task.report()
    if status:
        payload["status"] = status
    return f"{MARKER_PREFIX}{json.dumps(payload, ensure_ascii=False)}]"


def build_server(
    config: Config,
    *,
    backend: LLMBackend | None = None,
    context_client: Client | None = None,
    hub_client: Client | None = None,
    window_client: Client | None = None,
    source: Callable[[], Config] | None = None,
) -> FastMCP:
    """Build the agent-task server.

    **The same seams the agent server takes, and for the same reason**: a task is
    a turn, so exercising one means exercising a model call, a tool list, a
    restore, a rebuild and a hand-over — and every peer that would make those
    network calls can be handed in as a client over the in-memory transport
    instead.  `window_client` is the agent server's too, because the hand-over is
    the one thing a task does that a turn does not.

    `source` is the config to read when a task actually runs.  In production it
    is `load_cached`, so a `model_switch` or an edited prompt reaches the next
    task; a test hands in a config that no file backs, and without one the
    injected config is kept (`slife2.libs.host.config.for_the_call`).
    """

    #: Every task this process has run, live and recently ended, by the client
    #: id — `(home, agent, name)`.  **Not cleared on the way out**, for the
    #: reason the agent server's loop registry is not: the lifespan is not the
    #: process's lifetime over the in-memory transport, and clearing here would
    #: forget tasks every time a caller disconnected.
    tasks: dict[tuple[str, str, str], AgentTask] = {}

    #: The ended ones, oldest first, so that the cap has an order to forget in.
    #: Held as the objects rather than as keys: a name that has been started
    #: again is a *different* task under the same key, and evicting by key alone
    #: would throw away the new one.
    retired: list[AgentTask] = []

    #: **The middle of a task's turn**, shared with the agent server: the model
    #: clients, the context and hub connections, the restore, the rebuild and
    #: the trim.  What is a task's own is its store (the local message list) and
    #: its policy — it records nothing and delivers its answer by hand-over.
    runtime = TurnRuntime(
        config,
        backend=backend,
        context_client=context_client,
        hub_client=hub_client,
        source=source,
    )

    #: **One pre-turn read-then-write at a time for one conversation**, keyed by
    #: `(home, agent)` — which is what a conversation is (`AgentTask.key` minus
    #: the task's name).
    #:
    #: `restore` reads the agent's live-context list and `rebuild` writes it back
    #: (`set_context_turns`), and the two are **one step**: a rebuild's target is
    #: built from the snapshot its own restore read, so two tasks interleaving
    #: them write a list decided against a conversation that has moved — the
    #: task that wrote first runs its turn on a list the store no longer has, and
    #: the store keeps a decision taken before the other's turn was even in it.
    #: The cap allows `max_running` tasks for one agent, so this is a state two
    #: calls can reach rather than a hypothetical.
    #:
    #: The agent server holds its own `Loop.lock` across exactly this pair
    #: (`send_message`, which says why the restore has to be inside it).  This
    #: process cannot hold *that* lock — the agent's conversation is served by
    #: another daemon — so what is serialised here is the writers this process
    #: has: its own tasks, one turn's set-up at a time.
    conversations: dict[tuple[str, str], asyncio.Lock] = {}

    def conversation(task: AgentTask) -> asyncio.Lock:
        """This task's agent's lock, made on the first ask."""
        key = (task.home, task.agent)
        found = conversations.get(key)
        if found is None:
            found = asyncio.Lock()
            conversations[key] = found
        return found

    def current() -> Config:
        """The config a task is built from, read when it runs.

        Re-read rather than captured at start-up, so a `model_switch` or an
        edited prompt reaches the next task — the same rule the agent server
        follows, and here it is the whole of what makes a task's model current.
        """
        return runtime.current()

    async def registry(task: AgentTask) -> ToolRegistry:
        """The tools a task runs with: the hub's list, bound to the agent.

        Bound to the agent's own id, so a tool that reads this agent's store
        reads the agent's own — which is the same fact as the key being the
        agent's, seen from the tool side.

        **The hub's whole list, by name.**  A loop's capability is not a reason
        to special-case the catalogue: this loop has no `_async` (see below), so
        it will never hand the model a task id, but `check_async` and
        `cancel_async` are model tools like every other and arrive in the list
        the hub advertises.  The one exception is the two tools that act on a
        *live* conversation (`LIVE_CONVERSATION_TOOLS`): their declarations stay
        — dropping a name a model already knows sends it through
        `tool_search`/`func_tool_load` for ever — but the function behind each is
        a refusal, because this loop is not the conversation they mutate.
        """

        def refused(name: str):
            """The declaration a withheld tool keeps, backed by a refusal."""

            async def run(_arguments: dict[str, Any]) -> str:
                raise ToolFailed(
                    f"{name} is not available in an agent task: it acts on the "
                    f"agent's live conversation, and this task is not that "
                    f"conversation. Say what you need in your answer instead."
                )

            return run

        def withheld(name: str) -> Tool:
            """A stub for a by-name call the hub's list did not carry."""
            return Tool(
                spec=ToolSpec(name=name, description="", parameters={"type": "object"}),
                run=refused(name),
            )

        async def catalogue_has(name: str) -> CatalogueAnswer:
            """What the catalogue says about a name outside this request's list."""
            if name in LIVE_CONVERSATION_TOOLS:
                # A task has no live conversation for these to act on; the
                # catalogue has them for the agent, not for this model.
                return CATALOGUE_MISSING
            return (await tool_lookup(await runtime.hub(), name, task.identity)).answer

        async def catalogue_acquire(
            name: str, arguments: dict[str, Any]
        ) -> Tool | None:
            """The same name, made runnable when the call fits its current schema."""
            if name in LIVE_CONVERSATION_TOOLS:
                # **A by-name call is refused, not acquired.**  The registry
                # would otherwise fetch the real tool from the hub and run it —
                # the exact action this whole guard exists to prevent.
                return withheld(name)
            return await acquire_tool(
                await runtime.hub(), name, arguments, task.identity
            )

        # **The declaration stays, the behaviour goes.**  The hub advertises
        # these two to every model, and dropping them here would send a model
        # that knows their names through `tool_search`/`func_tool_load` in a
        # loop that can never load them.  A stub keeps the list the hub's own —
        # and makes the call a refusal the model reads, rather than an action on
        # somebody else's inbox.
        tools = [
            (
                tool
                if tool.spec.name not in LIVE_CONVERSATION_TOOLS
                else Tool(spec=tool.spec, run=refused(tool.spec.name))
            )
            for tool in await remote_tools(await runtime.hub(), task.identity)
        ]
        return ToolRegistry(tools, exists=catalogue_has, acquire=catalogue_acquire)

    # ── the loop ─────────────────────────────────────────────────────────

    async def run_loop(task: AgentTask) -> TurnResult:
        """One task's turn: the template as the user message, then the agent's
        own order around it — restore, rebuild, and the loop.

        **The steps are the turn runtime's** (`slife2.libs.turn.runtime`),
        shared with the agent server: the head is the agent's system prompt,
        `restore` puts back the context this conversation had, `rebuild` decides
        what the turn runs on, and the loop advances it.  What is a task's own
        is the store — a local list — the absence of an inbox, and the absence
        of a record, which is argued below.

        `opening` travels **beside** the list rather than inside it, exactly as
        the agent server passes its own prompt: the rebuild replaces the list
        wholesale, so anything appended before it would be destroyed by it, and
        `run_turn` is what appends the user message.

        **The one step that is missing is the record, and it is missing on
        purpose.**  A task's result reaches the conversation as a message, and
        *that* message is what the agent server records — so storing the task's
        own round trip as well would put the same answer in the transcript
        twice, under a description that is a rendered template rather than anything the
        person said.  A task reads the agent's context and adds nothing to it:
        what the conversation gains is the answer, which is the thing it asked
        for.  The agent server's `Loop.records` is its switch for the opposite
        case; there is no switch here because there is nothing to switch.
        """
        current = runtime.current()
        name, provider, settings = current.resolve("")
        model = ModelRef(f"{name}/{settings.model}", name, provider.api, settings.model)
        head = [
            Message(
                role="system",
                content=render_system_prompt(
                    current.agent.system_prompt, agent_name=task.agent
                ),
            )
        ]
        opening = render_instruction(
            OPENING_TEMPLATE, name=task.name, description=task.description
        )

        # **The two calls under the conversation's lock**, which is the rule the
        # agent server states for the same pair: a rebuild's target is built from
        # the list its own restore read, so a write that lands after the same
        # conversation's own write is a decision taken against a snapshot that
        # has moved.  Held here rather than inside the runtime because they are
        # one step — the second reads what the first answered.
        async with conversation(task):
            restored = await runtime.restore(task.identity, head)
            rebuilt = await runtime.rebuild(
                task.identity,
                restored.messages,
                restored.turn_ids,
                restored.covered,
                opening,
                model.reference,
            )

        loop = await runtime.make_loop(
            task.identity,
            model,
            partial(registry, task),
            # Asked again before every model call, as the agent server asks it:
            # a tool loaded mid-task is in the next call's list rather than
            # nowhere.
            refresh=partial(registry, task),
            max_iterations=current.agent.max_iterations,
            # **No `deliver`, and so no `_async` here — a stated limitation**
            # (SDD §14.2, "Background work inside an agent task").  This loop
            # runs one turn and has no second one for a background result to
            # arrive in, so there is nowhere to hand one back to: the loop
            # therefore *refuses* the mode with a sentence the model can act on
            # (`BACKGROUND_UNAVAILABLE`) rather than running the call quietly.
            # The background-call tools are still in the list like every other
            # model tool; there is simply no task id here for them to answer.
        )
        # **The task's prompt marker, and no strip after it.**  A task's messages
        # are not recorded — the answer is what the conversation gains — so
        # there is no row to keep the marker out of; it is prompt text for this
        # one turn and goes out of scope with the task.  The context figure is
        # the restored conversation's last turn, which is the number the agent's
        # status bar would have shown when the task started.
        opening = with_lead(
            opening, turn_prompt_lead(restored.context_tokens, settings.context_window)
        )
        result = await loop.run_turn(rebuilt.messages, opening)
        # After the turn and not during it: the tool list goes out with every
        # model call, so dropping the excess mid-turn would take away a tool the
        # model had just loaded and was about to use.
        await trim_tools(task, rebuilt.messages)
        return result

    async def trim_tools(task: AgentTask, messages: list[Message]) -> None:
        """Bring the model's tool list back within its budget — and say so in the
        turn, rather than doing it behind the model's back.

        **The write is the turn runtime's** (`slife2.libs.turn.runtime.trim`):
        the pair naming what left, with no await between its halves, is one
        mechanism for both servers.  What is a task's own is how a skip is
        reported.

        **A skip is a failure, and it is said at `error`.**  A task runs one
        turn and has no next request for a dead hub to make itself heard on, so
        the agent server's argument — "a hub that is really gone fails the next
        turn's tool list" — is not true here.  What is kept is the *outcome*
        (best-effort, so a task that has answered is not turned into a failed
        one by bookkeeping) and what is added is visibility: the skip is logged
        at `error`, naming the task, because the loaded set is one list for the
        whole system and an untrimmed one leaves every other conversation's
        request longer (SDD §9.3).  Registered as a fallback in SDD Appendix C:
        it *skips the trim* when the hub cannot be reached or answers with a
        shape this build cannot read; it *gives up* the trim for that turn; it
        *does not cover* a hub that is gone for good — there is no next turn
        here to surface it on, which is why the skip is said at `error`; the
        *failure goes* to the log as an error naming the task.
        """
        try:
            unloaded = await runtime.trim(messages)
        except Exception as exc:  # noqa: BLE001 - bookkeeping does not fail a turn
            logger.error(
                "agent task %s: the tool list was not trimmed (%s); the loaded "
                "set stays over budget for every conversation's next request",
                task.name,
                exc,
            )
            return
        if not unloaded:
            return
        logger.info(
            "agent task %s: %d tool(s) unloaded to stay within the budget: %s",
            task.name,
            len(unloaded),
            ", ".join(unloaded),
        )

    #: The steps here that are deliberately *not* taken, and why.
    #:
    #: **Nothing is recorded.**  A task's result reaches the conversation as a
    #: message, and *that* message is what the agent server records — so storing
    #: the task's own round trip as well would put the same answer in the
    #: transcript twice, under a description that is a rendered template rather than
    #: anything the person said.  A task reads the agent's context and adds
    #: nothing to it; what the conversation gains is the answer, which is the
    #: thing it asked for.  `Loop.records` is the agent server's switch for the
    #: opposite case, and there is no switch here because there is nothing to
    #: switch.

    async def run(task: AgentTask) -> None:
        """One task's whole life: run the loop, then hand the answer over.

        **Every failure is a message, not a silence** (SDD §5.1): a loop that raises
        still has an answer to deliver, and a task that vanished without one
        would leave the conversation waiting for something that will never
        arrive.  Cancellation is the one exception, and it is not one — a task
        that was called off has nothing to report, so it delivers nothing.

        **A task is `finished` only when its answer reached a window.**  The
        answer is the product and there is no result to keep (SDD §9.3), so a
        delivery that cannot be made ends the task `failed`: the work may have
        happened, but the task did not complete.  The loop's status stays
        `running` while the hand-over runs, which is what keeps a reader from
        seeing a completion that may still fail.

        **And the two things a task holds are dropped on every way out.**  The
        outer `try` is the whole life rather than the delivery alone, because a
        cancellation re-raises out of the `except` below and would otherwise take
        the path *past* the `finally` that lets go — and a cancelled task that
        kept its finished `asyncio.Task` would hold the cancelled coroutine's
        frames, and the answer beside them, for as long as the registry
        remembered it.  A task that has stopped holds nothing.
        """
        try:
            outcome = FINISHED
            # **One token for one task turn.**  A task runs a turn of the
            # agent's own conversation from a process beside the agent
            # server's, so the conversation alone cannot tell this turn's lines
            # from that server's; the token is what does.  It covers the turn's
            # failure line as well as its hops.  The delivery below is outside
            # it on purpose — the agent server mints the token of the turn that
            # delivery starts.
            with logcontext.turning(logcontext.new_turn()):
                try:
                    result = await run_loop(task)
                    task.text = (
                        result.text.strip()
                        or "(the task stopped without saying anything)"
                    )
                    task.finished_at = now()
                except asyncio.CancelledError:
                    # No await anywhere in this path: SDD §5.6 measured that a
                    # plain `await` in a cancelled scope is cancelled again at
                    # its next checkpoint, so anything awaited here would simply
                    # not happen.
                    task.status = CANCELLED
                    task.finished_at = now()
                    raise
                except Exception as exc:  # noqa: BLE001 - SDD §5.1's rule, one level down
                    logger.warning("agent task %s failed: %s", task.name, exc)
                    task.text = f"The agent task {task.name!r} failed: {exc}"
                    task.status = FAILED
                    task.finished_at = now()
                    outcome = FAILED
            # **The answer is the product, so a task is not `finished` until it
            # has reached a window.**  The loop's status stays `running` while
            # the hand-over runs, so a reader that asks in that moment is not
            # told a completion that may still fail; the marker carries the
            # outcome the answer will have if it lands.
            try:
                delivered = await deliver(task, status=outcome)
            except asyncio.CancelledError:
                task.status = CANCELLED
                task.finished_at = now()
                raise
            if outcome == FINISHED:
                task.status = FINISHED if delivered else FAILED
        finally:
            # **A task is retired when its ending is final** — for a successful
            # loop, after the answer has been handed over.  The cap counts
            # `running`, so a task that is still delivering holds its slot: it is
            # not finished yet, and this is the line that keeps the registry and
            # the contract saying the same thing.
            retire(task)
            task.handle = None
            # **The answer is let go of here, and this line is the design rather
            # than tidiness.**  A finished task's message list, its loop and its
            # tool registry are locals of `run_loop` and die with it; what
            # outlives a task is this object, and the only part of it worth
            # megabytes is the answer.  Nothing reads it again — the hand-over
            # was the only reader, and there is deliberately no tool that fetches
            # an answer back (SDD §9.3) — so keeping it would be holding a second
            # copy of something that is either already in the conversation or,
            # in the one case above, gone.  Keeping the *handle* would be the
            # same mistake in the other direction: a finished `asyncio.Task`
            # holds its result, so dropping the reference is what releases it.
            task.text = ""

    def retire(task: AgentTask) -> None:
        """Let go of an ended task, oldest first, past the cap.

        A task is over when its ending is final — for a successful loop, that is
        after the answer has been handed over, because a task that cannot deliver
        is `failed`.  It stays answerable until it is forgotten, which is what
        makes asking about one that has finished a question with an answer.
        What is bounded is how long that lasts.
        """
        retired.append(task)
        while len(retired) > RETIRED:
            oldest = retired.pop(0)
            if tasks.get(oldest.key) is oldest:
                del tasks[oldest.key]

    # ── the way back ─────────────────────────────────────────────────────

    async def deliver(task: AgentTask, *, status: str) -> bool:
        """Hand a task's answer to the conversation that asked for it.

        **The hand-over itself is `slife2.libs.harness.handover`** — one road,
        the window, and one failure when it cannot be made.  Every channel that
        puts a message into a conversation it does not own takes it, so what is
        left here is only the part that *is* an agent task's: what an answer
        wears, and what an undelivered answer *means*.

        **An undelivered answer is a failed task, not a stage.**  The answer is
        the product and there is no second copy of it (SDD §9.3), so a task whose
        answer reached no conversation did not finish, however much work it did.
        `run` sets the final status from what this returns; the log says why the
        answer went nowhere.  `status` is what the marker says the answer is —
        `finished` for a loop that succeeded, `failed` for one that did not —
        because the object is still `running` while a successful answer is being
        handed over.

        Returns whether the answer reached a window.
        """
        try:
            await hand_over(
                current(),
                task.agent,
                f"{marker(task, status=status)} {task.text}",
                channel_for(task),
                what=f"agent task {task.name}",
                window_client=window_client,
            )
        except DeliveryFailed as exc:
            logger.error(
                "agent task %s for %s was not delivered: %s", task.name, task.agent, exc
            )
            return False
        except Exception as exc:  # noqa: BLE001 — a task has nobody behind it to raise to
            # **A crash here is the one silence this task cannot afford.**  `run`
            # is a detached coroutine: an exception that leaves it is stored on a
            # task nothing reads, and the answer beside it is cleared by the
            # `finally` that unwinds — so the *work* would end in a trace nobody
            # saw instead of the message the agent is owed.  A failure to deliver
            # is a failure to deliver, however it arrived; it is said at `error`
            # with the trace, and `run` turns this `False` into a `failed` task.
            logger.exception(
                "agent task %s for %s was not delivered (%s); "
                "nothing else would report it",
                task.name,
                task.agent,
                exc,
            )
            return False
        task.delivered = True
        return True

    # ── whose task ───────────────────────────────────────────────────────

    #: The refusal this plugin says for a call that did not name a
    #: conversation — its own two words in the one shared sentence.
    agent_of = agent_reader("agent whose task it could be", "a task")

    def running(agent: str) -> int:
        """How many of **this home's** tasks for `agent` are still going.

        **Home-scoped as well as agent-scoped, because the registry is.**  A task
        is keyed by `(home, agent, name)` and `find` looks it up by that whole
        key — so a count that matched on the agent name alone would cap a start in
        one home on tasks running in another, which is a refusal neither the
        caller nor the operator could explain.  The home is read here, inside the
        call, which is the one place it is the caller's (`AgentTask.home` says why
        the objects keep their own).
        """
        return sum(
            1
            for task in tasks.values()
            if task.agent == agent
            and task.home == home_key()
            and task.status == RUNNING
        )

    def find(agent: str, name: str) -> AgentTask | None:
        found = tasks.get(conversation_key(agent, name))
        return found

    # ── the tools ────────────────────────────────────────────────────────

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        try:
            yield {}
        finally:
            # Closed *and* forgotten, which is what the runtime's close does as
            # well: this hook is not the process's lifetime over the in-memory
            # transport, so a client left in place is one the next session is
            # handed already shut — every task of it failing at its first hop.
            await runtime.close()
            # `tasks` is deliberately **not** cleared here, for the reason the
            # agent server gives about its loop registry: it is state the
            # process owns, and this hook is not the process's lifetime.  A
            # caller disconnecting must not forget the loops it started — which
            # is the whole of what a background task is.

    #  No `instructions`: each tool's own description says what the model cannot
    #  work out for itself, and the field is for guidance no tool carries.  A
    #  second telling of the same four tools is the version that goes stale first
    #  — `house_server` says the same thing about the spec's own advice.
    mcp: FastMCP = house_server(SERVER_NAME, lifespan=lifespan)

    @mcp.tool(name=AGENT_TASK_START, meta=FOR_THE_MODEL)
    async def agent_task_start(
        ctx: Context, name: str, description: str
    ) -> dict[str, Any]:
        """Start an agent task: one background loop that runs as you.

        For work that would hold you — long, or waiting on the home. The call
        returns at once and the answer reaches this conversation on its own: do
        not wait for it, and do not poll. **If no window is showing this
        conversation when it stops, the task ends `failed`** — the answer is the
        product and it is not kept, so there is nothing to fetch later.

        `description` is the task itself. It starts from this conversation's
        stored turns, so **the turn you are answering is not among them** — put
        in what the task needs from the last exchange, and let it read everything
        older for itself. It runs with your prompt, your model and your tools,
        and nobody can answer a question it raises: what it sends when it stops
        is the whole of what comes back.

        `name` is the handle — how the task is asked about and stopped. This is
        not a way to run a program in the background.

        Args:
            name: What to call the task — how it is asked about and stopped.
            description: The task itself: what to do, and what "done" looks
                like. It is the whole of what the task is given.
        """
        agent = agent_of(ctx)
        name = name.strip()
        if not name:
            raise ToolError("an agent task needs a name: it is how you address it.")
        if not description.strip():
            raise ToolError(
                f"agent task {name!r} needs a description — it is the task's whole "
                f"context, and nobody can answer a question it raises."
            )

        existing = find(agent, name)
        if existing is not None and existing.status == RUNNING:
            raise ToolError(
                f"an agent task named {name!r} is already running; stop it with "
                f"{AGENT_TASK_CANCEL} or wait for its answer."
            )

        # **The live config, and the two halves of this have to be read the same
        # way.**  The count below is *this home's* — `running` matches on the
        # `(home, agent, name)` key — so the number it is compared against has to
        # be this home's too, or a second home is bounded by a limit an operator
        # wrote in somebody else's file.  `current()` is the same read the model
        # is taken with and the iteration cap with (`run_loop`), and
        # `AgentTaskSettings.max_running` is written for it: the section this key
        # lives in is read by the plugin that enforces it, because the value is a
        # property of the home the caller is in and of the file as it is now —
        # the boot config is a snapshot of the daemon's own directory.
        limit = current().agent_task.max_running
        if running(agent) >= limit:
            raise ToolError(
                f"{limit} agent tasks are already running for this agent "
                f"(the `agent-task: max_running` cap); wait for one to finish, "
                f"or stop one with {AGENT_TASK_CANCEL}."
            )

        task = AgentTask(
            agent=agent,
            name=name,
            description=description,
            started_at=now(),
            # The home this call came in on, read *here*, at the only moment it
            # is the caller's (`slife2.libs.plugin.audience.conversation_key` explains why
            # the object keeps it rather than asking again later).
            home=home_key(),
        )
        tasks[task.key] = task
        # Detached from the call that starts it, which is the whole of what
        # "background" means here: the request that asked returns, and the loop
        # keeps going.  SDD §5.6's rule for a write from a cancelled turn is the same
        # rule — work created outside the cancelled scope does land.
        task.handle = asyncio.create_task(run(task))
        logger.info("agent task %s started for %s", name, agent)
        return task.report()

    @mcp.tool(name=AGENT_TASK_STATUS, meta=FOR_THE_MODEL)
    async def agent_task_status(ctx: Context, name: str) -> dict[str, Any]:
        """What one agent task is doing, by name.

        Its state — `running`, `finished`, `failed` or `cancelled` — and never
        the result, which arrives by itself and cannot be fetched. `finished`
        means the answer reached this conversation; a task whose answer could
        not be delivered is `failed`. Do not poll: if it is `running`, end your
        turn. `unknown` is a name no task here has.

        Args:
            name: The task's name, as `agent_task_start` was given it.
        """
        agent = agent_of(ctx)
        task = find(agent, name.strip())
        if task is None:
            return unknown_report(name.strip())
        return task.report()

    @mcp.tool(name=AGENT_TASK_LIST, meta=FOR_THE_MODEL)
    async def agent_task_list(ctx: Context) -> dict[str, Any]:
        """Every agent task this agent has, the running ones first.

        Each row is a task's id, name, state and times with a glance at its
        description, so that a name chosen a while ago still means something. It
        is bounded — the most recently ended tasks are kept and the tail is
        forgotten — so a row can go `unknown` like any other.
        """
        agent = agent_of(ctx)
        # **This home's tasks, and this home's only.**  The registry is keyed by
        # `(home, agent, name)` and every other reader goes through that key
        # (`find`), so a listing that matched on the agent name alone would name
        # another home's tasks — names, descriptions, states and times — for a
        # caller who could then neither inspect nor stop any of them, because
        # `agent_task_status` and `agent_task_cancel` do go through the key and
        # answer `unknown` for those very rows.
        here = home_key()
        mine = [
            task for task in tasks.values() if task.agent == agent and task.home == here
        ]
        # Running first, then the most recently started — the order the question
        # "what is still going" wants, and a total order rather than a ranking.
        # Two sorts and not one: `sorted` here is the recency order, and the
        # `rows.sort` below is stable, so it lifts the running ones to the front
        # without disturbing it.
        rows = []
        for task in sorted(mine, key=lambda t: t.started_at, reverse=True):
            row = task.report()
            row["description"] = task.description[:DESCRIPTION_CHARS]
            rows.append(row)
        rows.sort(key=lambda row: row["status"] != RUNNING)
        return {"tasks": rows}

    @mcp.tool(name=AGENT_TASK_CANCEL, meta=FOR_THE_MODEL)
    async def agent_task_cancel(ctx: Context, name: str) -> dict[str, Any]:
        """Stop an agent task, by name.

        Answers with the task's state *after* the call: `cancelled` when this is
        what stopped it, `finished` or `failed` when it was already over — a
        finished task still delivers, and a cancelled one never does. Nothing is
        rolled back: a tool call that was running is wherever it got to.

        Args:
            name: The task's name, as `agent_task_start` was given it.
        """
        agent = agent_of(ctx)
        task = find(agent, name.strip())
        if task is None:
            return unknown_report(name.strip())
        if task.status != RUNNING:
            # Already over: the answer is on its way, and it stays on its way.
            return task.report()
        # The state *is* the lock.  One process, one event loop, and no `await`
        # between reading the status above and writing it here — so this call
        # and the loop's own `finally` cannot interleave, and whichever gets
        # there first is the answer.
        task.status = CANCELLED
        task.finished_at = now()
        if task.handle is not None:
            task.handle.cancel()
        logger.info("agent task %s cancelled for %s", task.name, agent)
        return task.report()

    return mcp


def main(argv: list[str] | None = None) -> int:
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        # `load_cached` rather than the config: a task reads the file when it
        # runs, so a `model_switch` or an edited prompt reaches the next one.
        build=lambda config: build_server(config, source=load_cached),
        logger=logger,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
