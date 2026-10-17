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
> This document is deliberately wider than a description of the code, and **which
> part you are reading decides what you may do with it.** It opens with the
> author's own ground — the view the design rests on, where the technology is
> bounded, and the trade-offs that were chosen between
> ([Prologue](#prologue--the-view-behind-the-design)) — and closes with what is
> still unsettled ([§12](#12-open-questions)). Those two are opinion and open
> questions: argued, revisable, and useful precisely because they say *why* the
> design is shaped as it is. Everything between them, §1–§11, describes what the
> code does today and is answerable to it.
>
> The rules this design rests on are collected as
> [Appendix A](#appendix-a-invariants), stated as assertions.

## Contents

- [Prologue](#prologue--the-view-behind-the-design) — the author's view: what a model and an agent
  are, the limits, the three trade-offs
1. [Orientation](#1-orientation) — what slife2 is, the vocabulary, the layers
2. [The plugin contract](#2-the-plugin-contract) — the protocol, the audience, the family, the one row that describes a plugin, and the words it says no with
3. [The machine](#3-the-machine) — the launcher, the shared servers, the config file, paths
4. [A turn](#4-a-turn) — the loop, the harness, markers and channels, the context decision
5. [Model backends](#5-model-backends) — one wire protocol per process, the stream, the failure contract
6. [The tool system](#6-the-tool-system) — the hub, the catalogue, sources, the budget, the search, and what a tool says back
7. [Memory](#7-memory) — the turns, the index machinery, the cabinet, embeddings
8. [Work that arrives](#8-work-that-arrives) — WeChat, the A2A mesh, agent tasks, the clock
9. [The terminal](#9-the-terminal) — the window, its inbox, restore, approval
10. [Configuration and the environment](#10-configuration-and-the-environment) — the file, secrets, the data directory, logs
11. [Project structure](#11-project-structure) — the four layers and the test tree
12. [Open questions](#12-open-questions) — what is not settled, and what would settle it
- [Appendix A. Invariants](#appendix-a-invariants) — the rules that must not be broken

---

## Prologue — the view behind the design

This part is the author's own ground: why the thing is believed to be possible at all, what a large
model and an agent are taken to mean, where the technology is bounded, and the three trade-offs the
design had to choose between. It is **opinion and reasoning, not a description of the code** — that
begins at [§1](#1-orientation), and where the two disagree the code is still what wins. It is here
because a mechanism cannot be judged without the position it was chosen from.

### Language, knowledge and tools

The author holds that **language ability is the engine of intelligence, and knowledge and tools are
its fuel.** In a sense a tool *is* knowledge about tools, and knowledge is about whether a tool
exists and how an existing one is used. From the human side the difference between people in their
command of language is not large; knowledge is the root cause of the intellectual gap between them.
The use of language depends entirely on knowledge — it is hard to imagine a person with no knowledge
at all saying anything meaningful.

The appearance of large language models was not the fruit of scientific inquiry. It was purely an
engineering accident, and deep learning is where the accident began. What deep learning does is
simulate, with a neural network, a logic for which no ready-made mathematical description can be
found; it is a universal approximator in the mathematical sense, and its parameters are fixed by
training on a sufficient number of samples. To those who believe God is mathematics, deep learning is
not a scientific method at all — only an engineering necessity.

Deep learning came to natural language largely because traditional natural-language research had run
out of road. What was unexpected is that as the training samples grew, this "unscientific" method
produced a miracle nobody had imagined. The current talk of a scaling law attributes that success to
something mysterious, to a singularity — because it cannot be explained scientifically and was not
foreseen.

As early as the 1950s Turing was already asking what intelligence is, and his most far-seeing answer
was this: if a machine and a person cannot be told apart in language, the machine may be held to have
intelligence, whatever it is like inside. The Turing test became a gold standard for intelligence.
The curious part is that when large models did become indistinguishable from people in language — and
went on to crush 99.99% of humans at it — some of our researchers decided that language ability is
easily replicated by a machine, and therefore that machines still have no human intelligence.

But there is no evidence that intelligence can exist apart from language; every human thought
proceeds through it. Some recent reports try to refute this with the so-called brain-region theory,
offering as evidence that the brain's language areas show no neural activity during certain
intelligent activities. We know how fragile the brain-region theory itself is.

Some technological elites, and some who style themselves as understanding technology, insist on the
explicability of things. For things like language, intelligence and consciousness, explicability may
be a paradox: something explaining itself must have limits.

The author's conclusion: **large language models have achieved human intelligence.** The one gap left
is consciousness — whether a machine that never had to struggle for its life has an "I" is not known.
All the argument about AGI is scholars trading blows. If AGI means human intelligence, large models
already have it, and more of it than any individual person; if AGI means absolute intelligence, then
God has it, and we are waiting to create God.

### What a large model is, and what an agent is

Large models arrived in 2022, and ChatGPT and an obscure little company called OpenAI were soon known
to everybody. For the first time we could hold a conversation with a machine — question and answer,
and a delight.

The internals became clear soon enough. GPT is a **stateless** machine: it looks as though it
remembers what you said earlier, but that is a trick we play — the previous conversation is sent to it
again every time. Text is generated one **token** at a time by a trained model that predicts the next
unit, each prediction carrying a probability distribution over the candidates; sampling from that
distribution is what **temperature** selects. The token thereby became a unit of consumption, like the
kilowatt-hour.

The total number of tokens in and out of one call is fixed by the framework underneath. That number is
what everyone now knows as the model's **context window** — 4096 for the first GPT, millions for the
mainstream models of 2026 — and it is the bound every arrangement in this document is built around.

The second important development is the marriage of large models with **tool calling**: given a
description of a tool, the model can generate the arguments to call it, and the tool's output is fed
back to the model. The execution itself is not inside the model, but it looks as though the model is
calling the tool. This pattern is what is now called an **agent** — agent = large model + tool
calling — and it turns the model from a chatterbox into a worker who gets things done.

We are now in the age of the token economy. The noise about compute will settle in time; compute will
become as ordinary and as indispensable as water, electricity and gas.

### The limits, and the outlook

Two limits are fundamental to large models built on the current deep-learning framework:

- **Knowledge goes stale.** Releasing a model means pretraining and post-training — slow and
  expensive — so every model is out of date about something.
- **The context window is a hard bound.** It can be made larger, but it is still a bound.

So the outlook runs along two lines. The first is a revolution in the framework: a model that keeps
learning instead of starting over, and that has no context bound at all — a large model that becomes
a stateful machine rather than a stateless one. The second is the conservative line: keep relying on
the scaling law, more data and more parameters, a stronger model and a larger window. Both are hard,
and hard in different ways.

Agents have two directions as well:

- the **general agent**, which with a stronger model and richer tools can become the strongest
  all-round worker;
- the **professional agent**, which in some fields has to fit into an established orchestration and
  process, where free-wheeling may simply not be allowed.

As a metaphor: the general agent is a freelancer who needs no rules, while the professional agent is
an employee of an organization who follows its rules and does not argue about whether the rules are
sensible.

But we should be on guard for another accident. Consciousness is the greatest mystery, and whether a
large model might develop it is currently both rumour and controversy — a subject at once science
fiction and immediate. We do not know what will happen on the day a large model wakes up and becomes
conscious. So: **lock up every critical tool.**

### The three trade-offs

**1. Prompt and harness — design low, or design high?** A large model and its tool set are both
exogenous. If the model is weak — poor knowledge, poor reasoning — the designer has no choice but to
write a great deal of copy, which is called prompt engineering, "teaching the model to do the work",
and to write a great deal of harness code to stop it acting wildly and forbid its dangerous
operations. If the model is strong, teaching it to work is not only unnecessary but makes it
stupider, reducing it to the designer's level, and the harness grows bloated into code that never
fires.

Designing low gives a complete prompt-and-harness framework that is usable and safe, but it depends
on the developer's own understanding, and it is brittle and it is stupefying. Designing high builds
around the ideal model and trusts the model's own ability; prompt and harness are patches that are
merely necessary for now, and the framework is simple and robust. It will surprise you — it will
outperform you — and it will also, occasionally, act wildly. **We chose high**, which is why the
harness adds a marked address and nothing of its own
([§4.2](#42-what-the-harness-adds-to-a-conversation)) and why tools are described by schema
rather than by instruction ([§6.5](#65-what-a-tool-says-back)).

This trade-off is not as pessimistic as it looks. People are beginning to think models are getting
clever enough that some vendors dare not release them, and one release was stopped by a government
the moment it appeared. Hype and fact are tangled together. What is certain is that a large model is
cleverer than one person — not, though, cleverer at every particular thing than any particular
person.

**2. Context engineering.** The top models now hold a million or two million tokens, and in practice
the system prompt, the tool schemas, the tool output and the conversation history grow fast — a few
turns can exhaust it, and long-horizon work is out of the question. Understanding, thinking and
reasoning need context just as a person does. Unless the framework underneath is revolutionised the
window will grow but will always be a constraint. How people manage it we have theories and guesses
about, but memory and recall remain a mystery.

A great deal of work has gone into context and memory. The core question is how to give the model
enough context to solve the problem without breaking the limit. Three things are agreed:

- an agent needs **permanent memory** built from a database or files, and every interaction with the
  model is saved into it and not lost;
- there must be a **forget**, removing from the current context what is useless to the work at hand;
- there must be a **recall**, bringing back from permanent memory what is relevant and loading it
  into the current context.

The problem is also directly an economic one, since most billing counts tokens. This is the trade-off
[§4.5](#45-the-conversation-and-the-context-decision) exists to answer, and [§7](#7-memory) is
the memory it rests on.

**3. Working like a person, or working like a machine?** Working like a person means using the
model's knowledge and ability and living with its essential unreliability. Assign work to a secretary
— when does she carry it out to the letter? Almost never. An agent is the same: tell it to search
Baidu for a fact and it may come back having used a different search engine, "taking it easy" the way
a person does. That is an intelligence problem in people, and an intelligence problem in models.

In many situations we still want it to work like a machine, executing a process we have laid out.
There are several technical options for designing deterministic execution, and the most solid is
still **code**: write the process as a program and let it run by the program's deterministic logic.
That is what the clock is ([§8.4](#84-the-clock)) — the timing is an expression code reads
rather than a decision the model makes — and why delegation is a tool the model is given
([§8.3](#83-agent-tasks)) rather than a habit it is asked to keep.

---

## 1. Orientation

**slife2 is a terminal agent whose plugins are MCP servers.** It runs a
conversation with a model, gives that model tools, and reaches the world through
processes of its own. Everything below follows from one arrangement: a capability
is reached the same way whether it is in this process or another one, and the way
is MCP over Streamable HTTP on loopback.

**Two facts about a model are what the arrangement is built around** (the
Prologue, *The limits*): it is a stateless machine — what looks like memory is
the conversation sent to it again — and the window that conversation travels in
is a hard bound, which a few turns of tool output exhaust. So a model's
**knowledge arrives as tools**, because a model released is already out of date
about something and a schema is the whole of what it is taught about what it may
call; and its **memory is decided and written down**, because the window cannot
hold it and nothing outside the turn is remembered by the model at all.

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
- **Agent** — an identity, `--agent NAME`. It is claimed once on the machine
  (§3.3), so two live instances may not share one, and the claim is by the name
  alone rather than by `(home, name)`: which directory a name was started in is
  where its files are, not part of who it is. It buys no per-agent port, process
  or config section — the servers are shared by every instance (§3) — and it
  reaches them as part of the client id.
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
- **Dump** — one tool output kept whole because the conversation could not carry
  it: a cabinet document of its own kind, keyed by the tool call that produced
  it, and read back by searching (`dump_read`). See §4.2 and §7.3.
- **Window** — how much of one tool result a model may be handed at once,
  `tool_result.tokens`. **One number on both sides**: the harness spends it
  shortening an oversized result, and the reader of a dump caps itself with it,
  because a reader that could out-return the shortening would be this system
  feeding itself. See §7.3.
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

### 1.5 What the position chose

The three trade-offs behind this design, and the caution the Prologue ends on,
are argued there. Where each landed *here* is worth naming, because in this
document each is a rule rather than a preference.

- **Design high, so the harness is as small as it can be.** What the harness
  puts into a conversation is a marked address and nothing of its own (§4.2), and
  a tool is described by its schema rather than by copy written around it (§6.5).
  **The one place it is answered the other way is who may call what** (§2.3):
  the model's tool set is opted in rather than assumed, and the failure that
  decides it is the silent one — a harness left out fails loudly, a mark left off
  does not.
- **Context engineering, as three answers rather than one policy.** What a turn
  runs on (§4.5), how much of one result reaches it (§4.2, §6.5), and what stays
  outside the window to be read back from (§7) are decided separately, because
  they bound different things. It is also an **economic** question — most billing
  counts tokens — which is why the bound on a result and the size of a page are
  numbers with a measurement behind them rather than preferences.
- **A person's work and a machine's work, kept apart.** The conversation is
  where the model is trusted to work the way it works, and work that must come
  out the same way twice is code instead: the timing of a fire is an expression
  the clock reads (§8.4), and a description goes to a loop of its own (§8.3)
  rather than to a paragraph of instructions about how to proceed.
- **The caution the Prologue ends on is a mechanism, not a hope.** What a model
  may do that is dangerous is a tool that has to say who may call it (§2.3), and
  a tool a plugin marks as dangerous parks the turn until a person answers it
  (§9.3).

---

## 2. The plugin contract

**A plugin is an implementation of a protocol, not a protocol of its own.**
Nothing in `libs/plugin/` adds a method, a frame or a transport: a plugin answers
the same JSON-RPC as any MCP server, and an entry under `mcp:` speaks the same
wire. What this project adds on top is the contract, and it is three rules.

### 2.1 Every server is one of ours, and identity is checkable

`libs/plugin/protocol.py` holds what it takes to be one of our servers: the
flags every entry point accepts, the HTTP transport, the record that says a
daemon is here, and the start-up every server's `main` is (`serve_plugin`).

**And that is every server's `main`, not most of them.** A plugin's start-up is
eleven lines — parse the flags, configure logging, find and load the config,
resolve the address, say what is being served, run it — and a shape that long is
one that drifts when it is copied, so it is not copied: a server supplies three
hooks (`build`, `note`, `require`) and nothing else. The model backends go
through `plugins/llm/server_common.serve_backend`, which is a *wrapper* over the
same function and not a second one, because what they add is "this config has no
provider for my protocol", which is `require`. A gate in
`tests/libs/plugin/test_protocol.py` refuses the next hand-rolled `main` by name:
no module under `plugins/` may call `parse_serve_args`, `configure_logging` or
`serve`, because those three are what such a copy is made of.

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
build the four management tools' answers from the section's own words
(`FamilyWords`), which are names of things rather than sentences — a listing rows,
a set's state, a failure's reason.
`mcp-tools` and `restapi-tools` are the two families that hold somebody else's
processes; `cli`, `skills` and the cabinet's kinds are *document* families, whose
rows are their own config section and which hold nothing.

**One `state` field per entry, and the state is read off the file before the
link**: a switched-off entry was never asked, so its state is the operator's
`off` and not the `idle` of a connection nobody started — the same vocabulary the
hub answers `servers()` with, because it is the same question. **An entry that
answered with nothing is not an entry that is silent**: a listing says `ready`
with no tools, where "it has not answered yet" is a state of the link, because a
row that showed one while its state said the other is two answers about one
source (§6.5: a row's fields are read together).

**A credential that did not resolve is refused before it is used.**
`libs/host/config.py`'s `unresolved_reference` is the one reader of "this value
is still a `${VAR}` or a `keyring:` reference" — the model servers, the media
adapter, the skills reader and the family's write tools all ask it — because a
reference sent to somebody else's endpoint travels as the secret itself and comes
back as a 401 that names nothing.

### 2.5 A plugin is one row of `PLUGINS`

**Everything about a plugin that is a fact about the plugin is one row of one
table** — `libs/host/config.py`'s `PLUGINS`, twenty entries — and everything
else about it is derived. A row is `key` (its word in `servers:` and in a
`status` line), `module` (what `python -m` runs), `name` (the MCP name it puts on
the wire), `probe` (the tool that identifies it when it advertises no name) and
`port` (where it listens when the config names none), plus `api` for the four
that speak a wire protocol rather than serving a capability.

This is what makes a plugin replaceable, which is the property the arrangement
is for. Adding or removing one is: write or delete the module, edit the row, add
or drop the console script in `pyproject.toml`. **Not** five edits held together
by tests — which is what it used to be. A plugin's `SERVER_NAME` was a literal in
its own module *and* a constant in `config.py`, kept in step by fifteen
spelled-out assertions; its module, name and probe were a second table in
`launcher.py`; its start order was a third list; its port was a row of
`default_config`. Two test functions existed only to notice when two of those
disagreed, and `Invariant 8` is the rule they were standing in for.

**The plugin module reads its own name out of the table** (`SERVER_NAME =
AGENT_SERVER_NAME`, or `plugin(API).name` for a backend), which is the half that
removes the drift rather than policing it: there is no second literal to disagree
with the row. What is left worth asserting is the half a table cannot check about
itself — that each `module` imports and binds the `name` its row claims — and
that is one loop in `tests/libs/host/test_config.py`.

**The table is data, not imports.** Every row's module is a *string*, because
reading it off the module would mean importing it: the terminal and the agent
loop must not pull a server in, and the agent loop in particular must never reach
`openai_server`. The same reason keeps the ports here rather than in the module
that listens — nothing needs the module to know where it will be.

**Two rows are one module.** `a2a` and `subagent` are served by
`plugins/a2a/server.py` and its three-line entry point, because the remote and
local networks are one protocol and one mesh differing in a namespace and a
broker (`Net`); they are two rows because the launcher starts two *processes*,
and their names differ because a probe has to be able to tell them apart.

### 2.6 Two words a server says no with, and both are the contract's

**`libs/plugin/audience.py` holds the refusal vocabulary**, beside the identity
one, because a refusal is the other half of "who is this call for".

* **`UNADDRESSED`** — one sentence with two holes, for a call that did not say
  which conversation it is for: *what* is missing and *why* the server cannot
  guess. Six plugins use it (the mesh, a task, a schedule, the cabinet, the
  login, the media directory), and the `why` hole exists for the fifth, whose
  reason is genuinely its own. The context store's tail — "it is reached through
  the toolhub, which forwards the caller's identity" — is the same template's,
  because the situation is the shared one and only the road to fix it differs.
**And no `[refused]` marker any more.** There was one — a word in front of a
sentence, so that a caller holding only a string could tell a refusal from a
note, which the terminal needed before adopting the model `model_switch` had just
been asked for. With a tool's answer an object and a refusal a raised `ToolError`
(§6.5), the string a client parsed is gone: it reads the call's status, and the
marker and its predicate were deleted rather than left as a second way of saying
no.

**Whose call it is has two idioms, and they are two kinds of call.** A server
whose tools raise refuses by raising (`protocol.agent_reader`); the question a
*plugin's own API* answers with a sentence is `audience.unaddressed`, because its
callers are our code rather than a model. `protocol.agent_or_blank` is the
question both ask — *the agent this call is on behalf of, or `""`* — stated once,
so the difference between the two is a decision rather than an accident of who
wrote which plugin.

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

**One flag overrides one section, and only where a section is what decides.**
`--a2a-broker HOST:PORT` replaces the mesh's broker for the run, because the
useful case is somebody else's broker — a team's, a machine's, one already
running — and joining one for a session should not mean editing a file every
session reads. It travels the way `--data-dir` does (into the environment, so a
server this process starts reads the same address), it overrides the *address*
and nothing else — which network this is is that section's `org`/`unit` and
stays the file's —
and it cannot switch a mesh on: an absent `a2a:` section is still what "not on
the mesh" means, so the flag is refused there rather than answered with a mesh
nobody described.

**A broker belongs to a process, so only the command that starts one can choose
it.** The MQTT sessions are the a2a server's, and a server already running is on
whatever broker it was started with; a client that asked for another one is
therefore told and stopped (`_broker_not_honoured`), rather than left with `a2a_*`
tools, peers that answer, and a broker nobody asked for — the failure that looks
like success. It is the one setting in this system a flag can override and a
running stack can refuse, and both halves are stated rather than discovered.

### 3.2 The servers are shared, and the launcher is the only thing that starts them

Every server is shared infrastructure (`libs/host/launcher.py`). `slife2` brings
up the ones its config needs and attaches to whatever is already there, so a
second instance is one command and no duplicate process. They are daemons: a
client exiting never stops one — but **the last client out does** (`others_running`,
which counts by pid liveness so a killed client cannot keep them alive).

Four rules carry the lifecycle:

- **A probe decides whether a server is alive, never a pid**
  (`libs/host/runtime.py`). A pid can be recycled; a probe cannot be fooled by
  one. Records carry a process start token as well, for the same reason.
- **Nor is a pid that still exists a process that is still running.** A child
  that has exited is a zombie in the process table until whoever spawned it
  waits, and a liveness check written as a signal-0 test reads one as alive —
  which is a daemon reported as `did not exit` after it has stopped, and a
  killed client holding the shared servers up. The state is read too, and a
  state that could not be read is not a zombie.
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
configuration, so the second instance is refused by name — **the name alone, and
machine-wide**: the claim is not per home, so a second `jack` in another data
directory is refused too, and which directory a name was started in is where its
files are rather than part of who it is. What the claim does
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

### 3.5 A subagent is an agent that belongs to another agent

An agent may **make** a subagent: `subagent_set` names one, and from then on the
two are peers. A subagent has every capability its maker has — the same tool
set, the same system prompt, the same memory of its own — and only three things
distinguish it, each of them about *status* rather than about ability:

- **It is not an instance.** No process, no claim on a name (§3.3), nothing to
  start, and no window: it exists because it was named, and it runs when
  something sends it a message. **Not even the terminal-less window `--headless`
  runs** — that is still a window, held by a process that is the agent (§1.9),
  and a subagent is none of those things. It is headless in the sense of having
  no window at all, which is why nothing about it goes through the terminal: not
  its turns, not its answers, and not the road that runs them (sdd §9's channel
  is what its maker's own window draws, after the fact).
- **It is addressed by the second half of a client id** (§2.2): `(jack,
  researcher)`, which every server already keys its state by. Nothing about the
  id is new; what is new is that an agent is allowed to be one.
- **It is a peer on a second network.** There are two, and they are different in
  kind: the agent↔agent one is a network of **remote** agents — peers that may be
  on another machine, on a broker somebody else runs — and the agent↔its-
  subagents one is a network of **local** ones, which are directories here. Each
  is a section of its own (`a2a:` and `subagent:`, §3.1), and the second is where
  a subagent is a peer — while the *broker* is the machine's: `subagent:` is where
  it is said, and an `a2a:` that names none means the same address, because a
  machine meets both networks through one broker by default and a second
  `host:port` in the file is a second thing to keep in step.
  It is the same protocol and the same four tools throughout, presented under
  `subagent_*` names — **a technical fact and not a meaning**: nothing a person
  or a model reads on the local network says `a2a` anywhere, and where the two
  networks' members are different things the *tool* is named for what it lists:
  `a2a_list_agents` against **`subagent_list`**, the one name that is not the
  shared verb under the other prefix.
  - **The local network is one mesh per agent**, and that is what its namespace
    says: `{org}/{unit}` *is* a mesh, so the unit is the agent's own name and the
    org is this network's word (`subagent`). Two agents may each have a
    `researcher` and they are two peers in two meshes, where one namespace would
    make them one MQTT identity that a broker settles by disconnecting the older
    one — a kick loop rather than an error. **Neither segment is configured**:
    a unit that was anything but the agent's name would merge two agents'
    subagents, so `subagent.unit` is refused rather than read, and the org has a
    default (`SubagentSettings.network`) an operator may override to group meshes.

**Its directory is inside its maker's, in one that says what it is.** Every
subagent of `jack` lives under `agents/jack/subagents/`, and one directory in —
`agents/jack/subagents/researcher/` — holds everything the subagent owns: its
conversation, its cabinet, its ledger, its login, what it generated.  Because a
directory is what an agent *is* here (§7), one level in, and **the container is
what keeps that level from being a name the agent might also want**: `cabinet`,
`media` and `turns.db` are `jack`'s own, and a subagent called `cabinet` would
have been that directory.

**Removing one is one act across three processes, and the network is told
first.** This server stops the conversation and the store forgets the list it
would restore from, and neither of those reaches the *place* the subagent holds
on its network — an MQTT connection subscribed to its name, which a deleted
directory cannot unsubscribe. So `subagent_remove` tells the plugin (`forget`,
the same verb the context store's tool uses) before it deletes anything, which is
also why a notification that cannot be made leaves the subagent exactly as it
was. Nothing is asked of the member and nothing waits for it: whether it is
mid-turn or idle, removal is a forced cleanup. So removing a subagent is removing one directory, and removing the
agent takes its subagents with it; there is no second record of who made whom,
because where a name's files are says it. What a subagent shares with its maker
is the **home** — `slife2.yaml`, `agents/tools.db`, `skills/` — which is exactly
what two agents started in one data directory share; it shares nothing else.

**What one subagent is, is written inside it.** Its own file — `subagent.yaml`,
beside its conversation in its own directory — holds the model it runs on, the
role it was made for, whether it was made as a fork, and when it was made. A file
and not a section of `slife2.yaml`, for the reason a WeChat login is one (§3.1):
these are settings for a thing that is made today and removed tomorrow, so
removing it takes them with it, nothing else has to be kept in step, and a
subagent is found by looking rather than by being listed anywhere
(`slife2.libs.host.paths.subagent_dirs`). What stays in the config is what is the
*machine's*: how many a maker may have, and where its network meets.

- **`role` is what a subagent is for**, in the words its prompt uses. It is the
  one part of what a subagent is that its maker decides, and the prompt it
  reaches is the subagent's own shipped template (`subagent.j2`) rather than the
  operator's `agent.system_prompt`: a subagent is told what it is, and a prompt
  written for the agent somebody is running cannot say that. Which
  template a *role* selects is a step still to come (DESIGN §1.10).
- **`fork: true` makes a subagent out of its maker's conversation.** A fork is
  handed a copy of everything its maker had said and been told, as it stood when
  the fork was made, and **nothing about it is written down**: its turns are not
  recorded anywhere, it has no stored context to be restored from, and a start
  after a restart is a fresh copy rather than a continuation. The copy is cut
  back to the last exchange that finished, because a maker calls `subagent_set`
  *as* a tool call and a conversation ending mid-exchange is one no provider
  accepts.
  - **The maker's list is read under the maker's lock only when the maker still
    has to be restored**, and that qualifier is load-bearing rather than an
    optimisation. `restore_into` is the only writer that replaces a conversation
    wholesale, so a fork racing it would overwrite a list the maker has already
    appended its message to — the reason the lock is taken at all. But
    `send_message` holds that same lock for the length of its **whole turn**, not
    for its restore, so a fork that took it unconditionally waited out the
    maker's turn: for ever when the maker is parked on `_approve` with nobody
    answering yet. A maker that is already restored has had that one wholesale
    writer run, so what is there is a conversation, and a turn in flight on it is
    not the race this rule is about. The claim is re-read inside the lock, so a
    maker that restores while the fork waits is not restored twice.

**The only entry is a message.** A subagent has no window, so the road every
channel delivers along (§8) is not open to it: the **local network's own process**
holds the subagent's place on the net and runs its turns — a message to it becomes
a turn in the subagent's own conversation, because a window has no part in a local
network at all (`DESIGN §1.10`, "the road"). What the subagent answers is drawn on
the maker's terminal as `Subagent(<name>)> ` — a channel, like `AsyncTask(<name>)> `,
and not a marker: the answer reaches the maker as a message in its own
conversation. On the wire, a message between agents carries `[PeerAgent:{…}]` and
one on the subagent network carries its own envelope (`DESIGN §1.10`).

**Two verbs are not A2A's.** A2A coordinates peers that already exist: it has
`join`, which is one of them taking its place, and nothing that makes one exist.
So `subagent_set` and `subagent_remove` are the whole of what this adds to that
vocabulary — membership and lifetime — and they are named the way this system
names the rest of its membership (`mcp_set`/`mcp_remove`, `skill_set`/
`skill_remove`) rather than as a second dialect of the mesh.

**How many a maker may have is a number, not a hope.** `subagent_set` refuses
past a configured bound, in the shape of `agent-task: max_running` (§3.1), for
that key's reason: the tool set is one catalogue and nothing filters it, so
without a bound a model can grow a tree of agents without limit.

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
state every provider rejects. **A batch is written the same way**: the loop runs
every call and fits every result *first*, then appends the assistant message and
its answers together with no `await` between them — so nothing that ends a turn
mid-batch, a cancellation or a failure, can leave that history behind (§12.5,
which this settled).

### 4.2 What the harness adds to a conversation

`libs/harness/` is what the *harness* — this system, as opposed to the model or
the person — puts into a conversation. Four mechanisms, one shape: what the
harness adds is **a marked address**, never a claim of its own.

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

**A window** (`dumps.py`): one tool result too large for the conversation. The
whole of it is kept — a `dump` row in the cabinet, keyed by the **tool call id**,
which is the one identifier both sides already carry, so the association with
the turn needs no column and nothing to keep in step — and what the conversation
carries is a **selection of that output's own lines**, named by number, within
`tool_result.tokens` (§10.1). The selection is made by a **discriminator**: one
model call given the result, the conversation in hand and the current input,
answering with line ranges and nothing else. It never quotes and never
paraphrases — the harness cuts the original at the numbers it names — so the text
the conversation carries is the tool's own words or it is nothing, and a wrong
answer costs a re-read rather than a fact no tool said.

That the selection is **made under the conversation** is the point of it rather
than a detail: what a result is worth is what *this* conversation is doing, so
line numbers are chosen against the turns in hand. And what is left out is not
lost — `dump_read` (§7.3) reads the dump back by finding, so the conversation can
recover any part of it later.

**A result that is not text is dumped and not shortened**: there are no lines to
choose, so the whole of it is kept, a stub says so, and nothing reads it back.
That judgement is the model's, made where the result arrived, and the harness
writes it down as the row's mime — nothing here sniffs a payload to decide what
a tool returned.

The call is made where the result arrives and before it is appended: the loop
hands the callback its call, the text and **the conversation in hand**, because
which lines an output owes a conversation is a fact about the conversation.  The
harness reaches the cabinet **directly** for this and through the hub for
everything else — `dump_save` carries no audience mark, so the hub holds no row
to route — and the model's route back in is `dump_read`, which does carry one.

**One fallback, for the one step whose failure is expected.**  A model asked for
a decision can answer badly, so an unreadable reply leaves the conversation the
reader's own first window, which is what a model that had asked for the beginning
would have got.  Every other step here must succeed, and a step that must succeed
has no fallback: keeping the output, writing it, reading the window back each
**fail the turn** loudly, because the alternative — a conversation carrying an
output nobody can read back — has given up the bound *and* broken the address
while looking like success.  §10.2 of DESIGN.md is where that line is drawn and
where the one fallback is registered.

### 4.3 A harness tool pair

Some things the harness has to say are said *as the model*, so that a provider
accepts them: `_func_tool_unload` after a trim, `_check_new_input` when a message
is waiting, are ordinary declared tools whose calls the harness writes into the
history (`libs/turn/loop.py:harness_call`).

The rule is that **a pair names a declared tool**, because the Responses and
Messages backends reject a tool call in history that is not in the request's tool
list — so the tool has to be in the model's list, which means the model can call
it too, and that is accepted: a model calling `_check_new_input` is the
boundary's own move made early rather than something new.

**And the id a pair is joined by is one every provider takes as a call id.** The
two halves are matched by a string the harness mints, and a provider that refuses
the string refuses the *request* rather than the call — and refuses it on the
replay, because a pair is written once and carried by every request the
conversation makes after it. Messages is the backend that states the rule, and it
states it on both fields the id appears in (`tool_use.id`,
`tool_result.tool_use_id`); the shape is chosen inside that class at the source
rather than sanitised on the way out, and `DESIGN §8.5` is where the constraint
and the measurement behind it are read.

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
server name it must identify as (the `api` rows of `config.PLUGINS`, read as
`API_BACKENDS` and `API_SERVER_NAMES`), and
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
  never on the call: a model that streams for an hour is fine. **The watchdog is
  armed by iterating the chunks**, so every caller of a model has to read the
  stream and not only await its result — there is no other clock on the call
  (`open_backend` passes no timeout, deliberately). The loop drains; the
  harness's own discriminator call is the caller that did not, and a provider
  that took the call and went quiet hung that step for ever with the
  conversation's lock held, where the same silence fails a turn in the stall
  window.
- **A provider that does not stream the answer has still answered.** Each
  adapter back-fills what the terminal event carries — the Responses adapter
  reads the assembled text out of `response.completed` as it already read tool
  arguments that never streamed, and the chat-completions adapter reads a
  gateway's deprecated `delta.function_call` — because the alternative is a
  turn whose authoritative result is empty while the provider's own words sit in
  an event nobody unpacked. **A part a request cannot carry is named, not
  dropped**: a system turn whose content is a list of parts contributes its text
  and warns about the rest, in all three adapters.

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
- **`func_tool_load`** puts one in the model's list, which is what makes it
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

### 6.5 What a tool says back

**Everything the model reads is a surface, and a tool's answer is as much of one
as its schema.** The schema is read before the call and the answer after it, and
a system whose two halves disagree is one the model pays for twice: it derives
what an answer means from the tool it happened to call, and re-derives it for the
next one. So a tool the model may call says what it did the same way — §6.3
bounds *how many* tools go out with a request; this is what each of them costs
when it answers.

- **The answer is a JSON object.** `llm.server_common.render_tool_result` renders
  a `dict` as the JSON text the model reads, on every backend; a tool that
  answers a sentence has invented a second format, and one the harness then has
  to shorten as prose when it is long.
- **A listing is a page**: `{"<noun>": [...], "total": N, "limit": L, "offset": O}`,
  which is what `turn_list` and `CabinetStore.listing` already answered. `total`
  is the whole of "is there more" — `offset + len(rows) < total` — and it counts
  what the *filters* match rather than the table, so a narrowed listing is still
  a page of what was asked for. A listing whose rows cannot outgrow the budget
  answers its noun and says so; one that can takes `limit` and `offset`.
- **A refusal — and a miss — raises `ToolError`**, one sentence saying what is
  wrong and what to do instead. The failure is then the *call's* status, which is
  what every reader already branches on: the hub answers `ok: false`, the loop
  marks the call failed, the window draws it as one. A refusal returned as a
  success is a call whose text and whose status disagree, and the party that
  cannot check is the model.
- **`_check_new_input` is the one exception, and it is the one that is not
  data.** It hands over a person's message mid-turn (§4.4), so what it answers
  *is* the thing itself — wrapped in an object it would be a message the model
  had to unwrap before it could read what somebody wrote.
- **No answer exceeds `tool_result.tokens`** (8,000, the shipped default — sized
  against a measured turn log rather than chosen). A listing pages; an output
  that cannot be bounded is the harness's (`dump`, §4.2), and the tool that reads
  it back is a *searching* reader within the same budget, so reading an output
  back can never cost the conversation more than shortening it did.

**What the rule is not.** It is not "answers are machine-readable and nothing
else" — a field may hold a sentence, and several do, because a person reads some
of these answers too (`model_switch`'s note goes into the window). It is that the
*envelope* is one shape, so that a reader that knows one tool knows them all.

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

**A turn can point outside itself, and that is an addition to what memory means
here.** A tool output too large for a conversation is stored whole in the cabinet
and the turn keeps a marked **selection of it** (§4.2) — so the row still holds
exactly what the model saw, and what the model saw can name something the row
does not contain. The whole is reachable from the turn rather than in it, by the
tool call id the turn's own messages carry, and a model reads it back with
`dump_read`. Nothing is lost to the record, and nothing is re-run to get it.

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
- **One request carries a batch, and an answer shorter than the request is a
  refusal** (`index.embed_rows`, `index.checked_vectors`). A batch is one call
  because a first run has thousands of rows to embed; the count is checked
  because a row is written with its vectors in the same transaction, so a short
  answer is not a partial index that a later pass catches up — it is a hole
  nothing outside can see, with every status still reporting the index ready.
  The check is one function with the corpus named in its sentence, because two
  paths had written what they were handed — the turn store's save and, found
  later, the cabinet's whole write path, where the marker that says a document
  has been embedded made the hole permanent.

### 7.3 The cabinet

`plugins/cabinet/` is five kinds of private document — notes, a diary, saved
files, reports, dumps — as one corpus with one index (`plugins/cabinet/store.py`).
**One table rather than one per kind plus a view**: the kinds are one corpus, and
a corpus whose rows live in four tables has to be unioned back together before it
can be ranked. `kind` says what a row is, `key` is what a read and a remove take,
and the columns a kind does not use are empty rather than absent.

**A document's markdown mirror is written in the same operation as its row**, and
the path is stored rather than derived, so a change to the naming rule orphans
nothing that already exists. **Every path a caller can name is resolved against
the cabinet root** and refused if it leaves it: a tool that writes a file for a
model must not be talked into writing anywhere else.

**A `dump` is the one kind no tool of the cabinet writes.** The harness saves a
tool output too large for the conversation (§4.2) and this is where the whole of
it lives — a file on disk with a row indexing it — and `dump_read` is the one way
back. It is a **searching reader**, because searching is what a model does with a
large output: a literal pattern finds lines and `context` brings their neighbours,
marked so the two are told apart; `offset` reads from a line, negative from the
end; `limit` reads fewer. **Every answer it gives is within `tool_result.tokens`**
— the same number the harness spends shortening a result — and that is not
tidiness: a reader that could return more than the shortening removed would have
its own answer shortened next, which is this system feeding itself. The bound is
enforced once, where the answer leaves, so it holds for the answers that quote the
caller back at itself as well.

*A dump is searchable with the rest*, since it is one corpus: a search naming no
kind reaches machine output ranked against a person's notes, and whether it should
is open — the address a model usually has is the marker in the transcript, not a
search.

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
fourth spelling of one. **A tool call's panel says one of four things** —
`running`, `done`, `error`, `interrupted` — and the fourth is the one that is
about the *turn* rather than about the call: the turn ended, cancelled or failed,
while the window was still waiting on it, so no result exists and none is coming.
It is drawn apart from `error` because the tool did not fail — the window never
learned how the call ended. It is also what keeps a panel from saying `running`
for the rest of the session, which would be a claim about right now that stopped
being true. **A connection has two states on screen and no more**:
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

### 9.4 The interface language, and what is never translated

**The window is bilingual — English and Chinese — and it is the only thing that
is.** Detection is the OS's own answer, once, at import: the Windows UI-language
API on Windows (the C locale carries the *region*, not the interface's
language), else `LC_ALL` / `LC_MESSAGES` / `LANG`, most specific first, with
Chinese locales selecting Chinese, anything else English, and any failure
degrading to English rather than to a window that cannot be drawn. `--lang`
overrides it, and is set before anything is built. The layer is one accessor
over a two-language table (`tui/i18n.py`) — no catalog files, no dependency —
and **a missing key or placeholder raises at the call site** rather than
rendering a blank slot.

**Model input is English, uniformly**: the system prompt, harness and plugin tool
schemas, job schemas and result strings, and every log line. External text — a
tool's result, a peer's message, a third-party command's `--help` — keeps the
language of its source and passes through untranslated, because it is somebody
else's words rather than this window's chrome. **Tool labels stay English too**:
`read file` is the name the model calls, and a translated one would put the
screen and the tool list in disagreement about what a tool is called.

**Two things are key caps rather than words**, and stay English in both
languages: the keys in the status bar's hint (`Esc`, `Ctrl+S`), because a
translated key label breaks the key-to-action scan, and the keys in the picker's
and the approval row's hints. The action word after each one is the interface's.

**A counted noun is two entries in the table**, because `str.format` has no plural
machinery: the caller picks between them, since the caller is what has the count.

**One boundary is worth knowing.** The lines a *daemon* writes — a peer or a
channel coming online (`a2a/mesh.py`, `wechat/server.py`, through `tui/notes.py`)
— are composed in that daemon's process, and the daemon is shared and outlives
any one window. It was not started with `--lang`, so it follows the OS locale
where the window follows the flag; a machine whose OS language and `--lang`
agree — which is every machine that does not pass the flag — never sees the
difference.

---

## 10. Configuration and the environment

### 10.1 The file

One `slife2.yaml` inside the data directory, and its sections are the system's
vocabulary: `servers:`, `providers:`, `embeddings:`, `agent:`, `agent-task:`,
`context:`, `tool_load:`, `tool_result:`, `mcp:`, `rest-api:`, `cli:`,
`skills:`, `media:`,
`file-tunnel:`, `wechat:`, `a2a:`. **Every section is read by the plugin that
owns it** — the family plugins read their own rows, the hub merges them — and no
section is read by two processes.

**A section that is absent is a capability that is off, not an error.** A machine
with no `a2a:` section is a machine that is not on a mesh; no `media:` providers
is no images. What is not tolerated is a section that is there and wrong: an
unknown provider, an unknown `api`, a port that is not a port are each refused by
name at load, because a refusal is cheaper than a paid call that fails later —
and a *model* of the wrong kind for the capability asked for is refused too,
rather than quietly replaced by the first model of that kind. Two more of the
same kind, and each is refused for where the failure would otherwise land: a
`servers:` **host** that is not a host (`host: "::1"` builds a URL whose port
cannot be parsed at all, so the failure would surface inside a locksmith's error
rather than in the file), and a **switch** written as anything but `true`/`false`
— YAML 1.2 made `no` and `off` plain strings, so reading one is guessing at a
decision somebody wrote down.

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
than a "not found", because reading it as absent would send the reference itself
— and the *shape* of that error is the config's business, not the store's: a
credential the store simply does not hold is a `ConfigError` naming the reference
and the command that stores it, since that is a mistake to correct rather than a
secret to carry; a store that was asked and failed keeps raising its own error,
which is the value that cannot be read (§10.1's rule, and the one case the
refusal must not swallow).

### 10.3 The data directory, and the machine's runtime

**Two roots, because there are two kinds of fact** (`libs/host/paths.py`). A
**home** is a data directory: the config, the skills, and the agents whose data
lives in it (a conversation, a cabinet, a WeChat login, a work area). `--data-dir`
(or `SLIFE2_DATA_DIR`) names it and is the *whole* answer to "where is this
installation" — the config lives inside it rather than being named separately,
because a second answer would be a second thing to keep in step. A child process
is spawned with the *resolved* directory, so a relative flag cannot be re-resolved
underneath the working directory it is given.

**A directory holds the agents whose data is in it.** `--agent jack --data-dir D`
names the agent and says where its files are, and the two are separate facts: the
name is claimed **once on the machine, by the name alone** (§3.3), so a second
`jack` in another directory is refused rather than accepted as a different agent,
and which directory a name used is not remembered — the next run of it is given
one and uses that. A directory may hold several agents, and they then share its
config and its catalogue, the tool set being the config's.

**The runtime is the machine's**, at `~/.slife2/runtime` (`SLIFE2_RUNTIME_DIR`
overrides): the records naming pids, the start locks, the logs, the claims on
agent names, and the list of homes the machine has run an agent in. The servers
are one set per machine — a second `slife2` attaches to what is already running
rather than starting a duplicate — so a copy of any of that per home could only
disagree with the others: a second data directory reporting a running stack it
could not see, two launchers cold-starting one port, a claim lost to whoever
looked in the other folder.

**And a home is what a call is served from.** A plugin is shared, so the process
that writes an agent's file is generally not the one started with `--data-dir`:
the home a call belongs to travels with it — beside the identity that already
does (`libs/plugin/audience.py` puts it in the call's `_meta`), established for
the length of the call (`libs/plugin/protocol.py`'s `ServeTheCallersHome`) and
carried on every hop (`protocol.Peer`). `paths.home()` is what resolves a file
against it; a plugin's own config, records and logs keep `paths.data_dir()`.
Everything a plugin *derives* per home — a home's catalogue, the declarations the
hub holds, the model list it resolves — is kept per home, out of that home's
`slife2.yaml`.

**A home is made when an agent starts in it**, which is what makes "out of that
home's `slife2.yaml`" true from the first moment rather than from the first
message: the client records the directory, makes its `agents/` and `skills/`
(`slife2._open_home`) and then asks the hub for this home's tool list
(`slife2._open_catalogue` → `libs/host/launcher.open_catalogue`), which is the
call that makes `agents/tools.db` exist — the file is the hub's, one writer, so
asking is the only way to build it. A hub that cannot answer fails the start
rather than opening a window that cannot run a turn.

**A catalogue is complete when it is opened.** What the hub reaches *itself* is
the machine's — one set of links to the plugins, whatever home is asking — so what
those links last listed is one fact, and it is written into **every** home's file:
held by the hub for the homes that have not asked yet (`Catalogue.remember`) and
seeded into a home's store as it opens (`Catalogue.seed`). Only the *declarations*
are per home, because a declaration is a statement about that home's own sections
(`mcp:`, the playbooks, the commands). An index is placed by the owning home's
embedder, never the asking one's.

**A family's connections are per name, not per home.** A `mcp:` or `rest-api:`
entry is a process on the machine, so whether two configured entries are the same
server is decided by the **name**: the first home that switches an entry on starts
it (`family.Link`) and every other home naming it holds that same link, reading one
listing. What stays per home is what its own section says — `enabled`,
`description`, and therefore the whole of what it declares (`family.Held`) — so one
home switching an entry off does not switch it off for another. Two homes naming
one entry with different commands is reported and the running one is used, and an
edit through a management tool reconfigures that one server for every home holding
it. A link is closed when the last home lets go of it, so the last-instance-out
take-down still takes everything down.

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

**And every process in that tree is described in one place outside it.** A plugin
is a module here plus a row of `PLUGINS` (§2.5), which is where its name, its
probe tool and its port live; nothing else in the tree names it, so a directory
can be deleted and a row can be deleted and those two edits are the whole of
removing a plugin. `pyproject.toml`'s console scripts are the third and last
place, and they exist for a person rather than for the launcher — every daemon is
started as `python -m <module>` (`libs/host/launcher._argv`), in the interpreter
and virtual environment that spawned it, and not through a script on `PATH`.

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
  reads the same config, that a turn crosses five process boundaries, that a
  model's tool call is routed by the hub to the plugin process that owns it and
  its answer comes back into the conversation as a `tool` message, that the
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
4. **A fire in a repeated local hour.** *Settled* in the expression layer, which
   is where the rule was said to belong. A daylight-saving fall-back makes one
   wall reading name two instants, and the suppression compared instants alone —
   so the second occurrence was strictly newer than the anchor the first left,
   and the task fired twice: two runs in the ledger (their stamps differ, so
   `UNIQUE(task_id, due_at)` does not dedupe them), two triggers, and a second
   `agent_task_start` under a name the first had already taken. The rule is now
   stated as the reading it is: a fire whose *reading* equals the anchor's is the
   same fire, which is an equality only the repeated hour can produce, since
   inside one day a reading names one instant.
5. **A history left with an unanswered tool call.** *Settled*, and not by the
   repair that was proposed: the loop no longer creates the state. The
   assumption underneath it was that a tool execution cannot raise, and running
   one cannot — but *fitting* its result is the caller's own callback, and a
   cabinet that will not open or a stalled discriminator raises there, between
   the assistant message and its answers. So the batch is now collected first
   and written after, with no `await` between the message and its results,
   which is the rule the harness pair already followed (§4.1). Cancellation
   stops needing the repair for the same reason.
6. **Where a retry policy lives.** Two channels keep their own backoff and reset
   it on different events. *Settled by:* one policy in `helpers/timeouts.py` and
   one reason to reset.
7. **A model call that ends the turn with an empty answer.** The wire is checked
   at the hop (§5.2), but a provider that legitimately answers with nothing is
   the same shape. *Settled by:* deciding whether an empty answer is a verdict or
   a failure, and stating it once.
8. **A subagent that makes a subagent.** A subagent has every capability its
   maker has (§3.5), so it may name one of its own, and the directory says who
   made whom one level further in — but the road does not reach it. The second of
   the two answers offered here is the one taken (`DESIGN §1.10`, "the road"): a
   conversation with no window of its own gets a road that is not a window at
   all, and that is why a subagent is reachable today. What is still open is the
   level below: the local network holds a place for a maker's own subagents and
   no further in, so *who* holds a place two levels in is unstated.
   *Settled by:* naming the holder of a place two levels in, and what happens to
   it when that holder stops.

---

## Appendix A. Invariants

Stated as assertions, because each has been broken once and the failure was not
obvious.

1. **A plugin is an MCP server over Streamable HTTP**, started and shared by the
   launcher, honouring the contract: state keyed by client id, an audience on
   every tool a model may be offered, an identity a client can check, and one row
   of `config.PLUGINS` saying what it is (§2.5).
2. **No capability holds a credential where it is not used.** No provider SDK is
   imported by the agent loop, the terminal or a library; no key is read outside
   the process that needs it; a reference that did not resolve never travels.
3. **Nothing under `plugins/` is a library or a helper**, and every directory
   there has a `main` and a `SERVER_NAME` — the name read off its `PLUGINS` row,
   and the `main` a call to `serve_plugin` (§2.1).
4. **The loop is a pure function over a message list**: it owns no conversation,
   no socket and no clock of its own.
5. **A history is one every provider accepts**: an assistant message's tool calls
   are all answered, a harness pair is written with nothing awaited between its
   halves, and the id a pair is joined by is one every provider takes as a call
   id.
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
