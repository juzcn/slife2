"""slife2-agent-task — the agent task: one loop of the agent's own, run on a
description, answered by a message.

An **agent task** is a background loop.  The model starts one with a name and a
description; the loop runs as the calling agent — the same system prompt, the
same model, the same tools — and its last message is handed back to that
conversation when it stops.  Nothing waits for it: the call that starts one
answers at once, and the answer arrives later as a message on the `agent-task`
channel, wearing the marker that says which task it was (DESIGN.md §1.3).

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
conversation.  §5.1's *the context is chosen, not accumulated* has no exception
for a task: one that ran on whatever happened to be in the list would be the one
turn in this system whose context nobody decided.  `restore` and `rebuild` are
`slife2-context`'s and the trim is `slife2-toolhub`'s; what is repeated here is
the *order* they are called in, which is the price of this plugin and the thing
DESIGN.md §1.3 names rather than denies.

**Nothing is recorded, and that is a decision about tasks.**  A task's result
reaches the conversation as a message and *that* message is what the agent server
records, so writing the task's own round trip as well would file the same answer
twice under a description that is a rendered template rather than anything the person
said.  A task reads the agent's context and adds nothing to it.

Two things it deliberately does not have, each because nothing needs it rather
than because it was left out:

* **No cancellation repair.**  A cancelled turn in the agent server is truncated
  back to the user's message, because an assistant message with half its tool
  calls answered is a 400 from every provider and *that* conversation has to
  survive.  A task's list is local and dies with it, so there is no next request
  to poison.
* **No filtering of the tool set.**  The catalogue is one list for the whole
  system (`slife2-toolhub`), so a task may start a task and the registry here is
  **flat** — no parents, no depth.  `agent-task: max_running` is the bound
  instead; DESIGN.md §9 holds the one it does not bound.

The answer goes back through `slife2-tui`'s `submit`, which is this system's one
path for "something outside the conversation became a turn in it" (DESIGN.md
§1.2).  A window that is not showing that agent is a **failed delivery**: the
work happened, the answer reached no conversation, and the log says so — a task
is over either way, and running a turn for nobody to make the two agree is the
"carry on without the window" this system dropped.
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

import slife2.helpers.timeouts as _T
from slife2.clients.model import MCPBackend, open_backend
from slife2.clients.toolclient import (
    FUNC_TOOL_UNLOAD,
    remote_tools,
    trim_answer,
    unload_tools,
)
from slife2.helpers.clock import now
from slife2.libs.harness.handover import DeliveryFailed, hand_over
from slife2.libs.host.config import (
    AGENT_TASK_SERVER_NAME,
    API_SERVER_NAMES,
    CONTEXT_SERVER_NAME,
    TOOLHUB_SERVER_NAME,
    Config,
    load_cached,
)
from slife2.libs.host.paths import home as home_dir
from slife2.libs.model.base import LLMBackend
from slife2.libs.plugin.audience import FOR_THE_MODEL, client_key
from slife2.libs.plugin.protocol import (
    ClientId,
    agent_reader,
    close_server,
    house_server,
    open_server,
    serve_plugin,
    tool_payload,
    unreadable,
)
from slife2.libs.turn.loop import AgentLoop, TurnResult, harness_call
from slife2.libs.turn.messages import Message
from slife2.libs.turn.prompt import render
from slife2.libs.turn.tools import ToolRegistry

logger = logging.getLogger(__name__)

SERVER_NAME = AGENT_TASK_SERVER_NAME

#: This server's key in the config's `servers:` table, and the name of the
#: section that holds its one number.
CONFIG_KEY = "agent-task"

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
CHANNEL = "agent-task"

#: What the answer wears, so the model can read the task's id back out of the
#: row.  It is **content**, not an annotation this build derives: it goes into
#: the stored message verbatim, which by `DESIGN.md` §1.2's convention is what
#: the word-shaped spelling says and `[TURN:…]`'s capitals do not.
#:
#: **`AgentTaskResult`, because that is what the line is**: an agent task's
#: result, read by the model beside `slife2.libs.harness.async_tasks`'s
#: `[AgentToolResult:{…}]` — two results, one from a task and one from a tool
#: call, named so that neither can be taken for the other.  The *bubble* a window
#: draws is the shorter `AsyncTask(name)> ` (`slife2.tui.widgets.channel_prefix`):
#: a person is being told which task answered.
MARKER_PREFIX = "[AgentTaskResult:"

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

#: What a call that does not say which agent it is for is told.  A shared
#: process cannot guess it: one `slife2-agent-task` serves every agent, and
#: whose loop this is, is a fact only the caller has.


@dataclass
class AgentTask:
    """One background loop, and everything this process knows about it.

    The five fields `report` answers with are a task's *whole* description:
    there is no message list here, no transcript and no result beyond the one
    being delivered, because none of those outlive the coroutine that produced
    them (DESIGN.md §1.3).
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
    #: conversation twice (DESIGN.md §1.3).
    text: str = ""
    #: Whether the answer reached the conversation.  Kept because "was it handed
    #: over" is the one fact about a finished task that nothing else can
    #: reconstruct — the delivery log is the process's, and the conversation
    #: cannot be asked whether something arrived.
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
        §3 gives for `(agent, subagent)`: a name cannot go stale, and a handle a
        caller has to keep is a thing that can be lost.  It also says where the
        answer will come from.

        `channel_for` is the spelling, and this is the name the *key* goes by in
        a report: one string with two readers, rather than two `f"{CHANNEL}:…"`
        that a later change to the format would have to find.
        """
        return channel_for(self)

    @property
    def identity(self) -> ClientId:
        """`(agent, "")` — **the agent's own id, which is the whole point.**

        A task is the main agent's task, not a conversation beside it: the same
        prompt, the same model, the same tools, the same context store, and the
        same key.  So every hop it makes carries the agent's own id — the tool
        list the hub hands back is bound to it, the model call goes out under it,
        and `restore` and `rebuild` are asked about *this agent's* context, which
        is what "it runs on the current agent's context" means at the only place
        that is observable.  A `turn_read` from inside a task answers from the
        log the conversation is also written to, because that is the log a task
        is working out of.

        The empty second half is `subagent`'s "this agent's own conversation",
        which is exactly what a task is running on (DESIGN.md §3).
        """
        return (self.agent, "")

    def report(self) -> dict[str, Any]:
        """The five fields, in one shape for three readers.

        The marker on the answer, every tool's reply and a row of
        `agent_task_list` all read this, so a model never has to translate
        between two spellings of one fact — which is the same reason the marker
        keys are named after the arguments and fields the tools use
        (DESIGN.md §1.2).
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
    footnote hands the model (§5.1) — so a result says which task it came from
    outside the marker as well as inside it.
    """
    return f"{CHANNEL}:{task.name}"


def marker(task: AgentTask) -> str:
    """What a task's answer wears, so the model knows which task it came from.

    The space between a marker and the words beside it is the joiner's to add,
    exactly as `slife2-wechat` adds its own (DESIGN.md §1.2).
    """
    return f"{MARKER_PREFIX}{json.dumps(task.report(), ensure_ascii=False)}]"


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

    `source` is the config to read when a task actually runs, so a test can hand
    in a config that no file backs; without it the file is re-read, which is what
    makes a `model_switch` reach the next task.
    """

    #: Every task this process has run, live and recently ended, by
    #: `(agent, name)`.  **Not cleared on the way out**, for the reason the
    #: agent server's loop registry is not: the lifespan is not the process's
    #: lifetime over the in-memory transport, and clearing here would forget
    #: tasks every time a caller disconnected.
    tasks: dict[tuple[str, str, str], AgentTask] = {}

    #: The ended ones, oldest first, so that the cap has an order to forget in.
    #: Held as the objects rather than as keys: a name that has been started
    #: again is a *different* task under the same key, and evicting by key alone
    #: would throw away the new one.
    retired: list[AgentTask] = []

    #: Model clients, one per server, and the backends built over them — the
    #: same arrangement as the agent server's, and for the same reason: a
    #: handshake per task would be a handshake per task.
    clients: dict[str, Client] = {}
    backends: dict[str, MCPBackend] = {}

    #: The hub, opened on first use.  Kept for the process: it is asked for a
    #: tool list before every model call.
    hub_conn: Client | None = None

    #: The context plugin, opened on first use.  It is what holds a
    #: conversation's turn log and decides which of its turns a turn runs on,
    #: and a task asks it the same two questions a turn of the agent's does.
    context_conn: Client | None = None

    #: One opener at a time for lazily-built connections, so that two tasks
    #: starting together do not build two clients for one server.
    opening = asyncio.Lock()

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

    #: Whether this build opened the two long-lived clients, and so whether it
    #: is the one that closes them.  An injected client belongs to whoever
    #: handed it in.
    context_owned = context_client is None
    hub_owned = hub_client is None

    def live() -> Config:
        """The config a task is built from, read when it runs.

        Re-read rather than captured at start-up, so a `model_switch` or an
        edited prompt reaches the next task — the same rule the agent server
        follows, and here it is the whole of what makes a task's model current.
        """
        return source() if source is not None else load_cached()

    # ── the peers ────────────────────────────────────────────────────────

    async def hub() -> Client:
        """The toolhub, opened on first use.

        A hub that is not there **raises**, like every other peer in this
        system: a task with a quietly short tool list would be a loop that fails
        in ways the model cannot see (DESIGN.md §8).
        """
        nonlocal hub_conn
        if hub_client is not None:
            return hub_client
        if hub_conn is not None:
            return hub_conn
        async with opening:
            if hub_conn is None:
                hub_conn = await open_server(
                    config.server("toolhub").url,
                    name=TOOLHUB_SERVER_NAME,
                    fallback_tool="list_tools",
                    # No clock on the connection: a request deadline there would
                    # be one on every request that crosses it, tool calls
                    # included.  The asks this process makes state their own
                    # patience where they are made (`slife2.clients.toolclient`).
                )
        return hub_conn

    async def memory() -> Client:
        """The context plugin, opened on first use.

        A context plugin that is **not there raises**, like every other peer in
        this system, and it is the peer whose absence is felt soonest: a task
        whose context cannot be restored is a task that should not run.
        """
        nonlocal context_conn
        if context_client is not None:
            return context_client
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
        return context_conn

    async def registry(task: AgentTask) -> ToolRegistry:
        """The tools a task runs with: the hub's list, bound to the agent.

        Bound to the agent's own id, so a tool that reads this agent's store
        reads the agent's own — which is the same fact as the key being the
        agent's, seen from the tool side.
        """
        return ToolRegistry(await remote_tools(await hub(), task.identity))

    async def model_for(task: AgentTask) -> LLMBackend:
        """A model backend bound to the agent, opened on first use.

        The model is what `default:` names — the same reference `model_switch`
        writes — so the config stays the one place that says which model an
        agent is on, and a task started after a switch runs on the new one.
        """
        if backend is not None:
            return backend
        current = live()
        name, provider, settings = current.resolve("")
        reference = f"{name}/{settings.model}"
        found = backends.get(reference)
        if found is not None:
            return found.with_key(*task.identity)
        async with opening:
            if reference not in backends:
                url = current.server(provider.api).url
                if url not in clients:
                    # One client per *server*, not per provider: a server speaks
                    # one wire format for every provider that uses it.
                    client, built = await open_backend(
                        url,
                        settings.model,
                        provider=name,
                        name=reference,
                        server_name=API_SERVER_NAMES[provider.api],
                    )
                    clients[url] = client
                    backends[reference] = built
                else:
                    backends[reference] = MCPBackend(
                        clients[url],
                        settings.model,
                        provider=name,
                        name=reference,
                    )
        return backends[reference].with_key(*task.identity)

    # ── the loop ─────────────────────────────────────────────────────────

    def adopt(payload: dict[str, Any]) -> tuple[list[Message], list[int]]:
        """Take the context plugin's answer as this turn's list, or refuse it.

        Refused rather than ignored, for the reason the agent server gives: a
        peer that answers with a shape this build cannot read is a system that
        has come apart — the same rule `slife2.clients.toolclient.remote_tools` applies
        to a tool list — and quietly keeping the context we had would make an
        unreadable answer indistinguishable from a decision to keep it.

        **`turn_ids` is read as strictly as `messages`**, and it did not used to
        be: `payload.get("turn_ids") or []` adopted an answer that carried
        messages and no ids as a context whose turns nothing could name, so the
        task's loop and the store disagreed about what it held from that point
        on.  A missing or null `turn_ids` is the same damage as a non-list
        `messages` and is refused the same way (DESIGN.md §10.1: a value that
        cannot be read is not a value that is absent).
        """
        raw = payload.get("messages")
        if not isinstance(raw, list):
            raise unreadable("the context store", "return a message list", payload)
        turn_ids = payload.get("turn_ids")
        if not isinstance(turn_ids, list):
            raise unreadable("the context store", "return a turn-id list", payload)
        return (
            [Message.from_wire(item) for item in raw],
            [int(turn_id) for turn_id in turn_ids],
        )

    async def restore(
        task: AgentTask, head: list[Message]
    ) -> tuple[list[Message], list[int], int]:
        """Put the conversation back on the context it had when it last stopped.

        **The agent's conversation, under the agent's key.**  A task starts from
        what this agent already knows rather than from nothing, which is the
        whole difference between a task and a stranger.  Called once, when the
        loop is built, which is the moment the agent server calls it too — and
        `covered` is every message, since a task has no carried tail.
        """
        payload = tool_payload(
            await (await memory()).call_tool(
                "restore",
                {
                    "agent": task.agent,
                    "subagent": task.identity[1],
                    "messages": [message.to_wire() for message in head],
                },
            )
        )
        messages, turn_ids = adopt(payload)
        return messages, turn_ids, len(messages)

    async def rebuild(
        task: AgentTask,
        messages: list[Message],
        turn_ids: list[int],
        covered: int,
        prompt: str,
        reference: str,
    ) -> tuple[list[Message], int]:
        """Decide what this task's turn runs on: keep ∪ recall.

        **Once, and once is exactly right** — a task runs a single turn, so the
        one call the agent server makes before a turn is the one call a task
        makes.  *Not* making it is the thing that would be wrong: §5.1's rule is
        that the context is chosen rather than accumulated, and a turn that ran
        on whatever happened to be in the list would be the one turn in this
        system whose context nobody decided.

        It is asked about **the agent's own context**, so the discriminator may
        recall what this conversation was about and the task's first model call
        carries the relevant part of a conversation nobody described to it.

        **And it writes only when it changes something.**  A decision that asks
        for what is in hand rebuilds nothing and touches nothing on disk
        (§5.1's common case) — which is a saving and **not** a licence, and it
        did not use to be read that way: the argument ran that because this call
        usually writes nothing it need not be serialised.  The case that *does*
        write is the one that lands, and it is exactly the case that has to be
        serialised: `rebuild` replaces the conversation's stored list with one
        built from the snapshot its caller's `restore` read, so a write landing
        after another writer's leaves the store holding a decision taken against
        a conversation that has moved — and the next `restore` (an idle reap, a
        restart, a new terminal) rebuilds from that list, which is a conversation
        that comes back one turn short, silently
        (`slife2/plugins/context/server.py`'s own comment on the write).  So the
        call is made under the conversation's lock, together with the restore it
        depends on (`run_loop`).
        """
        carried = max(len(messages) - covered, 0)
        payload = tool_payload(
            await (await memory()).call_tool(
                "rebuild",
                {
                    "agent": task.agent,
                    "subagent": task.identity[1],
                    "messages": [message.to_wire() for message in messages],
                    "turn_ids": list(turn_ids),
                    "prompt": prompt,
                    "model": reference,
                    "carried": carried,
                },
            )
        )
        rebuilt, _ = adopt(payload)
        # The ids are dropped rather than carried: the only thing downstream
        # that reads a live-context list is `record`, and a task does not
        # record.  They went *in* — a keep-list is read as an intersection with
        # them — and nothing here needs them back.
        return rebuilt, len(rebuilt) - carried

    async def run_loop(task: AgentTask) -> TurnResult:
        """One task's turn: the template as the user message, then the agent's
        own order around it — restore, rebuild, and the loop.

        **The same logic a turn of the main agent goes through**, and the order
        is the whole of what this repeats: the head is the agent's system
        prompt, `restore` puts back the context this conversation had,
        `rebuild` decides what the turn runs on, and the loop advances it.  What
        is missing is the inbox, which a task has no use for, and the record,
        which is argued below.

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
        current = live()
        name, _, settings = current.resolve("")
        head = [
            Message(
                role="system",
                content=render(current.agent.system_prompt, agent_name=task.agent),
            )
        ]
        opening = render(OPENING_TEMPLATE, name=task.name, description=task.description)

        # **The two calls under the conversation's lock**, which is the rule the
        # agent server states for the same pair: a rebuild's target is built from
        # the list its own restore read, so a write that lands after the same
        # conversation's own write is a decision taken against a snapshot that
        # has moved.  Held here rather than inside `restore`/`rebuild` because
        # they are one step — the second reads what the first answered.
        async with conversation(task):
            messages, turn_ids, covered = await restore(task, head)
            messages, covered = await rebuild(
                task,
                messages,
                turn_ids,
                covered,
                opening,
                f"{name}/{settings.model}",
            )

        loop = AgentLoop(
            await model_for(task),
            await registry(task),
            max_steps=current.agent.max_steps,
            # Asked again before every model call, as the agent server asks it:
            # a tool loaded mid-task is in the next call's list rather than
            # nowhere.
            refresh=partial(registry, task),
            # **No `deliver`, and so no `_async` here — a stated limitation**
            # (DESIGN.md §9, "Background work inside an agent task").  This loop
            # runs one turn and has no second one for a background result to
            # arrive in, so there is nowhere to hand one back to: the loop
            # therefore *refuses* the mode with a sentence the model can act on
            # (`BACKGROUND_UNAVAILABLE`) rather than running the call quietly, and
            # this registry has no `check_async` to offer either — the two are one
            # decision (`slife2.plugins.agent.server.registry`).
        )
        result = await loop.run_turn(messages, opening)
        # After the turn and not during it: the tool list goes out with every
        # model call, so dropping the excess mid-turn would take away a tool the
        # model had just loaded and was about to use.
        await trim_tools(task, messages)
        return result

    async def trim_tools(task: AgentTask, messages: list[Message]) -> None:
        """Bring the model's tool list back within its budget — and say so in the
        turn, rather than doing it behind the model's back.

        **The agent server's step at the agent server's moment**, and it belongs
        here for a reason that is not symmetry: the loaded set lives in the tool
        *catalogue* (`slife2-toolhub`), which is one list for the whole system —
        so a task that loaded tools past the budget has left every other
        conversation's request longer, and the trim is the harness's call
        because the harness is what knows the turn is over.

        **What went is written into the turn** as the harness pair
        `_func_tool_unload` called with no names, so a reader of the record reads
        that the list shrank and which tools left, rather than reaching for one
        it still believes it has (§8.5).  Nothing is written when nothing moved:
        an empty pair would be a record of something that did not happen.

        **A skip is a failure, and it is said at `error`.**  A task runs one
        turn and has no next request for a dead hub to make itself heard on, so
        the agent server's argument — "a hub that is really gone fails the next
        turn's tool list" — is not true here.  What is kept is the *outcome*
        (best-effort, so a task that has answered is not turned into a failed
        one by bookkeeping) and what is added is visibility: the skip is logged
        at `error`, naming the task, because the loaded set is one list for the
        whole system and an untrimmed one leaves every other conversation's
        request longer (DESIGN.md §1.3).  Registered as a §10.2 fallback: it
        *skips the trim* when the hub cannot be reached or answers with a shape
        this build cannot read; it *gives up* the trim for that turn; it *does
        not cover* a hub that is gone for good — there is no next turn here to
        surface it on, which is why the skip is said at `error`; the *failure
        goes* to the log as an error naming the task.
        """
        try:
            found = await unload_tools(await hub())
            unloaded, text = trim_answer(found)
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
        call = harness_call(FUNC_TOOL_UNLOAD)
        # **Both halves with nothing awaited between them.**  An interruption in
        # that gap leaves an assistant message whose call is never answered, and
        # that is the one history state every provider rejects — the reason the
        # agent server gives for writing its own pair this way, and the reason
        # `harness_call` is the only builder of one.
        messages.append(Message(role="assistant", content=None, tool_calls=[call]))
        messages.append(
            Message(
                role="tool",
                content=text,
                tool_call_id=call.id,
            )
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

        **Every failure is a message, not a silence** (§7): a loop that raises
        still has an answer to deliver, and a task that vanished without one
        would leave the conversation waiting for something that will never
        arrive.  Cancellation is the one exception, and it is not one — a task
        that was called off has nothing to report, so it delivers nothing.

        **And the two things a task holds are dropped on every way out.**  The
        outer `try` is the whole life rather than the delivery alone, because a
        cancellation re-raises out of the `except` below and used to take the
        path *past* the `finally` that lets go: a cancelled task kept its
        finished `asyncio.Task` — and that object holds the cancelled
        coroutine's frames, and the answer beside them — for as long as the
        registry remembered it, which is the retained megabytes the comment
        below exists to release.  It is the same rule for the cancelled ending
        as for the two that deliver: a task that has stopped holds nothing.
        """
        try:
            try:
                result = await run_loop(task)
                task.text = (
                    result.text.strip() or "(the task stopped without saying anything)"
                )
                task.status = FINISHED
                task.finished_at = now()
            except asyncio.CancelledError:
                # No await anywhere in this path: §3 measured that a plain
                # `await` in a cancelled scope is cancelled again at its next
                # checkpoint, so anything awaited here would simply not happen.
                task.status = CANCELLED
                task.finished_at = now()
                raise
            except Exception as exc:  # noqa: BLE001 - §7's rule, one level down
                logger.warning("agent task %s failed: %s", task.name, exc)
                task.text = f"The agent task {task.name!r} failed: {exc}"
                task.status = FAILED
                task.finished_at = now()
            finally:
                # **A task is over when it stops, not when its answer has been
                # handed over.**  The hand-over waits on another process, and the
                # cap counts what has stopped, so retiring here is what keeps
                # "finished" and "still holding a slot" from being the same
                # thing.
                retire(task)
            # The cancelled ending left through the branch above, so this is
            # reached by the two that have an answer.
            await deliver(task)
        finally:
            task.handle = None
            # **The answer is let go of here, and this line is the design rather
            # than tidiness.**  A finished task's message list, its loop and its
            # tool registry are locals of `run_loop` and die with it; what
            # outlives a task is this object, and the only part of it worth
            # megabytes is the answer.  Nothing reads it again — the hand-over
            # was the only reader, and there is deliberately no tool that fetches
            # an answer back (§1.3) — so keeping it would be holding a second
            # copy of something that is either already in the conversation or,
            # in the one case above, gone.  Keeping the *handle* would be the
            # same mistake in the other direction: a finished `asyncio.Task`
            # holds its result, so dropping the reference is what releases it.
            task.text = ""

    def retire(task: AgentTask) -> None:
        """Let go of an ended task, oldest first, past the cap.

        A task stops being *running* when it stops, but it stays answerable until
        it is forgotten — which is what makes asking about one that has finished
        a question with an answer.  What is bounded is how long that lasts.
        """
        retired.append(task)
        while len(retired) > RETIRED:
            oldest = retired.pop(0)
            if tasks.get(oldest.key) is oldest:
                del tasks[oldest.key]

    # ── the way back ─────────────────────────────────────────────────────

    async def deliver(task: AgentTask) -> None:
        """Hand a finished task's answer to the conversation that asked for it.

        **The hand-over itself is `slife2.libs.harness.handover`, and it is shared with
        `slife2-schedule`** — one road, the window, and one failure when it
        cannot be made.  Two plugins that both put a message into a conversation
        nobody asked them about is one more copy than this project keeps of
        anything, so what is left here is only the part that *is* an agent
        task's: what an answer wears, and what "delivered" means when it does not
        arrive.

        **An undelivered answer is a failure rather than a stage.**  A task that
        finished with nobody watching is the case a background task exists for —
        start it, close the window, walk away — and the honest report of that is
        that the answer reached no conversation *and was not carried anywhere
        else*: the work happened, the delivery did not, and the log says which.
        The answer is not run into the conversation to make the mismatch go away,
        because that is a turn nobody is watching.
        """
        try:
            await hand_over(
                config,
                task.agent,
                f"{marker(task)} {task.text}",
                channel_for(task),
                what=f"agent task {task.name}",
                window_client=window_client,
            )
        except DeliveryFailed as exc:
            logger.error(
                "agent task %s for %s was not delivered: %s", task.name, task.agent, exc
            )
            return
        task.delivered = True

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
            and task.home == str(home_dir())
            and task.status == RUNNING
        )

    def find(agent: str, name: str) -> AgentTask | None:
        found = tasks.get(client_key(agent, name))
        return found

    # ── the tools ────────────────────────────────────────────────────────

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[dict[str, object]]:
        nonlocal hub_conn, context_conn
        try:
            yield {}
        finally:
            for client in clients.values():
                await close_server(client)
            clients.clear()
            backends.clear()
            # Closed *and* forgotten, which is what the clear above is for as
            # well: this hook is not the process's lifetime over the in-memory
            # transport, so a client left in place is one the next session is
            # handed already shut — every task of it failing at its first hop.
            for held, owned in (
                (hub_conn, hub_owned),
                (context_conn, context_owned),
            ):
                if held is not None and owned:
                    await close_server(held)
            hub_conn = None
            context_conn = None
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
        not wait for it, and do not poll.

        `description` is the task itself. It starts from this conversation's
        stored turns, so **the turn you are answering is not among them** — put
        in what the task needs from the last exchange, and let it read everything
        older for itself. It runs with your prompt, your model and your tools,
        and nobody can answer a question it raises: what it sends when it stops
        is the whole of what comes back.

        `name` is the handle — how the task is asked about and stopped. This is
        not a way to run a program in the background.
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
        # wrote in somebody else's file.  `live()` is the same read the model is
        # taken with (`model_for`) and the step cap with (`run_loop`), and
        # `AgentTaskSettings.max_running` is written for it: the section this key
        # lives in is read by the plugin that enforces it, because the value is a
        # property of the home the caller is in and of the file as it is now —
        # the boot config is a snapshot of the daemon's own directory.
        limit = live().agent_task.max_running
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
            # is the caller's (`slife2.libs.plugin.audience.client_key` explains why
            # the object keeps it rather than asking again later).
            home=str(home_dir()),
        )
        tasks[task.key] = task
        # Detached from the call that starts it, which is the whole of what
        # "background" means here: the request that asked returns, and the loop
        # keeps going.  §3's rule for a write from a cancelled turn is the same
        # rule — work created outside the cancelled scope does land.
        task.handle = asyncio.create_task(run(task))
        logger.info("agent task %s started for %s", name, agent)
        return task.report()

    @mcp.tool(name=AGENT_TASK_STATUS, meta=FOR_THE_MODEL)
    async def agent_task_status(ctx: Context, name: str) -> dict[str, Any]:
        """What one agent task is doing, by name.

        Its state — `running`, `finished`, `failed` or `cancelled` — and never
        the result, which arrives by itself and cannot be fetched. Do not poll:
        if it is `running`, end your turn. `unknown` is a name this process no
        longer holds.
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
        is bounded — the most recent finished tasks are kept and the tail is
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
        here = str(home_dir())
        mine = [
            task for task in tasks.values() if task.agent == agent and task.home == here
        ]
        # Running first, then the most recently started — the order the question
        # "what is still going" wants, and a total order rather than a ranking.
        # Two sorts and not one: `sorted` here is the recency order, and the
        # `rows.sort` below is stable, so it lifts the running ones to the front
        # without disturbing it.  (A third sort used to precede this one, on
        # `(status != RUNNING, started_at)`, and its list was thrown away by the
        # `sorted(...)` on the very next line — a statement that did nothing.)
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
        build=build_server,
        logger=logger,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
