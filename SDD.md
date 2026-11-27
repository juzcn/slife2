# slife2 — Software Design Description

| | |
|---|---|
| **Document** | Software Design Description (SDD) |
| **Version** | 0.6.0 |
| **Date** | 2026-11-27 |
| **Status** | Normative |
| **Audience** | Developers, operators, and reviewers of slife2 |

This document is the design of slife2: what each subsystem is, the contract it
implements, and the constraints that bind it. It is normative for the design.
The source code is normative for every implementation detail; where the two
disagree, the code is authoritative and this document is corrected to state the
rule again.

The user manual is [README.md](README.md). Implementation mechanics — call
order, private helpers, literal values — belong in the code and its comments.

## Contents

1. [Introduction](#1-introduction)
2. [System overview](#2-system-overview)
3. [Plugin contract](#3-plugin-contract)
4. [Machine and agent lifecycle](#4-machine-and-agent-lifecycle)
5. [Turn execution](#5-turn-execution)
6. [Model backends](#6-model-backends)
7. [Tool system](#7-tool-system)
8. [Memory and indexing](#8-memory-and-indexing)
9. [Inbound work](#9-inbound-work)
10. [Terminal](#10-terminal)
11. [Configuration and environment](#11-configuration-and-environment)
12. [Operations](#12-operations)
13. [Project structure and development](#13-project-structure-and-development)
14. [Open issues and deferred capabilities](#14-open-issues-and-deferred-capabilities)
- [Appendix A. Invariants](#appendix-a-invariants)
- [Appendix B. Plugin registry](#appendix-b-plugin-registry)
- [Appendix C. Fallback register](#appendix-c-fallback-register)

---

## 1. Introduction

### 1.1 Purpose

This document defines the design of slife2 version 0.6.0 for a person who will
change the code or operate the system: the architecture and process model, the
plugin contract, the turn loop, model backends, the tool system and memory, the
interfaces between terminal, plugins and configuration, and the invariants a
change must preserve.

### 1.2 Scope

The scope is the slife2 package, its plugins, its command-line entry point, its
terminal application, its configuration, and its data and runtime directories.
External components appear only where slife2 depends on their behavior: model
provider APIs, other parties' MCP servers, MQTT brokers, tunnel providers, and
the WeChat bridge.

### 1.3 Intended audience

Developers changing the agent loop, a plugin, the tool system, memory, or the
terminal; operators deploying slife2 and diagnosing the shared server stack;
reviewers checking a change against the invariants.

### 1.4 Definitions and abbreviations

| Term | Definition |
|---|---|
| **Agent** | An identity named by `--agent NAME`, claimed once per machine. It is not a process, a port, or a data directory. |
| **Conversation id** | The pair `(agent, subagent)` identifying the conversation a call is for; the subagent component is empty for an agent's own conversation. |
| **Conversation key** | `(home, agent, subagent)`: the identity a shared server keys per-conversation state by; unique on a machine. |
| **Home** | A data directory holding `slife2.yaml`, `AGENTS.md`, `skills/`, `jobs/`, and `agents/`. A home may contain several agents. |
| **Plugin** | One of slife2's own MCP servers, started and shared by the launcher. An external MCP server is not a plugin. |
| **Family** | A plugin that owns one configuration section or folder and declares the rows it holds to the hub. |
| **Source** | The name a plugin files rows under: an `mcp:` server, a `rest-api:` document, or the whole of a `skills`, `cli`, or `job` collection. |
| **Catalogue** | The tool catalogue: one SQLite file per home holding every row the model may discover. |
| **Conversation** | One agent's own history, or one of its subagents', stored by the context plugin. It is the unit every shared server addresses and keys state by. |
| **Turn** | One exchange: input, model output, and tool calls, stored as one row. |
| **Channel** | The origin of a message: `tui`, `wechat`, `peer_agent:<peer>`, `agent:<name>`, `subagent:<name>`, `agent-task:<name>`, or `async-tool:<name>`. |
| **Marker** | A machine-readable prefix on a delivered message, of the form `[Name:{…}]`; content stored with the message. |
| **Dump** | A tool output stored whole in the cabinet because it was too large for the conversation. |
| **Runtime directory** | The machine-wide directory holding server records, locks, logs, and agent-name claims. |
| **MCP** | Model Context Protocol. |
| **A2A** | Agent-to-Agent, carried over MQTT using the published A2A-over-MQTT profile. |
| **SSE** | Server-Sent Events, used by the MCP Streamable HTTP transport. |

### 1.5 References

| Reference | Use |
|---|---|
| [README.md](README.md) | User manual; normative for user-facing behavior. |
| Model Context Protocol specification | Wire protocol implemented by every server. |
| A2A-over-MQTT profile 0.1 (`emqx/mqtt-for-ai`, `specification/0.1/basic/mqtt_transport.md`, over A2A v1.0.0) | Wire protocol of the peer and subagent networks; §9.2 states the conformance claim. |
| OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages APIs | Model wire protocols. |
| `slife2.yaml` | Annotated configuration reference shipped with the project. |
| `AGENTS.md` | The model's guide to its tools: a home file beside the configuration, shipped and installed into every home; the system's deliverable, not a document to edit. |

### 1.6 Document conventions

- **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are used as in RFC 2119. A
  **MUST** violation is a defect; a **SHOULD** may be departed from only with a
  stated reason.
- Module and tool names are written in `code font`. `<data>` paths are relative
  to a home; `<runtime>` paths are relative to the runtime directory.
- **Why** paragraphs are explanatory: the decision, the rejected alternative,
  and the fact that settled it. They are not normative. A Why that no longer
  decides anything is deleted: this document states the design, not its
  history.
- "The system" means the slife2 package and all processes it starts.

### 1.7 Document structure

Sections 2–4 describe the architecture and lifecycle. Sections 5–10 describe the
runtime subsystems. Sections 11–12 describe configuration, storage, and
operations. Section 13 describes the project structure and development process.
Section 14 records open issues and deferred capabilities. Appendix A states the
invariants, Appendix B points at the plugin registry, and Appendix C is the
fallback register.

## 2. System overview

### 2.1 Purpose

slife2 is a terminal-based AI agent. It runs a conversation with a language
model, exposes tools to that model, and reaches the outside world through local
processes. The terminal window is a client; the agent loop, tool hub, model
backends, memory, and every capability are MCP servers.

### 2.2 Operating context

slife2 runs on a user's machine and talks to model providers over their HTTP
APIs; external MCP servers over stdio or Streamable HTTP; OpenAPI documents
through an MCP proxy; a local or remote MQTT broker for peer agents and
subagents; the WeChat iLink bridge; tunnel providers (ngrok, localhost.run,
Cloudflare Quick Tunnel); and the user, through a terminal UI.

All internal server-to-server communication is MCP over Streamable HTTP on
loopback. The terminal and the agent server are both MCP clients and servers:
the terminal is a client of the agent server and the terminal inbox plugin; the
agent loop is a client of the tool hub and the model backends.

### 2.3 Design goals

1. **Credential isolation.** A credential exists only in the process that uses
   it. The agent loop and terminal hold no provider credential and import no
   provider SDK.
2. **Provider and tool independence.** Switching providers changes a URL and a
   configuration entry, not the loop. Adding a tool adds an entry or a plugin,
   not a branch in the conversation code.
3. **One description of the machine.** `slife2.yaml` is the only statement of
   which providers, servers, sections, and capabilities exist.
4. **Explicit failure.** A missing plugin fails the turn; a malformed
   configuration fails the start; a refusal is a failure, not a silently
   degraded success.
5. **Testability without a stack.** The loop is a pure function over a message
   list; servers run over an in-memory MCP transport without binding a port.
6. **Minimum harness; trust the agent.** The model chooses what to call; the
   harness enforces only the invariants that hold for every choice — identity,
   isolation, budgets, delivery — never a tool-choice preference.
7. **Schema over prompt.** A tool's arguments, types and required fields are its
   JSON schema, checked on the call. Prose carries intent; a constraint that
   fits a schema is not a prompt sentence.
8. **No arbitrary fallback.** A fallback is a designed, temporary behaviour,
   registered in Appendix C: it either heals by itself and is said, or the
   operation that needs the damage repaired hard-fails. A failure read as a
   success is a bug; a degraded mode that never heals is a defect with a log
   line.

### 2.4 Constraints

- Python 3.13 or newer, on an interpreter that can load SQLite extensions
  (`sqlite-vec`).
- A plugin is a process. A capability that holds a credential, an SDK, or
  another party's connection MUST be a plugin. One that holds only a file MAY be
  a library; if it must also be a tool, the process is a plugin and the file
  handling is a library or private module.
- Internal state is addressed by a conversation key, not a session. The MCP
  transport must preserve conversation identity on every call.
- The model's context window is a hard bound, declared by every model entry.
  Every mechanism that adds text to a request needs a bound or eviction rule.

**Why.** The error boundary decides. A missing plugin fails the turn with a
named peer, while a file that will not open raises where it is used. A store has
one writer, which is what a SQLite file already is; a plugin around it would add
a hop without an isolation boundary. So the turn store is a library opened by
the context plugin, the tool catalogue a library opened by the hub, and the
cabinet store private to the cabinet plugin.

### 2.5 Architecture summary

Five layers:

| Layer | Contents | Rule |
|---|---|---|
| `slife2/plugins/` | One process per plugin, one directory each | A plugin has a `main` and a `SERVER_NAME`; the launcher starts it. |
| `slife2/clients/` | The client half of an MCP hop | It dials a server this system or another party runs. |
| `slife2/libs/` | Libraries | More than one component imports them. |
| `slife2/helpers/` | Stateless helpers | They depend on the standard library and other helpers only. |
| `slife2/tui/` | The terminal application | It is a client and serves nothing. |

A plugin directory MAY contain private libraries, such as
`plugins/cabinet/store.py` or `plugins/schedule/expressions.py`. The dependency
direction is:

```text
tui/ -> MCP -> plugins/agent/ -> libs/turn/loop.py
     -> {libs/model/base.py, libs/turn/tools.py, libs/turn/events.py,
         libs/turn/messages.py} -> libs/host/config.py
```

The loop imports neither the agent server nor the terminal. It receives a
backend and a coroutine returning a tool registry; the agent server supplies
them, and the loop does not know where they came from.

### 2.6 Process model

- **One process per plugin.** The launcher derives the set from the
  configuration and starts each plugin at its configured or default address.
- **One process per wire protocol, not per provider.** Providers speaking the
  same protocol share a model server.
- **Shared servers.** Every server is machine-wide infrastructure. A second
  slife2 instance attaches to the running servers rather than starting a second
  set. The last client to exit stops every server record on the machine —
  including a backend another configuration started — unless `--keep-servers`
  is used.
- **One mesh per agent.** The peer network and the subagent network are separate
  namespaces, but each agent has one MQTT identity per network.
- **Daemons are reconstructible.** The runtime directory can be deleted; the
  next start rebuilds what it needs.

Each plugin has one job:

| Plugin | Job |
|---|---|
| `agent` | The conversation loops |
| `context` | The turn log and the context decisions |
| `toolhub` | The tool set |
| `slife2-llm-*` | One wire protocol each |
| `mcp-tools`, `restapi-tools` | Outward connections for `mcp:` and `rest-api:` |
| `skills-server`, `cli-server` | Playbooks and the command registry |
| `job-server` | The home's Python functions as tools |
| `cabinet`, `media` | Documents and dumps; images, video, speech, transcription |
| `file-tunnel`, `wechat` | A public file URL; one WeChat login per agent |
| `agent-task`, `schedule` | Background turns; timers and the run ledger |
| `peer-agent`, `subagent` | The peer and local networks |
| `tui` | The terminal inbox |

A provider is a configuration row rather than a process, which is why one
process can serve many providers.

A `SKILL.md` is a manifest this system authors and reads. Its header MUST parse
to a mapping; `install`, `scan` and `skill_draft` run the same parser, so a draft
is either a `SKILL.md` `install` would accept or a refusal. `install` replaces a
skill whole; `edit` writes only the files it names; `remove` deletes the folder
and its entry.

A **job** is one Python function in `<data>/jobs/<name>.py`, exposed as
`job__<name>`; the docstring and typed signature are the definition, and
`job_set` loads the file before swapping it in, so a broken write restores the
previous version. **The job runtime enumerates no capabilities**: `call_tool`
reaches the hub's catalogue, `call` reaches any server in `servers:` by its key,
and `servers()` reads that table. The code runs in the jobs plugin with the
caller's identity, holds no credential, and is not sandboxed; the top-level tool
call is the approval point.

### 2.7 Key design decisions

| Decision | Choice | Consequence |
|---|---|---|
| Prompt and harness | Trust the model; keep the harness small | The harness adds a marked address and tool pairs, not instructions or hidden policy. |
| Context | Choose the context a turn runs on | One model call per turn decides what to keep and what to recall; the log is the permanent record. |
| Person work vs machine work | Conversation for model work; code for deterministic work | Scheduling is an expression code reads; delegation is a tool the model calls. |
| Plugin vs library | A process if it holds a credential, SDK, or connection | The agent loop never holds a provider key; files are opened in-process. |
| Configuration | One file, read per turn | An edit reaches the next turn rather than the next restart. |
| Names | Model-facing names are semantic, never protocol names | `peer_agent_*`, `subagent_*`, `turn_read`; no `a2a` in anything the model reads. |
| Failures | Raise or fail the turn; no silent fallbacks | A fallback heals or hard-fails, is registered, and is said. |

### 2.8 Quality attributes

| Attribute | Mechanism |
|---|---|
| **Isolation** | Credentials stay in the consuming process; state is keyed by conversation key. |
| **Recoverability** | Turns, the live-context list, the cabinet, the schedule ledger, and the loaded tool set are persisted; a restart restores the conversation and the model's list. |
| **Testability** | The loop is pure; servers run over in-memory transports in tests. |
| **Extensibility** | A plugin is a module plus one row in `PLUGINS`; an external tool is one configuration entry. |
| **Observability** | Startup reports each server as it lands; `slife2 status` probes each one; every turn, foreground tool call, and model call leaves one line with its duration, naming the conversation and the turn. |
| **Portability** | Tested on Windows, macOS, and Linux; the runtime has no POSIX-only dependency beyond a documented fallback. |

## 3. Plugin contract

### 3.1 Protocol and transport

A plugin is an MCP server over Streamable HTTP. It MUST answer the standard
JSON-RPC methods, including `tools/list` and `tools/call`; put its MCP name on
the wire; serve a probe tool whose presence identifies it; start through the
shared scaffold `serve_plugin`; accept the common flags (`--data-dir`, `--host`,
`--port`, `--path`); and resolve its own address from the configuration or the
default.

`libs/plugin/protocol.py` owns argument parsing, logging, configuration, address
resolution, daemon records, and startup, and installs the middleware every
server shares (§11.2, §11.7). A plugin supplies three hooks: what to build, what
to say about its own start, and what it cannot start without. Model backends use
the same scaffold through `plugins/llm/server_common.serve_backend`, a wrapper
rather than a second startup path.

A module under `plugins/` MUST NOT call `parse_serve_args`,
`configure_logging`, or `serve` directly. A test enforces this rule by name.

### 3.2 State keyed by conversation key

Every server holding per-conversation state MUST key it by the conversation key
`(home, agent, subagent)` (`audience.conversation_key`). The identity on the wire
is the conversation id `(agent, subagent)`; the home rides beside it in the same
`_meta` blob, because one process serves every home. No caller carries a session
handle on the server's behalf.

**Why a name, not a handle.** The MCP revision in use has no protocol-level
sessions: measured against the installed library, `session_id` was a fresh UUID
on every request. A server-minted handle was rejected because anything a caller
must keep can be lost, expired, or wrong after a restart; the conversation id is
a name the caller already has. Its price is guessability, which costs nothing
here: every server listens on loopback — a host beyond it is refused at load
(§11.1) — and the only other party is a process the user started.

Consequences:

- There is no "not found": state is created on first use and a key never goes
  stale.
- An idle sweep MAY drop state; the next call recreates it, invisibly.
- Isolation follows from the key: two conversations, and two homes' agents of one
  name, cannot reach each other's state.
- Every call is traceable, because every call carries the id even if the server
  holds no state.

For a model-facing call, the conversation id travels out of band in `_meta`,
not as an argument the model writes; a call that does not identify its
conversation is refused. A plugin's internal tools MAY take it as an argument
when the caller already knows the conversation (`remember`, `restore`,
`rebuild`). The home travels in the same blob: a shared server resolves a call's
files against the caller's home and keys state that outlives the call by that
home.

### 3.3 Audience and authorization

A tool MAY be called by the model only if it declares itself model-facing,
through the namespaced `_meta` key `slife2/audience` read from `tools/list`. The
rule is opt-in: internal tools need no mark and are withheld from the model; a
tool that forgets the mark is invisible, not dangerous; an `mcp:` entry needs no
mark, because the operator opted in by writing it.

Audience asks "may a model call this tool?"; identity asks "which conversation
is this call for?" A tool call runs for exactly one conversation; a model cannot
assert a different id, and the server reads it from `_meta`.

### 3.4 Family plugins

A family plugin owns one configuration section or folder and declares the rows
it holds to the hub. `libs/plugin/family.py` provides the shared half: the
connection families (`mcp:`, `rest-api:`) hold each entry's connection, declare
what it offers, route a call back to the holder, and build the management
tools' answers from `FamilyWords`; the document families (`skills:`, `cli:`)
hold the home's mirror of the section and declare its rows (`Documents`); the
job family shares the declaration envelope and page builders (`Homes`,
`listed`), and its rows are callable.

| Family | Section or folder | Holds |
|---|---|---|
| `mcp-tools` | `mcp:` | External MCP servers |
| `restapi-tools` | `rest-api:` | OpenAPI documents served by an MCP proxy |
| `cli-server` | `cli:` | Commands already installed |
| `skills-server` | `skills:` | Playbooks under `skills/` |
| `job-server` | `jobs/` | Code-defined tools under `jobs/` |

Other plugins — the cabinet, media, the terminal's inbox, and the rest — hold
no collection and serve their own tools; they are plugins, not families.

An entry has one `state` field, read from the file before the link is consulted.
A switched-off entry is `off`, not the `idle` of a connection nobody started; an
entry that answered with no tools is `ready` with zero tools. The vocabulary
matches the hub's `servers()` answer. A credential that did not resolve never
travels: a provider or media call refuses before use, while a tool server's
`env:`/`headers:` entry still holding its reference is **omitted** and warned
about, so the child's own missing-key policy decides — `tavily-mcp` runs
keyless, `serper` refuses in its own words. `libs/host/config.py:unresolved_reference`
is the single reader of "this value is still a reference".

### 3.5 Plugin registry

Every fact about a plugin that is a fact about the plugin is one row in
`libs/host/config.py:PLUGINS`:

| Field | Meaning |
|---|---|
| `key` | The plugin's key in `servers:` and in status output. |
| `module` | The module `python -m` runs. |
| `name` | The MCP name the server puts on the wire. |
| `probe` | The tool used to identify the server when it advertises no name. |
| `port` | The default listening port. |
| `api` | True for a model API backend; absent for a capability server. |

Everything else is derived: the start order, the default addresses, the
`*_SERVER_NAME` constants, the launcher's specs, and the probe a client uses. A
plugin reads its own name from its row; its `servers:` key is the same fact
under a second name, and one test imports each row's module and checks both.
Adding a plugin is writing the module and adding a row; a console script makes
it startable by name. The table is data, not imports: every module field is a
string, because importing a server to read its name would pull provider SDKs
into the terminal and the agent loop.

The two network rows share one implementation but have two entry modules: the
`peer-agent` row runs `slife2.plugins.a2a.server`, and the `subagent` row runs
`slife2.plugins.a2a.local_server`, a thin entry point that delegates to it. They
are separate processes because they are separate networks, and they have
different MCP names so a probe can tell them apart. Appendix B says where the
registry lives.

### 3.6 Identity verification

A client MUST prove which server it reached before believing anything the
server says. `identifies` compares the connected server's `server_info.name`
against the name the caller expected and, when the server reports no name, falls
back to the server's own probe tool. The build is advertised and recorded, not
compared: a daemon from another build answers to the right name, and whether
its tool surface fits the caller is a question only the call that uses it can
answer — two builds may be compatible, and a stop at the handshake would refuse
them. `status` reports the build each daemon wrote down. `open_server` is the
single place a client connects to one of our servers; it raises, naming what
could not be reached, rather than returning a client that will fail later.

### 3.7 Refusal vocabulary

`libs/plugin/audience.py` holds the refusal vocabulary.

- **`UNADDRESSED`** is a sentence template with two holes: what is missing and
  why the server cannot guess. It is used for a call that does not identify the
  conversation it is for.
- **A raised `ToolError`** is the refusal a model reads: one sentence saying
  what is wrong and what to do instead.
- **`protocol.agent_reader`** is the raising idiom for a tool server that needs
  the agent; **`protocol.conversation_reader`** is its pair-returning form for a
  tool that needs the conversation as well.
- **`audience.unaddressed`** is the sentence-returning idiom for a plugin's own
  internal API.
- **`protocol.agent_or_blank`** answers "the agent this call is for, or the
  empty string"; it is the shared question behind both raising idioms.

There is no `[refused]` marker. A refusal is the call's status, not a word in
its text.

### 3.8 Protocol revision compatibility

Four facts about the MCP revision in use bind every plugin and client:

1. **`ping` is gone.** A conforming server answers it with `Method not found`.
   Identity comes from the connection handshake: the client reads
   `server_info.name`, falling back to a `tools/list` membership check only when
   a server reports no name. A bare connection test would accept a different
   server on the same port.
2. **There are no sessions.** `Mcp-Session-Id` and `initialize` were removed
   from Streamable HTTP; a request carries its version and capabilities in
   `_meta` (§3.2).
3. **Change notifications are opt-in.** `notifications/tools/list_changed`
   reaches only a client that has opened `subscriptions/listen`. The installed
   FastMCP exposes no call that opens one and advertises `tools.listChanged:
   false`, so the hub re-reads an aged listing rather than waiting.
4. **A result that is not pushed must ride its request.** With no push channel,
   a queued caller waits on its own call, and the inbox is capped because that
   wait has a client-side timeout.

**Message size.** MCP `tools/call` is request/response, and the client SDK
refuses an SSE event over 1 MiB while the server SDK refuses a request body
over 4 MiB. Our own hops have neither ceiling — a conversation and its attached
images travel inline, and a daemon this system started is bounded by the
model's context and the attachment cap. The gateway to an external MCP server
keeps the SDK's ceilings: an external server is a peer, and they protect a
process from a peer making it buffer an unbounded message.

## 4. Machine and agent lifecycle

### 4.1 Configuration as the system description

One file, `slife2.yaml`, inside a home, is read by every process. It is the only
place that says what exists: which providers and models, which servers and
addresses, which tool families and entries, where embeddings come from, which
model is the default, and which networks and tunnels are enabled. The launcher's
server list is derived from it: `Config.plugins()` answers the wire protocols
some provider actually uses, then the local servers. A protocol no provider
speaks is not started; a provider is a row in a backend's configuration, not a
process.

A missing file is fatal, and so is a malformed one. There is no default machine:
a home without `slife2.yaml` stops the start with a one-line message naming the
directory searched, and a file that exists and cannot be read stops it naming
the file and the mistake. The file must name at least one provider and at least
one embedding provider: there is no built-in provider or embedder.

`AGENTS.md` is the second required document: the model's guide to its tools,
included in the system prompt and read from the home at that moment, so a
replacement reaches the next conversation. **It is the system's deliverable, not
the operator's document**: a person may read it, an edit is not supported, and a
reinstall replaces it, with no `AGENTS-default.md` beside it. A home without one
is refused where the prompt is rendered. A person's addition goes in a template
named by `agent.system_prompt`. The installer takes its requirements from the
shipped `slife2.yaml`, never the live one, which may lag a release and owes a
hand merge; an upgrade must not wait on it.

`libs/host/configfile.py` is the single writer for edits made by plugins: a
document edit under a cross-process lock, comments preserved, result handed to
the startup loader; an entry the loader refuses is restored. `model_config.py`
and `tool_config.py` are the canonical objects for a `providers:` entry and for
an `mcp:`, `rest-api:` or `cli:` entry: the loader validates YAML into them and
each family's `*_get`/`*_set` uses the same object, so the file is an artifact,
not the schema. An unknown key is refused at load, and `*_set` requires every
field. The other sections are read through one loader (`libs/host/section.py`),
which declares each key as a `Field` carrying the reader, the bound and the
missing-key sentence, and refuses an unknown key from that same list.

`--a2a-broker HOST:PORT` overrides `peer_agent.broker` for one run, through the
environment so a server started by this command reads the same address. It
overrides the address only; `org`/`unit` remain the file's, and it cannot enable
a network whose section is absent — the command that owns the flag refuses it
against its own config, while the loader leaves the override alone because a
shared daemon loads other homes' configs. If the A2A server is already running,
the flag is refused rather than silently ignored.

### 4.2 Shared servers and launcher

`libs/host/launcher.py` starts the servers a configuration needs and attaches to
those already running. Servers are daemons: a client exiting never stops one,
but the last client out does — and it stops the *machine's* records, not only
the exiting config's endpoints, because a backend another configuration started
is still a daemon with nobody left to stop it. `others_running` counts by
process liveness, so a killed client cannot keep the stack alive. A take-down
that has begun runs to the end: a repeated `Ctrl+C` is swallowed rather than
leaving the stack half stopped.

The lifecycle rules are:

1. **A probe decides whether a server is alive, never a pid.** Pids are
   recycled; records also carry a process start token.
2. **A pid that exists is not necessarily a running process.** A zombie is
   detected by reading its state; a state that cannot be read is not a zombie.
3. **Ready means the expected server answers as itself.** A port that accepts a
   connection is not enough. The probe's endpoint and the child's bind are one
   `ServerSpec` — host, port and HTTP path — so an edit during the start-up
   window cannot leave the probe waiting on an address the child did not take.
4. **Startup is a wave order.** `NEEDS` states which servers must precede which;
   everything in a wave with no unmet dependency starts at once; a server whose
   dependency failed is not attempted and is reported with what it waited for.

On Windows, a child must escape its parent's job with
`CREATE_BREAKAWAY_FROM_JOB`, or a daemon dies with a kill-on-close job owned by
a CI runner or an IDE task host. Startup is reported as it happens: `ensure_all`
is a generator, and the CLI prints each server as it lands.

### 4.3 Agent identity

`claim_agent` holds the exclusive right to run under an agent name for the life
of the client. The claim is taken with a kernel lock the operating system
releases if the holder dies; there is no stale-lock protocol.

- The name alone identifies the claim. It is machine-wide, not per home.
- A second live instance under the same name is refused by name.
- The claim does not partition servers: servers remain shared with every other
  agent.
- `--headless` is recorded in the claim so the agent server can render an
  appropriate system prompt.
- Which data directory a name used previously is not remembered.
- A sanitized name becomes a filename; a name that reduces to nothing is
  refused.

### 4.4 `status`, `down`, and `logs`

`slife2 status` reports each server as `running` (with its pid and start time),
`running (external)`, `port held by something else`, or `not running`, and every
A2A broker the configuration names — `peer_agent:` and `subagent.network:`, one
row per address. `--json` gives the same rows as `servers` and `brokers`, each
row's log path, and `warnings` for a configuration fact that changed what is in
force (a guessed WSL networking mode, an unreadable `SLIFE2_LOG_LEVEL`). It
exits 0 only when every server and every broker are usable, and 2 otherwise.

**A record that exists and cannot be read is damage, not `not running`.** The
row fails, naming the record and the repair; `down` refuses to signal past it,
and `live_clients` fails rather than undercounting a client that may still be
using the servers. A *missing* record remains the ordinary first start.

`slife2 down` stops the servers this configuration names and that this build can
prove it owns, and a broker slife2 started. It refuses while another live client
uses the servers and returns 2; `--force` stops them anyway and interrupts that
client's in-flight turns.

`slife2 logs` lists the logs, prints one by name, or prints all under headers;
the name is `status`'s, a server's MCP name, or a model backend's protocol key.
Logs are plain text, appended across restarts and rotated to `<log>.1` at the
next start. The level is INFO unless `SLIFE2_LOG_LEVEL` raises it. An external
stdio child writes its own file (`mcp-<name>-<digest>.log`), so its output is
attributable rather than interleaved (§11.7).

### 4.5 Subagents

A subagent is an agent that belongs to another agent. A subagent:

- has the maker's tool set, except that `subagent_create` and `subagent_remove`
  refuse a subagent caller (§4.5.3);
- has its own role-specific system prompt (`subagent.j2`), its own
  `subagent.yaml`, and its own stored conversation, cabinet, and media
  directory;
- has no process, no window, and no claim on an agent name; its conversation MAY
  be watched by a terminal (§4.5.6), and watching is a read rather than a
  window;
- has no independent external identity: no WeChat login and no peer-agent
  membership; it is reached only on the local subagent network;
- shares the maker's schedule ledger, because scheduling stays with the agent;
- runs when something sends it a message;
- is addressed by the second half of a conversation id: `(maker, name)`.

**Why a loop, not a process.** A headless instance costs about 100 MB private
and the fixed machine stack is about 3 GB because servers are shared; the loop
table already exists and is keyed by `(home, agent, subagent)`. The cost of a
subagent plugin would be the transport to it — a new protocol hop for every
message — while a subagent's identity is already the conversation id.

**The record is the membership**: a message addressed to a name with no
`subagent.yaml` — never made, or removed — is refused rather than starting a
conversation that would recreate the directory with no record and block the name
against both tools. After creation it is reached over the local network under
`subagent_*` names: the direct seven (`subagent_send_message`,
`subagent_task_response`, `subagent_continue_task`, `subagent_cancel_task`,
`subagent_list`, `subagent_task_list`, `subagent_broadcast`) and the six group
tools.

A message reaches a subagent the same way one reaches an agent: it is submitted
to the shared agent server's inbox, and a message that arrives while a turn is
running is folded into that turn at its next iteration boundary. The harness
delivers; whether a member answers is the model's own `group_send_message` call.

#### 4.5.1 Directory and record

Every subagent of `jack` lives under `agents/jack/subagents/<name>/`, with its
conversation (`turns.db`), its record (`subagent.yaml`), and its own `cabinet/`
and `media/` there. A subagent has no external identity of its own.

The container level is deliberate: `agents/jack/cabinet`, `agents/jack/media`,
and `agents/jack/turns.db` belong to the maker, so a subagent named `cabinet`
would otherwise collide with the maker's own directory. `subagent.yaml` records
the model, the role, and when it was made. **A record this build cannot read is
damage, not the defaults**: the list marks the entry `unreadable` with the
record's path, and a transcript or a run on that subagent fails naming it. The
file is deleted with the directory. The configuration file holds only
machine-wide facts: how many subagents a maker may have, and where the local
network meets.

#### 4.5.2 Role

- **`role`** is a word included in the subagent's prompt — the shipped
  `subagent.j2`, not the operator's `agent.system_prompt`, because a subagent
  must be told what it is, whose it is, and where its work comes from.

#### 4.5.3 One level only

A subagent MUST NOT create or remove a subagent. `subagent_create` and
`subagent_remove` are refused when the caller is itself a subagent: the local
network holds a place for the maker's own subagents and nothing holds a place
for a level below them.

#### 4.5.4 Removal

`subagent_remove` performs, in order: tell the local network to release the
subagent's place; stop the subagent's loop; delete its directory.

**The claim is the conversation table's, taken before the first step**: a
message or a second `subagent_create` that names the subagent while its
directory is being deleted is refused by the table, never raced against the
delete.

Releasing the place also withdraws every task around the member, in both
directions. Each open send *to* it is recorded `cancelled`; each task it *sent*
lives in the receiver's inbound table, so it is settled `cancelled` there and
the receiver's model is told. A `pending` row would name work whose member no
longer exists.

If the network notification cannot be made, the subagent is left unchanged.
Removal is a forced cleanup whether the member is idle or mid-turn. Releasing
the place is what **interrupts a turn**: a subagent has no window, so its turn
is the member call that process made, and dropping the session cancels it; a
turn that interrupt does not reach is interrupted by cancelling its own handler.
Removing an agent removes its subagents, because they are directories underneath
it.

#### 4.5.5 Local network

The local network is configured under `subagent:`.

- It needs a `broker`. A `peer_agent:` section with no broker uses the same
  address, because a machine meets both networks somewhere.
- The namespace is `{org}/{unit}` and the unit is always the agent's name. The
  code sets it; a file that writes `unit:` is refused, because a unit that was
  not the agent's name would merge two agents' subagents into one MQTT identity.
- `org` defaults to `subagent` and MAY be overridden to group meshes.
- The number of subagents a maker may have is `subagent.max_running` (default
  32), a bound on runaway growth.

A subagent's place on the network is held by the local network's process. A
message to a subagent becomes a turn in the subagent's own conversation, wearing
`Agent(<name>)>` — the sender is the maker — while an answer from a sibling
wears `Subagent(<name>)>`. The subagent's answer is drawn on the maker's terminal
as `Subagent(<name>)>` and reaches the maker as a message in the maker's own
conversation. A turn that ends in prose is not a reply: the model's own
`subagent_send_message` or `task_response` call is the only way out. The first
roster read after a start connects the caller's place before reporting; a
refused connection is reported as the tool's `error`, not raised.

**A withdrawal is a message, on both networks.** `subagent_cancel_task` settles
the task as cancelled where the withdrawal is accepted — the sender is told
`cancelled`, and a later `task_response` for the id is refused — and hands the
member's model the same `cancel_task` marker a peer's model gets: the task was
withdrawn, it may stop working on it, and it owes no response. Nothing cancels
the member's turn. A turn is not a task: one turn can fold in several tasks'
messages, a task can outlive the turn that started it, and only the model knows
which of them it is working on.

#### 4.5.6 Watching a conversation

A terminal can watch a subagent's conversation without becoming its window. The
agent server exposes two read-only management tools:

- `subagents(agent)` lists the maker's subagent directories with the model and
  role read from each `subagent.yaml`; it reads the filesystem, not the roster,
  because the roster connects the mesh and wakes members.
- `watch(agent, subagent)` is a held-open call. Its first progress frame is a
  snapshot of the stored turns — `transcript`'s own read, so history ends at the
  same place for a watcher and a restarted window. A turn in flight is replayed
  from a `TurnStarted` frame followed by the events already produced; live turn
  events follow. A turn whose record is still being written is waited out
  before that decision. A cancelled call is the detach. When the subagent is
  removed, the last frame is `Closed` and the call returns.

Watching is a read: it starts no turn, chooses no context, spends no model call,
and never answers an approval request. The watched turn remains unattended
(`watched=false`). A display that falls behind loses frames; the turn never waits
for it.

**An agent task is watched through the same vocabulary** (§9.3), from the task
server's own held-open `watch(name)` tool. A task stores no turns, so its first
frame is an empty `Snapshot` carrying the agent's plan; `TaskBrief` follows with
the description, then the one turn's events, then `Closed` when it stops. A task
that has already stopped is answered with the brief, its final status and the
sentence its ending carried; a name the registry no longer holds is a refusal.
Both servers keep their taps and watchers in `libs/turn/watchhub.py`, so the
join-mid-turn race is one implementation.

`Ctrl+O` offers the agent's subagents and its **running** agent tasks in one
picker; a stopped task has no events left to replay. Watching a task starts no
turn and does not make it `finished` — the answer still reaches the conversation
by hand-over.

## 5. Turn execution

### 5.1 Loop contract

A turn is: send the conversation to a model; stream the response; run the tools
it requested; append the results; repeat until the model answers without
requesting a tool. `libs/turn/loop.py` implements the algorithm;
`plugins/agent/` owns the conversations it runs in — `core.py` the loops table
and the turn, `state.py` the conversation and its turns, `recording.py` the
write, `model_tools.py` the model config, and `membership.py` the subagents.

`AgentLoop` is built with a **backend** with one streaming method and a
**registry** of tools, refreshed by a callback before every model call.
`AgentLoop.run_turn(messages, user, observer, auto=…)` receives the messages to
run on, the incoming user message, an **observer** that may drop, delay, or
coalesce display events without affecting the conversation, and optionally the
harness's `auto` callback, which names a tool the harness wants called mid-turn.

The loop is a pure function over a message list. It owns no conversation,
socket, or clock. The iteration cap is `agent.max_iterations` and the compaction
threshold `agent.compact_at`, both read when the conversation starts — the same
moment its model and system prompt are read — so an edit reaches the next
conversation, not the one already capped by the old value. `max_iterations: 0`
or an absent key means no cap: a number nobody wrote is a bound nobody chose. A
turn that reaches a stated cap ends normally with
`stop_reason: "max_iterations"`, not with an exception.

A tool that fails is a result, not an exception. `ToolRegistry.execute` answers
`(text, ok)` and never raises: a peer's refusal, a dead transport and a tool that
threw all come back as `ok=False` with text the model can act on. What ends the
turn is a failure outside that contract — a tool list that cannot be refreshed
before a model call, or a model call that failed.

**A message with no readable role is damage, not a guess.** The neutral message
reader refuses it (`UnreadableMessage`) and the turn fails: the four roles are
the contract every provider accepts, and inferring one would put words in the
person's mouth or make the model the author of something it did not say. A
stored body with the same damage fails at the row instead (§8.1).

**A turn's compaction is a view, not an edit.** The loop is handed a `present`
callback with the list it advances; the model is sent `present(messages)` and
the record is `messages`. `_turn_compact` (§5.3) is called by the harness when
the last call's `total_tokens` crosses `agent.compact_at` of the conversation's
**prompt budget** — the model's window minus the completion request it carries
(`max_tokens`), because a provider counts both against one window — or by the
model itself. A compaction that dropped something leaves the automated pass
live; one that kept everything or failed stops it for that turn
(`Loop.compact_hold`), and a later successful call resumes it. The turn's row,
`covered` and the store never see the view; the next turn starts from the full
list (§5.5).

**A final model call that answers with no text and no tool call is a failed
turn.** A provider can legitimately stop without saying anything, but that
result has the same shape as a call that produced nothing, and the turn's
product is its answer. An empty answer — whitespace alone is none — is a
failure, not a verdict (Appendix C). The loop decides this, because it is the
one place that knows the call ended the turn.

### 5.2 Harness additions

`libs/harness/` contains what the harness — the system, as distinct from the
model or the person — adds to a conversation. Four mechanisms share one rule:
what the harness adds is a marked address, never a claim of its own.

#### 5.2.1 Markers

`markers.py` defines `[Name:{…}]` as a prefix on a delivered message. A marker is
content: it is stored with the message and travels into the transcript, and it
is stripped for display by the reader that knows the vocabulary. A producer and
its reader share one spelling, from `libs/plugin/vocabulary.py`. Capitals mark a
harness annotation, never the conversation's own words.

#### 5.2.2 Hand-over

`handover.py` provides `hand_over`, which puts a message into an agent's
conversation through the window showing it; `note_to_window`, which draws a
harness line without creating a turn; and `submit_to`, which classifies a
delivery answer as **written**, **refused**, or **unknown**:

- a **refusal** proves nothing was written and may be retried;
- an **unknown** may already have landed and must not be repeated;
- a message that cannot be handed over is a `DeliveryFailed` and is reported as
  an error, because those words were the only answer available to the sender.

#### 5.2.3 Background calls

`_async: true` on a tool call runs it in the background and returns a task id to
the model. The result is delivered back into the conversation as a message. The
call is over when its status is final; delivery is a separate fact
(`Task.delivered`).

**A task id is not a capability.** Each task records the conversation it was
scheduled in, and `check_async`/`cancel_async` answer only inside it: an id from
another conversation is refused exactly as an unknown one, so the refusal cannot
confirm that the id exists. A task scheduled outside any conversation — a test,
a worker — is readable by any caller.

#### 5.2.4 Dumps

A tool result too large for the conversation is stored whole as a `dump` row in
the cabinet, keyed by the tool call id. The conversation carries a fixed
**head** of it within `tool_result.bytes`; the marker in front of the head names
the call, the file's byte size, and its path relative to the data directory, so
moving the home does not invalidate the marker. The size is one `stat` of the
file the cabinet wrote; the marker counts no lines and no characters.

**The bound is bytes, not tokens.** It is a size cap on one result, not a token
accounting of the conversation; the recall's `context.ceiling` is the token
guard. Bytes cost one expression to measure and track text volume across scripts
better than characters do; the readers' addresses stay characters.

The head is the largest prefix of the tool's own text that fits, so nothing is
rewritten. `dump_read` reads it by line or by character and `dump_grep` finds
text in it, answering both addresses (§8.3).

**Why not a model call.** A model asked which lines matter is a second model
call for every oversized result, carrying the result and the conversation; its
input can itself be larger than the context it was meant to protect. A fixed
head costs one measurement and cannot overflow.

**Why 32 KiB.** On the 2026-10 turn log (349 results), the largest result was
5,284 tokens — roughly 20 KB — so a 32 KiB window shortened none, while a 16 KiB
window would have shortened some. The case the mechanism exists for is a tool
bound to be long, such as a 10,000-word essay.

A non-text result gets the same treatment: the head tells a model what it is,
and the readers are bounded.

The harness reaches the cabinet directly for `dump_save`, because that tool
carries no audience mark; the model's routes back are `dump_read` and
`dump_grep`.

**An agent task writes no dumps**, and its messages are not recorded. Its loop
gets a `Compress` that cuts an over-budget result to the same byte bound with a
marker that promises no file (`dumps.cut`); the bound holds, the tail is gone,
and the model can call the tool again. A task opens the cabinet for one thing
only: resolving an `[Image:…]` key its own `attach_image` was handed.

#### 5.2.5 Fallbacks

No step in this path has a fallback registered in Appendix C. Keeping the
output, writing it, and building the head MUST succeed, and each fails the turn
loudly if it does not: a conversation carrying a marker whose file is not there
has given up the bound and broken the address while looking like success.

#### 5.2.6 The plan

A conversation has one plan: a list the model writes whole through `plan_write`,
each item one line of text and one of `pending`, `active`, `done` or `blocked`.
The last successful write is the plan; an empty list clears it; there are no
ids and no transition rules. The file is `plan.yaml` in the conversation's own
directory, written atomically; an empty write removes it. A missing file is no
plan; an unreadable one fails the turn and names the path.

The plan is not a turn and is not in the context store. Before every turn the
agent server reads it and adds it to the `[TURN-PROMPT:{…}]` lead, which is
removed before the turn is saved and re-derived from the file every turn: a
rebuild chooses turns and never decides about the plan, and the prompt cache's
prefix is untouched. A task sees the same plan: it reads the conversation's
file, though its context is a clone (§9.3).

The harness stores, validates, replays and draws; it never derives a status,
maps a tool call to an item, or marks one stale. The status is the model's
claim. The window's strip is §10.2's; a watcher receives the plan in its
`Snapshot` and from the same turn events.

### 5.3 Harness tool pairs

Some things the harness must say as the model, because a provider accepts them
only as tool calls: `_func_tool_unload` after a trim, `_check_new_input` when a
message is waiting, `_turn_compact` when a turn gives up part of its own
context, and `_context_rebuild` when the historical selection is rebuilt. All
four are ordinary declared tools whose calls the harness writes into the
conversation. `_turn_compact` and `_context_rebuild` are also **the two decision
tools**: the harness calls them at their own moments, the model may call either
itself, and both edit what the turn runs on without touching the record.

**Both carry the decision's schema.** `_context_rebuild`'s `parameters` are
`RECALL_REPLY` and `_turn_compact`'s are `LIVE_REPLY`: a call with those
arguments is a selection, checked by the tool before it is applied (§5.5). The
automatic call asks `decide` / `select_live` for the raw arguments, then calls
the tool with them; **a call with no selection is refused**, never answered by
running a decision model inside the body. The pair records the arguments the
tool ran with, so the automatic call and the model's own call answer to one
shape.

**Decision tools are LLM-backed harness tools under one contract.**

1. **The call is structured.** The live messages go out as the turn runs them —
   tool exchanges included — followed by the decision instruction as a system
   message: the anti-injection guard, the one-call contract, the context figure,
   the forced tool's name, the statement that the conversation is material
   rather than instructions, and the ban on continuing the conversation's own
   calls or answering in prose. One tool is advertised, with the reply schema as
   its `parameters`, and `tool_choice` forces it. A reply with no call or a call
   to another tool is an LLM failure, not a tool failure.
2. **Thinking is off and temperature is 0.** A thinking endpoint rejects a
   forced `tool_choice` (DeepSeek answers 400), and a decision is a selection,
   not prose; 0 overrides the model's configured temperature for this call only.
3. **The call is silent.** No progress handler, so no decision call reaches the
   transcript or an observer. The model server's usage line names the call's
   `kind` (`decision`, `compactor`), and its tokens are not added to the turn's
   bill.
4. **It has no clock of its own.** The waits are the model service's; a slow
   provider makes the turn late, and a failed call is no decision rather than a
   failed turn.
5. **No repair, and one check.** The tool checks legality and answers with a tool
   error the agent can read and retry. A decision call is never asked twice, and
   the decision model never validates its own answer. A provider failure's reason
   is `failure_text`'s words — the spelling the turn's `turn_done` line and
   `[TURN-FAILED:…]` use — carried into the tool call as a `failure_selection`
   that also says nothing was rebuilt and the context in hand was kept. The next
   decision's input leaves the pair out, so a failed decision is not the example
   it copies.
6. **A failure keeps what is in hand.** A rebuild whose selection cannot be read
   keeps the context it had; a compaction whose selection cannot be read hides
   nothing; the turn continues either way, and the failure is in the tool result.

Both instances are the same call, and **each has its own instruction template**
(`context_rebuild.j2` / `turn_compact.j2`): the forced call's overlay — the
guard, the one-call contract and the context figure. The task, the decision
order and every field rule are the schema's, because the model's own call never
sees a template. The guard and the one-call rules are repeated per template on
purpose, because calibrating one decision's wording must not move the other.
**The forced call names the real tool** — the same name, description and schema
the model calls in a turn — and its reply is the tool's arguments; the harness
calls the tool with them. The decision call never runs the tool itself, and
there is no private reply name. The three wire adapters share `forced_tool` — a
forced choice naming a tool the call does not send is refused — and
`call_options`. The model chooses; the harness applies. No other harness tool
calls a model.

`_check_new_input` is deliberately not a user message. The loop re-reads the list
at every iteration, so a user message would be read as the turn's own input; in
the middle of an unanswered tool exchange it is a 400 from several providers.

Rules:

1. **A pair names a declared tool.** The Responses and Messages backends reject
   a tool call in history that is not in the request's tool list. The model may
   therefore call it too; a model calling `_check_new_input` is the boundary's
   own move made early.
2. **The id joining a pair MUST be acceptable to every provider as a call id.**
   A provider that refuses the string refuses the request, including replays.
3. **A pair is written with nothing awaited between its halves.** An interruption
   in the gap leaves an assistant message whose call is unanswered, which every
   provider rejects.
4. **A pair is written only when something moved.**

A model's own `_check_new_input` call is the same cut-in, not a second account of
one. The channel the message arrived on rides its tool message, and the loop
emits the same `MessageCutIn`, so the window that submitted it does not draw the
line again.

**Every pair the machinery writes is visible to the person.** A harness pair's
answer is a note in the transcript — the compaction's count, the trim's names
that left, and a cut-in's message — drawn live and rebuilt from the same text.
The pair's call id (`HARNESS_CALL_PREFIX`) is what tells a machinery-written
call from a model's own; a model's own call to any harness tool is an ordinary
tool panel. The one finish that adds no line is the machinery's own
`_check_new_input`: its answer is the delivered message, whose line is
`MessageCutIn`. The screen decides by the pair's id class, not by a case per
tool, so a harness tool added later is visible without a drawing change.

### 5.4 Channels and delivery

A message that arrives from anywhere other than this terminal becomes a turn, and
the turn records where it came in.

| Channel | Source | Drawn as | Marker |
|---|---|---|---|
| `tui` | The person, in this window | `You>` | none |
| `wechat` | A WeChat login | `Wechat>` | `[Wechat:{…}]` |
| `peer_agent:<peer>` | A peer agent | `PeerAgent(<peer>)>` | `[PeerAgent:{…}]` |
| `agent:<name>` | The agent, in one of its subagents' conversations | `Agent(<name>)>` | `[Agent:{…}]` |
| `subagent:<name>` | One of this agent's subagents | `Subagent(<name>)>` | `[Subagent:{…}]` |
| `agent-task:<name>` | A background turn's answer | `AsyncTask(<name>)>` | `[AgentTaskResult:{…}]` |
| `async-tool:<name>` | A background tool call's result | `AsyncTool(<name>)>` | `[AgentToolResult:{…}]` |

The channel is recorded, so the marker belongs to the channel, the displayed line
wears the channel's name, and a rebuilt conversation still says where each
message came from. The channel is an address (`agent-task:research`); the
displayed line says who answered (`AsyncTask(research)>`). The prefix is decided
in one function (`tui/widgets.py:channel_prefix`), used by both the live prompt
and the rebuild.

The local network has **two kinds of speaker**, so it has two channel words. A
subagent's conversation is entered by the agent that made it and by a sibling;
`agent:<name>` says the first and `subagent:<name>` the second. The sender's kind
is the word, not the receiver's.

A channel that delivers a message owes the conversation a **whole turn**, not a
prompt. A channel that cannot reach a conversation says so and stops; it does not
try a second route. An injected message can jump the queue: what the person
types, what another channel hands to that window, and what a headless window's
input carries are all submitted to the conversation the moment the words arrive.
The window draws the message inside the running turn as a harness note, without
closing the turn the answer is still being streamed in. There is one queue: the
agent server's inbox.

**A message folded into a turn is delivered by it once its words are written.**
If that turn then ends without an answer — cancelled or failed — the cut-in pair
is still in its record, so its caller is answered as injected with no text.
Running it again would put the same prompt in the conversation twice and have
the model answer it twice. A take whose words were never written — the fit
failed, the interruption landed before the append, or the call was cut before it
answered so the batch wrote `INTERRUPTED` — is not a delivery: its caller still
owes itself a turn and runs it. The commit takes a count for exactly that
reason; it marks only the takes whose words are in the conversation. A refused
record, whose messages are rolled back, leaves the message owing itself a turn
the same way.

### 5.5 Context decision and memory selection

The agent server owns one loop per conversation key, created on first use. The
model, system prompt and iteration cap are **properties of that loop**, read when
it is created; a turn snapshots them when it starts, so `model_switch` moves the
conversation for its next turn, never the one in flight. Everything else — the
tool list, the decision's parameters, the timeout — is re-read per call.

Each turn, the context plugin answers one question: what will this turn run on?

1. **`restore`**, when a conversation starts: it replays the stored live-context
   list.
2. **`_context_rebuild`**, at the first boundary of each turn — the harness asks
   `decide` for the selection and calls the tool with it — and whenever the
   model calls it with `keep`/`recall` of its own. The decision reads the
   conversation, the turn's own message already in it, and answers two required
   halves for the current work: `keep` (`keep`, `clear`, or exactly `turn_ids`)
   and `recall` (the query for earlier turns). Their union is the context. The
   turn running has no id and is carried verbatim. The decision sees the last
   call's size against the conversation's window through `context_usage`.

A follow-up usually names its subject through the turns above it, so the whole
conversation is the input; there is no separate input argument. A finished
subject is not kept for a new one; a follow-up keeps the turns it continues;
`recall: {}` says nothing outside them is needed. The no-change answer —
`"keep"` with `{}` — rebuilds nothing and is right only when every turn in hand
serves the current work.

The automatic path is a forced `_context_rebuild` call under §5.3's contract.
The conversation is the input as the turn runs it, **minus the harness failure
pair**, whose non-selection arguments must not be copied (`_decision_messages`).
A model whose `compat.thinking: omit` refuses the thinking field is not
overridden; a forced choice the endpoint still refuses is an LLM failure carried
as a failure selection. A model left free in a tool-heavy conversation continues
it instead of deciding; the instruction forbids that explicitly.

Rules:

- **A decision that did not happen is a tool failure, not a hidden one.** The
  provider's failure or a reply that is not the forced call is carried into the
  tool call as a failure selection, and the tool's error result names it; the
  log carries `failure_text`. **The pair is not an example**: the next decision's
  input drops every pair whose arguments are not a selection. A recall nobody
  asked for is not a recall that found nothing.
- **The reports are counts; the ids are the pair's arguments.** The rebuild
  answers `kept K/M, recalled R/O, token usage ~N (P%)`, with `fitted` when the
  prompt budget cut the oldest turns; the compaction reports how many messages
  left. A selection that cannot be applied is the tool's error.
- **Kept and recalled turns are joined by id, in time order, never reconciled.**
  `normalise_ids` collapses duplicates to their first position. **A keep-list
  naming no turn in hand is not a clear**: the explicit clear is
  `keep.action: "clear"` (an empty list for the internal parser;
  `keep.action: "ids"` requires a non-empty `turn_ids`). A selection with no
  known turn to keep is the tool's error; the context stays.
- **A question has two spellings.** `sentences` are read by meaning, one
  question per entry; `query` is the words the keyword leg requires and the
  meaning fallback. A later sentence can only add a turn or raise one. The
  decision's `sentences` are its questions for the log, so there is no separate
  input; a keyword-only recall keeps its query as the one meaning question.
- **A recall is asked for what is missing.** Every turn in hand is excluded from
  its candidates, because a turn the decision dropped from the keep-list was
  dropped deliberately. **The exclusion is part of the KNN's own restriction**,
  through a per-call temp table, so `k` stays the page size; widening `k` by the
  excluded turns grew past `sqlite-vec`'s 4096-candidate ceiling on a large live
  context. A catalogue search's filters are pushed into its KNN the same way.
- **The recall is hybrid, and the ranker is the last word.** The legs are fused
  by weighted RRF (k=60, `keyword_weight` on the sparse leg), and
  `min_similarity` drops a measured candidate below the embedding model's noise
  floor. **A window is applied inside each leg**, the semantic KNN included, so
  `since`/`until` choose what the page may contain. **No term is cut for being
  common**: a common word can still be the exact string a query is about.
- **A cross-encoder reranks the whole fused pool** when the embeddings provider
  names a `rerank_model`: each leg retrieves `recall_page_limit` candidates (80
  by default), fusion returns their union uncut, and the floors and the token
  budget decide membership. `rerank_min_score` is the ranker's own floor. A
  missing or failing reranker keeps the fused order; a period recall, which has
  no question, keeps its time order. The catalogue and cabinet searches keep
  their own page order; their rerank is deferred (§14.2).
- **The live-context list's order is authoritative.** Reads replay it as written
  and never re-sort or re-slice it.
- **`send_message` answers with every key present**, including `refused` where
  nothing could be refused. A turn is an object, not a call, because a message
  can arrive mid-turn and must wait rather than displace the turn in flight.
- **A rebuilt turn carries a footnote** naming its id, channel, and span
  (`turn_note`). The newest turn is annotated after saving with the same
  timestamps and channel, so live and rebuilt spelling are byte-identical.
- **Every addressable message carries a live id**: `{turn_id}.{index}`
  historical, `cur.{index}` for the running turn. The view writes it as
  `[MESSAGE:{"id": …}]` on the messages the model did not write — the person's
  and the tool's — and keeps it on the others for the exchange rule. It is never
  stored; a turn's ids enter the live list when its id arrives, with the same
  spelling a rebuild derives from the row. The selector's view annotates a tool
  exchange's assistant message too; the model's own view does not, so the schema
  tells it that message's id is the one immediately before the first result's. A
  compaction's ids are the ones `_turn_compact` accepts.
- **The turn running carries a prompt, not a footnote.** `[TURN-PROMPT:{…}]`
  is added before the turn and removed before it is saved (`without_lead`), so
  it never reaches the row or the index. Its payload is a mapping — today `now`
  and `plan` — because a per-turn fact can grow. The agent, its subagents and an
  agent task all carry it. **It carries no context figure**: the last measured
  call is the previous turn's, and a rebuild can change the list; the turn's own
  estimate is `_context_rebuild`'s answer.
- **The turn-to-messages builder is one function** (`messages_from_turns`); two
  builders would drift into two conversations.

**The turn's own compaction is a view, not an edit.** `_turn_compact` hides the
live ids this turn no longer needs from the list the rest of the turn is sent
(`libs/turn/compact.py`). A call with `compact` is a selection checked against
the view; a call with no `compact` is refused, and the harness asks `select_live`
first under §5.3's contract. The annotation is on the person's and the tool's
messages alone: an assistant message's content is what the model said, and a
marker there is a sentence it never wrote that it copies into the next answer (a
live session showed the `cur.N` chain growing once per call). **Nothing is
removed**: the live list, the turn's row, `covered` and the store's live-context
list are the conversation, and the next turn starts from the full list. The
system message and the turn's opening message are the fixed two; the annotated
messages — and, for a tool exchange, its assistant message — are the model's to
drop. The exchange rule is the schema's, and the harness **keeps a half-named
exchange and applies the rest** (`split_exchanges`), so one unnamed member does
not discard the ids the model did name. **A selection naming an id the view does
not offer is refused whole** rather than filtered; an id an earlier compaction
already hid is already applied. A decision that fails or cannot be read compacts
nothing and the turn continues (Appendix C); one that keeps everything, fails, or
is refused stops the automated pass for the rest of the turn. The model can
still call `_turn_compact`, and a later successful call resumes the pass.

**The rebuild is the durable edit; the compaction is the transient view.** A
`_context_rebuild` call with `keep`/`recall` is a selection, applied as it came;
a call missing either half is refused. Its pair carries the decision's arguments,
and its answer reports the counts and an estimate of the context the turn runs on
(`estimate_turn_tokens` plus `context_usage`), never a provider measurement. A
rebuild resets the compaction's view (`hidden` is cleared). A decision whose
answer cannot be used leaves the turn on the context it had; a model's own
selection that cannot be read is a tool error it corrects.

**Why word-shaped queries.** On the live turn log, a sentence-shaped keyword
query fired on 0 of 16 queries; a word-shaped query fired on 10 of 15 and found
the target on 9. The meaning leg found the target in 32 of 32 recall queries,
first in 25. The sentence is therefore a shape the model may use, not one it
must use.

The context parameters are in `context:`:

| Key | Meaning |
|---|---|
| `ceiling` | Fraction of the **prompt budget** (`context_window - max_tokens`) the live context is topped up to. It is a hard bound: a kept selection heavier than it is cut from its oldest end, and a recall spends what the rest leaves below it. |
| `recall_page_limit` | Candidates each leg retrieves; fusion returns their union uncut, and the floors and the token budget decide membership from there. |
| `min_similarity` | Similarity a measured candidate must reach; keyword hits are exempt. |
| `keyword_weight` | Weighted RRF's weight on the keyword leg; the meaning leg is 1.0. |
| `rerank_min_score` | The cross-encoder score a candidate must reach, 0–1; the shipped `0.3` drops keyword incidentals, `0.0` keeps everything it returned. |

**One budget, and it is the ceiling's.** The kept turns are estimated first; a
recall spends what they leave below `ceiling`, so keeping less leaves it more
and a kept context at the ceiling leaves it nothing. There is no separate
per-recall fraction. **The bound is the prompt's, not the window's**: a provider
counts the prompt and the requested completion against one window, so a budget
sized against the full window fills the request past what any call can send — a
772k selection beside a 384k completion request was a 400 from a 1.05M window.
**It binds the kept turns too**: a selection heavier than the ceiling is cut from
its oldest end, and `fitted` says so. `min_similarity` and `rerank_min_score`
are floors whose scale belongs to the model in use (bge-m3 scores unrelated
same-domain text near 0.5), calibrated per home; the shipped 0.52 and 0.3 are
those calibrations. **Both are filters, not ranking rules**: they decide whether
a candidate stays, and the reranker's order is used only where the token budget
cuts. On the live log an unrelated query still passed `min_similarity` with
three neighbours at 0.52–0.54, and the reranker scored them 0.002–0.036 — below
the shipped 0.3 — which is the floor doing the work the cosine gate could not. A
provider with no `rerank_model` omits the rerank stage.

A missing store fails the turn, like any missing plugin. A decision-model
provider error or unreadable answer does not. There is no switch to turn the
decision off: an append-only context is bounded by nothing.

### 5.6 Cancellation and repair

An interrupted turn keeps what it completed. The batch of tool calls it was in is
completed on the way out, not dropped: the calls that already returned keep their
own answers, and every call that produced no answer is answered
`(Tool execution interrupted)`. The person's message is kept with it, so a
follow-up "continue" resumes from the work that was done.

A batch of tool calls is written atomically: run every call, keeping each answer
as it arrives; fit every result; append the assistant message and its complete
set of answers together, with no `await` between them.

**Why this order.** Running a call cannot raise; the registry turns failure into
text. Fitting a result can raise — a cabinet that will not open — so the
assistant message is written only after every result is fitted. Writing it first
would leave the rejected history behind, and a failed turn keeps its messages, so
that conversation would fail every later turn. An interrupted turn's row is
always written because a write conditional on how a turn ended is one somebody
can forget. A write from a cancelled handler is detached, because a plain `await`
in a cancelled scope is cancelled again at its next checkpoint.

An interrupted turn's write is detached, and the next turn takes its id up
(`settle_record`). A write the store or cabinet refuses is a hard failure: the
turn is unrecorded, so its messages are rolled back to the last backed prefix and
the caller sees the refusal.

**A message that left the inbox is a turn, even when no loop opened for it.** A
hub whose tool list is not answering, a store that will not restore, a rebuild
that fails: the message is put at the end of the live conversation as the turn's
opening message and recorded, so the failure ends with a row rather than with the
caller's words gone from both places. A picture the turn died before resolving is
named in an `image_note`, never kept as a part a provider cannot fetch. An agent
task records nothing at all and answers with its failure instead; its answer is
a message like any other, and this rule keeps that message when the turn it
arrives on fails too.

**A turn that cannot go on ends with the harness saying so.** A tool's failure
is text in the result the model reads and the turn carries on; an exception is
the turn itself stopping, so the turn ends with an assistant message carrying
`[TURN-FAILED:{"by": "harness", "error": …}]`. It is a message rather than a
footnote because a footnote is derived from the row and a failure is not a
column; it is capitalised because the harness is what failed, and a model
reading its own history must not take the words for its own. The message is
recorded with the turn, so the reason survives a restart; a cancellation ends
nothing this way — the caller stopped the turn, and there is nothing to report.

The model's tool result is authoritative. Display chunks are not: progress
notifications may be dropped, delayed, or coalesced without affecting the
conversation.

### 5.7 Images

An attached image is live state **and a cabinet document**. The live user
message keeps the `image_url` parts the model reads; when the turn is recorded,
each part becomes an `[Image:{…}]` reference to a cabinet row of the `image`
kind, keyed by the content digest. The turn log keeps the sentence that named
the file, the tool call and answer naming it, and the reference; the bytes never
enter the row. A rebuilt conversation is text plus references; a model that
needs the picture again calls `attach_image` with the reference's key or with the
URL.

A cabinet refusal fails the turn like a store refusal: a row that names an image
the cabinet does not hold is a marker that can never be attached again. A failed
image attachment is dropped from the live conversation with the same note the
record keeps, and the error says so. A provider that refuses an image refuses
every request that carries it, so such a turn is note-only.

## 6. Model backends

### 6.1 One process per wire protocol

A model backend is a plugin. There is one process per wire protocol, serving
every provider whose `api` is that protocol. Supported chat protocols are
`openai-completions`, `openai-responses`, and `anthropic-messages`. The
embeddings server is separate (`embeddings`): it speaks the embeddings endpoint
and nothing else, because no chat provider can serve an embedding model and it is
the other component that holds a provider SDK and key.

Responses is a separate protocol, not a flag on chat completions: it has
different input items, tool shapes, and streaming events. Merging the two behind
one `api` would make the choice a branch in the server rather than a fact about
the configuration. `api` selects the module to run, the server name it must
identify as, and the default port; an unknown `api` is a configuration error
naming the known values. `API_BACKENDS` and `API_SERVER_NAMES` are views over
`PLUGINS`, not second tables.

### 6.2 Wire abstraction

`libs/model/base.py` defines the neutral vocabulary: `Chunk` (a display
increment), `ToolCallDelta` (one provider fragment of a tool call), `Stream` (an
in-flight call), and `LLMBackend` (a way to call a model). `libs/model/wire.py`
encodes a chunk on the MCP progress channel; a display increment crosses a
process boundary as a progress notification.

### 6.3 Streaming

`plugins/llm/server_common.py` implements one pattern, used by the three chat
servers: one `stream_chat` tool; provider output pushed down the progress channel
as it arrives; the assembled message returned as the tool result.

**Why the progress channel rather than `Context.log()`.** Progress is scoped to
the call that caused it, so two turns in flight cannot be confused. Delivery is
opt-in, and logging is filtered by a negotiated level, so a client that never
negotiated one loses deltas silently; the revision in use also requires a server
not to emit a log notification for a request that did not ask for one.

Rules:

- **The result is authoritative and the chunks are not.** Progress notifications
  may be dropped, delayed, or coalesced by the transport.
- **An unreadable result is refused, not read as an empty answer.** A missing
  result read as an empty assistant message is a turn that "succeeds" with
  nothing in it. A `null` field, or a tool-call list holding something that is
  not an object, is unreadable in the same sense: this build writes a value for
  every key `to_wire` emits, and defaulting one would turn another build's
  answer into a confident empty one.
- **A stream that goes quiet is a stall, not a long answer.** The clock is on the
  wait for one chunk, never on the call; a model that streams for an hour is
  fine. The watchdog is armed by iterating chunks, so every caller must read the
  stream and not only await its result.
- **A provider that does not stream the answer has still answered.** Each adapter
  back-fills what the terminal event carries.
- **A part a request cannot carry is named, not dropped.** A system turn whose
  content is a list of parts contributes its text and warns about the rest.

The stream assembly is one implementation. A provider difference is
configuration, not a branch: `thinking`, `reasoning`, `store`, `temperature`,
`top_p`, `max_tokens`, `stream_usage`, `cache_control`, `input` capabilities,
and `context_window`. Each server sends only what its configuration asked for; a
value the protocol does not know is refused by name where it is read.

**A reasoning block a provider requires back is carried opaquely and replayed
unmodified.** The readable reasoning is `Message.thinking`; `Message.reasoning`
holds the adapter's own payload — Anthropic's signed thinking and
redacted-thinking blocks, the Responses API's reasoning items — stamped with the
model that produced it. The adapter that wrote it returns it beside the tool
results, and only to that model: a conversation moved with `model_switch` does
not replay model A's signed blocks to model B. This is what makes a first-party
reasoning model's second tool-loop call work. A Responses request with
`store: false` asks for `reasoning.encrypted_content` in the same breath, because
a stateless server has no other copy to replay.

### 6.4 Provider capability configuration

A model entry declares its capabilities in its own configuration row:

| Field | Meaning |
|---|---|
| `input` | Modalities the model accepts; `text` and `image`. |
| `context_window` | The model's window. **Required**: the loader refuses an entry that omits it or states a number that is not positive. A request's prompt and its completion request share it, so the prompt's own space is `context_window - max_tokens` (`ModelSettings.prompt_window`), and every context fraction and status percentage is measured against that. |
| `reasoning` | Whether the model exposes reasoning. |
| `compat.thinking` | Provider-specific thinking setting. |
| `compat.store` | Provider-specific persistence setting. |
| `compat.cache_control` | Anthropic's top-level automatic prompt cache: `ephemeral`, `5m` or `1h`; absent or `omit` sends nothing. |
| `max_tokens` | The completion request, sent as the call's `max_tokens`. A provider counts it against the window with the prompt, so it is reserved from the context budget rather than being a free parameter. |
| `temperature`, `top_p` | Generation parameters. |
| `compat.stream_usage` | Whether the provider reports usage in the stream. |

The `compat.*` keys sit under the model's own `compat:` mapping; the others sit
beside `model:` in the same entry. The terminal reads the same entry for its
status bar and image support. A model that does not declare `image` is not
offered image attachment.

### 6.5 Usage and cache accounting

`Usage` counts the cached prefix inside `prompt_tokens`: Anthropic's
`input_tokens` is the uncached remainder, so its adapter adds the two cache
fields back in; OpenAI-compatible wires already include cached tokens.
`cache_read` and `cache_creation` are parts of the prompt, never additions to it.

OpenAI sends token counts on a trailing chunk with an empty `choices` list;
DeepSeek attaches them to the final chunk that still carries a choice. The
adapters accept either shape; an adapter that handles only the documented OpenAI
shape reports zero tokens against a real DeepSeek endpoint.

`stream_chat` takes a `kind` word: `turn` for the loop's own calls, `decision`
for the recall decision, and `compactor` for the live-view selector. One line is
logged per call with the breakdown and the call's `elapsed`, because a cache hit
rate distinguishes a rebuild from an append. A call that raises logs
`stream_chat_failed` with the provider, model, conversation, kind and the
transport cause (`cause=ConnectTimeout`, `cause=RemoteProtocolError`) before the
error propagates; the cause is carried into the error the caller reads, because
the SDK's top-level sentence is the same for every member of its family and a
transport failure has no HTTP status of its own.

## 7. Tool system

### 7.1 Hub

`plugins/toolhub/server.py` is the only process that decides which tools the
model may see. It holds no connection to any external server and no credential.
Every question about the set — what exists, what the model is holding, what a
name resolves to, what the budget takes back, who may call what — is a question
about the whole list, so it is answered in one place.

The hub serves four set-level tools:

| Tool | Purpose |
|---|---|
| `tool_search` | Search the whole catalogue by keywords and by meaning. |
| `tool_list` | Page the catalogue — every category, every source — with `category`/`source` filters. |
| `func_tool_load` | Put one or more tools into the model's list. |
| `_func_tool_unload` | Remove tools from the list; the harness also calls it at a turn boundary to enforce the budget. |

The catalogue is a library (`libs/store/catalogue.py`), opened in-process by the
hub, which is its only writer. One SQLite file per home
(`<data>/agents/tools.db`) is the catalogue.

`list_tools` answers two numbers: `tools`, the list the model is holding, and
`total`, the size of the catalogue. `total` counts every category — a skill and
a CLI entry are rows with no load state, a job is a callable row with one — and
every source, including one that is down and keeps its rows on purpose. The
terminal's status bar shows it.

### 7.2 Sources and declarations

A family plugin owns one configuration section or folder and declares its rows
to the hub. The two connection families hold the connections their sections
name. The hub merges every declaration into rows and routes calls back to the
declaring plugin. This is the other half of credential isolation: the process
that starts a child needing `SERPER_API_KEY` reads that key; the hub holds none.

Every plugin is **required**. If one of ours cannot be reached, the listing
refuses rather than serving a shorter list. An external entry that cannot be
reached is reported and left out; the model simply has fewer tools.

A declaration is the whole list, and it is refreshed before every search. The
whole list is what makes a deleted entry stop being a hit; refreshing is what
makes dropping a directory into `skills/` or editing the configuration take
effect at the next search. The hub's own tools are rows too. A plugin that is up
but cannot answer this round keeps the sources it declared last time — its rows
stay in the catalogue, and the address book keeps their routes and its place in
`servers()` — so one late report costs freshness and nothing else. A plugin that
answers with fewer sources has those sources removed.

A plugin's own tools state the arguments they take; the hub stores the input
schema in the catalogue's `schema` column, which spells absence as the empty
string. A value that will not parse is damage: the hub refuses the list rather
than serving the tool with no arguments.

### 7.3 Naming

Names are rewritten once, at the hub.

- A tool from an external source becomes `{server}__{tool}`. The double
  separator is used because a single underscore is a character an upstream tool
  name may contain.
- A plugin's own tool keeps its bare name. Which plugin serves a tool is a fact
  about the system, not something the model needs in order to call it.
- All tools share one namespace. Any two that would present one name — two
  plugins, a source colliding with another, or two remote names the provider's
  alphabet makes alike — are refused by the catalogue rather than resolved
  silently.
- A name from an external source is not trusted: `model_name` derives it,
  `sanitise` applies the provider's alphabet, and the separator makes it
  reversible.

A skill and a command are rows but not callable tools. They have no load state
and never enter the model's list. Calling a skill row answers with the step that
reaches it (`skill_use`); calling a command row answers with the rule that a
command is run through the shell, not called as a tool. Their names are
namespaced (`skill:browser-harness`, `cli:yt-dlp`). Neither family gets a runner
of its own: a skill is instructions the model carries out, and a command is a
program the model runs through whatever shell its tool set provides. What the
row adds is that the command can be found.

A **job** is a row that *is* callable and loadable, because its implementation is
code rather than a document or an installed program. Its name is `job__<name>`
because the bare name is a user's Python identifier and the prefix keeps it out
of the system's namespace. Its row is the same shape as any function tool's.

### 7.4 Loaded set and budget

What the model is handed is the tools it has loaded, not every tool that exists.
The list is re-read from the hub before every model call.

Initial state: the tools named in `tool_load.autoload` are loaded, by the name
the model calls them — a plugin's bare name or an external server's
`{server}__{tool}`. The names the system itself refers to (the four set-level
tools, the two harness pairs, `skill_use`, and the two background-call tools) are
required in that list; a configuration that omits one fails the start.
Everything else, a plugin's tools included, starts unloaded when first seen. One
tool the model never calls is a prompt spent on every request.

**A call for a name that is not loaded is checked against the catalogue and, when
it fits the row's current schema, loaded and run in the same turn.** Loading is
what keeps the tool in the list sent with every request. A call that does not
fit — the schema changed since the model last saw it, or its arguments could not
be read — is not run; it takes the load hint, which is how the model reads the
current schema. A call that cannot be resolved that way reaches the registry's
error path, which asks the hub (`tool_exists`): `present` (load it with
`func_tool_load`), `missing` (no row; `tool_search` finds what does the job), or
`starting` (the catalogue is still being written, so retry). The third exists
because a first turn can land inside the merge pass; without it a transient
absence read as a capability the machine lacks.

**A restart restores what was left, not a fresh state.** The conversation's
context and the loaded tool set both come back as they were; only a change to
the configuration alters what a restart restores. The loaded set lives in the
catalogue, not in a process, so the boot pass's only move is to load a name on
`tool_load.autoload` that is still unloaded. Stopping or starting a process never
takes a tool out of the model's list; only eviction at a turn boundary, or the
model's own unload of an unprotected name, does.

The loaded set is bounded by `tool_load.threshold` (default 100 function tools).
Over the limit, the least recently **called** tools are unloaded; the names in
`tool_load.autoload` are never evicted, and a tool never called since it was
loaded is ordered by its load time. The trim happens at a turn boundary: the
harness calls `_func_tool_unload` before saving the turn and writes the pair into
the conversation, so the model learns that the list shrank from the transcript.
The pair's answer is the names that left, rendered as a note on the turn's
stream (§5.3). Nothing is written when nothing was removed. The same list bounds
the model's own unloads: `_func_tool_unload` refuses a name in
`tool_load.autoload`, so the tools that start loaded, the tools the budget may
not evict and the tools the model may not unload are one list. Editing the list
changes only that protection. Recency is learned by the hub: it stamps
`last_used` beside the call it just routed, including a refused call, before
returning the answer, so the stamp cannot land after the trim.

### 7.5 Catalogue search

`tool_search` takes two kinds of input:

- **`keywords`**: words to look for. The keyword leg is for names, paths, and
  symbols, and ranks documents carrying all of them first — but it is not a
  filter. A keywords-only call is read through both legs, because a word form the
  text does not carry would otherwise find nothing; a hit may therefore come from
  meaning without carrying the words.
- **`sentences`**: questions matched by meaning. Each sentence is a separate
  question, and another question is another chance to be found; it cannot cost a
  row an earlier result.

A keyword list with no term in it (`[""]`, blanks) is no keyword leg: beside a
sentence the meaning leg still runs, and alone there is nothing to search with
and the call is refused.

**A corpus with no vector index is not a corpus where nothing is similar.** If no
index was ever recorded, a sentences-only search fails, naming the missing index
and the sync/restart; a keywords-only search still answers and carries
`meaning: not_indexed`, so a keyword page is not read as the meaning leg's
verdict. A *recorded* index whose table is gone is damage and fails either way.

The two legs are fused by rank, not by raw score: `bm25` is unbounded and a
cosine depends on the embedding model, while a rank is comparable by
construction. Within one question that is the whole ordering. Across questions
the page is ordered by what the caller can see: rows the caller's own words
matched first, then by meaning descending. The words come first because cosines
cannot separate a near-duplicate cluster, and only the word match says which one
was named.

**Why these shapes.** Split inputs beat one string on the page at 20/20 vs 18/20.
Keywords-only reaching the meaning leg found 34/36 against 23/36 for the keyword
leg alone. Two sentences ranked by fused score let an unrelated second sentence
move a first question's fifth answer to rank 9, which is why the page rule
exists. Chinese text is indexed character by character and queried as a phrase:
FTS5's tokenizer would otherwise treat a contiguous Chinese run as one token.

The answer names its confidence in three tiers because the measurements overlap:
answerable queries had a best-on-page meaning no lower than 0.477, unanswerable
queries had no value higher than 0.512. Every row carries its meaning score and a
word-match marker saying whether the match was in the visible name or
description, or behind it in the body or parameters.

Filters narrow the result and never decide it. They are applied in SQL rather
than after fusion, because rank orders every match and cutting to the limit first
would drop rows the filter would have kept.

`unloaded_only` is the one filter that is not an equality: it means
`load_status != 'loaded'`, so a skill or a CLI entry — which has no load state
and is never in the model's list — and an unloaded job are still found. The
default stays the whole catalogue: the best answer may be a tool the model
already holds, and `loaded` on the row says so.

**Listing is the other question, and it is `tool_list`.** A search ranks what
matches a question; a listing pages what exists, for a caller with no words to
search with. They are separate tools because one answer cannot be both: a
listing has no score and no confidence, and its order — category, then source,
then name — is the only order it has. For the same reason `tool_search` takes no
`limit`: a search finds *a* tool, and paging the catalogue is the listing's
question. The name is last because it is the primary key, which makes the order
total and two pages unable to drop or repeat a row between them.

`category` and `source` narrow a listing and are applied in SQL, so `total`
counts what the filters match. `total`, `limit` and `offset` follow §7.6's page
rule; `limit` omitted is 20, and the cap is 200. The rows carry the same fields a
search result does, minus `meaning` and `matched`. A skill, a CLI entry and a job
are rows like any other; `loaded` is true only for a row in the model's list.

A category outside the closed set is refused rather than answered with
`total: 0`: an empty page is a confident "nothing is installed", and a typo must
not produce one. `source` is not validated the same way — an entry that is
connecting or switched off has no rows yet, so an empty page is a true answer
about the catalogue rather than a wrong one about the source.

A family no longer serves a listing of one server's tools (`mcp_list_tools`,
`rest_api_list_tools` are gone): those are catalogue rows, and this one pager
answers for every source. A search with no input names `tool_list` in its
refusal, since it is the tool that caller wanted.

### 7.6 Result contract

Everything the model reads is a surface. A tool's answer is as important as its
schema.

- **The answer is a JSON object, with the two exceptions below.** A tool that
  answers a sentence invents a second format.
- **A listing is a page:** `{"<noun>": [...], "total": N, "limit": L, "offset":
  O}`. `total` is the whole answer to "is there more" and counts what the filters
  match.
- **A refusal or a miss raises `ToolError`**, one sentence saying what is wrong
  and what to do instead. The failure is the call's status: the hub answers
  `ok: false`, the loop marks the call failed, and the window draws it as failed.
  The one batch whose entries stand alone — `file_save` — answers each refusal
  in its own part instead, and §8.3 says how.
- **Two tools answer with text rather than JSON, because the text is the
  thing.** `_check_new_input` hands over a message from this conversation's
  queue mid-turn; `skill_use` hands over the skill's whole `SKILL.md`.
- **No answer exceeds `tool_result.bytes`** (default 32 KiB; minimum 2 KiB). A
  listing pages; an output that cannot be bounded is the harness's dump, and
  `dump_read` and `dump_grep` are bounded readers within the same budget.

A field may contain a sentence, because a person reads some answers too. The rule
is that the envelope is one shape, so a reader that knows one tool knows them
all.

### 7.7 Meta-parameters

Every function tool accepts three meta-parameters. They are added to every
advertised schema and stripped before dispatch, so no tool server sees them.

| Parameter | Semantics |
|---|---|
| `_timeout` | Seconds for this call: a positive, finite number, overriding the tool's default. Any other value — `0`, negative, text, `inf` — fails the call with an error the model reads; the value is never read as "no limit" or as the default. A tool that declares its own `timeout` argument enforces it itself. With neither, the call runs until it finishes. There is no default budget. |
| `_async` | Run the call in the background. The model receives a task id; the result is handed back into the conversation. The result is still bound by `tool_result.bytes`. An agent task is one turn: its prompt tells the model not to use `_async` or start another task. |
| `_approve` | Ask the user before running. The terminal draws an inline row and answers through `answer_approval`. A channel with no person behind it does not ask; the call runs. A caller that can answer declares `watched=true`. |

The loop reads these parameters off the call and strips them before dispatch. The
strip is a copy: the tool is dispatched with one dict and the history keeps the
original.

## 8. Memory and indexing

### 8.1 Turn store

`libs/store/db.py` implements one SQLite file per conversation id:
`agents/<agent>/turns.db` for the agent's own conversation, and
`agents/<agent>/subagents/<name>/turns.db` for a subagent's. Isolation is by
file: no query can reach another agent's turns because they are not in the file.
The home a call came from selects which of the agent's directories is read or
written. The store holds the turn log, the live-context list, and the indexes
over the turns.

A turn is stored as the message list the model saw. What was asked is the first
user message; what was answered is the last assistant message with prose.

There is no migration layer. A new file gets the tables; a file this build
cannot read is refused, not upgraded in place, because an upgrade path is a
permanent second way to build the tables. A file whose `turn` table lacks a
column this build declares is refused at open, with the file and the column
named. A file that still holds the previous schema's `turns` table is refused
the same way, before any DDL. A file this build refuses fails the call that
uses it, and only that call; starting the plugin touches no store.

The live-context list is a row, written in the same transaction as the turn that
changed it. A turn can point outside itself: a tool output too large for the
conversation is stored whole in the cabinet, and the turn keeps a marked head of
it, so the row still holds exactly what the model saw and the whole is reachable
by the tool call id. **A stored value that is not a list of turn ids is refused
rather than coerced**: `true` is an `int` and a mapping iterates its keys, so a
damaged value would otherwise add a turn nobody named — and the append path would
write the wrong id back for good.

**A live-context id with no row, and a turn body that will not parse, are damage
the context refuses.** The store reports both (`fetch_turns`): a restore or
rebuild hard-fails, naming the ids and the repair, while the window's transcript
reader draws the damaged row as an error and counts only what it drew. A damaged
body is **not an empty turn**, and a missing id is **not a shorter context**.
`turn_read` refuses the damaged row by name.

**A read does not create the file.** A conversation that has never run and a
removed subagent's stale name are the same absent store, and a read answers its
empty shape rather than creating a directory with no `subagent.yaml` — which
would then block the name against `subagent_create` and `subagent_remove`. Only a
write makes the directory and the file.

### 8.2 Index machinery

`libs/store/index.py` implements the search machinery used by all three corpora:
turns, the tool catalogue, and the cabinet. Each corpus is searched by two legs —
**keyword** (FTS5 over a derived, normalized string) and **meaning**
(`sqlite-vec` over chunks of a different derived string) — fused by rank.

**The three searches answer one envelope.** `tool_search`, `cabinet_search` and
`turn_search` take the same two inputs — `keywords` for the words leg,
`sentences` for the meaning leg — and answer the same object: `results` (the
hits, in the order that search ranks them), `best` (the highest meaning score on
the page, `null` when nothing was scored by meaning), `confidence` (`sure`,
`weak` or `none`, or `null` on a browse, which judged nothing), `browsed` (true
only for the cabinet's both-empty call; the other two refuse it and name their
listing tool) and `limit`. `libs/store/answer.py` is the one home of the
envelope and of the floors behind `confidence`; the floors are the embedder's,
measured on the catalogue (§7.5), and the words override them in every corpus.
**`turn_search` alone takes `exclude_context`** (default false): the turns a
conversation is running on are still its history, and leaving them out is the
caller's request, not the search's rule. **`tool_search` and `cabinet_search`
also carry `meaning`** (`ok` or `not_indexed`): a sentences-only call against a
corpus whose index was never built fails, naming the sync, and a keywords-only
page says its meaning leg was not asked. `turn_search` is gated on a ready index
before it serves, so it needs no such field.

Rules:

1. **What a row is found by is not what it is about.** The keyword leg indexes
   everything worth looking a row up by; the vector is built from a text that
   describes what the row is.
2. **A chunk's vector is written before the chunk that names it**, because a
   `vec0` row carries its own rowid and a vector left behind is a row the next
   write collides with.
3. **The vector width is discovered, never configured.** A rebuild is decided by
   an identity recorded in the file (model, width, normalization rule,
   vectorized kinds); `vec0` fixes a table's width in its DDL, and a wrong width
   fails silently.
4. **One normalization rule** applies to stored text and to queries alike.
5. **A list the caller sized is bound in chunks**, because SQLite limits the
   values in a statement.
6. **One request carries a batch, and an answer shorter than the request is a
   refusal.** A row is written with its vectors in the same transaction.

**Why the two texts differ.** What a turn is about is its own voice: the
question, the model's reasoning, and the answer. Tool results were measured at
56–99% of a turn's text, so embedding them makes every turn describe "an agent
ran tools"; the keyword index keeps them whole because a path, an error string
or a command is named literally by the query that wants it. Dropping the
per-call argument head raised MRR from 0.83 to 0.88, adding the reasoning raised
it to 0.92; keeping full arguments cost 2.7x the chunks to recover one query.

A change to a composition is an identity change: `EMBED_TEXT_VERSION` is folded
into the vector identity and `SEARCH_TEXT_VERSION` into the keyword stamp beside
`RULES_VERSION`; either rebuilds the index at startup rather than mixing rows
made from two different texts.

The token estimator uses `tiktoken`'s `o200k_base`, **bundled with the package**:
`libs/turn/tiktoken_cache/` holds the cache file and the resolver points
`TIKTOKEN_CACHE_DIR` at it, so a resolution is a local read and parse (~186 ms
once per process) with no network. **It serves the context rebuild's recall
budget and nothing else**: a tool result is bounded by bytes (§5.2.4), and a
page or a dump reader fits itself by bytes too. **A missing or hash-mismatched
bundle is a damaged installation, not a second measure**: the first count that
needs it fails, naming the bundle and the reinstall/restart. Measured usage is
unaffected: the provider's own count is what the record keeps.

### 8.3 Cabinet

`plugins/cabinet/` stores six kinds of row; four are its corpus. A note, a
diary day and a report share four verbs: `{kind}_write` creates the document and
refuses a key that already exists, `{kind}_read` reads it, `{kind}_update`
replaces the whole document, and `{kind}_remove` deletes it. A saved file's
write is `file_save`, which copies a local file or fetches an `http(s)` URL in;
its read, search and remove keep their own names (`file_read`, `file_grep`,
`file_remove`).

| Kind | Key | Write behavior |
|---|---|---|
| `note` | A subject | Creates; an existing subject is refused. |
| `diary` | A date | Creates that day's entry; one day is one document. |
| `file` | Its path inside the cabinet | `file_save` copies a local file or fetches an `http(s)` URL in. |
| `report` | A title | Creates; an existing title is refused. |
| `dump` | The tool call that produced the output | Written by the harness; a second write of one call replaces. |
| `image` | The content digest | Written by the harness; bytes in `pictures/`. |

One table holds all six. The corpus is the four the cabinet manages — `note`,
`diary`, `file` and `report` — and a search ranks them against each other; a
corpus whose rows lived in four tables would have to be unioned before it could
be ranked. `kind` says what a row is, `key` is what a write, a read, an update
and a remove take, and columns a kind does not use are empty. A `dump` and an
`image` share the table because they share the directory, the key and the write
path, but they are not in the corpus: the turn's marker is their address
(`[DUMP:…]` → `dump_read`/`dump_grep`, `[Image:…]` → `attach_image`).

**The meaning leg covers the three agent-written kinds.** A note, a day and a
report are vectorized by their whole file text. A saved file is in the keyword
corpus only, and a dump and an image are in neither. **A file's and a dump's
size are not the agent's to control**, so meaning search over them is deferred
until a pass that chunks and embeds their contents exists; until then a file is
found by its words. A file's keyword text is its name, the paths that place it,
and a summary once one is written; writing a summary re-embeds nothing. An image
is not indexed at all.

The cabinet is per conversation: an agent's own conversation has
`agents/<agent>/cabinet/`; a subagent has its own under its directory.
**The file is the document.** For every mirrored kind — note, diary, report,
dump — the text is written to its file and the file put in place before the
row's transaction commits, so a filesystem that refuses the file takes the row,
its keyword text and its vectors back with it, and **there is no body column**:
the row is metadata and the address, not a second copy. The file holds the text
exactly as the caller gave it; a note's subject, a day's date and a report's
title are the row's and are never prepended to it. **A mirror file that is gone
is refused by name when read, and skipped by the index passes.** **Every answer
about a document carries `path`** — the file relative to the data directory,
where the agent's own tools run — so a model can hand the same file to `pandoc`,
`rg` or a shell; the internal `mirror` and `file_path` stay out of the answer. A
file and an image have no mirror, because their bytes are the document. The path
is stored rather than derived, so a change to the naming rule orphans nothing
that already exists. Every caller-supplied path is resolved against the cabinet
root and refused if it leaves it.

**A corpus-wide literal or regex search is the external `rg`**, run through the
model's shell over those paths, and not a third leg of the cabinet's search. The
documents are already files on disk, so rebuilding `grep` inside the cabinet
would be a second implementation of a program the machine has.

A subject that is not already lowercase carries a short digest in its filename,
because `Python.md` and `python.md` are one file on Windows and macOS.

A saved file's source is a local path or an `http(s)` URL. A local file is
copied and never moved; a relative path is resolved against the data directory,
where the agent's own tools run, rather than against the cabinet daemon's own
working directory. A URL is fetched whole, bounded by `MAX_FETCH_BYTES` and
`transport.file_fetch`. The URL is untrusted input: only `http(s)` is accepted,
credentials in a URL are refused, and a host written as an address or as
`localhost` must be globally routable, so a fetched URL reaches the internet
rather than this machine. Every redirect is checked by the same rule. Names are
left to the machine's resolver; a fake-ip proxy answers an address the proxy owns
rather than the destination, so resolving one in the guard would test the proxy
rather than the site.

**One call's sources stand alone.** `file_save` takes several, and each is
stored or refused on its own: the answer carries `saved` rows and `failed`
entries — the source as the caller gave it, and the refusal that stopped it —
so a later URL that cannot be fetched leaves the earlier files stored **and
named in the answer**, and a retry knows what not to store again.

A dump and an image have no model-facing writer. The harness saves a tool output
too large for the conversation with `dump_save`, and a picture a turn carried
with `image_save`; neither write produces an index entry, because the turn's
marker is the address. **A dump's text is its file, and its row holds no body** —
one copy, whether the output is a kilobyte or a gigabyte. `dump_read` and
`dump_grep` are the ways back for a dump. For an image it is `attach_image`: the
turn's `[Image:{…}]` reference names the key, `image_source` resolves it to a
`data:` URL (or the source URL), and the tool attaches it. An `http(s)` image is
never fetched here; the row keeps the URL and the provider fetches it.

`dump_read` and `file_read` take a line or character `offset` and `limit` and
answer `next`, the start of the following window; line mode numbers its lines and
character mode returns the raw slice, for text not organized in lines.
`dump_grep` and `file_grep` match a literal within one line — not a regex, which
over a 240,000-line output is an unbounded program in a shared server — and
answer both the line number and the character offset, so either reader can
continue from a match. All four stream: `file_grep` holds only the page, and
`file_read` counts once and then seeks, so memory is the window, not the file. A
PDF, image or archive is a bounded escaped rendering of its bytes. Path
resolution, the binary rendering and the encoding sniff are one function each, so
the paired readers cannot disagree about the text. Every answer stays within
`tool_result.bytes`, enforced where the answer leaves.

### 8.4 Embeddings

One server (`plugins/llm/embeddings_server.py`) and one client
(`clients/embedder.py`) serve all three indexes: the turn store, the tool
catalogue, and the cabinet. Sharing is required because the three indexes must
rank against the same model and agree about what an embedding is.

The server holds the provider key for the same reason the model servers do.
Embeddings are a hard dependency: a turn is written with its vector in one
transaction, so a save that cannot embed stores nothing. There is no background
drainer and no eventual-consistency mode: the context plugin checks a turn file
once before serving it, and `index_status` names a partial sync, a missing
vector, another model's identity, or a recorded vector index whose table is gone.
The catalogue and the cabinet ask the same question at their searches, which
raise rather than answering keyword-only (Appendix C). A recorded identity whose
vector table is gone is rebuilt by the sync; a restart is therefore a real
repair, not advice.

The SDK's own retries are zero (`timeouts.stream.retries`): a request that
reached the provider may have been billed, and a 429 is the caller's to pace.
The one repeat the server makes is a connection that never opened — a
`ConnectError` means nothing was sent and an embedding is idempotent, so the
retry cannot double-charge or change the answer, and a transient blip in the
link to a remote provider does not fail a cold catalogue build. A second failure
is the caller's, and the hard dependency above stands. **The client's repeat is
on a reopened connection**: a keep-alive the server closed leaves a dead MCP
session, not a dead socket a pool can replace, so the retry opens a new one
first — and a reopen that describes another model is refused rather than ranked
into an index built for the old one.

The width is measured, and the input limit is asked for: an input limit cannot
be measured without sending an oversized input, so a model whose endpoint
reports no limit is refused rather than given a guess.

Changing the embedding model — or the text a corpus builds its vectors from —
invalidates the index identity. The vector index is rebuilt whole on the next
start, because vectors from two models cannot be ranked against each other. The
turn store's text contract is `EMBED_TEXT_VERSION`, folded into the identity
beside the model and width.

## 9. Inbound work

Four kinds of work reach a conversation without a person typing it: WeChat
messages, peer-agent messages and tasks, agent tasks, and scheduled tasks. They
share one policy: what arrives becomes a turn, and the turn records where it came
from (§5.4).

### 9.1 WeChat

`plugins/wechat/` holds one login per agent and turns what arrives into a turn.
A subagent has no login of its own: WeChat is an external identity, and a
subagent is internal to its maker. WeChat exposes no direct API to a program; the
plugin speaks iLink's hosted bridge (`wechat/ilink.py`: the QR handshake, the
long poll, and the envelopes whose meaning differs from their status). The
plugin owns no configuration section and declares no catalogue rows; what it
holds is a credential and a live conversation per agent. Its four tools are
`wechat_login`, `wechat_check_status`, `wechat_send_message`, and
`wechat_logout`.

The file carries the raw agent name. The directory holds the sanitized name; a
session keyed by the directory name would build a second session for one login,
adopt the same token, start a second poll loop, and deliver every message twice.
A file too old to carry the raw name is skipped and named in the log.

One WeChat account has one live login. A second scan retires the first login
silently; the bridge answers the first with `session timeout` — a rejection
wearing HTTP 200. Both error-envelope shapes are read, so the first loop ends,
the window is told, and the model is given the bridge's own words.

**Two writes are the login's, and both are reported rather than retried for
ever.** The session file is the next start's record: a save that fails leaves
the login live only in this process; `wechat_check_status` says
`login_saved: false` and names the path, and the next cursor write tries again.
The cursor is the stream's place: a failed write is retried on the next move and
answers `cursor_saved: false` with the path until one lands; three consecutive
failures stop the poll (`polling: false`), because continuing would answer the
replay window again after every restart. **The failure belongs to the login that
met it**: a new scan and a logout clear the cursor's failure count and its
terminal flag, and `wechat_login` cannot repair it, since the token is already
live. A restart before the login file lands needs a new scan.

While a login waits, the session holds the code itself: `wechat_check_status`
hands out the drawn QR and what it encodes. A code that expires is replaced in
the background and the replacement becomes the session's. A new login clears the
code it replaces before fetching, and logout clears it with the state. **A scan
that lands while a replacement code is being fetched is the login**: the session
it confirmed is not reset to waiting behind a code nobody has to scan.

A message from WeChat becomes a turn in the agent's conversation, recorded with
channel `wechat` and a `[Wechat:{…}]` marker naming the sender. Answering is the
model's job; nothing auto-dispatches a reply. While the turn runs, the other
person sees a typing indicator. Each agent logs in separately and has its own
file and poll loop; a subagent does not, and the plugin is one shared process.
The connection's arrival and departure are drawn as harness lines
(`[⚡ Wechat online]`, `[✗ Wechat offline]`), not turns.

### 9.2 Peer agents (A2A over MQTT)

`plugins/a2a/` carries agent-to-agent traffic over MQTT, using the published
A2A-over-MQTT profile 0.1 (over A2A v1.0.0). Its remote instance is the peer
network of agents; a subagent is not a member of it and exists only on the local
instance, under its maker.

The wire vocabularies are the `a2a-over-mqtt` SDK's: the topic tree, the
retained agent card and presence, the JSON-RPC envelopes and their MQTT
properties, and the protocol's classification helpers. The responder lifecycle
is a local derivation in `plugins/a2a/responder.py`, because the profile's
interrupted-task continuation is not in SDK 0.1.0 (upstream `main` is
identical): a round may end `input-required`/`auth-required` without a terminal,
a new `SendMessage` with the same `Task.id` and `context_id` resumes it, a
terminal task refuses a new message, and `GetTask` reports the current task. The
requester ladder and the two stores are this package's.

**One identity per agent.** The agent name is the MQTT client id and the last
segment of the request topic. Two processes answering to one name trade the
session back and forth, and every task sent to it runs twice. So there is one
mesh per agent; a second live instance under one name is refused before joining;
and the collision check reads the retained card on the name's discovery topic.

Rules specific to this system:

1. **Presence follows a window.** A `join` call held open puts an agent on the
   mesh. Online and offline are notes, not messages; no turn runs and the model
   is not told. The roster is `peer_agent_list`. A call that takes the mesh
   during the departure wait restarts it rather than being disconnected under.
2. **A round is settled by the model's own call.** `peer_agent_task_response`
   settles it with `completed`, `failed` or `rejected`; `input_required` and
   `auth_required` pause it instead, and `peer_agent_continue_task` resumes a
   paused task this conversation sent. A turn that ends in prose sends nothing.
3. **A restart drops working rounds; task state is process-local.** The bridge,
   the reply topic, and a paused task's question and context live only in the
   process. The peer's own `task_list` names what it is waiting on, and it can
   cancel or re-send.
4. **Outcome is one word, and it is the peer's own.** `completed`, `cancelled`,
   `failed` and `rejected` come from the reply; `input_required` and
   `auth_required` mean the task is paused and open. The one outcome no reply
   can produce — the retry ladder running out — is `UNREACHABLE`, the
   requester's own failure.
5. **A withdrawal outranks a late answer.** Cancelling publishes the withdrawal
   first and records it only after the publish lands; a failed publish leaves the
   row pending. A later result cannot rewrite an ending the caller was told
   about.
6. **A send reported failed is not retried behind the caller.** If the first
   publish raises, the send is closed with the caller's error. A publish refused
   before the outbound client is touched drops the correlation it registered.
7. **The broker is probed, owned, and stopped with the stack.** The plugin starts
   a broker when the configured one is local and absent, fails the start if it
   still will not serve, and `slife2 down` stops a broker this machine started.
8. **An inbound response-typed message is acknowledged and dropped.** A stray
   `task_response` on the request topic is not a new task.
9. **A wildcard broker address is refused.** `0.0.0.0`, `::`, or empty is every
   interface, not the machine's loopback broker.
10. **`provider:` is a hard constraint.** Mosquitto is the only broker this build
    speaks; any other value is a configuration error.
11. **A broadcast is not delivered to its sender.** The event subscription uses
    MQTT v5's No Local option. An event that names this member but was published
    by somebody else is refused.
12. **A group is a named roster with an owner, kept per member.** The owner
    creates it and names the members; there is no invitation or acceptance. Each
    member persists its own YAML record and learns the roster from group traffic;
    only the owner may replace the roster or disband it, while any member may
    leave; an owner's departure transfers the group to the first remaining
    member or disbands it. A member's message can teach a group this receiver
    does not hold, never rewrite a roster it has.
    A `group_set` is idempotent: the same roster keeps the gid, and the repeat
    re-sends the notice so a member that missed the first catches up. Membership
    controls are silent — they change the local record, not a live check. **A
    groups file this build cannot read is damage, not an empty roster**: its bytes
    are preserved as `<file>.corrupt` and every group tool fails naming that file,
    while the peer network carries on without groups.
13. **Group traffic is directed, fire-and-forget, and never returns to its
    sender.** One QoS 0 event per recipient on that recipient's own topic, with
    the roster and a generation id, so a member that missed a control learns the
    group from the next message and a delayed event cannot resurrect a group it
    left. **A group chat is casual**: no member owes an answer, silence is not
    `pending` or `undelivered`, `sent` confirms publication only, and a task
    request is the only send with an expected answer. A group holding only the
    sender is empty, and a send there is refused. Mentions (`@name`, `@all` /
    `@所有人`) are text, not delivery; every other member still receives the
    message. The peer network uses the `PeerAgentGroup` channel and marker, the
    local network `SubagentGroup`; the marker carries `from`, the group and its
    owner, and the roster is a separate read.
14. **A local send names a member; a remote send says it is unverified.** On the
    subagent network membership is the directory set, so an unknown name is
    refused before it is published. The peer network has no authoritative
    roster, so its sends return `status: "unverified"`: the id means acceptance
    and publication, not receipt. A send that is never answered ends as an
    `UNREACHABLE` notice.
15. **A paused task is continued, not re-sent.** A continuation is a
    `SendMessage` with the same `Task.id` and `context_id` and a fresh
    correlation; the responder couples it to the next round. The retry ladder
    and the routing table are generation-scoped, so a late terminal from the
    paused round cannot settle the continuation. A terminal task accepts no new
    message, and a continuation that never lands restores the paused state with
    its question.
16. **A failed reply is a verdict with guidance, not an automatic retry.** The
    transport ladder stops at the first correlated reply. `-32003`/`-32004`
    render as failures the model may re-send; `-32005` is not retryable; an
    unknown task is `-32001`. A peer's `failed` or `rejected` is terminal.
17. **A message that reaches no conversation is rejected, not dropped.** The
    requester stops its retry ladder at the first correlated reply — the
    responder's `submitted` acknowledgment — so a message no window takes is
    settled as `rejected` with the reason, and a bare `message` reports
    `completed` only after the conversation has taken it. The peer reads the
    ending and decides whether to send it again.
18. **The broker address and namespace are read per call.** An edited
    `peer_agent:` reaches a member that already exists at its next call: the
    mesh built for the old address is torn down and a new one is built on the
    address the config names now. A roster read follows the same rule. The old
    address's outstanding sends end like any teardown's.

**Why directed topics.** Group traffic uses
`event/{org}/{unit}/member:<name>`, one topic per recipient, subscribed by that
member alone. A per-group topic needs dynamic subscriptions and a subscribe
lifecycle in the reconnect loop, and a member that missed the invitation is not
subscribed to the messages that would teach it the group. The request path would
give every group message a task lifecycle and a reply ladder it has no use for.
The directed topic needs no subscription state beyond the fixed
`member:<own name>`.

The model-facing tools are `peer_agent_send_message`,
`peer_agent_task_response`, `peer_agent_continue_task`, `peer_agent_cancel_task`,
`peer_agent_list`, `peer_agent_task_list`, `peer_agent_broadcast`, and the six
group tools. `peer_agent_task_list` reports `pending` (still running),
`interrupted` (paused; its `question` says what it wants) and `unreachable` (the
requester's retry ladder ran out); an unknown id is reported as withdrawn or
never seen. An interrupted task stays listed until it is continued or withdrawn
— there is no age at which a question somebody has not answered stops being
open.

A task id is a canonical `uuid4` string, generated by the requester; a
continuation reuses it and the original `context_id`, while a new task gets
fresh ids. The in-memory task store is shared by every mesh in the process, a
record carries the agent it belongs to, and the store is pruned with a soft cap.
**The order is one policy for the outbound store and the inbound table**:
terminal records first, then live work, then a paused task — whose peer is
waiting on an answer somebody has to give.

**The requester half is also a command.** `slife2 probe` sends one
`task_request` and prints the terminal artifact; it is a peer, not a channel, and
it reads no turn store. §10.5 has the one-shot argument.

**Profile conformance.** The implementation targets A2A-over-MQTT profile 0.1
(`emqx/mqtt-for-ai`, `specification/0.1/basic/mqtt_transport.md`) over A2A
v1.0.0 and is **Core Conformant**: retained discovery and presence, the
request/reply topic model and correlation, QoS 1, requester-generated `Task.id`
with retry dedup, multi-turn continuation, the mandatory error mapping, and
`a2a-status`/LWT. It also implements the profile's observable SHOULDs:
`GetTask` after a stalled stream, a 30 s `stream_idle`, and the optional
`a2a-context-id` property. Out of scope and not claimed: the Extended profiles
(pool/shared-subscription dispatch, JKU trust, broker-managed status,
observability, native binary artifacts, `ubsp-v1`, push notifications), durable
task state across a restart, non-text parts, and a synchronous standalone-Message
reply path. Two implementation notes: the auxiliary requester connection uses
`{org}/{unit}/{agent}-out` (the identity connection is exact; the suffix names
the requester role), and the private `message` type is a harness chat shape, not
a profile message.

### 9.3 Agent tasks

`plugins/agent_task/` runs one turn of the caller's conversation on a
description, and the answer comes back as a message. It is separate from a
subagent: it runs on a clone of the maker's context, has no address of its own,
records nothing, and accepts no second message.

Properties:

1. **A cap and a name.** `agent_task_start` refuses a duplicate name and refuses
   past `agent-task.max_running`. The name is the agent's handle — its own
   conversation, its subagents and the window all address a task by it.
2. **A task's turn is not written to the agent's database.** The task reads a
   clone of the maker's context and adds nothing to it. What the conversation
   gains is the answer, which the agent server records as a message.
3. **Every failure is a message.** A turn that raises still has an answer to
   deliver.
4. **Background work and a nested task are the prompt's rule.** A task runs one
   turn and has no second turn to receive a result from `_async` or from another
   task; the opening template says so, and the tool list is not filtered.
5. **`finished` means delivered.** A task's answer is its product and no copy is
   kept, so a task whose answer reached no window is `failed`.

**The maker is a conversation, not an agent name.** `agent_task_start` reads the
caller's whole `(agent, subagent)`, and that pair is the identity every hop the
task makes carries: the prompt (`system.j2` for an agent, `subagent.j2` with the
role for a subagent), the model, the context restored and rebuilt, the plan, and
the tools' binding. A subagent that starts a task is asking for a turn of *its*
conversation. A subagent's task whose record is gone fails instead of running: a
directory without `subagent.yaml` is not a conversation, and restoring it would
recreate the record-less directory that blocks the name.

A task runs one turn, so it makes one rebuild; zero would make it the one turn in
the system whose context nobody decided. That rebuild is **private**: the task's
`_context_rebuild` runs on the clone `restore` read and publishes nothing, so a
task cannot move the conversation's live-context list; the list a restart
replays is the principal's. Its opening message is the shipped `agent_task.j2`:
nobody is waiting, nobody can answer a question, the message sent when the task
stops is the whole of what comes back, and the model must not use `_async` or
start another task. It does not have the turn that started it, because `restore`
reads the stored turn list and a turn is written only when it ends; the
description must restate the last exchange.

**The four live-conversation tools are the task's own.** `_check_new_input`,
`attach_image`, `_turn_compact` and `_context_rebuild` act on a live turn's
message list — and a task is a live turn, but not the one in the agent server:
the same conversation id, a different list, in a different process. Their
declarations stay the hub's and their bodies are the task's: `_check_new_input`
answers "nothing waiting", `attach_image` injects into the task's own single
user message, `_turn_compact` compacts the task's own view and never the agent
server's loop, and `_context_rebuild` edits the task's clone with
`publish=False`. The task runs the same automatic passes as an agent server
turn — the rebuild at its first boundary, and its own `compact.due` against
`turn.live.last_usage` and the task's window — because a task is one turn and
records nothing: a turn that overflows has no next turn to recover on. Both
servers reach the same rule and failure sentences through
`TurnRuntime.rebuild_arguments` / `compact_arguments`; only the state they adopt
the answer into is theirs. The agent server refuses a call that names a turn
other than the loop's running one, and the hub forwards the caller's turn token
so that is decidable.

The answer is delivered as a message on the `agent-task:<name>` channel, with an
`[AgentTaskResult:{…}]` marker. **A task is `finished` only when its answer
reached a window**: if no window is showing the conversation, it ends `failed`
and the log records why. A subagent has no window of its own (§4.5), so a
subagent's task answer is delivered to its agent's window.

**A task can be watched while it runs.** The task server's management tool
`watch(name)` is the same read a subagent's conversation has (§4.5.6): an empty
`Snapshot` with the agent's plan, a `TaskBrief` with the description, the one
turn's events, and `Closed` with its ending. A stopped task is answered with the
brief and the sentence its ending carried; a name the registry no longer holds is
a refusal. The turn publishes through the shared watch hub, so a watcher that
joins mid-turn is handed the events already produced.

**The acknowledgement is the write, not the record.** `finished` means the window
took the answer: `hand_over` returns when the words are on their way down the
window's stream, and the window submits them into the conversation, which the
agent server records as a turn when that turn ends. A window that dies between
the two leaves a `finished` task whose answer is in no store. Closing that gap
needs a second acknowledgement after the record, which the hand-over design
refuses: it is how one message becomes two turns.

**A probe may start one too.** `slife2 probe --channel tui` starts a task as the
target agent, waits for its state, and reads the delivered message;
`agent_task_status` reports state and never the result.

### 9.4 Scheduled tasks

`plugins/schedule/` starts work nobody asked for at the moment it starts. A
scheduled task is durable, timed, and repeatable; an agent task is ephemeral and
one-shot. The definition and the ledger are the feature.

`schedule/expressions.py` is the only module that knows cron. It owns the
timezone policy and the error contract: `day_or` makes restricted day-of-month
and day-of-week a union, and `strict` cross-field validation rejects `0 9 31 2 *`
before it is stored as an enabled task that can never fire. The expression is
five fields — minute granularity — because the poll's fire window nudges its
reference by one second, a step a six-field cron would cross into a future-dated
fire.

The data model is three SQL tables — `meta`, `scheduled_tasks`, and
`scheduled_runs` — holding a task, a run, and the agent the ledger belongs to.

| State | Meaning |
|---|---|
| `pending` | The run was written; the outcome is not known yet. |
| `ran` | The work finished and its answer reached the conversation (§9.3); the task's `finished_at` is the run's time. |
| `failed` | The work failed or was cancelled, the answer could not be delivered, the run could not be started or followed, or a stopped process left it in flight. |
| `missed` | The fire occurred while no instance was running — no process, or no window showing the agent — so nothing started. |
| `skipped` | An operator or the model closed a run that will not happen. |

A run is named by the moment it was due. The row is written before the window is
asked, so a fire cannot repeat. A window not showing the agent is `missed`; a
terminal that cannot be asked, or a dispatch that fails, is `failed`; a task that
finishes but cannot deliver is `failed`; a run whose outcome does not come back
stays `pending` and the next start's sweep closes it. A fire follows the wall
clock of its task's timezone: a repeated local hour is the same fire only if its
wall-clock reading equals the anchor's, because comparing instants alone would
fire twice in a fall-back hour. `next_run` applies the same rule, so a listing
never promises a fire the poll will not run.

The anchor is a compare-and-set in the ledger's update: a fire not later than the
stored `last_due` leaves it alone, so concurrent fires cannot move it backwards.
An anchor that is not a time (`julianday(last_due)` is NULL, from a hand-edited
ledger) is reset to this pass's moment rather than raising, and the skipped pass
is said in the agent's window.

The ledger is one file per agent. The shared process finds agents by walking
`agents/*/schedule.db` across every known home; a ledger carries its home,
because the home is a property of the request, never of the process. **A ledger
that cannot be opened is damage, and it hard-fails for the agent it belongs to**:
that agent's schedule tools refuse, naming the path and the repair, and the clock
reports it to that agent's window; every other agent's clock keeps running. An
ownerless file is skipped with a warning.

A fire is gated, then dispatched. A due window gets an ordinary agent task named
`<schedule task>@<due time>`, because the agent-task plugin files a task by its
name, so two overlapping runs are two tasks. The schedule plugin is the caller,
so it receives the task id and can close the run when the task ends. No turn is
opened and no model call is spent: a trigger turn would ask a model to press a
button the plugin can press itself. An agent-task plugin that cannot come up is a
scheduler that cannot run, and the launch order reflects that dependency.

The clock is a poll: a fire is noticed up to `pacing.schedule_poll` (30 s) after
its due moment, and a finished run closes on the next pass after its task ends;
`ran_at` is still the task's own `finished_at`. Tasks fire only while slife2 is
running. On startup the sweep closes runs left open by a stopped process — a run
with no task was never started and is `missed`; one that names a task is
`failed` — and the poll records the newest missed fire for each task. A run
caught inside the dispatch round trip carries a dispatching marker written before
the peer call; the sweep resolves it against the agent-task plugin by the run's
name: a running task is adopted, an ended one supplies the ending, and one the
plugin does not hold is `failed`. `schedule_run_now(name, due_at)` re-runs a
missed or failed run; `schedule_run_skip` closes it. Nothing announces it; the
ledger holds the answer.

A task's `timezone` is an IANA name. Leaving it out uses the machine's clock as
it is now, which does not follow a daylight-saving change.

## 10. Terminal

### 10.1 Connections

The terminal is a client and serves nothing. It holds no conversation state, no
provider knowledge, and no API key. It maintains three connections:

| Connection | Client | Direction |
|---|---|---|
| Agent server | `AgentClient` | The window asks: `run_turn`, `transcript`, `models`, `subagents`, `answer_approval`, `switch_model`. |
| Terminal inbox plugin | `WindowClient` | The window is asked: `attach` is held open and messages are pushed down it. |
| Mesh plugin | `MeshClient` | The window holds a place: `join` is held open while the agent is online. |

`plugins/tui/server.py` is an ordinary plugin with one session per agent. The
window attaches to it and holds the call for as long as it is showing a
conversation. Two windows on one machine are two sessions. A session lives
exactly as long as the call that opened it; closing the stream cancels the
handler behind it, so a window that was killed stops counting. A message handed
to an agent with no window is refused by name (`NO_WINDOW`).

### 10.2 Rendering and state

- **Markers are stripped for display** by the reader that knows the vocabulary.
- **The channel prefix is decided in one function.**
- **A tool call is drawn as a line with its argument.**
- **Harness lines come from one place** (`libs/harness/notes.py`), a library's
  rather than the terminal's: a daemon that composes a presence line must not
  import the window's package.
- **Widgets are mutated only in Textual's message pump.** A callback from a
  transport posts a `Message` and never draws: a widget written from the
  transport's task can jump ahead of deltas already queued, so a harness line
  lands above text streamed before it.
- **A tool panel has four states**: `running`, `done`, `error`, and
  `interrupted`. `interrupted` is about the turn rather than the call: the turn
  ended, was cancelled, or failed while the window was waiting, so no result
  exists and none is coming.
- **The plan is a strip above the prompt**, hidden when empty. It is read once
  with the opening history and updated by a `plan_write` call that succeeded.
  Its four statuses are the model's claims, not the harness's measurements, so
  `done` is not drawn in the colour a verified tool result wears. A plan longer
  than the strip's cap scrolls inside it.
- **A question is a strip above the plan**, not a row in the transcript: an
  undecided picker (`Ctrl+S`, `Ctrl+O`) is current state, so it stays fixed
  while the loop keeps writing, and both strips stay visible. The choice writes
  no line of its own; `Esc` is silent. A list longer than the strip's cap
  scrolls inside it, with the cursor row kept in view. Only one question is open
  at a time.
- **A connection has two states on screen**: online and offline. The reasons
  live in the status tool. A turn that dies at the transport puts the bar
  offline — the client's link is what it reports, not the exception — while a
  refusal that left the link up is the transcript's error alone.
- **The status bar's `tools` count is loaded/total**, both from the hub's answer
  to one ask. The window replaces both with the pair the last model call was
  built from.
- **The status bar's context figure is the last model call's**, reported by
  `UsageReported` as each call ends and settled by `TurnFinished`. It follows a
  turn's calls rather than waiting for the turn to end, and it is never the
  turn's bill.
- **Model output is plain text.** Nothing a model produces is parsed as markup.
- **A restored conversation is the server's.** `transcript` is read once when the
  window opens; the window keeps no copy. A failed read is not an empty
  conversation. **A stored turn whose body is unreadable is drawn as damage**,
  not as an empty exchange, and the restored count counts only what was drawn
  (§8.1).
- **A rebuilt conversation uses the live drawing calls**, not a second rendering
  vocabulary.

### 10.3 Approval

A tool call carrying `_approve` parks the turn and asks the window through
`ToolApprovalRequested` on the progress channel. The answer is a call of its own
(`answer_approval`), because the turn's channel runs one way and the call cannot
wait on state the server holds per conversation. Losing the race is not an
error: the turn may have ended a moment earlier. A refusal mounts no tool panel,
because a panel that appeared and then said "denied" would tell a reader the tool
ran.

**A request nobody was told about is a refusal, not a wait.** A progress event
that cannot be written means no client saw the question, and a client that hears
it and never answers is bounded by the approval deadline; both refuse the call
and leave the turn running. The row is mounted on the screen in front, so a
watch screen does not hide the question it covers. **A turn's end settles the
row it left pending** — a finish, a cancellation and a failure alike.

### 10.4 Interface language

The terminal is bilingual: English and Chinese. It is the only bilingual part of
the system. Detection is the operating system's answer, read once at import:
Windows' UI-language API (the C locale carries the region, not the interface
language); elsewhere `LC_ALL`, then `LC_MESSAGES`, then `LANG`, most specific
first; Chinese locales select Chinese, everything else English; any failure
degrades to English.

`--lang` overrides detection and is applied before anything is built. The
translation layer is one accessor over a two-language table
(`libs/harness/i18n.py`), shared with the harness lines a daemon composes; there
are no catalog files and no dependency. A missing key or placeholder raises at
the call site rather than rendering a blank slot.

Always English, whatever the flag says: the system prompt; harness and plugin
tool schemas; job schemas and result strings; every log line; external text; and
tool labels, because a translated label would put the screen and the tool list in
disagreement. Key caps (`Esc`, `Ctrl+S`) remain English. A counted noun is two
entries in the table, because `str.format` has no plural machinery.

A daemon composes the lines it sends (a peer coming online) in its own process,
so it follows the OS locale rather than `--lang`; a machine whose OS language
and `--lang` agree never sees the difference.

### 10.5 Unattended operation

An agent is on the peer network while a window holds its `join` call. Everything
else already works without a terminal: a channel's hand-over goes to the window,
a scheduled fire asks the window, and a task's answer goes to the window. An
agent nobody holds is offline, and that is the one thing a terminal was required
for.

`--headless` runs the same `SlifeApp` with Textual's headless driver: the same
connections, the same queue, the same turns, the same prompts, nothing drawn; the
start-up report is still printed. No branch of the window is conditional on the
flag. The model is told that nobody is there: the flag is written into the
agent-name claim, which the agent server reads to render the system prompt. The
same sentence names the tools a reply leaves by, because prose reaches nobody.

The output is deliberately nothing. Turns are recorded, so a window opened later
restores the conversation, and presence is on the mesh where peers see it. Ctrl+C
stops a headless run; the broker's last will publishes the offline card.

`slife2 probe` has two roads and one answer contract. `--channel a2a` (the
default) is an A2A requester: one `task_request` to whichever window holds the
agent's name, and the model's `task_response` is the answer. The target's mode
changes its prompt and approval handling, not the probe; its retry ladder is the
readiness wait for a window that has not joined yet. `--channel tui` asks through
an agent task (§9.3), and its delivered `agent-task:<name>` message is the
answer. The container's one-shot entrypoint runs the default A2A road.

**A raw TUI submit is not the answer contract.** A channel's answer is the turn's
answer to everything folded into that turn, so no assistant text can be
attributed to one injected prompt. The TUI road therefore asks through a task's
named delivered message, and the A2A road's `task_response` is the per-request
completion.

## 11. Configuration and environment

### 11.1 Configuration file

One `slife2.yaml` inside a home. Its sections are the system's vocabulary:

| Section | Reader |
|---|---|
| `servers:` | The launcher, every plugin, the terminal, and the CLI |
| `providers:` | The agent server, model backends, and the terminal's status bar; `providers.default:` names the starting model |
| `agent:` | The agent server and agent-task server |
| `agent-task:` | The agent-task server |
| `subagent:` | The agent server and the local network plugin |
| `embeddings:` | The context store, the cabinet, and the tool hub; `embeddings.default:` selects the provider |
| `context:` | The context plugin |
| `tool_load:` | The tool hub |
| `tool_result:` | The harness, the cabinet's readers and `dump_read`/`dump_grep`, and the agent-task turn's cut |
| `mcp:` | `slife2-mcp-tools`, and the tool hub for its source names |
| `rest-api:` | `slife2-restapi-tools`, and the tool hub for its source names |
| `cli:` | `slife2-cli` |
| `skills:` | `slife2-skills` |
| `media:` | `slife2-media` |
| `file-tunnel:` | `slife2-file-tunnel`; `file-tunnel.default:` selects the provider |
| `peer_agent:` | `slife2-a2a`, the terminal, and the CLI |

`default:` is a key of the section it chooses from — `providers.default`,
`embeddings.default`, `file-tunnel.default` — never a section of its own. Every
section has one owner, and that owner reads the section in full. A value another
process also reads is a shared fact, stated rather than accidental: the plugin
reads its own address from `servers:`; the agent server resolves
`provider/model` from `providers:`; the agent-task server reads `agent:` because
a task is the agent working; the terminal holds its own `peer_agent:` mesh
connection. No other section is read by a second process.

`wsl_port_offset` is a top-level number rather than a section. It moves every
configured server port, and only inside WSL's mirrored networking, where the
Windows and WSL loopbacks are one address space and an unshifted second stack
would be silently reused. It defaults to 100 there and `0` disables it. The
shift is applied by the config loader, so every process resolves the same
addresses; the MQTT broker is not shifted, because it is the mesh's rendezvous
rather than an instance endpoint. **`wslinfo --networking-mode` is the platform's
answer; when it cannot answer, the mode is guessed from
`/sys/class/net/loopback0` and `slife2 status` says so in `warnings`**, because
that guess decides every port.

An absent section takes its built-in default, not an error. For a capability
whose default is empty that means *off*: no `peer_agent:` means the machine is
not on the peer network; no `media:` providers means the four media tools are
present but refuse, saying what to add.

`providers:` and `embeddings:` are not switches and have no defaults. The file
must name at least one provider and at least one embedding provider; an absent or
empty section is refused. A built-in provider was a machine calling an address no
file mentioned, and the built-in local embedder was an endpoint the file never
named; both are gone.

A present but invalid section is refused by name at load: an unknown provider, an
unknown `api`, a port that is not a port, a `servers:` host that is not a host or
not loopback, or a switch written as anything other than `true` or `false`
(YAML 1.2 made `no` and `off` plain strings). **A key no reader takes is refused
too**: a misspelt `max_iterations` loaded as "no cap" is the silent ignore the
loader forbids. An `mcp:` entry may name only the fields of its own transport
(`command`/`args`/`env`/`cwd` for stdio; `url`/`headers` for http), and an
`mcp:` or `rest-api:` name may not be `skills`, `cli` or `job`: those are the
document sources' names, and an entry under one would shadow the source in the
hub. An empty value of a required entry key is the same absence as leaving it
out.

**Loopback is the security boundary, and it is a check.** Every plugin is
unauthenticated Streamable HTTP, so `servers.<name>.host` takes `localhost`, a
`127.0.0.0/8` address, or the loopback IPv6 address `::1` only, and a
hand-started server's `--host` is refused the same way before it binds: a bind
beyond this machine would let the network call `job_set`, `mcp_set` and
`model_set` — arbitrary code and configuration edits — with no credential. An
IPv6 host in a URL is bracketed (`http://[::1]:24000/mcp`), because a bare
literal beside a port is not addressable.

The file is read per turn for everything a conversation can see, and cached by
identity: a reader asks whether an edit happened with one comparison rather than
a watcher or a lock.

### 11.2 Secrets

A value may be a literal, used as written; or `${VAR}`, resolved through the
shell environment, then credstore's OS keyring. Resolution is lazy: a process
nobody calls never touches the keyring, and the loader defers every `${VAR}` in
a tool server's `env:`/`headers:`, a `rest-api:` entry, a tunnel's options and a
`skills:` entry. The process that consumes the value resolves it, so a locked
keyring fails the one call that needed the value rather than every config load on
the machine, `slife2 down` included.

**An unresolved reference is left verbatim, and the use fails.** Loading does not
block on a missing secret; the consumer decides: a provider or media call refuses
naming the reference, a tool server's entry is left out of the connection with a
warning, so an absent variable reaches a child that knows how to handle one, and
a skill reports it missing. A store that is present and refuses — a locked
keyring — raises instead, because reading that as "not found" would send
`${VAR}` as the secret. `config.unresolved_reference` is the single reader of
"this is still a reference"; a reference MUST NOT travel as the secret.

**Output protection covers credstore's values, and only those.** A credential
resolved from credstore is registered in the resolving process and masked in
every surface that leaves it: tool results, tool descriptions, refusals, and log
lines, uvicorn's access log included. The mask shows the first four and last four
characters when at least as many stay hidden, otherwise `***`; a value with a
control character is masked whole, and a value shorter than eight characters is
not registered. An environment variable or a literal in the config is the
operator's; protecting every secret was attempted, and the variety of shapes made
it a maintenance burden.

### 11.3 Data directory

A home is a data directory: `slife2.yaml`, `AGENTS.md`, `skills/`, `jobs/`,
`agents/` (one directory per agent), and `agents/tools.db` (the shared
catalogue). `--data-dir` and `SLIFE2_DATA_DIR` name the home. A child process is
spawned with the resolved directory, so a relative path cannot be re-resolved
underneath the working directory it is given.

A directory may hold several agents. They share its configuration, its skills,
and its catalogue; the tool set is the configuration's. A name is not a property
of a directory: `--agent jack --data-dir D` names the agent and says where its
files go, and which directory a name used previously is not remembered.

The default location is the working directory when its `pyproject.toml` names
this project; every other working directory is production and uses `~/.slife2`,
and a subdirectory of a checkout is production too. A marker that cannot be read
is refused rather than read as an installation.

### 11.4 Runtime directory

The runtime directory is `~/.slife2/runtime`; `SLIFE2_RUNTIME_DIR` overrides it.
It contains:

```text
<runtime>/
  servers/      a record per running server: pid, url, config
  logs/         what each server printed, one file per server and per
                external stdio entry (plus a rotated <log>.1)
  locks/        per-endpoint start locks
  agents/       agent-name claims
  clients/      which clients use the shared servers (pid, agent, start token)
  homes/        data directories this machine has used
```

The runtime is machine-wide because the servers are machine-wide. A copy per home
could only disagree with the others: a second data directory reporting a stack it
could not see, two launchers cold-starting one port, or a claim lost to whoever
looked in another folder. The runtime is reconstructible: deleting it causes the
next start to rebuild what it needs.

### 11.5 Per-home state in shared servers

A plugin is shared, so the process that writes an agent's file is generally not
the one started with `--data-dir`. The home a call belongs to travels with the
call: `audience.py` puts it in the call's `_meta`; `ServeTheCallersHome`
establishes it for the length of the call; `protocol.Peer` carries it on every
hop. `paths.home()` resolves a file against the caller's home; a plugin's own
configuration, records, and logs keep using `paths.data_dir()`.

A start-up touches no home's stores: per-home work happens on the call that is
for that home — the context plugin syncs a store on first use, with that home's
embedding model, and a file or configuration this build cannot read fails that
call. Starting a daemon is not a reason to walk the machine's other homes.

Everything a plugin derives per home is kept per home: a home's catalogue, the
declarations the hub holds, and the model list it resolves.

A home is created when an agent starts in it. The client records the directory,
creates `agents/`, `skills/` and `jobs/`, and asks the hub for the home's tool
list. That call creates `agents/tools.db`, because the hub is the file's only
writer. A catalogue is complete when it is opened: the hub's own links are the
machine's, and what they last listed is written into every home's catalogue,
seeded as the home opens. Only declarations are per home.

### 11.6 Family connections and per-home state

A family's connections are per name, not per home. An `mcp:` or `rest-api:` entry
is a process on the machine; whether two configured entries are the same server
is decided by the name.

- The first home that switches an entry on starts it.
- Every other home naming it holds the same link and reads one listing.
- What stays per home is what its own section says: `enabled`, `description`, and
  therefore what it declares.
- One home switching an entry off does not switch it off for another.
- Two homes naming one entry with different commands is reported; the running
  server is used.
- An edit through a management tool reconfigures that server for every home
  holding it.
- A link is closed when the last home lets go.

### 11.7 Records and logs

A daemon writes a log under the runtime directory and records that it is running.
`status` reads the records; `down` stops only what it can prove is ours. A failed
start reports the end of the log, because a real error message is worth more than
a status code. The startup report goes to stdout; a plugin's own output goes to
its log. The server's own first line names its address, pid, data directory and
config, and a line at shutdown closes the run.

**The record is part of the start.** A server or client whose record or claim
cannot be written does not run unrecorded: `status` would not see it and `down`
would have no pid to stop, so the start fails naming the path. A record that
exists and will not parse is the same damage read from the other side —
`status`, `down` and `live_clients` fail naming it rather than reporting a live
daemon as absent. A missing record is the ordinary first start.

**Logs are for tracing, so every line is placed.** The format is
`time level logger: [agent/subagent turn=<token>] message`. The conversation is
established by `TraceMiddleware` from the call's `agent`/`subagent` arguments and
the turn from the call's `_meta` (`audience.TURN`); both are carried in
`contextvar`s and put on every record by `TraceFilter`. A line outside a turn has
no token, and a line outside a call has no prefix. The prefix names the
conversation the call is *about*; for the a2a send tools that is the recipient,
not the caller.

One line is written per turn on every ending — success, failure or cancellation:

```text
turn_done agent=… subagent=… turn=… token=… model=… stop=… iterations=… \
prompt_tokens=… completion_tokens=… cache_read=… cache_creation=… \
messages=… elapsed=…
```

`prompt_tokens` and `completion_tokens` are the bill's two halves; `cache_read`
and `cache_creation` are its breakdown, so a cache-hit ratio is read from this
one line. A cancelled turn reports the model calls that completed before the cut.

`token` is minted when the turn starts, before the turn writes anything, and
travels on every hop's `_meta`; every process's lines for that turn carry it in
their prefix. `turn` is the stored row's rowid, which exists only once the row
lands — a cancelled turn can have `turn=-` and still have a token. Reading one
turn across the agent, context and model logs is a grep for its token;
`turn_done` is where the token and the rowid are joined.

One line is written per **foreground** tool call:
`tool_call name=… ok=… elapsed_ms=… result_chars=…`. Arguments and result text
are not logged; the transcript is where words live. A background call is logged
by `libs/harness/async_tasks.py` when it is scheduled and when it finishes.

The root level is INFO. `SLIFE2_LOG_LEVEL`, read per process, raises it; a value
that is not a level logs at INFO and is named in `slife2 status`'s `warnings`.
HTTP libraries, provider SDKs, FastMCP and `uvicorn.access` are pinned to
WARNING: their debug output can carry credentials, and an access line is one line
per MCP request. `uvicorn.error` stays at INFO for the run boundaries. The
FastMCP banner is disabled.

A log is written as UTF-8 (§12.3) and masked at the formatter (§11.2). At startup
a log larger than its ceiling is renamed to `<log>.1`; the previous run is kept,
not deleted. An external stdio entry's child writes its own log under `logs/`
(`mcp-<name>-<digest>.log`), so a crash or start-up chatter is attributable to
its entry.

### 11.8 Configuration edits

A model may edit the sections it can read. Every management tool writes through
`libs/host/configfile.py`, the single writer described in §4.1; the rendered
document is staged beside the file and validated by the same loader a start uses
before it is published, so a refused entry is never written. An atomic swap waits
briefly for a Windows reader that holds the destination open rather than failing
the edit; a destination that stays locked still raises.

A `set` writes the whole entry, not a patch. A field left out is a field the
entry does not have, which is what makes switching an entry from `url` to
`command` possible. `set_enabled` changes one field and reads the entry first. A
write preserves an existing file's mode; a mode that cannot be preserved is
logged rather than silently tightened.

**A section's explanation is written above the section key.** A comment above the
section's first entry belongs to that entry, and goes with it when the entry is
removed. ruamel keeps both in one slot, so the file follows this convention and
the writer reads the two positions as the two things.

The model configuration is live: the agent server resolves a reference per turn.
`model_switch` is the one edit that moves the current conversation; a turn in
flight finishes on the model it started with.

There is no approval gate before a write. `mcp_set` with a `command:` records a
program slife2 will start; the write is legible but not gated. The model's
`_approve` is the only confirmation mechanism, and it is model-driven.

## 12. Operations

### 12.1 File tunnel

`expose_local_image` returns a public HTTPS URL for a file, for a vision model or
a chat API that cannot read the machine's disk. The plugin holds a registry of
token to file, pinned to the file's device, inode, size, and modification time. A
link stops working when the file changes, when `unexpose_local_image` is called,
or when the process exits. A share outlives the turn that made it. A link belongs
to the conversation that exposed it: two conversations exposing one file hold two
tokens, each revocable only by the one that made it. A relative `path` is
resolved against the caller's data directory, not against the daemon's working
directory, which is the reconstructible runtime directory. The response is built
from an open handle: the pin, the length and the bytes served are one observation
of the file, so a file replaced while a request is in flight is never served
under the old token.

**A published URL is not a working one.** A transport that has lost the edge
keeps its URL and answers HTTP 530, so reachability is checked separately.
`expose_local_image` refuses on `reachable: false` and on `null` hands the URL
out with the caveat. A merely unreachable transport gets a heal window before
being respawned, and a stop that lands while a start is establishing the
transport invalidates that attempt: the worker releases whatever it established
instead of publishing after `stop` returned. `tunnel_status` reports
`probe: failed`, `inherited`, and — for a CLI provider — `binary_source`
(`named` or `path`). A CLI provider's binary is resolved on PATH; one that is
not there is a hard failure naming the install and the `binary:` option.

Three providers are supported:

| Provider | Requirement | Behavior |
|---|---|---|
| ngrok | `NGROK_AUTHTOKEN` | Stable dev domain; free tier shows an interstitial to browsers. |
| localhost.run | `ssh` | Random rotating hostname; TLS ends at the provider. |
| cloudflare | `cloudflared` | Random hostname stable for the process. |

An unknown `active:` is a config error: the loader refuses it and names the
providers this build has, and a provider named under `providers:` is checked the
same way. Which tunnel a machine publishes through is the operator's reachability
decision, so an unrecognised name is a mistake rather than a version to tolerate.

**An inherited URL is another process's tunnel.** `SLIFE2_FILE_TUNNEL_URL` makes
the URL active and leaves it alone (Appendix C), but its share listener is the
other process's, and this process's tokens are not in that listener's map —
`expose_local_image` refuses rather than handing out a link that answers 403.
Load and the health monitor therefore start nothing for an inherited URL and
never write the variable the parent passed down.

The tunnel is pointed at a loopback listener that serves `GET /share/{token}`
and nothing else, on a port of its own; the MCP endpoint keeps
`servers.file-tunnel.port` and is not on the public hostname. A tunnel forwards
a port, not a path, so this makes the published surface the one route: `/mcp` on
the tunnel's URL is a 404. The 30-hex-character token is the whole capability.

### 12.2 Media

A media provider is not a chat provider. Its models are declared per kind: image,
video, tts, asr. A vendor may serve several kinds, so `kind` is a fact about the
model and `api:` selects the wire adapter.

`media.defaults:` names a `provider/model` per kind. A name that does not resolve
— an unknown provider, an unknown model, or one of another kind — is refused at
load, naming the `media.defaults.<kind>` line. It is not replaced by the first
model of the kind: a default is the operator's statement, and a typo must not
spend a paid call on a model nobody named.

Artifacts are written under the conversation's work directory by default: the
agent's `media/`, or a subagent's `subagents/<name>/media/`. A caller that names
an `output` path writes there instead — a relative path is relative to that
directory, an absolute one is used as written. They never go to the process's
working directory, which is the runtime directory and reconstructible. **An
`output` file is replaced only by a complete artifact**: a generated artifact is
renamed onto it from the same directory, and a transcript is written beside it
and renamed over it, so a failed write leaves the file that was there whole. The
file-to-file pair is `transcribe_audio` (audio `input` → transcript `output`, or
the text when omitted) and `text_to_speech` (a text file in `input` when `text`
is empty → the audio `output`). Every media tool answers with the path as text.

Two adapters are supported: `dashscope-aigc` (images, video, speech,
transcription) and `openai-images` (image generation only; it refuses the other
three by name).

### 12.3 Text encoding and time

Content SQLite refuses is normalised first, so an unencodable character does not
fail the turn: a valid UTF-16 surrogate pair is put back together, and a lone
surrogate becomes U+FFFD. The UTF-16 codec is the one in the standard library
that distinguishes the two; encoding with `errors="replace"` would turn an emoji
into `??` and a lone surrogate into a question mark indistinguishable from a
typed one. A spawned Python child is given `PYTHONIOENCODING=utf-8`, or every
non-ASCII line reaches `slife2 logs` as replacement characters.

Every timestamp comes from one place: local time with an offset, seconds
precision. A range compares instants as `julianday`, because two offsets around a
DST change do not sort as strings in the order they happened.

### 12.4 Timeouts

Every await that can block has a bound, and the bound belongs to the layer that
awaits it. A callee never sets a total for its caller.

There is no turn deadline and no default tool-call budget. A single number
applied to every tool is wrong for most of them. What bounds a call is the
model's `_timeout`, the tool's own declared timeout, or nothing.

Where a clock is needed and the work can legitimately be long, it is an
inactivity bound rather than a wall clock. The model stream has one: 120 seconds
of silence, reset by every chunk.

`helpers/timeouts.py` is the single source of truth. It distinguishes **budgets**,
which bound an await, from **cadences**, which say how often something runs.
Consumers read values at call time, never into a module constant or a def-time
default, so a test can patch one. `validate()` asserts the relations that fail
silently, such as an outer bound shorter than an inner one, and fails at import.

A retry policy is one `Retry` in `pacing`, driven by one `Backoff`: the pause
grows from `base` to `cap`, and a run of failures is cleared by exactly one
event — an attempt that did not fail.

A gate (`tests/test_no_magic_timeouts.py`) rejects a time-style literal anywhere
in `slife2/` unless it is in the registry or carries a `# timeouts:` note. In
`tests/` only two rules apply: a module-level constant with a time-style name,
and a registry lookup used as a function-argument default.

### 12.5 Daemons outlive builds

Reuse is by identity: the launcher attaches to whatever answers to the expected
name, whichever build started it. The daemons on a machine are its own — the
first starter's build is what runs — and a later build uses them rather than
stopping them; `status` reports each daemon's build. A tool surface that differs
fails at the call that uses it. A rename of a server or a tool must be
accompanied by a stop.

An internal wire carries no compatibility path for a previous build. A progress
line requires every field this build's encoder writes, and a store from another
schema is refused rather than migrated. The failure is reported where it
happens, not prevented by a compatibility layer.

## 13. Project structure and development

### 13.1 Layers

The layering rule is stated in §2.5. Its purpose is that a module's kind is
visible from its path: a reader who wants the loop opens `libs/turn/loop.py`; a
reader who wants the process that runs conversations opens
`plugins/agent/server.py`, and the conversations it runs are `core.py` beside
it. The cost is one more directory level.

Every process in the tree is described outside it by one row of `PLUGINS`, which
holds its name, probe, and port. `pyproject.toml`'s console scripts are the third
and last place a plugin is named, and they exist for a person rather than for the
launcher. Every daemon is started as `python -m <module>` in the interpreter and
virtual environment that spawned it, not through a script on `PATH`.

### 13.2 Test tree

`tests/libs/`, `tests/clients/`, `tests/plugins/`, `tests/helpers/`, `tests/tui/`,
and `tests/e2e/` mirror the package: one directory per layer, one module per
module. The files at the top level — `test_smoke.py`, `test_install_scripts.py`,
and `test_no_magic_timeouts.py` — are the gates that span the tree.

Shared doubles live in `tests/fakes.py`: a stub model, a stub embedder, fake
window and mesh clients, and factories (`upstream_server`, `plugin_transports`,
`config_file`). A test module MUST NOT import another test module for a fixture;
that is how two lanes end up disagreeing about the shape of a double.

The e2e lane's own edges live in `tests/e2e/edges.py` — the stub provider, the
iLink bridge, the MQTT broker, and the terminal the stack hands messages to —
and `tests/e2e/conftest.py` holds the fixture that starts the stack every
scenario is handed. They are apart from `tests/fakes.py` because they bind
ports and start threads: a fast-lane test that reached for one would have left
its lane.

### 13.3 Test lanes

| Lane | Command | Contents |
|---|---|---|
| Fast | `uv run pytest` | Unit and integration tests, every module, across cores. Excludes `e2e`. No API key, no running server, no fixed port. |
| Unit | `uv run pytest -m unit` | Fast, isolated unit tests. |
| Integration | `uv run pytest -m integration` | Tests that bind a socket or spawn a process. Each binds an ephemeral port. |
| End-to-end | `uv run pytest -m e2e` | The whole stack as processes: the daemons, a real launcher, a configuration on disk, and the three external edges — the model provider, WeChat's iLink bridge and the MQTT broker — stubbed at their own protocols. |

The fast lane drives servers over FastMCP's in-memory transport: real MCP
messages and real progress notifications, with no port bound.

The e2e lane proves what no in-memory test can: the launch order works, every
child reads the same configuration, a turn crosses every process boundary, a
model's tool call is routed by the hub to the plugin process that owns it, the
answer arrives both as a result and over the progress channel, and the
conversation is still there for the next turn.

**Every way work arrives without a person typing has a scenario there**: a
WeChat message the bridge serves, a task a peer publishes to the mesh, a
scheduled fire the clock starts, and a subagent's turn a watch reads while it
runs. Each starts from the same fixture, the same config and the same data
directory shape, so the channels are proven against one stack rather than four
arrangements of it.

The model is faked at its own protocol boundary in both lanes: a stub backend
in the fast lane, a stub HTTP endpoint in the e2e lane. The e2e lane stubs the
other two external edges the same way — the iLink bridge at HTTP and the broker
at MQTT — and nothing in between is faked at all. A change to what the system
does with a model, a bridge or a broker cannot pass unseen by changing only the
fake.

### 13.4 Static checks and CI

- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run pyright` (project code only; tests are excluded)

CI runs on Ubuntu, macOS, and Windows with Python 3.13: a wheel build, an
assertion that the interpreter can load SQLite extensions, lint and format
checks, type checking, the fast lane, and the e2e lane. The suite is the gate;
both lanes pass before a change is done (Appendix A, invariant 13).

## 14. Open issues and deferred capabilities

### 14.1 Open issues

None. The whole-codebase audit (2026-11-17) is closed: its design changes were
made on 2026-11-18, and each resulting rule now lives where its subject does. A
new issue whose fix is a design change is recorded here with what it costs and
how the current code behaves, rather than carried as silent debt.

### 14.2 Deferred capabilities

The following are not implemented in this version:

| Capability | Why deferred | Current behavior |
|---|---|---|
| Durable inbound WeChat messages | The bridge re-serves a batch only until its ack cursor advances; holding one without a durable queue would re-run every already-delivered message on reconnect, and that queue is a capability of its own. | The cursor advances before the hand-over, and a message past `MAX_INFLIGHT` is dropped for good; a redelivery is deduplicated only for `pacing.wechat_dedup` (30 s). |
| Durable interrupted tasks | The continuation state is process-local, like the bridge and reply topic; the profile does not require persistence. | A restart loses a paused task; the peer can cancel or re-send. |
| Markdown rendering | A taste decision about the transcript, not a system gap. | Model output is plain text. |
| Delta coalescing | One progress notification per token is acceptable on loopback. | One progress notification per token is sent. |
| A cap on total agent-task work | `max_running` bounds concurrency; a rate is the missing number. | Total work over time is unbounded. |
| A cap on the number of conversations | The inbox is capped; the number of loops is not. | A client can create conversations without a machine-wide limit. |
| A cap on scheduled tasks | A cron is bounded by time; a runaway is many rarely firing tasks. | `schedule_list`/`schedule_remove` make it visible and stoppable. |
| A conversation id on the turn record | Adding a column means deleting every existing `turns.db`. | No per-turn conversation id is stored; the file name is the id. |
| One config reader | Three readers differ in wording for real reasons. | A reader may not recognize the same problem twice. |
| Selective re-embedding | Per-row content hashing would keep unchanged vectors across a rules change. | A model change re-embeds every row. |
| A cross-encoder rerank for the catalogue and cabinet searches | The context recall is the one whose bad candidate persists into every later turn; both searches are one-shot reads the model can repeat, and their pages order words-first on purpose. | Both use RRF and keep that page order; the cross-encoder (`rerank_model`) is wired to the context recall only. |
| A policy that forces approval | The model-driven `_approve` exists; a named list does not. | `skill_set` and `mcp_set` rely on model judgment. |
| Device-code OAuth for tool servers | `${VAR}` headers cover a token somebody already has. | A server that needs a browser login cannot be configured. |
| A bound on tool-call arguments | Arguments are the model's own words; shortening them is a different problem. | A model can write an unbounded argument into the conversation. |
| Stopping a server started elevated | `taskkill /F /T` opens the target with `PROCESS_QUERY_INFORMATION`, which UAC refuses across integrity levels; the alternative — a Toolhelp walk with `TerminateProcess`, which needs only `PROCESS_TERMINATE` — is a second kill path rather than a flag. | Each such server reports `pid N did not exit; the record is kept`, and a `down` run at the server's integrity is what can still find and stop it. |
| Reaping a tool server's spawned processes | A process started through a tool server's own session (`desktop-commander__start_process`) is that server's: slife2 makes one MCP call and the call is over before the process ends, so nothing here holds a handle to it. A generic reaper would have to know which tool names spawn processes — one server's vocabulary inside the agent. | The process outlives the turn, the conversation and a subagent's removal; it ends with the stack, which stops the server's tree, or when the model kills it with the server's own tool. |

### 14.3 Candidate capabilities

The following are not scheduled. Each is a capability a mature agent harness
carries, and the row says what it would add and what the system does without it.
The order is a suggested priority.

| Candidate | What it would add | Why it is worth doing |
|---|---|---|
| A question the person answers | An `ask_user` tool, and `_approve` routed to every channel that has a person: the window, WeChat, a peer. | Only the TUI can answer today; a WeChat or peer turn that asks is granted silently. Human-in-the-loop is the difference between an agent that checks a destructive step and one that performs it. |
| WeChat media | Inbound image, voice note and file become an attachment, a transcription or a cabinet document; an outbound reply can carry an image or a file. | The bridge is text-only in both directions. The media server already transcribes and the cabinet already stores images, so the missing half is the bridge. |
| A spend budget | A `budget:` section (per turn, day or conversation), a usage ledger, and a hard stop. | Usage is measured per turn and logged, but nothing bounds it. On a paid model an unbounded loop is a bill. |
| Scheduled catch-up | A per-task policy for fires missed while no instance ran, instead of a manual `schedule_run_now`. | Cron users expect anacron-like backfill; a laptop asleep at the due moment currently loses the run. |
| Store-and-forward on the mesh | A durable per-member inbox and read cursor — MQTT persistent sessions with QoS-1 events, or an application-level store under the member's directory — so direct messages and group fan-out are held while a member is away, replayed when it returns, and acked so a replay is not answered twice. | An agent is absent whenever no window is showing it, which is ordinary rather than exceptional. Today a group message to an absent member is lost with no trace, and a `peer_agent_send_message` call ends as `unreachable` after the retry ladder, so the sender cannot tell "not heard" from "heard and not answered". WeChat holds messages for an absent recipient; the group's casual contract is the design question this raises, because a backlog needs a rule for what a returning member answers. |
| Retrieval over files and dumps | Chunk and embed saved files and dumps so a search reaches their contents. | §8.3 leaves this out on purpose; documents are the largest corpus an agent saves and the one it cannot search by meaning. |
| Sandboxed jobs | Run `jobs/` code in a child process with declared filesystem and network limits. | Jobs are arbitrary code with the caller's identity. Least privilege is the standard answer, and the plugin boundary already exists to carry it. |
| Tool progress and cancellation | A long tool reports progress down its call; the window draws it and can cancel that call without ending the turn. | A long media or shell call is a spinner today, and the only lever is `Esc`, which ends the whole turn. |
| Branch from a turn | Start a new conversation from any stored turn id, leaving the original intact. | Checkpoints are the standard undo an agent user expects; exploration and retry become possible without resetting the conversation. |
| An eval lane | Recorded conversations, a provider fixture, and metrics for tool choice, recall and cost, run when a prompt, a tool schema or a model changes. | A stub model cannot catch a regression in what a real model chooses; eval-driven development is how the prompt and tool surfaces are changed safely. |

## Appendix A. Invariants

Each invariant is stated as an assertion. A change that violates one is a defect.

1. **A plugin is an MCP server over Streamable HTTP**, started and shared by the
   launcher, honoring the contract: state keyed by conversation key, an
   audience on every model-facing tool, a checkable identity, and one row of
   `PLUGINS`.
2. **No capability holds a credential where it is not used.** No provider SDK is
   imported by the agent loop, the terminal, or a library. No key is read outside
   the process that needs it. A reference that did not resolve never travels.
3. **Nothing under `plugins/` is a library or a helper.** Every directory there
   has a `main` and a `SERVER_NAME`; the name is read from its `PLUGINS` row, and
   the `main` is a call to `serve_plugin` — directly, or through the model
   backends' `serve_backend` wrapper, which is the same startup path.
4. **The loop is a pure function over a message list.** It owns no conversation,
   no socket, and no clock of its own.
5. **A history is one every provider accepts.** An assistant message's tool calls
   are all answered. A harness pair is written with nothing awaited between its
   halves. The id joining a pair is acceptable to every provider as a call id.
6. **A reading is never a verdict.** An empty answer, an unreadable payload, and
   a silent peer are three different facts, and each is named where it is read.
7. **A fallback is temporary: it heals by itself and is said, or the operation
   that needs the damage repaired hard-fails.** Every remaining fallback is
   registered in Appendix C with its outlet; **a log line is not an outlet**.
8. **One rule, one place.** A name, a path, a cap, a sentence, or a vocabulary
   that two modules need is stated once and imported, never copied.
9. **The configuration file is the only description of what exists.** Every
   process reads it as it is now, not as it was at boot, for everything a turn can
   see.
10. **A message that arrives becomes a turn, and the turn records where it came
    from.**
11. **A client proves which server it reached** before believing anything that
    server says. A payload this build cannot read is refused, not read as a short
    or empty one.
12. **A list the caller sized is bound in chunks.** A query that joins two tables
    names its columns or keeps one table in scope.
13. **The suite is the gate.** Both lanes pass before a change is done. A bug
    found in the field arrives as a test that fails without the fix.

## Appendix B. Plugin registry

The registry is `PLUGINS` in `libs/host/config.py`: one authoritative row per
plugin, holding its key, module, MCP name, probe, port, and whether it is a model
API backend. It is not copied here, because a copy drifts. §2.6 states each
plugin's job. The probe each client uses is derived from that row: the three chat
backends probe `stream_chat`, the embeddings backend probes `describe`, the peer
and subagent networks probe `peer_agent_list` and `subagent_list`, and the other
plugins probe their first stable, argument-free tool.

## Appendix C. Fallback register

A fallback is a **temporary** degraded behaviour, and it is the one kind that is
invisible when wrong. Falling back is allowed; falling back undeclared, or
falling back for ever, is not.

**A fallback heals by itself** — a retry, the next turn, the next refresh, the
next probe — and the failure is said: to the Agent in the answer it reads, or to
the person as a `degrade` line in the window. A fallback that needs repair is not
one: the operation that needs the damage repaired **hard-fails**, naming what to
fix, and is not registered here. A permanent degraded mode is a defect with a log
line.

**`logger` is not an outlet.** A failure that only reaches `runtime/logs/` is not
registered here — it is a defect. The "failure goes to" column names the Agent
(the tool result or harness pair it reads) or the person (a window line, or a
startup/`status` fact).

The four shapes an undeclared fallback takes:

- **A value that cannot be read is not a value that is absent.** Damage and
  emptiness are different. The empty value is often written back, which makes the
  loss permanent.
- **A write that failed is not a write that happened.** A bookkeeping write either
  lands or is reported. Silence becomes a wrong fact.
- **A failure is not a verdict.** A search whose answer could not be read is not
  "nothing matched"; a tool list that could not be decoded is not "no tools".
- **A weaker path is not a substitute for a missing capability.** Feature
  detection must not silently select a degraded branch.

### Self-healing fallbacks

| Situation | Fallback | Heals by | Failure goes to |
|---|---|---|---|
| The context decision fails | Keep the context in hand | The next boundary's decision | The `_context_rebuild` pair (Agent) and a window note |
| A selection is heavier than the prompt budget | Cut the oldest turns until it fits | The next rebuild | The pair's `fitted` / `kept N of M` (Agent) |
| The selection decision fails, answers unreadably, or names an id the view does not offer | Compact nothing; the turn continues on its full view | The next boundary's selector | The `_turn_compact` pair (Agent) and a window note |
| A catalogue hint cannot be asked | The call keeps its generic "unknown tool" sentence, now naming the failed ask | The next call | The Agent's tool result |
| A turn fails with an image attached | Drop every image from the live list, keeping the note | The next turn | The TUI error, the dropped-image sentence, and `[TURN-FAILED]` |
| A tunnel liveness or reachability probe cannot answer | Treat the tunnel as alive for the decision; report `reachable: null`, `probe: failed` | The next probe | `tunnel_status`, and the `expose_local_image` caveat |
| A plugin's declaration refresh fails | Keep the rows from the last refresh | The next refresh | `sources_stale` in `tool_search` / `tool_list` |
| The post-turn trim cannot reach the hub | Skip the trim | The next turn, whose tool list fails loudly on a dead hub | A window `degrade` line |
| A stored anchor is not a time | Reset it to `now()` | The next pass reads a whole anchor | A window `degrade` line |
| A WeChat cursor write fails | Retry on the next move | The next cursor write; three failures are a hard failure, not a fallback | `wechat_check_status`'s `cursor_error` and a warning |
| The window's `attached` answer cannot be read | Answer `None`, not "no window" | The next run | The run recorded `FAILED` with the reason |
| The async registry is over `MAX_ASYNC_TASKS` | Drop the oldest finished tasks | `check_async` names the drop; the work can be run again | The tool's refusal |
| A probe threw | Treat the peer as not up | The next probe | The offline line / warning |
| `taskkill` could not run | Poll the pid | The pid may still exit | A warning naming the cause, and `down`'s detail |
| A job's direct `call` answer is not a mapping | Read the content text; `data` is empty | The next call | The job author's `text` and an INFO line |
| A server reports no name | Probe by tool presence | Every connection is checked | An INFO line naming the probe |
| A card has no presence property | Read it as online, marked `presence: assumed` | The card's next claim | `peer_agent_list` |

### Compatibility, platform readings, and tolerances

These are not degrades: nothing failed that a retry could heal, and nothing needs
repair. They are recorded so a reader can tell them from the self-healing
fallbacks above.

| Situation | Reading | Where it is said |
|---|---|---|
| A record file is missing | Read as absent | `status` reports not running; the ordinary first start |
| Text SQLite cannot encode | Normalise surrogates | U+FFFD in the record |
| A bookkeeping file is already gone | `missing_ok` | An idempotent delete is not a fallback |
| A log cannot be rotated | Leave it in place | A startup warning; the log ceiling is diagnostic |
| A progress line cannot be read | Drop it | The record is the channel |
| A lone surrogate reaches tool text | Escape it | Nothing; the request survives |
| Closing a link fails | Suppress the teardown error | A debug line |
| The interface language cannot be detected | English | The window is English |
| An inherited file-tunnel URL is present | Treat it as active | `tunnel_status.inherited`; an exposure refuses |
| A Windows job forbids breakaway | Start without it | A startup warning |
| `wslinfo --networking-mode` cannot answer | Guess from `/sys/class/net/loopback0` | `slife2 status`'s warnings |
| `SLIFE2_LOG_LEVEL` is not a level | Log at INFO and name the value | `slife2 status`'s warnings |
| A tool fails | `ok: false` with text the model reads | The model reads the text |

Everything not in this register is a defect. A path that substitutes a weaker
behaviour and is not here is an undeclared fallback. A fallback that stops being
needed is deleted rather than left standing, because a designed path that never
runs is a designed path that has stopped being tested.
