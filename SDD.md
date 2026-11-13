# slife2 — Software Design Description

| | |
|---|---|
| **Document** | Software Design Description (SDD) |
| **Version** | 0.4.0 |
| **Date** | 2026-10-29 |
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

This document defines the software design of slife2 version 0.4.0, for a person
who will change the code or operate the system. It states the architecture and
process model, the contract every plugin implements, the behavior of the turn
loop, model backends, tool system and memory, the interfaces between the
terminal, the plugins and the configuration, and the invariants a change must
preserve.

### 1.2 Scope

The scope is the slife2 package, its plugins, its command-line entry point, its
terminal application, its configuration, and its data and runtime directories.
External components are described only where slife2 depends on their behavior:
model provider APIs, MCP servers written by others, MQTT brokers, tunnel
providers, and the WeChat bridge.

### 1.3 Intended audience

- **Developers** changing the agent loop, a plugin, the tool system, memory, or
  the terminal.
- **Operators** deploying slife2, configuring providers and tools, and
  diagnosing the shared server stack.
- **Reviewers** checking a change against the system's invariants.

### 1.4 Definitions and abbreviations

| Term | Definition |
|---|---|
| **Agent** | An identity named by `--agent NAME`, claimed once per machine. It is not a process, a port, or a data directory. |
| **Conversation id** | The pair `(agent, subagent)` identifying the conversation a call is for. The subagent component is empty for an agent's own conversation. |
| **Conversation key** | `(home, agent, subagent)`: the identity a shared server keys per-conversation state by; unique on a machine. |
| **Home** | A data directory holding `slife2.yaml`, `AGENTS.md`, `skills/`, `jobs/`, and `agents/`. A home may contain several agents. |
| **Plugin** | One of slife2's own MCP servers, started and shared by the launcher. An external MCP server is not a plugin. |
| **Family** | A plugin that owns one configuration section or folder and declares the rows it holds to the hub. |
| **Source** | The name a plugin files rows under: an `mcp:` server, a `rest-api:` document, or the whole of a `skills`, `cli`, or `job` collection. |
| **Catalogue** | The tool catalogue: one SQLite file per home holding every row the model may discover. |
| **Conversation** | One agent's own history (`subagent` empty) or one of its subagents' history, stored by the context plugin. It is the unit every shared server addresses and keys state by. |
| **Turn** | One exchange: input, model output, and tool calls, stored as one row. |
| **Channel** | The origin of a message: `tui`, `wechat`, `peer_agent:<peer>`, `agent:<name>`, `subagent:<name>`, `agent-task:<name>`, or `async-tool:<name>`. |
| **Marker** | A machine-readable prefix on a delivered message, of the form `[Name:{…}]`. A marker is content and is stored with the message. |
| **Dump** | A tool output stored whole in the cabinet because it was too large for the conversation. |
| **Runtime directory** | The machine-wide directory holding server records, locks, logs, and agent-name claims. |
| **MCP** | Model Context Protocol. |
| **A2A** | Agent-to-Agent, carried over MQTT using the published A2A-over-MQTT profile. |
| **SSE** | Server-Sent Events, used by the MCP Streamable HTTP transport. |

### 1.5 References

| Reference | Use |
|---|---|
| [README.md](README.md) | User manual and normative description of user-facing behavior. |
| Model Context Protocol specification | Wire protocol implemented by every server. |
| A2A-over-MQTT profile 0.1 (`emqx/mqtt-for-ai`, `specification/0.1/basic/mqtt_transport.md`, over A2A v1.0.0) | Wire protocol used by the peer and subagent networks; §9.2 states the Core conformance claim. |
| OpenAI Chat Completions API | Model wire protocol. |
| OpenAI Responses API | Model wire protocol. |
| Anthropic Messages API | Model wire protocol. |
| `slife2.yaml` | Annotated configuration reference shipped with the project. |
| `AGENTS.md` | The model's guide to its tools: a home document beside the configuration, shipped with the project and edited by an operator. |

### 1.6 Document conventions

- **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are used as in RFC 2119. A
  **MUST** is a constraint whose violation is a defect. A **SHOULD** is a
  constraint that may be departed from only with a stated reason.
- Module and tool names are written in `code font`.
- Paths beginning with `<data>` are relative to a home. Paths beginning with
  `<runtime>` are relative to the runtime directory.
- **Why** paragraphs are explanatory: the decision, the alternative rejected,
  and the fact that settled it. A rule is normative; a **Why** paragraph is not.
  A **Why** paragraph that no longer decides anything is deleted: this document
  states the design, not its history.
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
model, exposes a set of tools to that model, and reaches the outside world
through local processes. The terminal window is a client; the agent loop, tool
hub, model backends, memory, and every capability are MCP servers.

### 2.2 Operating context

slife2 runs on a user's machine and communicates with:

- one or more model providers over their HTTP APIs;
- external MCP servers, over stdio or Streamable HTTP;
- OpenAPI documents, served through an MCP proxy;
- a local or remote MQTT broker for peer agents and subagents;
- the WeChat iLink bridge;
- tunnel providers (ngrok, localhost.run, Cloudflare Quick Tunnel);
- the user, through a terminal UI.

All internal server-to-server communication uses MCP over Streamable HTTP on
loopback. The terminal and the agent server are both MCP clients and servers:
the terminal is a client of the agent server and of the terminal inbox plugin;
the agent loop is a client of the tool hub and of the model backends.

### 2.3 Design goals

1. **Credential isolation.** A provider key, tool token, or other credential
   exists only in the process that uses it. The agent loop and terminal hold no
   provider credential and import no provider SDK.
2. **Provider and tool independence.** Switching model providers changes a URL
   and a configuration entry, not the loop. Adding a tool adds a configuration
   entry or a plugin, not a branch in the conversation code.
3. **One description of the machine.** `slife2.yaml` is the only statement of
   which providers, servers, sections, and capabilities exist.
4. **Explicit failure.** A missing plugin fails the turn. A malformed
   configuration fails the start. A refusal is a failure, not a silently
   degraded success.
5. **Testability without a stack.** The turn loop is a pure function over a
   message list. Servers can be driven over an in-memory MCP transport without
   binding a port.

### 2.4 Constraints

- Python 3.13 or newer.
- The interpreter must support loading SQLite extensions (`sqlite-vec`).
- A plugin is a process. A capability that holds a credential, an SDK, or
  another party's connection MUST be a plugin. A capability that holds only a
  file MAY be a library; if it must also be reached as a tool, the process is a
  plugin and the file handling is a library or a private module.
- Internal state is addressed by a conversation key, not by a session. The MCP
  transport must therefore preserve the conversation identity on every call.
- The model's context window is a hard bound. Every mechanism that adds text to
  a request must have a corresponding bound or eviction rule.

**Why.** The difference is the error boundary: a missing plugin fails the turn
with a named peer, while a file that will not open raises where it is used. A
store has one writer, which is what a SQLite file already is; a plugin around it
would add a hop without adding an isolation boundary. The turn store is
therefore a library opened by the context plugin, the tool catalogue a library
opened by the hub, and the cabinet store private to the cabinet plugin.

### 2.5 Architecture summary

The system has five layers:

| Layer | Contents | Rule |
|---|---|---|
| `slife2/plugins/` | One process per plugin, one directory each | A plugin has a `main` and a `SERVER_NAME`, and the launcher starts it. |
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
those, and the loop does not know where they come from.

### 2.6 Process model

- **One process per plugin.** The launcher derives the set from the configuration
  and starts each plugin at its configured or default address.
- **One process per wire protocol, not per provider.** All providers that speak
  the same protocol share a model server.
- **Shared servers.** Every server is machine-wide infrastructure. A second
  slife2 instance attaches to the running servers rather than starting a second
  set. The last client to exit stops the running servers, unless
  `--keep-servers` is used.
- **One mesh per agent.** The peer network and the subagent network are separate
  namespaces, but each agent has one MQTT identity per network.
- **Daemons are reconstructible.** The runtime directory can be deleted; the
  next start rebuilds what it needs.

Each plugin has one job:

| Plugin | Job |
|---|---|
| `agent` | The conversation loops |
| `context` | The turn log and the context decision |
| `toolhub` | The tool set |
| `slife2-llm-*` | One wire protocol each |
| `mcp-tools`, `restapi-tools` | Outward connections for `mcp:` and `rest-api:` |
| `skills-server`, `cli-server` | Playbooks and the command registry |
| `job-server` | The home's Python functions as tools |
| `cabinet`, `media` | Documents and dumps; images, video, speech, transcription |
| `file-tunnel`, `wechat` | A public file URL; one WeChat login per agent |
| `agent-task`, `schedule` | Background loops; timers and the run ledger |
| `peer-agent`, `subagent` | The peer and local networks |
| `tui` | The terminal inbox |

A provider is a configuration row rather than a process, which is why one
process can serve many providers.

A `SKILL.md` is a manifest this system authors and reads. Its header MUST parse
to a mapping. `install` parses it before writing anything, and `scan` parses a
copied-in directory with the *same* function. A header that does not parse — not
YAML, not a mapping, an unterminated fence, or a malformed `metadata`/`requires`
block — is refused at both gates: the install writes nothing, and the copied-in
skill stays visible as an `error` row that `skill_use` refuses. A `SKILL.md`
with no frontmatter is a prose skill and is not damage. `skill_draft` renders a
playbook and runs the same parser before answering, so a draft is either a
`SKILL.md` `install` would accept or a refusal. `install` replaces a skill whole;
`edit` copies the installed folder forward, writes only the files it names, and
parses the manifest before swapping; `remove` deletes the folder and its entry.

A **job** is one Python function in `<data>/jobs/<name>.py`, exposed to the model
as `job__<name>`. The docstring and typed signature are the tool definition —
there is no manifest — and `job_set` loads the file before swapping it in, so a
broken write restores the previous version. `job_remove` deletes the file and its
row; `job_list`, `job_read` and `job_run` list, read and test one. The jobs
plugin declares the folder as one `job` source, and the hub's per-search refresh
makes a new job findable without a restart.

**The job runtime enumerates no capabilities.** `call_tool(name, arguments)`
reaches the hub's catalogue; `call(server, tool, arguments)` reaches any server
in `servers:` by its key — an agent turn (`send_message`), a raw model call
(`stream_chat`), or any plugin's own tools — and `servers()` reads that config
table. The code runs in the jobs plugin with the caller's identity, holds no
credential, and is not sandboxed; the top-level tool call is the approval point.
The authoring quality loop is the `job-coding` skill: it tells the model to run
the project's `ruff` and `pyright` over the file it wrote and fix the findings.

### 2.7 Key design decisions

| Decision | Choice | Consequence |
|---|---|---|
| Prompt and harness | Trust the model; keep the harness small | The harness adds a marked address and tool pairs, not instructions or hidden policy. |
| Context | Choose a context before each turn | One model call per turn decides what to keep and what to recall. The log is the permanent record. |
| Person work vs machine work | Conversation for model work; code for deterministic work | Scheduling is an expression code reads; delegation is a tool the model calls. |
| Plugin vs library | A process if it holds a credential, SDK, or connection | The agent loop never holds a provider key; files are opened in-process. |
| Configuration | One file, read per turn | An edit reaches the next turn rather than the next restart. |
| Names | Model-facing names are semantic, never protocol names | `peer_agent_*`, `subagent_*`, `turn_read`; no `a2a` in anything the model reads. |
| Failures | Raise or fail the turn; no silent fallbacks | A fallback must be registered, scoped, and stated. |

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

A plugin is an MCP server over Streamable HTTP. It MUST:

- answer the standard MCP JSON-RPC methods, including `tools/list` and
  `tools/call`;
- put its MCP name on the wire so a client can identify it;
- serve a probe tool whose presence identifies the server;
- start through the shared scaffold `serve_plugin`;
- accept the common flags (`--data-dir`, `--host`, `--port`);
- resolve its own address from the configuration or the default.

The scaffold `libs/plugin/protocol.py` owns argument parsing, logging,
configuration, address resolution, daemon records, and server startup, and
installs the middleware every server shares (§11.2, §11.7). A plugin supplies
three hooks: what to build, what to say about its own start, and what it cannot
start without. The model backends use the same scaffold through
`plugins/llm/server_common.serve_backend`, which is a wrapper, not a second
startup path.

A module under `plugins/` MUST NOT call `parse_serve_args`,
`configure_logging`, or `serve` directly. A test enforces this rule by name.

### 3.2 State keyed by conversation key

Every server that holds per-conversation state MUST key that state by the
conversation key `(home, agent, subagent)` (`audience.conversation_key`). The
identity that travels on the wire is the conversation id `(agent, subagent)`;
the home rides beside it in the same `_meta` blob, because one process serves
every home. No caller carries a session handle on the server's behalf.

**Why a name, not a handle.** The MCP revision in use has no protocol-level
sessions: measured against the installed library, `session_id` was a fresh UUID
on every request. A server-minted handle was rejected because anything a caller
must keep can be lost, expired, or wrong after a restart. The conversation id is
a name the caller already has and cannot go stale. Its price is that a name is
guessable; here there is no authorization to subvert, because every server
listens on loopback and the only other party is a process the user started.

Consequences:

- There is no "not found" for a caller to handle: state is created on first use
  and a key never goes stale.
- An idle sweep MAY drop state; the next call recreates it, and the caller
  cannot observe the difference.
- Isolation follows from the key: two conversations cannot reach each other's
  state, and neither can two homes' agents of one name.
- Every call is traceable, because every call carries the id even if the server
  holds no state.

For a model-facing call, the conversation id travels out of band in the call's
`_meta` and is not an argument the model writes; a call that does not identify
its conversation is refused. A plugin's internal tools MAY take it as an
ordinary argument when the caller already knows the conversation — the context
store's `remember`, `restore`, and `rebuild` do. The home travels in the same
blob: a shared server resolves a call's files against the caller's home and
keys state that outlives the call by that home, not against the directory it
was started in.

### 3.3 Audience and authorization

A tool MAY be called by the model only if the tool declares itself model-facing.
The declaration is a namespaced `_meta` key (`slife2/audience`) read from
`tools/list`. The rule is opt-in:

- a plugin's internal tools need no mark and are withheld from the model;
- a tool that forgets the mark is invisible, not dangerous;
- an entry under `mcp:` needs no mark; the operator opted in by writing it.

Audience and identity are separate questions: audience asks "may a model call
this tool?"; identity asks "which conversation is this call for?". A tool call
runs for exactly one conversation. A model cannot assert a different
conversation id; the server reads it from `_meta`.

### 3.4 Family plugins

A family plugin owns one configuration section or folder and declares the rows
it holds to the hub. `libs/plugin/family.py` provides the shared half: the two
connection families (`mcp:`, `rest-api:`) hold each entry's connection, declare
what each entry offers, route a call back to the holder, and build the
management tools' answers from the family's own words (`FamilyWords`); the two
document families (`skills:`, `cli:`) hold the home's mirror of the section and
declare its rows (`Documents`); the job family shares the declaration envelope
and the page builders (`Homes`, `listed`) and its rows are callable.

| Family | Section or folder | Holds |
|---|---|---|
| `mcp-tools` | `mcp:` | External MCP servers |
| `restapi-tools` | `rest-api:` | OpenAPI documents served by an MCP proxy |
| `cli-server` | `cli:` | Commands already installed |
| `skills-server` | `skills:` | Playbooks under `skills/` |
| `job-server` | `jobs/` | Code-defined tools under `jobs/` |

Other plugins — the cabinet, media, the terminal's inbox, and the rest — hold no
collection and serve their own tools; they are plugins, not families.

An entry has one `state` field, read from the file before the link is consulted.
A switched-off entry is `off`, not the `idle` of a connection nobody started. An
entry that answered with no tools is `ready` with zero tools; it is not silent.
The vocabulary matches the hub's `servers()` answer. A credential that did not
resolve never travels: a provider or media call refuses before use, while a tool
server's `env:`/`headers:` entry still holding its reference is **omitted** and
warned about, so the child's own missing-key policy decides — `tavily-mcp` runs
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

A client MUST prove which server it reached before believing anything the server
says. `identifies` compares the connected server's `server_info.name` against
the name the caller expected, and `server_info.version` against this build's;
when the server reports no name, it falls back to the server's own probe tool.
Every server built by `house_server` advertises this package's version, so a
daemon from a previous build fails the check on its build even though it answers
to the right name. `open_server` is the single place a client connects to one of
our servers; it raises, naming what could not be reached — or which build
answered, and the `slife2 down` that replaces it — rather than returning a
client that will fail later. The check prevents a server on the wrong port or an
old build from answering MCP correctly and being mistaken for the intended
server.

### 3.7 Refusal vocabulary

`libs/plugin/audience.py` holds the refusal vocabulary.

- **`UNADDRESSED`** is a sentence template with two holes: what is missing and
  why the server cannot guess. It is used for a call that does not identify the
  conversation it is for.
- **A raised `ToolError`** is the refusal a model reads. It carries one sentence
  saying what is wrong and what to do instead.
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
model is the default, and which networks and tunnels are enabled.

The launcher's server list is derived from the configuration. `Config.plugins()`
answers what this machine runs: the wire protocols some provider actually uses,
then the local servers. A protocol no provider speaks is not started; a provider
is a row in a backend's configuration, not a process of its own.

A missing file is fatal, and so is a malformed one. There is no default machine:
a home without `slife2.yaml` stops the start with a one-line message naming the
directory that was searched, and a file that exists and cannot be read stops it
naming the file and the mistake. The file must name at least one provider and at
least one embedding provider: there is no built-in provider or embedder.

`AGENTS.md` is the second required document. It is the model's guide to its own
tools, included in the system prompt every conversation opens with, and read
from the home at that moment, so an edit reaches the next conversation. A home
without one is refused where the prompt is rendered, naming the file. The
installer seeds it under the configuration's rule: a live file is never
overwritten, and a changed shipped copy is written beside it as
`AGENTS-default.md`. The installer takes its requirements from the shipped
`slife2.yaml`, never the live one: the live file is the server's, may lag a
release, and a merge is owed by hand — an upgrade must not wait on it.

`libs/host/configfile.py` is the single writer for edits made by plugins. It
performs a document edit under a cross-process lock, preserves comments, and
hands the result to the same loader used at startup. The loader is the judge; an
entry it refuses is restored.

`libs/host/model_config.py` is the canonical object for a `providers:` entry and
its models; `libs/host/tool_config.py` is the same for an `mcp:`, `rest-api:`
or `cli:` entry. The loader validates YAML into them, and each family's
`*_get`/`*_set` use the same object: the file is an artifact, not the schema.
Their keys and closed value sets are literal Pydantic fields, and an unknown key
is refused at load. The objects are complete — the loader applies the file's
defaults; `*_set` requires every field, so a partial write is a schema error,
not a silent reset.

`--a2a-broker HOST:PORT` overrides `peer_agent.broker` for one run, through the
environment so a server started by this command reads the same address. It
overrides the address only; `org`/`unit` remain the file's, and it cannot enable
a network whose section is absent. If the A2A server is already running, the
flag is refused rather than silently ignored.

### 4.2 Shared servers and launcher

`libs/host/launcher.py` starts the servers a configuration needs and attaches to
those already running. Servers are daemons: a client exiting never stops one,
but the last client out does. `others_running` counts by process liveness, so a
killed client cannot keep the stack alive.

The lifecycle rules are:

1. **A probe decides whether a server is alive, never a pid.** A pid can be
   recycled; records also carry a process start token.
2. **A pid that exists is not necessarily a running process.** A zombie is
   detected by reading its state. A state that cannot be read is not treated as
   a zombie.
3. **Ready means the expected server answers as itself.** A port that accepts a
   connection is not enough.
4. **Startup is a wave order.** `NEEDS` states which servers must precede which.
   Everything in a wave with no unmet dependency starts at once. A server whose
   dependency failed is not attempted and is reported with the name of what it
   was waiting for.

On Windows, a child must escape its parent's job with
`CREATE_BREAKAWAY_FROM_JOB`; otherwise a daemon dies with a kill-on-close job
owned by a CI runner or an IDE task host. Startup is reported as it happens:
`ensure_all` is a generator, and the CLI prints each server as it lands.

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
- A sanitized name becomes a filename; a name that reduces to nothing is refused.

### 4.4 `status`, `down`, and `logs`

`slife2 status` reports each server as `running` (with its pid and start time),
`running (external)`, `port held by something else`, or `not running`, and the
A2A broker when `peer_agent:` is configured. `--json` gives the same rows with
each server's log path. It exits 0 only when every server and the broker are
usable, and 2 otherwise.

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

- has the maker's tool set, with one exception: `subagent_set` and
  `subagent_remove` refuse a subagent caller (§4.5.3);
- has its own role-specific system prompt (`subagent.j2`) and its own
  `subagent.yaml`; a non-fork subagent also has its own stored conversation,
  cabinet, and media directory, while a fork stores no turns, reads the maker's
  turn log for context without writing it, and shares the maker's cabinet and
  media;
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

A subagent is created with `subagent_set` and removed with `subagent_remove`.
After creation it is reached over the local network under `subagent_*` names:
the direct seven (`subagent_send_message`, `subagent_task_response`,
`subagent_continue_task`, `subagent_cancel_task`, `subagent_list`,
`subagent_task_list`, `subagent_broadcast`), and the six group tools
(`subagent_group_set`,
`subagent_group_list`, `subagent_group_member_list`, `subagent_group_remove`,
`subagent_group_leave`, `subagent_group_send_message`).

A message reaches a subagent the same way one reaches an agent: it is submitted
to the shared agent server's inbox, and a message that arrives while a turn is
running is folded into that turn at its next iteration boundary. The harness
delivers; whether a member answers is the model's own `group_send_message` call.

#### 4.5.1 Directory and record

Every subagent of `jack` lives under `agents/jack/subagents/<name>/`. A non-fork
subagent has its conversation (`turns.db`), its record (`subagent.yaml`), and its
own `cabinet/` and `media/` there. A fork has only the record: it is a copy of
its maker's conversation held in memory, so it stores no turns, reads the
maker's turn log for context without writing it, and shares the maker's cabinet
and media. It has no restore or rebuild. That record is removed when the agent
server next starts: a fork is session-lived, and the start of a session ends the
forks of the last one.

**A fork is never a group member**: it is temporary, so a roster naming one
would keep addressing a name nobody answers; creation refuses it, placement does
not wake it, and its group traffic is dropped. A subagent has no external
identity of its own: WeChat and the peer-agent network belong to the agent, and
scheduling stays with the agent.

The container level is deliberate: `agents/jack/cabinet`, `agents/jack/media`,
and `agents/jack/turns.db` belong to the maker, so a subagent named `cabinet`
would otherwise collide with the maker's own directory. `subagent.yaml` records
the model the subagent runs on, the role it was made for, whether it was made as
a fork, and when it was made. The file is deleted with the directory — or, for a
fork, when the next session starts. The configuration file holds only
machine-wide facts: how many subagents a maker may have, and where the local
network meets.

#### 4.5.2 Role and fork

- **`role`** is a word included in the subagent's prompt. The prompt is the
  shipped `subagent.j2`, not the operator's `agent.system_prompt`, because a
  subagent must be told what it is, whose it is, and where its work comes from.
- **`fork: true`** starts the subagent from a copy of its maker's conversation
  as it stood when the subagent's conversation starts. Nothing about the fork is
  stored: its turns are not recorded, it has no stored context to restore, and a
  restart removes it. The copy is cut back to the last completed exchange,
  because a provider rejects a conversation ending mid-exchange.

#### 4.5.3 One level only

A subagent MUST NOT create or remove a subagent. `subagent_set` and
`subagent_remove` are refused when the caller is itself a subagent: the local
network holds a place for the maker's own subagents and nothing holds a place
for a level below them.

#### 4.5.4 Removal

`subagent_remove` performs, in order:

1. tell the local network to release the subagent's place;
2. stop the subagent's loop;
3. delete its directory.

If the network notification cannot be made, the subagent is left unchanged.
Nothing is asked of the subagent and nothing waits for it; removal is a forced
cleanup whether it is idle or mid-turn. Removing an agent removes its subagents,
because they are directories underneath it.

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

#### 4.5.6 Watching a conversation

A terminal can watch a subagent's conversation without becoming its window. The
agent server exposes two read-only management tools:

- `subagents(agent)` lists the maker's subagent directories with the model, role,
  and fork flag read from each `subagent.yaml`; it reads the filesystem, not the
  roster, because the roster connects the mesh and wakes members.
- `watch(agent, subagent)` is a held-open call. Its first progress frame is a
  snapshot of the stored turns — `transcript`'s own read, so history ends at the
  same place for a watcher and a restarted window. A turn in flight is replayed
  from a `TurnStarted` frame followed by the events already produced; live turn
  events follow. A cancelled call is the detach. When the subagent is removed,
  the last frame is `Closed` and the call returns.

Watching is a read: it starts no turn, chooses no context, spends no model call,
and never answers an approval request. The watched turn remains unattended
(`watched=false`), exactly as it is when nobody watches. A display that falls
behind loses frames; the turn never waits for it.
## 5. Turn execution

### 5.1 Loop contract

A turn is: send the conversation to a model; stream the response; run the tools
it requested; append the results; repeat until the model answers without
requesting a tool. `libs/turn/loop.py` implements the algorithm;
`plugins/agent/server.py` owns the conversations it runs in.

`AgentLoop` is built with a **backend** with one streaming method and a
**registry** of tools, refreshed by a callback before every model call.
`AgentLoop.run_turn(messages, user, observer, auto=…)` receives the messages to
run on, the incoming user message, an **observer** that may drop, delay, or
coalesce display events without affecting the conversation, and optionally the
harness's `auto` callback, which names a tool the harness wants called mid-turn.

The loop is a pure function over a message list. It owns no conversation,
socket, or clock. The iteration cap is `agent.max_iterations`, read when the
conversation starts — the same moment its model and system prompt are read — so
an edit reaches the next conversation, not the one already capped by the old
value. `0` or an absent key means no cap: a number nobody wrote is a bound
nobody chose. A turn that reaches a stated cap ends normally with
`stop_reason: "max_iterations"`, not with an exception.

A tool that fails is a result, not an exception. `ToolRegistry.execute` answers
`(text, ok)` and never raises: a peer's refusal, a dead transport and a tool that
threw all come back as `ok=False` with text the model can act on. What ends the
turn is a failure outside that contract — a tool list that cannot be refreshed
before a model call, or a model call that failed.

**A final model call that answers with no text and no tool call is a failed
turn.** A provider can legitimately stop without saying anything, but that result
is the same shape as a call that produced nothing, and the turn's product is its
answer: there is nothing to show, nothing to record, and no word in the
conversation. An empty answer — whitespace alone is none — is a failure, not a
verdict (Appendix C). The loop is the one place that decides this, because it is
the one place that knows the call ended the turn.

### 5.2 Harness additions

`libs/harness/` contains what the harness — the system, as distinct from the
model or the person — adds to a conversation. Four mechanisms share one rule:
what the harness adds is a marked address, never a claim of its own.

#### 5.2.1 Markers

`markers.py` defines `[Name:{…}]` as a prefix on a delivered message. A marker is
content: it is stored with the message and travels into the transcript, and it
is stripped for display by the reader that knows the vocabulary. A producer and
its reader share one spelling, and the names live in
`libs/plugin/vocabulary.py`: a server imports its channels, markers, tool names
and states from there, and a reader imports the same constants rather than the
server. Capitals mark a harness annotation, never the conversation's own words.

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
(`Task.delivered`). A task whose hand-back is still in flight is neither running
nor a failed delivery.

#### 5.2.4 Dumps

A tool result too large for the conversation is stored whole as a `dump` row in
the cabinet, keyed by the tool call id. The conversation carries a fixed
**head** of it within `tool_result.tokens`, and the marker in front of the head
names the call, the file's byte size and its path relative to the data
directory — where the agent's own tools run, so moving the home does not
invalidate a marker already in a turn. The size is one `stat` of the file the
cabinet wrote; the marker counts no lines and no characters, because a count is
a scan the marker does not need — the readers answer it when asked.

The head is a truncation by rule, not a selection by model. It is the largest
prefix of the tool's own text that fits, so nothing is rewritten; the file is
the same text, and the agent can open it. `dump_read` reads it by line
(`mode="line"`) or by character (`mode="char"`), and `dump_grep` finds text in
it and answers both addresses — the line and the character offset — so either
reader can continue from a match. Character mode exists because some text files
are not organized in lines: minified JSON, one long record, output whose
separators are not line breaks.

**Why not a discriminator.** A model asked which lines matter is a second model
call for every oversized result, carrying the result and the conversation. That
is expensive, and its input can itself be larger than the context it was meant
to protect. A fixed head costs one measurement and cannot overflow; the model
that needs a part the head did not carry reads it from the file.

**Why 8,000 tokens.** The window is meant to fire rarely: on the 2026-10 turn
log (349 results), the largest result was 5,284 tokens, so an 8,000-token window
shortened none, while a 4,000-token window would have shortened 2%. The case the
mechanism exists for is a tool bound to be long, such as a 10,000-word essay.

A non-text result gets the same treatment: the head is what tells a model what
it is, and the readers are bounded, so an attempt to read bytes is a bounded
answer rather than a second overflow.

The harness reaches the cabinet directly for `dump_save`, because that tool
carries no audience mark; the model's routes back are `dump_read` and
`dump_grep`.

#### 5.2.5 Fallbacks

No step in this path has a fallback registered in Appendix C. Keeping the
output, writing it, and building the head MUST succeed, and each fails the turn
loudly if it does not: a conversation carrying a marker whose file is not there
has given up the bound and broken the address while looking like success.

### 5.3 Harness tool pairs

Some things the harness must say as the model, because a provider accepts them
only as tool calls: `_func_tool_unload` after a trim, and `_check_new_input` when
a message is waiting. Both are ordinary declared tools whose calls the harness
writes into the conversation.

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
| `agent-task:<name>` | A background loop's answer | `AsyncTask(<name>)>` | `[AgentTaskResult:{…}]` |
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
The window draws the message inside the running turn, in its own bubble, without
closing the answer still being streamed. There is one queue: the agent server's
inbox.

### 5.5 Context decision and memory selection

The agent server owns one loop per conversation key, created on first use. The
model, the system prompt and the iteration cap are **properties of that loop**,
read when it is created: an edit reaches the next conversation, and a running
one moves model only through `model_switch`. What a turn re-reads per call is
everything else the configuration supplies — the tool list, the context
decision's parameters, the timeout — so an edit to those reaches the next turn,
not the next restart.

Before each turn, the context plugin answers one question: what will this turn
run on?

1. **`restore`**, when a conversation starts. It replays the stored live-context
   list.
2. **`rebuild`**, before each subsequent turn. It asks a discriminator model call
   what to keep of the conversation in hand and what to recall from the turn log.

The discriminator reads the conversation in hand rather than the new message
alone, because a follow-up usually names its subject through the turns above it.
Its answer is a selection, never a dump. The common answer is `{}`: a decision
that asks for exactly what is in hand rebuilds nothing.

Rules:

- **An unreadable reply is not a decision.** `parse` returns `None`, and the turn
  runs on the context it already has (Appendix C).
- **A kept turn and a recalled turn are joined by id, in time order, and never
  reconciled.** `normalise_ids` collapses duplicates to their first position; an
  overriding selection would discard the context it replaced.
- **A question has two spellings.** `sentences` are read by meaning, one question
  per entry; `query` is the words the keyword leg requires and the meaning
  fallback. The keyword leg is fused into each sentence's list, and a later
  sentence can only add a turn or raise one.
- **The person's input is a meaning question too**, unless the decision sets
  `prompt_as_sentence: false` for input that names nothing. A keyword-only recall
  keeps its query as the one meaning question; a period recall is not narrowed.
- **A recall is asked for what is missing.** Kept turns are excluded from its
  candidates, so nothing already in hand spends retrieval depth or budget.
- **The recall is hybrid, and the ranker is the last word.** The two legs
  are fused by weighted RRF (k=60, `keyword_weight` on the sparse leg), and
  `min_similarity` drops a measured candidate below the embedding model's
  noise floor. **No term is cut for being common**: a word that appears in
  half the log can still be the one exact string a query is about, and the
  reranker can only judge a candidate it is given.
- **A cross-encoder reranks the whole fused pool** when the embeddings
  provider names a `rerank_model`: each leg retrieves `recall_page_limit`
  candidates (80 by default), fusion returns their union — twice that for one
  question, more for several — and nothing cuts it; the floors and the token
  budget decide membership. `rerank_min_score` is the ranker's own floor. It
  is a precision stage, not a retriever: a missing or failing reranker keeps
  the fused order (a short answer is refused, not read as low scores), and a
  period recall — which has no question — keeps its time order. The catalogue
  and cabinet searches keep their own page order; their rerank is deferred
  (§14.2).
- **The live-context list's order is authoritative.** Reads replay it as written
  and never re-sort or re-slice it; a selection is not always contiguous.
- **`send_message` answers with every key present**, including `refused` where
  nothing could be refused. A turn is an object, not a call, because a message
  can arrive mid-turn and must wait rather than displace the turn in flight.
- **A rebuilt turn carries a footnote** naming its id, channel, and span
  (`turn_note`). The newest turn is annotated after saving with the same
  timestamps and channel, so the live and rebuilt spelling are byte-identical and
  no prompt-cache miss is paid on rebuild.
- **The turn running carries a prompt, not a footnote.** `[TURN-PROMPT:{…}]`
  (`turn_prompt_note`) is added after the recall and removed before the turn is
  saved (`without_lead`), so it never reaches the row, the index or a rebuild. Its
  payload is a mapping — today `now` and `token-usage` — because a per-turn fact
  can grow. The agent, its subagents and an agent task all carry it; a fork's
  turns are never recorded, so its marker lives only in memory.
- **The turn-to-messages builder is one function** (`messages_from_turns`); two
  builders would drift into two conversations.

**Why word-shaped queries.** On the live turn log, a sentence-shaped keyword
query fired on 0 of 16 queries; a word-shaped query fired on 10 of 15 and found
the target on 9. The meaning leg found the target in 32 of 32 recall queries,
first in 25. The sentence is therefore a shape the model may use, not one it
must use.

The context parameters are in `context:`:

| Key | Meaning |
|---|---|
| `ceiling` | Fraction of the model's window the live context is topped up to; a recall spends what the kept turns leave below it. |
| `recall_page_limit` | Candidates each leg retrieves; fusion returns their union uncut, and the floors and the token budget decide membership from there. |
| `min_similarity` | Similarity a measured candidate must reach; keyword hits are exempt. |
| `keyword_weight` | Weighted RRF's weight on the keyword leg; the meaning leg is 1.0. |
| `rerank_min_score` | The cross-encoder score a candidate must reach, 0–1; the shipped `0.3` drops keyword incidentals, `0.0` keeps everything it returned. |
| `timeout` | Seconds before the discriminator call is abandoned and the turn runs on the current context. |

**One budget, and it is the ceiling's.** The kept turns are estimated first;
a recall may spend what they leave below `ceiling`, so keeping less leaves it
more and a kept context at the ceiling leaves it nothing. There is no separate
per-recall fraction. `min_similarity` and `rerank_min_score` are floors whose
scale belongs to the model in use (bge-m3 scores unrelated same-domain text
near 0.5), so both are calibrated per home: the shipped 0.52 and 0.3 are those
calibrations. **Both are filters, not ranking rules**: they decide whether a
candidate stays, and the reranker's order is used only where the token budget
cuts. On the live log an unrelated query still passed
`min_similarity` with three neighbours at 0.52–0.54, and the reranker scored
them 0.002–0.036 — below the shipped 0.3 — which is the floor doing the work
the cosine gate could not. A provider with no `rerank_model` simply omits the
rerank stage.

A missing store fails the turn, like any missing plugin. A discriminator
timeout, provider error, or unreadable answer does not. There is no switch to
turn the decision off: an append-only context is bounded by nothing.

### 5.6 Cancellation and repair

An interrupted turn keeps what it completed. The batch of tool calls it was in is
completed on the way out, not dropped: the calls that already returned keep their
own answers, and every call that produced no answer is answered
`(Tool execution interrupted)`. The person's message is kept with it, so a
follow-up "continue" resumes from the work that was done.

A batch of tool calls is written atomically:

1. run every call, keeping each answer as it arrives;
2. fit every result;
3. append the assistant message and its complete set of answers together, with
   no `await` between them.

**Why this order.** Running a call cannot raise; the registry turns failure into
text. Fitting a result can raise — a cabinet that will not open — so the
assistant message is written only after every result is
fitted. Writing it first would leave the rejected history behind, and a failed
turn keeps its messages, so that conversation would fail every later turn. An
interrupted turn's row is always written because a write conditional on how a
turn ended is one somebody can forget. A write from a cancelled handler is
detached, because a plain `await` in a cancelled scope is cancelled again at its
next checkpoint.

An interrupted turn's write is detached, and the next turn takes its id up
(`settle_record`). A write the store or cabinet refuses is a hard failure: the
turn is unrecorded, so its messages are rolled back to the last backed prefix and
the caller sees the refusal.

**A message that left the inbox is a turn, even when no loop opened for it.** A
hub whose tool list is not answering, a store that will not restore, a rebuild
that fails: the message is put at the end of the live conversation as the turn's
opening message and recorded, so the failure ends with a row rather than with the
caller's words gone from both places. A picture the turn died before resolving is
named in an `image_note`, never kept as a part a provider cannot fetch. A fork
records nothing but keeps the message the same way — the store is what a fork
does not have, not the conversation. An agent task records nothing at all and
answers with its failure instead; its answer is a message like any other, and
this rule is what keeps that message when the turn it arrives on fails too.

**A turn that cannot go on ends with the harness saying so.** A tool's failure
is text in the result the model reads and the turn carries on; an exception is
the turn itself stopping, so the turn ends with an assistant message carrying
`[TURN-FAILED:{"by": "harness", "error": …}]`. It is a message rather than a
footnote because a footnote is derived from the row and a failure is not a
column; and it is a capitalised marker rather than prose because the harness is
what failed, and a model reading its own history must not take the words for its
own. The message is recorded with the turn, so the reason survives a restart; a
cancellation ends nothing this way — the caller stopped the turn, and there is
nothing to report.

The model's tool result is authoritative. Display chunks are not: progress
notifications may be dropped, delayed, or coalesced without affecting the
conversation.

### 5.7 Images

An attached image is live state **and a cabinet document**. The live user
message keeps the `image_url` parts the model reads; when the turn is recorded,
each part becomes an `[Image:{…}]` reference to a cabinet row of the `image`
kind, keyed by the content digest. The turn log keeps the sentence that named
the file, the tool call and answer naming it, and the reference. The bytes never
enter the row. A rebuilt conversation is therefore text plus references; a model
that needs the picture again calls `attach_image` with the reference's key or
with the URL.

A cabinet refusal fails the turn like a store refusal: a row that names an image
the cabinet does not hold is a marker that can never be attached again. A failed
image attachment is dropped from the live conversation with the same note the
record keeps, and the error says so. A provider that refuses an image refuses
every request that carries it, so one bad attachment could otherwise make the
conversation unusable; such a turn is note-only.

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
the configuration.

`api` selects the module to run, the server name it must identify as, and the
default port. An unknown `api` is a configuration error naming the known values.
`API_BACKENDS` and `API_SERVER_NAMES` are views over `PLUGINS`, not second
tables.

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
opt-in: a client that supplied no progress token costs the server nothing.
Logging is filtered by a negotiated level, so a client that never negotiated one
loses deltas silently. The revision in use deprecates `logging` and requires a
server not to emit a log notification for a request that did not ask for one.

Rules:

- **The result is authoritative and the chunks are not.** Progress notifications
  may be dropped, delayed, or coalesced by the transport.
- **An unreadable result is refused, not read as an empty answer.** A missing
  result read as an empty assistant message is a turn that "succeeds" with
  nothing in it.
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
and `context_window`. Each server sends only what its configuration asked for.
A value the protocol does not know is refused by name where it is read.

### 6.4 Provider capability configuration

A model entry declares its capabilities in its own configuration row:

| Field | Meaning |
|---|---|
| `input` | Modalities the model accepts; `text` and `image`. |
| `context_window` | The model's window, used for context fractions and status display. |
| `reasoning` | Whether the model exposes reasoning. |
| `compat.thinking` | Provider-specific thinking setting. |
| `compat.store` | Provider-specific persistence setting. |
| `compat.cache_control` | Anthropic's top-level automatic prompt cache: `ephemeral`, `5m` or `1h`; absent or `omit` sends nothing. |
| `max_tokens`, `temperature`, `top_p` | Generation parameters. |
| `compat.stream_usage` | Whether the provider reports usage in the stream. |

The `compat.*` keys are written under the model's own `compat:` mapping; the
others sit beside `model:` in the same entry. The terminal reads the same entry
for its status bar and image support. A model that does not declare `image` is
not offered image attachment.

### 6.5 Usage and cache accounting

`Usage` counts the cached prefix inside `prompt_tokens`: Anthropic's
`input_tokens` is the uncached remainder, so its adapter adds the two cache
fields back in; OpenAI-compatible wires already include cached tokens.
`cache_read` and `cache_creation` are parts of the prompt, never additions to it.

OpenAI sends token counts on a trailing chunk with an empty `choices` list;
DeepSeek attaches them to the final chunk that still carries a choice. The
adapters accept either shape; an adapter that handles only the documented OpenAI
shape reports zero tokens against a real DeepSeek endpoint.

`stream_chat` takes a `kind` word: `turn` for the loop's own calls, and
`discriminator` for the recall decision, the one call that reaches the model
server outside the loop's backend. One line is logged per call with the
breakdown and the call's `elapsed`, because a cache hit rate distinguishes a
rebuild from an append. A call that raises logs `stream_chat_failed` with the
provider, model, conversation and kind before the error propagates.
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
every source, including one that is down and keeps its rows on purpose. It is
read on the ask that already refreshes the declarations, and the terminal's
status bar shows it.

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
effect at the next search. The hub's own tools are rows too.

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
- All of our tools share one namespace. Two plugins cannot offer the same name;
  the catalogue refuses rather than resolving silently.
- A name from an external source is not trusted: `model_name` derives it,
  `sanitise` applies the provider's alphabet, and the separator makes it
  reversible.

A skill and a command are rows but not callable tools. They have no load state
and never enter the model's list. Calling a skill row answers with the step that
reaches it (`skill_use`); calling a command row answers with the rule that a
command is run through the shell, not called as a tool. Their names are
namespaced (`skill:browser-harness`, `cli:yt-dlp`).

Neither family gets a runner of its own, and that is the design rather than a
gap: a skill is instructions the model carries out, and a command is a program
the model runs through whatever shell its tool set provides. What the row adds
is that the command can be found.

A **job** is a row that *is* callable and loadable, because its implementation is
code rather than a document or an installed program. Its name is `job__<name>`
because the bare name is a user's Python identifier and the prefix keeps it out
of the system's namespace. Its row is the same shape as any function tool's.

### 7.4 Loaded set and budget

What the model is handed is the tools it has loaded, not every tool that exists.
The list is re-read from the hub before every model call.

Initial state:

- The tools named in `tool_load.autoload` are loaded, by the name the model
  calls them: a plugin's bare name or an external server's `{server}__{tool}`.
- The names the system itself refers to (the four set-level tools, the two
  harness pairs, `skill_use`, and the two background-call tools) are required in
  that list; a configuration that omits one fails the start.
- Everything else, a plugin's tools included, is discoverable and starts
  unloaded when first seen. One tool the model never calls is a prompt spent on
  every request. A call to one is checked against its catalogue row and, when it
  fits the row's current schema, the harness loads it and runs it in the same
  turn.

**A restart restores what was left, not a fresh state.** The conversation's
context and the loaded tool set both come back as they were; only a change to
the configuration alters what a restart restores. The loaded set lives in the
catalogue, not in a process, so the boot pass's only move is to load a name on
`tool_load.autoload` that is still unloaded. Stopping or starting a process never
takes a tool out of the model's list; only eviction at a turn boundary, or the
model's own unload of an unprotected name, does.

A model can call a name returned by `tool_search` or `tool_list` without loading
it: the call is validated against the schema the catalogue holds *now*, loaded
by the harness (`tool_load`) and run. Loading is what keeps the tool in the list
sent with each request. A call that does not fit — the schema changed since the model last saw
it, or its arguments could not be read — is not run; it takes the load hint,
which is how the model reads the current schema.

The loaded set is bounded by `tool_load.threshold` (default 100 function tools).
Over the limit, the least recently **called** tools are unloaded; the names in
`tool_load.autoload` are never evicted, and a tool never called since it was
loaded is ordered by its load time. The trim happens at a turn boundary: the
harness calls `_func_tool_unload` before saving the turn and writes the pair into
the conversation, so the model learns that the list shrank from the transcript.
Nothing is written when nothing was removed. The same list bounds the model's own
unloads: `_func_tool_unload` refuses a name in `tool_load.autoload`, so the tools
that start loaded, the tools the budget may not evict and the tools the model may
not unload are one list. Editing the list changes only that protection. Recency
is learned by the hub: it
stamps `last_used` beside the call it just routed, including a refused call,
before returning the answer, so the stamp cannot land after the trim.

A call naming a tool outside the request's list is first checked against the
catalogue row: if the row exists and the call fits its current schema, the
harness loads it (`tool_load`) and runs it in the same turn. Only a call that
cannot be resolved that way — an unreadable argument list, a schema it no longer
fits, a row that cannot be loaded — reaches the registry's error path, which
asks the hub (`tool_exists`). The answer has three values: `present` (load it
with `func_tool_load`, which is what puts the current schema in front of the
model), `missing` (no row; `tool_search` finds what does the job), and `starting`
(the catalogue is still being written, so retry). The third exists because a
first turn can land inside the merge pass; without it a transient absence read
as a capability the machine lacks.

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

The two legs are fused by rank, not by raw score: `bm25` is unbounded and a
cosine depends on the embedding model, while a rank is comparable by
construction. Within one question that is the whole ordering. Across questions
the page is ordered by what the caller can see: rows the caller's own words
matched first, then by meaning descending. The words come first because cosines
cannot separate a near-duplicate cluster, and only the word match says which one
was named.

**Why these shapes.** Split inputs beat one string on the page at 20/20 vs 18/20.
Keywords-only reaching the meaning leg found 34/36 against 23/36 for the keyword
leg alone. A word list and a sentence ranked identically on the meaning leg. Two
sentences ranked by fused score let an unrelated second sentence move a first
question's fifth answer to rank 9, which is why the page rule exists.

Chinese text is indexed character by character and queried as a phrase: FTS5's
tokenizer would otherwise treat a contiguous Chinese run as one token.

The answer names its confidence in three tiers because the measurements
overlap: answerable queries had a best-on-page meaning no lower than 0.477,
unanswerable queries had no value higher than 0.512. Every row carries its
meaning score and a word-match marker saying whether the match was in the
visible name or description, or behind it in the body or parameters.

Filters narrow the result and never decide it. They are applied in SQL rather
than after fusion, because rank orders every match and cutting to the limit
first would drop rows the filter would have kept.

`unloaded_only` is the one filter that is not an equality: it means
`load_status != 'loaded'`, so a skill or a CLI entry — which has no load state
and is never in the model's list — and an unloaded job are still found. The
default stays the whole catalogue: the best answer may be a tool the model
already holds, and `loaded` on the row says so.

**Listing is the other question, and it is `tool_list`.** A search ranks what
matches a question; a listing pages what exists, for a caller with no words to
search with. They are separate tools because one answer cannot be both: a
listing has no score and no confidence, and its order — category, then source,
then name — is the only order it has. For the same reason `tool_search` takes
no `limit`: a search finds *a* tool, and paging the catalogue is the listing's
question — the search's answer still says the ten rows it was cut to. The name is last because it is the primary
key, which is what makes the order total and two pages unable to drop or repeat
a row between them.

`category` and `source` narrow a listing and are applied in SQL, so `total`
counts what the filters match. `total`, `limit` and `offset` follow §7.6's page
rule; `limit` omitted is 20, and the cap is 200. The rows carry the same fields
a search result does, minus `meaning` and `matched`. A skill, a CLI entry and a
job are rows like any other; `loaded` is true only for a row in the model's
list, so a category with no load state is not a missing field.

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
- **Two tools answer with text rather than JSON, because the text is the
  thing.** `_check_new_input` hands over a person's message mid-turn; `skill_use`
  hands over the skill's whole `SKILL.md`.
- **No answer exceeds `tool_result.tokens`** (default 8,000; minimum 512). A
  listing pages; an output that cannot be bounded is the harness's dump, and
  `dump_read` and `dump_grep` are bounded readers within the same budget.

A field may contain a sentence, because a person reads some answers too. The
rule is that the envelope is one shape, so a reader that knows one tool knows
them all.

### 7.7 Meta-parameters

Every function tool accepts three meta-parameters. They are added to every
advertised schema and stripped before dispatch, so no tool server sees them.

| Parameter | Semantics |
|---|---|
| `_timeout` | Seconds for this call: a positive, finite number, overriding the tool's default. Any other value — `0`, negative, text, `inf` — fails the call with an error the model reads; the value is never read as "no limit" or as the default. A tool that declares its own `timeout` argument enforces it itself. With neither, the call runs until it finishes. There is no default budget. |
| `_async` | Run the call in the background. The model receives a task id; the result is handed back into the conversation. The result is still bound by `tool_result.tokens`. An agent task's loop has no `_async`. |
| `_approve` | Ask the user before running. The terminal draws an inline row and answers through `answer_approval`. A channel with no person behind it does not ask; the call runs. A caller that can answer declares `watched=true`. |

The loop reads these parameters off the call and strips them before dispatch.
The strip is a copy: the tool is dispatched with one dict and the history keeps
the original.

## 8. Memory and indexing

### 8.1 Turn store

`libs/store/db.py` implements one SQLite file per conversation (conversation
id): `agents/<agent>/turns.db` for the agent's own conversation, and
`agents/<agent>/subagents/<name>/turns.db` for a non-fork subagent's. A fork has
no file of its own; it reads the maker's turn log and never writes it, and its
`subagent.yaml` is removed when the agent server starts. Isolation is by file: no
query can reach another agent's turns because they are not in the file. The home
a call came from selects which of the agent's directories is read or written. The
store holds the turn log, the live-context list, and the indexes over the turns.

A turn is stored as the message list the model saw. What was asked is the first
user message; what was answered is the last assistant message with prose.

There is no migration layer. The tables are created on a file that is not this
build's. A file this build cannot read is refused, not upgraded in place,
because an upgrade path is a permanent second way to build the tables. A file
whose `turn` table lacks a column this build declares is refused at open, with
the file and the column named. A file that still holds the previous schema's
`turns` table is refused the same way, before any DDL. The startup pass over
every known home carries on past a file it refuses: the refusal is that file's.

The live-context list is a row, not a file. It is written in the same transaction
as the turn that changed it. A turn can point outside itself: a tool output too
large for the conversation is stored whole in the cabinet, and the turn keeps a
marked head of it, so the row still holds exactly what the model saw and the
whole is reachable by the tool call id.

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
listing tool) and `limit` (the page size applied). `libs/store/answer.py` is the
one home of the envelope and of the floors behind `confidence`; the floors are
the embedder's, measured on the catalogue (§7.5), the words override them in
every corpus, and the tier labels a page without cutting it.

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

The token estimator uses `tiktoken`'s `o200k_base` when the vocabulary can be
fetched, and a script-aware character measure when it cannot. The character
measure was measured about 25% high on English and 60% high on Chinese — a bias
rather than a tolerance. Measured usage is unaffected: the provider's own count
is what the record keeps.

### 8.3 Cabinet

`plugins/cabinet/` stores six kinds of row; four of them are its corpus:

A note, a diary day and a report share four verbs: `{kind}_write` creates the
document and refuses a key that already exists, `{kind}_read` reads it,
`{kind}_update` replaces the whole document, and `{kind}_remove` deletes it. A
saved file's write is `file_save`, which copies a local file or fetches an
`http(s)` URL in; its read, search and remove keep their own names
(`file_read`, `file_grep`, `file_remove`).

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
`image` share the
table because they share the directory, the key and the write path, but they
are not in the corpus: the turn's marker is their address (`[DUMP:…]` →
`dump_read`/`dump_grep`, `[Image:…]` → `attach_image`).

**The meaning leg covers the three agent-written kinds.** A note, a day and a
report are vectorized by their whole file text. A saved file is in the keyword
corpus only, and a dump and an image are in neither. **A file's and a dump's
size are not the agent's to control**, so meaning search over them is deferred
until a pass that chunks and embeds their contents exists (a RAG concern, not a
cabinet one); until then a file is found by its words.

The cabinet is per conversation: an agent's own conversation has
`agents/<agent>/cabinet/`; a non-fork subagent has its own under its directory;
a fork uses the maker's. **The file is the document.** For every mirrored kind —
note, diary, report, dump — the text is written to its file in the same
operation as its row, and **there is no body column**: the row is metadata and
the address, not a second copy. The file holds the text exactly as the caller
gave it; a note's subject, a day's date and a report's title are the row's and
are never prepended to it. **Every answer about a document carries `path`** —
the file relative to the data directory, where the agent's own tools run — so a
model can hand the same file to `pandoc`, `rg` or a shell; the internal `mirror`
and `file_path` stay out of the answer, and the readers carry `path` too. A
file and an image have no mirror, because
their bytes are the document. The path is stored rather than derived, so a
change to the naming rule orphans nothing that already exists. Every
caller-supplied path is resolved against the cabinet root and refused if it
leaves it.

**A corpus-wide literal or regex search is the external `rg`**, run through the
model's shell over those paths, and not a third leg of the cabinet's search. The
documents are already files on disk, so rebuilding `grep` inside the cabinet
would be a second implementation of a program the machine has.

A subject that is not already lowercase carries a short digest in its filename,
because `Python.md` and `python.md` are one file on Windows and macOS.

A file's bytes are never indexed, and it has no vector. The keyword leg indexes
its name, the paths that place it, and a summary once one is written; the
summary of a file is therefore keyword text and re-embeds nothing. An image is
not indexed at all: neither its bytes nor its row is in the corpus. A
note/diary/report's vector is its file's whole text; its summary is keyword text
only, so there is no re-embedding asymmetry left in `annotate`.

A saved file's source is a local path or an `http(s)` URL. A local file is
copied and never moved; a relative path is resolved against the data directory,
where the agent's own tools run, rather than against the cabinet daemon's own
working directory. A URL is fetched whole, bounded by `MAX_FETCH_BYTES`
and `transport.file_fetch`. The URL is untrusted input: only `http(s)` is accepted,
credentials in a URL are refused, and a host written as an address or as
`localhost` must be globally routable, so a fetched URL reaches the internet
rather than this machine. Every redirect is checked by the same rule, so a
public URL cannot hop to a private one. Names are left to the machine's
resolver; a fake-ip proxy answers an address the proxy owns rather than the
destination, so resolving one in the guard would test the proxy rather than the
site.

A dump and an image have no model-facing writer. The harness saves a tool output
too large for the conversation with `dump_save`, and a picture a turn carried
with `image_save`. Neither write produces an index entry; the turn's marker is
the address. **A dump's text is its file, and its row holds no body** — one copy,
whether the output is a kilobyte or a gigabyte. `dump_read` and `dump_grep` are
the ways back for a dump. For an image it is `attach_image`: the turn's
`[Image:{…}]` reference names the key, `image_source` resolves it to a `data:`
URL (or the source URL), and the tool attaches it. An `http(s)` image is never
fetched here; the row keeps the URL and the provider fetches it.

`dump_read` reads by line or by character. `mode="line"` takes a 1-based
`offset`, negative from the end; `mode="char"` takes a 0-based character
`offset`, negative from the end. `limit` caps lines or characters, and `0`
shows as many as fit. Line mode numbers the lines it returns; character mode
returns the raw slice, which is the way to read a file that is not organized in
lines. Both counts come from one streaming counting pass; line mode uses the
same pass to page. `next` is where the following window starts in the mode's
own units.

`dump_grep` matches a literal pattern within one line, ignoring case. Nothing
the caller wrote is quoted back. Every match answers its line number and the
0-based character offset where it starts, so either read mode can continue from
it; `matched` counts the whole output rather than the page, and `next` names the
line of the next match when not all matches fit. The pattern is text, not a
regular expression: a regex over a 240,000-line output is an unbounded program
running in a shared server.

`file_read` and `file_grep` are the same two readers over a saved file's
decoded text — the file counterpart of `dump_read`/`dump_grep`, differing only
in where the body comes from. Both stream: `file_grep` holds only the page as
it scans; `file_read` makes one counting pass for the totals and a second that
seeks the window (skipping lines, or decoding forward to a character offset),
so its memory is the window rather than the file. A saved file is whole on
disk and is not truncated; the window is the answer's bound, and `next` asks
for the following one. Path resolution and the binary rendering live in one
function, and the encoding sniff in another, so the two readers cannot disagree
about what the file's text is.

Every answer stays within `tool_result.tokens`; the bound is enforced where the
answer leaves, or a reader could return more than the shortening removed.

A dump and an image are not in the corpus. Their address is the marker the turn
carries — the dump's call id and the image's key — so `dump_read`/`dump_grep`
and `attach_image` are the readers, and neither has a search entry to rank.

### 8.4 Embeddings

One server (`plugins/llm/embeddings_server.py`) and one client
(`clients/embedder.py`) serve all three indexes: the turn store, the tool
catalogue, and the cabinet. Sharing is required because the three indexes must
rank against the same model and agree about what an embedding is.

The server holds the provider key for the same reason the model servers do.
Embeddings are a hard dependency: a turn is written with its vector in one
transaction, so a save that cannot embed stores nothing. That is why there is no
background drainer and no "index not ready" state.

The SDK's own retries are zero (`timeouts.stream.retries`): a request that
reached the provider may have been billed, and a 429 is the caller's to pace.
The one repeat the server makes is a connection that never opened — a
`ConnectError` means nothing was sent and an embedding is idempotent, so the
retry cannot double-charge or change the answer, and a transient blip in the
link to a remote provider does not fail a cold catalogue build. A second failure
is the caller's, and the hard dependency above stands.

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

`plugins/wechat/` holds one login per agent and turns what arrives into a turn. A
subagent has no login of its own: WeChat is an external identity, and a subagent
is internal to its maker. WeChat exposes no direct API to a program; the plugin
speaks iLink's hosted bridge (`wechat/ilink.py`: the QR handshake, the long poll,
and the envelopes whose meaning differs from their status). The plugin owns no
configuration section and declares no catalogue rows; what it holds is a
credential and a live conversation per agent. Its four tools are its own
model-facing tools.

The file carries the raw agent name. The directory holds the sanitized name; a
session keyed by the directory name would build a second session for one login,
adopt the same token, start a second poll loop, and deliver every message twice.
A file too old to carry the raw name is skipped and named in the log.

One WeChat account has one live login. A second scan retires the first login
silently; the bridge answers the first with `session timeout` — a rejection
wearing HTTP 200. Both error-envelope shapes are read, so the first loop ends,
the window is told, and the model is given the bridge's own words.

While a login waits, the session holds the code itself: `wechat_check_status`
hands out the drawn QR and what it encodes. A code that expires is replaced in
the background and the replacement becomes the session's. A new login clears the
code it replaces before fetching, and logout clears it with the state.

The model-facing tools are `wechat_login`, `wechat_check_status`,
`wechat_send_message`, and `wechat_logout`. A message from WeChat becomes a turn
in the agent's conversation, recorded with channel `wechat` and a `[Wechat:{…}]`
marker naming the sender. Answering is the model's job; nothing auto-dispatches
a reply.

### 9.2 Peer agents (A2A over MQTT)

`plugins/a2a/` carries agent-to-agent traffic over MQTT, using the published
A2A-over-MQTT profile 0.1 (over A2A v1.0.0). Its remote instance is the peer
network of agents; a subagent is not a member of it and exists only on the
local instance, under its maker.

The wire vocabularies are the `a2a-over-mqtt` SDK's: the topic tree, the
retained agent card and presence, the JSON-RPC envelopes and their MQTT
properties, and the protocol's classification helpers. The responder lifecycle
is a local derivation in `plugins/a2a/responder.py`, because the profile's
interrupted-task continuation is not in SDK 0.1.0 (upstream `main` is
identical): a round may end `input-required`/`auth-required` without a
terminal, a new `SendMessage` with the same `Task.id` and `context_id` resumes
it, a terminal task refuses a new message, and `GetTask` reports the current
task. The requester ladder and the two stores are this package's. The
conformance claim at the end of this section names what is covered.

**One identity per agent.** The agent name is the MQTT client id and the last
segment of the request topic. Two processes answering to one name trade the
session back and forth, and every task sent to it runs twice. So there is one
mesh per agent; a second live instance under one name is refused before joining;
and the collision check reads the retained card on the name's discovery topic.

Rules specific to this system:

1. **Presence follows a window.** A `join` call held open puts an agent on the
   mesh. Online and offline are notes, not messages; no turn runs and the model
   is not told. The roster is `peer_agent_list`.
2. **A round is settled by the model's own call.** `peer_agent_task_response`
   settles it with `completed`, `failed` or `rejected`; `input_required` and
   `auth_required` pause it instead, and `peer_agent_continue_task` resumes a
   paused task this conversation sent. A turn that ends in prose sends nothing.
3. **A restart drops working rounds; task state is process-local.** The bridge
   and the reply topic are process-local, and an interrupted task's question
   and context live only here, so a restart loses the ability to continue it.
   The peer's own `task_list` names what it is waiting on, and it can cancel or
   re-send.
4. **Outcome is one word, and it is the peer's own.** `completed`, `cancelled`,
   `failed` and `rejected` come from the reply; `input_required` and
   `auth_required` mean the task is paused and open. The one outcome no reply
   can produce — the retry ladder running out — is `UNREACHABLE`, the
   requester's own failure.
5. **A withdrawal outranks a late answer.** Cancelling publishes the withdrawal
   first and records it only after the publish lands; a failed publish leaves the
   row pending. A later result or failure cannot rewrite an ending the caller was
   already told about.
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
    member persists its own YAML record and learns the roster from group traffic.
    Only the owner may replace the roster or disband the group; a member's message
    can teach a group this receiver does not hold, never rewrite a roster it has.
    A `group_set` is idempotent: the same roster keeps the gid and leaves a
    receiver that already applied it unchanged; the repeat re-sends the notice,
    which is how a member that missed the first one catches up.
13. **Leaving is autonomous, including for the owner.** An owner's departure
    transfers the group to the first remaining member, or disbands it. A fork is
    never a member: a roster naming one would keep addressing a name nobody
    answers.
14. **Group traffic is directed, fire-and-forget, and never returns to its
    sender.** One QoS 0 event per recipient on that recipient's own topic, carrying
    the roster and a generation id, so a member that missed a control learns the
    group from the next message and a delayed event cannot resurrect a group it
    left. **A group chat is casual**: no member owes an answer, silence is not
    `pending` or `undelivered`, `sent` confirms publication, not that anyone
    read it, and a task request is the only send with an expected answer. A
    group whose roster holds only the sender is empty; a send there is refused
    rather than fanned out to nobody.
15. **Group messages have their own vocabulary.** The peer network uses the
    `PeerAgentGroup` channel and marker; the local network uses `SubagentGroup`,
    for the maker and its subagents alike. The marker carries `from`, the group and
    its owner; the roster is a separate read (`group_member_list`) and rides the
    wire payload only so membership converges from traffic.
16. **Mentions are text, not delivery.** `@name` (one or several) and `@all` —
    `@所有人` in a Chinese conversation — address members the way a WeChat group
    does; every other member still receives the message.
17. **Membership controls are silent.** A join, leave or disband changes the
    local record and nothing else; the member list is what this conversation has
    persisted, not a live check. Membership is not activity.
18. **A local send names a member; a remote send says it is unverified.** On the
    subagent network membership is the directory set, so an unknown name is refused
    before it is published. The peer network has no authoritative roster, so its
    sends return `status: "unverified"`: the id means acceptance and publication,
    not receipt. A send that is never answered ends as an `UNREACHABLE` notice.
19. **A paused task is continued, not re-sent.** A continuation is a
    `SendMessage` with the same `Task.id` and `context_id` and a fresh
    correlation; the responder couples it to the next round. The retry ladder
    and the routing table are generation-scoped, so a late terminal from the
    paused round cannot settle the continuation. A terminal task accepts no new
    message, and a continuation that never lands restores the paused state with
    its question.
20. **A failed reply is a verdict with guidance, not an automatic retry.** The
    transport ladder stops at the first correlated reply. `-32003`/`-32004`
    render as failures the model may re-send; `-32005` is not retryable; an
    unknown task is `-32001`. A peer's `failed` or `rejected` is terminal.
21. **A message that reaches no conversation is rejected, not dropped.** The
    requester stops its retry ladder at the first correlated reply — the
    responder's `submitted` acknowledgment — so a message no window takes is
    settled as `rejected` with the reason, and a bare `message` reports
    `completed` only after the conversation has taken it. The peer reads the
    ending and decides whether to send it again.

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
group tools `peer_agent_group_set`, `peer_agent_group_list`,
`peer_agent_group_member_list`, `peer_agent_group_remove`,
`peer_agent_group_leave`, `peer_agent_group_send_message`.
`peer_agent_task_list` reports `pending` (still running), `interrupted` (paused;
its `question` says what it wants) and `unreachable` (the requester's retry
ladder ran out); an unknown id is reported as withdrawn or never seen.

A task id is a canonical `uuid4` string, generated by the requester as the
profile requires; a continuation reuses it and the original `context_id`, while
a new task gets fresh ids. The in-memory task store is shared by every mesh in
the process, a record carries the agent it belongs to, and the store is pruned
with a soft cap.

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

`plugins/agent_task/` runs one loop of the agent's own on a description, and the
answer comes back as a message. It is a separate mechanism from a subagent: it
runs on the agent's own context, has no address of its own, records nothing, and
accepts no second message.

Properties:

1. **A cap and a name.** `agent_task_start` refuses a duplicate name and refuses
   past `agent-task.max_running`.
2. **A task's turns are not written to the agent's database.** The task reads the
   agent's context and adds nothing to it. What the conversation gains is the
   answer, which the agent server records as a message.
3. **Every failure is a message.** A loop that raises still has an answer to
   deliver.
4. **Background work inside a task is refused.** The task loop runs one turn and
   has no second turn for a background result to arrive in.
5. **`finished` means delivered.** A task's answer is its product and no copy is
   kept (§9.3), so a task whose answer reached no window is `failed`; the status
   can be trusted to mean what it says.

A task runs one turn, so it makes one rebuild; zero would make it the one turn in
the system whose context nobody decided. It runs with the agent's system prompt,
model, tools, and context decision. Its opening message is the shipped
`agent_task.j2`: it says nobody is waiting, nobody can answer a question, the
message sent when the task stops is the whole of what comes back, and background
work or a nested task is refused. It does not have the turn that started it,
because `restore` reads the stored turn list and a turn is written only when it
ends; the description must restate the last exchange.

A task may start another task, because the tool set is one catalogue and nothing
filters it. `agent-task.max_running` bounds concurrency, not total work over
time.

The answer is delivered as a message on the `agent-task:<name>` channel, with an
`[AgentTaskResult:{…}]` marker. **A task is `finished` only when its answer
reached a window**: if no window is showing the conversation, it ends `failed`
and the log records why — the answer is the product, and an answer nobody
received is not a finished task.

**The acknowledgement is the write, not the record.**  `finished` means the
window took the answer: `hand_over` returns when the words are on their way down
the window's stream, and the window submits them into the conversation, which
the agent server records as a turn when that turn ends.  A window that dies
between the two leaves a `finished` task whose answer is in no store.  Closing
that gap needs a second acknowledgement after the record, which the hand-over
design refuses: it is how one message becomes two turns.

**A probe may start one too.** `slife2 probe
--channel tui` starts a task as the target agent, waits for its state, and reads
the delivered message; `agent_task_status` reports state and never the result.

### 9.4 Scheduled tasks

`plugins/schedule/` starts work nobody asked for at the moment it starts. A
scheduled task is durable, timed, and repeatable; an agent task is ephemeral and
one-shot. The schedule's definition and ledger are the feature.

`schedule/expressions.py` is the only module that knows cron. It owns the
timezone policy and the error contract: `day_or` makes restricted day-of-month
and day-of-week a union, and `strict` cross-field validation rejects `0 9 31 2 *`
before it is stored as an enabled task that can never fire.

The data model is three SQL tables — `meta`, `scheduled_tasks`, and
`scheduled_runs` — holding a task (name, description, cron expression), a run
(one fire and its outcome), and the agent the ledger belongs to.

| State | Meaning |
|---|---|
| `pending` | The run was written; the outcome is not known yet. |
| `ran` | The work finished and its answer reached the conversation (the window's write, §9.3); the task's `finished_at` is the run's time. |
| `failed` | The work failed or was cancelled, the answer could not be delivered, the run could not be started or followed, or a stopped process left it in flight. |
| `missed` | The fire occurred while no instance was running — no process, or no window showing the agent — so nothing started. |
| `skipped` | An operator or the model closed a run that will not happen. |

A run is named by the moment it was due, not by when it ran. The row is written
before the window is asked, so a fire cannot repeat on the next poll. A window
that is not showing the agent marks the run `missed`; a terminal that cannot be
asked, or a dispatch that fails, marks it `failed`; a task that finishes but
cannot deliver its answer is `failed` too, because an agent task is `finished`
only when the answer reached the conversation; a run whose outcome does not come
back remains `pending`, and the next start's sweep closes it — `missed` if it
never started, `failed` if it had. A fire follows the wall clock of its task's
timezone; a repeated local hour is the same fire only if its wall-clock reading
equals the anchor's, because comparing instants alone would fire twice in a
fall-back hour.

The anchor is a compare-and-set in the ledger's own update: a fire whose instant
is not later than the stored `last_due` leaves it alone, so two concurrent fires
cannot move it backwards. A stored anchor that is not a time
(`julianday(last_due)` is NULL, from a hand-edited ledger) is replaced by the
next fire rather than raising, because nothing else could ever move it again.

The ledger is one file per agent. The shared process finds agents by walking
`agents/*/schedule.db` across every known home; a ledger carries the home it came
from, because the home is a property of the request, never of the process.

A fire is gated, then dispatched. The poll asks the terminal whether a window is
showing the agent: one that is not is `missed`, because the answer would have
nowhere to go, and the instance that comes up later decides whether to backfill
the run with `schedule_run_now(name, due_at)` or close it with
`schedule_run_skip`. One that is there is started directly as an ordinary agent
task (`agent_task_start`); the schedule plugin is the caller, so it receives the
task id and its poll can close the run when the task ends. No turn is opened and
no model call is spent — a trigger turn would ask a model to press a button the
plugin can press itself, and a model that did not press it would leave the run
`pending`. An agent-task plugin that cannot come up is a scheduler that cannot
run, and the launch order reflects that dependency.

The clock is a poll, not a timer: a fire is noticed on the next pass — up to
`pacing.schedule_poll` (30 s) after its due moment — and a finished run is closed
on the next pass after its task ends.  `ran_at` is still the task's own
`finished_at`, not the poll's.

Tasks fire only while slife2 is running. On startup, the sweep closes runs left
open by a stopped process — a run with no task was never started and is
`missed`; one that names a task is `failed` — and the poll records the newest
missed fire for each task. A missed or failed run can be re-run with
`schedule_run_now(name, due_at)` or closed with `schedule_run_skip`. Nothing
announces it; the ledger holds the answer.

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
- **Harness lines come from one place** (`tui/notes.py`).
- **A tool panel has four states**: `running`, `done`, `error`, and
  `interrupted`. `interrupted` is about the turn rather than the call: the turn
  ended, was cancelled, or failed while the window was waiting, so no result
  exists and none is coming.
- **A connection has two states on screen**: online and offline. The reasons
  live in the status tool.
- **The status bar's `tools` count is loaded/total**, both from the hub's answer
  to one ask. A tool nobody has loaded is still a tool that exists. The window
  replaces both with the pair the last model call was built from.
- **Model output is plain text.** Nothing a model produces is parsed as markup.
- **A restored conversation is the server's.** `transcript` is read once when the
  window opens; the window keeps no copy. A failed read is not an empty
  conversation.
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

### 10.4 Interface language

The terminal is bilingual: English and Chinese. It is the only bilingual part of
the system. Detection is the operating system's answer, read once at import:

1. On Windows, the Windows UI-language API. The C locale carries the region, not
   the interface language.
2. Elsewhere, `LC_ALL`, then `LC_MESSAGES`, then `LANG`, most specific first.
3. Chinese locales select Chinese; everything else selects English.
4. Any detection failure degrades to English.

`--lang` overrides detection and is applied before anything is built. The
translation layer is one accessor over a two-language table (`tui/i18n.py`);
there are no catalog files and no dependency. A missing key or placeholder raises
at the call site rather than rendering a blank slot.

Always English, whatever the flag says: the system prompt; harness and plugin
tool schemas; job schemas and result strings; every log line; external text; and
tool labels, because a translated label would put the screen and the tool list in
disagreement. Key caps (`Esc`, `Ctrl+S`) remain English in both languages. A
counted noun is two entries in the table, because `str.format` has no plural
machinery.

A daemon composes the lines it sends (a peer coming online) in its own process,
so it follows the OS locale rather than `--lang`; a machine whose OS language and
`--lang` agree never sees the difference.

### 10.5 Unattended operation

An agent is on the peer network while a window holds its `join` call. Everything
else already works without a terminal: a channel's hand-over goes to the window,
a scheduled fire asks the window, and a task's answer goes to the window.
An agent nobody holds is offline, and that is the one thing a terminal was
required for.

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
| `providers:` | The agent server (reference resolution, and the list `Ctrl+S` reads at the key), model backends, and the terminal (the status bar's model facts) |
| `default:` | The agent server |
| `agent:` | The agent server and agent-task server |
| `agent-task:` | The agent-task server |
| `subagent:` | The agent server and the local network plugin |
| `embeddings:` | The context store, the cabinet, and the tool hub |
| `context:` | The context plugin |
| `tool_load:` | The tool hub |
| `tool_result:` | The harness, the cabinet's `dump_read` and `dump_grep`, and the media server |
| `mcp:` | `slife2-mcp-tools`, and the tool hub for its source names |
| `rest-api:` | `slife2-restapi-tools`, and the tool hub for its source names |
| `cli:` | `slife2-cli` |
| `skills:` | `slife2-skills` |
| `media:` | `slife2-media` |
| `file-tunnel:` | `slife2-file-tunnel` |
| `peer_agent:` | `slife2-a2a`, the terminal, and the CLI |

Every section has one owner, and that owner reads the section in full. A value
another process also reads is a shared fact, stated rather than accidental: the
plugin reads its own address from `servers:`; the agent server resolves
`provider/model` from `providers:`; the agent-task server reads `agent:` because
a task is the agent working; the terminal holds its own `peer_agent:` mesh
connection. No other section is read by a second process.

`wsl_port_offset` is a top-level number rather than a section. It moves every
configured server port, and only inside WSL's mirrored networking, where the
Windows and WSL loopbacks are one address space and an unshifted second stack
would be silently reused by the first. It defaults to 100 there and `0` disables
it. The shift is applied by the config loader, so every process resolves the same
addresses; the MQTT broker is not shifted, because it is the mesh's rendezvous
rather than an instance endpoint.

An absent section takes its built-in default, not an error. For a capability
whose default is empty that means *off*: no `peer_agent:` means the machine is
not on the peer network; no `media:` providers means the four media tools are
present but refuse, saying what to add; no `wechat:` section exists at all.

`providers:` and `embeddings:` are not switches and have no defaults. The file
must name at least one provider and at least one embedding provider; an absent or
empty section is refused. A built-in provider was a machine calling an address no
file mentioned, and the built-in local embedder was an endpoint the file never
named; both are gone.

A present but invalid section is refused by name at load: an unknown provider, an
unknown `api`, a port that is not a port, a `servers:` host that is not a host,
or a switch written as anything other than `true` or `false` (YAML 1.2 made `no`
and `off` plain strings).

The file is read per turn for everything a conversation can see, and cached by
identity: a reader asks whether an edit happened with one comparison rather than
a watcher or a lock.

### 11.2 Secrets

A value may be a literal, used as written; or `${VAR}`, resolved through the
shell environment, then credstore's OS keyring.

**Output protection covers credstore's values, and only those.** A credential
resolved from credstore is registered in the resolving process and masked in
every surface that leaves it: tool results, tool descriptions, refusals, and log
lines, uvicorn's access log included. The mask shows the first four and last
four characters when at least as many stay hidden, otherwise `***`; a value with
a control character is masked whole, and a value shorter than eight characters is
not registered. An environment variable or a literal in the config is the
operator's; protecting every secret was attempted, and the variety of shapes made
it a maintenance burden.

Resolution is lazy: a process nobody calls never touches the keyring. An
unresolved reference is left verbatim, and the consumer decides: a provider or
media call refuses before use; a tool server's `env:`/`headers:` entry is left
out of the connection with a warning, so an absent variable reaches a child that
knows how to handle one. `config.unresolved_reference` is the single reader of
"this is still a reference"; a reference MUST NOT travel as the secret. A
keyring that is installed and refuses is an error, not "not found".

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

The default location is the checkout when running from one — the nearest ancestor
whose `pyproject.toml` names this project, so a command run from a subdirectory
still writes beside the checkout — otherwise `~/.slife2`. A marker that cannot be
read is refused rather than read as an installation.

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
turn across the agent, context and model logs is therefore a grep for its token;
`turn_done` is where the token and the rowid are joined.

One line is written per **foreground** tool call:
`tool_call name=… ok=… elapsed_ms=… result_chars=…`. Arguments and result text
are not logged; the transcript is where words live. A background call is logged
by `libs/harness/async_tasks.py` when it is scheduled and when it finishes.

The root level is INFO. `SLIFE2_LOG_LEVEL`, read per process, raises it; a value
that is not a level falls back to INFO and logs a warning. HTTP libraries,
provider SDKs, FastMCP and `uvicorn.access` are pinned to WARNING: their debug
output can carry credentials, and an access line is one line per MCP request.
`uvicorn.error` stays at INFO for the run boundaries. The FastMCP banner is
disabled.

A log is written as UTF-8 (§12.3) and masked at the formatter (§11.2). At startup
a log larger than its ceiling is renamed to `<log>.1`; the previous run is kept,
not deleted. An external stdio entry's child writes its own log under `logs/`
(`mcp-<name>-<digest>.log`), so a crash or start-up chatter is attributable to
its entry.

### 11.8 Configuration edits

A model may edit the sections it can read. Every management tool writes through
`libs/host/configfile.py`, the single writer described in §4.1; the result is
validated by the same loader a start uses, and a refused entry is restored.

A `set` writes the whole entry, not a patch. A field left out is a field the
entry does not have, which is what makes switching an entry from `url` to
`command` possible. `set_enabled` changes one field and reads the entry first. A
write preserves an existing file's mode; a mode that cannot be preserved is
logged rather than silently tightened.

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
or when the process exits. A share outlives the turn that made it.

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

A published URL is not a working URL. A transport that has lost the edge keeps
its URL and answers HTTP 530, so reachability is checked separately and the tool
refuses rather than handing out a dead link. A merely unreachable transport gets
a heal window before being respawned. A stop that lands while a start is
establishing the transport invalidates that attempt: the worker releases whatever
it established instead of publishing after `stop` returned.

The tunnel is pointed at a loopback listener that serves `GET /share/{token}`
and nothing else, on a port of its own; the MCP endpoint keeps
`servers.file-tunnel.port` and is not on the public hostname. A tunnel forwards
a port, not a path, so this is what makes the published surface the one route:
`/mcp` on the tunnel's URL is a 404. The 30-hex-character token is the whole
capability.

### 12.2 Media

A media provider is not a chat provider. Its models are declared per kind: image,
video, tts, asr. A vendor may serve several kinds, so `kind` is a fact about the
model and `api:` selects the wire adapter.

`media.defaults:` names a `provider/model` per kind. A name that does not resolve
— an unknown provider, an unknown model, or one of another kind — is refused at
load, naming the `media.defaults.<kind>` line. It is not replaced by the first
model of the kind: a default is the operator's statement, and a typo must not
spend a paid call on a model nobody named.

Artifacts are written under the conversation's work directory: the agent's
`media/`, or a non-fork subagent's `subagents/<name>/media/`. A fork writes to
its maker's. They never go to the process's working directory, which is the
runtime directory and reconstructible. `folder=` is contained inside that root;
it is a model-written string.

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
event — an attempt that did not fail. Nothing else resets a run.

A gate (`tests/test_no_magic_timeouts.py`) rejects a time-style literal anywhere
in `slife2/` unless it is in the registry or carries a `# timeouts:` note. In
`tests/` only two rules apply: a module-level constant with a time-style name,
and a registry lookup used as a function-argument default.

### 12.5 Daemons outlive builds

Reuse is by identity: the launcher attaches to whatever answers to the expected
name and this build's version. A daemon from a previous build answers the name
and still has the old tool signatures, so the handshake's version is what says
it is not this build; `ensure` then refuses the port and names `slife2 down`,
which is the remedy. A rename of a server or a tool must be accompanied by a
stop.

An internal wire carries no compatibility path for a previous build. A progress
line requires every field this build's encoder writes, and a store from another
schema is refused rather than migrated. A fallback for an older shape is stale
logic that hides the version mismatch instead of reporting it.

## 13. Project structure and development

### 13.1 Layers

The layering rule is stated in §2.5. Its purpose is that a module's kind is
visible from its path: a reader who wants the loop opens `libs/turn/loop.py`; a
reader who wants the process that runs conversations opens
`plugins/agent/server.py`. The cost is one more directory level.

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
checks, type checking, the fast lane, and the e2e lane.

The suite is the gate. Both lanes pass before a change is done. A bug found in
the field arrives as a test that fails without the fix.

## 14. Open issues and deferred capabilities

### 14.1 Open issues

1. **A scheduled run and the task that runs it.** A run is tied to its task by a
   name-derived id, which is not unique across two overlapping runs of one task.
   The poll and `schedule_run_now` settle a finished run before starting another;
   *an id minted per run* is still the deeper fix.
2. **A server may be bound beyond loopback.** `servers.<name>.host` accepts any
   host, and every plugin is unauthenticated Streamable HTTP. Binding one to
   `0.0.0.0` therefore lets the network call `job_set`, `mcp_set` and
   `model_set` — arbitrary code and configuration edits — with no credential.
   The design's "loopback is the boundary" is a default, not a check.
   *Resolution would require:* refusing a non-loopback host at load, or an
   explicit opt-in that states the exposure and its authentication.
3. **A cabinet mirror can disagree with its row.** The document's file is
   written after the row's transaction, so a mirror the filesystem refuses — an
   editor holding it open on Windows — leaves the row, its keyword text and its
   vectors committed while the file is the old one or absent. A `CREATE` cannot
   be retried either, because the row now exists. *Resolution would require:*
   staging the mirror and committing the row only once the file is in place, or
   rolling the row back when the mirror write fails.
4. **A multi-path `file_save` is not all-or-nothing.** Each entry is copied and
   committed in turn, so a later URL that cannot be fetched leaves the earlier
   files and rows stored while the call reports failure; a retry stores those
   documents again under `-1` names. *Resolution would require:* resolving and
   fetching every entry before the first write, or answering with the saved and
   the failed parts instead of raising.
5. **A scheduled run can be recorded `missed` while its task runs.** `dispatch`
   records the run `pending` and writes `agent_task_id` only after
   `agent_task_start` returns; a crash in that window makes the sweep classify a
   live task as one that never started, and a manual backfill can run the same
   work a second time. *Resolution would require:* a dispatching marker written
   before the peer call and resolved against `agent_task_list` by name.

### 14.2 Deferred capabilities

The following are not implemented in this version:

| Capability | Why deferred | Current behavior |
|---|---|---|
| Durable interrupted tasks | The continuation state is process-local, like the bridge and reply topic; the profile does not require persistence. | A restart loses a paused task; the peer can cancel or re-send. |
| Markdown rendering | A taste decision about the transcript, not a system gap. | Model output is plain text. |
| Delta coalescing | One progress notification per token is acceptable on loopback. | One progress notification per token is sent. |
| A cap on total agent-task work | `max_running` bounds concurrency; a rate or depth is the missing number. | Total work over time is unbounded. |
| A cap on the number of conversations | The inbox is capped; the number of loops is not. | A client can create conversations without a machine-wide limit. |
| A visible agent-task progress line | The four tools are the model's; the window has no line for a running task. | A person's only lever is to ask the model. |
| A word to the model about the loaded set | The tool descriptions explain themselves. | A model may not use loading unless it reads them. |
| A cap on scheduled tasks | A cron is bounded by time; a runaway is many rarely firing tasks. | `schedule_list`/`schedule_remove` make it visible and stoppable. |
| A word to the agent about unclosed runs | The ledger holds the answer; nothing has watched one go unnoticed. | The agent must think to call `schedule_runs`. |
| Background work inside an agent task | A task runs one turn and has no second turn for a result. | `_async` is refused with an explanation. |
| A conversation id on the turn record | Adding a column means deleting every existing `turns.db`. | No per-turn conversation id is stored; the file name is the id. |
| One config reader | Three readers differ in wording for real reasons. | A reader may not recognize the same problem twice. |
| Selective re-embedding | Per-row content hashing would keep unchanged vectors across a rules change. | A model change re-embeds every row. |
| A cross-encoder rerank for the catalogue and cabinet searches | The context recall is the one whose bad candidate persists into every later turn; both searches are one-shot reads the model can repeat, and their pages order words-first on purpose. | Both use RRF and keep that page order; the cross-encoder (`rerank_model`) is wired to the context recall only. |
| A policy that forces approval | The model-driven `_approve` exists; a named list does not. | `skill_set` and `mcp_set` rely on model judgment. |
| Device-code OAuth for tool servers | `${VAR}` headers cover a token somebody already has. | A server that needs a browser login cannot be configured. |
| A bound on tool-call arguments | Arguments are the model's own words; shortening them is a different problem. | A model can write an unbounded argument into the conversation. |

### 14.3 Candidate capabilities

The following are not scheduled. Each is a capability a mature agent harness
carries, and the row says what it would add and what the system does without it.
The order is a suggested priority.

| Candidate | What it would add | Why it is worth doing |
|---|---|---|
| A question the person answers | An `ask_user` tool, and `_approve` routed to every channel that has a person: the window, WeChat, a peer. | Only the TUI can answer today; a WeChat or peer turn that asks is granted silently. Human-in-the-loop is the difference between an agent that checks a destructive step and one that performs it. |
| WeChat media | Inbound image, voice note and file become an attachment, a transcription or a cabinet document; an outbound reply can carry an image or a file. | The bridge is text-only in both directions. The media server already transcribes and the cabinet already stores images, so the missing half is the bridge, not the capability. |
| A plan the model maintains | A per-conversation plan/todo tool, drawn in the window, with per-item status. | Long-horizon work needs an explicit plan; a plan also survives a context rebuild better than the prose that described it. |
| A spend budget | A `budget:` section (per turn, day or conversation), a usage ledger, and a hard stop. | Usage is measured per turn and logged, but nothing bounds it. On a paid model an unbounded loop is a bill. |
| Scheduled catch-up | A per-task policy for fires missed while no instance ran, instead of a manual `schedule_run_now`. | Cron users expect anacron-like backfill; a laptop asleep at the due moment currently loses the run. |
| Compact inside a turn | Rebuild or trim at an iteration boundary when the live context approaches the ceiling. | The context decision runs only before a turn, so one long tool loop can exceed the window before the next decision that could shorten it. Bounded context is the core discipline of a long-running agent. |
| Retrieval over files and dumps | Chunk and embed saved files and dumps so a search reaches their contents. | §8.3 leaves this out on purpose; documents are the largest corpus an agent saves and the one it cannot search by meaning. |
| Sandboxed jobs | Run `jobs/` code in a child process with declared filesystem and network limits. | Jobs are arbitrary code with the caller's identity. Least privilege is the standard answer, and the plugin boundary already exists to carry it. |
| Tool progress and cancellation | A long tool reports progress down its call; the window draws it and can cancel that call without ending the turn. | A long media or shell call is a spinner today, and the only lever is `Esc`, which ends the whole turn. |
| Branch from a turn | Start a new conversation or fork from any stored turn id, leaving the original intact. | Checkpoints are the standard undo an agent user expects; exploration and retry become possible without resetting the conversation, and the fork machinery already knows how to copy one. |
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
7. **A failure that affects the design's behavior hard-fails rather than falling
   back.** Every remaining fallback is registered in Appendix C, scoped, and
   states what it does not cover.
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

A fallback is a design behaviour, and it is the one kind that is invisible when
wrong. Falling back is allowed; falling back undeclared is not.

A registered fallback states what it does, what it gives up, what it does not
cover, and where the failure goes.

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

| Situation | Fallback | Gives up | Does not cover | Failure goes to |
|---|---|---|---|---|
| A secret reference does not resolve | Left verbatim; fails at the API call | Load-time certainty | A store that is present and refuses (raises) | The API error naming the reference |
| A server reports no name | Probe by tool presence | Exact identity | A server that reports a name (compared exactly) | `open_server` raises, naming the URL |
| The context discriminator fails | Keep the context in hand | The rebuild for that turn | A store that cannot be asked (fails the turn) | The turn runs on its current list |
| The conversation moved while a rebuild decided | Adopt the stored list; the decision is abandoned | The rebuild for that turn | A store that cannot take the write (fails the turn) | A warning naming the abandoned rebuild |
| Tokenizer vocabulary cannot be fetched | Script-aware character measure | Accuracy in both directions | Nothing; this is the floor | A warning, and a retry after a cooldown |
| A tool fails | `ok: false` with text the model reads | A typed distinction between error and result | Harness failures, which fail the turn | The model reads the text |
| A turn fails with an image attached | Drop every image from the live list, keeping the note | The bytes in the live conversation | An image attached after the repair; the record, which never had the pixels | The note, the dropped-image sentence, and the original error |
| Text SQLite cannot encode | Normalise surrogates | The original code unit | Nothing; a failed write fails the turn | U+FFFD in the record |
| A pid cannot be proven ours | Refuse to signal it | `down`'s reach on that machine | A pid it can prove | `down` says it cannot prove ownership |
| A stored turn body will not parse | Read as a turn with no messages | The turn's text and vector | A structural value, which is refused | A warning naming the row |
| A stored `usage` breakdown will not parse, or is not the four-key shape | Read the turn without the split, keeping its totals | The token split for that turn | The turn's messages, which have their own row; a structural value, which is refused | A warning naming the row |
| A live-context id has no row | Drop it and count it | The conversation is shorter | A row that exists but is damaged | A warning naming the ids |
| A tool answer cannot be read | Return `{}` | The distinction at the shared hop | A payload that parses | The caller must check the keys |
| A card has no presence property | Read as online | Telling omission from a claim | A deleted card, refused earlier | A retained offline card or last will |
| An unopenable `schedule.db` | Skip the ledger and log an error | Every task in that ledger while damaged | A programming error, which propagates; bad rows, handled per pass | The error naming the path |
| A stored anchor is not a time | Replace it with the next fire's instant | The damage itself — the old value is gone | A ledger that cannot be opened, which is skipped and logged | The new anchor; the damage is not reported |
| A WeChat cursor write fails | Retry on the next move | This stream's place on disk | The in-memory cursor, which still acks | A warning naming the session file |
| A damaged install has no `qrcode` renderer | Leave the code empty and keep the URL | The scannable code | An installed renderer | The URL in the login answer |
| A server-record or claim write fails | Log and continue | The record | A client record, which refuses the start | A warning naming the path |
| A record is missing or malformed | Read as absent | The pid the record held | Nothing; this is the floor | The writer's warning is the trace |
| A subagent record cannot be read | Treat it as not a fork | The shared work directory a fork uses | A record that parses | A warning naming the record |
| A message has no readable role | Infer it from its shape: a tool result, an assistant tool call, else the user's | The role the sender wrote | A role this build knows | A warning naming the inferred role |
| A Windows job forbids breakaway | Retry without the flag | Survival of the enclosing kill-on-close job | Nothing | A warning saying the server will not outlive its job |
| No ancestor names this checkout | Use `~/.slife2` | The checkout's data directory | An override, which wins; a marker that cannot be read, which raises | A missing database or config |
| The default CLI tunnel binary is not on PATH | Guess the platform's install location | Certainty about which copy ran | An explicitly named binary, which is refused when not found | The resolved path in `tunnel_status` |
| A tunnel liveness or reachability probe cannot answer | Treat the tunnel as alive and its URL as reachable | Telling a broken probe from an outage | A transport the provider proves dead | A debug line and `tunnel_status` |
| A progress line cannot be read | Drop it | The line's display | The terminal event, whose fields the transcript keeps | Nothing; the record is the first channel |
| A turn weighs less than one token | Floor at one | Exactness for the smallest turns | Measured usage, which is the provider's | Nothing; the floor is the answer |
| A probe threw | Treat the peer as not up | Whether the connect was refused or failed mid-handshake | A peer that answers and names another server; a code-level failure, warned | A warning for a code-level failure, debug for a connection failure |
| `taskkill` could not run | Poll the pid and report | Why the force failed | A process that survives the poll | The pid the caller then polls |
| A tool answer is not JSON-serialisable | Render a string form of the value (`default=str`) | The object's exact shape | An answer JSON can encode, or `None` | The rendered text in the record |
| A lone surrogate reaches tool text | Escape it | Nothing the model needs | Text that is not a surrogate | Nothing; the request survives |
| The post-turn trim cannot reach the hub | Skip the trim | The trim for that turn; the set stays over budget | The next turn, which fails loudly on a dead hub | A warning; an error for a task |
| A bookkeeping file is already gone | Delete with `missing_ok` | The distinction between present and absent | An unlink that fails for another reason | A warning naming the path |
| A log cannot be rotated | Leave it in place and warn | The ceiling | A log that is already gone | A warning naming the path |
| `SLIFE2_LOG_LEVEL` is not a level | Log at INFO and name the value | The requested detail | A value that names a level | A warning from the protocol module |
| A trim answer cannot be read | Return `{}`; `trim_answer` refuses it | The trim and the names the model would have seen | A call that could not be made, which raises | A refusal in the server log |
| Closing a link fails | Suppress the teardown error | The report of a transport that did not tear down | The call path, where failures are values | A debug line naming the link |
| The interface language cannot be detected | English | The operator's locale | A locale that resolves | Nothing; the window is English |
| An A2A groups file will not parse | Move it aside as `.corrupt` and start with no groups | Every group in that file | A file that cannot be moved aside, where mutations refuse | A warning naming the path, and the preserved file |
| A continuation has no `messageId` | Compare the request text with the task's last text | Telling a re-minted retry from a continuation that repeats the text | A peer that sends neither an id nor text, which is indistinguishable from a retry | A debug line naming the peer and the task |

Everything not in this register is a defect. A path that substitutes a weaker
behaviour and is not here is an undeclared fallback. A fallback that stops being
needed is deleted rather than left standing, because a designed path that never
runs is a designed path that has stopped being tested.
