# slife2 — Software Design Description

| | |
|---|---|
| **Document** | Software Design Description (SDD) |
| **Version** | 0.2.0 |
| **Date** | 2026-10-17 |
| **Status** | Normative |
| **Audience** | Developers, operators, and reviewers of slife2 |

This document describes the structure and behavior of slife2: what each
subsystem is, how it is decomposed, the interfaces between its parts, and the
constraints that bind them.

The document is normative for the design. The source code is normative for every
implementation detail. Where this document and the code disagree, the code is
authoritative, and this document must be corrected to state the rule again rather
than the incidental value.

The user-facing description of the system is [README.md](README.md). The working
record of design decisions, rejected alternatives, and measurements is
[DESIGN.md](DESIGN.md); it is explanatory, and where it and this document
disagree, this document is authoritative.

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

This document defines the software design of slife2 version 0.2.0. It is written
for a person who will change the code or operate the system. It states:

- the system's architecture and process model;
- the contract every plugin implements;
- the behavior of the turn loop, model backends, tool system, and memory;
- the interfaces between the terminal, the plugins, and the configuration;
- the invariants that a change must preserve.

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
| **Agent** | An identity named by `--agent NAME`. It is claimed once per machine and is not a process, a port, or a data directory. |
| **Client id** | The pair `(agent, subagent)` identifying the conversation a call is made on behalf of. A subagent component is empty for an agent's own conversation. |
| **Home** | A data directory containing `slife2.yaml`, `skills/`, and `agents/`. A home may contain several agents. |
| **Plugin** | One of slife2's own MCP servers. It implements the plugin contract and is started and shared by the launcher. An external MCP server is not a plugin. |
| **Family** | A plugin that owns one configuration section and declares the rows that section describes. |
| **Source** | One entry a plugin holds, such as an `mcp:` server, a `rest-api:` document, a playbook, or a command. |
| **Catalogue** | The tool catalogue: one SQLite file per home holding every row the model may discover. |
| **Conversation** | One client id's history, stored by the context plugin. |
| **Turn** | One exchange: input, model output, and tool calls, stored as one row. |
| **Channel** | The origin of a message: `tui`, `wechat`, `peer_agent:<peer>`, `agent-task:<name>`, `async-tool:<name>`, or `schedule:<name>`. |
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
| [DESIGN.md](DESIGN.md) | Working record: alternatives considered, measurements, and rationale. Explanatory, not normative. |
| Model Context Protocol specification | Wire protocol implemented by every server. |
| A2A-over-MQTT published profile | Wire protocol used by the peer and subagent networks. |
| OpenAI Chat Completions API | Model wire protocol. |
| OpenAI Responses API | Model wire protocol. |
| Anthropic Messages API | Model wire protocol. |
| `slife2.yaml` | Annotated configuration reference shipped with the project. |

### 1.6 Document conventions

- **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are used as in RFC 2119. A
  **MUST** is a constraint whose violation is a defect. A **SHOULD** is a
  constraint that may be departed from only with a stated reason.
- Module and tool names are written in `code font`.
- Paths beginning with `<data>` are relative to a home. Paths beginning with
  `<runtime>` are relative to the runtime directory.
- "The system" means the slife2 package and all processes it starts.

### 1.7 Document structure

Sections 2–4 describe the architecture and lifecycle. Sections 5–10 describe the
runtime subsystems. Sections 11–12 describe configuration, storage, and
operations. Section 13 describes the project structure and development process.
Section 14 records open issues and deferred capabilities. Appendix A states the
invariants, Appendix B lists the plugin registry, and Appendix C is the fallback
register.

## 2. System overview

### 2.1 Purpose

slife2 is a terminal-based AI agent. It runs a conversation with a language
model, exposes a set of tools to that model, and reaches the outside world
through local processes. The terminal window is a client. The agent loop, tool
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
- Internal state is addressed by a client id, not by a session. The MCP transport
  must therefore preserve the caller's identity on every call.
- The model's context window is a hard bound. Every mechanism that adds text to a
  request must have a corresponding bound or eviction rule.

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
`plugins/cabinet/store.py` or `plugins/schedule/expressions.py`. `libs/` holds
code shared by more than one component.

The dependency direction is:

```text
tui/ -> MCP -> plugins/agent/ -> libs/turn/loop.py
     -> {libs/model/base.py, libs/turn/tools.py, libs/turn/events.py,
         libs/turn/messages.py} -> libs/host/config.py
```

The loop imports neither the agent server nor the terminal. It receives a
backend and a coroutine returning a tool registry. The agent server supplies
those; the loop does not know where they come from.

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
| **Isolation** | Credentials stay in the consuming process; state is keyed by client id. |
| **Recoverability** | Turns, the live-context list, the cabinet, and the schedule ledger are persisted. A restart restores the conversation. |
| **Testability** | The loop is pure; servers run over in-memory transports in tests. |
| **Extensibility** | A plugin is a module plus one row in `PLUGINS`; an external tool is one configuration entry. |
| **Observability** | Startup reports each server as it lands; `slife2 status` probes each server; logs are per daemon; usage is logged per model call. |
| **Portability** | Tested on Windows, macOS, and Linux; the runtime has no POSIX-only dependency beyond a documented fallback. |

## 3. Plugin contract

### 3.1 Protocol and transport

A plugin is an MCP server over Streamable HTTP. It MUST:

- answer the standard MCP JSON-RPC methods, including `tools/list` and
  `tools/call`;
- put its MCP name on the wire so a client can identify it;
- serve a probe tool whose presence identifies the server;
- start through the shared scaffold `serve_plugin`;
- accept the common flags (`--data-dir`, `--host`, `--port`, and any section
  flag defined by the protocol module);
- resolve its own address from the configuration or the default.

The shared scaffold `libs/plugin/protocol.py` implements argument parsing, logging,
configuration loading, address resolution, daemon records, and server startup.
A plugin supplies three hooks:

1. what to build;
2. what to say about its own start;
3. what it cannot start without.

The model backends use the same scaffold through
`plugins/llm/server_common.serve_backend`, which is a wrapper, not a second
startup path.

A module under `plugins/` MUST NOT call `parse_serve_args`, `configure_logging`,
or `serve` directly. A test enforces this rule by name.

### 3.2 State keyed by client id

Every server that holds per-conversation state MUST key that state by the client
id `(agent, subagent)`. No client carries a session handle on the server's
behalf.

Consequences:

- There is no "not found" for a caller to handle: state is created on first use
  and a key never goes stale.
- An idle sweep MAY drop state; the next call recreates it, and the caller
  cannot observe the difference.
- Isolation follows from the key: two agents cannot reach each other's state.
- Every call is traceable, because every call carries the id even if the server
  holds no state.

For a model-facing call, the client id travels out of band in the call's `_meta`
as `libs/plugin/audience.client_meta`. It is not an argument the model writes.
`audience.request_client` reads it back. A model-facing call that does not
identify its conversation is refused.

A plugin's internal tools MAY take the client id as an ordinary argument when
their caller already knows the conversation. The context store's `remember`,
`restore`, and `rebuild` do so, because the agent server is the caller and
already owns the conversation. This is the same distinction as the refusal
vocabulary in §3.7: the model chooses to read its history, and must not be able
to say whose; the agent server is the party that already knows whose it is.

The home a call belongs to travels with the same identity. A shared server
resolves a call's files against the caller's home, not against the directory the
server was started in.

### 3.3 Audience and authorization

A tool MAY be called by the model only if the tool declares itself model-facing.
The declaration is a namespaced `_meta` key (`slife2/audience`) read from
`tools/list`.

The rule is opt-in:

- A plugin's internal tools need no mark and are withheld from the model.
- A tool that forgets the mark is invisible, not dangerous.
- An entry under `mcp:` needs no mark; the operator opted in by writing it.

The audience and identity checks are separate:

- **Audience** answers "may a model call this tool?"
- **Identity** answers "on whose behalf is this call made?"

A tool call runs on behalf of exactly one conversation. A model cannot assert a
different client id; the server reads it from `_meta`.

### 3.4 Family plugins

A family plugin owns one configuration section and declares the rows that
section describes. `libs/plugin/family.py` provides the shared behavior:

- hold each entry's connection;
- declare what each entry offers;
- route a call back to the holder;
- build the management tools' answers from the section's own words
  (`FamilyWords`).

The families are:

| Family | Configuration section | Holds |
|---|---|---|
| `mcp-tools` | `mcp:` | External MCP servers |
| `restapi-tools` | `rest-api:` | OpenAPI documents served by an MCP proxy |
| `cli-server` | `cli:` | Commands already installed |
| `skills-server` | `skills:` | Playbooks under `skills/` |
| `cabinet` | none | Documents and saved files |

An entry has one `state` field, read from the file before the link is consulted.
A switched-off entry is `off`, not the `idle` of a connection nobody started. An
entry that answered with no tools is `ready` with zero tools; it is not silent.
The state vocabulary matches the hub's `servers()` answer.

A credential that did not resolve is refused before use.
`libs/host/config.py:unresolved_reference` is the single reader of "this value is
still a reference". A reference that reaches another party's endpoint travels as
the secret itself and returns an authentication error that names nothing.

### 3.5 Plugin registry

Every fact about a plugin that is a fact about the plugin is one row in
`libs/host/config.py:PLUGINS`. A row contains:

| Field | Meaning |
|---|---|
| `key` | The plugin's key in `servers:` and in status output. |
| `module` | The module `python -m` runs. |
| `name` | The MCP name the server puts on the wire. |
| `probe` | The tool used to identify the server when it advertises no name. |
| `port` | The default listening port. |
| `api` | True for a model API backend; absent for a capability server. |

Everything else is derived:

- the start order;
- the addresses in the default configuration;
- the `*_SERVER_NAME` constants;
- the launcher's specs;
- the probe a client uses.

A plugin reads its own name from its row. Adding a plugin is writing the module
and adding a row. Removing one is deleting both. The table is data, not imports:
every module field is a string, because importing a server to read its name would
pull provider SDKs into the terminal and the agent loop.

Two rows may share a module. `peer_agent` and `subagent` are served by
`plugins/a2a/server.py`; the second row is a three-line entry point
(`plugins/a2a/local_server.py`). They are separate processes because they are
separate networks; they have different MCP names so a probe can tell them apart.

Appendix B lists the registry as of this version.

### 3.6 Identity verification

A client MUST prove which server it reached before believing anything the server
says.

`identifies` compares:

- the connected server's `server_info`;
- the name the caller expected;
- the server's own probe tool.

`open_server` is the single place a client connects to one of our servers. It
raises, naming what could not be reached, rather than returning a client that
will fail later.

The check prevents a server on the wrong port or an old build from answering MCP
correctly and being mistaken for the intended server.

### 3.7 Refusal vocabulary

`libs/plugin/audience.py` holds the refusal vocabulary.

- **`UNADDRESSED`** is a sentence template with two holes: what is missing and
  why the server cannot guess. It is used for a call that does not identify the
  conversation it is for.
- **A raised `ToolError`** is the refusal a model reads. It carries one sentence
  saying what is wrong and what to do instead.
- **`protocol.agent_reader`** is the raising idiom for a tool server.
- **`audience.unaddressed`** is the sentence-returning idiom for a plugin's own
  internal API.
- **`protocol.agent_or_blank`** answers "the agent this call is on behalf of, or
  the empty string". It is the shared question behind both idioms.

There is no `[refused]` marker. A refusal is the call's status, not a word in its
text.

### 3.8 Protocol revision compatibility

Four facts about the 2026-07-28 MCP revision bind every plugin and client:

1. **`ping` is gone.** A conforming server answers it with `Method not found`,
   and the specification names no replacement liveness probe. Identity comes
   from the connection handshake: the client reads `server_info.name`, falling
   back to a `tools/list` membership check only when a server reports no name.
   A bare connection test would accept a different server on the same port.
2. **There are no sessions.** `Mcp-Session-Id` and `initialize` were removed
   from Streamable HTTP; a request carries its version and capabilities in
   `_meta` (§3.2).
3. **Change notifications are opt-in.** `notifications/tools/list_changed`
   reaches only a client that has opened `subscriptions/listen`. Against
   FastMCP 4.0.11, `Client` exposes no call that opens one, and a FastMCP server
   advertises `tools.listChanged: false` and answers the listen request with
   `Method not found`. The hub therefore re-reads an aged listing instead of
   waiting for a notification.
4. **A result that is not pushed must ride its request.** With no push channel,
   a queued caller waits on its own call, and the inbox is capped because that
   wait has a client-side timeout.

**Message size.** MCP `tools/call` is request/response, and the client SDK
refuses an SSE event over 1 MiB (`DEFAULT_MAX_SSE_EVENT_SIZE`). Our own hops use
a transport with no ceiling, because a daemon this system started is bounded by
the model's context — `rebuild` and `transcript` send exactly that list. The
gateway to an external MCP server keeps the SDK's ceiling: an external server is
a peer, and the ceiling protects a process from a peer making it buffer an
unbounded message.

## 4. Machine and agent lifecycle

### 4.1 Configuration as the system description

One file, `slife2.yaml`, inside a home, is read by every process. It is the only
place that says what exists:

- which providers and models;
- which servers and addresses;
- which tool families and entries;
- where embeddings come from;
- which model is the default;
- which networks and tunnels are enabled.

The launcher's server list is derived from the configuration, not written
separately. `Config.plugins()` answers what this machine runs:

1. the wire protocols some provider actually uses;
2. the local servers.

A protocol no provider speaks is not started. A provider is a row in a backend's
configuration, not a process of its own.

A malformed file is fatal. A missing file is not: built-in defaults are used. A
file that exists and cannot be read stops the start with a one-line message
naming the file.

`libs/host/configfile.py` is the single writer for edits made by plugins. It
performs a document edit under a cross-process lock, preserves comments, and
hands the result to the same loader used at startup. The loader is the judge. An
entry the loader refuses is restored.

`--a2a-broker HOST:PORT` overrides `peer_agent.broker` for one run. It travels
through the environment so a server started by this command reads the same
address. It overrides the address only; the network's `org`/`unit` remain the
file's. It cannot enable a network whose section is absent. A broker belongs to
the process that started the mesh: if the A2A server is already running, the flag
is refused rather than silently ignored.

### 4.2 Shared servers and launcher

`libs/host/launcher.py` starts the servers a configuration needs and attaches to
those already running. Servers are daemons: a client exiting never stops one, but
the last client out does. `others_running` counts by process liveness, so a
killed client cannot keep the stack alive.

The lifecycle rules are:

1. **A probe decides whether a server is alive, never a pid.** A pid can be
   recycled. Records also carry a process start token.
2. **A pid that exists is not necessarily a running process.** A zombie is
   detected by reading its state. A state that cannot be read is not treated as
   a zombie.
3. **Ready means the expected server answers as itself.** A port that accepts a
   connection is not enough.
4. **Startup is a wave order.** `NEEDS` states which servers must precede which.
   Everything in a wave that has no unmet dependency starts at once. A server
   whose dependency failed is not attempted and is reported with the name of what
   it was waiting for.

Startup is reported as it happens. `ensure_all` is a generator, and the CLI
prints each server as it lands, because bringing up a server blocks for a second
or two and a report held to the end gives no indication of which server is slow.

### 4.3 Agent identity

`claim_agent` holds the exclusive right to run under an agent name for the life
of the client. The claim is taken with a kernel lock that the operating system
releases if the holder dies.

- The name alone identifies the claim. It is machine-wide, not per home.
- A second live instance under the same name is refused by name.
- The claim does not partition servers: servers remain shared with every other
  agent.
- `--headless` is recorded in the claim so the agent server can render an
  appropriate system prompt.
- Which data directory a name used previously is not remembered.
- A sanitized name becomes a filename. A name that reduces to nothing is
  refused; writing to a surprising path is worse than saying no.

### 4.4 `status` and `down`

`slife2 status` reports, for each server named by the configuration, one of:

- `running`, with its pid and start time;
- `running (external)`, when the server answers but no record of ours exists;
- `port held by something else`, when the port is occupied but the expected
  server does not answer;
- `not running`.

`slife2 down` stops the servers this configuration names and that this build can
prove it owns. It also stops a local A2A broker if the peer network is configured
and the broker was started by slife2. Stopping a plugin stops the child processes
that plugin started.

`status` does not report the broker; it reports MCP servers only. The broker is
stopped by `down` and started by the A2A plugin on demand.

### 4.5 Subagents

A subagent is an agent that belongs to another agent.

A subagent:

- has the maker's tool set, with one exception: `subagent_set` and
  `subagent_remove` refuse a subagent caller (see §4.5.3);
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
- is addressed by the second half of a client id: `(maker, name)`.

A subagent is created with `subagent_set` and removed with `subagent_remove`.
After creation, it is reached over the local network with the same operations
as a peer agent, under `subagent_*` names: the direct five, and the six group
tools (`subagent_group_set`, `subagent_group_list`,
`subagent_group_member_list`, `subagent_group_remove`, `subagent_group_leave`,
`subagent_group_send_message`).

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
and media. That record is removed when the agent server next starts: a fork is
session-lived, and the start of a session ends the forks of the last one.
**A fork is never a group member**: it is temporary, so a roster naming one
would keep addressing a name nobody answers; creation refuses it, placement
does not wake it, and its group traffic is dropped. A
subagent has no external identity of its own: WeChat and the peer-agent network
belong to the agent, and scheduling stays with the agent too.

The container level is deliberate. `agents/jack/cabinet`, `agents/jack/media`,
and `agents/jack/turns.db` belong to the maker. A subagent named `cabinet` would
otherwise collide with the maker's own directory.

`subagent.yaml`, beside the conversation, records:

- the model the subagent runs on;
- the role it was made for;
- whether it was made as a fork;
- when it was made.

The file is deleted with the directory — or, for a fork, when the next session
starts. The configuration file holds only machine-wide facts: how many subagents
a maker may have, and where the local network meets.

#### 4.5.2 Role and fork

- **`role`** is a word included in the subagent's prompt. The prompt is the
  shipped `subagent.j2`, not the operator's `agent.system_prompt`, because a
  subagent must be told what it is, whose it is, and where its work comes from.
- **`fork: true`** starts the subagent from a copy of its maker's conversation as
  it stood when the subagent's conversation starts. Nothing about the fork is
  stored: its turns are not recorded, it has no stored context to restore, and a
  restart removes it — its record is deleted at the next start, and the name is
  free for a new fork. The copy is cut back to the last completed exchange,
  because a model provider rejects a conversation ending mid-exchange.

#### 4.5.3 One level only

A subagent MUST NOT create or remove a subagent. `subagent_set` and
`subagent_remove` are refused when the caller is itself a subagent.

Rationale: the local network holds a place for the maker's own subagents and
nothing holds a place for a level below them. A subagent of a subagent would have
no listener for its messages.

#### 4.5.4 Removal

`subagent_remove` performs, in order:

1. tell the local network to release the subagent's place;
2. stop the subagent's loop;
3. delete its directory.

If the network notification cannot be made, the subagent is left unchanged.
Nothing is asked of the subagent and nothing waits for it; removal is a forced
cleanup whether it is idle or mid-turn. The agent server owns the two tools; the
context store is not asked to forget the subagent, because the stored context
lives in the file being deleted.

Removing an agent removes its subagents, because they are directories underneath
it. There is no second record of who made whom.

#### 4.5.5 Local network

The local network is configured under `subagent:`.

- It needs a `broker`. A `peer_agent:` section with no broker uses the same
  address, because a machine meets both networks somewhere and a second
  `host:port` would be a second thing to keep in step.
- The namespace is `{org}/{unit}` and the unit is always the agent's name. The
  code sets it. A file that writes `unit:` is refused, because a unit that was
  not the agent's name would merge two agents' subagents into one MQTT identity.
- `org` defaults to `subagent` and MAY be overridden to group meshes.
- The number of subagents a maker may have is `subagent.max_running` (default
  32). The cap is a bound on runaway growth, not a target.

A subagent's place on the network is held by the local network's process. A
message to a subagent becomes a turn in the subagent's own conversation, wearing
`Agent(<name>)>` — the sender is the maker — while an answer from a sibling
wears `Subagent(<name>)>`. The subagent's answer is drawn on the maker's
terminal as `Subagent(<name>)>`, and it reaches the maker as a message in the
maker's own conversation.

A turn that ends in prose is not a reply. The harness does not send one. The
model's own `subagent_send_message` or `task_response` call is the only way out,
so a task answered in prose remains pending and visible as a model failure.

The first roster read after a start connects the caller's place before reporting.
A refused connection is reported as the tool's `error`, not raised. An unwoken
member remains `offline`.

#### 4.5.6 Watching a conversation

A terminal can watch a subagent's conversation without becoming its window. The
agent server exposes two read-only management tools:

- `subagents(agent)` lists the maker's subagent directories, with the model, role,
  and fork flag read from each `subagent.yaml`. It reads the filesystem, not the
  local network roster: the roster is the model's, and it connects the mesh and
  wakes members to answer.
- `watch(agent, subagent)` is a held-open call. Its first progress frame is a
  snapshot of the stored turns — `transcript`'s own read, so history ends at the
  same place for a watcher and a restarted window. If a turn is in flight, it is
  replayed from a `TurnStarted` frame (the message its caller sent and a watcher
  never saw) followed by the events already produced (the tap). Live turn events
  follow. A cancelled call is the detach. When the subagent is removed, the last
  frame is `Closed` and the call returns.

Watching is a read: it starts no turn, chooses no context, spends no model call,
and never answers an approval request. The watched turn remains unattended in the
loop's sense (`watched=false`), exactly as it is when nobody watches. A display
that falls behind loses frames; the turn never waits for it.

**Rationale.** A turn's events reach the caller of `send_message`, and a
subagent's turn has no such caller with a screen (§4.5.5: the local network runs
it with no progress handler). The watch adds a second consumer of the same events
rather than a second event vocabulary. The store remains the history of completed
turns; the tap is the turn in flight, which the store does not have yet, and it
is replaced at the next turn's start.

## 5. Turn execution

### 5.1 Loop contract

A turn is:

1. send the conversation to a model;
2. stream the response;
3. run the tools it requested;
4. append the results;
5. repeat until the model answers without requesting a tool.

`libs/turn/loop.py` implements the algorithm. `plugins/agent/server.py` owns the
conversations it runs in.

`AgentLoop.run_turn(messages, prompt)` receives:

- the messages to run on;
- the incoming prompt;
- a **backend** with one streaming method;
- a **registry** of tools, refreshed by a callback before every model call;
- an **observer** that may drop, delay, or coalesce display events without
  affecting the conversation.

The loop is a pure function over a message list. It owns no conversation, socket,
or clock.

The iteration cap is `agent.max_iterations`, read per turn from the current
configuration. `0` or an absent key means no cap. A turn that reaches the cap
ends with a stated reason.

A tool that fails is a result, not an exception. `ToolRegistry.execute` answers
`(text, ok)`. `ok=False` is text the model can act on. An exception is a failure
that is not the tool's, such as a dead transport, and it ends the turn.

### 5.2 Harness additions

`libs/harness/` contains what the harness — the system, as distinct from the
model or the person — adds to a conversation. Four mechanisms share one rule:
what the harness adds is a marked address, never a claim of its own.

#### 5.2.1 Markers

`markers.py` defines `[Name:{…}]` as a prefix on a delivered message. A marker is
content: it is stored with the message and travels into the transcript. It is
stripped for display by the reader that knows the vocabulary. A producer and its
reader share one spelling.

#### 5.2.2 Hand-over

`handover.py` provides:

- `hand_over`, which puts a message into an agent's conversation through the
  window showing it;
- `note_to_window`, which draws a harness line without creating a turn;
- `submit_to`, which classifies a delivery answer as **written**, **refused**, or
  **unknown**.

The distinction matters:

- a **refusal** proves nothing was written and may be retried;
- an **unknown** may already have landed and must not be repeated;
- a message that cannot be handed over is a `DeliveryFailed` and is reported as
  an error, because those words were the only answer available to the sender.

#### 5.2.3 Background calls

`_async: true` on a tool call runs it in the background and returns a task id to
the model. The result is delivered back into the conversation as a message. The
call is over when its status is final; delivery is a separate fact
(`Task.delivered`). A task whose hand-back is still in flight is neither running
nor a failed delivery; treating either as the other causes an answer to arrive
twice or a cancel to land on finished work.

#### 5.2.4 Dumps

A tool result too large for the conversation is stored whole as a `dump` row in
the cabinet, keyed by the tool call id. The conversation carries a selection of
the output's own lines, chosen by a **discriminator** model call. The
discriminator receives:

- the result;
- the conversation in hand;
- the current input.

It answers with line ranges and nothing else. It never quotes or paraphrases; the
harness cuts the original at the returned line numbers. A wrong selection costs a
re-read, not a fabricated fact.

The selection is made under the conversation because what a result is worth
depends on what the conversation is doing. What is left out is not lost;
`dump_read` reads it back by searching.

A non-text result is dumped and not shortened: there are no lines to select. The
whole result is kept, a stub says so, and the row's MIME records the decision.
The harness does not sniff payloads to decide what a tool returned.

The harness reaches the cabinet directly for `dump_save`, because that tool
carries no audience mark and the hub holds no row to route. The model's route
back is `dump_read`, which does carry the mark.

#### 5.2.5 Fallbacks

Exactly one step in this path has a fallback registered in Appendix C: an
unreadable answer to the dump discriminator leaves the conversation with the
first window, which is what a model asking for the beginning would have
received.

Every other step MUST succeed. Keeping the output, writing it, and reading the
window back fail the turn loudly if they fail. The alternative — a conversation
carrying an output that cannot be read back — gives up the bound and breaks the
address while looking like success.

### 5.3 Harness tool pairs

Some things the harness must say as the model, because a provider accepts them
only as tool calls:

- `_func_tool_unload` after a trim;
- `_check_new_input` when a message is waiting.

Both are ordinary declared tools whose calls the harness writes into the
conversation (`loop.harness_call`).

Rules:

1. **A pair names a declared tool.** The Responses and Messages backends reject a
   tool call in history that is not in the request's tool list. The tool must
   therefore be in the model's list, which means the model may call it too. A
   model calling `_check_new_input` is the boundary's own move made early.
2. **The id joining a pair MUST be acceptable to every provider as a call id.** A
   provider that refuses the string refuses the request, including replays. The
   Messages backend states the rule on both `tool_use.id` and
   `tool_result.tool_use_id`; the id shape is chosen at the source rather than
   sanitized later.
3. **A pair is written with nothing awaited between its halves.** An interruption
   in the gap leaves an assistant message whose call is unanswered, which every
   provider rejects.
4. **A pair is written only when something moved.** An empty pair every turn
   records something that did not happen.

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
| `schedule:<name>` | The scheduler | `Scheduled(<name>)>` | none; a trigger is not a result |

The channel is recorded, so:

- the marker belongs to the channel;
- the displayed line wears the channel's name;
- a rebuilt conversation still says where each message came from.

The channel is an address (`agent-task:research`); the displayed line says who
answered (`AsyncTask(research)>`). The two names are not derived from each other.
The prefix is decided in one function (`tui/widgets.py:channel_prefix`), used by
both the live prompt and the rebuild.

The local network has **two kinds of speaker**, so it has two channel words. A
subagent's conversation is entered by the agent that made it and by a sibling;
`agent:<name>`/`[Agent:{…}]`/`Agent(<name>)>` says the first and
`subagent:<name>`/`[Subagent:{…}]`/`Subagent(<name>)>` the second. The sender's
kind is the word, not the receiver's, so the model answering a turn and a person
reading it both know whose instruction it is.

A channel that delivers a message owes the conversation a **whole turn**, not a
prompt. A channel that cannot reach a conversation says so and stops; it does not
try a second route.

An injected message can jump the queue. Every source uses the same mechanism:

- what the person types in a window;
- what another channel hands to that window;
- what a headless window's input carries.

All are submitted to the conversation the moment the words arrive. The window
draws the message inside the running turn, in its own bubble, without closing the
answer still being streamed. A rebuilt conversation draws the same line from the
pair in the turn log. There is one queue: the agent server's inbox.

### 5.5 Context decision and memory selection

The agent server owns one loop per client id, created on first use. It reads the
configuration per turn for everything a conversation can see:

- the system prompt;
- the model;
- the iteration cap.

An edit to `slife2.yaml` therefore reaches the next turn, not the next restart.

Before each turn, the context plugin answers one question: what will this turn run
on?

The decision is:

1. **`restore`**, when a conversation starts. It replays the stored live-context
   list.
2. **`rebuild`**, before each subsequent turn. It asks a discriminator model call
   what to keep of the conversation in hand and what to recall from the turn log.

The discriminator reads the conversation in hand rather than the new message
alone, because a follow-up usually names its subject through the turns above it.
Its answer is a selection, never a dump.

Rules:

- **An unreadable reply is not a decision.** `parse` returns `None`, and the turn
  runs on the context it already has. This is the fallback registered in
  Appendix C for every way the discriminator call can fail.
- **A kept turn and a recalled turn are joined by id, in time order.**
  `normalise_ids` collapses duplicates to their first position.
- **A question has two spellings.** `sentences` are read by meaning, one
  question per entry; `query` is the words the keyword leg requires and the
  meaning fallback when there is no sentence. The keyword leg is fused into each
  sentence's list, and a turn keeps its best question; a later sentence can only
  add a turn or raise one — never cost a turn an earlier sentence found.
- **The person's input is a meaning question too.** When a recall asks in
  sentences, the input joins them: the model's sentence is a paraphrase of what
  the input points at. The decision may set `prompt_as_sentence: false` when the
  input names nothing ("hi", "continue", "what about the other one?"). A
  keyword-only recall keeps its query as the one meaning question; a period
  recall is not narrowed by the input.
- **A recall is asked for what is missing.** The kept turns are excluded from
  its candidates — in SQL for the keyword and time legs, after fusion for the
  meaning leg — so a turn in both does not spend a recall place, a distance in
  `k`, or a share of the budget on something already in hand.
- **The live-context list's order is authoritative.** Reads replay it as written
  and never re-sort, because the list encodes what was kept, dropped, and
  recalled, and a selection is not always contiguous.
- **`send_message` answers with every key present**, including `refused` on the
  path where nothing could be refused. A caller that has to test for a key gets
  a `KeyError` on the one branch nobody exercised.
- **A rebuilt turn carries a footnote** naming its id, channel, and span
  (`turn_note`), so a model can distinguish it from a turn it just said.
- **The turn-to-messages builder is one function** (`messages_from_turns`), shared
  by restore and rebuild. Two builders would drift into two conversations.

The context parameters are in `context:`:

| Key | Meaning |
|---|---|
| `ceiling` | Fraction of the model's window a kept context is measured against. |
| `floor` | Fraction a recall may spend when nothing was kept. |
| `recall_limit` | Maximum number of turns one recall may select. |
| `min_similarity` | Similarity a measured candidate must reach; keyword hits are exempt. |
| `timeout` | Seconds before the discriminator call is abandoned and the turn runs on the current context. |

A missing store fails the turn, like any missing plugin. A discriminator timeout,
provider error, or unreadable answer does not.

### 5.6 Cancellation and repair

An interrupted turn keeps what it completed. The batch of tool calls it was in
is completed on the way out, not dropped: the calls that already returned keep
their own answers (including a call whose result was in hand when its fit was
cut), and every call that produced no answer is answered
`(Tool execution interrupted)`. The person's message is kept with it, so a
follow-up "continue" resumes from the work that was done.

A batch of tool calls is written atomically:

1. run every call, keeping each answer as it arrives;
2. fit every result;
3. append the assistant message and its complete set of answers together, with
   no `await` between them — on the interruption path this happens inside the
   `except` that is unwinding the turn.

Nothing that ends a turn mid-batch — an interruption or a failure — can leave an
unanswered tool call in the history. A failure keeps the messages the turn had
completed; an interruption keeps those and the completed batch.

An interrupted turn's write is detached, and the next turn takes its id up
(`settle_record`), so the turn carries a `[TURN:…]` footnote and a keep-list can
name it. A write the store or cabinet refuses is a hard failure: the turn is
unrecorded, so its messages are rolled back to the last backed prefix and the
caller sees the refusal.

The model's tool result is authoritative. Display chunks are not: progress
notifications may be dropped, delayed, or coalesced without affecting the
conversation.

### 5.7 Images

An attached image is live state **and a cabinet document**. The live user message
keeps the `image_url` parts the model reads; when the turn is recorded, each part
becomes an `[Image:{…}]` reference to a cabinet row of the `image` kind, keyed by
the content digest. The turn log keeps:

- the sentence that named the file;
- the tool call and answer naming it;
- the reference: the cabinet key for stored bytes, or the source URL when the
  provider fetches it.

The bytes never enter the row. A rebuilt conversation is therefore text plus
references; a model that needs the picture again calls `attach_image` with the
reference's key (the cabinet answers a `data:` URL via `image_source`) or with
the URL.

A cabinet refusal fails the turn like a store refusal: a row that names an image
the cabinet does not hold is a marker that can never be attached again.

A failed image attachment is dropped from the live conversation with the same
note the record keeps, and the error says so. A provider that refuses an image
refuses every request that carries it; a failed turn's messages are re-sent by
every later turn, so one bad attachment could otherwise make the conversation
unusable. Such a turn is note-only: the image was dropped before the turn was
recorded.

## 6. Model backends

### 6.1 One process per wire protocol

A model backend is a plugin. There is one process per wire protocol, serving
every provider whose `api` is that protocol.

Supported chat protocols:

- OpenAI chat completions (`openai-completions`);
- OpenAI Responses (`openai-responses`);
- Anthropic Messages (`anthropic-messages`).

The embeddings server is separate (`embeddings`). It speaks the embeddings
endpoint and nothing else, and it exists because it is the other component that
holds a provider SDK and a provider key.

Responses is a separate protocol, not a flag on chat completions. It has
different input items, tool shapes, and streaming events. Merging the two behind
one `api` would put two adapters in one file and make the choice a branch in the
server rather than a fact about the configuration.

`api` selects:

- the module to run;
- the server name it must identify as;
- the default port.

An unknown `api` is a configuration error naming the known values.

`API_BACKENDS` and `API_SERVER_NAMES` are views over `PLUGINS`, not second
tables.

### 6.2 Wire abstraction

`libs/model/base.py` defines the neutral vocabulary:

- `Chunk`: a display increment;
- `ToolCallDelta`: one provider fragment of a tool call;
- `Stream`: an in-flight call;
- `LLMBackend`: a way to call a model.

`libs/model/wire.py` encodes a chunk on the MCP progress channel. A display
increment crosses a process boundary as a progress notification.

### 6.3 Streaming

`plugins/llm/server_common.py` implements one pattern, used by all four model
servers:

1. one `stream_chat` tool;
2. provider output pushed down the progress channel as it arrives;
3. the assembled message returned as the tool result.

Rules:

- **The result is authoritative and the chunks are not.** Progress notifications
  may be dropped, delayed, or coalesced by the transport. The returned
  `StreamChatResult` is what the loop appends.
- **An unreadable result is refused, not read as an empty answer.** The client
  checks the payload shape before building a result. A missing result read as an
  empty assistant message is a turn that "succeeds" with nothing in it.
- **A stream that goes quiet is a stall, not a long answer.** `MCPBackend`'s
  drain watches the wait for one chunk. The clock is on the chunk, never on the
  call. A model that streams for an hour is fine. The watchdog is armed by
  iterating chunks, so every caller must read the stream and not only await its
  result. `open_backend` passes no timeout.
- **A provider that does not stream the answer has still answered.** Each adapter
  back-fills what the terminal event carries. The Responses adapter reads the
  assembled text from `response.completed`; the chat-completions adapter reads a
  gateway's deprecated `delta.function_call`.
- **A part a request cannot carry is named, not dropped.** A system turn whose
  content is a list of parts contributes its text and warns about the rest, in
  all three adapters.

The stream assembly is one implementation. A provider difference is
configuration, not a branch:

- `thinking`;
- `reasoning`;
- `store`;
- `temperature`;
- `top_p`;
- `max_tokens`;
- `stream_usage`;
- `input` capabilities;
- `context_window`.

Each server sends only what its configuration asked for. A value the protocol
does not know is refused by name at the point it is read, not passed through to a
provider error.

### 6.4 Provider capability configuration

A model entry declares its capabilities in its own configuration row:

| Field | Meaning |
|---|---|
| `input` | Modalities the model accepts; `text` and `image`. |
| `context_window` | The model's window, used for context fractions and status display. |
| `reasoning` | Whether the model exposes reasoning. |
| `thinking` | Provider-specific thinking setting. |
| `store` | Provider-specific persistence setting. |
| `max_tokens`, `temperature`, `top_p` | Generation parameters. |
| `stream_usage` | Whether the provider reports usage in the stream. |

The terminal reads the same entry for its status bar and image support. A model
that does not declare `image` is not offered image attachment.

### 6.5 Usage and cache accounting

`Usage` counts the cached prefix inside `prompt_tokens`:

- Anthropic's `input_tokens` is the uncached remainder, so its adapter adds the
  two cache fields back in;
- OpenAI-compatible wires already include cached tokens in the prompt count.

`cache_read` and `cache_creation` are parts of the prompt, never additions to it.

`stream_chat` takes a `kind` word:

- `turn` for the loop's own calls;
- `discriminator` for the recall decision, which is the one call that reaches the
  model server outside the loop's backend.

One line is logged per call with the breakdown. A cache hit rate distinguishes a
rebuild from an append; an average across the two lineages could not say which
one missed.

## 7. Tool system

### 7.1 Hub

`plugins/toolhub/server.py` is the only process that decides which tools the
model may see. It holds no connection to any external server and no credential.

The hub serves three set-level tools:

| Tool | Purpose |
|---|---|
| `tool_search` | Search the whole catalogue by keywords and by meaning. |
| `func_tool_load` | Put one or more tools into the model's list. |
| `_func_tool_unload` | Remove tools from the list; the harness also calls it with no names at a turn boundary to enforce the budget. |

The catalogue is a library (`libs/store/catalogue.py`), opened in-process by the
hub. The hub is its only writer. One SQLite file per home
(`<data>/agents/tools.db`) is the catalogue.

`list_tools` answers two numbers: `tools`, the list the model is holding, and
`total`, the size of the catalogue. They are different facts. `total` counts
every category — a skill and a CLI entry are rows with no load state — and every
source, including one that is down and keeps its rows on purpose. It is read on
the ask that already refreshes the declarations, which is what makes it current
without a timer, and the terminal's status bar shows it.

### 7.2 Sources and declarations

A plugin that owns a configuration section holds the connections that section
names and declares what each entry offers. The hub merges the declarations into
rows and routes calls back to the declaring plugin.

This is the other half of credential isolation: the process that starts a child
needing `SERPER_API_KEY` reads that key; the hub holds none.

A plugin is a **required** source. If one of ours cannot be reached, the listing
refuses rather than serving a shorter list. An external entry that cannot be
reached is reported and left out; the model simply has fewer tools.

A declaration is the whole list, and it is refreshed before every search. The
whole list is what makes a deleted entry stop being a hit. Refreshing is what
makes dropping a directory into `skills/` or editing the configuration take
effect at the next search rather than at the next restart.

The hub's own tools are rows too. They are the set-level work that no plugin can
answer: what exists, what the model is holding, and what the budget took back.

A plugin's own tools also state what their answer holds: the `Returns:` section
of the docstring travels in `_meta` under `slife2/returns` (`DeclareReturns` in
`libs/plugin/protocol.py`), because FastMCP drops that section from the
description whenever it parses the arguments. The hub stores it in the
catalogue's `returns` column beside the input schema. An external server may
state the same key; the hub reads it there too, and a tool that says nothing
gets the catalogue's absence sentinel rather than an empty answer.

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
reaches it (`skill_use`); calling a command row says that nothing runs one yet.
Their names are namespaced (`skill:browser-harness`, `cli:yt-dlp`) because one
name is one row.

### 7.4 Loaded set and budget

What the model is handed is the tools it has loaded, not every tool that exists.
The list is re-read from the hub before every model call.

Initial state:

- The tools named in `tool_load.autoload` are loaded, by the name the model
  calls them — a plugin's bare name or an external server's `{server}__{tool}`.
- The names the system itself refers to (the two list tools, the two harness
  pairs and `skill_use`) are required in that list; a configuration that omits
  one fails the start rather than serving a model that cannot manage its list.
- Everything else, a plugin's tools included, is discoverable but not callable
  until loaded. A plugin's set is no longer loaded in full by category: one of
  its tools the model never calls is a prompt spent on every request.

A model can call a name returned by `tool_search` without loading it, but loading
is what keeps it in the list sent with each request.

The loaded set is bounded by `tool_load.threshold` (default 100 function tools).
Over the limit, the least recently **called** tools are unloaded:

- the names in `tool_load.autoload` are never evicted;
- a tool never called since it was loaded is ordered by its load time.

The trim happens at a turn boundary. The harness calls `_func_tool_unload` before
saving the turn and writes the pair into the conversation. The model learns that
the list shrank from the transcript rather than from a failed call to a tool that
is no longer there. Nothing is written when nothing was removed.

### 7.5 Catalogue search

`tool_search` takes two kinds of input:

- **`keywords`**: words to look for. The keyword leg is for names, paths, and
  symbols, and ranks documents carrying all of them first — but it is not a
  filter. A keywords-only call joins them into one sentence and reads it
  through both legs, because a word form the text does not carry would
  otherwise find nothing (`ToolStore.search` measured 23/36 to 34–36 on the
  tool catalogue); a hit may therefore come from meaning without carrying the
  words.
- **`sentences`**: questions matched by meaning. Each sentence is a separate
  question, and another question is another chance to be found; it cannot cost a
  row an earlier result.

The two legs are fused by rank, not by raw score. `bm25` is unbounded and a
cosine depends on the embedding model; a rank is comparable by construction.

Filters narrow the result and never decide it. They are applied in SQL rather
than after fusion, because rank orders every match and cutting to the limit
first would drop rows the filter would have kept.

`unloaded_only` is the one filter that is not an equality: it means
`load_status != 'loaded'`, so a skill or a CLI entry — which has no load state
and is never in the model's list — is still found. The default stays the whole
catalogue: the best answer may be a tool the model already holds, and `loaded`
on the row says so.

### 7.6 Result contract

Everything the model reads is a surface. A tool's answer is as important as its
schema.

- **The answer is a JSON object, with the two exceptions below.**
  `llm.server_common.render_tool_result` renders a `dict` as JSON text on every
  backend. A tool that answers a sentence invents a second format.
- **A listing is a page:** `{"<noun>": [...], "total": N, "limit": L, "offset": O}`.
  `total` is the whole answer to "is there more" and counts what the filters
  match.
- **A refusal or a miss raises `ToolError`**, one sentence saying what is wrong
  and what to do instead. The failure is the call's status: the hub answers
  `ok: false`, the loop marks the call failed, and the window draws it as failed.
- **Two tools answer with text rather than JSON, because the text is the thing.**
  `_check_new_input` hands over a person's message mid-turn; `skill_use` hands
  over the skill's whole `SKILL.md`. Neither is data *about* something, so
  neither is wrapped in an object.
- **No answer exceeds `tool_result.tokens`** (default 8,000; minimum 128). A
  listing pages; an output that cannot be bounded is the harness's dump, and
  `dump_read` is a searching reader within the same budget. A long playbook is
  bounded by that path too: `skill_use` returns the document whole, and the dump
  discriminator chooses the lines the conversation carries.

A field may contain a sentence, because a person reads some answers too. The
rule is that the envelope is one shape, so a reader that knows one tool knows
them all.

### 7.7 Meta-parameters

Every tool accepts three meta-parameters. They are added to every advertised
schema (`libs/turn/tools.advertised`) and stripped before dispatch, so no tool
server sees them.

| Parameter | Semantics |
|---|---|
| `_timeout` | Seconds for this call. Overrides the tool's default. `0` or negative means "use the default", never "no limit". A tool that declares its own `timeout` argument enforces it itself. With neither, the call runs until it finishes. There is no default budget. |
| `_async` | Run the call in the background. The model receives a task id; the result is handed back into the conversation. The result is still bound by `tool_result.tokens`. An agent task's loop has no `_async`. |
| `_approve` | Ask the user before running. The terminal draws an inline row and answers through `answer_approval`. A channel with no person behind it does not ask; the call runs. A caller that can answer declares `watched=true`. |

The loop reads these parameters off the call and strips them before dispatch.

## 8. Memory and indexing

### 8.1 Turn store

`libs/store/db.py` implements one SQLite file per conversation (client id):
`agents/<agent>/turns.db` for the agent's own conversation, and
`agents/<agent>/subagents/<name>/turns.db` for a non-fork subagent's. A fork has
no file of its own; it reads the maker's turn log and never writes it. A fork's
`subagent.yaml` is removed when the agent server starts, so a fork does not
outlive the session that made it. The store holds:

- the turn log;
- the live-context list;
- the indexes over the turns.

A turn is stored as the message list the model saw. The two halves of an exchange
are read from that list rather than from separate columns:

- what was asked is the first user message;
- what was answered is the last assistant message with prose.

There is no migration layer. The tables are created on a file that is not this
build's. A file this build cannot read is reported and rebuilt, not upgraded in
place, because an upgrade path is a permanent second way to build the tables.

The live-context list is a row, not a file. It is written in the same transaction
as the turn that changed it.

A turn can point outside itself. A tool output too large for the conversation is
stored whole in the cabinet, and the turn keeps a marked selection of it. The row
still holds exactly what the model saw, and the whole is reachable from the turn
by the tool call id. Nothing is lost to the record and nothing is re-run to get
it.

### 8.2 Index machinery

`libs/store/index.py` implements the search machinery used by all three corpora:
turns, the tool catalogue, and the cabinet.

Each corpus is searched by two legs:

- **Keyword**: FTS5 over a derived, normalized string.
- **Meaning**: `sqlite-vec` over chunks of a different derived string.

The legs are fused by rank.

Rules:

1. **What a row is found by is not what it is about.** The keyword leg indexes
   everything worth looking a row up by; the vector is built from a text that
   describes what the row is.
2. **A chunk is a row's text cut into pieces, each with a vector.** The vector is
   written first, through the chunk that names it, because a `vec0` row carries
   its own rowid and a vector left behind is a row the next write collides with.
3. **The vector width is discovered, never configured.** A rebuild is decided by
   an identity recorded in the file (model, width, normalization rule), not by a
   migration. `vec0` fixes a table's width in its DDL; a wrong width fails
   silently.
4. **One normalization rule** applies to stored text and to queries alike. Two
   rules would agree everywhere except at the edges.
5. **A list the caller sized is bound in chunks.** SQLite has a limit on the
   number of values in a statement.
6. **One request carries a batch, and an answer shorter than the request is a
   refusal.** A batch is one call because a first run embeds thousands of rows.
   The count is checked because a row is written with its vectors in the same
   transaction; a short answer is a hole that no later pass can see.

### 8.3 Cabinet

`plugins/cabinet/` stores six kinds of document:

| Kind | Key | Write behavior |
|---|---|---|
| `note` | A subject | Replaces. |
| `diary` | A date | Appends a section. |
| `file` | Its path inside the cabinet | Copies the file in. |
| `report` | A title | Appends a section. |
| `dump` | The tool call that produced the output | Written by the harness. |
| `image` | The content digest | Written by the harness; bytes in `pictures/`. |

One table holds all kinds. The kinds are one corpus; a corpus whose rows live in
four tables would have to be unioned before it could be ranked. `kind` says what
a row is, `key` is what a read and a remove take, and columns a kind does not use
are empty.

The cabinet is per conversation. An agent's own conversation has
`agents/<agent>/cabinet/`; a non-fork subagent has its own under its directory;
a fork uses the maker's. A fork is a temporary copy of the maker's conversation,
and its work products belong with it.

A document's Markdown mirror is written in the same operation as its row. The
path is stored rather than derived, so a change to the naming rule orphans
nothing that already exists. Every caller-supplied path is resolved against the
cabinet root and refused if it leaves it.

A subject that is not already lowercase carries a short digest in its filename.
`Python.md` and `python.md` are one file on Windows and macOS, so two notes
sharing one file would let the second write replace the first's Markdown while
both rows stayed correct.

A file's or an image's bytes are never indexed. What is indexed is what the
document is: its name, the paths that place it, and a summary once one is
written. Embedded from a summary alone, every summary-less file would have no
vector, which the sync pass then reports forever as a document it cannot catch
up.

A dump and an image have no model-facing writer. The harness saves a tool output
too large for the conversation with `dump_save`, and a picture a turn carried
with `image_save`; each is bytes or text on disk with a row indexing it.
`dump_read` is the way back for a dump. For an image it is `attach_image`: the
turn's `[Image:{…}]` reference names the key, `image_source` resolves it to a
`data:` URL (or the source URL), and the tool attaches it. An `http(s)` image is
never fetched here; the row keeps the URL and the provider fetches it.

`dump_read` is a searching reader:

- `pattern` finds lines, matched literally and ignoring case;
- `context` adds the lines around each match;
- `offset` reads from a line, negative from the end;
- `limit` reads fewer lines.

Every answer stays within `tool_result.tokens`. A reader that could return more
than the shortening removed would have its own answer shortened next, which is
the system feeding itself. The bound is enforced where the answer leaves.

A dump is in the same corpus as the other kinds, but a search that names no kind
does not see dumps. The usual address for a dump is the marker in the
transcript, not a search. An image *is* seen by a search that names no kind: it
is content a person attached, and its title, key and source are what the
keyword leg reads.

### 8.4 Embeddings

One server (`plugins/llm/embeddings_server.py`) and one client
(`clients/embedder.py`) serve both indexes. Sharing is required because the two
indexes must rank against the same model. Two copies of "ask and check the shape
of the answer" would let the indexes disagree about what an embedding is.

The server holds the provider key for the same reason the model servers do.

The width is measured, and the input limit is asked for:

- the width can be discovered by a probe;
- an input limit cannot be measured without sending an oversized input, so a
  model whose endpoint reports no limit is refused rather than given a guess.

Changing the embedding model — or the text a corpus builds its vectors from —
invalidates the index identity. The vector index is rebuilt whole on the next
start, because vectors from two models cannot be ranked against each other, and
a vector is a function of the text it was made from. The turn store's text
contract is `EMBED_TEXT_VERSION`, folded into the identity beside the model and
width.

## 9. Inbound work

Four kinds of work reach a conversation without a person typing it:

- WeChat messages;
- peer-agent messages and tasks;
- agent tasks;
- scheduled tasks.

They share one policy: what arrives becomes a turn, and the turn records where it
came from (§5.4).

### 9.1 WeChat

`plugins/wechat/` holds one login per agent and turns what arrives into a turn.
A subagent has no login of its own: WeChat is an external identity, and a
subagent is internal to its maker. WeChat exposes no direct API to a program;
the plugin speaks iLink's hosted bridge. `wechat/ilink.py` contains the protocol:

- the QR handshake;
- the long poll;
- the envelopes whose meaning differs from their status.

The file carries the raw agent name. The directory holds the sanitized name; a
session keyed by the directory name would build a second session for one login,
adopt the same token, start a second poll loop, and deliver every message twice.
A file too old to carry the raw name is skipped and named in the log rather than
resumed under a guessed name.

One WeChat account has one live login. A second scan retires the first login
silently; the bridge answers the first with `session timeout`. The plugin reads
that answer as an offline state and reports the reason through
`wechat_check_status`.

The model-facing tools are:

- `wechat_login`;
- `wechat_check_status`;
- `wechat_send_message`;
- `wechat_logout`.

A message from WeChat becomes a turn in the agent's conversation. The plugin
hands it to the terminal showing that conversation, which runs it like typed
input. The turn is recorded with channel `wechat` and a `[Wechat:{…}]` marker
naming the sender. Answering is the model's job; nothing auto-dispatches a reply.

### 9.2 Peer agents (A2A over MQTT)

`plugins/a2a/` carries agent-to-agent traffic over MQTT, using the published
A2A-over-MQTT profile. Its remote instance is the peer network of agents; a
subagent is not a member of it. A subagent exists only on the local instance,
under its maker.

The wire is the `a2a-over-mqtt` SDK's:

- the topic tree;
- the retained agent card and presence;
- the JSON-RPC envelopes and their MQTT properties;
- the task lifecycle and per-task deduplication;
- the requester's retry ladder.

What this package writes is the glue around that SDK. Interoperability follows
from the wire being somebody else's; a self-built binding would be a second
protocol using the same name.

Rules specific to this system:

1. **Presence follows a window.** A `join` call held open puts an agent on the
   mesh. Coming online and going offline are notes, not messages; no turn runs
   and the model is not told. The roster is `peer_agent_list`.
2. **A task is completed by the model's own call.** A `task_response` sent with
   `peer_agent_send_message` settles the inbound bridge before the call returns.
   A turn that ends in prose sends nothing; the inbound message is delivered and
   the model speaks for itself or the task stays pending.
3. **A restart drops in-flight inbound tasks.** The completion bridge and the
   peer's reply topic are process-local. Nothing is persisted and nothing is
   announced. The requester's `peer_agent_task_list` names a task it never heard
   back about, and a re-send arrives as a fresh request with a fresh bridge.
4. **Outcome is one word, and it is the peer's own.** `mesh._reply_state`
   distinguishes completed, cancelled, and failed. The one outcome no reply can
   produce — the retry ladder running out — is `UNREACHABLE`, because it is the
   requester's failure and must not be rendered as a peer's answer.
5. **A withdrawal outranks a late answer.** Cancelling records the cancellation,
   and the task store refuses to let a later result or failure rewrite an ending
   the caller was already told about.
6. **The broker is probed, owned, and stopped with the stack.** The plugin starts
   a broker when the configured one is local and absent, fails the start if it
   still will not serve, and `slife2 down` stops a broker this machine started.
   A record of ownership, not a guess, makes stopping safe.
7. **An inbound response-typed message is acknowledged and dropped.** A peer that
   streams its result reaches the response topic. A stray `task_response` on the
   request topic is not a new task.
8. **A wildcard broker address is refused.** `0.0.0.0`, `::`, or empty is every
   interface, not the machine's loopback broker; starting mosquitto there with
   anonymous access would publish every topic and accept tasks from anybody on
   the network.
9. **`provider:` is a hard constraint.** Mosquitto is the only broker this build
   speaks; any other value is a configuration error, not a silently disabled
   feature.
10. **A broadcast is not delivered to its sender.** The event subscription uses
    MQTT v5's No Local option, so the broker never forwards a member's own
    publication back to it. The sender named in the payload is the receiver's
    attribution; an event that names this member but was published by somebody
    else is refused rather than treated as its own.
11. **A group is a named roster with an owner, kept per member.** The owner
    creates it and names the members; there is no invitation or acceptance.
    Every member persists its own YAML record under its own directory, and
    learns the roster from group traffic. Only the owner may replace the roster
    or disband the group; a member's way out is leaving.
12. **Leaving is autonomous, including for the owner.** A member leaves itself,
    with no one's approval. An owner's departure transfers the group to the
    first remaining member in roster order, or disbands it when nobody remains.
    A fork is never a member: it is temporary, so a roster naming one would
    keep addressing a name nobody answers.
13. **Group traffic is directed, fire-and-forget, and never returns to its
    sender.** One QoS 0 event per recipient on that recipient's own topic; the
    other members receive it and the sender does not. A member that is not
    listening misses it. Every event carries the roster and a generation id, so
    a member that missed a control learns the group from the next message, and
    a delayed event cannot resurrect a group the member left or the owner
    disbanded. **A group chat is casual**: no member owes an answer, and a
    member that stays silent is not `pending` or `undelivered` — a group
    message is not a task, `sent` is only a publish count, and a task request
    is the only send with an expected answer.
14. **Group messages have their own vocabulary.** The peer network uses the
    `PeerAgentGroup` channel and marker; the local network uses
    `SubagentGroup`, for the maker and its subagents alike. The marker carries
    `from` (the sender), the group and its owner. The roster is a separate read
    (`group_member_list`), so a large group is not repeated in every message; the wire
    payload carries it because membership has to converge from traffic. Control
    events use the wire only and never become a marker.
15. **Mentions are text, not delivery.** `@name` and `@all` address members the
    way a chat group does. They are attention: every other member still
    receives the message.
16. **Membership controls are silent.** A join, leave or disband changes the
    local record and nothing else; the conversation learns the current roster
    from the group member list tool, or from the next group message's wire
    event. Membership is not activity: a member that never answers is still a
    member.
17. **A local send names a member; a remote send says it is unverified.** On
    the subagent network membership is the directory set, so an unknown name
    cannot be delivered by any later mechanism — the send is refused before it
    is published rather than retried and reported against a name that never
    existed. The peer network has no authoritative roster (a peer may not have
    published its card yet), so its sends stay asynchronous and return
    `status: "unverified"`: the id means acceptance and publication, not
    receipt. A send that is never answered ends as an `UNREACHABLE` notice in
    the conversation.

The model-facing tools are:

- `peer_agent_send_message`;
- `peer_agent_cancel_task`;
- `peer_agent_list`;
- `peer_agent_task_list`;
- `peer_agent_broadcast`;
- `peer_agent_group_set`;
- `peer_agent_group_list`;
- `peer_agent_group_member_list`;
- `peer_agent_group_remove`;
- `peer_agent_group_leave`;
- `peer_agent_group_send_message`.

`peer_agent_task_list` reports two states:

- `pending`: a task still running;
- `unreachable`: an outbound task whose requester retry ladder ran out.

An unknown id is reported as withdrawn or never seen.

A task id is a `uuid4` hex string. The in-memory task store is shared by every
mesh in the process; a record carries the agent it belongs to, so sharing cannot
confuse two agents' tasks. The store is pruned with a soft cap.

### 9.3 Agent tasks

`plugins/agent_task/` runs one loop of the agent's own on a description, and the
answer comes back as a message.

Properties:

1. **A cap and a name.** `agent_task_start` refuses a duplicate name and refuses
   past `agent-task.max_running`.
2. **A task's turns are not written to the agent's database.** The task reads the
   agent's context and adds nothing to it. What the conversation gains is the
   answer, which the agent server records as a message.
3. **Every failure is a message.** A loop that raises still has an answer to
   deliver. A task that vanished without one would leave the conversation
   waiting.
4. **Background work inside a task is refused.** The task loop runs one turn and
   has no second turn for a background result to arrive in, so `_async` is
   refused with a sentence the model can act on.

A task runs with the agent's system prompt, model, tools, and context decision.
It does not have the turn that started it, because `restore` reads the stored
turn list and a turn is written only when it ends. The description must restate
the relevant context.

A task may start another task, because the tool set is one catalogue and
nothing filters it. `agent-task.max_running` bounds concurrency, not total work
over time.

The answer is delivered as a message on the `agent-task:<name>` channel, with an
`[AgentTaskResult:{…}]` marker. If no window is showing the conversation, the
result is `undelivered` and the log records that rather than running a turn for
nobody.

### 9.4 Scheduled tasks

`plugins/schedule/` starts work nobody asked for at the moment it starts.

`schedule/expressions.py` is the only module that knows cron. It owns the
timezone policy and the error contract for the system. The five fields are
trivial and the corners are not: `day_or` makes restricted day-of-month and
day-of-week a union, and `strict` cross-field validation rejects `0 9 31 2 *`
before it is stored as an enabled task that can never fire.

The data model has two tables:

- a **task**: a name, a description, and a cron expression;
- a **run**: one fire and its outcome.

Run states:

| State | Meaning |
|---|---|
| `pending` | The trigger was handed over but the outcome is not known. |
| `ran` | The work finished; the task's `finished_at` is the run's time. |
| `failed` | The work failed, was cancelled, or could not be started. |
| `missed` | The fire occurred while no process was running. |
| `skipped` | An operator or the model closed a run that will not happen. |

A run is named by the moment it was due, not by when it ran. The row is written
before the work is handed over, so a fire cannot repeat on the next poll. A
hand-over that is refused marks the run `failed`. A run whose outcome does not
come back remains `pending`.

A fire is handed over, not executed. The trigger is delivered to the window
showing the agent as a turn whose only instruction is to call `schedule_run_now`
with the due time. `schedule_run_now` then dispatches the work to the agent-task
plugin, which runs the loop. The schedule plugin is the caller of
`agent_task_start`, so it receives the task id and its poll can close the run
when the task ends. An agent-task plugin that cannot come up is therefore a
scheduler that cannot run, and the launch order reflects that dependency.

Tasks fire only while slife2 is running. On startup, the sweep closes runs left
open by a stopped process, and the poll records the newest missed fire for each
task. `schedule_runs(status="missed")` lists them. A missed run can be backfilled
with `schedule_run_now(name, due_at)` or closed with `schedule_run_skip`.

A trigger is the only thing the clock ever says to a conversation. A trigger
carries no marker, because the marker vocabulary matches a result to what
produced it, and a trigger is not a result. The `schedule:<task>` channel carries
which task, and the trigger text says whose ledger the run is in.

A task's `timezone` is an IANA name. Leaving it out uses the machine's clock as
it is now, which does not follow a daylight-saving change. A repeated local hour
is handled by comparing wall-clock readings: a fire whose reading equals the
anchor's is the same fire.

## 10. Terminal

### 10.1 Connections

The terminal is a client and serves nothing. It holds no conversation state, no
provider knowledge, and no API key.

It maintains three connections:

| Connection | Client | Direction |
|---|---|---|
| Agent server | `AgentClient` | The window asks: `run_turn`, `transcript`, `answer_approval`, `switch_model`. |
| Terminal inbox plugin | `WindowClient` | The window is asked: `attach` is held open and messages are pushed down it. |
| Mesh plugin | `MeshClient` | The window holds a place: `join` is held open while the agent is online. |

`plugins/tui/server.py` is an ordinary plugin with one session per agent. The
window attaches to it and holds the call for as long as it is showing a
conversation. Two windows on one machine are two sessions, and neither is more
special than the other.

A session lives exactly as long as the call that opened it. Closing the stream
cancels the handler behind it, so a window that was killed stops counting. A
message handed to an agent with no window is refused by name (`NO_WINDOW`), and
the caller decides what to do about it.

### 10.2 Rendering and state

- **Markers are stripped for display** by the reader that knows the vocabulary.
- **The channel prefix is decided in one function** (`tui/widgets.py:channel_prefix`).
- **A tool call is drawn as a line with its argument.**
- **Harness lines come from one place** (`tui/notes.py`): history restored, a
  turn cancelled, a peer online, and similar events.
- **A tool panel has four states**: `running`, `done`, `error`, and
  `interrupted`. `interrupted` is about the turn rather than the call: the turn
  ended, was cancelled, or failed while the window was waiting, so no result
  exists and none is coming. It is separate from `error` because the tool did not
  fail; the window never learned how the call ended.
- **A connection has two states on screen**: online and offline. The reasons live
  in the status tool. A line that tried to state them would be a line about the
  plugin's internals.
- **The status bar's `tools` count is the catalogue's size**, not the loaded
  list's length. The hub reports it beside the list on the same ask; the window
  replaces its number with the one the last model call was built from.
- **Model output is plain text.** Nothing a model produces is parsed as markup;
  a `[bold]` a model wrote is seven characters on screen, not a formatting
  instruction.
- **A restored conversation is the server's.** `transcript` is read once when the
  window opens. The window keeps no copy, so what it shows after a reconnect is
  what the server has. A failed read is not an empty conversation; that
  distinction is the difference between "no history" and "history could not be
  read".

### 10.3 Approval

A tool call carrying `_approve` parks the turn and asks the window through
`ToolApprovalRequested` on the progress channel.

The answer is a call of its own (`answer_approval`), because the turn's channel
runs one way: a notification cannot carry an answer back, and the call cannot
wait on state the server holds per conversation, because the waiting turn holds
that state.

Losing the race is not an error. The turn may have ended a moment earlier. A
client that raised over it would call a prompt that arrived too late a connection
failure.

A refusal mounts no tool panel, because a panel that appeared and then said
"denied" would tell a reader the tool ran.

### 10.4 Interface language

The terminal is bilingual: English and Chinese. It is the only bilingual part of
the system.

Detection is the operating system's answer, read once at import:

1. On Windows, the Windows UI-language API. The C locale carries the region, not
   the interface language.
2. Elsewhere, `LC_ALL`, then `LC_MESSAGES`, then `LANG`, most specific first.
3. Chinese locales select Chinese; everything else selects English.
4. Any detection failure degrades to English.

`--lang` overrides detection and is applied before anything is built.

The translation layer is one accessor over a two-language table
(`tui/i18n.py`). There are no catalog files and no dependency. A missing key or
placeholder raises at the call site rather than rendering a blank slot.

The following are always English, whatever the flag says:

- the system prompt;
- harness and plugin tool schemas;
- job schemas and result strings;
- every log line;
- external text, such as a tool result or a peer's message;
- tool labels, because a translated label would put the screen and the tool list
  in disagreement about what a tool is called.

Key caps (`Esc`, `Ctrl+S`) remain English in both languages, because a translated
key label breaks the key-to-action scan. The action word after each key is the
interface's.

A counted noun is two entries in the table, because `str.format` has no plural
machinery. The caller picks between them, because the caller has the count.

A daemon composes the lines it sends (a peer or channel coming online) in its own
process. The daemon is shared and outlives any one window; it was not started with
`--lang`, so it follows the OS locale rather than the flag. A machine whose OS
language and `--lang` agree — which is every machine that does not pass the flag —
never sees the difference.

### 10.5 Unattended operation

An agent is on the peer network while a window holds its `join` call. Everything
else already works without a terminal: a channel's hand-over goes to the window,
a scheduled trigger goes to the window, and a task's answer goes to the window.
An agent nobody holds is offline, and that is the one thing a terminal was
required for.

`--headless` runs the same `SlifeApp` with Textual's headless driver: the same
connections, the same queue, the same turns, the same prompts, nothing drawn and
nothing printed. No branch of the window is conditional on the flag; it is a
mode, not a second kind of agent.

The model is told that nobody is there. The flag is written into the agent-name
claim, which the agent server already reads to render the system prompt, and the
template receives it as `headless`. No instance at all renders the same way: a
peer tasking an agent nobody is running is the same situation.

The output is deliberately nothing. Turns are recorded, so a window opened later
restores the conversation, and presence is on the mesh where peers see it.
Ctrl+C stops a headless run; a kill is honest too, because the broker's last
will publishes the offline card.

## 11. Configuration and environment

### 11.1 Configuration file

One `slife2.yaml` inside a home. Its sections are the system's vocabulary:

| Section | Reader |
|---|---|
| `servers:` | The launcher |
| `providers:` | The agent server (reference resolution), model backends, and terminal (for the picker) |
| `default:` | The agent server |
| `agent:` | The agent server and agent-task server |
| `agent-task:` | The agent-task server |
| `subagent:` | The agent server and the local network plugin |
| `embeddings:` | The context store and cabinet |
| `context:` | The context plugin |
| `tool_load:` | The tool hub |
| `tool_result:` | The harness and the cabinet's `dump_read` |
| `mcp:` | `slife2-mcp-tools` |
| `rest-api:` | `slife2-restapi-tools` |
| `cli:` | `slife2-cli` |
| `skills:` | `slife2-skills` |
| `media:` | `slife2-media` |
| `file-tunnel:` | `slife2-file-tunnel` |
| `peer_agent:` | `slife2-a2a` |

Every section has one owner, and that owner reads the section in full. A few
values are also read by another process that needs the same fact; the shared
readings are stated rather than accidental:

- `servers:` is owned by the launcher; each plugin also reads its own address.
- `agent:` is read by the agent server and by the agent-task server, because a
  task is the agent working.
- `subagent:` is read by the agent server for `max_running` and by the local
  network plugin for the broker and `org`.
- `embeddings:` is read by the two stores that build an index (the turn store and
  the cabinet).
- `tool_result:` is read by the harness that shortens an oversized result and by
  `dump_read`, which caps its own answer with the same number.
- `providers:` is read by the model backends and by the terminal's model picker.

No other section has two readers.

An absent section is a capability that is off, not an error:

- no `peer_agent:` means the machine is not on the peer network;
- no `media:` providers means the four media tools are present but refuse, saying
  what to add;
- no `wechat:` section exists at all; a login is created by scanning.

A present but invalid section is refused by name at load. Examples:

- an unknown provider;
- an unknown `api`;
- a port that is not a port;
- a `servers:` host that is not a host;
- a switch written as anything other than `true` or `false` (YAML 1.2 made `no`
  and `off` plain strings).

The file is read per turn for everything a conversation can see, and cached by
identity: a reader asks whether an edit happened with one comparison rather than
a watcher or a lock.

### 11.2 Secrets

A value may be:

- a literal;
- `${VAR}`, resolved through the shell environment and then the OS keyring;
- `${VAR:-default}`, with a literal default;
- `keyring:<service>/<key>`, resolved directly against the OS keyring.

Resolution is lazy: a process nobody calls never touches the keyring. An
unresolved reference is left verbatim, so the failure lands where the name can be
stated.

`config.unresolved_reference` is the single reader of "this is still a
reference". A reference MUST NOT travel as the secret.

A keyring that is installed and refuses is an error, not "not found". Reading it
as absent would send the reference itself. A credential the store does not hold
is a `ConfigError` naming the reference and the command that stores it. A store
that was asked and failed keeps its own error.

### 11.3 Data directory

A home is a data directory:

- `slife2.yaml`;
- `skills/`;
- `agents/`, one directory per agent;
- `agents/tools.db`, the shared catalogue.

`--data-dir` and `SLIFE2_DATA_DIR` name the home. A child process is spawned with
the resolved directory, so a relative path cannot be re-resolved underneath the
working directory it is given.

A directory may hold several agents. They share its configuration, its skills,
and its catalogue. The tool set is the configuration's.

A name is not a property of a directory. `--agent jack --data-dir D` names the
agent and says where its files go. The name is claimed once per machine. Which
directory a name used previously is not remembered.

The default location is the checkout when running from a checkout, otherwise
`~/.slife2`.

### 11.4 Runtime directory

The runtime directory is `~/.slife2/runtime`; `SLIFE2_RUNTIME_DIR` overrides it.

It contains:

```text
<runtime>/
  servers/      a record per running server: pid, url, config
  logs/         what each server printed
  locks/        per-endpoint start locks
  agents/       agent-name claims
  clients/      which clients use the shared servers, and their home
  homes/        data directories this machine has used
```

The runtime is machine-wide because the servers are machine-wide. A copy per home
could only disagree with the others: a second data directory reporting a stack it
could not see, two launchers cold-starting one port, or a claim lost to whoever
looked in another folder.

The runtime is reconstructible. Deleting it causes the next start to rebuild what
it needs.

### 11.5 Per-home state in shared servers

A plugin is shared, so the process that writes an agent's file is generally not
the one started with `--data-dir`.

The home a call belongs to travels with the call:

- `libs/plugin/audience.py` puts it in the call's `_meta`;
- `libs/plugin/protocol.py:ServeTheCallersHome` establishes it for the length of
  the call;
- `protocol.Peer` carries it on every hop.

`paths.home()` resolves a file against the caller's home. A plugin's own
configuration, records, and logs keep using `paths.data_dir()`.

Everything a plugin derives per home is kept per home:

- a home's catalogue;
- the declarations the hub holds;
- the model list it resolves.

A home is created when an agent starts in it. The client records the directory,
creates `agents/` and `skills/`, and asks the hub for the home's tool list. That
call creates `agents/tools.db`, because the hub is the file's only writer. A hub
that cannot answer fails the start.

A catalogue is complete when it is opened. The hub's own links are the machine's;
what they last listed is written into every home's catalogue, seeded into a home
as it opens. Only declarations are per home, because a declaration is a statement
about that home's sections.

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
- A link is closed when the last home lets go, so the last-instance-out take-down
  still takes everything down.

### 11.7 Records and logs

A daemon writes a log under the runtime directory and records that it is running.
`status` reads the records. `down` stops only what it can prove is ours.

A failed start reports the end of the log, because a real error message is worth
more than a status code. The startup report goes to stdout; a plugin's own output
goes to its log.

### 11.8 Configuration edits

A model may edit the sections it can read. Every management tool writes through
`libs/host/configfile.py`, the single writer described in §4.1; the result is
validated by the same loader a start uses, and a refused entry is restored.

A `set` writes the whole entry, not a patch. A field left out is a field the
entry does not have, which is what makes switching an entry from `url` to
`command` possible. `set_enabled` changes one field and reads the entry first.

The model configuration is live: the agent server resolves a reference per turn,
so an edit reaches the next conversation. `model_switch` is the one edit that
moves the current conversation; a turn in flight finishes on the model it
started with.

There is no approval gate before a write. `mcp_set` with a `command:` records a
program slife2 will start; the write is legible but not gated. The model's
`_approve` is the only confirmation mechanism, and it is model-driven.

## 12. Operations

### 12.1 File tunnel

`expose_local_image` returns a public HTTPS URL for a file, for a vision model
or a chat API that cannot read the machine's disk.

The plugin holds a registry of token to file, pinned to the file's device,
inode, size, and modification time. A link stops working when:

- the file changes;
- `unexpose_local_image` is called;
- the process exits.

A share outlives the turn that made it.

Three providers are supported:

| Provider | Requirement | Behavior |
|---|---|---|
| ngrok | `NGROK_AUTHTOKEN` | Stable dev domain; free tier shows an interstitial to browsers. |
| localhost.run | `ssh` | Random rotating hostname; TLS ends at the provider. |
| cloudflare | `cloudflared` | Random hostname stable for the process. |

An unknown `active:` provider degrades to ngrok with a warning, and
`tunnel_status` reports the provider actually in use. A malformed shape is
refused. The tunnel is a subordinate dependency; a name this build does not have
must not keep the plugin from loading.

A published URL is not a working URL. A transport that has lost the edge keeps
its URL and answers HTTP 530, so reachability is checked separately and the tool
refuses rather than handing out a dead link. A merely unreachable transport gets
a heal window before being respawned, and an unanswerable liveness probe is not
an outage.

The tunnel publishes the MCP endpoint as well as `/share/{token}`. Any caller
who knows the hostname can call `expose_local_image` and read any file the
process can read. What stands between that and a stranger is the hostname's
entropy; closing it properly requires an authorization story MCP does not carry
here. A tunnel is a decision to make this machine reachable.

### 12.2 Media

A media provider is not a chat provider. Its models are declared per kind:
image, video, tts, asr. A vendor may serve several kinds, so `kind` is a fact
about the model and `api:` selects the wire adapter.

Artifacts are written under the conversation's work directory: the agent's
`media/`, or a non-fork subagent's `subagents/<name>/media/`. A fork writes to
its maker's. They never go to the process's working directory: the daemon's
working directory is the runtime directory, which is reconstructible, and a
generated file left there is the one file certain to be lost.

`folder=` is contained inside that root. It is a model-written string;
`folder="../../.."` would be a model choosing where on the operator's machine
to write a file.

Two adapters are supported:

- `dashscope-aigc`: images, video, speech, transcription;
- `openai-images`: image generation only; it refuses the other three by name.

### 12.3 Text encoding and time

A failed database write does not fail the turn; it loses the text. Content
SQLite refuses is normalised first:

- a valid UTF-16 surrogate pair is put back together;
- a lone surrogate becomes U+FFFD.

The UTF-16 codec is the one in the standard library that distinguishes the two.
Every timestamp comes from one place: local time with an offset, seconds
precision. One format is what keeps ordering and range bounds correct.

### 12.4 Timeouts

Every await that can block has a bound, and the bound belongs to the layer that
awaits it. A callee never sets a total for its caller.

There is no turn deadline and no default tool-call budget. A single number
applied to every tool is wrong for most of them. What bounds a call is:

- the model's `_timeout`;
- the tool's own declared timeout;
- nothing.

Where a clock is needed and the work can legitimately be long, it is an
inactivity bound rather than a wall clock. The model stream has one: 120 seconds
of silence, reset by every chunk.

`helpers/timeouts.py` is the single source of truth. It distinguishes:

- **budgets**, which bound an await;
- **cadences**, which say how often something runs.

Consumers read values at call time, never into a module constant or a def-time
default, so a test can patch one. `validate()` asserts the relations that fail
silently, such as an outer bound shorter than an inner one, and fails at import.

A gate (`tests/test_no_magic_timeouts.py`) rejects a time-style literal anywhere
in `slife2/` or `tests/` unless it is in the registry or carries a `# timeouts:`
note. The gate also rejects a registry lookup in a signature, because that reads
the value once at import and is invisible to a patch.

### 12.5 Daemons outlive builds

Reuse is by identity: the launcher attaches to whatever answers to the expected
name. A daemon from a previous build can answer the name check and still have
the old tool signatures, so every turn fails until the stack is restarted.
`slife2 down` is the remedy. A rename of a server or a tool must be accompanied
by a stop.

## 13. Project structure and development

### 13.1 Layers

The layering rule is stated in §2.5. Its purpose is that a module's kind is
visible from its path:

- a reader who wants the loop opens `libs/turn/loop.py`;
- a reader who wants the process that runs conversations opens
  `plugins/agent/server.py`.

The cost is one more directory level.

Every process in the tree is described outside it by one row of `PLUGINS`, which
holds its name, probe, and port. `pyproject.toml`'s console scripts are the third
and last place a plugin is named, and they exist for a person rather than for the
launcher. Every daemon is started as `python -m <module>` in the interpreter and
virtual environment that spawned it, not through a script on `PATH`.

### 13.2 Test tree

`tests/libs/`, `tests/clients/`, `tests/plugins/`, `tests/helpers/`,
`tests/tui/`, and `tests/e2e/` mirror the package. One directory per layer, one
module per module. "Where is the test for this" has one answer.

Shared doubles live in `tests/fakes.py`:

- a stub model;
- a stub embedder;
- fake window and mesh clients;
- factories (`upstream_server`, `plugin_transports`, `config_file`).

A test module MUST NOT import another test module for a fixture. That is how two
lanes end up disagreeing about the shape of a double.

### 13.3 Test lanes

| Lane | Command | Contents |
|---|---|---|
| Fast | `uv run pytest` | Unit and integration tests, every module, across cores. Excludes `e2e`. No API key, no running server, no fixed port. |
| Unit | `uv run pytest -m unit` | Fast, isolated unit tests. |
| Integration | `uv run pytest -m integration` | Tests that bind a socket or spawn a process. Each binds an ephemeral port. |
| End-to-end | `uv run pytest -m e2e` | The whole stack as processes: seventeen daemons, a real launcher, a configuration on disk, and a stub OpenAI-compatible provider. |

The fast lane drives servers over FastMCP's in-memory transport: real MCP
messages and real progress notifications, with no port bound.

The e2e lane proves what no in-memory test can:

- the launch order works;
- every child reads the same configuration;
- a turn crosses five process boundaries;
- a model's tool call is routed by the hub to the plugin process that owns it;
- the answer arrives both as a result and over the progress channel;
- the conversation is still there for the next turn.

The model is the only thing faked in either lane, and it is faked at its own
protocol boundary: a stub backend in the fast lane, a stub HTTP endpoint in the
e2e lane. A change to what the system does with a model cannot pass unseen by
changing only the fake.

### 13.4 Static checks and CI

- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run pyright` (project code only; tests are excluded)

CI runs on Ubuntu, macOS, and Windows with Python 3.13. It performs:

1. a wheel build;
2. an assertion that the interpreter can load SQLite extensions;
3. lint and format checks;
4. type checking;
5. the fast lane;
6. the e2e lane.

The suite is the gate. Both lanes pass before a change is done. A bug found in the
field arrives as a test that fails without the fix.

## 14. Open issues and deferred capabilities

### 14.1 Open issues

1. **An interrupted peer task.** The SDK's `TERMINAL_KINDS` is wider than its
   name: `input_required` ends the requester's retry ladder. This system has no
   way to continue an interrupted task, so such a reply is recorded and read as
   `failed`. A peer that wanted more input is told its task failed.
   *Resolution would require:* a continuation path, or a fourth outcome that both
   the task store and the display carry.

2. **An inbound message that reaches no window.** A peer's message that cannot be
   handed over is logged and dropped, on the assumption that the peer re-sends
   it. The SDK acknowledges a request before the handler runs, so the peer's
   retry ladder has already stopped, and the responder remains parked holding a
   slot.
   *Resolution would require:* an explicit "could not deliver" the peer reads, or
   a delivery retried until a window appears.

3. **A scheduled run and the task that runs it.** A run is tied to its task by a
   name-derived id, which is not unique across two overlapping runs of one task.
   *Resolution would require:* an id minted per run.

4. **Where a retry policy lives.** Two channels keep their own backoff and reset
   it on different events.
   *Resolution would require:* one policy in `helpers/timeouts.py` and one reason
   to reset.

5. **A model call that ends the turn with an empty answer.** The wire is checked
   at the hop, but a provider that legitimately answers with nothing has the same
   shape.
   *Resolution would require:* deciding whether an empty answer is a verdict or a
   failure, and stating it once.

6. **Dump search scope.** A dump is in the same corpus as the cabinet's other
   kinds, but a search that names no kind excludes it. Whether machine output
   should be ranked against a person's notes is not settled.
   *Resolution would require:* a stated rule, not a query filter.

### 14.2 Deferred capabilities

The following are not implemented in this version:

| Capability | Current behavior |
|---|---|
| Executing `cli:` entries | Entries are catalogued and discoverable; no tool runs them. `func_tool_load` reports that the entry is not callable. |
| Executing playbook scripts | `skill_use` returns the playbook text; the model runs commands itself. |
| Fetching a URL in `file_save` | `file_save` copies local files only. |
| A listing tool | A model must search instead of listing; `tool_search` does not promise a list it cannot give. |
| Markdown rendering | Model output is plain text. |
| Delta coalescing | One progress notification per token is sent. |
| The pass behind the context ceiling | A very large turn can exceed the ceiling until the next selection. |
| A cap on total agent-task work | `agent-task.max_running` bounds concurrency only; total work over time is unbounded. |
| A cap on the number of conversations | A client can create conversations without a machine-wide limit. |
| A visible agent-task progress line | The window has no line for a running task and no key that stops one; a person's only lever is to ask the model. |
| A word to the model about the loaded set | A model may not use loading unless it reads the tool descriptions. |
| A cap on scheduled tasks | `schedule_list`/`schedule_remove` make a runaway visible and stoppable; no cap exists. |
| A word to the agent about unclosed runs | The agent must think to call `schedule_runs`; nothing announces a missed run. |
| A trigger nobody acted on | One turn's inattention leaves a run `pending`. |
| Background work inside an agent task | `_async` is refused with an explanation. |
| A client id on the turn record | No per-turn client id is stored. |
| The window wire in a library | `libs/harness/handover.py` imports a server module. |
| One config reader | A reader may not recognize the same problem twice. |
| Selective re-embedding | A model change re-embeds every row. |
| A `grep` search leg | Partial spellings and symbols rely on the two existing legs. |
| A policy that forces approval | `skill_set` and `mcp_set` rely on model judgment. |
| Device-code OAuth for tool servers | A server that needs a browser login cannot be configured. |
| A discriminator result larger than its own window | Such a result fails the turn loudly. |
| A bound on tool-call arguments | A model can write an unbounded argument into the conversation. |

## Appendix A. Invariants

Each invariant is stated as an assertion. A change that violates one is a defect.

1. **A plugin is an MCP server over Streamable HTTP**, started and shared by the
   launcher, honoring the contract: state keyed by client id, an audience on
   every model-facing tool, a checkable identity, and one row of `PLUGINS`.

2. **No capability holds a credential where it is not used.** No provider SDK is
   imported by the agent loop, the terminal, or a library. No key is read outside
   the process that needs it. A reference that did not resolve never travels.

3. **Nothing under `plugins/` is a library or a helper.** Every directory there
   has a `main` and a `SERVER_NAME`; the name is read from its `PLUGINS` row, and
   the `main` is a call to `serve_plugin`.

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

The following table is informative and reflects the registry at version 0.2.0.
`PLUGINS` in `libs/host/config.py` is authoritative.

| Key | MCP name | Module | Default port |
|---|---|---|---|
| `openai-completions` | `slife2-llm-openai` | `slife2.plugins.llm.openai_server` | 24001 |
| `anthropic-messages` | `slife2-llm-anthropic` | `slife2.plugins.llm.anthropic_server` | 24002 |
| `openai-responses` | `slife2-llm-openai-responses` | `slife2.plugins.llm.openai_responses_server` | 24003 |
| `embeddings` | `slife2-llm-embeddings` | `slife2.plugins.llm.embeddings_server` | 24004 |
| `context` | `slife2-context` | `slife2.plugins.context.server` | 24010 |
| `skills-server` | `slife2-skills` | `slife2.plugins.skills.server` | 24031 |
| `cli-server` | `slife2-cli` | `slife2.plugins.cli.server` | 24032 |
| `mcp-tools` | `slife2-mcp-tools` | `slife2.plugins.mcp_tools.server` | 24033 |
| `restapi-tools` | `slife2-restapi-tools` | `slife2.plugins.restapi.server` | 24034 |
| `wechat` | `slife2-wechat` | `slife2.plugins.wechat.server` | 24030 |
| `cabinet` | `slife2-cabinet` | `slife2.plugins.cabinet.server` | 24035 |
| `media` | `slife2-media` | `slife2.plugins.media.server` | 24036 |
| `file-tunnel` | `slife2-file-tunnel` | `slife2.plugins.file_tunnel.server` | 24038 |
| `tui` | `slife2-tui` | `slife2.plugins.tui.server` | 24005 |
| `toolhub` | `slife2-toolhub` | `slife2.plugins.toolhub.server` | 24020 |
| `agent` | `slife2-agent` | `slife2.plugins.agent.server` | 24000 |
| `agent-task` | `slife2-agent-task` | `slife2.plugins.agent_task.server` | 24037 |
| `schedule` | `slife2-schedule` | `slife2.plugins.schedule.server` | 24039 |
| `peer_agent` | `slife2-a2a` | `slife2.plugins.a2a.server` | 24040 |
| `subagent` | `slife2-subagent` | `slife2.plugins.a2a.local_server` | 24041 |

The probe tool for each plugin is the tool that identifies it when its
`server_info` does not carry the expected name. The four model backends probe
`stream_chat`; the peer and subagent network plugins probe `peer_agent_list` and
`subagent_list`; the other plugins probe their first stable, argument-free tool.

## Appendix C. Fallback register

A fallback is a design behaviour, and it is the one kind that is invisible when
wrong. Falling back is allowed; falling back undeclared is not.

A registered fallback states:

1. what it does;
2. what it gives up;
3. what it does not cover;
4. where the failure goes.

| Situation | Fallback | Gives up | Does not cover | Failure goes to |
|---|---|---|---|---|
| No `slife2.yaml` | Built-in default configuration | The operator's sections | A file that exists and does not parse (refused) | `status` prints the model in use |
| A secret reference does not resolve | Left verbatim; fails at the API call | Load-time certainty | A store that is present and refuses (raises) | The API error naming the reference |
| A server reports no name | Probe by tool presence | Exact identity | A server that reports a name (compared exactly) | `open_server` raises, naming the URL |
| A call had no client to make it | Reconnect and make the first attempt | One round trip | A call that went out and then failed, which is never repeated | The tool's error text |
| The context discriminator fails | Keep the context in hand | The rebuild for that turn | A store that cannot be asked (fails the turn) | The turn runs on its current list |
| Tokenizer vocabulary cannot be fetched | Script-aware character measure | Accuracy in both directions | Nothing; this is the floor | Remembered once per process |
| A tool fails | `ok: false` with text the model reads | A typed distinction between error and result | Harness failures, which fail the turn | The model reads the text |
| An image is refused by a provider | Drop the pixels, keep the note | The bytes in the live conversation | The record, which never had them | The note and the error |
| Text SQLite cannot encode | Normalise surrogates | The original code unit | Nothing; a failed write fails the turn | U+FFFD in the record |
| A pid cannot be proven ours | Refuse to signal it | `down`'s reach on that machine | A pid it can prove | `down` says it cannot prove ownership |
| Unknown `file-tunnel.active` | Degrade to ngrok with a warning | The operator's intent | A malformed shape (refused) | The warning and `tunnel_status` |
| A stored turn body will not parse | Read as a turn with no messages | The turn's text and vector | A structural value, which is refused | A warning naming the row |
| A live-context id has no row | Drop it and count it | The conversation is shorter | A row that exists but is damaged | A warning naming the ids |
| A turn file from an older schema | Create an empty table and run | The turns on disk | A file this build wrote | One `logger.error` naming the file |
| A stored tool schema will not parse | Serve the tool with no arguments | The arguments | A schema that parses | The tool's own error |
| A tool answer cannot be read | Return `{}` | The distinction at the shared hop | A payload that parses | The caller must check the keys |
| A card has no presence property | Read as online | Telling omission from a claim | A deleted card, refused earlier | A retained offline card or last will |
| An unopenable `schedule.db` | Skip the ledger and log an error | Every task in that ledger while damaged | Bad rows, handled per pass | The error naming the path |
| A WeChat cursor write fails | Retry on the next move | This stream's place on disk | The in-memory cursor, which still acks | A warning naming the session file |
| A bookkeeping write fails | Log and continue | The record | Nothing; the write is attempted or not | A warning naming the path |
| A record is missing or malformed | Read as absent | The pid the record held | Nothing; this is the floor | The writer's warning is the trace |
| A Windows job forbids breakaway | Retry without the flag | Survival of the enclosing kill-on-close job | Nothing | A warning saying the server will not outlive its job |
| The checkout marker is absent | Use `~/.slife2` | The checkout's data directory | An override, which wins | A missing database or config |
| A stale `media.defaults:` entry | Use the first model of the kind | The operator's default | A wrong-kind or unknown-provider reference (refused) | The answer's own `provider/model` |
| A `SKILL.md` header will not parse | Read as a skill with no header | The declared name, description, and requirements | A header that is a mapping | A warning naming the parse error |
| A CLI tunnel binary is not on PATH | Guess the platform's install location | Certainty about which copy ran | An explicitly named binary, or one found nowhere | The resolved path in `tunnel_status` |
| A progress line cannot be read | Drop it; default counts to zero | The difference between unmeasured and zero | The terminal event, whose fields the transcript keeps | Nothing; the record is the first channel |
| A turn weighs less than one token | Floor at one | Exactness for the smallest turns | Measured usage, which is the provider's | Nothing; the floor is the answer |
| A probe threw | Treat the peer as not up | Whether the connect was refused or failed mid-handshake | A peer that answers and names another server | The probe's log and the launcher's conflict report |
| `taskkill` could not run | Poll the pid and report | Why the force failed | A process that survives the poll | The pid the caller then polls |
| A tool answer is not JSON-serialisable | Render a string form of the value (`default=str`) | The object's exact shape | An answer JSON can encode, or `None` | The rendered text in the record |
| A lone surrogate reaches tool text | Escape it | Nothing the model needs | Text that is not a surrogate | Nothing; the request survives |
| The post-turn trim cannot reach the hub | Skip the trim | The trim for that turn; the set stays over budget | The next turn, which fails loudly on a dead hub | A warning; an error for a task |
| A bookkeeping file is already gone | Delete with `missing_ok` | The distinction between present and absent | An unlink that fails for another reason | A warning naming the path |
| The trim tool is missing or unreadable | Answer empty, log the reason | The trim and the names the model would have seen | A trim the hub answers | A warning in the server log |
| Closing a link fails | Suppress the teardown error | The report of a transport that did not tear down | The call path, where failures are values | Nothing; a teardown must not replace the caller's work |
| The dump discriminator reply is unreadable | Carry the first window | The selection | Keeping, writing, and reading the dump, which fail the turn | An info line naming the call |

Everything not in this register is a defect. A path that substitutes a weaker
behaviour and is not here is an undeclared fallback. A fallback that stops being
needed is deleted rather than left standing, because a designed path that never
runs is a designed path that has stopped being tested.
