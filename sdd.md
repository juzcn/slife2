# slife2 — Software Design Description (SDD)

> The design of slife2 at the level a person who will change the code needs it:
> what each subsystem is, how it is decomposed, the interfaces between the parts,
> and the constraints that bind them. **The level is deliberate.** Mechanisms are
> described as rules and consequences, never as procedure: an algorithm, a
> default value, an identifier or a column list is stated in the code, which is
> the only place it cannot go stale. Where this document and the code disagree,
> **the code wins** — and this document should then be corrected to state the
> rule again rather than the fact.
>
> Four documents, and which one you are reading decides what you may do with it.
> **[README.md](README.md)** is the user's manual and the normative statement of
> what the system offers — install, configuration, the tool inventory, the
> keyboard. **This document** is the normative statement of how it is built. Both
> are answerable to the code. **[DESIGN.md](DESIGN.md)** is the developers'
> document: the record of *why* — the decisions, what was rejected, and the
> measurement that forced the choice — argued rather than normative, and cited
> from here as `DESIGN §n`.
> **[designer_notes.md](designer_notes.md)** is the designer's own memory of
> intent, is nobody else's to edit, and is cited the same way.
>
> The rules this design rests on are collected as
> [Appendix A](#appendix-a-invariants), stated as assertions.

## Contents

1. [Orientation](#1-orientation) — what slife2 is, the vocabulary, the layers
2. [The plugin contract](#2-the-plugin-contract) — the protocol, the audience, the family
3. [The machine](#3-the-machine) — the launcher, the shared servers, the config file, paths
4. [A turn](#4-a-turn) — the loop, the harness, markers and channels, the context decision
5. [Model backends](#5-model-backends) — one wire protocol per process, the stream, the failure contract
6. [The tool system](#6-the-tool-system) — the hub, the catalogue, sources, the budget, the search
7. [Memory](#7-memory) — the turns, the index machinery, the cabinet, embeddings
8. [Work that arrives](#8-work-that-arrives) — WeChat, the A2A mesh, agent tasks, the clock
9. [The terminal](#9-the-terminal) — the window, its inbox, restore, approval
10. [Configuration and the environment](#10-configuration-and-the-environment) — the file, secrets, the data directory, logs
11. [Project structure](#11-project-structure) — the four layers and the test tree
12. [Open questions](#12-open-questions) — what is not settled, and what would settle it
- [Appendix A. Invariants](#appendix-a-invariants) — the rules that must not be broken

---

## 1. Orientation

**slife2 is a terminal agent whose plugins are MCP servers.** It runs a
conversation with a model, gives that model tools, and reaches the world through
processes of its own. Everything below follows from one arrangement: a capability
is reached the same way whether it is in this process or another one, and the way
is MCP over Streamable HTTP on loopback.

### 1.1 The four layers

The tree says what each module *is*, and the rule is a fact about what it holds
rather than about its size (`README`'s Layout draws the tree):

| layer | what it is | the test |
|---|---|---|
| `plugins/` | a **process** — one directory per plugin, `server.py` inside it | it has a `main` and a `SERVER_NAME`, and something starts it |
| `clients/` | the **client half** of a hop | it dials a server this system or somebody else runs |
| `libs/` | a **library** | more than one thing imports it |
| `helpers/` | a small, stateless helper | stdlib and the other helpers, and nothing else |
| `tui/` | the terminal | it is a client of the robots, and serves nothing |

Two consequences are load-bearing. **`plugins/` holds no library and no
helper** — a module at that level with no `main` would be a file in a directory
whose every other entry is a process — which is why the plugin *contract* is a
library (`libs/plugin/`) even though every plugin sits on it. And **a plugin
directory keeps its own private libraries**: `cabinet/store.py`,
`schedule/expressions.py`, `skills/playbooks.py`, `wechat/ilink.py`,
`media/adapters.py`, `a2a/mesh.py`. `libs/` is for what more than one thing
imports, and a private library that moves up becomes a change about somebody
else's corpus (`DESIGN §1.1`).

**A library is not a lesser plugin, and a plugin is not a bigger library.** The
same capability can be both — `plugins/toolhub/server.py` is a process, and the
catalogue it reads is `libs/store/catalogue.py`, imported *inside* that process
rather than reached over a socket (`DESIGN §8.4`) — and that is the arrangement,
not a compromise. The cost of a library is the error boundary: a plugin that is
gone fails the turn with a named peer, and a file that will not open raises
where it is used. That is the right trade for a file, whose traceback lands in
the process that can describe it, and the wrong one for a service.

### 1.2 Vocabulary

- **Plugin** — one of ours: an MCP server over Streamable HTTP, started and
  shared by the launcher, honouring the contract in §2. "MCP server" alone does
  not separate ours from the twenty somebody else wrote, and the line is
  load-bearing everywhere: a missing plugin **fails the turn**, where a missing
  entry under `mcp:` is a model with fewer tools. An external MCP server is not
  a plugin.
- **Client id** — `(agent, subagent)`: which agent is talking, and which of that
  agent's conversations. It travels with every call at every hop, and every
  server keys whatever state it has by it. See §2.2.
- **Agent** — an identity, `--agent NAME`. It is a label and nothing more: no
  per-agent port, process or config section. The servers are shared by every
  instance (§3), and the label reaches them as part of the client id.
- **Conversation** — one client id's history, held by the agent server, stored
  by the context store.
- **Turn** — one exchange: the messages in, the model's answer, the tool calls
  it made, recorded as one row.
- **Channel** — where a message came in: `tui`, `wechat`, `a2a:<peer>`,
  `agent-task:<name>`, `async-tool:<name>`, `schedule:<name>`. It is recorded
  with the turn and drawn in front of the line (§4.4).
- **Marker** — the machine-readable prefix a channel puts in front of a message
  it delivers, `[Name:{…}]` (`libs/harness/markers.py`). A marker is content: it
  is stored with the message and stripped only for display.
- **Family** — a plugin that owns a **config section** and declares the rows that
  section describes: `mcp-tools` and `restapi-tools` hold connections
  (`libs/plugin/family.py`), `cli`, `skills` and the document families declare
  rows with nothing behind them.
- **Source** — the thing a row came from. One plugin holds one source per entry
  of its section, and declares it to the hub.
- **Corpus** — one searchable body of text with its two indexes: a
  conversation's turns, the tool catalogue, the cabinet's documents. All three
  are made of the same machinery (§7.2).

### 1.3 Two properties the arrangement buys

**A provider credential exists only inside the model-server process that needs
it.** The agent loop imports no provider SDK and holds no key: its only backend
talks MCP, so switching providers is changing a URL. The same holds for tools —
an API key a tool needs is exported into the environment of the child process
that plugin starts, and the conversation that asks for the tool never sees it.
The hub decides what the model may call and holds no credential at all.

**The loop is a pure function over a message list.** It owns no conversation, no
socket and no clock of its own (`libs/turn/loop.py`): it is handed a backend and
a registry, and everything it needs to know about the world arrives as an
argument or a callback. That is what lets a turn be tested with no server, no
model and no port.

### 1.4 What this costs

One JSON-RPC hop per token on loopback, which is small and is the price of the
architecture. There is a second cost of a different kind, and it is about size
rather than frequency: the MCP *client* refuses an SSE event over 1 MiB, and this
system reached it in a real session. So our own hops run a transport with **no**
ceiling (`libs/plugin/protocol.py`) — the peer is a process this system started,
and the largest message it can produce is bounded by the model's context — while
the gateway that dials somebody else's server keeps the SDK's ceiling, because
there a peer *is* a peer (`DESIGN §1`).

---

## 2. The plugin contract

**A plugin is an implementation of a protocol, not a protocol of its own.**
Nothing in `libs/plugin/` adds a method, a frame or a transport: a plugin answers
the same JSON-RPC as any MCP server, and an entry under `mcp:` speaks the same
wire. What this project adds on top is the contract, and it is three rules.

### 2.1 Every server is one of ours, and identity is checkable

`libs/plugin/protocol.py` holds what it takes to be one of our servers: the
flags every entry point accepts, the HTTP transport, the record that says a
daemon is here, and the start-up every plugin's `main` is (`serve_plugin`).

**A client proves which server it reached before it believes anything it says.**
`identifies` compares the connected server's own `server_info` against the name
the caller expected and that server's own probe tool; `open_server` is the one
place a client connects to one of ours, and it raises naming what could not be
reached rather than returning a client that will fail later. The rule is stated
once because the failure it prevents is the one that reads as something else: a
server on the wrong port, or a daemon from a previous build, answers MCP
perfectly well and is not the server anybody meant.

### 2.2 State is keyed by client id, and the id is a name

**Every server keys whatever state it has by a client id, and no client carries
state on a server's behalf.** There is nothing to open, no handle to carry and
no lifetime for a caller to observe, so there is no "not found" for one to
handle either: state is created when a key is first used and a key never goes
stale. An idle sweep may drop it underneath; the next call simply starts it
again, and the caller cannot tell.

This is the shape SEP-2567 asks for once sessions are gone — state addressed by
an explicit argument rather than by the connection — with one difference: **the
id is a name the caller already has**, not an opaque handle the server minted. A
handle has to be stored, and a stored handle is one more thing that can be lost,
expired, or wrong after a restart (`DESIGN §3`).

Two properties follow, and neither is enforced by a query. **Isolation is the
key**: two agents cannot reach each other's state because their ids differ.
**A hop is never anonymous**: even a server with no state to key receives the id,
so any log line can say whose call it served.

### 2.3 Who a call is for travels beside the model, not in it

`libs/plugin/audience.py` carries two facts, both out of band, because a tool
call has two parties.

**The tool says who may call it**, on the tool itself, under a namespaced `_meta`
key (`slife2/audience`, read from `tools/list`). A plugin's tool reaches the
model only if the tool says so; everything else — a tool that forgot, a tool
written before this rule existed — stays where it was. Opt-in and the default is
the safe half: the other convention, marking the internal tools and exposing the
rest, fails the other way, and one forgotten mark puts `remember` in a model's
hands with nothing anywhere saying so. An entry under `mcp:` needs no mark,
because the operator already opted in by writing it down.

**The caller says who it is**, on the call, and it is not an argument a model
writes: `client_meta` attaches the id, `request_client` reads it back, and the
reader is also what refuses an unaddressed call (`unaddressed`). A tool call runs
on behalf of exactly one conversation, and a model that could assert whose would
turn "read my history" into "read anybody's".

### 2.4 A family is one plugin holding one section

`libs/plugin/family.py` is the half a family plugin shares: hold each entry's
connection, declare what each offers, route a call back to whoever holds it, and
answer the four management tools in the section's own words (`FamilyWords`).
`mcp-tools` and `restapi-tools` are the two families that hold somebody else's
processes; `cli`, `skills` and the cabinet's kinds are *document* families, whose
rows are their own config section and which hold nothing.

**One sentence per state, and the state is read off the file before the link.**
`state_word` answers what a listing puts in `[brackets]`: a switched-off entry was
never asked, so its state is the operator's `off` and not the `idle` of a
connection nobody started. **An entry that answered with nothing is not an entry
that is silent**: `tools_as_text` keeps "it offers no tools" apart from "it has
not answered yet", because a listing that shows `ready` while the model is told
"not answering yet" is two answers about one source.

**A credential that did not resolve is refused before it is used.**
`libs/host/config.py`'s `unresolved_reference` is the one reader of "this value
is still a `${VAR}` or a `keyring:` reference" — the model servers, the media
adapter, the skills reader and the family's write tools all ask it — because a
reference sent to somebody else's endpoint travels as the secret itself and comes
back as a 401 that names nothing.

---

## 3. The machine

### 3.1 The config file is the system's description of itself

One file, `slife2.yaml`, inside the data directory, read by every process and by
the launcher (`libs/host/config.py`). It is the only place that says what exists:
which providers, which servers, which sections the tool families own, where the
embeddings come from, which model is the default. Nothing else in the tree holds
a second copy of those facts, and the vocabulary the file uses is the vocabulary
the code uses.

**The launcher's table is derived, never written down twice.** `Config.plugins()`
answers what this machine runs — the wire protocols some provider actually uses,
then the local servers — and the launcher starts exactly that, in that order.
The hub asks the same list which plugins to expect, so "which of ours" exists
once (`DESIGN §8.3`). A protocol no provider speaks is not started at all, and a
provider is a row in a backend's config rather than a process of its own: three
providers that speak two protocols are two model processes, because the
smallness is in what each process *does*.

**A malformed file is fatal; a missing one is not.** No config is a working
single-provider default, which is what a fresh data directory has. A file that is
there and cannot be used stops the start with a one-line message naming it: a
config mistake is worth a sentence rather than a traceback, because nothing has
started yet and the fix is in the file being named.

**`libs/host/configfile.py` is the one writer.** Four plugins and the model edit
their own sections through it, as a document edit under a cross-process lock: the
file's comments and its explanation of itself survive, and the *loader* is the
judge of what was written — a `slife2.yaml` this build cannot read back is put
back the way it was rather than left half-edited (`DESIGN §8.7`).

### 3.2 The servers are shared, and the launcher is the only thing that starts them

Every server is shared infrastructure (`libs/host/launcher.py`). `slife2` brings
up the ones its config needs and attaches to whatever is already there, so a
second instance is one command and no duplicate process. They are daemons: a
client exiting never stops one — but **the last client out does** (`others_running`,
which counts by pid liveness so a killed client cannot keep them alive).

Three rules carry the lifecycle:

- **A probe decides whether a server is alive, never a pid**
  (`libs/host/runtime.py`). A pid can be recycled; a probe cannot be fooled by
  one. Records carry a process start token as well, for the same reason.
- **Ready is the server answering as itself**, which is what `probe` checks:
  up *and* the server we expected, not merely a port that accepts a connection.
- **Start-up is a wave order, and the waves are a table** (`NEEDS`): everything
  a wave does not have to wait for starts at once, and a server whose dependency
  failed is not attempted at all — reported with the name of what it was waiting
  for, because a second failure describing the first is noise and a start that
  could not work is a process to clean up.

**A start is reported as it happens.** `ensure_all` is a generator and `slife2`
prints each server as it lands, because bringing one up blocks for a second or
two and there are ten of them: a report held back to the end is ten seconds of
blank screen and no way to tell a slow start from one stuck on a particular
server.

### 3.3 An agent name is an identity, and one name has one holder

`claim_agent` holds the exclusive right to run under a name for the life of the
client, taken with a kernel lock the operating system releases if the holder
dies. Two live claims on one identity is a contradiction rather than a
configuration, so the second instance is refused by name. What the claim does
**not** do is partition anything: the servers stay shared with every other agent,
and `--headless` is recorded in the claim because it is what tells a *model* that
nobody is sitting in front of this one (`DESIGN §1.9`).

### 3.4 `status`, `down`, and the one daemon the launcher does not own

`slife2 status` reports what each server this config names is doing, from its
record: running, unmanaged, not running, with the pid and since when. `slife2
down` stops the servers this config names and only ones this build can prove are
its own (`stop`), plus the A2A broker — the one daemon here the launcher does not
start, because it has no module and no MCP identity, and what makes stopping it
safe is a record written by whoever started it (`DESIGN §1.8`).

---

## 4. A turn

**One turn is: send the conversation to a model, stream what comes back, run the
tools it asked for, put the results back, and repeat until the model answers
without calling one.** `libs/turn/loop.py` runs that; `plugins/agent/server.py`
owns the conversations it runs in. The split is the design: the algorithm is a
pure function over a message list, and everything about *who* and *where* is the
server's.

### 4.1 The loop's contract

`AgentLoop.run_turn(messages, prompt)` takes the messages to run on and the
incoming prompt, and answers a `TurnResult`. What it needs from outside arrives
as an argument:

- a **backend** (`LLMBackend.stream`) — one method, and no knowledge of which
  provider is behind it;
- a **registry** (`libs/turn/tools.py`) — what the model may call, refreshed by a
  callback before *every* model call, so a tool loaded mid-turn is in the next
  call's list rather than nowhere;
- an **observer** — a display channel that may drop, delay or coalesce anything
  without the conversation noticing, which is what makes streaming over MCP safe
  to build on.

**The step cap is enforced where the count is known**, and a cap nobody wrote is
not a cap: `agent.max_steps` is read per turn from the config as it is now
(§4.5). A turn that reaches it ends with a stated reason rather than a silent
stop.

**A tool that fails is a result, not an exception.** `ToolRegistry.execute`
answers `(text, ok)`; `ok=False` reaches the model as text it can act on, which
is the loop's normal feedback path. The exception is a failure that is not the
tool's — a transport that died — and a tool call is never allowed to end a turn
by raising.

**Cancellation is a repair, not an unwind.** A turn cut off mid-stream leaves the
user's message in the conversation and nothing the model produced, because an
assistant message whose tool calls are only partly answered is the one history
state every provider rejects. The same repair is what a failure on the way out
of a tool batch needs: whatever ends a turn, the history it leaves is one a
provider will accept (§12, where that path is listed as still open).

### 4.2 What the harness adds to a conversation

`libs/harness/` is what the *harness* — this system, as opposed to the model or
the person — puts into a conversation. Three mechanisms, one shape.

**Markers** (`markers.py`): `[Name:{…}]` in front of a delivered message. A
marker is **content**, so it is stored with the message and travels into the
transcript; it is stripped for display by the reader that knows the vocabulary
(`markers.strip`), which is why a producer and its reader share the spelling
rather than each holding a copy.

**A hand-over** (`handover.py`): `hand_over` puts a message into an agent's
conversation through the window that is showing it — the one path a message from
another channel takes — and `note_to_window` draws a harness line in that window
without it ever becoming a turn. `submit_to` classifies the answer into
*written*, *refused* and *unknown*, and the difference is load-bearing: a
**refusal** proves nothing was written and may be retried, an **unknown** may
already have landed and must not be repeated. A message that cannot be handed
over is a `DeliveryFailed`, said at `error`, because those words were the only
thing that could answer the peer that sent them.

**Background calls** (`async_tasks.py`): `_async: true` on one tool call runs it
in the background and hands the model a task id. The answer arrives the way a
channel's message does — handed back into the conversation it came from — so
`check_async` is not how a result arrives; its job is the case where delivery did
not provably land, and the text is still there to be read. **The call is over
when its status is final, and the delivery is a separate fact**
(`Task.delivered`): a task whose hand-back is still in flight is neither running
nor a delivery that failed, and reading either as the other is how one answer
arrives twice and how a cancel lands on work that already finished.

### 4.3 A harness tool pair

Some things the harness has to say are said *as the model*, so that a provider
accepts them: `_func_tool_unload` after a trim, `_check_new_input` when a message
is waiting, are ordinary declared tools whose calls the harness writes into the
history (`libs/turn/tools.py:harness_call`).

The rule is that **a pair names a declared tool**, because the Responses and
Messages backends reject a tool call in history that is not in the request's tool
list — so the tool has to be in the model's list, which means the model can call
it too, and that is accepted: a model calling `_check_new_input` is the
boundary's own move made early rather than something new.

A pair is written with **nothing awaited between its two halves**. An
interruption in that gap leaves an assistant message whose call is never
answered, which is the one history state every provider refuses. And **a pair is
written only when something actually moved**: an empty pair every turn is a
record of something that did not happen, and a model that read those would learn
to distrust the mechanism.

`clients/toolclient.py:trim_answer` is where a trim's answer is read: only a
*readable* empty answer is a verdict, and a missing key is damage — the same rule
the hub's listing applies to a payload this build cannot read.

### 4.4 A channel, and what a channel owes

A message that arrives from somewhere other than this terminal becomes a turn,
and the turn records **where it came in**:

| channel | arrives from | drawn as | marker |
|---|---|---|---|
| `tui` | the person, in this window | `You>` | none |
| `wechat` | a WeChat login | `Wechat>` | `[Wechat:{…}]` |
| `a2a:<peer>` | a peer agent | `PeerAgent(jill)>` | `[PeerAgent:{…}]` |
| `agent-task:<name>` | a background loop's answer | `AsyncTask(research)>` | `[AgentTaskResult:{…}]` |
| `async-tool:<name>` | a background call's result | `AsyncTool(pdf_to_md)>` | `[AgentToolResult:{…}]` |
| `schedule:<name>` | the clock | `Scheduled(daily)>` | none — a trigger is not a result |

Three things follow from the channel being recorded: the marker is the
channel's, the line wears the channel's name, and a rebuilt conversation still
says where each message came from. **The two names are not the same string and
neither is derived from the other**: the channel is an address
(`agent-task:research`) and the line says who answered (`AsyncTask(research)> `),
which is the whole of what a person is being told. The prefix is decided in one
function (`tui/widgets.py:channel_prefix`) because the live prompt and the
rebuild both go through it, and a prefix decided twice is two answers to "who
said this".

A channel that delivers a message owes the conversation a **whole turn**, not a
prompt: it hands the words over and the answer is the conversation's. A channel
that cannot reach a conversation says so and stops — it does not try a second
road, because "carry on without the window" is exactly the fallback that hides a
broken system.

**An injected message can jump the queue.** The inbox has a cut-in mechanism, and
a message that takes it is drawn by nobody: the window is not what delivered it,
and a line about it would be a line about a turn that had not happened yet.

### 4.5 The conversation, and the context decision

The agent server owns the loops (`Loop`), keyed by client id and created on first
use. It reads the config **per turn** for everything a conversation can see — the
system prompt, the model, the step cap — so an edit to `slife2.yaml` reaches the
next turn rather than the next restart.

Before each turn, one question is answered: **what is this turn going to run
on?** `plugins/context/server.py` owns the turn log and the decision, and the
decision is a pure function (`libs/turn/context.py`): a discriminator model call
is asked what to keep of the conversation in hand and what to recall from the
log, and its reply is parsed into a `Decision`. Three rules bound it:

- **A reply that cannot be read is not a decision.** `parse` answers `None` and
  the turn runs on the context it already has — the registered fallback for every
  way that call can fail, not an error.
- **A kept turn and a recalled one are joined by id, in time order**
  (`normalise_ids` collapses duplicates to their first position).
- **A rebuilt turn wears a footnote** naming its id, its channel and its span
  (`turn_note`), so a model reading a recalled turn can tell it from one it just
  said; the footnote's *payload* is one function because a rebuilt turn and the
  same turn in memory differ by a prompt-cache miss when they are not
  byte-identical.

**The turn→messages builder is one function** (`messages_from_turns`), shared by
the restore path and the rebuild: they are the same conversation seen at two
moments, and two builders would drift into two conversations.

---

## 5. Model backends

### 5.1 One process per wire protocol

A model backend is a plugin like any other: **one process per wire protocol**,
serving every provider in the config whose `api` is that protocol
(`plugins/llm/`). Three chat protocols — OpenAI chat completions, OpenAI
Responses, Anthropic Messages — and one embeddings server, which is there
because it is the other thing here that speaks a provider's HTTP API.

**Responses is a protocol, not a flag on chat-completions**: different input
items, differently shaped tools, different streaming events. Merging them behind
one `api` would put two adapters in one file and make the choice a branch inside
the server rather than a fact about the config. `api` selects a module and the
server name it must identify as (`config.API_BACKENDS`, `API_SERVER_NAMES`), and
an unknown value is a typo worth reporting rather than a provider that quietly
never starts.

**The two properties of §1.3 are enforced here.** The provider SDK is imported
only inside these processes, and only inside the function that builds the client;
the key is resolved at that moment, so a server nobody calls never touches the
keyring; and a reference that did not resolve is refused by name before the SDK
sees it (`server_common.unresolved_key`).

### 5.2 The wire, and the one streaming pattern

`libs/model/base.py` is the neutral vocabulary: `Chunk` (a display increment),
`ToolCallDelta` (one provider fragment of a tool call), `Stream` (an in-flight
call), `LLMBackend` (a way to call a model). `libs/model/wire.py` encodes a chunk
onto the MCP progress channel, which is how a *display* increment crosses a
process boundary.

`plugins/llm/server_common.py` is the pattern, used four times: **one
`stream_chat` tool, provider output pushed down the progress channel as it
arrives, and the assembled message returned as the result**. The distinctions
that matter:

- **The result is authoritative and the chunks are not.** Progress notifications
  can be dropped, delayed or coalesced by the transport without the conversation
  noticing; the returned `StreamChatResult` is what the loop appends. That is
  what makes a display channel safe to lose.
- **An unreadable result is refused, not read as an empty answer.** The client
  checks the payload's shape before building a result (`clients/model.py`),
  because a missing result read as an empty assistant message is a turn that
  "succeeds" with nothing in it.
- **A stream that goes quiet is a stall, not a long answer.** `MCPBackend`'s
  drain watches the wait for *one chunk*, and a provider silent for the stall
  window is indistinguishable from one that has hung. The clock is on the chunk,
  never on the call: a model that streams for an hour is fine.

**A provider difference is config, not a branch.** `thinking`, `reasoning` and
`store` are fields of a model's entry, and each server sends only what its config
asked for (`build_request`); a value the protocol does not know is refused by
name at the point it is read rather than passed through to a 400.

---

## 6. The tool system

**Everything the model may call is a row in the tool catalogue, and one process
decides what is in it.** `plugins/toolhub/server.py` is that process: it holds no
connection to anybody's server (§6.2), keeps the rows, applies the naming rule
and the budget, and routes each call back to whoever declared it.

### 6.1 What the hub is

The hub is the only place the model's tool list comes from, and it is a plugin
because it is a *set*: which tools exist is a fact about the whole machine rather
than about one conversation. It serves the model three tools of its own, which
are the whole of how a tool arrives:

- **`tool_search`** finds rows by words and by meaning (§6.4);
- **`tool_func_load`** puts one in the model's list, which is what makes it
  callable: the model's list is the *loaded* set, and everything else is findable
  but not callable;
- **`_func_tool_unload`** is the harness's trim, called by the harness at a turn
  boundary (§6.3).

**The catalogue is a library and the hub is the process that opens it**
(`libs/store/catalogue.py`, one file for the whole data directory): the hub is
its only writer, and one writer is what a SQLite file already is.

### 6.2 Sources, and why a plugin declares rows

**The connections live with the plugins, and the hub holds none.** A plugin that
owns a config section holds the servers that section names and *declares* what
each offers; the hub merges the declarations into rows and routes calls back
(`libs/plugin/family.py`). What that buys is §1.3's property from the other side:
`SERPER_API_KEY` is read by the process that starts the child needing it, and the
hub — the process that decides what a model may call — holds no key at all.

**A plugin is a required source.** An external entry that cannot be reached is a
tool the model does not have; one of *ours* that cannot be reached is this system
coming apart, and the listing refuses rather than quietly serving a shorter one.
That is why a partial stack cannot run a turn, and why the end-to-end lane starts
all of them (§11.3).

**Names are rewritten once, at the hub.** Our own tools keep their bare name —
the server behind one is slife2 itself — and somebody else's becomes
`server__tool`, with the double separator because a single underscore is a
character an upstream tool name can contain. A name that came from somewhere else
is not a name to trust: `model_name` derives it and the separator is what makes
it reversible, and the provider's narrower alphabet is applied by `sanitise`.

### 6.3 The budget, and the pair that says what left

The model may hold a bounded number of function tools (`tool_load:` in the
config, enforced by the catalogue). The rule that makes the bound workable is
that **the trim happens at a turn boundary and the model is told**: the tools a
model has loaded go out with every request, so dropping the excess mid-turn would
take away a tool it had just loaded and was about to use, and dropping it
silently leaves it reaching for something gone.

So the harness calls `_func_tool_unload` after the turn, and **what left is
written into the conversation as a harness pair** (§4.3). The count and the
threshold are the catalogue's; what the server decides is *when* to ask, which is
the half a provider cannot do for us. The loaded set is one list for the whole
machine, which is why an untrimmed task leaves every conversation's next request
longer — the reason the agent-task server says a skipped trim at `error` rather
than at `warning`.

### 6.4 The search over the catalogue

`tool_search` takes **words** and **sentences**, and they are different inputs
because the two legs want different things: the keyword leg asks for every term
it is handed, so a sentence-shaped query demands six words at once and matches
nothing — which is most of why a sentence search was really a semantic search.
Both legs are fused by *rank* (`fuse_ranked`), because `bm25` is unbounded and a
cosine depends on the model, while a rank is comparable by construction. Several
sentences are several *questions*, and the rule a caller can rely on is that
**another question is another chance to be found and cannot cost a row an
earlier one already found**.

**Nothing asks for the whole catalogue by accident, and a listing is a different
question from a search** — which is why the store answers a browse and the tool
does not.

**Filters narrow and never decide**, and they are applied in SQL rather than to
the fused answer, because `rank` orders *every* match: cutting to the limit
afterwards still returns the best rows inside the filter. The clauses name the
corpus table's columns and are spliced into a query with one table in scope
(`index.filter_clause`/`where_clause`, and the subquery the keyword leg uses so
that a two-table join cannot make a column name ambiguous).

---

## 7. Memory

Three corpora are searched the same way, and they are made of the same
machinery. `libs/store/index.py` holds it: the FTS5 and `sqlite-vec` tables, the
identity recorded beside them that decides a rebuild, the chunk write, the keyset
paging, and the rank fusion over the two legs.

**It is a library because it is a mechanism three stores are made of.** The
alternative was each store owning its copy, and the cost of that is not
duplication but *silence*: a rule stated once is a rule the second copy can be
missing without anything saying so (`DESIGN §1.1`, where `_rowid` is the case in
point).

### 7.1 The turns

`libs/store/db.py` is one SQLite file per agent — a conversation's history, which
cannot be derived from anything and would be a loss. One file per *agent*, not
per conversation: a worker's store sits beside its parent's and carries the
worker's name, because a directory cannot carry it for two stores.

**A turn is stored as the message list the model saw**, which is why the two
halves of an exchange are read out of it rather than out of columns
(`TurnRecord.to_listing`): what was asked is the first user message and what was
answered is the last assistant message with prose in it.

**There is no migration layer.** The tables are created on a file that is not
this build's, and a file this build cannot read is reported and rebuilt rather
than upgraded in place, because an upgrade path is a permanent second way to
build the tables.

**The live-context list is a row, not a file**: what a conversation runs on is
persisted with the turns it chose, written in the same transaction as the turn
that changed it (`read_context`/`write_context`/`append_context`).

### 7.2 The index machinery

**Two legs, one answer.** A corpus is found by its words (FTS5 over a derived,
normalized string) and by its meaning (`sqlite-vec` over chunks of a different
derived string), and the two are fused by rank. The distinctions the machinery
exists to keep:

- **What a row is *found* by is not what it is *about*.** The keyword leg indexes
  everything worth looking a row up by; the vector is of a text that describes
  what the row *is*. One string for both would make one of the two worse for no
  reason.
- **A chunk is a row's text cut into pieces that each get a vector**, and the
  vector goes first, through the chunk that names it: a `vec0` row carries a
  rowid of its own, so a vector left behind is a row the next write collides
  with.
- **The width is discovered, never configured**, and a rebuild is decided by an
  *identity* recorded in the file (model, width, normalization rule) rather than
  by a migration: `vec0` fixes a table's width in its DDL, and a wrong one fails
  silently rather than loudly.
- **One normalization rule**, applied to a row's text and to a query alike
  (`libs/store/textindex.py`): a query is built to match what was stored, and two
  rules would agree everywhere except at the edges, where they would be wrong.
- **A list the caller sized is bound in chunks** (`index.chunks`), because a
  statement with more values than SQLite accepts raises `OperationalError`, and
  the corpus it raises on is the one nobody tests at that size.

### 7.3 The cabinet

`plugins/cabinet/` is four kinds of private document — notes, a diary, saved
files, reports — as one corpus with one index (`plugins/cabinet/store.py`).
**One table rather than four plus a view**: the four kinds are one corpus, and a
corpus whose rows live in four tables has to be unioned back together before it
can be ranked. `kind` says what a row is, `key` is what a read and a remove take,
and the columns a kind does not use are empty rather than absent.

**A document's markdown mirror is written in the same operation as its row**, and
the path is stored rather than derived, so a change to the naming rule orphans
nothing that already exists. **Every path a caller can name is resolved against
the cabinet root** and refused if it leaves it: a tool that writes a file for a
model must not be talked into writing anywhere else.

### 7.4 Embeddings

One server (`plugins/llm/embeddings_server.py`) and one client
(`clients/embedder.py`), shared by the two things with an index — **shared
because they must rank against the same model**: two copies of "ask and check the
shape of the answer" is how two indexes would come to disagree about what an
embedding is. The server holds the key for the same reason the model servers do —
it speaks a provider's API.

**The width is measured and the input limit is asked for.** The width can be
discovered by a probe; an input limit cannot be measured without sending the
oversized input, which is the failure this module's history records, so a model
whose endpoint reports no limit is refused rather than handed a guess.

---

## 8. Work that arrives

Four ways work reaches a conversation that a person did not type, and they share
one policy: **what arrives becomes a turn, and the conversation is told where it
came from** (§4.4).

### 8.1 WeChat

`plugins/wechat/` holds a login per agent and turns what arrives on it into a
turn. WeChat opens no API to a program; what exists is a hosted bridge, and
`wechat/ilink.py` is the whole of that protocol — the QR handshake, the long
poll, and the two envelopes that mean something other than what their status
says.

**Whose login a file is comes from the file, not the directory.** The directory
holds the *sanitised* agent name, so a session keyed by it would be a second key
for one login: a call from the raw name would build a second session, adopt the
same token and start a second poll loop, delivering every message twice. The raw
name is written beside the token, and a file too old to carry one is **skipped
and named in the log** rather than resumed under a guessed name — the same answer
an ownerless ledger gets from the schedule and A2A plugins.

### 8.2 The A2A mesh

`plugins/a2a/` is agent-to-agent traffic over MQTT, on the published profile. The
wire is the `a2a-over-mqtt` SDK's — topics, the retained agent card, the JSON-RPC
envelopes, the task lifecycle and the requester's retry ladder — and what this
package writes is the harness glue around it, because interop follows from the
wire being somebody else's.

Six rules here are this system's rather than the SDK's:

- **A window is what puts an agent online** (`join`), and presence is a *note*
  rather than a message: a peer coming online asks the model for nothing, and
  handing it to the conversation would spend a turn on a fact nobody has to act
  on.
- **A task is completed out of band.** The SDK runs the protocol lifecycle inside
  `on_request`, and this side blocks that call on a per-task outcome the model
  resolves later — which is what lets an answer arrive many turns after the
  request.
- **Outcome is one word, and it is the peer's own.** `mesh._reply_state`
  distinguishes completed, cancelled and failed, and the plugin renders *that*
  word: with the callback carrying a single `cancelled` flag, a peer whose
  handler raised had its `TASK_STATE_FAILED` read as "completed", which told the
  model the opposite of what had happened. The one outcome no reply can produce —
  the retry ladder running out — is named separately (`UNREACHABLE`), because it
  is the *requester's* failure and must not be rendered as a peer's answer.
- **A withdrawal outranks a late answer.** Cancelling records the cancel, and the
  store refuses to let a later result or failure rewrite an ending the caller was
  already told.
- **The broker is probed, owned and stopped with the stack.** The plugin starts a
  broker when the configured one is this machine and absent, fails the start if
  it still will not serve, and `slife2 down` stops the one this machine started —
  a record, not a guess, is what makes that safe.
- **An inbound response-typed message is acknowledged and dropped**: a peer that
  streams its result reaches us on the response topic, so a stray
  `task_response` on the request topic is not a new task.

### 8.3 Agent tasks

`plugins/agent_task/` runs **one loop of the agent's own** on a description, and
the answer comes back as a message. The properties that make it a task rather
than a second conversation:

- **A cap, and a name taken while it runs** — `agent_task_start` refuses a
  duplicate name and refuses past the cap, because a task is a means to an end
  inside one of the agent's turns.
- **A task's turns are not written to the db**: the task reads the agent's
  context and adds nothing to it — what the conversation gains is the answer,
  which is the message the agent server records.
- **Every failure is a message, not a silence**: a loop that raises still has an
  answer to deliver, and a task that vanished without one would leave the
  conversation waiting for something that never arrives.
- **Background work inside a task is refused, not ignored**: this loop runs one
  turn and has no second one for a background result to arrive in, so `_async` is
  refused with a sentence the model can act on rather than run quietly.

### 8.4 The clock

`plugins/schedule/` is the one thing here that starts work nobody asked for at
the moment it starts. `schedule/expressions.py` is the only module that knows
cron — a library, because there is nothing to serve — and it owns the timezone
policy and the error contract for the whole system.

**Two tables and one rule that is not written down as a constraint: no note means
it failed.** A **task** is a name, a description and an expression; a **run** is
one fire and what came of it. A run is opened when the task is handed to its
agent and closed when the answer is delivered, so "finished" and "still holding a
slot" are never the same thing.

**A fire is handed over, not executed**: the schedule plugin dispatches to the
agent-task plugin, which is the loop that runs it — which is why an agent-task
that cannot come up is a schedule that cannot run, and why the launch order has
the edge it does.

---

## 9. The terminal

**The terminal is a client and nothing else** (`tui/`, `plugins/tui/server.py`).
It holds no conversation state, no provider knowledge and no API key: it sends a
prompt and renders what comes back, which is the only role the MCP split leaves
it.

### 9.1 Three connections, three directions

| where | what it is | direction |
|---|---|---|
| the agent server | `AgentClient` — `run_turn`, `transcript`, `answer_approval`, `switch_model` | the window asks |
| the terminal's inbox plugin | `WindowClient` — `attach` held open, hand-overs pushed down it | the window is asked |
| the mesh plugin | `MeshClient` — `join` held open while this agent is on the mesh | the window holds a place |

**The window does not serve.** The thing that knows where each terminal is is an
ordinary plugin, started and shared like the rest, with a session per agent
(`plugins/tui/server.py`): the window is a *client* of it, which is what lets two
windows on one machine both be reachable. The exception this project used to
carry — "every plugin is an MCP server but the TUI" — is gone, and what replaced
it is a session per agent inside that plugin.

**A session lives exactly as long as the call that opened it.** Nothing has to be
deregistered: closing the stream cancels the handler behind it, which is what
makes a window that was killed rather than quit stop counting. A message handed
to an agent no window is showing is refused by name (`NO_WINDOW`), and the caller
decides what to do about it.

### 9.2 What the window draws, and what it does not keep

**Markers are stripped for display** by the reader that knows the vocabulary, and
the prefix is decided in one function (§4.4). A tool call is drawn as a line with
its argument; the harness's own lines — history restored, a turn cancelled, a
peer online — come from one place (`tui/notes.py`), so a producer cannot invent a
fourth spelling of one. **A connection has two states on screen and no more**:
the reasons live in the status tool, and a line that tried to say them would be a
line about the plugin's internals.

**A restored conversation is the server's.** `transcript` is read once when the
window opens; the window keeps no copy of its own, so what it shows after a
reconnect is what the server has. A read that fails is not an empty conversation
— that distinction is the difference between "this agent has no history" and
"this agent's history could not be read".

### 9.3 Approval, and the one call that runs the other way

A tool a plugin marks as needing approval parks the turn and asks the window
(`ToolApprovalRequested` on the progress channel). The answer is a **call of its
own** (`answer_approval`), because the turn's channel runs one way: a
notification cannot carry an answer back, and the call cannot wait for anything
the server holds per conversation — the waiting turn holds that. Losing the race
is not an error: the turn may have ended a moment earlier, and a client that
raised over it would call a prompt that arrived too late a failure of the
connection.

---

## 10. Configuration and the environment

### 10.1 The file

One `slife2.yaml` inside the data directory, and its sections are the system's
vocabulary: `servers:`, `providers:`, `embeddings:`, `agent:`, `agent-task:`,
`context:`, `tool_load:`, `mcp:`, `rest-api:`, `cli:`, `skills:`, `media:`,
`file-tunnel:`, `wechat:`, `a2a:`. **Every section is read by the plugin that
owns it** — the family plugins read their own rows, the hub merges them — and no
section is read by two processes.

**A section that is absent is a capability that is off, not an error.** A machine
with no `a2a:` section is a machine that is not on a mesh; no `media:` providers
is no images. What is not tolerated is a section that is there and wrong: an
unknown provider, an unknown `api`, a port that is not a port are each refused by
name at load, because a refusal is cheaper than a paid call that fails later —
and a *model* of the wrong kind for the capability asked for is refused too,
rather than quietly replaced by the first model of that kind.

**The file is read per turn for everything a conversation can see** (§4.5), and
cached by identity: a reader asks "did an edit happen" with one comparison
rather than a watcher, a version counter or a lock.

### 10.2 Secrets

A value may be a literal, a `${VAR}` resolved through the environment and then
the OS keyring, or a `keyring:` URI. **Resolution is lazy** — at the moment the
value is used, so a process nobody calls never touches the keyring — and an
unresolved reference is **left verbatim** so the failure lands where the name can
be said. `config.unresolved_reference` is the one reader of "this is still a
reference", and the rule it exists for is that a reference must never travel as
the secret (§2.4). A keyring that is *installed* and refuses is an error rather
than a "not found", because reading it as absent would send the reference itself.

### 10.3 The data directory

One directory, and everything is under it (`libs/host/paths.py`): the config, the
runtime state, the databases, and the agents' own files (a conversation, a
cabinet, a WeChat login, a work area). `--data-dir` (or `SLIFE2_DATA_DIR`) is the
knob and the *whole* answer to "where is this installation" — the config lives
inside it rather than being named separately, because a second answer would be a
second thing to keep in step. A child process is spawned with the *resolved*
directory, so a relative flag cannot be re-resolved underneath the working
directory it is given.

### 10.4 Records, logs, and what a person is shown

A daemon writes to a log under the runtime directory and records that it is
running; `status` reads the records and `down` stops only what it can prove is
ours. A **failed start reports the end of the log**, because a real error in a
real message is worth more than a status code, and the start-up report goes to
stdout while a plugin's own output is the log — which is what keeps the report
readable.

---

## 11. Project structure

### 11.1 The four layers

§1.1 states the rule and `README`'s Layout draws the tree. What the layering buys
is that a module's *kind* is visible from its path: a reader who wants the loop
opens `libs/turn/loop.py`, and a reader who wants the process that runs
conversations opens `plugins/agent/server.py`. The cost is one more directory
level, paid once.

### 11.2 The test tree mirrors it

`tests/libs/`, `tests/clients/`, `tests/plugins/`, `tests/helpers/`, `tests/tui/`
and `tests/e2e/` are named for what they test: one directory per layer, one
module per module, so "where is the test for this" has one answer. **The doubles
more than one module needs live in `tests/fakes.py`** — a stub model, a stub
embedder, the fake window and mesh clients, and the small factories
(`upstream_server`, `plugin_transports`, `config_file`) — because a test module
importing another test module is how two lanes end up disagreeing about the shape
of a double.

### 11.3 Two lanes

- **The fast lane** (`uv run pytest`): everything except `tests/e2e/`, across
  cores. No API key, no running server, and no fixed port — the `integration`
  tests each bind an *ephemeral* one, so a suite run beside a live stack still
  passes.
- **The end-to-end lane** (`uv run pytest -m e2e`): the whole stack as processes.
  Seventeen daemons spawned by the real launcher, on ports the test picks, with a
  config written into a temporary data directory, driven through the real client
  against an OpenAI-compatible endpoint served by the test itself. What it proves
  is what no in-memory test can: that the launch order works, that every child
  reads the same config, that a turn crosses five process boundaries, that the
  answer arrives both as a result and over the progress channel, and that the
  conversation it produced is still there for the next turn.

**The model is the only thing faked, in either lane, and it is faked at its own
protocol boundary in both** — a stub backend in the fast lane, a stub HTTP
endpoint in the e2e lane. A change to what the system does *with* a model
therefore cannot pass unseen by changing only the fake: the fake speaks the
protocol, and the code under test is on the other side of it.

---

## 12. Open questions

Each of these is a rule the design does not yet state, found by reviewing the
code against its own docstrings. What would settle each is named with it.

1. **An interrupted peer task.** The SDK's `TERMINAL_KINDS` is wider than its
   name — `input_required` ends the requester's ladder — and this system has no
   way to continue an interrupted task, so such a reply is recorded and read as
   `failed`. A peer that wanted more input is told its task failed. *Settled by:*
   a continuation path, or a fourth outcome both the store and the line carry.
2. **An inbound message that reaches no window.** A peer's message that cannot be
   handed over is logged and dropped, on the theory that a peer re-sends it — but
   the SDK acknowledges a request before the handler runs, so the peer's ladder
   has already stopped, and the responder stays parked holding a slot. *Settled
   by:* an explicit "could not deliver" the peer reads, or a delivery that is
   retried until a window appears.
3. **A scheduled run and the task that runs it.** A run is tied to its task by a
   name-derived id, which is not unique across two overlapping runs of one task.
   *Settled by:* an id minted per run.
4. **A fire in a repeated local hour.** A daylight-saving fall-back can make a
   daily expression fire twice, because the suppression compares instants and the
   repeated hour is two of them. *Settled by:* the rule stated in the expression
   layer, and a test in a zone that has the transition.
5. **A history left with an unanswered tool call.** The repair runs on
   cancellation; a failure that lands mid tool-batch needs the same repair by a
   different path, and the loop's comment assumes a tool execution cannot raise.
   *Settled by:* the failure path running the same repair, with a test that fails
   a tool the way a transport does.
6. **Where a retry policy lives.** Two channels keep their own backoff and reset
   it on different events. *Settled by:* one policy in `helpers/timeouts.py` and
   one reason to reset.
7. **A model call that ends the turn with an empty answer.** The wire is checked
   at the hop (§5.2), but a provider that legitimately answers with nothing is
   the same shape. *Settled by:* deciding whether an empty answer is a verdict or
   a failure, and stating it once.

---

## Appendix A. Invariants

Stated as assertions, because each has been broken once and the failure was not
obvious.

1. **A plugin is an MCP server over Streamable HTTP**, started and shared by the
   launcher, honouring the contract: state keyed by client id, an audience on
   every tool a model may be offered, an identity a client can check.
2. **No capability holds a credential where it is not used.** No provider SDK is
   imported by the agent loop, the terminal or a library; no key is read outside
   the process that needs it; a reference that did not resolve never travels.
3. **Nothing under `plugins/` is a library or a helper**, and every directory
   there has a `main` and a `SERVER_NAME`.
4. **The loop is a pure function over a message list**: it owns no conversation,
   no socket and no clock of its own.
5. **A history is one every provider accepts**: an assistant message's tool calls
   are all answered, and a harness pair is written with nothing awaited between
   its halves.
6. **A reading is never a verdict.** An empty answer, an unreadable payload and a
   silent peer are three different facts, and each is named where it is read.
7. **A failure that affects the design's behaviour hard-fails rather than falling
   back**, and every remaining fallback is registered, scoped, and says what it
   does not cover.
8. **One rule, one place.** A name, a path, a cap, a sentence or a vocabulary two
   modules need is stated once and imported, never copied.
9. **The config file is the only description of what exists**, and every process
   reads it as it is now rather than as it was at boot, for everything a turn can
   see.
10. **A message that arrives becomes a turn, and the turn records where it came
    from.**
11. **A client proves which server it reached** before believing anything that
    server says, and a payload this build cannot read is refused rather than read
    as a short or empty one.
12. **A list the caller sized is bound in chunks**, and a query that joins two
    tables names its columns or keeps one table in scope.
13. **The suite is the gate.** Both lanes pass before a change is done, and a bug
    found in the field arrives as a test that fails without the fix.
