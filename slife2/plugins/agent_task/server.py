"""slife2-agent-task — the agent task: one turn of the caller's own, run on a
description, answered by a message.

An **agent task** is a background turn.  The model starts one with a name and a
description; the turn runs as the conversation that asked for it — the agent's
own or one of its subagents' — with that conversation's system prompt, model,
tools and context, and its last assistant message is the task result.  Nothing
waits for it: the call that starts one answers at once, and the answer arrives
later as a message on the `agent-task` channel, wearing the marker that says
which task it was (SDD §9.3).

**It is a plugin because it holds loops**, which is what `slife2-agent` holds and
why that one is a plugin.  What it does *not* do is reach for anything the agent
*server* owns: the turn it runs is `slife2.libs.turn.loop.AgentLoop` with its
own `auto` — the rebuild at the first boundary and the compaction, never an
inbox, which a task does not have — and an observer that publishes the turn to
whoever is watching.  That observer is a reader, not a caller's
progress stream: a task has no caller waiting on it.  A behaviour this turn needs
and the loop does not have belongs in `AgentLoop` behind a parameter that
defaults off, never in a copy of the loop.

**A task runs on a clone of the caller's context, under the caller's own key.**
The key every hop carries is `(agent, subagent)`: the agent's own conversation
for a task the agent started, and the subagent's when a subagent started it.
`restore` reads that conversation's live-context list, and `rebuild` at the
turn's first boundary decides on that snapshot with `publish=False` — the
task's context is its own, and the conversation's list is not moved.  The head
is that conversation's system prompt, rendered the way the agent server renders
it.
SDD §5.5's *the context is chosen, not accumulated* has no exception for a
task: one that ran on whatever happened to be in a list would be the one turn in
this system whose context nobody decided.  `restore` and `rebuild` are
`slife2-context`'s and the trim is `slife2-toolhub`'s; the order they are called
in is `slife2.libs.turn.runtime`'s, one runtime shared with the agent server.

**Nothing is recorded, and that is a decision about tasks.**  A task's result
reaches the conversation as a message and *that* message is what the agent server
records, so writing the task's own round trip as well would file the same answer
twice under a description that is a rendered template rather than anything the person
said.  A task reads a clone of the caller's context and adds nothing to it — no
turn, and no context decision written back.

**Four of the model's tools are served here rather than by the agent server.**
A task is a live turn, but the agent server's live turn is a different one — the
same conversation id, a different message list, in a different process — so the
four tools that act on a live turn are the task's own
(`LIVE_CONVERSATION_TOOLS`).  `_check_new_input` has nothing to hand over: a task
has no inbox and no harness cut-in, so it answers "nothing waiting" and never
touches the maker's queue.  `attach_image` injects into the task's own single
user message — the rendered description — because that message *is* the turn's
request, and the cabinet key route is asked of the cabinet directly, as the
agent server asks it.  `_turn_compact` and `_context_rebuild` act on the task's
own list and its clone of the history.  A by-name call reaches the same four
bodies; none is left to fall through to the hub.

**A running task is watchable, and the watch is a read.**  The turn's observer
publishes to the task's tap and watchers (`slife2.libs.turn.watchhub`, shared
with the agent server); the stream opens with an empty snapshot and the agent's
plan, a `TaskBrief` with the description, the turn's events from its start, and
`Closed` with the ending.  Watching starts no turn, spends no model call, and
does not change the `finished` rule: the answer still reaches the conversation
by hand-over.

Two things it deliberately does not have, each because nothing needs it rather
than because it was left out:

* **No cancellation repair of its own.**  The loop completes an interrupted
  batch for every caller (`slife2.libs.turn.loop`), so the list it leaves is well
  formed; and a task's list is local and dies with it, so there is no next
  request to poison either way.
* **The rules are the prompt's, not the registry's.**  A task is one turn, so a
  result that arrives later — from a background tool call or from another task —
  has nowhere to land.  The opening template tells the model not to use
  `_async` or start another task; the hub's catalogue is unchanged.  The
  registry is **flat** — no parents, no depth — and `agent-task: max_running`
  bounds concurrency; SDD §14.2 holds the one it does not bound.

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
from pathlib import Path
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.logcontext as logcontext
import slife2.helpers.timeouts as _T
from slife2.clients.toolclient import acquire_tool, remote_tools, tool_lookup
from slife2.helpers.clock import now
from slife2.libs.harness import dumps
from slife2.libs.harness.handover import DeliveryFailed, hand_over
from slife2.libs.host.config import (
    AGENT_TASK_SERVER_NAME,
    Config,
    load_cached,
)
from slife2.libs.host.paths import home_key, subagent_file
from slife2.libs.host.subagents import subagent_spec
from slife2.libs.model.base import LLMBackend
from slife2.libs.plugin.audience import (
    FOR_THE_MODEL,
    ConversationId,
    conversation_meta,
)
from slife2.libs.plugin.protocol import (
    agent_reader,
    conversation_reader,
    house_server,
    serve_plugin,
    tool_payload,
    unreadable,
)
from slife2.libs.plugin.vocabulary import (
    AGENT_TASK_CANCEL,
    AGENT_TASK_LIST,
    AGENT_TASK_START,
    AGENT_TASK_STATUS,
    CANCELLED,
    FAILED,
    FINISHED,
    NOTHING_WAITING,
    RUNNING,
    UNKNOWN,
)
from slife2.libs.plugin.vocabulary import (
    AGENT_TASK_CHANNEL as CHANNEL,
)
from slife2.libs.plugin.vocabulary import (
    AGENT_TASK_MARKER as MARKER_PREFIX,
)
from slife2.libs.turn import attachments, compact, plan
from slife2.libs.turn.context import (
    context_usage,
    rebuild_report,
    turn_prompt_lead,
    with_lead,
)
from slife2.libs.turn.events import TurnEvent, TurnObserver
from slife2.libs.turn.loop import AgentLoop, AutoCall, TurnResult
from slife2.libs.turn.messages import Message
from slife2.libs.turn.prompt import (
    SUBAGENT_PROMPT,
    render_instruction,
    render_system_prompt,
)
from slife2.libs.turn.runtime import ModelRef, TurnRuntime
from slife2.libs.turn.tokens import estimate_turn_tokens
from slife2.libs.turn.tools import (
    ATTACH_IMAGE,
    CHECK_NEW_INPUT,
    CONTEXT_REBUILD,
    LIVE_CONVERSATION_TOOLS,
    TURN_COMPACT,
    CatalogueAnswer,
    Tool,
    ToolFailed,
    ToolFunc,
    ToolRegistry,
)
from slife2.libs.turn.watch import Closed, Snapshot, TaskBrief, WatchItem
from slife2.libs.turn.watch import encode as encode_watch
from slife2.libs.turn.watchhub import Tap, WatchHub

logger = logging.getLogger(__name__)

SERVER_NAME = AGENT_TASK_SERVER_NAME

#: This server's key in the config's `servers:` table, and the name of the
#: section that holds its one number.  It is the channel too: the key a plugin
#: runs under is the channel its answers come back on.
CONFIG_KEY = CHANNEL

# **A task shares the maker's identity, and the prompt carries the rest.**  The
# modes it must not use — `_async` and another task — are named in the opening
# template; the registry is the hub's list, with the four live-conversation
# bodies replaced by this turn's own (`live_bodies`).

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

#: The registry's key: `(home, agent, name)` — and the watch hub's key too, so
#: a watcher and the task it is reading cannot disagree about which task is
#: meant.  **The maker's conversation is not in the key**: a task's name is the
#: handle the agent's own conversation, its subagents and the window all address
#: it by, so one agent has one task namespace however many conversations it runs
#: (`AgentTask.key`).
TaskKey = tuple[str, str, str]


class WatchPublisher:
    """The observer a task's turn publishes through, for whoever is watching.

    A task has no caller progress stream: nobody asked for the turn and nobody
    is waiting on it, so the one reader it can have is a watcher.  `publish` is
    synchronous by the hub's contract; `on_event` is async because a turn's
    observer is.
    """

    def __init__(self, publish: Callable[[TurnEvent], None]) -> None:
        self._publish = publish

    async def on_event(self, event: TurnEvent) -> None:
        self._publish(event)


@dataclass
class AgentTask:
    """One background turn, and everything this process knows about it.

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
    #: **The maker's conversation**, as the second half of `(agent, subagent)`
    #: — empty for the agent's own, a name for one of its subagents'.  A task
    #: stands in for the conversation that asked for it, so this is the key every
    #: hop it makes carries: the context it restores and rebuilds, the plan it
    #: reads and writes, the model it runs on, and the store a `turn_read` reads.
    #: It is not part of the registry's key, which is the agent's (`TaskKey`).
    subagent: str = ""
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
    #: The sentence its last watch frame carried, kept while the task is still
    #: answerable.  A watch that arrives after the end has no events to replay;
    #: this is what lets the screen say what happened rather than open on a
    #: status with no account of it.  Set when the task stops, never served to
    #: the model (`report` is the model's shape).
    ending: str = ""
    #: The running coroutine, kept so it is not collected mid-flight and so that
    #: `agent_task_cancel` has something to cancel.
    handle: asyncio.Task[None] | None = field(default=None, repr=False)
    #: The tap `start_tap` opened for this task, kept so its `finally` can tell
    #: its own from a replacement's.  A cancelled task's `finally` runs after
    #: `agent_task_cancel` has returned, and the name may already have been
    #: started again: the new task's tap and watchers are under the same key,
    #: and closing or dropping by key alone would end the new task's watch and
    #: take its replay from later joiners (`WatchHub.drop_if`'s reason).
    tap: Tap | None = field(default=None, repr=False)

    @property
    def key(self) -> TaskKey:
        """`(home, agent, name)` — what this process's registry is keyed by.

        Keyed by the agent and not by the name, because this server is shared:
        two agents may each have a task called `research`, and neither may see
        the other's, stop it, or be capped by it.  And keyed by the home for the
        same reason one level up: two homes may each be running an agent called
        `jack`, whose tasks are as separate as their conversations.

        **A subagent's task shares its agent's namespace**, because the name is
        the handle every reader addresses it by — the agent's own conversation,
        any of its subagents, and the window — and a name that meant one task to
        one reader and another to the next would be no handle at all.  The
        conversation the task *runs on* is `identity`, which is not this key.

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
        """`(agent, subagent)` — **the conversation this task stands in for.**

        A task is one turn of the caller's own conversation: the same prompt,
        the same model, the same tools, the same context store, and the same
        key.  So every hop it makes carries that conversation id — the tool list
        the hub hands back is bound to it, the model call goes out under it, and
        `restore` and `rebuild` are asked about *its* context, which is what "it
        runs on the caller's current context" means at the only place that is
        observable.  A `turn_read` from inside a task answers from the log the
        conversation is also written to, because that is the log a task is
        working out of.

        **The second half is the caller's, and that is the difference a subagent
        makes.**  A subagent that starts a task is running its own conversation
        — its own prompt, model, context and plan — and a task that resolved
        `(agent, "")` for it would be standing in for somebody else.  The empty
        second half is `subagent`'s "this agent's own conversation" (SDD §3.2).
        """
        return (self.agent, self.subagent)

    def report(self, *, status: str = "") -> dict[str, Any]:
        """The five fields, in one shape for three readers.

        The marker on the answer, every tool's reply and a row of
        `agent_task_list` all read this, so a model never has to translate
        between two spellings of one fact — which is the same reason the marker
        keys are named after the arguments and fields the tools use
        (SDD §5.5).

        `status` is the marker's alone: what the answer *will be* if it lands,
        while the object is still `running` through the hand-over.  The finish
        time follows the status reported rather than the object's —
        `finished_at` is empty until the task stops, so a reader asking
        mid-delivery is not told a finish time beside a `running` status, two
        contradictory facts.  The stamp itself is written when the loop returns
        (`run`), and a terminal status reports it unchanged.
        """
        shown = status or self.status
        return {
            "task_id": self.task_id,
            "name": self.name,
            "status": shown,
            "started_at": self.started_at,
            "finished_at": self.finished_at if shown != RUNNING else "",
        }


@dataclass
class TaskTurn:
    """The live half of a task's one turn: the list a tool mutates, and the model
    that reads it.

    **Not on `AgentTask`.**  A task object outlives its turn — the registry keeps
    it answerable until the retired cap forgets it — and the message list is the
    one thing that must die with the coroutine that produced it (`run` lets go of
    the answer for that reason).  So the list lives here, in a holder the tool
    registry closes over, made when the turn is built and dropped when `run_loop`
    returns.
    """

    #: The list `run_turn` advances in place.  `attach_image` injects into its
    #: last user message — the rendered description, the task's single request.
    messages: list[Message]
    #: The model reference, for the "cannot read images" refusal.
    model: str
    #: Whether that model accepts images at all.  Taken once, when the turn is
    #: built: a task's backend is fixed for its one turn, so a `model_switch`
    #: mid-task moves the agent server's loops and not this one.
    accepts_images: bool
    #: The model's window, so the compaction's context figure is the one this
    #: task's model is measured against.  Also the denominator of the automatic
    #: threshold below.  Required: the config entry states it.
    context_window: int
    #: The turns `messages` is made of, and how many of them the prefix accounts
    #: for.  The restored live-context list; a private `_context_rebuild`
    #: replaces them, and the opening after `covered` is the carried tail.
    turn_ids: list[int] = field(default_factory=list)
    covered: int = 0
    #: Whether this task's one automatic `_context_rebuild` has been tried —
    #: set before the call, because a failed hub hop never reaches the body.
    rebuilt: bool = False
    #: The messages this task's own compaction has hidden from its later model
    #: calls.  The same turn-scoped projection the agent server keeps: the list
    #: `run_turn` advances is the task's whole exchange, and only the view the
    #: model is sent shrinks (`slife2.libs.turn.compact`).
    hidden: list[Message] = field(default_factory=list)
    #: The fraction at which this task's own turn compacts itself, read from the
    #: live config when the task starts — the same `agent.compact_at` the agent
    #: server's conversations use.  A task is one turn and records nothing, so a
    #: turn that overflows has no next turn to recover on: this is the bound
    #: that keeps it alive.
    compact_at: float = 0.0
    #: Whether the automatic pass has stopped asking: a selector that kept
    #: everything or failed has made a decision about the whole context.  A
    #: later successful call — the model's own — resumes the pass.
    compact_hold: bool = False
    #: The figure the last saved turn measured, read back with the list.  The
    #: task's own rebuild runs before its first call, so this is the context
    #: usage the decision is shown until one of the task's own calls completes.
    context_tokens: int = 0
    #: The loop running this task's one turn, set once it is built; the
    #: compaction reads its last measured call, the same figure the agent
    #: server's own tool uses.
    live: AgentLoop | None = None


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
    marker has to say what the answer will be if it lands.  It goes through
    `report`, so the finish time follows the status the marker claims — the
    answer says `finished` with the moment the work ended, not with an empty
    field beside it.
    """
    payload = task.report(status=status)
    return f"{MARKER_PREFIX}{json.dumps(payload, ensure_ascii=False)}]"


def closed_reason(task: AgentTask, outcome: str) -> str:
    """What a task's last watch frame says happened.

    `outcome` is what the *loop* did, which is not the same as the task's
    status: a loop that answered and a hand-over that failed is `finished` work
    and a `failed` task, and the frame has to say both rather than call the
    answer a failure.  A cancelled task never reaches the other branches —
    cancellation is not an outcome the answer can describe, and there is no
    answer.
    """
    if task.status == CANCELLED:
        return "the task was cancelled"
    if outcome == FINISHED:
        if task.status == FINISHED:
            return "the task finished; its answer was delivered to the conversation"
        return "the task finished, but its answer could not be delivered"
    return task.text.strip() or "the task failed"


def build_server(
    config: Config,
    *,
    backend: LLMBackend | None = None,
    context_client: Client | None = None,
    hub_client: Client | None = None,
    cabinet_client: Client | None = None,
    window_client: Client | None = None,
    source: Callable[[], Config] | None = None,
) -> FastMCP:
    """Build the agent-task server.

    **The same seams the agent server takes, and for the same reason**: a task is
    a turn, so exercising one means exercising a model call, a tool list, a
    restore, a rebuild and a hand-over — and every peer that would make those
    network calls can be handed in as a client over the in-memory transport
    instead.  `window_client` is the agent server's too, because the hand-over is
    the one thing a task does that a turn does not; `cabinet_client` is only the
    `[Image:…]` key route inside a task's own `attach_image`, and it is opened
    lazily so a task with no picture never reaches it.

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
    tasks: dict[TaskKey, AgentTask] = {}

    #: The ended ones, oldest first, so that the cap has an order to forget in.
    #: Held as the objects rather than as keys: a name that has been started
    #: again is a *different* task under the same key, and evicting by key alone
    #: would throw away the new one.
    retired: list[AgentTask] = []

    #: The turn in flight and every open `watch` call, keyed like the registry.
    #: **The mechanism is `slife2.libs.turn.watchhub`'s**, shared with the agent
    #: server: a task's tap carries its `TaskBrief` and the events its loop
    #: produced, and a watcher that joins mid-turn is handed them from the start.
    #: A task records nothing, so the tap's `recorded_turn` stays `None`.
    watching: WatchHub[TaskKey] = WatchHub()

    #: **The middle of a task's turn**, shared with the agent server: the model
    #: clients, the context and hub connections, the restore, the rebuild and
    #: the trim.  What is a task's own is its store (the local message list) and
    #: its policy — it records nothing and delivers its answer by hand-over.
    #: The cabinet is here for the one route a task's own `attach_image` has to
    #: ask it about: an `[Image:…]` key its restored context carries.
    runtime = TurnRuntime(
        config,
        backend=backend,
        context_client=context_client,
        hub_client=hub_client,
        cabinet_client=cabinet_client,
        source=source,
    )

    def current() -> Config:
        """The config a task is built from, read when it runs.

        Re-read rather than captured at start-up, so a `model_switch` or an
        edited prompt reaches the next task — the same rule the agent server
        follows, and here it is the whole of what makes a task's model current.
        """
        return runtime.current()

    async def registry(task: AgentTask, turn: TaskTurn) -> ToolRegistry:
        """The tools a task runs with: the hub's list, bound to the maker.

        Bound to the maker's id, so a tool that reads this conversation's store
        reads the one the task is standing in for — which is the same fact as
        the key being the maker's, seen from the tool side.

        **The hub's whole list, by name, with two bodies replaced.**  A task's
        opening template is the one place that says what *choices* not to make —
        `_async` and another task — and the registry is not a second list of the
        same rules.  But `attach_image` and `_check_new_input` are not choices:
        they act on a live turn's own message list, and the list the hub's
        versions would reach is this conversation's *other* turn, in another
        process.  So their declarations stay the hub's and their bodies are this
        turn's (`live_tools`), and a by-name call is answered with the same body
        rather than fetched from the hub (`catalogue_acquire`).
        """

        tools = await remote_tools(await runtime.hub(), task.identity)
        local = live_tools(task, turn, tools)

        async def catalogue_has(name: str) -> CatalogueAnswer:
            """What the catalogue says about a name outside this request's list."""
            return (await tool_lookup(await runtime.hub(), name, task.identity)).answer

        async def catalogue_acquire(
            name: str, arguments: dict[str, Any]
        ) -> Tool | None:
            """The same name, made runnable when the call fits its current schema."""
            if name in LIVE_CONVERSATION_TOOLS:
                # **A by-name call is this turn's, not the hub's.**  The
                # registry would otherwise fetch the agent server's version,
                # which acts on the conversation's other live turn — the exact
                # action the local body exists to keep off it.  The hub is still
                # asked for the declaration and the fit (and does the load), so
                # the model's next request carries the same tool.
                found = await acquire_tool(
                    await runtime.hub(), name, arguments, task.identity
                )
                if found is None:
                    return None
                return Tool(spec=found.spec, run=live_bodies(task, turn)[name])
            return await acquire_tool(
                await runtime.hub(), name, arguments, task.identity
            )

        return ToolRegistry(
            [local.get(tool.spec.name, tool) for tool in tools],
            exists=catalogue_has,
            acquire=catalogue_acquire,
        )

    def live_tools(
        task: AgentTask, turn: TaskTurn, tools: list[Tool]
    ) -> dict[str, Tool]:
        """The live-conversation tools as this turn's, paired with the hub's
        declarations.

        `registry` passes the advertised list through, so the model reads the
        agent server's own description and schema; only the body is the task's.
        """
        declared = {tool.spec.name: tool.spec for tool in tools}
        bodies = live_bodies(task, turn)
        return {
            name: Tool(spec=declared[name], run=body)
            for name, body in bodies.items()
            if name in declared
        }

    def live_bodies(task: AgentTask, turn: TaskTurn) -> dict[str, ToolFunc]:
        """The bodies behind `LIVE_CONVERSATION_TOOLS`, for **this task's own
        turn**.

        **The four tools are the task's because the list is.**  A task is a single
        user message and a single turn; `_check_new_input` has no inbox to hand
        anything over from and no harness cut-in to serve, `attach_image`
        injects into the task's own message list so the picture is on the task's
        *next* model call, and `_context_rebuild` edits the task's clone with
        `publish=False`.  The agent server's versions resolve a loop by the
        conversation id alone, and a task's id is the maker's — the other live
        turn on that conversation — so leaving them in the list is the aliasing
        this body exists to close.
        """

        async def nothing_waiting(_arguments: dict[str, Any]) -> str:
            """There is never anything waiting here, and saying so is the whole
            answer: a task has no queue, and the maker's is not the task's."""
            return NOTHING_WAITING

        async def attach_image(arguments: dict[str, Any]) -> dict[str, Any]:
            """Attach images to the task's own single user message."""
            given = arguments.get("sources")
            wanted = (
                [str(source) for source in given if str(source).strip()]
                if isinstance(given, list)
                else []
            )
            if not wanted:
                raise ToolFailed(attachments.NO_SOURCE)
            if not turn.accepts_images:
                raise ToolFailed(attachments.cannot_read_images(turn.model))

            async def lookup(
                keys: list[tuple[str, str]],
            ) -> tuple[list[tuple[str, str]], list[str]]:
                try:
                    client = await runtime.cabinet()
                except Exception as exc:  # noqa: BLE001 - an unreachable cabinet is each key's refusal
                    return [], [
                        attachments.cabinet_unreachable(refusal, exc)
                        for _key, refusal in keys
                    ]

                async def fetch(key: str) -> str | None:
                    answer = await client.call_tool(
                        "image_source",
                        {"key": key},
                        # **The task's own home**, kept on the object: this runs
                        # after the call that started it has returned, and a
                        # home read from whatever directory the daemon happens
                        # to be in would ask the wrong cabinet (`AgentTask.home`).
                        meta=conversation_meta(*task.identity, home=task.home),
                        timeout=_T.timeouts.work.hub,
                    )
                    payload = tool_payload(answer)
                    if "url" not in payload:
                        raise unreadable(
                            "the cabinet", "return an image source", payload
                        )
                    url = payload.get("url")
                    return url if isinstance(url, str) and url else None

                return await attachments.from_keys(keys, fetch)

            # A model's path is relative to its data directory, exactly as the
            # agent server resolves it; a key is a key to the maker's cabinet.
            # The base is the task's kept home rather than the ambient one, for
            # `fetch`'s reason.
            pairs, refusals = await attachments.resolve_sources(
                wanted, base=Path(task.home), lookup=lookup
            )
            if pairs and not attachments.inject(
                turn.messages, [url for _source, url in pairs]
            ):
                raise ToolFailed(attachments.NO_MESSAGE)
            logger.info(
                "attach_image in agent task %s: %d attached, %d refused",
                task.name,
                len(pairs),
                len(refusals),
            )
            return {
                "attached": [source for source, _url in pairs],
                "refused": refusals,
            }

        async def turn_compact(_arguments: dict[str, Any]) -> str:
            """Drop what this task no longer needs from its own model context.

            **The task's own list and the task's own state.**  The agent
            server's version resolves a loop by the conversation id, which is
            the maker's — the other live turn on that conversation — so this
            body exists for the reason `attach_image`'s does: the id alone
            cannot tell the two loops apart.  A task records nothing, so a
            compaction here is a projection of `turn.messages` and touches no
            record at all.
            """
            result = await runtime.compact_turn(
                task.identity,
                turn.messages,
                turn.hidden,
                turn.model,
                context_tokens=(
                    turn.live.last_usage.total_tokens if turn.live is not None else 0
                ),
                context_window=turn.context_window,
            )
            # **The same rule as the agent server's**: a decision to keep
            # everything, or a failed selector, stops the automatic pass; a
            # drop leaves it live for the next crossing.
            turn.compact_hold = result.dropped == 0 or bool(result.decision)
            logger.info(
                "agent task %s: turn_compact seen=%d dropped=%d decision=%s hold=%s",
                task.name,
                result.seen,
                result.dropped,
                result.decision or "ok",
                turn.compact_hold,
            )
            return compact.report(result, recorded=False)

        async def context_rebuild(_arguments: dict[str, Any]) -> str:
            """Rebuild this task's own clone of the conversation's history.

            **The task's clone, and `publish=False`.**  The task records
            nothing, so its context decision must not move the conversation it
            stands in for; the restored list is the snapshot and the rebuild
            replaces its historical prefix only.  The turn running is carried
            verbatim, exactly as on the agent server, and the decision is made
            from the live context — the task's own opening is in it.
            """
            at = compact.opening(turn.messages)
            if at is None:
                return "This task has no opening message; nothing to rebuild."
            rebuilt = await runtime.rebuild(
                task.identity,
                turn.messages,
                turn.turn_ids,
                turn.covered,
                turn.model,
                publish=False,
                # The last LLM call: this task's own completed call when one
                # returned, otherwise the restored turn's last call.
                context_tokens=(
                    turn.live.last_usage.total_tokens
                    if turn.live is not None and turn.live.last_usage.total_tokens
                    else turn.context_tokens
                ),
                context_window=turn.context_window,
            )
            turn.messages[:] = rebuilt.messages
            turn.turn_ids = rebuilt.turn_ids
            turn.covered = rebuilt.covered
            turn.hidden.clear()
            turn.compact_hold = False
            logger.info(
                "agent task %s: context_rebuild changed=%s kept=%d in_hand=%d "
                "outside=%d recalled=%d rerank=%s decision=%s",
                task.name,
                rebuilt.changed,
                rebuilt.kept,
                rebuilt.in_hand,
                rebuilt.outside,
                rebuilt.recalled,
                rebuilt.rerank or "-",
                rebuilt.decision or "-",
            )
            tokens = context_usage(
                estimate_turn_tokens(
                    [message.to_record() for message in turn.messages]
                ),
                turn.context_window,
            )[0]
            return rebuild_report(
                changed=rebuilt.changed,
                kept=rebuilt.kept,
                in_hand=rebuilt.in_hand,
                outside=rebuilt.outside,
                recalled=rebuilt.recalled,
                rerank=rebuilt.rerank,
                decision=rebuilt.decision,
                tokens=tokens,
            )

        return {
            CHECK_NEW_INPUT: nothing_waiting,
            ATTACH_IMAGE: attach_image,
            TURN_COMPACT: turn_compact,
            CONTEXT_REBUILD: context_rebuild,
        }

    # ── the loop ─────────────────────────────────────────────────────────

    async def run_loop(task: AgentTask, observer: TurnObserver) -> TurnResult:
        """One task's turn: the template as the user message, then the maker's
        own order around it — restore, rebuild, and the loop.

        **The steps are the turn runtime's** (`slife2.libs.turn.runtime`),
        shared with the agent server: the head is the maker's system prompt,
        `restore` reads the context this conversation had, `rebuild` decides
        what the turn runs on **privately** — `publish=False`, so the
        conversation's live-context list is not moved — and the loop advances
        it.  What is a task's own is the store — a local list — the absence of
        an inbox, and the absence of a record, which is argued below.

        **The head and the model are the maker's, not the agent's.**  A task
        stands in for the conversation that asked for it, so an agent's task
        runs on the operator's `agent.system_prompt` and the default model, and
        a subagent's task runs on `subagent.j2` and the model its own record
        names.  The two are the same choice the agent server makes when it opens
        that conversation, and they are read from `subagent_spec` — the library
        both servers share — so a prompt or a model changed since the maker's
        last turn reaches the next task.

        **The observer is the watcher's half**, and it is the same seam the
        agent server uses: the loop reports every event through it, and the
        task's `_context_rebuild` pair is in the turn's stream like every other
        tool's.  A task has no caller progress stream; the observer is the only
        reader its turn can have.

        `opening` travels **beside** the list rather than inside it, and
        `run_turn` appends it: the first boundary's `_context_rebuild` then
        replaces the historical prefix and carries the task's opening verbatim.

        **The one step that is missing is the record, and it is missing on
        purpose.**  A task's result reaches the conversation as a message, and
        *that* message is what the agent server records — so storing the task's
        own round trip as well would put the same answer in the transcript
        twice, under a description that is a rendered template rather than anything the
        person said.  A task reads a clone of the maker's context and adds
        nothing to it: what the conversation gains is the answer, which is the
        thing it asked for.  There is no switch here because there is nothing
        to switch.
        """
        current = runtime.current()
        # **A subagent that is gone has no conversation to stand in for.**  The
        # record's *existence* is what makes a directory a subagent's, so a name
        # with none is one that was never made or was removed — and a task that
        # ran on it anyway would restore a conversation whose store recreates
        # the directory without a record, blocking the name for both subagent
        # tools.  The agent server refuses the same name the same way
        # (`conversation`); a task fails with a message instead of a turn, which
        # is SDD §5.1's rule one level down.
        if task.subagent and not subagent_file(task.agent, task.subagent).is_file():
            raise RuntimeError(
                f"there is no subagent {task.subagent!r} of {task.agent}'s: it "
                f"was never made, or it was removed. The task has no "
                f"conversation to run on."
            )
        # **The maker's own record**: the defaults for the agent's own
        # conversation, and the role and model a subagent was made with.  A task
        # of a subagent without this would run the agent's prompt on the agent's
        # model under the subagent's key — a stand-in for somebody else.  A
        # record that cannot be read is that same mistake, so this read is
        # strict and the task fails naming the record.
        spec = subagent_spec(task.agent, task.subagent, strict=True)
        name, provider, settings = current.resolve(spec.model)
        model = ModelRef(f"{name}/{settings.model}", name, provider.api, settings.model)
        if task.subagent:
            system = render_system_prompt(
                SUBAGENT_PROMPT,
                agent_name=task.subagent,
                agent=task.agent,
                role=spec.role,
            )
        else:
            system = render_system_prompt(
                current.agent.system_prompt, agent_name=task.agent
            )
        head = [Message(role="system", content=system)]
        opening = render_instruction(
            OPENING_TEMPLATE, name=task.name, description=task.description
        )

        # **A clone, not a share.**  `restore` reads the conversation's
        # live-context list once; the task's own `_context_rebuild` decides on
        # that snapshot with `publish=False`, so the task's turn runs on a
        # context of its own and the conversation's list is not moved.  A task
        # records nothing, and a turn that leaves no record must not change what
        # the conversation comes back to.  No lock is needed: a read and a
        # private decision have no write to interleave.
        restored = await runtime.restore(task.identity, head)

        # **The live half of the turn**, made before the loop because the list a
        # tool mutates is the one the rebuild decides on.  The registry closes
        # over it, so `attach_image` injects into the same list `run_turn`
        # advances and `_context_rebuild` replaces the same prefix.
        turn = TaskTurn(
            messages=restored.messages,
            model=model.reference,
            accepts_images=settings.accepts_images,
            context_window=settings.context_window,
            compact_at=current.agent.compact_at,
            turn_ids=restored.turn_ids,
            covered=restored.covered,
            context_tokens=restored.context_tokens,
        )

        loop = await runtime.make_loop(
            task.identity,
            model,
            partial(registry, task, turn),
            # Asked again before every model call, as the agent server asks it:
            # a tool loaded mid-task is in the next call's list rather than
            # nowhere.
            refresh=partial(registry, task, turn),
            max_iterations=current.agent.max_iterations,
            # **A task has no cabinet writer and records no messages**, so its
            # tool results are cut to the same byte bound rather than dumped:
            # the bound is the guarantee, the dump is the agent server's
            # recovery convenience (`slife2.libs.harness.dumps.cut`).
            compress=dumps.cut_for(current.tool_result.bytes),
            # **No `deliver`, so a background result has nowhere to be handed
            # back.**  The prompt carries the rule instead of the loop: it tells
            # the model not to use `_async` (SDD §14.2, "Background work inside
            # an agent task") because a result arriving after this turn would
            # have nowhere to go.  The mode is not refused; a call the model
            # backgrounds anyway keeps its result in the async registry, and
            # `check_async` is in the list like every other model tool.
            # `agent_task_start` is in the list too — the prompt is what tells
            # the model not to start another task.
            # **The task's own view.**  A `_turn_compact` call hides messages
            # from `turn.hidden`; the list `run_turn` advances is the task's
            # whole exchange, and the answer the task hands back is its own.
            present=lambda messages: compact.view(messages, turn.hidden),
        )
        # **The loop, for the one tool that reads its bill.**  The registry was
        # built above and closes over `turn`, so the body sees this the moment a
        # `_turn_compact` call arrives.
        turn.live = loop

        def compact_due() -> bool:
            """Whether this task's one turn should compact itself now.

            **A task is one turn and records nothing**, so a turn that overflows
            has no next turn to recover on: this is the bound that keeps it
            alive.  The rules are the agent server's (`AgentCore._compact_due`):
            the last call's measured total against `agent.compact_at` of the
            model's window; a keep-all or a failed selector stops the pass; a
            drop leaves it live for the next crossing.
            """
            if turn.live is None or turn.compact_at <= 0:
                return False
            if turn.live.last_usage.total_tokens < (
                turn.context_window * turn.compact_at
            ):
                return False
            if turn.compact_hold:
                return False
            at = compact.opening(turn.messages)
            if at is None:
                return False
            view = compact.view(turn.messages, turn.hidden)
            return bool(compact.selectable(view, turn.messages[at]))

        def auto_invoke() -> AutoCall | None:
            """The harness's own move at an iteration boundary.

            **The rebuild first, once** — the task's clone gets the same
            historical selection the agent server's turn gets, with
            `publish=False` and no record — then the compaction when due.
            """
            if not turn.rebuilt and turn.live is not None:
                # **Set before the call, not by its body** — a failed hub hop
                # never reaches the body (`slife2.plugins.agent.core` says why
                # for the agent server's turn).  A hub that predates the tool
                # is the same fall-through: the task runs on what it has.
                turn.rebuilt = True
                if turn.live.declares(CONTEXT_REBUILD):
                    return AutoCall(CONTEXT_REBUILD)
            return AutoCall(TURN_COMPACT) if compact_due() else None

        # **The task's prompt marker, and no strip after it.**  A task's messages
        # are not recorded — the answer is what the conversation gains — so
        # there is no row to keep the marker out of; it is prompt text for this
        # one turn and goes out of scope with the task.  The context figure is
        # not here: `_context_rebuild`'s report carries the turn's own estimate.
        opening = with_lead(
            opening,
            turn_prompt_lead(
                # A task runs on the maker's own conversation, so it runs on the
                # same plan: the halfway case — a task that did not see the plan
                # it could write — would be two answers to one conversation.
                plan.read(*task.identity),
            ),
        )
        result = await loop.run_turn(turn.messages, opening, observer, auto=auto_invoke)
        # After the turn and not during it: the tool list goes out with every
        # model call, so dropping the excess mid-turn would take away a tool the
        # model had just loaded and was about to use.
        await trim_tools(task, turn.messages, observer)
        return result

    async def trim_tools(
        task: AgentTask, messages: list[Message], observer: TurnObserver
    ) -> None:
        """Bring the model's tool list back within its budget — and say so in the
        turn, rather than doing it behind the model's back.

        **The write is the turn runtime's** (`slife2.libs.turn.runtime.trim`):
        the pair naming what left, with no await between its halves, is one
        mechanism for both servers, and so is the note it reports on this
        task's stream.  What is a task's own is how a skip is reported.

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
            unloaded = await runtime.trim(messages, observer)
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
    #: anything the person said.  A task reads the maker's context and adds
    #: nothing to it; what the conversation gains is the answer, which is the
    #: thing it asked for.  There is no switch here because there is nothing to
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
        outcome = FAILED
        try:
            outcome = FINISHED
            # **One token for one task turn.**  A task runs a turn of the
            # maker's conversation from a process beside the agent server's, so
            # the conversation alone cannot tell this turn's lines — or its
            # live-list tools — from that server's; the token is what does.  It
            # covers the turn's failure line as well as its hops.  The delivery
            # below is outside it on purpose — the agent server mints the token
            # of the turn that delivery starts.
            with logcontext.turning(logcontext.new_turn()):
                try:
                    result = await run_loop(
                        task,
                        WatchPublisher(partial(watching.publish, task.key)),
                    )
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
            # told a completion that may still fail — and `report` withholds the
            # finish time for the same reason: a `running` status beside a
            # finish time is two contradictory facts.  The marker carries the
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
            # **A watcher is told how it ended, and the tap goes with the task.**
            # The sentence is kept on the object first: a watch that arrives
            # after this frame has no events to replay, and the status alone
            # cannot say whether a successful loop failed to deliver.  It is
            # sent before `retire` and before the answer is let go — a failed
            # loop's sentence is what the watcher has to show for the failure,
            # and `task.text` is cleared below.
            task.ending = closed_reason(task, outcome)
            # **Close and drop only while the tap is still this task's.**  A
            # cancelled task's `finally` runs after the cancel call returned,
            # and the name may already have been started again: the new task's
            # tap and watchers are under the same key, and ending them by key
            # alone would tell the new task's watchers the old task's ending
            # and take the new tap's replay from later joiners
            # (`WatchHub.drop_if`'s reason).  The check and the two calls are
            # one synchronous block, so no start can slip between them; a
            # normal ending reaches here with its own tap still in place.
            if watching.tap(task.key) is task.tap:
                watching.close(task.key, task.ending)
                watching.drop(task.key)
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
        answer went nowhere.  `status` is what the marker says the answer *is* —
        `finished` for a loop that succeeded, `failed` for one that did not —
        because the object is still `running` while a successful answer is being
        handed over.

        **The window is the agent's, even for a subagent's task.**  A subagent
        has no window of its own (SDD §4.5), so the only conversation that can
        take its task's answer is its maker's — the agent whose window is
        showing, and the one that can act on it.  The channel and the marker
        name the task the answer came from, and the name is the handle back to
        it.

        Returns whether the answer reached a window.
        """
        maker = logcontext.describe(task.agent, task.subagent)
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
                "agent task %s for %s was not delivered: %s", task.name, maker, exc
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
                maker,
                exc,
            )
            return False
        return True

    # ── whose task ───────────────────────────────────────────────────────

    #: The refusal this plugin says for a call that did not name a
    #: conversation — its own two words in the one shared sentence.  The status,
    #: the list and the cancel address a task by the agent's handle alone; the
    #: start needs the whole pair, because the task runs on that conversation.
    agent_of = agent_reader("agent whose task it could be", "a task")
    conversation_of = conversation_reader(
        "conversation whose task it could be", "a task"
    )

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
        """The task under this home's `(agent, name)` handle, or None.

        **A literal triple, not `conversation_key`.**  The two were the same
        shape while a task's identity was always the agent's own, and the
        overload read as if the name were a subagent's.  It is not: the key's
        third slot is the task's name (`TaskKey`), and a task's maker is
        `AgentTask.subagent`, which is not part of what this registry is keyed
        by.
        """
        found = tasks.get((home_key(), agent, name))
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
        """Start an agent task: one background turn that runs as you.

        For work that would hold you — long, or waiting on the home. The call
        returns at once and the answer reaches this conversation on its own: do
        not wait for it, and do not poll. **If no window is showing this
        conversation when it stops, the task ends `failed`** — the answer is the
        product and it is not kept, so there is nothing to fetch later.

        A task is one turn: inside it, do not use `_async` or start another
        task, because there is no later turn to receive their result.

        `description` is the task itself. It starts from this conversation's
        stored turns, so **the turn you are answering is not among them** — put
        in what the task needs from the last exchange, and let it read everything
        older for itself. It runs with your prompt, your model and your tools,
        and nobody can answer a question it raises: what it sends when it stops
        is the whole of what comes back.

        A subagent's task runs as that subagent and its answer is delivered to
        the agent's conversation, because a subagent has no window of its own.

        `name` is the handle — how the task is asked about and stopped. This is
        not a way to run a program in the background.

        Args:
            name: What to call the task — how it is asked about and stopped.
            description: The task itself: what to do, and what "done" looks
                like. It is the whole of what the task is given.
        """
        # **The whole pair, because the task runs on this conversation.**  A
        # subagent that starts one gets a task of its own — its prompt, its
        # model, its context — not a task of the agent it belongs to.
        agent, subagent = conversation_of(ctx)
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
            # The maker's conversation — the other half of the identity every
            # hop the task makes carries, and the one its prompt and model are
            # read from.
            subagent=subagent,
            # The home this call came in on, read *here*, at the only moment it
            # is the caller's (`slife2.libs.plugin.audience.conversation_key` explains why
            # the object keeps it rather than asking again later).
            home=home_key(),
        )
        tasks[task.key] = task
        # **The tap opens with the task, before the loop has produced an
        # event.**  A watcher may join during the restore or the rebuild, and
        # the brief is the one frame it could not otherwise see; a tap started
        # inside `run` would leave that window with nothing to replay.  There is
        # no await between the registry insert and this line, so a watch call
        # cannot find the task without the tap.
        task.tap = watching.start_tap(
            task.key,
            TaskBrief(
                name=task.name,
                description=task.description,
                status=task.status,
            ),
        )
        # Detached from the call that starts it, which is the whole of what
        # "background" means here: the request that asked returns, and the loop
        # keeps going.  SDD §5.6's rule for a write from a cancelled turn is the same
        # rule — work created outside the cancelled scope does land.
        task.handle = asyncio.create_task(run(task))
        logger.info(
            "agent task %s started for %s",
            name,
            logcontext.describe(agent, subagent),
        )
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

    @mcp.tool
    async def watch(name: str, ctx: Context) -> dict[str, Any]:
        """Watch a running agent task's turn — read-only, until it stops.

        **Hold this call open for as long as the view is up.**  The first
        progress notification is a `Snapshot` of the task's opening read — an
        empty turn list, because a task records nothing, and the agent's plan,
        because that is what the task runs on.  `TaskBrief` follows with the
        description the task was given, then the turn events from the task's
        start (a watcher that joins mid-turn is handed the events already
        produced), then live events.  The last frame is `Closed`, and the call
        returns after it.  A cancelled call is the detach.

        **A task that has already stopped is answered, not refused.**  It stays
        in the registry until the retired cap forgets it, so a watch that
        arrives a moment late gets the brief with the final status and the
        sentence its ending carried — the screen says what happened rather than
        failing to open on a task that ran.  A name this process does not hold
        is a refusal.

        **Watching is a read.**  It starts no turn, spends no model call, and
        does not make the task finished: the answer still reaches the
        conversation by hand-over, and a task whose answer reaches no window is
        `failed` whether or not somebody watched.

        Args:
            name: The task's name, as `agent_task_start` was given it.

        Returns:
            `closed`: true, after `Closed` has been sent.  A cancellation
                returns nothing — it is a detach.
        """
        agent = agent_of(ctx)
        task = find(agent, name.strip())
        if task is None:
            raise ToolError(
                f"no agent task named {name.strip()!r} is held here; it may have "
                f"finished and been forgotten. Its answer, if it had one, is in "
                f"the conversation."
            )

        count = 0

        async def send(item: WatchItem) -> None:
            nonlocal count
            count += 1
            # **Reported from this handler's own task**, which is what makes the
            # progress token this call's (the hub's `Watcher` says why).
            await ctx.report_progress(count, None, encode_watch(item))

        plan_items = plan.read(*task.identity)
        # **The status read and the registration are one synchronous step.**
        # `run` sets a final status and closes the watchers without an await
        # between, so a task seen running here cannot stop under this
        # registration; a task seen stopped here has no future frames to wait
        # for and is answered below without a watcher.
        if task.status == RUNNING:
            watcher = watching.watch(task.key)
            replay = watching.replay(task.key)
        else:
            watcher = None
            replay = None

        try:
            await send(Snapshot([], plan_items))
            if watcher is None:
                await send(
                    TaskBrief(
                        name=task.name,
                        description=task.description,
                        status=task.status,
                    )
                )
                await send(Closed(task.ending or "the task is no longer running"))
                return {"closed": True}
            if replay is None:
                # The task is running but has published nothing yet, or its
                # build kept no tap; the brief is still the frame that says what
                # is being watched, and the live queue follows.
                await send(
                    TaskBrief(
                        name=task.name,
                        description=task.description,
                        status=task.status,
                    )
                )
            else:
                # The brief and every event the turn has produced, from its
                # start.  A task's tap has no store id, so there is no snapshot
                # that could already contain this turn and nothing to skip.
                await send(replay.started)
                for event in replay.events:
                    await send(event)
            while True:
                item = await watcher.queue.get()
                await send(item)
                if isinstance(item, Closed):
                    # The task stopped; the last frame has been written and
                    # there is nothing after it to wait for.
                    return {"closed": True}
        finally:
            if watcher is not None:
                watching.unwatch(task.key, watcher)

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
