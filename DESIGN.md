# slife2 — design

**A terminal agent whose plugins are MCP servers.**

This document records the decisions behind the first cut: what was chosen, what
was rejected, and — where it matters — the measurement that forced the choice.
It is written to be read before changing the code, because several of these
decisions look arbitrary until you know what happens if you undo them.

**Where this sits among the four documents.** [README.md](README.md) is the
user's manual and, with [sdd.md](sdd.md), the *normative* pair: what the system
offers and how it is built, both answerable to the code, and the SDD is what to
read to find out what a subsystem *is*. This file is the argument behind those
two — why the shape is what it is, and what each alternative cost — and
[designer_notes.md](designer_notes.md) is the designer's own memory of intent.

---

## Contents

**Every section here is a decision, and the title is the decision.**  The
subsection titles are what a reader is looking for — *the agent task, and what a
second loop costs* — so they are listed rather than summarised; a gloss on one
would be a second title for the same thing.  The chapters below carry one,
because a chapter is a subject and not a decision.

1. [The shape](#1-the-shape) — the plugin/library split, and what each port to this shape cost
   - [1.1 Plugins and libraries](#11-plugins-and-libraries)
   - [1.2 The plugin that is not a family](#12-the-plugin-that-is-not-a-family)
   - [1.3 The agent task, and what a second loop costs](#13-the-agent-task-and-what-a-second-loop-costs)
   - [1.4 The cabinet, and what a kind of document costs](#14-the-cabinet-and-what-a-kind-of-document-costs)
   - [1.5 A second provider table, and what a *kind* is on](#15-a-second-provider-table-and-what-a-kind-is-on)
   - [1.6 The file tunnel, and the first second route](#16-the-file-tunnel-and-the-first-second-route)
   - [1.7 The scheduled task, and what a clock costs](#17-the-scheduled-task-and-what-a-clock-costs)
   - [1.8 The A2A mesh, and what a per-process identity costs a shared one](#18-the-a2a-mesh-and-what-a-per-process-identity-costs-a-shared-one)
   - [1.9 The unattended agent, and what a window was for](#19-the-unattended-agent-and-what-a-window-was-for)
   - [1.10 The subagent, and what a peer without a process costs](#110-the-subagent-and-what-a-peer-without-a-process-costs)
   - [1.11 The interface language, and the line a shared daemon cannot be told](#111-the-interface-language-and-the-line-a-shared-daemon-cannot-be-told)
2. [Streaming: one pattern, used twice](#2-streaming-one-pattern-used-twice) — `tools/call` is a request/response and a TUI needs tokens as they arrive, so one pattern rather than two special cases
3. [The loop, and why the server owns it](#3-the-loop-and-why-the-server-owns-it) — stateless servers, and the largest change of the implementation
4. [Shared servers, and the launcher](#4-shared-servers-and-the-launcher) — the servers as shared infrastructure, the machine's bookkeeping, and the home a call is served from
5. [The context store](#5-the-context-store) — the turn log, the recall, and the decision about which turns a conversation runs on
   - [5.1 The live context, and the rebuild](#51-the-live-context-and-the-rebuild)
   - [5.2 A result that does not fit, and the second discriminator](#52-a-result-that-does-not-fit-and-the-second-discriminator)
6. [Compatibility notes](#6-compatibility-notes) — four facts about the installed library the code depends on, each measured rather than read
7. [The agent loop](#7-the-agent-loop) — `loop.py` as a pure function over a message list
   - [7.1 The clocks](#71-the-clocks)
8. [The toolhub](#8-the-toolhub) — where the model's tools come from, and the one process that decides it
   - [8.1 What the hub owns, and how a tool gets into the set](#81-what-the-hub-owns-and-how-a-tool-gets-into-the-set)
   - [8.2 Sources: what a plugin holds, and how it declares it](#82-sources-what-a-plugin-holds-and-how-it-declares-it)
   - [8.3 The list, and what says when something is missing](#83-the-list-and-what-says-when-something-is-missing)
   - [8.4 The catalogue, and the search over it](#84-the-catalogue-and-the-search-over-it)
   - [8.5 The budget](#85-the-budget)
   - [8.6 Names, and the links the hub holds](#86-names-and-the-links-the-hub-holds)
   - [8.7 Editing the config](#87-editing-the-config)
   - [8.8 The three meta-parameters](#88-the-three-meta-parameters)
   - [8.9 What a tool answers, and what two vocabularies cost](#89-what-a-tool-answers-and-what-two-vocabularies-cost)
9. [Deferred](#9-deferred) — named so they are decisions rather than oversights: what is missing, where it would go, and what the current answer costs — and, first, the two of v1's features this build dropped rather than deferred
10. [Fallbacks](#10-fallbacks) — the one kind of behaviour that is invisible when it is wrong
   - [10.1 The four deformations](#101-the-four-deformations)
   - [10.2 The register](#102-the-register)

---

## 1. The shape

**"Plugin" is the word for one of ours, and it names an implementation rather
than a protocol.** A plugin is an MCP server over Streamable HTTP — the same
transport, the same JSON-RPC and the same tool listing as the servers under
`mcp:` — plus the *plugin contract*, which is the part this project adds on
top: state keyed by a client id rather than by a session, a tool that says who
may call it, and an identity another process checks before it reuses what it
found (`slife2.libs.plugin.protocol`).  A **client id** is who a call is for —
**the name of an agent, and which of its conversations** — and **where the work
belongs: the home**, the data directory the call came from (§4).  The two halves
answer different questions and it is worth keeping them apart: a name picks out
one agent on this machine, once, and the home says which directory *this call's*
files are in.  The home was implied for as long as a machine ran one data
directory and had to be said once shared processes began serving several; the name
needed no such widening, which is the point of it.  The word is worth having because "MCP server" does
not separate the servers slife2 starts from the twenty somebody else wrote, and
that line is load-bearing everywhere below: a missing plugin fails the turn,
where a missing entry under `mcp:` is a model with fewer tools.

**Every server here is a plugin**, brought up on demand by `slife2` and shared
by every instance (see §4) — and the terminal is not a server at all: it is a
client of two of them, the agent it asks for turns and the plugin that hands it
messages (§1.2).  There used to be an exception to that sentence, and removing it
is what `slife2-tui` is:

```
slife2                    TUI, MCP client              (no provider key, no SDK)
  │  HTTP 127.0.0.1:8000/mcp
  ▼
slife2-agent              agent loop, MCP server       (no provider key, no SDK)
  │  MCP client
  ├── HTTP 127.0.0.1:8010/mcp ──▶ slife2-context         (one SQLite file per client id)
  │                                 │  the turn log, and the decision about it
  │                                 └── HTTP 127.0.0.1:8004/mcp ──▶ slife2-llm-embeddings
  │                                                                    (openai SDK, holds keys)
  ├── HTTP 127.0.0.1:8020/mcp ──▶ slife2-toolhub                (the tool set)
  │                                 │  the tool catalogue, in-process (`slife2.libs.store.catalogue.ToolStore`)
  │                                 ├── :8030/mcp ──▶ slife2-wechat        (a WeChat login per agent)
  │                                 │                  └── MCP ──▶ slife2-tui    (into that agent's window)
  │                                 ├── :8035/mcp ──▶ slife2-cabinet       (notes, diary, files, reports)
  │                                 ├── :8036/mcp ──▶ slife2-media         (images, video, speech, transcripts)
  │                                 ├── :8031/mcp ──▶ slife2-skills        (`skill_use`, the playbooks)
  │                                 ├── :8032/mcp ──▶ slife2-cli           (the `cli:` entries, as rows)
  │                                 ├── :8033/mcp ──▶ slife2-mcp-tools     (holds the `mcp:` entries)
  │                                 │                  └── MCP ──▶ external tool servers (stdio or http)
  │                                 └── :8034/mcp ──▶ slife2-restapi-tools (holds the `rest-api:` ones)
  │                                                    └── MCP ──▶ the OpenAPI proxy an entry expands into
  ├── HTTP 127.0.0.1:8001/mcp ──▶ slife2-llm-openai             (openai SDK, holds keys)
  ├── HTTP 127.0.0.1:8002/mcp ──▶ slife2-llm-anthropic          (anthropic SDK, holds keys)
  ├── HTTP 127.0.0.1:8003/mcp ──▶ slife2-llm-openai-responses   (openai SDK, holds keys)
  └── HTTP 127.0.0.1:8005/mcp ──▶ slife2-tui                    (a session per agent)
```

The last edge is the one that goes the other way, and it is worth reading
twice.  `slife2-tui` is the terminal's own inbox: the window holds an `attach`
call open to it for as long as it is showing a conversation, and a channel
hands that agent's window a message with `submit`.  So the window is a *client*
of this one too — nothing serves the terminal, and the thing that knows where
each terminal is is an ordinary plugin, started and shared like the rest.

Two things reach the embeddings server — the context store, for a turn's
vectors, and the hub, for a tool row's — because what needs a vector is an index
and there are two of them. They share one *client* (`slife2.clients.embedder`) and one
server, which is the arrangement that keeps them ranking against the same model:
two copies of "ask and check the shape of the answer" is how the two indexes
would come to disagree about what an embedding is.

**One plugin, one job, and the granularity is deliberate.**  A model backend
speaks one wire protocol; the context store keeps a conversation's turns and
decides which of them it runs on; the hub is where the tools come from; skills
reads the playbooks, cli owns the command registry, and mcp-tools and
restapi-tools hold the servers those two sections name; wechat holds a login per
agent and turns what arrives on it into a turn; the cabinet keeps four kinds of
private document and searches them as one corpus; the agent loop runs
turns.  A provider is a row in a backend's config
rather than a process of its own, so three providers that happen to speak two
protocols are two model processes and not three — the smallness is in what each
process *does*, not in how many there are.  The count in the diagram is what one
config uses, not a fixed number: a protocol no provider speaks is not started at
all, and the hub is one process whether one plugin holds a source or twenty do.

`slife2-cli` is that rule at its most extreme — a family with no connection,
no tool and nothing but rows to declare, and still a process of its own — and
the reason is the same in both directions: a job belongs to the process that
does it, and a process that has one is a process that can grow the tool the
family is missing. See §8.

The two OpenAI entries are the point worth checking, because they look like
duplication and are not.  **Responses is a different wire format, not a flag on
chat-completions** — different input items, differently-shaped tools, different
streaming events — so it is a protocol, and a protocol is a process.  Merging
them behind one `api` would put two adapters in one file and make the choice a
branch inside the server rather than a fact about the config.

Two properties fall out of this and are the reason for it:

- **A provider API key exists only inside the model server process that needs
  it.** The agent loop cannot leak one because it never has one. The same holds
  for tools, which is the second thing the arrangement buys: `SERPER_API_KEY` is
  read by `slife2-mcp-tools` and `GITHUB_TOKEN` by `slife2-restapi-tools`, each
  exported into the environment of a child process that plugin starts, and the
  agent that asks for the tool never sees either. The hub decides what the model
  may call and holds no key at all — which is the point of moving the connections
  out of it.
- **The agent loop imports no provider SDK.** Its only backend talks MCP, so
  switching providers is changing a URL. `grep -rn "^ *from \(openai\|anthropic\) import"
  slife2/` matches four files, all under `llm/` and all servers, each importing
  inside the function that builds the client: three model servers, one per wire
  protocol, plus the embeddings server. Nothing else in the tree imports either
  — and the fourth is the one worth checking, because it is not a *model*
  backend: the two things with an index reach it for their vectors, and it holds
  a key for the same reason the others do rather than for any reason of its own.
  It is also the only plugin kept after the test in §1.1 was applied, which is
  the same fact said the other way round.

The cost is one JSON-RPC hop per token on loopback. That is small and it is the
price of the architecture; `ProgressObserver` is where a coalescing fix goes if
it ever stops being small.

**There is a second cost of a different kind, and it is about size rather than
frequency.** The unit the hop carries is one JSON-RPC message, and the MCP
*client* puts a ceiling on one: `httpx2`'s SSE parser refuses an event over
1 MiB (the SDK's `DEFAULT_MAX_SSE_EVENT_SIZE`). This system reached it in a real
session, as `send_message` failing with "Server-sent event exceeded the 1048576
byte limit" — and the wall is entirely on the client and entirely inside our own
process, because the MCP server bounds neither a body nor an event and neither
does uvicorn.

**So it is off for our own hops and on for anybody else's**, which is why
`slife2.libs.plugin.protocol` has a transport of its own rather than a setting: what the
ceiling buys is keeping a *peer* from making this process buffer an unbounded
message, and a peer is exactly what a daemon we started is not. `slife2.clients.gateway`,
which dials the entries under `mcp:` and `rest-api:`, keeps it. FastMCP 4.0.11
does not pass the parameter through, which is the only reason reaching the knob
takes a class rather than an argument.

**`None` rather than a large number**, because a number is the same wall further
away: the largest message this system can produce is bounded by the model's
context — `rebuild` sends exactly that list, and so does `transcript` — so there
is no size at which one of our own conversations is too big, and nothing here
needs a degraded mode to fall back into. What is given up is the *reporting*: an
overrun is now the machine's memory instead of a named `SSEError`, and that is
only an acceptable trade because the peer is a process this system started.

### 1.1 Plugins and libraries

**A plugin is a capability; a library is its implementation, and the two are not
alternatives.**  v1 says the same thing by shipping one component as both:
`memdb` is an MCP server *and* `store.py`, imported by the host and by four other
plugins; `mcp_gateway`'s own docstring says it "ships with Slife as its MCP
plugin but has no dependency on it". A capability is a plugin so that anything
across a process boundary reaches it one way — list, call, progress — and the
code that implements it is a library so that a process which already has it does
not pay a hop to use it.

Which one a component needs is decided by **what it holds**:

| it holds | it is | why |
|---|---|---|
| a credential, a provider SDK, somebody else's connection | a **plugin** | a library would put the key and the SDK in every importer, which is what §1's two properties forbid |
| a file, and nothing else | a **library** | a file needs no process, and one writer is what a SQLite lock already is |
| a mechanism more than one store is made of | a **library** | a store that owns it makes every change to it a change about *its* corpus, and a copy per store is how one bug has to be found and fixed twice |

So `db` is a library — it holds the turn files, and nothing else — and
`slife2.libs.store.catalogue` is the same row of that table for the other file this system
keeps, one per data directory rather than one per agent. The embeddings server
stays a plugin because it reads an
endpoint and a key off `embeddings:` and links `openai`. The `mcp-tools` and
`restapi-tools` families stay plugins under the same rule, pointed at somebody
else's processes rather than at a file: `mcp:` and `rest-api:` are config
sections, and the plugin that owns a section is the one that holds the
connections its entries describe (§8). The gateway underneath them
(`slife2.clients.gateway`) is a library, and always was. **And the one *writer* of
`slife2.yaml` is a library too** (`slife2.libs.host.configfile`), which is the same row of
that table rather than a new one: it holds a file and nothing else, the four
plugins that own the four sections import it, and the cross-process lock it
takes is the other half of "one writer" — a SQLite lock for a database, a kernel
mutex for a file both a human and four daemons edit.

**`slife2.libs.store.index` is the third row of that table, and it is the row the first two
do not predict.** It holds no file and answers no connection: what it is is the
mechanism a store is *made of* — the FTS5 and `sqlite-vec` tables, the identity
recorded beside them that decides a rebuild, the keyset paging, the rank fusion
over the two legs — and three corpora are made of it: a conversation's turns,
the tool catalogue, and the cabinet's four kinds of document. It was `slife2.libs.store.db`'s
until two things said otherwise. The cabinet was importing twenty-two names out
of a module about turns, which made every change to the machinery a change to a
file about something else. And a rule stated once is a rule the second copy can
be missing without anything saying so: `_rowid` is the case in point, where a
missing rowid is a fault rather than a zero, and the copy that read it as `0`
was the one nobody had looked at.

The cost of a library is the error boundary: a plugin that is gone fails the
turn with a named peer, and a file that will not open raises where it is used.
That is the right trade for a file — the traceback lands in the process that can
describe it — and the wrong one for a remote service. And it does not change
with the number of agents: the servers are shared by every
instance (§4), so the multiplier for a library is the number of *processes* that
import it, not the number of agents.

**A plugin is described once, and that is what makes it replaceable.**  The rule
above says which side of the line a capability falls on; this says what it costs
to *be* one, and the cost was five statements of the same facts.  A plugin's MCP
name was a constant in `config.py` and a `SERVER_NAME` literal in its own module,
held in step by fifteen spelled-out assertions; its module, its name and the tool
a probe identifies it by were a second table in `launcher.py`; the order it
starts in was a third list; its port was a row of `default_config`'s `servers`
table; and `pyproject.toml` had the console script.  Two test functions existed
only to notice when two of those disagreed — which is `Invariant 8` being paid
for rather than honoured, and the arrangement `libs/plugin/protocol.py` refuses
in as many words about the two source names it replaced ("three copies of `"cli"`
and a test to hold them together is what this replaces").

**So there is one table and everything else is derived**: `config.PLUGINS`, one
row per server, carrying the key, the module, the name, the probe tool and the
default port, with `api` marking the four that speak a wire protocol.  The
constants, the two dicts the launcher used to key by hand, the start order, the
ports `default_config` begins from and the launcher's `ServerSpec`s are all
*views* of it now, and a plugin binds its own `SERVER_NAME` to its own row.  What
that buys is the sentence a reader wants: **to add a plugin, write the module and
add a row; to remove one, delete both.**  What it costs is that the table cannot
be checked against the modules by construction — the module path is a string,
because reading one means importing it, and the agent loop must never reach
`openai_server` — so one loop in `tests/libs/host/test_config.py` imports each
row's module and checks it binds the name the row claims.  That is the half a
table genuinely cannot check about itself, which is a smaller thing to keep in
step than five lists were.

**The same rule applied twice more, one layer down.**  A server's `main` was
eleven lines with three implementations (`serve_plugin`, and hand-rolled copies
in `agent/server.py` and `llm/embeddings_server.py`, plus `serve_backend`'s
second body for the model backends); it is one function with three hooks, and a
gate refuses the next copy.  And the words a server says — the refusal sentence,
"the agent this call is on behalf of" — were spelled in the modules that produce
them, including one defined in a *client* of the agent server.  They are
`libs/plugin`'s now, which is where the rest of the contract is.  **The
`[refused]` prefix that paragraph used to name is gone** (§8.9): a refusal is a
raised `ToolError` now, so the marker and the predicate a client parsed it with
had no producer and no reader left.

**The same question asked again, one layer down: the machinery, and the copies
whose own docstrings had already admitted it.**  A later pass for the same
question — *how many places state this* — found five more, and four of them said
so on the page: `ToolStore._embed` and `TurnStore._embed_turns` were the same
twelve lines with the corpus noun changed, and the second one's docstring read
"The catalogue's `_embed` with turns in it"; `plugins/a2a`'s plain YAML writer was
"set up the way `plugins/wechat`'s is"; a comment in the turn store's save path
enumerated the sibling paths that refuse a short embedding answer — while being
the one that did not.  The thread hop that keeps a blocking SQLite call off the
loop every server answers from had four bodies in four modules, three of them
untyped, so `helpers.threads.off_loop`'s `ParamSpec` is the first time those
forty-seven call sites were checked at all.  And the cabinet served five kinds
through ten tool bodies that differed only in the kind and in the words a miss is
refused with.

**What decided each merge was whether the difference was the point.**  The
cabinet's misses are model-facing text — "there is no note about {subject!r}"
against "there is no report called {title!r}" — so `page_of`, `missing_row` and
`remove_of` take them as arguments and each tool stays the kind it serves; the
sentences are the *reason* a raised `ToolError` carries now (DESIGN.md §8.9),
which is the same place they were.  The
embedding count check is one rule with one consequence, so it is one function
(`index.checked_vectors`) with the corpus noun passed in for the sentence, and
the batch it wraps is one call because a first run has thousands of rows.  **That
one is not only tidying: gathering the five seats found a fourth corpus that had
none.**  `CabinetStore.vectors` handed its pieces to the model and wrote whatever
came back, and the cabinet is the worst place for that — `place` marks a document
tried whether or not it has vectors, so a partial answer left the rest of the
document unreachable by meaning with `sync` reporting the cabinet ready, which is
the exact hole the check exists to close.  Its search path was unchecked too, and
there the symptom was a `zip()` of two lists of different lengths rather than a
named fact.  Both call `checked_vectors` now, with a test for each that fails
without it.  A
numbered window is measured against the *harness's* budget from both ends — the
shortening that fills it and `dump_read`, which fills one back — so the
measurement is `tokens.estimate_lines_tokens` rather than two expressions that
happen to agree today.  And the two `_yaml` writers were not only the same three
lines: the atomic swap around them was the same five, differing in which one
cleaned up its temporary file, so `libs/host/statefile.py` keeps the mechanics
(one `mkstemp`, one `fsync`, one `os.replace`, one unlink on failure) and both
plugins keep their own *policy* — a corrupt login is a cache miss that is logged
and ignored, a corrupt ledger is a refusal to start from nothing — by catching
what `write` raises.

**Read, and left as they are.**  This is the half that keeps the next pass from
re-finding them, so each says why:

- **`where_clause` and `filter_clause`** (`libs/store/index.py`) differ by one
  word, `AND` against `WHERE`, and both are used at six and eight sites.  A
  `_clauses(clauses, lead)` behind them would be a third name for two, and the
  load-bearing half of both is the empty-string guard, which is already shared.
- **The search legs** (`_keyword_hits`, `_nearest`, `_meaning_of`,
  `_matched_in_line`, `_semantic_lists`) are per store, and `index.py`'s module
  docstring already says why: they differ in their join, their ranking and the
  alias they answer under, and "a helper that took a callback for each of them
  would hide the one part the tests pin".
- **`_place` and `_place_vectors`** are four lines over the shared
  `place_vectors`, whose only difference is the key's type.  Merging them means
  passing the per-row writer in, which is more machinery than the loop it
  removes.
- **`_ensure_schema` in `db.py` and `catalogue.py`** are not copies: the
  catalogue *drops and rebuilds* a file that is not this build's, and the turn
  store only reports one.  Same three-word shape, opposite behaviour.
- **`runtime._write_json_atomic` beside `statefile.write`** is JSON rather than
  YAML, does not fsync, and its failure is a registered degrade that logs and
  carries on where `statefile.write` raises for its caller to catch.  Which of
  those one helper should do is a design decision, not a duplication.
- **The file tunnel's `_find_binary` and `_missing_binary_error`** are one line
  each per provider, and the message *is* the whole of what the second one adds —
  "ssh client not found … on Linux/macOS install openssh-client" is not a
  sentence to parameterise.
- **The five management tools `mcp_tools` and `restapi` share** are one line
  each, already routed through `family.list_entries`/`remove_entry`/
  `switch_entry`/`list_entry_tools`; what is left of each is its decorator name
  and its docstring, which *are* the model-facing schema.
- **The ten plugin `main`s** are `serve_plugin`/`serve_backend` calls differing by
  `build=`, `note=` and `api=`, which is the table `PLUGINS` above.
- **The four `channel_for`s** are one expression each over a different prefix
  (`async-tool`, `schedule`, `agent-task`, `a2a`/`subagent`), and those prefixes
  are the vocabulary `tui/widgets.channel_prefix` branches on.  What was a
  duplication — `agent_task`'s own `task_id` and two hand-built `f"{CHANNEL}:…"`
  strings beside it — is one spelling now.
- **`media`'s three `NotImplementedError` stubs** (`generate_video`,
  `text_to_speech`, `transcribe_audio`) differ only in a word, and the *method
  name* is the capability contract: folding them behind one parameterised method
  would delete the interface.
- **The mirror pairs** in `tui/widgets.py` (scroll and page, up and down) and
  `schedule.expressions.next_run`/`previous_run` are each two calls with the same
  shape and the opposite direction — `get_next`/`get_prev` is croniter's own API,
  and the "strictly after"/"strictly before" rule each docstring carries is the
  reason they exist.

### 1.2 The plugin that is not a family

**`slife2-wechat` is the first plugin here that holds no section and declares no
rows.**  Every other one is a *family*: it owns a config section, and what it
does with it is hold connections (`mcp:`, `rest-api:`) or build rows from
documents (`skills:`, `cli:`) and declare them to the hub.  What this one holds
is a credential and a conversation — the first table's
"a credential, a provider SDK, somebody else's connection" row, with nothing
else in it — which is also why the hub never asks it for sources: `_declares` is
read off the listing (§8.2), `list_sources` is not in it, and a plugin with
nothing to declare is a plugin with nothing to be asked.  Its four tools are the
model's, under their own names, like any other plugin's.

**A login is per agent, and that is the port's whole difference from v1.**  v1
ran one wechat child per agent, so its module globals *were* that agent's: one
`_client`, one queue, one dedup window, because there was exactly one agent in
the process.  These servers are shared (§4), so the same four things live in a
`Session` keyed by the **calling agent** — part of the client id every call carries
(§3), and one part of it that a home made longer (§4) — and a second agent that
scans its own code gets its own file, its own poll loop and its own
conversation; a second home's login is a second session for the same reason, and
its cursor is written to that home's `agents/<agent>/wechat.yaml`.  This is the contract
`slife2.libs.plugin.protocol` states, at the first plugin where it costs something: a
process that cannot ask an operator which agent it is serving has to be told, per
call, and a call that does not say is refused rather than guessed.

**The plugin delivers its own turns, and it delivers them to the window.**  v1's
harness polled the plugin for messages and injected them into the inbox, so the
plugin's job ended at "here is what arrived" — and that seam was a tool named
`__wechat_drain_incoming`.  There is no harness here, and there is something
better than a seam: a message is handed to the terminal showing this
conversation, which puts it in the same queue the person's own typing goes into
(`slife2.plugins.tui.server`).  So a message sent from a phone is *visible in the
terminal*, wears `Wechat> ` on screen, and is recorded as the turn it is — one
inbox, one display, and the one path for a turn whether it was typed or arrived.

**That is why the drain tool has no port here either: it *was* the seam.**  What
replaced it is not a smaller seam but the removal of the question — the window is
where a message becomes a turn, and the window is already the thing that shows
turns.

**What the window is handed over *on* is a call it holds open, and that is the
second thing `slife2-tui` is.**  The window attaches to the plugin when it
mounts; the plugin keeps a session per agent; the push down that open call is
what arrives, and the window's handler puts the words in its queue — so what
crosses is a message, not a notification the window has to go and collect.  It
is deliberately *not* an inbox the window polls, for the reason the paragraph
above gives, and not a port per terminal either: one plugin, one address, and
the agent name is what picks the window.  Two windows used to be impossible,
because the address was one per machine and a call could not say which terminal
it meant.

Two things follow, and both are the shape rather than an implementation detail:

* **A message is handed over and let go.**  `submit` returns once the words are
  written down the stream that window is holding, *not* when the turn ends, and
  that is what makes handing over safe to retry: a caller whose call came back
  knows the message is somebody else's problem, so a connection that drops
  afterwards cannot run the same words twice.  What the terminal does with them is the ordinary thing it
  does with anything — one turn at a time, in arrival order (§3) — and the plugin
  does not wait for any of it.  The bound on how many may be in flight is the
  plugin's, because a person flooding a conversation is a fact the plugin sees
  first.  The one thing that is *not* done is the window acknowledging the
  message once it has it: that reads as the stronger promise and is the weaker
  one, since an acknowledgement landing after a `submit` gave up leaves a message
  that still runs (§2).
* **The reply is still the model's to send.**  Nothing here auto-dispatches an
  answer, which is v1's rule unchanged: a turn that says nothing sends nothing.
  What the plugin does do on its own is the typing indicator — up when a message
  is handed over, down when the reply goes out — because that is a courtesy owed
  to somebody holding a phone, not a decision about the conversation.

**And the window is told about the link itself, in the one vocabulary a
channel's arrival is spelled in.**  A login coming up, and a login going away,
are the same news as a peer arriving on the mesh (§1.8) — something this
conversation can now reach, and then cannot — so they are drawn the same way:
`⚡ Wechat online`, `✗ Wechat offline`, in the same bracket as `[restored 3
turns]`, as a note rather than a turn.  **Both channels' lines come from one
place** (`slife2.tui.notes`), so the shared shape is code rather than a
resemblance a later edit can break; what each channel supplies is the name
(`jill`, `Wechat`).

What is said is the *transition* and nothing else, and the two halves of that
are both deliberate.  The announcement rides the poll loop — a token the far end
refused ends the loop, and its last act is to say so, which is how a link that
dies on its own reaches a window nobody is asking anything of — and `logout`
says it where somebody asked for it, since it cancels that loop where it stands.
A link that was **never** up produces no line at all: a cold absence is not an
event, which is the rule §1.8 states about a cold retained card.  **And neither
does a login picked up at start-up**: `_resume` adopts a saved session with the
state already marked as known, so a link that was up before the window opened is
not the window's news — what a window shows is what happened while somebody was
watching, which is why a scan landing does say so and a process starting does
not.

**One WeChat account holds one live login, and iLink decides which.**  Scanning
a code for a second agent — or on a second machine — with the same phone does not
fail and does not warn: it *retires* the login that was there first, which is
then answered `{"errcode": -14, "errmsg": "session timeout"}` to its polls *and
to its sends*, instantly, for as long as that token is offered.  That is a
rejection wearing a `200` — the thing §1.2's reader exists for — in the *other*
spelling of it: `errcode` / `errmsg` where a revoked token answers `errno` /
`error`.  A reader that knew one pair and not the other left the retired login
polling a bridge that had stopped serving it, `online` in its window, reporting
answers it had sent nowhere; both pairs are read now
(`slife2.ilink.ENVELOPE_ERROR_KEYS`), so that loop ends, the window is told, and
the model is given the bridge's own words for what happened rather than being
sent to scan a code for no stated reason.

**And a line is said once, where a window is.**  It is the window that runs an
agent — a terminal or `--headless`, and both attach — so there is no window only
in the seconds around that agent's own start, and a line the gap swallows is not
offered again.  Holding it *owed* and redialling for it is machinery for a case
this deployment does not have: the line is for somebody watching, and in that gap
nobody was.

And a window is
not where the *reasons* are told apart — "no login saved", "waiting for a scan",
"connected but not answering" are three different pieces of advice — so those
stay in `wechat_check_status`, which is where a model asks.

**The prompt carries its own addressing, because the turn's record cannot.**
`channel` says `wechat` and nothing more, and answering needs the peer's id and
the conversation's `context_token` — so the message is prefixed
`[Wechat:{"peer_wechat_id": …}]`, with the JSON keys *named after the arguments
of `wechat_send_message`*, so that replying is copying two values rather than
translating between two vocabularies.  v1's marker, keys and case, for the same
reason its session file is: a habit and a file format both survive a port.

**Its reader is the model, and a screen has the channel instead.**  A window
shows `Wechat> ` and takes the marker back off, because the two ids inside it are
what a *reply* needs — nothing a person reading over the model's shoulder can act
on. That is one unwrapping asked where the line is drawn, not a rule each path
remembers (§5.1).

**And of the two markers a conversation carries, this is the one that is
stored.**  A turn footnote is derived every time a message list is built and is
never written down (§5); this marker is content — it goes into the turn's row
along with the message and comes back out of it verbatim.  The two are the same
shape, `[NAME:{…}]`, so **the case is what tells them apart** — and that
correspondence is a convention and nothing more: no code reads the case, and the
two markers are never compared to each other anywhere.  What the capitals say is
where the marker lives: this build's own annotation is `[TURN:{…}]`, which is not
part of the conversation, and a name that reads as a word — `[Wechat:{…}]` — is a
marker that is in what the model remembers.  Since nothing parses either, nothing
has to be migrated, and the spelling is only ever read.

**And it has no config section at all.**  Every other plugin here owns one, and
this one owns none — not even a switch.  v1's `wechat: {enabled: false}`
declined to *spawn* the plugin, and there is no gate to hang that on now that the
launcher starts every plugin and shares it; a switch that only refused the *work*
would have been a key whose whole content was "do not do what this process
exists to do".  So the rule is the one the data already states: **a machine that
should not talk to WeChat is a machine where nobody scans a code.**  There is no
saved login to pick up until somebody does, and the `wechat.yaml` inside an
agent's own directory is the whole of what this process resumes.  What is left in
the config is an address
under `servers:` and nothing else — `slife2-toolhub` is the only other plugin
that reads no section of its own.

### 1.3 The agent task, and what a second loop costs

**`slife2-agent-task` is a second agent server, and it is a plugin for the reason
`slife2-agent` is one: what it holds is loops.**  It owns no section of entries
and nobody's connection, so §1.1's first row does not reach it — and the two
facts that do are these.  A plugin's own tools are the only ones the model may
call (§8.1's gate, `toolhub.Upstream._offered`), so a tool the model is meant to
reach has to have a server to be served from.  And what a task *is* — a loop in
flight, holding a message list nobody else addresses — is the same kind of state
`slife2-agent` keeps, for the same reason: no file is the answer to it, and no
other process can reach it.

**It does one thing, and the whole surface is four tools.**
`agent_task_start(name, description)` begins one loop of the *calling agent's
own* and answers at once with its id; `agent_task_status(name)`,
`agent_task_list()` and `agent_task_cancel(name)` are the rest.  The model
reaches them the way it reaches any plugin's tools, and the identity rides on the
call (`audience.client_of`), so a task runs as the agent that asked — the same
system prompt, the same model, the same tool set — and its answer comes back to
that agent's conversation.  **A task never receives anything**: no second message
into it, no queue, no cut-in, no turn after the first.

**It is not a subagent, and that is a fact about the code rather than a word.**
§9's Subagents entry describes the other thing: `send_message` already takes a
`subagent` and already runs a separate conversation under it, with its own
history, inbox and lock.  Reading a task off that mechanism costs four things:

* **The context would be the wrong one, and it is the whole point of the
  feature.**  A task has to run on *this conversation's* context — the description
  is what the caller chose to write down, and everything else the agent already
  knows is in the log the discriminator reads from.  A `subagent` key is a
  different file (`db.database_path`, `db.py:190`), so a task read off that
  mechanism would rebuild against a log nothing has ever written to: the
  discriminator would be paid the most expensive call a turn makes (§5.1,
  `rebuild_into` at `server.py:832`) to decide that an empty list stays empty, and
  the task would know only what its description said.
* **A task becomes an addressable conversation.**  A `(agent, subagent)` key is
  nameable by anybody, so any caller could `send_message` into a running task —
  and the running turn would fold the message in, because the agent server always
  passes `auto_invoke`.  A task is not a conversation, and nothing should be able
  to name one.
* **Not recording would come from a rule about something else.**  A worker does
  not record because `records` is `not self.subagent` — a flag about subagents.  A
  task does not record because the *answer* is what the conversation gains (below),
  and that is a decision about tasks: inheriting it from a flag about a different
  thing is how the two come apart the first time either one changes.
* **The agent server would grow a second kind of thing it owns.**  §3's whole
  architecture is *a conversation is an object, named by `(agent, subagent)`.*  A
  task has no inbox, no transcript and no turn of its own in the log; it enters
  that table only as an exception — an entry that is addressable, that any caller
  may `send_message` into, and that nothing ever reads back.

**So the task's turn path is the plugin's own, and it touches nothing the agent
server owns.**  It does not call `send_message`, does not build a `Loop`, and
does not run `run_turn_into` with anything switched off.  It assembles a loop:

```
system prompt   render(config.agent.system_prompt, agent_name=agent)
opening         render("agent_task.j2", name=…, description=…)
restore         the agent's own context, under the agent's own key
rebuild         once — keep ∪ recall, this conversation's turns included
tools           remote_tools(hub, client_id)          the hub's list, unfiltered
loop            AgentLoop(backend, tools, max_steps=…, refresh=…)
answer          loop.run_turn(messages, opening)      `opening` beside the list
trim            _func_tool_unload, at the turn boundary the agent server uses
```

**A task runs on the agent's own context, and gets it the way the agent does.**
Its key is `(agent, "")` — the agent's own conversation — so `restore` puts back
what this conversation had, and `rebuild` asks the same discriminator the agent
server asks, once, before the turn.  That is what "it runs on the current agent's
context" means when it is written down, and it is the difference between
delegating and asking a stranger: the description is what the caller chose to
write down, and everything else the agent already knows is in the log the
discriminator reads from.

**And the context is a snapshot with an edge, which is the one thing about it the
model cannot see.**  What `restore` reads is the *stored* list, and a turn is
written when it ends — so the turn that is calling `agent_task_start` is not in
what the task starts from.  That is not a detail of this plugin: it is the same
fact as "nothing is recorded", seen from the other side, and it is the reason
`agent_task_start`'s description says to restate the last exchange and let the
task read everything older for itself.  A description that said the task had
"none of this conversation's history" was the opposite of true, and a model that
believed it wrote descriptions that re-explained what the task could already see; the
truth is more useful than the caveat was.

**Once is exactly right, and zero would be wrong.**  A task runs a single turn,
so the one rebuild the agent server makes before a turn is the one a task makes
— and a turn that ran on whatever happened to be in the list would be the one
turn in this system whose context nobody decided (§5.1).  The call is made
outside the agent's lock, which is safe for the reason §5.1 gives on the other
side of it: **a decision that asks for what is in hand rebuilds nothing and
writes nothing**, and that is the common answer.  The turn's writer is the agent
server; this one speaks only when it has something to say.

**And nothing is recorded.**  A task's result reaches the conversation as a
message, and *that* message is what the agent server records — so writing the
task's own round trip as well would put the same answer in the transcript twice,
under a description that is a rendered template rather than anything the person said.
A task reads the agent's context and adds nothing to it, and what the
conversation gains is the answer, which is the thing it asked for.  `Loop.records`
is the agent server's switch for the opposite case; there is no switch here
because there is nothing to switch.

**The trim is not optional, and it is not for this turn.**  The tools a model has
loaded go out with every request, so a turn that loaded several past
`tool_load.threshold` has left the list longer than the config allows — and the
loaded set lives in the *catalogue*, which is one list for the whole system.  So
a task that did not trim would leave every other conversation's request longer.
It runs where the agent server runs it, at the turn boundary and never mid-turn
(dropping the excess there would take away a tool the model had just loaded and
was about to use), and what went is written into the turn as the harness pair
`_func_tool_unload` called with no names — the same shape and the same rule, so
that a reader of the record reads that the list shrank rather than reaching for a
tool it still believes it has.

**The opening message is rendered, and that is the third prompt-with-holes.**
`slife2/templates/agent_task.j2` joins `system.j2` and `recall.j2` under the
mechanism `slife2.libs.turn.prompt` already describes for both, for the same reason: it is
a prompt with holes in it, so it is a template rather than an f-string.  It is
asked for by bare name and is therefore **shipped rather than configured** —
which is what `recall.j2` does, and the opposite of what `agent:`'s
`system_prompt` does.  The difference is whose words they are: the agent's prompt
is the operator's, because it is *who the agent is*, and a task gets it because a
task is that agent; this one is machinery, the way this system tells a loop that
nobody is listening.  An operator with an opinion about what a task is told has
the agent's own prompt to say it in, and it already reaches tasks.

Three things the file has to carry, and only the first is obvious:

* **Nobody can answer a question.**  A turn that ends by asking which report was
  wanted is a task that spent a whole loop producing nothing.
* **It is the agent, on the agent's own context.**  Said out loud, because the
  alternative reading is a stranger being briefed: a task that behaved like one
  would ask for what the conversation already holds, and would not use what it
  already knows.
* **Its last message is the result, and it goes back on its own.**  So it is the
  answer rather than a plan, an offer to begin, or a note about what it is about
  to do — nobody is going to ask for it, and nothing else will be sent.

Everything else a task needs is in the agent's own prompt, and what is in this
file and *not* there is exactly the difference between being an agent and being a
task.  That is why it is a file, and why it is short.

**The step machine is a library, and this is the sentence §1.1 was written for.**
`slife2.libs.turn.loop` is a pure function over a message list: it owns no conversation,
knows no provider, and does not know whether anyone is watching (§7).  It is not
"the main agent's loop" — it is *the* loop, and the agent server is one of its
callers.  And it is called here with nothing that has to be turned *off*:
`run_turn`'s `observer` defaults to `NULL_OBSERVER` and its `auto` to `None`,
which are exactly a task's two answers — nobody is watching, and there is no
inbox to cut in from.  **A library that needs a row of switches set to off before
it fits has to be reused; one whose defaults are already the caller's needs only
to be called**, and the distance between those two is the whole of "clean" here.

**The boundary that keeps it that way**: a behaviour the task's turn needs and
`AgentLoop` does not have is added to `AgentLoop` behind a parameter that
defaults off, or the task does not get it.  It is never added to a copy.  Phase A
strictly before Phase B, every tool failure becoming a message, and `max_steps`
ending a turn normally are three decisions §7 names as load-bearing, and a second
implementation of them is a second answer waiting to drift.

**A task carries the agent's identity, and no identity of its own.**  Its tool
calls and its model calls go out as `(agent, "")` — the agent itself, from
`audience.client_meta`.  Nothing is invented to hold a task's name, and that is a
decision rather than a shortcut: a key of its own would be a second slot in a
namespace that means "a conversation", and the name a task has lives where the
thing that owns it lives.  Two consequences follow, and both are wanted:

* **`turn_read` and `turn_list` answer from the agent's own log.**  That is what
  "it runs on the agent's context" means where it is observable: a task is the
  agent working, so the agent's memory is what it reads.
* **A task may start a task, because the tool set is not filtered.**  The
  catalogue is one list for the whole system (§8) — what a caller gets is what is
  loaded — and "this caller's list, minus one name" would be the first
  per-caller tool set in this design.  So the registry is **flat**: one name per
  task, no parents, no depth, and `agent-task: max_running` is the bound instead.
  What that does not bound is how much work happens over *time*; §9 says so.

**The four tools answer in one vocabulary**, which is why stopping a task needs
no second set of outcomes:

```
agent_task_start(name, description)  -> {"task_id", "status": "running"}
agent_task_status(name)              -> {"task_id", "status", "started_at", …}
agent_task_list()                    -> {"tasks": [{task_id, status, started_at, …}]}
agent_task_cancel(name)              -> {"task_id", "status"}   after the call
```

`status` is one of `running`, `finished`, `failed`, `cancelled` and `unknown`.
`agent_task_cancel` answers with that same field — `cancelled` when this call
stopped it, `finished` or `failed` when it was already over — and that is the
difference between *I stopped it* and *I arrived late*, which is worth one shared
vocabulary rather than two.  `finished` and `failed` are apart because a model
asking about a task needs both halves: that it is over, and how it went.  The
failure's own words arrive by message like any other answer, so the field is a
state and never a result.

**And there is no tool that reads an answer back**, which is a decision and not a
gap left open.  The answer always comes *to* the conversation: that is what the
push is for, and a second way to fetch one thing is the way that ends up used
twice — the same argument that keeps the result out of `agent_task_status`.  What
remains uncovered is a hand-over that landed nowhere at all, and that is the case
above: it is logged with the answer's length, and it means the terminal and the
agent server were both gone.

**And nothing keeps a second copy.**  A finished task's message list, its loop
and its tool registry are locals of the run and die with it; the answer is
dropped once it has been handed over, and the finished coroutine's handle with
it — a finished `asyncio.Task` holds its own result, so letting go of the
reference is what releases it.  What outlives a task is a name, a state, two
timestamps and the description, which is what `agent_task_list` answers from and
what the registry's cap bounds.

**The result is a message, and asking is a different question.**
`agent_task_status` does not return the answer's text and does not suppress the
message that carries it: if it did either, the same result would enter the
conversation twice and a model could act on it twice.  So the answer travels one
way, and the poll answers *is it still running, and did it go well*.  A name that
never ran and a name the registry has forgotten both answer `unknown`, which is
honest — the registry keeps the tasks it has run, capped, and forgets from the
tail.

**Cancelling pushes nothing, and the state is the lock.**  A cancelled task's
result never arrives: there is nothing to report about work that was called off,
which is §3's rule for a cancelled turn one level down (`Pending.result` stays
`None`).  Its finaliser awaits nothing — §3 measured that a plain `await` in a
cancelled scope is cancelled again at its next checkpoint — so the registry entry
is popped synchronously and the delivery is simply not reached.  The two
happenings that decide an outcome cannot interleave: one process, one event loop,
and no `await` between reading the status and writing it.  A cancel arriving
after the loop returned therefore answers `finished` and the delivery completes —
**a late cancel cannot take a result away.**

**And a task needs no cancellation repair.**  A cancelled turn in the agent
server is truncated back to the user's message, because an assistant message with
half its tool calls answered is a 400 from every provider and *that* conversation
has to survive.  A task's message list is the runner's local and dies with it, so
there is no next request to poison.  That is the fifth thing the task's path does
not have, and this one is not saved by omission — it is not needed.  What is not
repaired either is a tool that was running when the cancel landed: cancelling
mid-call is not a rollback, in a task exactly as in a turn.

**The delivery is §1.2's path, because that is what the path is for.**
`slife2-tui`'s `submit(agent, text, channel)` is how something outside a
conversation becomes a turn in it — WeChat is its first client
(`wechat._to_window`) — and a finished task is its second.  The words go into the
same queue a person's own typing goes into, so a parent that is mid-turn takes
them at a step boundary as the `_check_new_input` pair it already knows how to
fold in, and a parent that is idle runs them as its next turn, on screen.  That
is the whole of "pushed back into the agent's inbox": the inbox already decides
what an arriving message is, and nothing here has to.

**The channel is `agent-task:<name>`, and the answer wears a marker.**  A channel
is the key a plugin runs under — `wechat`, `tui`, and this one (§1.2) — and a
task's answer also carries the task's `name`, because the window builds the
bubble from the channel and nothing else: `AsyncTask(research)> ` is a fact the
channel has to carry rather than one the window could guess.  The *recorded*
channel carries it too, which is what the `[TURN:{…}]` footnote hands the model
(§5.1) — so a result says which task it came from outside the marker as well as
inside it.

**The marker is `[AgentTaskResult:{…}]`, and a screen takes it off like WeChat's.**  It
carries five fields — `task_id`, `name`, `status`, `started_at`, `finished_at` —
and its reader is the model: the id is what its *next* call needs, since asking
about a task or stopping one names it, and this is where the name is learned.
Those keys are the ones every tool's reply and every row of `agent_task_list`
uses, so a model never translates between two spellings of one fact — the rule
`slife2-wechat` states for its own marker, applied to a marker whose keys are
named after the fields the tools answer with.  A screen has the channel already,
so it removes the marker before drawing, exactly as it does WeChat's:
`slife2.libs.harness.markers.strip` takes the prefixes a caller knows about and removes
whichever it finds, which is one function for two markers and a third channel
away from being a line in `slife2.tui.widgets.MARKERS`.  The *names* stay with
their writers, because the module that writes a marker is the one that knows what
it looks like, and a copy drifts into printing the raw marker at somebody.

**And a delivery that cannot be made is a failure.**  There used to be a second
road for it — `send_message` on the agent server, so that "start a task, close
the window, walk away" left the answer waiting in the transcript — and it was
wrong in the way every fallback for a normal case is: it made "the window is
gone" a state to carry on in, and the work went ahead in a conversation nobody
was running.  An agent's window is what runs it (a terminal or `--headless`, and
both attach), so a message with no window has no conversation to become a turn
in, and now says so (`slife2.libs.harness.handover.DeliveryFailed`).

What survives that is the *classification*, and it is about retrying rather than
about roads.  `submit` refuses when no window is showing that agent, and it looks
the session up before it posts anything (`slife2.plugins.tui.server`) — so **that
refusal is the one answer that proves nothing was written**.  Anything else means
the call did not come back, and the words may be down the stream already:
repeating them is how one answer arrives *twice*, which is what treating "it
raised" as "it did not happen" produced when it was tried.  So the hand-over
answers in three values rather than a boolean — written, refused, unknown — tries
a refusal again while the window may still be attaching, and raises on the
unknown one without repeating it.  A caller that gave up cannot know, so it does
not repeat; what it does instead is its own business, and §1.2 states the rule
from the caller's side: the run ends `failed`, the answer is undelivered, and
neither pretends the delivery happened.

**Nothing here auto-dispatches an answer**, which is §1.2's rule unchanged.  A
task's result is something the agent acts on; whether that becomes a message to
somebody is that turn's business, not this plugin's.

**`agent-task:` owns one number, and `agent:` gains a second reader.**  The
section holds `max_running`, because a task is the one loop source that can grow
without bound and §9 has owed a cap on loops since before this section existed.
Everything else a task's turn is made of — `system_prompt`, `max_steps` — comes
out of `agent:`, which gives that section two readers and §8's rule (*the section
a key lives in is the plugin that reads it*) an exception.  It is the right one:
those keys are not the agent *server's*, they are **the agent's**, and a task is
the agent working.  A prompt or a step count that meant something different for a
task would be two agents wearing one name.

**The name says what the thing is, and this system already had the other word.**
`media/adapters.py` speaks of a `task_id` and polls a provider's `task_status`
(`adapters.py:433`): that is somebody else's asynchronous job, waited for inside
a turn.  An **agent task** is the opposite — a loop of ours that nobody waits
for — and `agent_task_*` names it as what it is: a task an agent runs, which
calls a model.  The word in the name is the word the model needs in order to know
it is asking for work rather than filing one.

**What it costs, said plainly.**  A second place where a turn is assembled, which
the boundary rule above is the answer to rather than a denial of.  A task's model
calls are indistinguishable from the agent's at the far end of the hop, because
the identity they carry is the agent's — the price of not inventing a key, paid
in the one log that is not this system's, while the task plugin's own log is
where a task is visible.  And nothing bounds how much agent-task work happens
over time; §9 holds that one open, because the two ways to close it are a rate
and a depth, and `agent_task_list` with `agent_task_cancel` is what makes it
visible and stoppable in the meantime.

### 1.4 The cabinet, and what a kind of document costs

**`slife2-cabinet` is the first plugin here that owns a store of its own and
nothing else** — not a family (it declares no catalogue rows), not a connection
(it holds nobody's server), not a model.  v1's `memfiles`, ported: a **note**, a
**diary**, a **saved file** and a **report**, which are kinds of document and,
deliberately, **one corpus** — and later a fifth, the **dump** (§5.2), which is
where §1.4's own arithmetic was collected for the first time: a kind costs a
constant, a `SPECS` row, the schema's `CHECK` and two docstrings, and the reason
to add one is that the alternative is a second store with a second index.

**One table, because four kinds are one thing to search.**  v1 had four tables, a
`cabinet_docs` view that unioned them and twelve triggers keeping a shared FTS
table in step.  What all of that was *for* is the rule §8.4 states for the
fusion: a rank only means something inside the corpus that produced it, so four
indexes would be four corpora and a query spanning them would answer with an
order nothing had measured.  Here the corpus is literal — one `cabinet_doc` table
with a `kind` column — and two things fall out of that which v1 had to build:
the **rowid is the corpus id**, so the two legs fuse on it exactly as the turns
and the catalogue do (`fuse_ranked`), where v1 synthesised a global id from a
per-kind offset; and the keyword text is written **in the same transaction as the
row that produced it**, which is why this store, like both of slife2's others,
has no trigger at all.  What is per-kind is a row in `SPECS`: which column is the
key, which is the time axis a list orders by, whether a second write appends, and
where the markdown mirror goes.

**A document is written twice, and the mirror is the half a person reads.**  The
row is what a search finds; `notes/会议记录.md`, `diary/2026-10-11.md`,
`reports/weekly.md` are the same document as a file somebody can open, edit or
delete without asking a process.  The store owns both writes because they are one
operation, and the mirror's path is *stored* rather than derived, because two
subjects slugify to one stem and a derived path would let the second overwrite
the first's markdown while both rows stayed correct.  A saved file is the one
kind with no mirror: its bytes are the document, and a text copy of a PDF is a
copy of something that is not text.

**And a subject is readable as itself only when it is already casefolded**, which
is the second half of that rule and the one a *path* forced.  Two keys that
slugify alike were the first case; `Readme` and `readme` are the second, and they
satisfy every string-shaped test of "the slug is the key" while landing on one
file on Windows and macOS.  So the readable half is reserved for keys no
case-insensitive lookup can conflate, and everything else carries a short digest
of the key beside its slug — `notes/Python~6e3604888c.md`.  *Gives up:* the plain
filename for a subject with a capital in it, which is most of them; a person still
reads the name, and a date still reads as a date, because a key `slug` does not
change is a key that never needs the digest.  The alternative — keeping the plain
name for whichever key claimed it first — needs the store to answer, and the
mirror's path is deliberately a **function of the key**: that is what makes the
row and the file agree without a lookup, and what makes "these two can never be
one file" something to read rather than something to test.

**A file's bytes are never in the index, and its identity is.**  What is indexed
and embedded for a file is what it *is* — its name, the paths that place it, and
the summary once a model writes one — never the bytes.  That composition is
load-bearing rather than tidy: measured while porting, a file embedded from its
summary alone left every summary-less file with **no vector at all**, which
`sync` then reported forever as a document it could never catch up.  Which is
also the one asymmetry of `cabinet_summarize`: writing a summary **re-embeds** a
file, because the stored vector was built from the text without it, and does not
re-embed a note, whose vector is its body and unchanged by a description.

**And there is no drainer.**  v1's cabinet shared `memdb`'s `SemanticManager`, a
background drainer that walked unembedded documents and caught them up on its own
schedule.  This has none, and the difference is where the work went: **nothing is
written until the embedding is in hand**, so a write that cannot embed stores
nothing — the same contract a turn's save keeps, and what lets `file_save` take
back the bytes it copied.  What is left of the drainer is one pass at open
(`sync`): it does not remember which rows are missing (the index is derived, so
"missing" is a query) and it does not run in the background, and a document that
*is* missing is reported rather than quietly caught up later, because a cabinet
whose meaning leg is silently incomplete is a search that lies.

**What is deliberately not ported**, each as a nameable change rather than a
half-built one: **the URL half of `file_save`** (v1 downloaded public `http(s)`
URLs behind an SSRF guard of its own, in `slife/net.py` — a library with a threat
model, a config switch and its own tests, and one whose justification is a
measured property of *this* machine's resolver, so it belongs in a change that
argues for it); **the `grep` leg** (§9 defers v1's third search mode for the
whole system, and a cabinet of files is exactly where it is wanted, so the
cabinet inherits the deferral rather than growing a second regex engine);
**the schedule half** (v1's plugin also owned the scheduled-task tables and
sixteen tools over them; nothing here schedules anything, so a report is keyed by
its title instead of by a task and is otherwise the log it always was); and
**`USER.md`** (v1 served the profile *and* rendered it into the system prompt,
and wiring a file into a prompt is a change to the prompt).

### 1.5 A second provider table, and what a *kind* is on

**`slife2-media` is the embeddings server's shape applied to four capabilities
that are not embeddings** — an image, a video, speech and a transcript — and the
differences are the port's whole content.

**The kind is declared per model, not per provider.**  `providers:` and
`embeddings:` are tables of *endpoints*, and an endpoint is one thing: a wire
protocol, an embedding model.  A media vendor is several things at once —
Bailian serves all four kinds — so a `media:` entry declares what each of its
models is *for* (`kind: image`), and the four-step search in `MediaSettings.resolve`
(named reference, bare name, `defaults:`, first of the kind) is what turns "make
me a picture" into an endpoint, a key and a model id.  The rule the table keeps
is that a *capability* is what a caller knows it wants and which model provides
it is the config's business.

**Two wires, one contract, and a registry of two.**  `api:` names an adapter
rather than a brand — that is the extension point, and it exists because the
vendor gap is real rather than hypothetical: an OpenAI-compatible images endpoint
has no video, speech or transcription service behind it, so the second adapter
implements one capability and refuses the other three *by name*.  A refusal that
names the adapter is a config to revisit; a 404 from a vendor is a mystery.

**Artifacts go under the agent's own directory**, and that is where this port
departs from v1.  v1 wrote them to the process's working directory, which was the main
process's — a person's shell.  A daemon here is spawned with its working
directory set to the machine's runtime directory (`~/.slife2/runtime`, §4), which
this design documents as *reconstructible — delete it and the next start rebuilds
whatever it needs*, so
v1's rule would put every generated image in the one directory guaranteed to lose
it.  They land in `agents/<agent>/media/`, and
what is kept from v1 is the distinction underneath: a generated file is a **work
product**, not a cabinet document — nothing registers it, the answer is a path,
and saving it somewhere durable is a separate call by whoever wants that.  And
`folder=` is *contained* inside that root, because it is a model-written string:
`folder="../../.."` would be a model choosing where on the operator's machine to
write a file, which is the same line `cabinet_store.resolve` draws.

**A failure is a `ToolError` rather than an `"Error: …"` string.**  v1 returned
the latter, which was its convention for a tool failure; here the convention is
the exception, for the reason `slife2.plugins.skills.server` states — a refusal returned
as text reaches the transcript as a *success*.  The three failures are told
apart because a person does three different things about them: an adapter that
cannot do this at all (a config choice), a provider that answered badly (worth a
retry), and a path that is not there (the caller's own typo).

### 1.6 The file tunnel, and the first second route

**`slife2-file-tunnel` is v1's `sharefile`, and it exists for the one thing a
local path cannot do.**  A vision model asked about `D:\pictures\cat.png`, or a
chat API that takes a URL and nothing else, cannot read this machine's disk.
`expose_local_image(path)` answers with a public HTTPS URL that can fetch it.
Everything else about the plugin follows from that sentence: to have a public
URL there has to be a **tunnel**, which is somebody else's process or somebody
else's account; to serve the bytes there has to be an **HTTP route** on the same
port the tunnel forwards to; and to hand a URL out and later withdraw it there
has to be a **registry** of what has been exposed.

**A plugin, by §1.1's table, and comfortably so** — the first row is "a
credential, a provider SDK, somebody else's connection", and this is all three
at once: an ngrok session, or an `ssh` child, or a `cloudflared` one.  What it
does *not* hold is anything a library could hold instead, which is the whole
reason that row does not reach `db`.

**It is the first plugin here that answers a second HTTP route.**  Every other
one is a single MCP endpoint at `servers.<name>.path`, and §1's claim that "a
plugin is an MCP server over Streamable HTTP" is exactly true of them.  This one
also serves `GET /share/{token}` beside `/mcp`, on the same port, because the
far end fetches *that path* through the tunnel — the URL handed to a model is
`https://<tunnel>/share/<token>`, and nothing about MCP is involved in the fetch
that follows.

Three things are worth saying about it, because each is a place the arrangement
could have been got wrong:

* **It needs nothing new from `slife2/libs/plugin/protocol.py`.**  FastMCP has had
  `custom_route` all along and `serve()` still runs the app with
  `mcp.run(transport="http", …)`, so the route is served by the same call, on
  the same port, outside the `/mcp` prefix.  Measured against the installed
  4.0.11 rather than assumed, and pinned by a test that drives the real ASGI
  app (`http_app()`) with no socket: a URL a third party cannot fetch is the
  failure this feature cannot survive.
* **The port is the config's, not a bound socket's.**  v1 called
  `bind_free_port()` and told the parent which one it got, so the tunnel could
  be pointed at it.  Here the address is already written down — `serve_plugin`
  reads it from `servers.file-tunnel` — so the tunnel is pointed at that.  What
  it costs is `--port` on this one plugin, which would move the MCP server and
  leave the tunnel forwarding to the configured port; nothing calls it that
  way, and a tunnel needs an address written down somewhere regardless.
* **The registry is one map for the whole process, and that is not the usual
  rule here.**  §1.1's contract keys a server's state by client id, and this one
  deliberately does not: the thing being addressed is `GET /share/{token}`, a
  third party fetching it says nothing about who they are, and the token *is*
  the capability.  What is kept per agent is the *owner* of each exposure, which
  is the one place the identity is available and can be made to matter —
  `unexpose_local_image` refuses to withdraw another conversation's link.  v1
  did not need the distinction because one child process served one agent; here
  it is the difference between isolation and a shared namespace.

**What it costs is that the tunnel publishes an MCP endpoint.**  The port being
forwarded is the plugin's, so while a tunnel is up the public internet reaches
`/mcp` as well as `/share/{token}` — and no server here has an authorization
story, because every one of them is bound to loopback and served to its own
operator.  The surface is therefore wider than the feature: a caller who knows
the hostname can call `expose_local_image` itself and read any file this process
can read.  What stands between that and a stranger is the hostname's entropy — a
random `*.trycloudflare.com` or `*.lhr.life` name — and nothing else.  This is
v1's property unchanged, it is not fixable by making the tool stricter (the
caller is anonymous either way, and a client id is a field an attacker sets), and
closing it properly means an authorization story MCP does not carry anywhere in
this build.  So it is written down rather than papered over: **a tunnel is a
decision to make this machine reachable**, and that is the whole of what it does.

**Its config is a section, not a file.**  v1 kept the provider table in a
standalone `sharefile.yaml`, which this project has no room for: there is one
config file and one writer for it (`slife2/libs/host/configfile.py`), and every other
plugin that owns entries owns a section.  So the provider table is `file-tunnel:`
in `slife2.yaml` — a table like `providers:` or `embeddings:`, with one thing
that makes it unlike both: **its entries are options rather than a schema.**
What a provider needs is a binary name, a host, a protocol — vendor words that
belong to the tunnel and not to this project — so they are kept as the mapping
they were written as and read by the provider that understands them.  A typed
field per option would put three vendors' vocabulary in `slife2.libs.host.config` and
change it every time a tunnel learns a flag.

**And that section is the one place this project warns instead of refusing.**
Every other malformed section is a `ConfigError` at load, because a mistake in a
file a person edits is a mistake to be told about.  A `file-tunnel:` naming a
provider this build does not have is the exception, and it is v1's: **a tunnel
is a subordinate dependency**, and a name this build cannot construct — a typo,
a provider dropped since the config was written, a file copied from another
machine — must not be able to keep the plugin from loading.  It degrades to
ngrok with a warning, `tunnel_status` reports the provider actually in use, and
the asymmetry is the point rather than an oversight.  A malformed *shape* is
still refused, like every other section: the two are different mistakes, and
only one of them is about a vendor this build lacks.

**v1's state machine is kept whole, because the failures it names are not
guesses.**  A tunnel takes seconds to come up and can die at any moment, so
`status()` carries `active` (a URL is live), `starting` (an attempt is in
flight), `failed` (terminal, with the reason) or `idle` (no attempt made) — the
four answers a caller needs to decide whether to wait, refuse or retry.  Two of
the behaviours in there cost real time to learn and are worth keeping visible:

* **A published URL is not a working URL.**  A transport that has lost the edge
  keeps its URL and answers every request to it with HTTP 530, so `is_reachable`
  is asked separately from `is_active`, and `expose_local_image` *refuses*
  rather than handing out a link that will fail for whoever fetches it.  The
  cost of getting this wrong is paid by a model that cannot fetch the file and
  cannot say why.
* **A wrong "dead" costs a hostname.**  The health monitor answers a false
  negative by tearing the tunnel down and minting a new one, which kills every
  link already handed to a person or a model.  So a transport that is merely
  unreachable is given a heal window before it is respawned, an unanswerable
  liveness probe is not an outage, and ngrok's listener list answers "dead" only
  when it is non-empty and demonstrably missing our URL.

**The one diagnosis that had nowhere else to live** is the fake-ip proxy: a
Clash/Mihomo TUN in fake-ip mode resolves the tunnel's edge to an address of its
own choosing, so the control connection is carried and cut by the proxy while
the log blames the transport.  `tunnel_status` reports the edge address and
whether it is a synthetic one, which is the only place that shows.  It is an
accusation and not a permission — coarsely true, because a false positive adds a
hint that is still true of the machine and nobody grants anything through it —
which is why `slife2/plugins/file_tunnel/providers.py` holds the ~60 lines v1 kept in a
shared `slife2.net`: here there is one caller, and §1.1's rule about libraries
is that the implementation goes where the capability is.

### 1.7 The scheduled task, and what a clock costs

**`slife2-schedule` is v1's 定时任务, and it is the one thing here that starts
work nobody asked for at the moment it starts.**  A **scheduled task** is a name,
a description and a cron expression, written down; a **run** is one fire of it —
also written down, with what came of it.  The model writes the definition and
reads the ledger; the clock is what fires it; and the answer reaches the
conversation the way every other out-of-band message does.

**A plugin, by §1.1's table, and by the sentence that table already justified
`agent-task` with: what it holds is timers.**  That is not a figure of speech — a
timer is state with a lifetime, and both halves of it are wrong anywhere but in a
process of its own.  In a library every importer gets its own clock, so two
instances of one agent fire the same task twice and a task written by one process
is invisible to the next; in the agent server it would be a loop the conversation
owns, which is exactly the arrangement §3 exists to argue against.  A plugin is
started once, shared by every instance, and outlives every turn — which is what a
clock has to be.

**It is not a second `agent-task`, and the difference is the one §1.3 turns on.**
That section's whole claim about a task is **"nothing is recorded"** — the answer
*is* the record, the task lives in the process's memory, and a restart is
supposed to lose it.  A scheduled task is the inverse on every one of those
points: the definition and the ledger are the feature, `pending → ran | failed |
missed | skipped` exists *because* the process goes down, and `UNIQUE(task_id,
due_at)` exists so that a fire which happened twice on the clock is one run.  A
scheduled task is the thing that survives a restart; an agent task is the thing
that is not supposed to.  Folding them together would impose durability on the
ephemeral case §1.3 deliberately kept stateless — and §1.3 is an essay on what a
second loop *costs*, so the test to apply is whether the object is genuinely
different.  It is: durable, timed and repeatable against ephemeral and one-shot.

**It does not own the loop, and it does own the dispatch — which is the whole
trick.**  v1 split the feature three ways — *timing* | *execution* | *record* —
and the execution half was a dispatch into its subagent worker pool.  slife2 has
no pool: what hangs a worker off its parent is §9's deferred half.  What it has
instead is `agent-task`, a plugin whose entire job is "a loop of the agent's own,
answering by a message" — so the fire does what §1.2 is for (`slife2-tui`'s
`submit` opens a turn of the agent's own, and a window that is not showing that
agent is a failure rather than a detour) and that turn has **one instruction**:

> call `schedule_run_now` with the due time the trigger carried.

**And the trigger says whose task it is.**  The hand-over already carries the
agent as its *address* — the window to type into, and the only one there is —
and an address is not a thing the model reading the turn can see.  So the words
name the owner as well, and that name is all a conversation holding no such task
has to go on: without it, a trigger that was mis-delivered and a trigger for a
task this agent never had are one message, and the honest answer to both is "not
mine, and I cannot say whose".  With it, the reader can say whose it claims to be
and hold its own ledger against the claim — which is the whole of the diagnosis
available from inside one conversation, and the reason the answer to a trigger
nobody can act on is a *report* rather than a repair.

**That call is this plugin starting the work itself**, as an ordinary agent task
under the scheduled task's own name — the same thing a model would get by calling
`agent_task_start`, except that here *this process* is the caller.  And that is
what makes the ledger close itself, which is the reason the dispatch is here
rather than in the prompt's prose:

```
clock fires → run written `pending` → trigger turn → schedule_run_now
                                                        └─ agent_task_start → id
poll (30s)  → agent_task_list → finished → ran, at the task's own finished_at
```

**A prompt that told the model to start the work *and* close the run when the
answer came back would be asking it to remember something across turns.**  The
answer arrives later, in whatever turn the person happens to be in, wearing
`[AgentTaskResult:…]` — a marker that says nothing about schedules — and a run
nobody closes is `pending` forever, which is the one state this ledger cannot
account for.  Nothing ever asks again (the reconciliation below is deliberately
not announced), so the failure is silent and permanent.  Making the *plugin* the
caller removes the whole class: it is handed the id, so a later poll can ask what
became of it, and the model's job is one synchronous call it cannot half-do.

**A task that has come due is not the agent's to decline**, which is why the
trigger names one thing to do and no way out of it.  `schedule_run_skip` is still
in the tool set, and it is for the runs that *did not happen* — one that could not
be started, one whose moment passed while slife2 was down — where the
alternatives are doing it late or leaving a `pending` row that the ledger cannot
account for.  The difference is the one this section keeps making: what a prompt
tells the agent to do is a decision about the common case, and a tool that exists
is a decision about the rest.

Nothing in this plugin makes a model call, and the loop it assembles is no loop at
all: a poll, a store, and **six tools — v1's six.**  The seventh it had,
`schedule_run_settle`, is gone rather than merely unused: with the harness closing
runs, a tool for a model to close one would be a second writer of the same row,
and the honest account of a run is the one the peer's own `finished_at` gives.

**The product of a run is not this plugin's either.**  v1's `memfiles` owned the
`reports` table *and* the schedules, so `report_save` backfilled `report_id` and
flipped a run to `ran` inside one write — the store was the thing that knew a
report meant a run had succeeded.  §1.4 records that the split is already made:
the cabinet is a plugin of its own, keyed by title, and knows nothing about
schedules.  And the work here is an agent task, whose answer `slife2-agent-task`
delivers back to the conversation — so what this ledger holds is that the run
happened and when it ended, and **what came of it is read where it was said**.
The price is the one v1 paid differently: there is no link from a run to its
output, and a person who wants both reads two places.  What it buys is that
neither plugin has to know the other exists.

**The ledger's rules, which are v1's, are the whole of the correctness here:**

* **`pending → ran | failed | missed | skipped`.**  The row is written *before*
  the trigger is handed over, so a fire cannot be repeated on the next poll: the
  anchor is the newest `due_at` on record, and recording is what moves it.  A
  hand-over that *refuses* proved nothing was written and the run is marked
  `failed`; a hand-over that does not come back is §1.2's third answer and leaves
  the row `pending`, because a run whose fate is unknown must not be reported as
  either.  A **dispatch** that fails closes the run too, with the peer's own
  words as the error — a `pending` row with no task behind it being the one state
  nothing can resolve.
* **A gap is recorded once, not once per fire.**  Coming up after a week down, a
  five-minute task has fourteen hundred fires behind it; v1's sweep would have
  written a row for each, which is a ledger that makes the gap *less* visible.
  So the sweep marks **the newest missed fire per task per gap** — one row saying
  "this was due and slife2 was not running", which is the fact a person acts on.
* **Nothing is announced, and the ledger is why that is enough.**  v1 printed
  unsettled runs into every turn's prompt, which is one plugin reaching into a
  prompt another plugin owns — the same objection §1.4 raises about `USER.md` —
  so that is out.  What is *also* out, for now, is the obvious replacement of
  handing the agent a note on the trigger's channel: a run that was missed or
  that never settled is **recorded**, `schedule_runs` lists it by state, and an
  agent that wants to know reads it there.  So a **fire is the only thing this
  plugin ever says to a conversation** — one path into it, one template, and a
  reader who learns one thing rather than two.  That the agent has to think to
  look is the cost, and DESIGN.md §9 holds it rather than this section quietly
  growing a second message.

**And that one message wears no marker, which is a decision rather than an
omission.**  A marker exists so a *result* can be matched to what produced it —
`[AgentTaskResult:{…}]` carries the task's id, name and state — and the
vocabulary is per-writer precisely because each writer's marker says something
different.  A trigger is not a result: it carries no id a caller needs back, and
what produced it is a clock.  The two facts it does have to carry are carried by
other means — **the channel** says the words did not come from the person
(`schedule:daily`, drawn as its own bubble by
`slife2.tui.widgets.channel_prefix`), and the **template's own first line** says
which task, exactly as `agent_task.j2` opens with `▸ Agent task: {{ name }}`.
The answer, when it comes, wears `[AgentTaskResult:{…}]` — because it *is* an
agent task's answer, and that is the plugin that produced it.

**The cron parser is `croniter`, which is v1's dependency and v1's decision made
twice.**  Its design notes for this feature say "no apscheduler, no croniter,
hand-write the parser"; the implementation used croniter anyway, and this is the
second time the same argument has ended the same way.  What it turns on is that
the five fields are trivial and their corners are not: cron's `day_or` rule — a
restricted day-of-month and a restricted day-of-week are a **union**, which is
nobody's guess at first reading — the DST rule, and `strict` cross-field
validation, which is what stops `0 9 31 2 *` from being stored as a task that is
enabled and can never fire.  A hand-written parser that got `day_or` backwards
would be wrong once a month and look like a clock problem.

It is used the way v1 used it: **one module wraps it and nothing else imports
it.**  `slife2.plugins.schedule.server` owns the timezone policy (an explicit IANA zone, else the
system local one) and the error contract (`ScheduleError`), so a `ZoneInfo` that
does not exist surfaces as the one exception a caller already catches rather than
as a `ZoneInfoNotFoundError` escaping through it.  What that wrapper still has to
say out loud is the part cron leaves underspecified: **a fire follows the wall
clock of its task's timezone**, and an empty zone holds the offset the machine is
on *now* rather than the one it will be on — so a task with no zone named does not
follow a DST change, and a named zone does.

**Its database is one file per agent, and that is also how it finds them.**  The
ledger is `<data>/agents/<agent>/schedule.db`, beside `turns.db`, for §5's reason
— agents are isolated by file rather than by a `WHERE` clause — and the shared
process learns which agents exist by listing those directories, exactly as
`slife2-wechat` learns it from the logins it wrote (`slife2.yaml`'s note on the
`wechat` server).  The poll loop is one loop for every agent, which is the second
half of why this is a plugin: two instances of one agent must not both fire.

**And the walk reaches every *home*, so a ledger carries the home it came from.**
`schedule_dbs` walks `known_homes()`, which is what makes one clock serve a
machine's homes rather than the one its daemon was started in — and a home is a
property of the *request* a shared process is serving, never of the process
(`slife2.libs.host.paths.in_home`).  So `paths.Ledger` is a pair: the ledger, and
the home it was found in.  Without the second half every fire reached for the
daemon's own home instead — the address `client_meta` stamps, the agent-task peer
that starts the work, and the window the trigger is offered to — and the result
was a trigger refused by a conversation that exists ("no window is showing it"),
three hand-over attempts later a run settled `failed`, and a task that therefore
never fires again.  It is the same defect the sibling plugins had already solved
and had already written down in their own comments: `wechat` enters each session's
home around the work it starts (`_spawn_in_home`), and `tui`'s per-window state is
keyed by the triple that includes the home, with the reason spelled out at
`slife2/plugins/tui/server.py`'s window table.  `reconcile`'s ask was the other
half: `agent_task_list` answers about *the caller's* home, so a run legitimately
dispatched from another home read as "no longer held" while its work was still
going.

**Whose a ledger is comes from a row inside it, not from the directory's name**,
and that is the one place this feature contradicts `slife2.libs.host.paths`' rule that
nothing inside an agent's directory repeats its name.  The rule is about a file
that could be renamed out of step; this is about a *sanitised* name that is not
the agent's — `a/b` lands in `a_b/` (`slife2.libs.store.db.safe_agent_name`) — where the
directory therefore cannot say whose ledger it is.  A trigger sent under the
sanitised name would go to a conversation that is not the one that asked, and
nothing about it would look wrong, so the row is worth the exception.  A ledger
that does not carry one is skipped with a warning rather than guessed at.

**It owns no config section, and v1 had none either.**  What is written down
about it is an address under `servers:`.  A machine that should not run scheduled
tasks is a machine where nobody writes one — the same argument the `wechat`
section is absent by — and every number the feature has is pacing inside the
plugin, where a number with no decision behind it belongs.  What that leaves
open is the one thing §9 names: nothing bounds how many tasks exist.

### 1.8 The A2A mesh, and what a per-process identity costs a shared one

**A2A is agent-to-agent traffic over MQTT, and the wire is not ours.**  The
publicly specified **A2A-over-MQTT** profile is the `a2a-over-mqtt` SDK: a
versioned topic tree with four categories, a retained agent card whose presence
rides an MQTT user property and a last will, JSON-RPC 2.0 envelopes whose
`ResponseTopic`/`CorrelationData` properties carry the reply routing, and the
task lifecycle (acknowledgement → working → artifact → terminal) with per-task
dedup and a requester retry ladder.  What slife2 writes is the harness glue
around it — the driver, the plugin, the two ledgers — which is v1's split and
v1's reason: interop with the rest of the ecosystem follows from the wire being
somebody else's, and a hand-rolled binding would be a second protocol wearing
the name.

**A plugin, by §1.1's first row.**  What it holds is somebody else's connection
— a broker session per agent — and a library would put an MQTT client in every
importer.  It is also the one plugin here whose **identity is per agent**, and
that is the whole of what the port had to decide.

**v1's identity *was* the process, and it cannot be here.**  The agent name is
not a label: it is the MQTT client id of both connections *and* the last segment
of the request topic, so two processes answering to one name trade the session
back and forth and every task sent to it runs **twice**.  v1 therefore ran one
plugin process per agent, which made its module globals that agent's by
construction.  These servers are shared (§4), so the same things live in a
**session per agent** — §1.2's WeChat rule, one section up — and a mesh is
created for the calling agent when a tool first says who it is.  **A name picks
out one agent on this machine, once** (§4), and the mesh is where that rule
becomes observable rather than merely enforced: a peer addresses `jack`, so a
second `jack` — another data directory, another terminal, another process — is a
name taken and is refused as one.  That is not a limitation of the mesh, it is
the same rule the launcher applies to two windows, arriving at the place where
somebody else can see it.  The collision
machinery ports unchanged and for the same reason it existed: before joining,
a throwaway connection with its own client id reads the retained card on the
name's own discovery topic, and a card that is neither our own instance's nor a
*retirement* means the name is taken and the mesh refuses to connect.  A twin
that appears afterwards takes the mesh down rather than fighting for the id, and
**no winner is picked** — a departing agent's last act is to publish an *offline*
card on the name's own topic, which would overwrite the survivor's card and
leave it advertising as offline to every peer.

**And a session is one `(home, agent)`, because a ledger, a card and a window are
per home.**  A shared daemon serves every home on the machine (`known_homes`), so
the walk that finds each mesh ledger finds it *in* one — `paths.Ledger`, the pair
`schedule_dbs` returns and `a2a_inbound_paths` returns for the same reason — and
the session key is that pair rather than the agent name, with each session's
writer running inside `in_home` for its whole life.  Keying by the name alone was
the defect `schedule` had one section over: a peer's message for *another* home's
`jack` was delivered in this home, where the window refused it, and the orphan
notice was built against a ledger that was empty.  What has not changed is the
broker's own answer to two homes' `jack`s: a name is an identity there (§4), the
collision machinery above refuses the second, and the correct outcome is the
refusal rather than the second silently attaching to the first one's mesh.

**And a name off the wire is scrubbed before it becomes one of ours.**  A card's
`agent_name` and an envelope's `from` are written by another process and
validated by nothing, and each becomes a *channel* (`a2a:jill`), a marker, a
stored turn and a log line.  So `safe_name` runs at every callback that reads
one — a name carrying a newline or an escape would otherwise forge a line in
every window showing that conversation, and in the log beside it.  It is §10.1's
rule one level over: what a peer sends is data, and data does not get to be the
shape of our display.

**A peer on this machine and a peer on another one are the same peer.**  Nothing
in the driver distinguishes them and nothing needs to: a local agent is simply
one whose mesh this process happens to hold, published to the same broker under
its own name.  What that costs is the enrollment question — a shared process has
to know *which names to be reachable under* — and the answer is **a window**.
An agent is on the mesh while a window is showing it: the window holds `join`
open for its life, so the connection, the retained card and the last will all
end at the moment somebody stops looking, and a peer that watched it arrive
watches it leave.  That is `attach`'s contract on the same window's inbox,
reused because it answers the same question — is anybody there? — and it is why
`resume` announces orphaned tasks and joins nothing.  A machine with nobody at a
terminal is the case that question has a second answer for: `slife2 --headless`
is that same window, holding the same call, with nothing drawn (§1.9).

**The ledger is not the enrollment, and using it as one was the mistake.**  It
is tempting — the file at `agents/<agent>/a2a.yaml` is already a per-agent record
— but what it records is the *inbound tasks a process died holding*, which is a
fact about work already received.  Read as an enrollment list it made presence
mean "has spoken at least once, ever": a card on the broker for a conversation
nobody was having, none at all for the window somebody had just opened, and a
permanent one for every agent that had ever called a tool.  Comparing against
`agents/` would be worse in exactly the way the old rule was meant to avoid.
A tool call still joins its agent lazily and keeps it — what follows a send is a
reply the agent is waiting for, so an agent with a task outstanding *is*
reachable, and that reason is never given back because nothing here knows when
the last reply arrived.

**The plugin delivers its own turns, and the drain tool was the seam.**  v1's
harness polled `__a2a_drain_incoming` once a second and injected what it found
into the agent's inbox; there is no harness here.  What replaced it is exactly
what §1.2 says replaced `__wechat_drain_incoming`: **`slife2.libs.harness.handover`** — the
window showing that conversation, which is the only road, and a hard failure when
it cannot be reached.  So the five queues become one ordering queue per session
with one writer behind it, which is the job the queues were doing for the
harness: arrival order, and keeping a mesh callback off the MQTT reader's stack.
A peer's message that reached no window is logged and dropped rather than run
somewhere else, because a peer re-sends a task itself when it gets no answer, and
that is the retry that knows what it is retrying.

**The marker is one envelope with a `type` in it** — `[PeerAgent:{"from": …}]`,
with the peer under `from` and deliberately *not* `agent_name`, which every system
prompt uses for the agent's own identity; v1 measured receivers misreading a
sender as themselves.  The `type` field is the one envelope's classifier, and the
writer and the reader (`marker`, `a2a_message_type`) are one pair so the
vocabulary stays one list.  `PeerAgent(<peer>)> ` is the bubble, drawn from the
channel alone (`a2a:<peer>`) because a hand-over carries the channel and the words
and nothing else.  **The spelling is not v1's** (`[A2A:` was): it is word-shaped
like every other marker a row carries (§1.2), and it says *peer* because a prefix
that said only "agent" would leave open which of the two agents is which — the
very confusion `from` exists to prevent.  The payload survives the port for the
reason `[Wechat:{…}]`'s keys do.

**A restart orphans every inbound task, and the agent is told.**  The completion
bridge and the peer's reply topic are both process-local — the latter is read off
the inbound request's own MQTT properties and never leaves it — so a task in
flight when a process dies can never be completed by anyone.  The ledger is the
line between *orphaned* and *never seen*: written on arrival, removed on
departure, so what a fresh process finds is exactly what its predecessor died
holding.  v1 rode that list on every turn's prompt; there is no per-turn prompt
here, so it is **one message handed over at the start that found it**, naming the
one reply those tasks can still get — a plain message to the peer.

**And it is queued rather than sent from there, which is a repair.** The start
that finds an orphan is a start whose peers cannot run a turn yet: a turn's first
step is the hub, the hub waits for every plugin (§4), so it is up *after* this
one. A notice handed over on the spot is therefore lost by construction — and one
that is not lost is worse, because the hand-over falls through to the agent
server, that call runs a whole turn, and the deadline judging *this* plugin is
ninety seconds against the turn's six hundred. So it goes onto the session's own
queue, the one writer every arrival already takes, and it carries a retry:
`False` — the one answer that proves nothing reached the conversation — puts it
back, and `None` does not. Nothing a *peer* sent is ever offered twice, because a
peer re-sends its own task when it gets no answer; a notice has nobody behind it,
which is exactly why its delivery is this plugin's to repeat. That this plugin
says something nobody asked for is the point rather than a lapse: the peer is
waiting.

**Everything else the mesh delivers is a message too, with one exception, and
that exception is presence.**  A task request is marked and answerable, an
auto-pushed result wears its task id, a withdrawal and a broadcast arrive as
messages — all of them things the model may have to act on.  A presence
*transition* is not: it is a line the harness has to say and nobody has to
answer, so it is a **note** — `ChatView.add_note` in the window showing that
agent, in the same bracket as `[restored 3 turns]`, with no turn run, nothing
stored, and the conversation never told.  v1 kept presence out of the inbox for
the same reason (its TUI had an activity feed, its model a per-turn prompt);
here the roster a model can ask for is `a2a_list_agents`, and a peer that wants
to be answered sends a task.  The words are v1's `format_presence_line` minus
its trailing `[status]`, which v1 needed because a model read that line too and
`online` twice in one line is a fact the first word already carries.

A non-transition renders as `None` and is not delivered, and a **cold retained
offline card** — a peer already gone before we subscribed — is cached for the
roster but never announced, so a dead card from a past session cannot fire a
fake "went offline".  Peer values reach a window, a model's context or a log
only through `safe_name`, which strips the control characters a peer could
otherwise use to write instructions into somebody's turn.

**Two things v1 had are gone.**  `__check` has no port: v2 has no
`system_health` and no `__`-prefixed tools at all, and what it reported — the
name, the collision, the broker, the roster — is `a2a_list_agents`' answer now,
which is where a model would look anyway.  And `broker.py`, v1's TCP probe, is
`slife2.libs.host.runtime.tcp_listening`.

**And the broker is started, which is the one place this contradicts v1.**
v1's rule was that slife leaves mosquitto to the user and treats a listening
broker as the feature's on/off switch; here a config that says `a2a:` has said it
wants the mesh, so a missing broker is a missing dependency rather than a
preference.  `slife2/plugins/a2a/broker.py` probes the configured address, starts
`mosquitto` on it if it can, and **fails the start** if it cannot — reported by
the launcher with the child's own log, because the alternative is four `a2a_*`
tools offered to a model for a mesh that is not there.  Only a **local** broker is
started: `broker.host` may name another machine, which is reached exactly the
same way and which nothing on this machine can stand in for, so an absent remote
broker is the same hard failure with no attempt made and a message saying which
of the two it was.  The config it hands mosquitto is not decoration — from
mosquitto 2.0 a bare `-p` version binds, listens, and then refuses every
anonymous client, which would arrive as a connect timeout naming nothing.

**And the take-down stops it, which is the other half of starting it.**  The
broker is the one daemon here the launcher does not launch — a native binary,
with no module and no MCP identity to check — so it is not in `specs` and
`launcher.stop` never sees it; what it has instead is a **record**, written by
`ensure` at the moment it spawns one, and `slife2.a2a.broker.stop` reads exactly
that record and nothing else.  So the last instance out (or `slife2 down`) takes
mosquitto with it, and a broker *slife2 did not start* — a system service, an
operator's own, one belonging to another data directory — is never signalled,
because there is no record to prove it is ours.  This replaced "a broker is a
machine's, not a run's", and the measurement that replaced it is what an orphan
looks like: a plugin killed rather than stopped leaves mosquitto alive with no
parent and no record, at a port the next start adopts as "already listening"
while owning nothing.

**And "local" means loopback, which is a third case rather than a detail.**  A
wildcard `broker.host` — `0.0.0.0`, `::`, or the empty string — is not this
machine's broker and not another machine's either: it is *every* interface, so
starting mosquitto on it with `allow_anonymous true` (the thing that makes a
loopback broker usable without a credential dance) would publish every
`$a2a/v1/…` topic and accept a task from anybody on the network.  It is refused
before any config is written, with a message naming the address rather than the
generic "remote broker is down" — the local half of the split is safe *because*
nothing but loopback reaches it, so an address that removes that property is not
a wider version of the same thing.

**`provider:` is a hard constraint and not a default.**  v1 carried a
`transport:` field, and answered a value it did not implement by disabling the
feature with a warning at config load — which is precisely the outcome that
leaves the model tools for a mesh that was silently switched off.  Mosquitto is
the only broker this build speaks to, any other value is a `ConfigError` when
the config is read, and adding one is adding a branch and a driver rather than
writing a different string.

### 1.9 The unattended agent, and what a window was for

**What the flag is for is §1.8's enrollment, because that is the one thing only
a window could provide.**  Everything else already works with no *terminal*, and
that is not a fallback but the same fact twice: §1.2's hand-over goes to the
window, and `--headless` **is** a window — the same connections, the same turns,
with a driver that reads and writes nothing — so a peer's task, a due scheduled
run and a task's answer all reach it.  What no other process could do was hold
`join`, and an agent nobody holds is simply **offline**: a machine with no
terminal is a machine a peer cannot task, which is the gap this closes.
`--headless` is a window with no
screen, so the enrollment is held and the agent is on the mesh — and everything
the window was already doing, it keeps doing.

**And it is the same code, which is the property worth paying for.**  "Headless
behaves like a window" is not a resemblance a later edit has to keep: what runs
is `SlifeApp`, with Textual's own headless driver — no input, no output, nothing
drawn and nothing printed — and **not one branch of the window is conditional on
it**.  The same three connections, the same queue, the same turns, the same
prompts: the inbox is held, so a WeChat message or a due scheduled run reaches a
headless agent exactly as it reaches one on a screen, and a tool call the model
asks about is put to the window exactly as it is there.  Anything else would be
a *capability* difference, and this is a mode rather than a second agent.

**The unattended agent is told it is unattended, and that is the whole of the
mechanism.**  A model is the thing that decides whether a call is safe to make
without a person, so the difference between a watched conversation and an
unwatched one belongs where the model reads it: the system prompt.  For a prompt
to see it, it has to be written down — the prompt is rendered by a *server*,
another process, which never saw the command line — so the flag rides the claim
the client already writes (`AgentClaim.headless`, `slife2.libs.host.runtime`) and
`opening_messages` reads it back to render it into the template as `headless`.
The claim is the right carrier for the reason it exists at all: one instance per
name, and it is the record of *which* one.  **No instance at all** renders the
same way, because it is the same situation — a peer tasking an agent nobody is
running is exactly what `handover.to_agent` is for, and nobody is there either.

A model that has not been told may ask for a confirmation nobody can give, and
spend the turn it was given on the question.  That is the cost of the lever being
a prompt rather than a rule, and it is the cost this design chooses: the
alternative is the harness deciding which calls are safe without a person, which
is a policy decision the operator never made.

**What it costs is the output, deliberately rather than for want of it.**  A
headless run prints nothing: no transcript, no status, no line per turn.  The
turns are recorded either way, so a window opened later restores the whole
conversation — §1.2's answer for a message that reached no window — and the
presence is on the mesh, where peers see it.  `Ctrl+C` is how it stops, which is
a headless-only path: a window reads that key itself (`SlifeApp.action_interrupt`
cancels the turn, or quits).  A `kill` is honest too — the broker's last will
publishes the offline card a cancelled `join` would have published.

### 1.10 The subagent, and what a peer without a process costs

**The design is sdd §3.5; this is what it costs and what it was chosen over.**
It began as a question about scale — the literature runs swarms of a thousand
agents, and should this machine be able to? — and the answer turned out to be
about one number, measured rather than reasoned.

**Two ways to have another agent, and they differ by ~50×.**  A `--headless`
instance is one OS process per agent, measured here at **99.5 MB private
(120 MB working set)**, 5–11 threads, ~260 handles, three held calls (the agent
server, the window's `attach`, the mesh's `join`) and two MQTT connections
(measured at exactly 2×N).  Its warm start is 2–4 s, of which the shared stack's
share is 171 ms for eighteen probes and 170 ms for the catalogue — so the cost is
the interpreter, not the handshakes.  A conversation held instead in the shared
agent server is a `Loop`, and `tracemalloc` over a real message list puts 500
turns at **2.35 MB of text, ~1.5 MB of objects**.  Fifty instances, or a
thousand loops.  The fixed stack — eighteen plugins at ~100 MB each, the `mcp:`
children, the broker — is **~2.9 GB per machine** either way, because the servers
are shared; what varies is only what each *agent* adds.

So a thousand is reachable, and it is reachable only on the second path:
3,000 × 100 MB of private commit plus a thousand interpreters is not a machine,
it is a cluster.  **What the design adds is a peer with no process** — and the
cost of that is the whole of the argument: at thirty to three hundred subagents
the bill is ~0.5–1 GB, not 30 GB, and the mesh's fan-out (every member subscribes
the unit's discovery wildcard and `event/+`, and the broker replays every
retained card on every join) stays inside a unit of tens rather than a thousand.

**Three shapes were considered, and two were dropped.**

- **A subagent as a headless instance.** The smallest change — `--headless`
  already exists, and "make it a subagent" is "run one without a terminal". It is
  also the 100 MB one, and the design that *wanted* it is not this one: an
  instance is a window that draws nothing, and a subagent has no window at all.
- **A `slife2-subagent` plugin holding the loops**, which is the shape
  `slife2-agent-task` has and argues for well ("it is a plugin because it holds
  loops"). Dropped because the loops are not the cost — the transport to them is:
  a new plugin means a new protocol hop for every message, and the identity of a
  subagent is already the client id, which every server keys by today.
- **A loop in the shared agent server, addressed by the second half of the client
  id** — this one.  `loops: dict[ConversationKey, Loop]` already exists, keyed by
  `(home, agent, subagent)`; nothing about the key is new. A subagent is an agent
  that is *allowed to be* the second half, so the whole of the storage change is
  a directory: `agent_dir(agent, subagent)`.

**Four decisions inside it, each with a reason that would be invisible later.**

- **The client id, not a new global name.** The alternative was to mint
  subagents as agent names — `researcher` as a name in its own right, with the
  parent recorded beside it. That needs new state (who made whom) and a new
  lookup; the client id needs neither, and it is what the hub, the context store,
  the cabinet and the model backend all key by already. The nesting is the
  *consequence*, not the mechanism: `agents/jack/researcher/` says who made whom
  because it is inside it.
- **`Loop.records` reverses.** It was `not self.subagent`, and its reason was
  written down: *"a subagent is a means to an end inside one of that agent's
  turns rather than an agent that owns anything"* — so its round trips were not
  filed under a conversation they were never part of. This design overturns the
  premise rather than the rule: a subagent now owns a conversation, a cabinet and
  a directory, and `subagent_remove` deletes them. The worker the old rule was
  about does not exist in this build (nothing ever created one), so the rule is
  replaced rather than kept beside it.
- **The marker is not renamed.** `[PeerAgent:{…}]` stays, and the subagent
  network gets its own envelope rather than `[Agent:{…}]`/`[Subagent:{…}]`. The
  reason is already in the code and it is a measurement: *"`Agent` alone is the
  one word this marker must not say … v1 measured receivers reading the sender as
  themselves."* `Peer` is relational and cannot be read as the reader; `Agent` and
  `Subagent` are *role* words, and on the subagent network both ends are one — a
  child receiving from its maker would read `Subagent` as the sender, and a
  subagent receiving from a sibling would read it as itself. What the design
  wants on screen (`Subagent(<name>)> `) is a **channel**, and the window draws
  from the channel alone (`slife2.tui.widgets.channel_prefix`), exactly as
  `AsyncTask(<name>)> ` is drawn. A marker renamed would split stored turns in
  half and buy nothing on screen.
- **Two networks, and the second is a section of its own** — not a namespace
  inside the first. The distinction the designer settled it with is the one that
  makes them different in kind: the agent↔agent network is a network of
  **remote** agents (peers that may be on another machine, on a broker somebody
  else runs) and the agent↔its-subagents network is a network of **local** ones
  (directories under the maker's own). So `subagent:` is a second section — its
  own broker address, which may be the same one by default and may be a
  different one, and its own `org`/`unit` — and the plugin that carries it is a
  second instance of the a2a module rather than a second namespace inside the
  first.

  **Its names carry no `a2a`**, which is the difference between a technical fact
  and a meaning: the protocol is what this build happens to reach a local agent
  with, while what the thing *is* is an agent and the agents it made. So the
  section is `subagent:`, the server answers as `slife2-subagent`, the four tools
  are `subagent_*`, the channel is `subagent:<name>` — and the one word that
  differs between the two envelopes is the one a reader could get wrong
  (`[PeerAgent:` against `[SubagentPeer:`; the window says `Subagent(name)>`
  either way, because there the reader *is* the maker). Its unit is per maker
  for the same reason the names are separate: two makers may each name a
  `researcher`, and one namespace would make those two one MQTT identity. What that buys over one process with two namespaces: a swarm's several
  hundred MQTT sessions and their tasks are not the machine's agent mesh's
  process, so neither can wedge or cap the other, and a machine can reach its
  peers through a team broker while its own subagents stay on the broker it
  runs. `unit` is still what keeps two makers' `researcher` from colliding and
  the fan-out per network small, but it is now a property of the *section*.

**What it deliberately does not add.**  No `spawn`: nothing here starts a
process, and `--headless` is a different thing (an instance with no terminal). No
claim: a name is claimed by an instance, and a subagent is not one. No third tool
vocabulary: the mesh's four tools are presented under `subagent_*` names, and the
*only* verbs this adds are the two A2A has no equivalent of — membership and
lifetime, `subagent_set` and `subagent_remove`. The schema is priced: the A2A
family is 2,492 bytes ≈ 620 tokens, plugin tools are exempt from `tool_load`
eviction (`ToolLoadSettings`), so a second family is a per-request tax in every
conversation forever — which is why the shell is names and descriptions rather
than code, and why its descriptions have to say *whose* roster they list
(`a2a_list_agents` says "peer"; a subagent family must say "yours").

**The second pass: what one subagent is, and what a fork is.**  `subagent_set`
grew two parameters and the record grew two keys, and both are answers to one
question — *where does one subagent's setting live?*

- **In the subagent, and nowhere else.**  The model, the role and whether it is a
  fork are in `agents/<maker>/<name>/subagent.yaml`, beside a conversation and a
  cabinet that are already per-subagent, and that file is deleted with the
  directory.  A key in `slife2.yaml` was the alternative and it loses on what this
  thing is: a subagent is made today and removed tomorrow, so a config key would
  be a setting to keep in step with a directory that can vanish without notice.
  The `subagent:` section stays for `max_running` and the local network, and the
  line between them is whose fact it is: *this subagent's* runs on this model in
  this role, and *the machine's* is the cap and where the network meets.
- **`fork` is a copy, not a shared list.**  "Share everything with the agent that
  made you" is answered with the context — copied at the moment the fork is made —
  and the fork's own turns are written nowhere: not under its own key, because a
  store's job is to be *that* conversation's memory and this conversation is
  somebody else's, and not under its maker's, because that would file one agent's
  work as another's history.  What that costs is stated rather than hidden: a
  fork's memory is the loop's, so a restart or the idle sweep means its next start
  is a fresh copy of its maker as it stands then.  That is the rule read at a
  second boundary rather than a gap, and it is exactly what distinguishes a fork
  from a plain subagent, whose turns are recorded and which therefore comes back.
- **The copy has to be *sendable*.**  A maker is mid-turn when it calls
  `subagent_set` — the call *is* a tool call — so its live list ends with an
  assistant message whose results have not come back yet, which is the one history
  every provider refuses (`trim_tools` guards the same pair from the other side).
  So the copy is cut back to the last exchange that finished
  (`slife2.plugins.agent.server.settled_messages`), which keeps a tool round the
  model has already been told about and drops the one still in motion.  The
  partial batch is in the rule because it is reachable: `_one` runs a step's calls
  in order, so a turn parked on the second of two has answered the first.
- **All three of a fork's omissions come off one flag.**  No restore (nothing is
  stored to restore from), no rebuild (no log to go back to, and a discriminator
  call would be spent deciding "keep what you have"), no record.  That is why it
  is a field on the `Loop` rather than the `records` property coming back: the old
  rule asked *who is asking* — a subagent's turns are not filed under a
  conversation they were never part of — and this one asks *what this conversation
  is made of*.
- **A subagent's prompt is its own, and one template serves every role.**
  `subagent.j2` ships beside `system.j2`: the operator's `agent.system_prompt` is
  a prompt for the agent they are running, and it cannot say the two things a
  subagent needs to be told — what it is, and that nobody is watching.  `role`
  reaches that template as a word ("You are scout, a code reviewer") and not yet
  as a choice of *file*.  **The per-role template is the step left open**, and it
  is left open rather than guessed at: `role: reviewer` wanting `reviewer.j2`
  needs an answer for a role with no file, and whether that answer is a refusal or
  a fall back to the shipped one is the designer's to give — a call-site fallback
  would be a degrade nobody approved (§10.2).

**The road: a subagent's turn is run by the network's own process, not by a
window.**  The window is the only road this system has for "something outside the
conversation became a turn in it" (`slife2.libs.harness.handover`: `to_window`,
and a delivery that finds no window is a hard failure).  **A subagent has no
window, and the TUI does not participate in a local network at all** — so the
local network needs a road that is not one, and it turns out to need no new
mechanism either: `send_message` is a *server* tool, and any peer may call it.

Three things, and they are one change:

- **The network holds its members' places.**  Not a window's `join`: the local
  plugin connects a member's mesh when it has one to serve, and membership is
  *read* rather than registered — `subagent_dirs(maker)`, the directories that
  carry a `subagent.yaml`, which is `schedule_dbs`' principle one level down.  So
  a member needs no record anywhere and a swarm is whatever the filesystem says;
  a member added while everything is up is picked up by the next look, which the
  send path takes anyway (below).
- **An inbound message becomes a `send_message` on the agent server — one rule,
  both directions.**  The message names its receiver (the topic's last segment),
  the receiver's conversation is `(maker, receiver)`, and the plugin runs
  `send_message(agent=maker, subagent=<the receiver's half>, prompt=…,
  channel="subagent:<sender>")`.  A message *to* a subagent is a turn in the
  subagent's conversation; a message *from* one is a turn in its maker's, which
  is the shape `agent-task` already has.  **And the subagent's reply is a tool
  call of its own** (`subagent_send_message`, or `subagent_broadcast` for all its
  maker's subagents), so nothing has to relay a turn's return value: every reply
  is simply the next inbound message, and the rule above handles it.
  A broadcast is the same rule once per member, since every member's responder
  receives it.
- **So the send path has to ensure the receiver's mesh exists.**  A message
  published to a member whose responder is not subscribed goes to nobody and
  reports nothing.  On the remote network that cannot happen — the peer's own
  window is holding its `join` — and here nothing is, so `a2a_send_message`
  ensures the *target's* session before publishing.  It cannot be done once at
  start: a model may `subagent_set` while everything is already up.

**What this costs, stated rather than implied.**  `handover` argues that running
a turn with nobody watching is a fallback — work advancing where no person can
see it, answering a peer that believes it was heard.  That argument is about an
*agent*, whose window is what makes it an agent somebody is running.  A local
member is defined as not having one, and its answer does not go to a person at
all: it goes to its maker, which is a model, and the maker reads it the way it
reads anything else — the turn is in the member's store, and what the maker is
handed is a message in *its* conversation.  So this is a second road on purpose
and not a fallback: it is what the local network *is*, and the TUI's part in it
is only to draw what it later reads (`subagent:<member>` → `Subagent(<member>)> `).

**Build state.**  In order, each green before the next: **(1) the directory** —
`agent_dir(agent, subagent)`, `database_path`, `turn_dbs`, and the docstring
reasons the old flat `<name>.turns.db` convention carried — **done**; **(2)
identity and lifetime** — `Loop.records`, `subagent_set`/`subagent_remove`, the
bound, the prompt's own name for a subagent — **done**; **(3) the second network**
— `Net`, a second instance of the module (`slife2.plugins.a2a.local_server`), its
own section and port, the four tool names *and descriptions* per network — **done
and seen running**: one `slife2` brings up both, `slife2-subagent` on 8041
serving `subagent_*`; **(4) the road** — the four things above, and the display
half of it is **done** (`subagent:<name>` → `Subagent(<name>)> `).  What is still
unstated is whose window runs a turn two levels down (sdd §12); and **(5) what one
subagent is** — `role`, `fork`, and the subagent's own file that holds them —
**done**, which is the second pass above.  What is still open there is a per-role
prompt template.

### 1.11 The interface language, and the line a shared daemon cannot be told

**Ported from v1, module and all** (`slife2/tui/i18n.py` is `slife/ui/i18n.py`
with this project's reasons on it): two languages, one table, one accessor, and
detection done in a dozen lines of stdlib rather than by a dependency.  The whole
user-facing surface is under a hundred strings; the packages in this space either
shell out to PowerShell on Windows or read the same environment variables this
does, and a catalog file for two languages is a build step for a dictionary.

**The language is resolved once, at import, and `--lang` overrides it before
anything is built.**  That ordering is the design and not an implementation
detail: strings are resolved where they are composed — the prompt's placeholder
at `compose`, the status bar's first connection word in `SlifeApp.__init__` — and
a language that could change under a running window would be two answers to "what
language is this window in" with nothing able to say which is right.

**What the port cost is a boundary, and it is this project's shape rather than
v1's.**  In v1 the plugins ran in the window's process, so `slife2.tui.notes`'
arrival lines (`⚡ jill online`) were composed by the same interpreter that drew
them and the flag reached every one of them.  Here the mesh and the WeChat link
are daemons: a shared process that outlives any one window, started once for the
whole machine and not started with anybody's `--lang`.  So those two lines follow
the OS locale while the window follows the flag.  Three ways out were considered
and none was taken: **pass the language to the daemons** (a second source of
truth, stale the moment somebody's flag differs from the process already up —
`--a2a-broker`'s problem, arriving a second time); **send the peer's name and
state over the wire and let the window write the sentence** (correct, and a change
to a hand-over vocabulary that belongs to the mesh rather than to a translation);
and **leave those lines in English** (which makes the mixed interface this port
exists to remove).  What is left is the cheapest true statement: a machine whose
OS language and `--lang` agree — every machine that does not pass the flag, which
is why the flag is an override rather than a setting — never sees the difference.

**Two rules about what stays English, and both were measured rather than
assumed.**  A *key cap* is not a word: `Esc`, `Ctrl+S` and `↑/↓` are what is
printed on the keyboard, and a translated one breaks the scan from key to action
— v1's status bar kept its keys and translated the words after them, and this one
does the same.  And a *tool label* is the model's vocabulary rather than the
window's: `_label()` derives `Read file` from `read_file`, and a translated one
would have the screen naming a tool the model cannot call by that name.

**A counted noun is two keys.**  `str.format` has no plural machinery and neither
does the table, so `{n} turn` / `{n} turns` are entries the *caller* chooses
between — the caller being the only place that has the count — and Chinese gives
both the same value.  The alternative, `{n} turn{s}`, puts English's plural rule
in a template and hands a translator a brace to get wrong.

**Nothing the model reads is in here, and no log line either.**  The system
prompt, every tool schema, every job's result strings and every `logger` call are
English by rule (sdd §9.4); so is the report `slife2` prints as the servers come
up, which is the terminal's account of the *processes* rather than the window's
chrome, and is the same surface as `slife2 status`.  A `--lang zh` start therefore
prints eight English server lines and then draws a Chinese window, and that is the
intended reading: the window is what the flag names.

**The tests are pinned to English, and they have to be.**  `_detect_language()`
asks the OS, and a contributor's Chinese Windows answers `zh` — so an autouse
fixture in `tests/conftest.py` sets it back before every test, because the tests
it protects are exactly the ones that never mention a language (a widget test
asserting a panel says `Arguments` has no idea it is asserting on a translation).
The fixture is what keeps the suite's verdict about the code rather than about
the machine it ran on.

## 2. Streaming: one pattern, used twice

MCP `tools/call` is a request/response. A TUI needs tokens as they arrive. Both
hops solve this the same way rather than inventing a special case each:

```
caller:  tools/call send_message(agent, subagent, ...)
server:    ← notifications/progress   {text: "你"}
           ← notifications/progress   {text: "好"}
           ← notifications/progress   {tool_start: ...}
           → result                   {text: "...", new_messages: [...]}
```

**The result is authoritative; the notifications are display.** This is not a
nicety. Progress notifications can be dropped, delayed, or coalesced by the
transport without the caller noticing, because the caller builds its state from
the returned value. It is also why `slife2.tui.widgets.ChatView` *discards*
deltas that arrive after a turn's result: they are late fragments of something
already replaced.

**There is a third use, and it inverts that rule rather than joining it.**  The
message a channel hands a window travels down the *window's* open call as one
progress notification (`slife2.plugins.tui.server`), and there the notification is not
display at all — it is the payload, and a dropped one is a turn nobody ever ran.
So the caller's assurance cannot come from the end of the stream: it comes from
the write. `submit` returns when the plugin has put the words down that stream,
a `submit` that could not is withdrawn rather than delivered late, and the
window does *not* acknowledge — an acknowledgement is a second call whose late
arrival resurrects a message the caller already gave up on, which is the one
failure the write-as-acknowledgement is there to prevent. The rate is one
notification per message rather than one per token, which is what makes a
lossy channel acceptable here and not there.

Why progress and not `Context.log()`, which would carry structured `extra` and
skip the JSON encoding:

1. **Request scoping.** Progress is delivered to the handler of the call that
   caused it, so an event belongs to a known request. `notifications/message`
   has no request correlation at all — with two turns in flight the stream is
   uninterpretable.
   (An earlier version of this file credited `related_request_id` for that. It
   is on the wire and it does carry the correlation, but it never reaches the
   handler: the callback signature is `(progress, total, message)`. The
   attribution is the per-call handler, and the difference matters the day
   somebody tries to use the field.)
2. **Opt-in delivery.** `report_progress` is a silent no-op when the client
   supplied no progress token. A script calling the same tool gets a correct
   answer and the server does no extra work. Logging is always on.
3. **No level gating.** Logging is filtered by `logging/setLevel`; a client that
   never negotiates a level loses deltas silently.
4. **No log noise.** FastMCP mirrors to-client log messages into the server's own
   log pipeline — every token would land in the log at DEBUG.
5. **Logging is on its way out.** The 2026-07-28 revision deprecates
   `logging`/`notifications/message` outright (SEP-2577), and a server MUST NOT
   emit a log notification for a request that did not ask for one. So a design
   built on `Context.log()` would be migrating rather than shipping — and the
   migration target is progress, which is where this started.

The payload is a plain string, so events cross as compact JSON with a `type`
tag, `ensure_ascii=True`. Both halves live next to each other
(`events.py`, `llm/wire.py`) so one round-trip test covers each contract.

## 3. The loop, and why the server owns it

**Every server is stateless; the loop is where the conversation lives.**

This was the largest change during implementation, and it was forced by
measurement rather than preference. The original design keyed conversation
memory on `Context.session_id`. Measured on FastMCP 4.0.11:

```
in-memory transport:   call 1 → fba941ad-…   call 2 → 719b5744-…
real HTTP, same client: call 1 → 8e0a6a61-…   call 2 → 3c80209a-…
incoming headers:      {}   ← the client never sends mcp-session-id
```

`session_id` is a fresh uuid per request. That is not a bug: the **2026-07-28**
revision of MCP removed the `Mcp-Session-Id` header and the `initialize`
handshake from Streamable HTTP (SEP-2567, SEP-2575). MCP has no protocol-level
sessions, and building memory on one means the memory is only as stable as a
header that no longer exists.

That measurement is what rules out the obvious answer. A server cannot key state
on the connection, because the protocol has no connection identity left to key
on. So the state has to be **named by the caller** — which is a decision per
call, not per server.

**So a turn is not a call, and the server is not a function.** `run_turn(messages,
prompt)` — send the whole conversation, get back what to remember — is the right
shape for exactly one client whose state is the conversation it is already
displaying, and that premise is what a message arriving mid-turn breaks. A turn
has to be addressable in its own right, for two reasons that are really one: so
that a message arriving while it is busy can **wait** for it rather than displace
what is running, and so that a caller which is not displaying the conversation
can still continue one. (A subagent is that caller — see §9.)

So a conversation is an object, and the surface is three tools:

```
send_message(agent, subagent, prompt) → {text, usage, steps, stop_reason, model,
                                         refused}
reset(agent, subagent)
transcript(agent, subagent)           → {turns}   the conversation, for a screen
```

**Every key is always there**, including `refused` on the path where nothing
could be refused: `refused` is the calls a person declined in this turn, and a
caller that has to ask a reply dict whether a key exists is a caller that gets a
`KeyError` on the one branch nobody exercised. The folded-in case — a message
that arrived mid-turn and rode the running one — answers the same keys, which is
what a previous version got wrong by building the dict a second time by hand.

**`new_messages` is gone**, and its absence is the point: the caller has nothing
left to remember. It keeps an identity and no state at all — and the identity is
not something the server gave it.

**This is where the design departs from SEP-2567, deliberately.** That SEP is the
one that removed sessions, and its guidance for what replaces them is *"servers
that need cross-call state use explicit, server-minted handles passed as ordinary
tool arguments"*. The first cut of this file followed that literally, and the
handle was a mistake — not because it was opaque or because it was minted, but
because **anything a caller has to keep is a thing that can be lost, expired, or
wrong after a restart, and every caller then needs a path for that.** A loop id
could go stale two ways, so the TUI carried a "your conversation is gone" branch
and DESIGN carried a paragraph about how ordinary that was.

A name cannot go stale. So the id is `(agent, subagent)` — something the caller
already knows, because it is *who the caller is*. The server starts a
conversation the first time it sees a key, so sending is opening; an idle sweep
may drop the state underneath and the next message simply begins again. There is
no create, no handle, no expiry, and no error class. What the SEP's four
requirements were buying — opaqueness, entropy, bounded lifetime, a readable
expiry error — are all answers to problems a keyed-by-name design does not have.

The price is the one the SEP warns about: a name is guessable, so possession is
not authorization. Here there is no authorization to subvert — every server
listens on loopback, the only thing on the other end is a CLI the user started,
and the same was already true of `agent` before this change.

Four things come back with the state, and each is paid for here rather than
avoided:

- **A per-conversation lock**, because two turns can now race over one list.
- **An inbox**, which is that lock seen from the other side: a `send_message` for
  a busy loop *waits*, in arrival order. Nothing is dropped and nothing is
  cancelled, and what it waits for is the running turn's next **step boundary** —
  v1's *cut-in*, ported. What it is handed at that boundary is a **harness tool
  pair**: the message becomes a call to `_check_new_input` and a result carrying
  the message's own words, written into the running turn's message list. It is
  deliberately not appended as a *user* message on arrival — the loop re-reads
  the list at every step, so that would be read as the turn's own input, arriving
  silently, and in the middle of an unanswered tool exchange it is a 400 from
  several providers. The model reads the pair as what it is: something that
  arrived while it was working.
  The arrangement is v1's `Inbox`/`_check_new_input` except in one place, and
  that place is why the answer handoff had to be invented rather than copied.
  v1's senders were channels that could give up — its own words are "sender-side
  timeout+degrade is the backstop" — so a message folded into a running turn
  simply never got its own reply. Here every inbox entry has a caller parked on
  the lock expecting `{text, usage, steps}`. So the absorbing turn *answers for
  it*: `Pending.result` is set from that turn's result, and the waiter returns
  what the turn's own caller got, with `injected` true. A turn that produced no
  result — cancelled or failed — writes nothing, and the message then runs its
  own turn, which is the only reason the field can be `None`.
  **And no screen draws it.** The pair is in the record and in every request the
  model makes afterwards, so the conversation has it either way — but the live
  transcript is rendered from `TurnEvent`s and an auto-invoked call raises none
  (v1 routes around the tool-execution path), and a rebuilt one filters
  `_`-prefixed calls out by name. Drawing it is §9's, and it is three changes
  rather than one, which is why it is named there instead of half-built here.
- **Cancellation repair**, which the plan called the sharpest correctness edge in
  the system and which statelessness had deleted. A cancelled turn can leave an
  assistant message whose tool calls are only partly answered, and *that* list is
  a 400 from every provider — so the repair truncates back to the user's own
  message. The `+ 1` is load-bearing: the loop appends the user's message itself,
  so truncating to the snapshot would delete the very message this arrangement
  exists to not lose.
- **The image repair**, which is that same edge from the other side and no
  cancellation at all. A turn that dies at the provider keeps its messages — that
  is `remember_turn`'s rule — and one part of it can make every later turn the
  same 400: an attached image the provider refuses is carried by every request
  that follows, because a failed turn's messages are unbacked and ride the next
  rebuild's carried tail verbatim. In a process that outlives the terminal there
  is then no way back to a usable conversation. So the repair drops the pixels
  and keeps the text, which is exactly what a restore would have left — storage
  never keeps an image — and says so in the error the caller reads.
- **A context that has to be decided rather than accumulated**, which is the
  fifth and the one this design arrived at last. A conversation's turns are
  *stored* — that is what makes the state survivable — and which of them a turn
  runs on is a decision somebody has to make, made once per turn by the
  discriminator (§5.1). The server owns the conversation; the store owns the log
  and the decision.

**Every turn of a loop that records is written, cancelled ones included.**
The rule is deliberately not "remember to record the cancel path": a write that
is conditional on how a turn ended is a write somebody can forget to make, and
the failure it produces is a conversation in the transcript that the database has
never heard of. A cancelled turn's row is a user message with no answer, which is
exactly what it was. (Nothing writes it today, but `Loop.records` is what a
subagent's loop will set false: a worker's round trips must not land in its
parent agent's record.)

One measurement decides *how* that write is made. From a cancelled handler a
plain `await` is cancelled again at its next checkpoint — measured against the
real transport, not assumed — so a record written that way is simply lost. A task
created outside the cancelled scope does land, so the **cancel path** detaches
the write and logs its failure instead of dropping it.

**The ordinary path writes inside the lock.** Outside it, a queued turn could
start before the write landed — and the write maintains the *live-context list*,
which a rebuild reads, so that turn would be handed a context missing the turn it
is following up on. The price is one loopback before a queued turn starts, and
nothing is given up: the ordering hazard the *detached* path still has is
confined to a cancelled turn, whose messages are deliberately left *unbacked* and
carried verbatim by the next rebuild (§5.1) rather than counted twice.

**The id travels with the call, at every hop.** It is not only the agent server
that receives it: the write to the store and the model call are both made under the
same `(agent, subagent)`, so no hop in this system is anonymous. What the model
servers do *not* do with it is keep a conversation — see §6, and the note there
about why the history cannot live on the far side of a protocol-specific hop.

**`--agent` exclusivity is now the only thing keeping one agent to one
conversation.** The first cut enforced it in the server, because a caller
arriving under a name had to be given *its* loop rather than the previous one's.
A key is created on demand, so there is no previous one to hand over: two live
clients under one name would now genuinely share a conversation. `slife2.libs.host.launcher`
still refuses to start the second one (§4), and that is where the rule lives —
one place, and the place that already had to know.

`stateless_http=True` stays, and it is not in tension with any of this: at
2026-07-28 the flag is consulted only on the handshake-era path, so what it
pins is that no request depends on a session surviving between two of them.
Nothing here does — the state is named by an ordinary argument, which is the
whole point.

## 4. Shared servers, and the launcher

The servers are **shared infrastructure**, not a row of terminals a user
babysits. `slife2` brings up what its config needs and attaches to whatever is
already running, so a second instance is one command and no duplicate process
appears.

Five decisions carry that:

**A probe decides whether a server is alive**, and what it compares is the
server's *identity* — `Client.server_info`'s name, which the handshake carries
for free, falling back to a `tools/list` membership check only when a server
reports no name. §6 has the measurement and why the fallback is load-bearing.
What the probe rules out either way is being pointed at a *different* MCP
server, which a bare connection test waves through. A listening port proves
something is there and a record file proves something was there once, so the
record is never consulted for liveness and a stale one cannot wedge a start.

**A lock is a kernel object.** A Windows named mutex, a POSIX `flock`. The
operating system releases both when the holder dies, however it dies — so there
is no stale-lock protocol, no "is the holder still alive", and no window where
the answer is wrong. An `O_EXCL` lockfile was rejected precisely because all of
that would have to be written by hand and could still be wrong.

**A pid is not an identity.** Windows recycles pids, so by the time `down` runs,
the number in a record may belong to something else. Each record carries a
process start token, and `down` refuses to signal a pid it cannot prove is ours —
because leaving a daemon running is a far better failure than killing whatever
the user happened to be running.

**A pid that exists is not a process that is running.** On POSIX a child that
has exited keeps its place in the process table — the pid, the state, an exit
status nobody has read — until whoever spawned it waits, and `os.kill(pid, 0)`
succeeds on that zombie exactly as it does on a live process. `runtime.pid_alive`
reads the state too, and two questions were being answered wrongly without it:
`stop`, which asked after seventeen daemons it had just killed and was told `pid
N did not exit` for every one of them on Linux and macOS while Windows, which has
no zombies, passed; and `live_clients`, the count that decides whether the last
client out stops the servers, which a killed client left as a zombie would hold
open forever. A state that could not be read is **not** a zombie — a daemon
reported as gone is one nobody goes looking for, and the port it still holds is
the symptom that arrives later.

**A child must escape its parent's job.** A process created without
`CREATE_BREAKAWAY_FROM_JOB` inherits its parent's job object, and if the parent
is inside a kill-on-close job — a CI runner, some terminal hosts — a merely
"detached" child dies with it. v1 does the opposite on purpose: it assigns
children to a kill-on-close job so they die with their parent. Here the
requirement is exactly inverted, and it is a requirement about *survival*, so it
is worth knowing which way it points.

**Which server waits for which is a table rather than prose, and everything the
table says nothing about comes up at once.** `slife2.libs.host.launcher.NEEDS` is that
table, and the shape it buys is the one the *graph* has rather than the one the
config file lists: five waves, whose depth is the real chain — the embedder, then
the stores that open it, then the hub, then the task server, then the clock —
with each plugin riding whichever wave its own dependencies allow. Two of its
entries are rules rather than pairs of names. The hub waits for **every** plugin,
because it asks each one for its tools — including the ones with nothing to offer
the model — and refuses to serve a list while any of them is not answering; the
only plugins it may not wait for are the three that wait for it (itself, the task
server, and the clock). And the agent server has no entry at all, though its
first turn reaches both the hub and the store: the edge the other way would close
a cycle, and what makes leaving it out safe is that the agent server does nothing
until a caller arrives — and a caller arrives either from a window, which exists
only once the last wave is done, or from a plugin that is already waiting on the
hub. The one exception is named where it lives: `a2a` hands over its orphan
notice at its *own* start-up, before any of that is true, and it is the reason
that notice's delivery is retried rather than attempted once (§1.8).

On top of that sits one piece of bookkeeping: each client registers itself, and
the **last one out** stops the servers. Without it, the daemon rule would mean an
ordinary exit leaves the servers running until the next reboot. The count is
by pid liveness rather than a counter, so a client that was killed — and so
never deregistered — cannot keep them alive forever.

**That bookkeeping belongs to the machine, and it moved to say so.** The records
naming pids, the start locks, the logs, the claims on agent names and the list of
homes this machine has run an agent in live in `~/.slife2/runtime`
(`paths.runtime_dir`; `SLIFE2_RUNTIME_DIR` overrides, which is what a test uses to
keep off a developer's real one). Not under a data directory, for the reason the
servers are shared: there is one set of them, so there is one record of them, and
a copy of any of it per directory can only disagree with the others. That is not
a hypothetical — the shape it had is what a second `--data-dir` ran into. Its
`status` reported a running stack it could not see (`running (external) — no
record; not started by slife2`, all eighteen lines of it) because the records
were in the other directory; the start lock was in neither launcher's way, so two
cold starts could race for one port, which is the one thing that lock exists to
prevent; and `others_running` — the count `live_clients` is built from — read
*this* directory's clients, so a client there counted itself the last one out
while another home was still working. What is left in a data directory is what a
person wrote and what an agent's run produced, and the test is the one `runtime/`
always had: delete it and the next start rebuilds whatever it needs.

**And a call is served from the home it came from.** A plugin is shared, so the
process that writes an agent's turn file is generally not the one that was
started with `--data-dir`: it is whichever is already up. Which directory the
work belongs in therefore travels *with the call*, out of band, in the same
`_meta` blob that already carried the conversation (`slife2.libs.plugin.audience`,
key `data_dir`) — beside the agent and the subagent, and out of reach of a model
for the same reason they are: an argument would be a directory a model could name,
and naming somebody else's is the whole of what this prevents. `paths.home()`
answers with it for the length of a call, `protocol.ServeTheCallersHome` is the
middleware every server registers that puts it there (`house_server`, so a server
built over the in-memory transport honours it too), and `protocol.Peer` — what
`open_server` hands back — puts it on every call this process makes in turn, which
is what carries it past the first hop: a turn's `remember` reaches the context
store with the home the turn is in, and the store resolves `agents/<agent>/…`
there rather than in its own directory. Everything a plugin *derives* rather than
reads is kept per home, built the first time that home asks, out of **that home's**
`slife2.yaml`: the catalogue a hub opens (`Catalogue.store`), one `Family` per
home for the four section plugins (`slife2.libs.plugin.family.Homes`), the
declarations and the embedder the hub holds, and the config cache itself
(`load_cached`, one entry per file — a single slot would have made two homes
alternating look to every reader like a config edited between turns, since
"has it changed?" is an `is` comparison). What is **not** per home is the set of
plugin addresses the hub connects to: those are the shared servers, one set of
ports for the whole machine, and nothing in this arrangement starts a process or
a port. The rule for which config a call is served from is one line in one place
(`config.for_the_call`): the home's file, unless one was injected — every test
builds its server with one, and a server that went looking for a file instead
would be reading a developer's own `slife2.yaml`.

**An external server, though, is one server per *name* — not one per home.** The
per-home rule above is about what a config section *says*: the entries, their
switches, the declarations. It was also being applied to what a section *starts*,
and that is a different kind of thing: a `mcp:` entry is a process on this machine,
and two data directories naming it are two homes naming one server. The cost was
concrete and immediate — `slife2 --agent jack --data-dir D` over a copy of a
config has the same eighteen `mcp:` entries and the same two `rest-api:` ones, so
the second home started twenty `npx`/`uvx` processes for servers the first was
already running.

So the split is by *kind of fact*, one level down from the section:
`family.Link` is the machine's — the connection and the listing — and
`family.Held` is the home's — the switch, the description, the autoload flag, and
therefore the whole of what that home declares. `Links` pools the former by
`(category, name)` per plugin process; the latter is what two homes' sections may
disagree about, and a home with an entry switched *off* holds no link at all,
which is what stops one home's switch from keeping another home's process up.

**The designer's rule is that the name decides, and the consequences are worth
having written down.** Two homes naming one entry with different commands is not
two servers: the first one started wins, the second is warned about by name and
directory, and the running link is what both homes use — because the *rows* are
keyed by the entry's name too, so a second process would offer the same tool names
and the catalogue would refuse one of them anyway. `Held.settings` is therefore
not what the link is running, and the entry that *is* running is the one the first
home named. An edit through a management tool is the exception and the reason
`Links.reconfigure` exists: the name is the identity, so `mcp_set` on a shared
entry is one server being reconfigured for every home holding it, rather than a
second one appearing beside it — and without that step a shared link joined
without being reconfigured would ignore the edit in silence.

The one property a shared process cannot have per home is its **working
directory**: an entry that cares names its own `cwd:`, and one that does not gets
the home that first asked for it rather than a directory that would move under a
running process. That is a wart, it is stated in the code and in `sdd.md` §10.3,
and the alternative — a process per home — is the twenty processes this rule
exists to stop.

**And the pool belongs to the plugin process, not to the module.** Built in
`build_family_server` and handed to every family, like `Homes`, so that two
servers built in one *process* — which is what a test builds — do not share a pool
and a family cannot inherit a process from the previous test. A module-level
dictionary would have been shorter and would have made a test pass for a reason
that is not its own.

`--agent NAME` **decides everything about who an agent is, and two agents may not
share a name.**  It titles the window, it reaches the agent server, and it creates
no port, no process and no config section — but it is no longer "a label and
nothing more": the claim on a name is taken **by the name alone**, machine-wide,
so a second `jack` — another data directory, another terminal — is refused rather
than accepted as a different agent. Which home a name was started in is where its
files are; it is not part of who the agent is. **And this machine keeps no memory
of it**: the next run of that name is told a home and uses *that* one, whether it
is the same directory or another. The home it used before is the operator's to
keep, to reuse or to delete, and nothing here goes looking for it — one live
instance per name is the whole of what a name is held to, and a home is not
something the machine administers. What the *servers* key their state by is the
client id §1 defines, name and conversation together with the home the call came
from, because a shared process has to resolve a file as well as recognise a
caller — so the isolation the earlier claim deferred ("if it ever
appears, it belongs inside an MCP server") is exactly what appeared there: a
plugin keeps one of each thing per conversation, and no call can reach another's.

**The window does not listen at all, and that is a repair rather than a
taste.**  An earlier shape gave the terminal an MCP server of its own, on a
`tui:` section of its own that nothing started — so that a channel like WeChat
could hand it a message to run as something the person typed (§1.2).  It worked
for one window and could not work for two: the address was one for the machine,
so a second terminal bound nothing and was unreachable, and a call had no way to
say *which* window it meant.  That is the topology the paragraph above forbids,
arriving in practice.

What replaced it is the ordinary answer this section gives for everything else.
`slife2-tui` is a plugin: an address under `servers:`, started and shared by the
launcher, probed by its own name, recorded and stopped with the rest — and the
session inside it is keyed by the agent, exactly as a WeChat login is (§1.2).
The window is a *client* of it and holds one `attach` call open for as long as it
is showing a conversation, which is what "a session per agent" means concretely
and why a window that dies leaves nothing behind: the stream goes, the handler
behind it is cancelled, and the session with it.  `--agent NAME` still creates no
port and no process — it is now, as it always was meant to be, an identity that
reaches a server rather than a piece of topology.

The honest cost is the one the old paragraphs claimed and did not have to pay:
the terminal is reachable by anything on loopback that knows the plugin's
address.  Which is the same standing as every other server here (§3: possession
is not authorization, because there is nothing on loopback but this machine's own
processes) — and it is now true of a *shared* address rather than of a port one
window happened to hold.

## 5. The context store

**A conversation's turns, and the decision about which of them it runs on.**  One
plugin, `slife2-context`, and this is v1's `memdb` ported: the turn log, the two
tools a model reads it with, the live-context list, the recall, and the rebuild
and restore that put a context together. The store underneath is a library
(`slife2.libs.store.db`), and the two indexes under *that* are a library of their own
(`slife2.libs.store.index`, §1.1) — for the reason §1.1 gives, that a file needs no process
— and that is v1's arrangement too, where the headless host restores a session
straight from `SessionStore` with no MCP transport in the path.

The **second** thing this system keeps in a file, and not in this module, is the
tool catalogue (`slife2.libs.store.catalogue`), which is the *other* thing a file is enough
for. It sits beside the turns rather than inside them because the two have no
name in common, and it is opened by the toolhub rather than served by anybody
(§8.4).

**Kept as it happened, and nothing else.**  The store does not summarise, does
not decide what mattered, and until this change put nothing back into a
conversation. The schema is v1's `turn` table, minus one column — one row per
turn, columns for the two token counts, the two timestamps and the identity that
v1 arrived at by using it. The missing column is `user_message`: v1 keeps the user's half beside the
assistant's so it can be searched and embedded apart from the answer, and here
it is `messages[0]` instead, because a turn is one list of messages and a column
holding the first element of it would be a second copy to keep in step.
Everything is stored as it happened, with **one** deliberate
exception, announced rather than silent: an attached image becomes a note saying
it was there. There used to be a second — an oversized tool result was kept as a
head-and-tail digest — and §5.2 is where that went and what replaced it. Nothing
is lost by the one that is left that the row was the only copy of — the
file is still named — in the sentence the person wrote, and in the answer of the
`attach_image` call that attached it — so attaching it again is what sends it
again. What the
rule buys is that a turn is text a model can read back: a ten-megabyte
screenshot in the row would be megabytes carried into every later read of that
turn, to say what one line already says.

**A picture leaves the live conversation the same way, and for a sharper
reason.** A provider that refuses one refuses it on every request that carries
it, and a failed turn's messages are re-sent by every turn after it — so one bad
attachment is a conversation with no way forward at all. The agent server drops
the pixels when a turn dies there and names what it did in the error
(`slife2.libs.turn.attachments.drop`, whose note is the one the row keeps). What that buys
is that the live list and the record never disagree about whether a picture is
still in hand: the picture is live state and nothing else, on either side of the
save.

So a question the schema cannot answer today can be asked of the same rows later
without a migration — and the columns the later features need already exist,
because adding a column to a table with rows in it is the one change this schema
has no mechanism for.

**Agents are isolated by file.** `agent="jack"` reads and writes
`<data>/agents/jack/turns.db`, and no query can reach another agent's turns because there are
no other agent's turns in the file. Isolation as a property of the filesystem
beats isolation as a `WHERE` clause somebody can forget to write. The name is the
whole of whose conversation it is — one instance of a name runs on a machine at a
time (§4) — and what the *call* adds is where that conversation's files are: the
store is shared, so `agent="jack"` has a directory under every home that has ever
run a jack in it, and the home the call came from is what says which of them this
turn writes to. Both the file and the in-memory loop are keyed by it
(`slife2.libs.plugin.audience.client_key`), which is also what keeps a call from
one home from finding a conversation belonging to another.

### 5.1 The live context, and the rebuild

**The context is chosen, not accumulated.**  Before every turn the agent says
what to keep of the turns in hand and what to *recall* from the log, and the turn
runs on the two together. One model call decides it — the *discriminator* — and
it is the most expensive thing in a turn, which is why every way it can fail ends
in "keep the context" rather than in a retry:

```
send_message
  └─ the first message for a key      → restore   the exit-time context, replayed
  └─ per turn, inside the lock, before the user's message:
       rebuild                                    keep ∪ recall, one model call
       run the turn (the loop appends the message)
       save                                       the new id joins the live list
```

**The live-context list is the state this adds**, and it is what makes the rest
work. An ordered array of turn ids, in the `context` table of the turn file, with
three writers: the save appends the new rowid *inside the turn's own
transaction*; a rebuild replaces it wholesale; `forget` clears it. **Its order is
authoritative** — reads replay it as written and never re-sort, because the list
already encodes what was kept, what was dropped and what was recalled, and a
selection is not always contiguous. It is an *addition* to the schema, so a file
that predates it opens unchanged and answers `[]`, which is the honest answer for
a conversation whose context has never been recorded.

**The union is what makes "keep this and add that" expressible.** Kept and
recalled are joined by id and never reconciled, so an empty recall is harmless —
a union with nothing is the base — where an overriding selection would have
discarded the context it replaced, and a query that merely failed to match would
empty it. Clearing is only ever the explicit `"clear"`.

**What decides is one call against the conversation in hand**, with the current
input quoted inside the instruction. That shape is load-bearing twice over: the
turn being recalled is usually a follow-up, and a follow-up names its subject
only through the conversation, so a query written from the input alone retrieves
nothing. The reply is `{"context": …, "recall": …}` — the ids to keep, or
`"keep"`, or `"clear"`; and one recall condition, which is a period, a query, or
a query within a period. Six decisions from two independent fields, and the
common one is `{}` — **a decision that asks for exactly what is in hand rebuilds
nothing at all**, which is what keeps a call this expensive from being paid for
nothing.

**The query is words, and the instruction is where that is said.** The two legs
want different things from one string: the words are matched exactly and are
asked for *all at once* (`textindex.match_expression`), while the meaning leg
reads whatever it is handed. So a sentence is a query with one leg switched off,
and it is measured: on the live turn log — 17 turns, the real embedder — a
sentence-shaped query fires the keyword leg **not once in 16**, and the log's own
seventeen inputs are the same fact from the other side, sixteen of them reaching
only the turn they were typed into. A word-shaped query fires it 10 times in 15
and finds the target with it on 9 of those. The meaning leg is unharmed either
way — a compact word list ranks the same as the sentence it was taken from
(36/36 and the same MRR on the 130-tool catalogue, §8) — so naming the subject is
the shape that keeps both halves working, and it gives up nothing.

**And that shape is asked for in prose rather than in the store, because the
recall is good either way.** Measured on the same log, the target came back in 32
of 32 queries and was ranked first in 25, the meaning leg alone finding every one
of them, and no page was emptied by `min_similarity`. The split `tool_search`
uses — the words to the keyword leg, the sentences to the meaning leg — measured
one row of rank in fifteen here; and the half of it that mattered most there (a
keywords-only call reaching the meaning leg, 23/36 to 34/36, §8) is what a single
query already does for turns, since `recall` hands its one string to both legs
and a recall has exactly one question for `fuse_ranked` to fuse. So the template
and `RECALL_REPLY` say what the query is and `slife2.libs.store.db` is left alone: one row
of rank is not worth a second field that every reply has to get right.

**The decision is reported, because it is the one thing in a turn nobody sees.**
`rebuild` answers with two counts — what survived of the turns in hand, and how
many came back from the log — and the agent server turns them into a
`ContextChosen` event **before the turn's first token**: it is a fact about what
the conversation is about to be, and a display that learned it afterwards would be
showing one turn's context under another turn's answer. The window draws it as a
note between the prompt and the answer, in the shape every harness line wears:
the *words* are `slife2.tui.notes`' and the *brackets* are `ChatView.add_note`'s,
applied to whatever that one widget is given — so `[restored 3 turns]`,
`[kept 12 turns, recalled 3]`, `[cancelled]` and a peer's `[⚡ jill online]`
cannot be spelled differently from each other, whether the window wrote them or a
plugin did.  What they have in common is the reason the shape is worth fixing:
they all say what the harness did rather than what anyone in the conversation
said.

**And there is no switch for this**, which is a decision rather than an omission.
A `context.rebuild: false` that grew the context append-only would configure a
system nobody runs — the step is not optional, since a context that grows
append-only is bounded by nothing (§9 still owes the trim) — and it would make
the turn's own note lie, because `kept 12, recalled 0` is a true sum describing a
decision no discriminator made.

**Relevance and time are different axes, and the axis decides the cut.** With a
query the candidates are ordered by relevance and the caps spend from that head,
skipping a turn too large to fit rather than stopping. With no query the axis is
*time*: `anchor` names the end the caps spend from, and the first candidate that
does not fit **ends** the selection — a period read from its end is "the last N
days", and skipping a large turn in the middle to reach a small one further back
would answer a question nobody asked. The similarity floor gates only a
*measured* similarity: a turn found by the keyword leg alone has not been
measured against anything, and "no number" is not evidence against an exact
match. The budget is the headroom below the **ceiling**, not the floor, because
the floor is where a live context already sits.

**Restore is the same replay at the other end.** When a conversation starts — a
restarted process, a conversation the idle sweep let go — the list is replayed
verbatim, in its own order, with no ceiling re-slicing. Each turn contributes a
copy of its first message carrying a `[TURN:{…}]` footnote naming its id, its
channel and its span, which is **how a turn id reaches the model at all** and
therefore how a keep-list is expressible. The footnote is added when a message
list is built and never stored, so it cannot drift from the row it describes —
and its name is in capitals, which by the convention of §1.2 is what says the
harness made it rather than the conversation.

**And there is a second place it is written, because a rebuild is not the only
way a turn enters the list.** The turn that has just run was appended by the loop
and is in no rebuilt list, so annotating only at build time leaves exactly the
newest turns unaddressable — the ones a keep-list would most often want, and
whose absence is silent. So the agent server writes the same footnote onto the
message that opened the turn once the turn is saved (`annotate_turn`) and the id
is known: after the save and never into it, and with the timestamps and channel
the row was written with, because the rebuilt spelling and the in-memory one have
to agree to the character or every rebuild costs a prompt-cache miss.

v1 spelled the envelope `[INFO: …]` and put two things in it: this footnote, on
the user message a turn opened with, and a trim note — *N oldest turns have been
removed from context* — on the **last assistant message**, written by the loop
after a cut and stripped before the save, so no stored row ever held one. This
build has no trim (§9), so the envelope has one occupant, and one occupant is
what lets it say what it holds: `TURN`. The other thing that changed is where the
footnote lives: v1's rode the stored user message, and this build's is derived
when a list is built — so a row holds the user's own words, plus the marker a
plugin put on them where one did, and no footnote.

**Both live behind one tool each, and neither is the model's.** A model may read
its history (`turn_list`, `turn_read`); it may not decide what its context is. It
says what it wants kept by what it writes, never by calling anything.

**The restoring read has two readers, and the second one is a screen.** The
context is put back by `send_message` — the first message under a key builds the
loop, and a loop that has just been built is the one moment the store is asked
what this conversation was made of. That leaves a terminal that has just opened
with the opposite question: the context is already right, and what is missing is
the *sight* of it. So `transcript` answers the same read with the stored turns
instead of the rebuilt message list, and the TUI draws them. It is the turns and
not the messages because the message list is the model's: when a line was said,
which turn it belongs to and where a turn's work ends are the record's facts, and
a window that had only the messages would render every restored conversation as
one undivided block stamped now.

It is a read, and it stays one. Opening a window must not start, end or rebuild
anything, so `transcript` touches neither the loops nor the live-context list —
which is also why it answers from the store rather than from the running loop: a
window opened against a server that has been up for a week shows the same
conversation as one opened against a server that has just started. The drawing
half is `slife2/tui/restore.py`, and it drives the transcript with the *live*
calls (`add_user`, `finish_assistant`, `add_tool_start`/`add_tool_end`) rather
than with a rendering vocabulary of its own — which is what makes a rebuilt
conversation and a live one the same thing rather than two renderings that
happen to agree.

**A turn that arrived over another channel wears it on screen**, and both of the
things that takes belong to the one call that draws the line. `add_user` is
given the turn's `channel` and asks `channel_prefix` for the bubble (`Wechat> `
for a WeChat turn, `You> ` for this window's own) and `slife2.plugins.wechat.server.
strip_marker` for the text — the marker is stored, so a rebuild reads it back out
of the row, and the two ids in it are what a *reply* needs rather than anything a
person watching can use (§1.2). One mapping, one unwrapping, and the person's own
channel is never unwrapped: what they typed is theirs, marker-shaped or not.

That required one thing of the record. The model's reasoning is part of what
happened — a transcript read back that has lost it has a hole exactly where the
reader was looking — so it is kept on the message, and it is the one key on that
message no provider is guaranteed to know. The way out is v1's: the neutral
message (`Message.to_wire`) is what *we* pass a conversation around in, and each
adapter builds its own provider request from it, so a field can be renamed or
dropped at the edge. `slife2.plugins.llm.openai_server` renames it to DeepSeek's
`reasoning_content` — including, deliberately, an *empty* one on assistant
messages that reported no reasoning, because a reasoner that has been asked to
think requires the field on every assistant message in the history and answers
400 when one is missing. The Anthropic and Responses adapters build their
requests field by field and simply never put it in.

One thing a rebuild still cannot show: **a tool's failure.** It is a fact the
loop knows only while it runs (`ToolCallFinished.ok`), where the record keeps the
text the tool answered with — and for most tools a failure is an ordinary
sentence. A restored panel therefore reads as done. That is an absence in the
record rather than a mistake in `slife2/tui/restore.py`, and it is worth closing
in the record rather than guessed at from the text.

**The real token count is not carried over, and that is a decision rather than a
gap.** What `slife2.libs.turn.tokens` *sizes* with is `tiktoken`'s `o200k_base` — v1's
choice — and what it falls back to, when the vocabulary cannot be fetched, is the
script-aware character measure it used to be alone. The reversal is worth stating
because the argument for the measure was written here first and it was wrong on
its own numbers: charging four characters per token for Latin text and one token
per CJK character came out about 25% high on English and about 60% high on
Chinese, which is a *bias* and not the tolerance it was described as — every
budget it fed was spending a window it did not need. A real BPE over the same
scripts errs small and in both directions, and the vocabulary is fetched once per
process rather than provisioned at install, so an installation that cannot reach
it degrades to exactly the old measure instead of failing.

None of this is exactness about the endpoint: a Qwen or DeepSeek model has its
own vocabulary and `o200k_base` is not it. Measured usage is untouched either
way — `context_tokens` is the provider's own count, and it is the number §5.1's
selection is not spending.

**The trim is the one thing still owed here**, and it is named in §9 rather than
half-built: with the rebuild on, it is a *guard* behind the selection rather
than the mechanism the context is normally held to.

**A missing server is a broken system, not a degraded one.** `slife2` starts
every plugin together and refuses to start at all if one of them will not come
up — before it draws anything, so the failure is two lines rather than a terminal
that can never connect. A peer that goes missing *later* takes the same answer:
the turn fails where the peer is used, rather than being answered and not
recorded.

That rule lives in one place, `slife2.libs.plugin.protocol.open_server` — connect, prove the
server is the one you meant, raise otherwise. It is there because it had four
implementations and one of them was its own opposite: the LLM backend and the TUI
each probed and raised, and the agent server's store client swallowed the
failure and latched itself off, which made the same situation fatal at startup
and silent a minute later.

One thing is deliberately not fatal: **a `ToolError` is not absence.** It means
the store answered and refused *this* request — an agent name that cannot
be a filename, say — which is one caller's problem rather than a sign that
anything is down.

Two things about the daemons themselves still need care:
- **A daemon outlives the build that started it.** Reuse is by identity — the
  launcher attaches to whatever answers to the name it expects — which is the
  right policy and has one sharp edge. Change what a tool *takes*, and a server
  from the previous build keeps the old signature, passes the name check, and
  refuses every request; the agent server reads that as one caller's bad data,
  logs it as a refusal, and goes on doing that every turn. The remedy is
  `slife2 down`, and it is worth knowing because nothing can notice on its own:
  the name is the only thing the probe compares. Two things close the gap if it
  ever bites twice — the probe could check the tool's parameters and not just
  its name, or the record's `version` (written by every server, read by nobody)
  could start being compared.

  This is no longer hypothetical. Renaming the agent server's tool from
  `run_turn` to `send_message` is the first live instance: a daemon left over from
  the previous build still advertises `slife2-agent`, so the identity check
  passes, and then every turn fails with "unknown tool" while the log calls it
  one caller's bad data. `slife2 down` is required after that upgrade, and the
  probe's expected tool name (`slife2.libs.host.launcher.AGENT_SERVER`) has to move in the
  same commit as the tool or the symptom looks like a bug in the new code.

  Note what the identity check changed here, because it moved this edge rather
  than removing it. It compares the *server's* name, so a build that renames a
  server is now refused by a daemon from the old one — which is louder than the
  old behaviour (a tool list that happened to still match) and shows up as a
  port conflict at startup rather than as a refusal on every turn. Louder is
  better, but it is still a `slife2 down`.

**`--agent` cannot become a path.** The name arrives from a command line and
becomes a filename, so anything outside a conservative set is replaced and a
name that reduces to nothing is refused: writing to a surprising path is a worse
failure than saying no.

**Text the database cannot encode is normalised, not dropped.** A failed write is
swallowed here, so content SQLite refuses does not fail the turn — it *loses* it,
leaving a warning in a log and no record. And the content is not exotic: a
provider's token stream is JSON, and CPython's `json` does not combine `\uXXXX`
pairs, so `json.loads('"😀"')` returns **two** surrogate characters.
Binding either one raises. So a valid pair — an emoji — is put back together, and
a genuinely lone surrogate becomes U+FFFD, on the UTF-16 codec, which is the one
in the standard library that tells those two cases apart. Encoding to UTF-8 with
`errors="replace"` is the tempting one-liner and is wrong: it turns the emoji
into `??` and a lone surrogate into a question mark nobody can tell from a typed
one.

**Every timestamp comes from one place.** `created_at`, `completed_at`, a daemon
record and a claim all want to be comparable, and each writer spelling the format
out is how that quietly stops being true — one `+00:00` or one microsecond among
local seconds-precision strings breaks both lexicographic ordering and any range
bound that falls inside the same second. So there is `slife2/helpers/clock.py` and one
format: local time with an offset, seconds precision, which is v1's convention
and the one a future port of its time-window queries will compare against.

**Reading is by time, and there are two ways to do it.** `turn_list` browses —
newest first, one line per turn, paged — and `turn_read` returns one turn whole.
Both are the *model's* tools, and both are the reason the store is a plugin the hub
asks rather than ours alone, and both are windows over `created_at` with the
grammar v1's `timeutil` implemented (ISO, `yesterday`, `last month`,
`3 days ago`), ported whole so a window means the same thing on both sides of
the schema. A bound in no known grammar is an error rather than an empty
result: SQLite answers an unrecognised string with no rows, and "no rows" is an
answer a caller believes.

**And a model reads only its own history, which is not something an argument can
say.** V1's store ran one process per agent, so the connection *was* the
identity. This one is shared — one process serves every client id, the way one
model server serves every provider — so the identity has to travel, and it
travels in the call's `_meta` rather than in its arguments (`slife2.libs.plugin.audience`).
The agent binds the conversation it is running for when it builds the loop, the
hub forwards what it was given without reading it, and the store answers about the
conversation the call came from. A model that could name an agent could read
somebody else's turns, and the only thing standing in the way would be a
sentence in its own system prompt — which is an instruction, not a boundary.

**Recall needs two indexes, and neither of them changes what was kept.** The
store finds a turn by keyword and by meaning, and both indexes are *derived*:
`turn_fts` holds the text a turn is found by and `turn_vec` the vectors of what
it was about, with `index_meta` recording the identity each was built with. Not
one column of `turn` changed to make room for them, which is why there is still
no migration layer — the tables are additions, and `CREATE TABLE IF NOT EXISTS`
is what additions need.

**And the second thing worth keeping is kept here, which is what this module's
own docstring said it would be.** `ToolStore` is the tool catalogue — v1's
`tools.db`, rows plus a keyword index and a vector index — in
`<data>/agents/tools.db`, beside the agents rather than inside one, for the same reason: the
store, the embedder, the text normalization and the vector index are already
here, and a catalogue needs all four. **It is opened here and served by nobody**, which is §1.1's test applied: a
store has one writer — this process — and one writer is what a SQLite lock
already is. What the hub does with it is `slife2.plugins.toolhub.server.Catalogue`, a thin face
over this, and §8.4 has the rest.

**One file for the tool catalogue and one per agent for the turns**, which is
not an inconsistency but the same rule read twice. `--agent` partitions what
belongs to a conversation; the tools are not anybody's — one hub serves every
conversation and cannot tell them apart — so what is installed, and what has
been loaded, belongs to the machine. It is also why a tool loaded in one
conversation is loaded for the next, and still loaded after a restart.

**Every row in the catalogue is derived from a server except one column.**
`load_status` is what the *model* decided, and it is the only thing in that file
that a server cannot be asked for again; everything else — the names, the
descriptions, the schemas, the two indexes — is rebuilt by listing a source. So
a stale file is reported and rebuilt rather than migrated, exactly as a stale
`turn` file is, and the loaded set is what the rebuild costs (a `tool_search`
and a `func_tool_load` per tool, which is why it is a cost and not a loss).

**A source the config no longer names has its rows deleted, and the pass that
does it is told every name the config carries.** Marking it `error` was one step
short of the conclusion — an `error` row is still found by a search, ranked by
the semantic leg and counted by the budget — so `ToolStore.reset` calls
`forget_source`: the rows and their vectors are gone, which is what makes
"removed" mean removed rather than hidden until next time. A source the config
still names is one the hub is about to ask about, and whether it answers is the
runtime's to say. So the set of names
that pass is handed is load-bearing — and it is not the plugins, which is the
only list `Config.plugins()` gives. The entries under `mcp:` and `rest-api:`,
and the two document sources, are named in the config too. Handed the plugins
alone, a restart read every one of them as a source that had gone, and the truth
came back only once the holding plugin had started, reached the entry and
declared it — tens of seconds, for a section of twenty `npx` servers — and in
between a `tool_search` called those tools unusable and `func_tool_load`
refused them as "its owner is not answering" about a server that answers. Every
row was then written back, so a restart of the real catalogue logged `0 added,
239 changed` about a config nobody had edited: the two halves of that are
`slife2.plugins.toolhub.server.configured_sources` and a family that does not stamp a verdict
of its own on a row (`slife2.libs.plugin.family.Held._row`). **And a store nobody told
is not a store told "none"**: `known` of `None` forgets nothing, which is the
difference between a catalogue built without the config and one whose config
names no sources — collapsing them deleted every `plugin`, `mcp` and `rest` row
in the file, and it is §10.1's first deformation wearing the boot pass's
clothes.

An index whose recorded identity no longer matches the configuration is dropped
and rebuilt rather than read, and that is one mechanism for all four things that
can invalidate one: a different normalization rule, a different embedding model,
a different vector width, a repointed endpoint. The cost is real and paid at
startup — changing the embedding model re-embeds every turn in every file,
because vectors from two models cannot be ranked against each other — and it is
the reason the sync runs before anything is served. A file this build cannot
bring up to date is *named*, with its reasons, rather than upgraded: the
doctrine above is that an old database is deleted rather than migrated, and
`TurnStore.index_status` is how that stays a decision instead of a surprise.
**The mechanism is one function** (`slife2.libs.store.index.rebuild_vector_index`), because
the same five statements in the same order are what all three corpora ask for —
and the order is the part that matters, since SQLite rolls a `CREATE` back and
not a `DROP`.

**The keyword leg is not a `LIKE` fallback, and the difference is the whole
reason `slife2.libs.store.textindex` exists.** FTS5's tokenizer sees a contiguous run of
Chinese as *one* token, so a two-character query is an exact-token lookup that
misses the word wherever it sits inside a longer run — measured on this
repository's own log, it matched only the turns where the word happened to sit
beside punctuation or a digit. The fix is to separate the characters before
indexing and to build the query the same way, which costs a normalization pass
and buys a leg that ranks. Terms are `AND`ed and never `OR`ed, because with
single-character tokens an `OR` matches any turn holding any one character.

**What a turn's vector is a vector *of* is the conversation, not the turn.**
The embedded text is what was asked, what was answered, and which tools were
called with what arguments — and *not* what the tools answered. Measured on
v1's live turn log, tool results were 56–99% of a turn's text, so an index built
on them describes "an agent ran tools" rather than what the turn was about:
every turn lands in one narrow cosine band and no threshold has anything left to
separate. The keyword index keeps the whole of it, summaries and tags included,
which is what those two columns are for.

**Embeddings are a hard dependency, not a feature flag.** A turn is written with
its vector in one transaction, so a save that cannot embed stores nothing rather
than storing a turn that semantic search can never find — and the failure is
visible where it happens instead of as a hole nobody can see. That is also why
there is no degradation path, no gate and no background drainer: with the write
path atomic and the model mandatory, "the index is not ready" is not a state the
system can be in, so nothing has to manage it. v1's answer to the same problem
was an embedder lifecycle, a binary gate and an event-driven drainer, and its
write path was deliberately kept on the other side of all three.

### 5.2 A result that does not fit, and the second discriminator

**The harness may not judge whether a tool output is the right length.**  That is
the tool designer's business, and a rule of ours that decided which part of an
output "mattered" would be this system guessing at a job nobody gave it.  What
*is* ours is two things, and they are answered separately: **the burden on a
context**, which is arithmetic — a window is so many tokens — and **the
completeness of what is kept**, which on the record side now has no exceptions at
all (`db.append`'s docstring is where the old digest went and why).

**So the record keeps everything and the conversation gets a window.**  A result
over `tool_result.tokens` is stored whole as a `dump` — a cabinet document of its
own kind (§1.4), keyed by the **tool call id** — and what the conversation
carries is a selection of that output's own lines.  An identifier both sides
already hold is the whole of the association with the turn: the messages carry
the call id and the dump is keyed by it, so there is no column to write at save
time, nothing to keep in step, and — the constraint that decided it — nothing
that has to exist *before* the turn is saved.  A turn id does not.

**The selection is made by a discriminator, and it is the second one.**  §5.1's
decides which turns a conversation runs on; this one decides which lines of one
result join it.  Both are one model call whose reply surface is **rendered into
the instruction** (`templates/recall.j2`, `templates/compact.j2`) and parsed
strictly into a shape the harness acts on, and what they share is what makes
either safe — which is not that a model judges well:

* **the answer is an address, never a text.**  §5.1's is a list of turn ids; this
  one is a list of line ranges.  The harness cuts the original at the numbers the
  reply names, so the text the conversation carries is the tool's own words or it
  is nothing, and a bad selection costs a re-read rather than a fact no tool
  said;
* **an unreadable reply is not a decision to do nothing** (`_Rejected` against
  `None`, §5.1's split), and every failure ends in the conversation's own
  fallback rather than in a retry;
* **the ordinary case costs nothing**: a result under the window is never
  handed to it.

**And the judgement is made under the conversation.**  The result, the turns in
hand and the current input are the call's whole input, because what a result is
worth is what *this* conversation is doing — a follow-up's subject is usually in
the turns above rather than in the input.  A result that is not text at all takes
the other branch: there are no lines to choose, so it is kept whole, stubbed in
the conversation, and nothing reads it back.  That judgement is the model's too,
and the harness writes it down as the row's mime rather than sniffing a payload
itself.

**What is windowed out stays readable, and that half is built.**  `dump_read`
reads a dump back by *finding* rather than by scrolling: a literal pattern
chooses lines and `context` brings their neighbours, merged into one block so
nothing is said twice; `offset` reads from a line, negative from the end; `limit`
reads fewer.  Every answer is numbered, matches are marked, and **every answer is
within `tool_result.tokens` — the same number the shortening spends.**  That is
the whole point of the number being one: a reader that could hand back more than
the shortening removed would have its *own* answer shortened next, which is this
system's machinery feeding itself.  So the bound is enforced once, where the
answer leaves the tool, rather than argued for at each return — and it therefore
holds for the answers built out of what the caller asked as well.  A `pattern`
with a megabyte in it is clipped rather than echoed; that was a real hole, found
by asking what happens when the thing being bounded is the argument.

**Why the pattern is text and not an expression.**  A regex over a 240,000-line
output is a program whose runtime nobody bounds, running in a server every agent
shares; the code-reading tools that take one take it because their engines cannot
backtrack, which Python's can (the cabinet's missing `grep` leg, §9, is the same
argument one level up).  A literal substring is what the common case wants — a
symbol, a path, a spelling remembered approximately — and a model that needs a
real regex has the file, which every answer names.

**What it costs, measured.**  One model call per oversized result, whose *input*
is the largest thing in a turn — the result plus the conversation it is judged
under — and how often that happens is the number the whole mechanism's price
turns on.  Read off this machine's turn log on 2026-10-15: **181 turns, 347 tool
results, median 48 tokens, p90 1,556, largest 5,284**.  Against the shipped
window of 4,000 that is **7 results, 2.0%** over it (17% are over 1,000 and
nothing at all is over 8,000), so the mechanism fires about once in fifty calls
and the call it fires is worth paying for: the alternative is that result carried
by every request the conversation makes afterwards.  It is one machine's log
rather than a corpus, and it is the number that sizes `tool_result.tokens` — the
instrument is the rows themselves, so re-reading it is a query (§8.5 owes the
same observation about the tool list's threshold).

A file per dump that nothing deletes, which is the honest price of "kept whole".
And a dump is the one kind nobody wrote, so a search that names no kind skips it:
machine output is not ranked against a person's own notes, and the address a
model has for a dump is the marker in the transcript rather than a search.

**Both halves are built, and each is tested where it binds.**  The cabinet's: the
kind, the reader, the config key, and an e2e that drives the reader as a real
process over a real socket with the window read from the config file.  The
harness's: the seam in the loop (`AgentLoop._fit`, a callback the server
injects), the choice and its rendering (`libs/harness/dumps.py`), and the calls —
which run over four tests in `tests/plugins/agent/test_compact.py`, one per
outcome: a window over a huge result, the ordinary result left alone, an
unreadable reply falling back to the reader's page, and a cabinet that cannot
answer failing the turn.

**Two gaps are real and both are named rather than half-solved** (§9): the
discriminator's own input can exceed the model's window (the result *and* the
conversation are handed to one call), and a model's *arguments* are not bounded
at all — this mechanism is about what a tool returns, and a model that writes a
hundred thousand characters into an argument has put them in the conversation
itself.

## 6. Compatibility notes

Four things about the 2026-07-28 revision that the code depends on, all
verified by running against the installed library rather than by reading:

- **`ping` is gone, and nothing replaced it.** The protocol-level ping was
  removed, and a conforming server answers it with `MCPError: Method not found`
  (measured). What matters as much is the second half: **the spec names no
  replacement liveness probe.** An earlier draft of this file called
  `tools/list` "the migration guidance's recommended" replacement; no official
  source says that, and it is corrected here rather than left to be re-derived
  the next time somebody reads it.

  What the handshake gives for free is *identity*, which is the question both
  probe sites were really asking. The client performs `server/discover` as part
  of connecting — a method every 2026-07-28 server MUST implement — and that
  result carries the name the server was built with. So a probe reads
  `Client.server_info` and compares `name`, falling back to a `tools/list`
  membership check only when a server reports none. (A client pinned to an
  exact protocol version gets a synthesized identity with an empty name, so the
  fallback is load-bearing rather than theoretical.) Both halves catch being
  pointed at a *different* MCP server, which a bare connection test waves
  through and which would otherwise fail halfway through a turn; the name is
  exact, and costs no round trip. See `slife2.libs.plugin.protocol.identifies`.
- **There are no sessions, and there is no handshake to negotiate one in.**
  SEP-2567 and SEP-2575 removed `Mcp-Session-Id` and `initialize` from
  Streamable HTTP; a request carries its version and capabilities in `_meta`,
  and a server must ignore a session header if one arrives. This is the decision
  §3 is built on, and it is listed here because it is the same kind of fact as
  the two around it: a property of the revision that the code depends on, and
  one that would be re-derived wrongly from an older memory of MCP.
- **A change notification is opt-in now, and nothing here can opt in.** The
  old HTTP GET endpoint and `resources/subscribe` were replaced by
  `subscriptions/listen`, a single long-lived POST-response stream, so
  `notifications/tools/list_changed` reaches only a client that has opened one.
  Measured against FastMCP 4.0.11: a client that has *not* opened one receives
  nothing when the server adds a tool, `Client` exposes no call that would open
  one, and a FastMCP **server** advertises `tools.listChanged: false` and
  answers the listen request with `Method not found` — so our own plugins never
  send one either. The consequence for §8 is that the peer's notification could
  no longer be the only thing a tool list stays true by, and
  `Connection` re-reads an aged listing instead. The same absence is why **an
  answer rides the request that asked for it**: with no push channel there is
  nowhere to tell a caller later, which is why a queued caller waits on its own
  call (§3) and why that wait is the thing the inbox is capped for.

- **`stream_options` and usage.** OpenAI sends token counts on a trailing chunk
  with an empty `choices` list. DeepSeek attaches them to the final chunk that
  *still carries a choice*. An adapter that handles only the documented OpenAI
  shape reports zero tokens against a real DeepSeek endpoint — which is exactly
  what happened here, and what a live call caught after the unit tests, built
  from synthetic OpenAI-shaped chunks, had all passed.

## 7. The agent loop

`loop.py` is a pure function over a message list. It does not own the
conversation, know what MCP is, know which provider answered, or know whether
anyone is watching.

The server owns the conversation and the loop still does not, and that is not a
contradiction worth smoothing over: **the state is the server's, the algorithm
is the loop's.** `AgentLoop.run_turn` is handed a list it mutates in place, which
is exactly what it did when the caller owned that list. The inbox is a lock in
the server rather than a queue inside the loop, so nothing about the step
machinery below changed when the state moved in.

```
append the user message
for step in 1..max_steps:                     ← 0 = no cap: until the model stops
    Phase A: exhaust the model stream, forwarding deltas       ← no tool runs here
    if no tool calls: append the assistant message, done
    Phase B: run each call, and fit its result                 ← the fit can raise
    append the message and every one of its results at once    ← no await inside
```

**The step cap is opt-in.** `max_steps: 0` — and an absent key, which the config
reads as the same 0 — is no cap at all: the turn ends when the model stops
asking for tools, and a turn that never stops is a turn the operator asked for
by running an agent with nobody watching the bill. A number nobody wrote is a
bound nobody chose, and inventing one is how a turn comes to be cut off at
sixteen for no reason anybody can point at. Where a cap does exist it produces
an ordinary result (`stop_reason: "max_steps"`), not an exception.

**And a turn says which step it is on, on the screen.** Each tool panel carries
v1's counter — `(3/30)` while the turn has a cap, a bare `(3)` while it does
not, and nothing at all when the step is unknown — which is why
`ToolCallStarted` carries both numbers: a closed model API answers in one round
trip and a long turn is the only reason to care, so the panel is where somebody
watching sees it advancing rather than a spinner that says nothing. A rebuild
from the turn log draws no counter, because the log keeps no such column;
`restore` is not guessing one.

**A panel's only ending is an event, so every ending settles what it left open.**
`ToolCallFinished` is what completes a panel — it is the only caller of
`ToolCallWidget.set_complete` — and the ways a turn stops without one are
cancelled, failed, and a transport that died mid-call. All three used to leave the
panel saying `running` for the rest of the session, which is not an edge case: it
is what pressing escape during a slow call does. Nothing closed it because nothing
could — `add_tool_start` is guarded by no ticket (the cancel path retires the
turn's ticket for exactly that reason), and the panel had already been mounted by
the time the turn ended. So `ChatView.settle_tools` runs from the one place every
ending passes through, the turn's `finally` rather than the two branches that
happen to exist today, and the failure path retires its ticket as the cancel path
does, because a turn that is over must not mount anything either.

What it settles *to* is a fourth state rather than `error`: the window never
learned how the call ended, and a red ✗ would be this display inventing a verdict
about a tool that may have run to completion at the far end. `interrupted` says
the one thing that is known — the turn ended first — and it is drawn in the dim
colour with a note in the expanded panel. A state that is a claim about *now* is
the one thing a status may not leave standing after now has passed, which is the
same rule the presence line is held to (sdd §9.2's two states, and the reasons
that live in the status tool).

**What the loop is *not* shown is everything around the turn.** The context is
decided before `run_turn` is called and the turn is recorded after it returns,
and both are the *agent server's* — the loop is handed a list and asked to
advance it, which is the same contract as when the caller owned that list. The
one thing that crossed that line is the *carried tail*: a cancelled turn leaves
the user's own message in the list with no row behind it, so the server tracks
how many messages the live-context list accounts for and tells the rebuild how
many it does not. A library did not have to know that; a server that decides what
to keep does.

Three load-bearing details:

- **Phase A strictly precedes Phase B.** Running a tool inside the `async for`
  would hold the provider's response stream open across the call, risking its
  read deadline and pinning a socket for nothing.
- **Every tool failure is a message, not an exception.** Unknown name, bad
  arguments, a tool that throws — all become error text handed back to the
  model, which can read it and correct itself. The error path is a feedback
  channel, not a failure mode.
- **A batch is written at once, and only once it has all been answered.** The
  assistant message carries *every* call of the step, and a provider rejects a
  history in which one of them is unanswered — so the message and its results
  are appended together, with no `await` between them. This was written the
  other way round first, on the argument that nothing in Phase B can raise; the
  argument is false, and by one step. *Running* a call cannot raise (the
  registry turns a failure into text), but **fitting** its result is the
  caller's own callback — a cabinet that will not open, a discriminator that
  stalled — and appending the message before the fitting left exactly the
  rejected history behind. Nothing repaired it afterwards: a failed turn keeps
  its messages, so that conversation failed every later turn until it was
  rebuilt. Fitting against the list *before* the step's answer is also the
  honest list to judge a result by, since it no longer holds a half-written
  batch. `sdd §12.5` is where this arrived from.
- **The observer is one method on a tagged union**, not v1's eight-method
  handler. Adding an event to a union breaks only the observers that want
  exhaustiveness; adding a method breaks every observer in the codebase. A
  broken observer cannot end a turn — the loop swallows its exceptions, but
  deliberately lets `CancelledError` through.

### 7.1 The clocks

**Every await that can block has a bound, and it belongs to the layer that
awaits it.** A callee never sets a total for its caller: this is v1's
owner-of-await rule, and it is why `slife2.clients.gateway` holds no clock of its own
and why `slife2.libs.plugin.protocol`'s transport delegates read and write (v1's registry
spelled that line `read: None # DELEGATED — owned by the loop's tool budget`). A
bound at a lower layer applies to *every* message that crosses it, so a number
meant for one thing becomes the deadline for whatever the far end happens to be
doing — and a tool that takes twenty minutes is a tool doing its job, not a link
to give up on.

**The only total in this system is one somebody stated.** There is no turn
deadline, and **no default tool-call budget** — v1 had one (`work.tool_budget`,
120 s) and v2 does not, because a single number applied to every tool is wrong
for most of them: a search and a render are both tools. What bounds a call is the
model's `_timeout` or the tool's own declared parameter, and otherwise nothing.
Where a clock *is* needed and the work can legitimately be long, it is an
**inactivity** bound rather than a wall clock: the model stream has one (120 s of
silence, reset by every chunk), and the registry's comment says why the
difference is the whole of that number.

**One file is the single source of truth for the numbers**, `slife2/helpers/timeouts.py`,
with roles that say what kind of value each one is: *budgets* bound an await,
*cadences* say how often something runs. Consumers read them at call time
(`_T.timeouts.ready.probe`), never into a module constant or a def-time default,
so a test can patch one and so nothing drifts from the value it was measured
against. `validate()` asserts the relations that go wrong silently — an outer
bound shorter than an inner one is an inner bound that never fires — and fails at
import rather than in production. The relations themselves are argued where they
are declared: the hub's patience must outlast the hub's own settle wait, the
hand-over to the agent server (a whole turn) must outlast the one to a window (a
write), a tunnel's heal window must outlast a read of the URL it published.

**And a gate keeps it that way** (`tests/test_no_magic_timeouts.py`, ported from
v1): a time-style literal anywhere in `slife2/` or `tests/` fails CI unless it is
in the registry or carries a `# timeouts:` note saying why it stays. What the
gate cannot check is which of two numbers should be bigger; that is what the
invariants are for.

**It enforces the second half of the rule too, and that half was missing.** "Read
at call time" is broken by a registry lookup in a *signature* exactly as it is by
a literal — `def exclusive(*, timeout: float = _T.timeouts.grace.lock)` reads the
value once, at import, and a patch to the registry then changes nothing about
any call that took the default. The gate caught only literals, so three of these
were live at once (`runtime.exclusive`, `runtime.start_lock`, `a2a.broker.ensure`)
— each sitting beside a docstring that stated the rule it broke, which is how a
miss of this shape hides: a lookup in a signature *looks* like the convention it
violates. The gate now fails on either spelling, so the rule has one form in the
file and one form in the check.

## 8. The toolhub

**Where the model's tools come from, and the one process that decides it.** It
is a port of v1's `mcp-gateway` with one thing moved out of it, and the shape
that survived the port is the whole of the rest:

```
slife2-agent  ──MCP──▶  slife2-toolhub  ──MCP──▶  plugins      (ours; `context`, `skills-server`, …)
                          list_tools        └──▶  what they declare   (their servers, their rows,
                          call_tool                                   and the families that are
                          servers                                      not servers at all)
                             │
                             └── in-process ──▶ `slife2.libs.store.catalogue.ToolStore`  (the catalogue's own file)
```

### 8.1 What the hub owns, and how a tool gets into the set

**What the hub owns is the set, and it owns all of it.** Which tools exist, what
the model is holding, what a name resolves to, what the budget takes back, and
who may call what — every one of those is a question about the *whole* list, so
it is answered in one place or it is answered twice and differently.  **One set
per home** (§4): the configuration a set is built from is a home's, so the
catalogue, the declarations behind it and the embedder that places its rows are
held per home and built the first time that home asks.  The addresses the hub
*connects to* are not — they are the shared servers, one set of ports for the
machine — which is why the split is not "everything per home" but "everything
derived from a config section". What the hub
does not own any more is a *connection*: the servers under `mcp:` and
`rest-api:` are held by the two plugins named after those sections, and the hub
is told about them. `slife2.clients.gateway` is the link itself — connect, list, call,
say whether it is answering — and it knows no catalogue, no category and no
config section, which is what lets one implementation serve the hub and both
families.

**Two ways in, and they are not the same kind of claim.** A *plugin's own tools*
arrive by `tools/list` and have to declare themselves the model's
(`slife2.libs.plugin.audience`), because they belong to that plugin's code — `remember`
writes into a conversation's log and `send_message` drives another conversation,
and those are exactly the tools a model would reach for if it could read their
descriptions. What a plugin *holds* arrives by declaration instead, and is not
gated, because it is the operator's configuration: a `mcp:` entry, a playbook,
a command. The entry is the opt-in; there was never a mark to forget. So the
default is still the safe half — a forgotten mark costs a tool that is absent,
not a tool that is dangerous — and the rule that keeps the two apart is that a
declaration may not use the category `plugin`, which means *the servers slife2
starts*, the one the gate decides about.

**And the agent server's own tools are where the model config lives, which is
worth saying because it is not a family plugin.** `_check_new_input` is declared
here already, for the reason its own docstring gives; the four `model_*` tools
are the newer case. `providers:` and the top-level `default:` are what this
server turns a conversation's model into — it resolves the reference, picks the
backend for the protocol, and reads the model's own settings — so the tools that
edit them belong to that process rather than to one of their own: the edit and
the read that honours it are one process, which is what makes a switch live with
no reload protocol between two of them. See §8.7.

**Which makes a source something a plugin owns rather than something the hub
holds.** A source — `arxiv`, `skills`, `cli` — is a name, a category, its rows,
and two facts about it: whether the operator switched it off and whether it is
answering. The hub merges the rows, gates them by category, counts them and
routes by them, exactly as it did when it held the connection; what changed is
that it asks who holds one rather than being the one who does. **The price is
freshness**, and it is the price of anything over a wire: the answer is as old as
the last declaration, which is why declarations are refreshed wherever liveness
is read and why a plugin that holds sources and cannot answer for them fails the
list rather than quietly contributing none.

**A call can say who it is on behalf of, and the hub passes that on without
reading it.** The store is the case that needs it: the model may browse its own
history and must not browse anybody else's, and one store serves every
conversation in the system. So the conversation rides in the call's `_meta`
rather than in its arguments — off the schema the model reads, out of reach of a
prompt that asks for somebody else's turns — and the hub, which cannot act on it
and could not use it, forwards exactly that key and nothing else of `_meta` (the
protocol's own keys name *this* request's progress stream, and a proxy has no
business passing those on). §5 has the rest.

**The hub's API is the agent's and never the model's.** Like the store's
`remember`, the model never sees `list_tools`, `call_tool` or
`servers`; it sees the tools themselves, by the names below. That
indirection is what keeps the hub's surface constant: a server coming and going
changes what the model may call without changing anything about the hub's own
protocol. `tool_search`, `func_tool_load` and `_func_tool_unload` are the three
tools this process serves *to a model*, and they are not part of that API — they
are a source of tools like any other, which is why they appear in the list and
not in the protocol. They are what is left when everything with a server behind
it moved out, and what is left is exactly the set-level work: which tools exist,
what the model is holding, and what the budget takes back are questions about the
whole catalogue, so the process that owns the set answers them and no plugin can.
The third is the odd one and has both: the model gets it as a tool, and the
harness gets it on the API, because the trim is the one thing here that is
*recorded* — see the budget, below.

**A name carries a server only where it has to, and for ours it never does.**
`turn_read` and `tool_search` are slife2's tools, and `context__turn_read` would
be the system's own arrangement leaking into the one thing the model reads on
every request: a plugin is a process slife2 starts, which is a fact about us and
not about the tool — the model choosing `turn_read` has no use for it, and
`servers()` reports it to the person who does. What the prefix is *for*
is somebody else's tools, where the operator may write down four servers that
each offer a `search`: `arxiv__search` against `serper__search` is the
difference between reaching the tool the model read about and reaching a
stranger. So the rule is `model_name`'s — the bare name for ours,
`{server}__{tool}` for the rest — and it is stated once, for the row and the
listing both. Its cost is that ours are one namespace: two plugins cannot offer
one name between them, and the catalogue refuses the second loudly (`merge`)
rather than letting a name mean two things.

**Nothing with a server behind it is served by the hub process.** Every plugin's
tools come up through a connection and reach the model by the same road a
`mcp:` entry's do — `skill_use` and `arxiv__search` alike. The alternative, a
hub that served one of them itself, is the second mechanism this whole
arrangement exists to avoid: that tool would not be in `servers()`, it would not
have a connection that can fail, it would not be in whatever a tool search is
eventually built on, and the first thing to drift would be the one place the
tool table has a branch in it. What the hop costs is
one loopback call per model call; what it buys is that "where the tools come
from" has one answer and no exceptions.

### 8.2 Sources: what a plugin holds, and how it declares it

**Two families are not tools, and they got servers of their own.** A skill is a
document in `<data>/skills/` and a `cli:` entry is a program already installed:
neither is anything a call could reach, and for a while that was the argument for
keeping them out of a process — the hub read the folder itself and mirrored the
config's own section. That was defensible while the families were two
row-builders and one `read_text`, and it stopped being defensible for two
reasons. The first is that it put the hub in charge of two config sections that
belong to somebody else's job, which is not the job the hub is the only one who
can do. The second is what was next: both families were going to have
**model-facing tools** — and they have them now, `skill_list`, `skill_use` and
the `skill_*` and `cli_*` sets that edit the two sections, with one tool per
`cli:` entry that *runs* a command still to come (an argv rather than a shell,
and §9). A tool needs a server to be served from, which is why these plugins
existed before their tools did. So `slife2-skills` and `slife2-cli` are plugins
like any other, each owning its section, and the rule that survives is the one
worth having: **everything with a server behind it goes through the one code
path**, and a source that owns rows which are not tools *declares* them.

**A plugin that declares nothing is asked for nothing, and that is read off the
listing rather than configured.** `_declares` is one question — is `list_sources`
among the tools this server offers — so `slife2-wechat`, which holds a login and
no rows, is not a source the hub has to remember to skip: it is a server that
never said it was one (§1.2). A flag in the config saying "this one has no rows"
would be the second registry that §8.1 exists to avoid, and the first thing to
disagree with the tools the server actually serves.

**A source declares rows; the hub merges them, and the hub is still the only
writer.** The declaring tool is `list_sources`, unmarked and so invisible to the
model — a plugin's *other* tools are its own, and these families have two kinds:
what the hub calls (`list_sources`, `call_source`) and the `*_set` / `*_remove` /
`*_list` names the model calls, which are what the paragraph above describes.
The declaring one answers with the source's whole list, and the hub merges it
exactly as it merges the tools a `tools/list` returned:

* **The whole list, so a deleted skill stops being a hit.** A merge reads an
  absent name as a row the source no longer has, which is the half that makes
  removing a directory the whole of uninstalling one.
* **Asked again before every search**, because the folder is the install: a
  skill dropped in must not be readable and unfindable at the same time. A
  declaration of an unchanged folder plans no writes, and a source that is not
  answering is skipped rather than waited for — the rows it declared last time
  are still in the catalogue, so a late plugin costs freshness and nothing else.
* **Only one of ours may declare, and only into the categories nothing connects
  to.** A declared row is merged *without* passing the audience gate — that is
  what declaring is — so the permission has to come from somewhere else, and it
  comes from the two facts that already mean "ours": `required` on the upstream,
  which is what a plugin is, and a closed pair of categories (`skill`, `cli`),
  because a source able to name its own category could offer the model
  `remember`.
* **The rows are filed under a source that is not the server's own name.** A
  source's *verdict* is written across every row it owns, so a plugin holding
  both its own tool and its documents would mark every playbook broken whenever
  its process faltered — and a document row has no connection a verdict could
  come from. `skills-server` serves; `skills` is what its rows are filed under.

What is left of v1's `sync_category`, and each piece is load-bearing:

* **The row name is namespaced** — `skill:browser-harness`, `cli:yt-dlp` — and
  the collision is not hypothetical: this config has `browser-harness` as a
  command *and* as the skill documenting it. A name is a row's identity, so two
  families cannot share one, and the prefix also tells the reader which of the
  two a hit is.
* **A skill's schema is the whole document**, which is what makes "drive a
  browser" reach the playbook: the semantic leg ranks the text, and a playbook
  *is* its documentation. `cli:` rows carry the invocation and the `install`
  line for the same reason.
* **The declaring plugin writes the status** — a `cli:` entry is `disabled` when
  the config says so, an unreadable `SKILL.md` is `error` — because there is no
  connection whose state a verdict could come from. That is the one family where
  a merge may write `status`, and the one family a re-merge must *not* re-enable:
  the rows are declared again before every search, so "a source that answered is
  enabled again" would flip a switched-off command back on several times a
  minute.

Neither family has a load state (`n/a`), and that is also what keeps them out of
the model's list — the gate is the function categories — so **findable and
callable stay two different things**. The model will still call one, because a
search result is an invitation to call the name in it; the answer says what to
do instead (`skill_use` for a playbook, and for a command the truth that nothing
runs one yet). Those sentences are the hub's, keyed on the row's *category* —
which is the catalogue's vocabulary rather than knowledge of the family, and cheaper
than a per-source template the hub would have to hold and keep.

**A credential is held by the process that needs it, and that is a plugin.**
baidu-search's header declares `BAIDU_API_KEY`, and the playbook's first
instruction runs a script that dies without it — so a skill that "needs nothing"
was the wrong thing to say, and the first version of this paragraph said it.
What that paragraph got wrong was the conclusion: it argued that a credential is
not a server, which is true, and that a skill is therefore not a server's
business, which does not follow. A declared key needs *one process that knows
the answer*, and the resolution is the config's own secret chain (`skills:` in
`slife2.yaml`, shell then credstore). That process is now `slife2-skills`, which
holds what the chain resolved for exactly the reason `slife2-mcp-tools` holds a
tool server's headers and nothing else does: one process knows, and nothing
about a declared key needs an address or a protocol. `skill_use` reads the declaration
rather than ignoring it, so a model told which key is missing — before it acts —
is the difference between a skill that does not work and a skill that does not
work silently.

**REST APIs are not a second mechanism, and `restapi-tools` is not a second
implementation.** A `rest-api:` entry is expanded *by the config layer* into the
stdio command that serves it — `uvx mcp-openapi-proxy` with the environment it
reads — so what reaches `slife2-restapi-tools` is an ordinary stdio server and
nothing outside the config layer knows that REST exists. The two family plugins
are the same code over two sections, which is the honest consequence: what is
REST-specific is the *entry* — a spec, a base URL, a key — and the expansion that
turns it into a command, and both of those are the config layer's. That wrapper
is v1's, kept because it is what the ecosystem publishes and because writing an
OpenAPI-to-tools converter here would be a large feature that is wrong in
interesting ways. Splitting the two sections into two plugins anyway is about
ownership rather than mechanism: each is a place an operator writes a server
down, and a family that owns its section is one whose next change has somewhere
to land.

### 8.3 The list, and what says when something is missing

**A tool list is one thing and it has one owner.** Provenance (whose tool is
this), the naming rule that keeps two servers' `search` apart, and — the first
time it appears — which tools may run without asking, are all questions about
the *set*, and a set assembled in two places disagrees with itself. That is why
the agent holds no registry of its own and why a plugin's own tools are not
exempt from it.

**The list is read before every model call, and that is what keeps it in sync.**
The tool list goes out *with* each request, so it is asked for with each request:
`AgentLoop` is handed a coroutine (`refresh=`) and calls it at the top of every
step, and step 1's list is also what a missing hub fails on, before the
conversation has been touched. A tool a server grew, or a server that just
finished starting, is in the next *call*'s list rather than the next turn's. v1
needed a `tools/list_changed` subscription, a shared catalog and a reconcile
pass to arrive at the same place; here the answer can be at most one call old,
and nothing has to be kept in step to make that true. The loop still does not
know what a tool server is — it is handed a coroutine that returns a registry.

**Health is a tool list, not a connection** — v1's rule, and it is most of what
`Upstream` does. A server is either usable, meaning its tool list is in hand, or
it is not, and in the second case the useful fact is what it said the last time
we asked. There is no connection state machine: the listing goes to the
catalogue when it arrives, the source stops counting as *live* when a call fails
at the transport or when a connect fails, and the next ask re-lists it. **What
nobody holds any more is the tool list itself** — that is a row now, and the
gateway's `_ready` is a flag saying whose rows may be injected
(`Connection.usable`). "The snapshot is dropped" survives as that flag, and it
is the same flag whether the connection is the hub's to a plugin or a family's
to somebody else's server.

**What a person reads when a tool is missing is `servers()`, and it answers
about sources rather than about links.** Three of its words describe a
connection — `ready`, `connecting`, `failed` — and two are outside one: `idle`
for a source nobody has asked yet, and `off` for an entry the operator switched
off, which is the commonest reason a *configured* server's tools are absent and
the one answer a report that simply left the row out could not give. Its two
counts are the distinction everything above rests on: `tools` is what the source
last offered, which is a row on disk, while `loaded` is what the model is
*holding* — and a load outlives the connection, so the second is taken against
the live set (`ToolStore.source_counts`) or a server that has stopped answering
would be reported as one whose tools the model still has.  A source nobody has
counted answers **`not counted`** rather than `0`: a zero about a fact this
process does not have is a verdict, and §8.3 is the answer a person reads when a
tool is missing.

**A listing also ages, because the change notification no longer arrives.** At
2026-07-28 change notifications moved to `subscriptions/listen` — a stream the
client must open — and against FastMCP 4.0.11 there is no way to open one: no
such call on `Client`, and a FastMCP *server* advertises `tools.listChanged:
false` and answers listen with `Method not found`, so our own plugins never send
one either. So `Connection` re-reads a listing older than `RELIST_AFTER_SECONDS`
on the next ask, and the notification — where a legacy peer still sends one — is
the fast path *over* that bound rather than the only path. **The age costs a round trip and never a tool**: a stale listing stays
usable and stays in the model's list while it is replaced, and it is replaced
inside the same ask that noticed, because `list_tools` already waits briefly for
work it started.

**Two failures that look alike and are not.** The hub distinguishes a *transport*
failure from a peer's *refusal*, and does opposite things with them. A refusal —
an unknown tool, bad arguments, a permission it will not grant — is a value the
model reads and acts on, and rebuilding the link would only be told the same
thing again. **A transport failure is not retried at all**, and this reverses
what this paragraph used to say. The request *was* handed to the SDK, so it may
have reached the peer — and the SDK raises one code (`CONNECTION_CLOSED`) both
for a send onto a link that was already gone and for every request that was in
flight when the link went, so no failure carries the claim "nothing was asked".
Reading it as that claim is what ran a model's file write, render or message a
second time. What is retried is the one case this process can prove on its own:
a connection with **no client behind it**, where there was nothing to ask with —
and that is a first attempt rather than a second one. Nothing is left broken by
it: the link a failed call dropped is rebuilt by the *next* ask, at the cost of
one call answered with the failure. FastMCP makes the refusal/transport split
visible for free — `call_tool(..., raise_on_error=False)` returns `is_error`
where the raising form throws — which is one of the reasons this port is a few
hundred lines where v1's was three thousand.

**Three kinds of missing, and only one of them is ours.** A missing *hub* is a
plugin gone and fails the turn, like the store. A missing *source* — a plugin
that is not answering, a `mcp:` entry whose process will not start — is not the
same kind of thing, and is said in its own words rather than reported as "that
tool server is broken". A missing *upstream* is the operator's configuration and
somebody else's process: reported by `servers()`, its rows left in the catalogue
with `error` on them, and retried on the next ask. An upstream *refusing a call*
is one caller's bad data. Collapsing these is how a config mistake becomes an
outage, and separating them is most of what the module's prose is about.

### 8.4 The catalogue, and the search over it

**The catalogue is a file this process opens, not a plugin it asks** — and it is
**the calling home's** file (§4): `agents/tools.db` inside the directory the call
came from, so two homes on one machine have two catalogues, each built from the
config that home runs on, and a `func_tool_load` in one writes into that one.  It was
the second half of `slife2-db` and that plugin is gone, for the test in §1.1: a
store has one writer — this process — and "one writer" is what a SQLite lock
already is. So `Catalogue` in `slife2.plugins.toolhub.server` is a thin face over
`slife2.libs.store.catalogue.ToolStore` — the catalogue was `slife2.libs.store.db`'s until the two
halves of that module turned out to share nothing but a logger — opened on first
use with its indexes brought up to date,
and what it replaces is a hop, a kept MCP client, a reconnect path and a
`CatalogueUnavailable` — the last of which was a class for a failure that can no
longer happen. What is left of the old split is the one that mattered and is not
about processes at all: **the hub decides, the record remembers.** A refusal is
the record saying no to *this* data (`_refused`, a `ValueError`), which is the
calling source's own problem; anything else is the catalogue itself failing to
work, which fails the tool list rather than quietly shortening it.

**And a home's catalogue holds the *machine's* tools, not only its own
sections.** The one thing the per-home split got wrong, and it was wrong twice
over. `upstreams` are one set of links for every home — stated, deliberately, as
what makes a second home cost no second stack — and yet a plugin's listing was
merged into whichever home happened to be in front of the hub when the link came
up (`Upstream._listed`), and the hub's own four rows once, at start-up, into the
starting home (`start`). A second home therefore opened with its *declarations*
and nothing else, which is not a smaller tool set but a broken one: measured
against the real plugin set, twenty-four of twenty-eight tools missing, the hub's
own `tool_search` and `func_tool_load` among them — so the model could neither
search for a tool nor load one.

What made it more than a slow start is the *timing* of a re-list.
`Connection.connecting` does nothing while the listing is younger than
`timeouts.pacing.relist_after` (five minutes), so a second home's ask cannot
repair this by waiting for a fresh listing, and the re-list that eventually
happens lands in whichever home asked for it. The repair is therefore not a
re-list but a **record**: `Catalogue.remember` holds what each machine-level
source last said and writes it into every open store, and `Catalogue.seed` writes
the same record into a home's file as that file opens. `Upstream` still keeps no
tool table — what is held belongs beside the writes it is the source of, where
the "one copy" argument is about one copy *per source* rather than one per home,
which is not a copy at all but the pending write.

Two consequences worth stating rather than discovering. **The seed must not
raise**: it runs inside a store's `__init__`-adjacent open, so a source whose rows
cannot be recorded has to become the *state* `failure` already is — otherwise a
name collision written into one home's file would take the hub's whole catalogue
down and leave every home with no file at all. And **`remember` writes the
ambient home first**: a refusal has to be attributable to a source (`_listed` is
what turns it into that source's verdict), and only a write against a real file
can refuse one, so a listing that is merely held and never written would move the
collision to a home's first ask, where there is no source left to blame.

**The embedder is the *file's* home's, not the caller's**, which only shows on the
fan-out: an index placed by the asking home's model is vectors the owning home
cannot search, and its file would go on claiming an identity its numbers do not
have — the one failure here that looks exactly like success. So the write into a
given store wraps the lookup in `in_home(<that store's home>)`, and `embedding_for`
— which is keyed by `home_dir()` and already kept one connection per home — is
asked from inside it.

**The model's list is the tools it has loaded, and the rest are on demand.** This
is the change that a catalogue buys, and it is v1's mechanism restored with one
difference that matters: because the list is re-read before every *model call*,
a load takes effect one step later **inside the same turn** — v1 needed a turn
boundary for that. `tool_search` is the way in and `func_tool_load` is the way
through — and **what the way in takes has been narrowed twice**, which is what
this paragraph is the record of.

It took `category`, `source_id`, `status`, `load_status` and `limit`, and an empty
`query` was a *browse*: five filters and a listing in one tool, which meant a
model could (and did) read the catalogue ten rows at a time and call it
searching. A search ranks; a listing has no ranking to give and is as long as the
catalogue, so they are two answers and belong to two tools. The filters and the
page size are gone — the listing is §9's next tool, and a search for *a* tool
wants the top few.

Then the one `query` that replaced them went too, because one string cannot serve
both legs. The keyword leg asks for every term it is handed, so a sentence —
"take a screenshot of a web page" — demands six words at once and matches
nothing. What is left is `keywords` and `sentences`, both required, either may be
an empty array, and each goes to the leg shaped for it: words are matched
exactly, meaning by meaning. Measured on the live catalogue — 253 tools, the real
embedder — the split is **20/20 in the page against 18/20** for the single
string, and the two it missed are found by the two halves of the split: `work out
17 times 23` by a second sentence, `读一下这个网页的内容` by one written in
English. Every hit in the split is inside the top three, against three outside it
before.

**That is not because the semantic leg wants a sentence over words.** This said
"the semantic leg takes a phrase and is wasted on three loose words", and that
was measured and is false: a compact word list embeds as well as the sentence it
was taken from — on the 130-tool catalogue the two rank identically, 36/36 with
the same MRR either way. The two inputs are one call apart, not one being the
other's poor relation. What a *sentence* breaks is the keyword leg, which cannot
`AND` six words into a row that holds three.

**A keywords-only call reaches both legs anyway** (`ToolStore.search`): given
no sentences, the words are handed to the meaning leg as well. Measured through
the real handler on the 253-tool catalogue, that is **34/36 against 23/36** for
the keyword leg alone, and none of the 36 now returns nothing — where nine did.
The words were never the poorer text; they never reached the leg that could
answer them.

**Two sentences are two questions, and the page is not the fusion's to order.**
Ranking *across* questions is wrong twice over, and the second way is the one
that took two tries to see. `fuse_ranked` sums `1/(k+rank)` over its lists, which
is right for one question asked twice — the keyword leg and the meaning leg of the
same request, where a row both found is evidence — and inverted for two: a row
present in *both* sentences' lists scores at least `1/(k+40) + 1/(k+40)` = 0.0200
where a row that is *first* in one scores `1/(k+1)` = 0.0164, so mediocre-in-both
outranks first-in-one. Ordering by the best rank *any* question gave instead fixes
that and is still wrong: it hands each question an alternating half of the page,
so a second question the caller appended costs the first one five of its ten rows.
Measured on the live catalogue, appending an unrelated second sentence to a
question moved that question's fifth answer from **rank 5 to rank 9**, under four
rows of a question it had nothing to do with — while the second sentence's own
rows measured 0.544 and below against the first's 0.551 and up.

So the questions are **unioned into a shortlist and the page is ordered by what
the caller can see**: the rows the caller's own words matched first, then by
meaning, descending. Measured benefit, the report's own pair: with the second
sentence appended the page becomes **identical to the single-sentence page**, the
second question contributing nothing, because every one of its rows scores below
every one of the first's. And the ordering is no longer a second thing the answer
has to explain — the number on each row is the number that placed it, within the
tier the header names.

**The words come first because the cosines cannot separate a cluster at all.**
`set_table_column_width` and `..._widths` measure 0.63 and 0.62; `v0` and `v0_1`
both 0.66. Nothing in the meaning leg can tell which of a pair was named — only
the word match can, and that is the one piece of evidence here that is certain
rather than graded. The words belong to every question — they are the same request
for each — so they are fused into each question's list rather than standing as one
of their own.

Two things about the measurement are worth carrying forward, because both look
like language problems and neither is. **A sentence kills the keyword leg in
either language** (`take a screenshot of a web page` and `搜索一下附近的餐厅`
both match nothing — the first six terms `AND`ed, the second one phrase of ten
adjacent characters), which is why splitting the inputs rather than tuning the
boolean was the fix. And **Chinese words were never the problem**: a CJK run is
indexed character by character and queried as a *phrase*, so `搜索` finds the
rows holding 搜索. That is a measurement and not a preference — on the twelve
Chinese tool descriptions the catalogue holds (1178 characters, the only Chinese
documents there are), character tokens and *bigrams* answer the same 8 of 15
queries with the same MRR, cell for cell, and a word dictionary buys one more
(9 of 15) until it is the boolean rather than the tokenizer that changes. What
Chinese shares with English is the one real weakness left: a query in one
language against a document in another is the semantic leg's alone, and it is the
case that needed help.

**And the keyword leg is worth more the larger the catalogue.** At 130 tools the
fused answer was the semantic leg's answer with a worse MRR, and no query was
rescued by it; at 253 it finds one the semantic leg alone misses and ranks better
overall (34/36 and MRR 0.786, against 33/36 and 0.781). The case is `screenshot`,
where 55 of the 253 rows are browser or document tools: the embedding's
neighbourhood is crowded and the exact token cuts through it. One case is not a
trend, but it is the direction the theory predicts, and it is the reason both
legs are still here.

**The answer is a structure, and its first line is a verdict.** A page of the ten
nearest rows is the same shape whether the catalogue can answer the question or
cannot — which is the one thing the caller needs from it — so the answer says
which: `Best match 0.73 by meaning`, or `Weak — nothing is above 0.55 by
meaning`, or `Nothing here matches`. The floors are measurements: 36 queries the
catalogue can answer have a best-on-page meaning no lower than 0.477, and 10 it
cannot answer no higher than 0.512, so the two overlap in a 0.035 band and no
single cutoff is exact. That is why there are three tiers and not one cut —
whatever falls in the overlap is *said* rather than decided — and why nothing is
filtered by them: a weak page still shows its rows. The rows are numbered at the
left margin, which is also what keeps a ninety-line description from making the
answer uncountable.

**Every row carries the same two fields, and neither is the order.** `meaning
0.73` is the cosine, one measurement for every row on the page — including the
rows the meaning leg never returned, which `ToolStore._meaning_of` reads out of
the vectors the index already holds.  Without that second read the rows the
*caller's own words* found would carry no number at all, and the one row it named
outright would read as the weakest thing on the page. `matched your words` is the other half and is not a score: it is certain
where a cosine is graded, and it is the one piece of evidence no cutoff can
express, because a row can be named exactly whatever it scores — `pandoc` scores
0.514 against the row *named* `mcp-pandoc`, and a threshold alone would answer
"nothing here" to somebody who just said the tool's name.

It also says **where** those words are, and that is not a detail: a row is
indexed by five columns and a result line prints two of them, so a mark whose
evidence is not on the page is a mark the reader has to take on faith. The case
that made it matter is a skill, whose whole playbook rides in the `schema` column
— `skill:browser-harness` matched `read`/`file` through its body while its
description mentioned neither — so a row says `matched your words` when they are
in the name or the description it prints, and `... in its body` or `... in its
parameters` when they are behind it.

And the order is that number, in the two tiers the header names. A column that
does not run in order is a column a model will sort by and be wrong about — the
same failure as an inverted score, reached from the other side — so the number
had to become the order rather than sit beside it.
Both rows are the hub's own, so they are found and loaded like anything
else, and both are in the whitelist that is never evicted — which is the hub's
own three, plus `skill_use` and `_check_new_input`, the two entries there a
*plugin* serves (`ALWAYS_LOADED`). `_check_new_input` is the one of the five the
model never chooses: it is the name a message that arrives mid-turn is delivered
under, so it is on the list because a tool pair has to name a tool the request
declares — the same reason `_func_tool_unload` is. A plugin's tools start loaded, because
a model that has quietly lost `turn_read` is the failure this section is built
around; a server's do not
unless its entry says `autoload: true`, which is the operator saying that this
one is wanted every turn.

### 8.5 The budget

**The budget is enforced by the harness, at a turn boundary, and it says what it
took — in the conversation, not only in a log.** Over `tool_load.threshold` (a
hundred, v1's number), the least recently *called* function tools are unloaded —
never a plugin's, never an `autoload` one — and the trim is a call rather than a
rule inside the gate: `_func_tool_unload` is on the hub's API, the agent server
runs it before it saves a turn, and the answer names the tools the model has
just lost. Trimming inside the gate would be taking a tool away underneath a
model that is still using it; trimming *silently* would leave it looking for a
tool it believes it has.

So the trim is written back as a **harness tool-pair** — v1's mechanism, and the
reason it works is v1's rule: the pair names a tool, and a request whose history
calls a tool its `tools` array does not declare is a 400 from the Responses and
Messages backends. A pair invented in the history layer would be exactly that,
which is why `_func_tool_unload` is the **one `_`-prefixed name a model sees**
(v1's single exception to its own convention, kept as its own): the name is
declared, the model can call it with the names it is done with, and the pair the
harness writes is a call the tool could genuinely have made. Nothing is written
when nothing moved — an empty pair every turn is a record of something that did
not happen.

**A pair's id is the other half of that legality, and this half is ours to
choose.** The call and its result are matched by a string the harness mints, and
a provider that refuses the string refuses the whole *request* — on the replay,
because a pair is written once and carried by every request the conversation
makes afterwards, so the failure surfaces far from the code that wrote it.
Messages is the backend that states the rule, and it states it on both fields the
id appears in: `tool_use.id` and `tool_result.tool_use_id` are
`^[a-zA-Z0-9_-]+$`, with no length bound, in the reference's own words. The shape
`harness_call` mints — `_harness_`, the tool's name with its own mark stripped,
and `time_ns` in hex — sits inside that class by construction, so nothing is
sanitised on the way out; what the field shows is the other arrangement, where
the ids that trip the pattern are ones no part of this system minted (a dot from
an OpenAI-shaped translator, a colon from another producer's SDK) replayed into a
history that reaches Messages later.

**Recency is a call, and the hub is where it is learned.** The row carries two
stamps — `last_loaded`, written when it entered the list, and `last_used`,
written when the model called it — and the trim orders by the newer of the two.
One stamp would not do: ordering by the load alone evicts the wrong tool, because
a batch `func_tool_load` restamps everything it brings in and the tools the model
is actually working with — loaded long ago, called all turn — then look like the
oldest in the list. And no notification path is needed to know about a call: the
hub is not told which tools the model called, it is the process that *makes* the
call, so `call_tool` stamps the row beside the call it just routed — every routed
call, refused ones included, since a tool that keeps erroring is one the model
keeps wanting. The stamp is written before the answer is returned, which is what
keeps it ahead of the trim: a detached write could land after the turn boundary
and cost the model the tool it had just been using.

**The number itself has not been re-measured.** 100 is v1's — arrived at by
watching a real tool set — and it is carried here unchanged. Nobody has yet
watched a real twenty-server config against it, so what this section owes is not
a mechanism but an observation: on-demand loading landed, and the measurement
that would move the threshold has not been taken.

### 8.6 Names, and the links the hub holds

**A name is a row's identity, and two sources cannot offer one.** That is the
catalogue's primary key and the merge's match key, so a collision is refused
rather than resolved by whoever happened to be listed last — silently replacing
somebody's `search` is how a model calls one server and reaches another. The
merge itself has four outcomes and no fifth: a name that is not there is added,
one the source dropped is deleted, one whose columns moved is updated, and one
already identical is left alone. A steady state therefore writes nothing at all,
which is what makes asking before every model call affordable.

**Every connection the hub holds is one of ours, and that is a rule rather
than a coincidence.** A hub that cannot read a plugin's tool list *refuses to
list anything* — because a model that has quietly lost `turn_read` is a
failure nobody can see, and a shorter tool list is exactly what that failure
looks like. That rule is what `required` carries, and the other half of the same
fact is that the only links this process holds are the ones it is allowed to be
strict about — everything else is behind a plugin that declares what it holds and
can be reported without failing a turn.
Ours are the worked example of it being ordinary: a plugin's tools are declared
rows like any source's, findable by search and countable, and nothing about them
is special but the category they are filed under. And the one thing a declaration
cannot say is `plugin`, which is
the category of exactly these: a plugin may not mint a source meaning *the
servers slife2 starts*, because that is the category whose rows the audience gate
decides about.

There is deliberately no list of "plugins worth asking". The hub asks all of
them, including the model backends a config uses and the agent server, which have
nothing to offer: which tools a server has is not knowable without asking, and a
second list is a list that goes stale the first time somebody adds a tool.

### 8.7 Editing the config

**A model may edit the sections it can read, and what bounds that is the path an
edit takes rather than who takes it.** The objection this answers is real and was
made here first: an entry under `mcp:` names a program slife2 will *start*, so
writing one is a capability — "a language model choosing a program is the same
capability with a worse author". What that argument gets wrong is "bounded by
what". Not the author of the bytes, but the road they travel: every one of the
tools writes through
`slife2.libs.host.configfile`, the single writer of `slife2.yaml`, which holds the file's
cross-process lock across a read-modify-write, edits the *document* so the
file's own explanation survives, and hands the result to `slife2.libs.host.config.load` as
the judge — a file the loader refuses is put back and the caller told why. The
parsers that decide what an entry may say are the same ones a start uses
(`_tool_server`, `_rest_api`, `_cli_tool`), so an entry written by a model is an
entry an operator could have written, and the same names are refused
(`toolfamily.refusal`: a name the hub is already connected under, or one the two
document families file their rows beneath).

**The one thing that is genuinely new, and it is not a permission.** A `mcp:`
entry is a process, so a model that can write one can name a program that gets
spawned — and the argument above does not make that safe, it makes it *legible*:
it lands in the operator's file, in the section the section's comment explains,
and a person reading it sees `command: npx …` in the place they would have
written it. What the system does not have yet is a gate *before* the write, and
that is §9's tool approval rather than this paragraph's problem. The management
tools are v1's, and they are the model's (`FOR_THE_MODEL`), because the
alternative — an operator-only path — is a second way to write the same file and
therefore a second thing to keep in step. **And where families are one mechanism,
so is their code** — in two places, because there are two mechanisms.

`mcp:` and `rest-api:` hold connections, and `slife2.libs.plugin.family`'s
`build_family_server` is their server: `mcp_set` takes a command and
`rest_api_set` a spec, so those stay apart — the parameter list *is* the schema
the model reads — while the other four (`list`, `remove`, `set_enabled`,
`list_tools`) have one control flow and one set of sentences in which only the
words change.  A `FamilyWords` per section supplies `tools`/`rest-api`,
`server`/`API`, `tool`/`operation`, and the sentence is written once.  Two copies
of "is not in `…:` — `…_set` adds it" is two chances for a model to learn two
vocabularies for one road.

`skills:` and `cli:` hold documents and no connection, and
`build_document_server` is their server: one `list_sources` built from a mirror of
the section, and the mirror's two writes (`switched`, `forgotten`) shared, because
those are the parts a second copy gets wrong silently.  Their tool *bodies* stay
apart on the same argument as above — `cli_set` takes a command line and
`skill_set` takes files — which is also why `management` is a callback rather
than something the shared builder writes.

**What a management tool does not do is write the catalogue, and the bill
arrives later.** None of them touches `tools.db`: a management tool writes a
config entry and the family holds the connection, and what writes the *catalogue*
is `refresh_declared`, at the next ask — and asking is what a search does. So
adding a server is cheap and the model's **next search** is what pays: the whole
of that server's tool list is merged then, one row each, and embedded in one
request. Nothing splits that request — `index.embed_rows` chunks *documents* by
`max_chars` and not requests by size — so a published server with four figures of
tools makes a single search the expensive one, and the count is the only warning
anybody can give: `mcp_set` says so when it is over `TOOL_LIST_LIMIT`. A batch
that big is also the one failure here that is *not* the source's fault and is
reported as such (§8, "Three kinds of missing"): a merge the catalogue *refuses*
marks that source unusable, and a merge that raises fails the list rather than
quietly shortening it. Removing costs nothing in this direction — the rows, their
keyword documents and their vectors go in one transaction and nothing is
embedded, which is the whole reason a removal is a deletion rather than a
re-merge with an empty list.

**And the writer is where the promise has to be kept, so it is worth saying what
it is.** `slife2.libs.host.configfile` edits `slife2.yaml` in four steps, and each answers
one way the promise could break: it takes the *kernel's* lock on the file (the
same `slife2.libs.host.runtime.exclusive` the launcher uses for a cold start), so four
daemons and a person cannot interleave a read-modify-write; it loads the file in
ruamel's round-trip mode and edits the *document*, so every comment, quote style
and blank line around the change is still exactly where the operator put it —
which is not decoration in a file that is mostly explanation; it swaps the result
in atomically, preserving the file's mode; and it hands the whole thing to
`slife2.libs.host.config.load` before the write counts, putting the old text back if the
reader refuses. That last step is what makes the parsers the boundary rather than
a convention: an entry is judged by the same `_tool_server` / `_rest_api` /
`_cli_tool` a start uses, so "what a model may write" and "what a start accepts"
are one question with one answer. **And a `set` writes the whole entry, not a
patch of it** (`configfile.upsert` replaces): a field the caller leaves out is a
field the entry does not have. That is what makes switching a server from `url`
to `command` possible at all — a merge would keep the stale `url` and the loader
would then refuse the entry for naming two transports. Changing one field and
leaving the rest is a different operation and the one that has to read the entry
first, which is `set_enabled`.

**Two writers joined the three for the model config, and one of them is a
different shape.** `update_entry` is a read-modify-write of one entry *inside*
the lock, because a `providers:` entry holds a *list* of models and `upsert`
writes exactly what it is handed: writing the new model through it would delete
its siblings, and reading then upserting would be a read-modify-write with the
lock only on the write — the interleave the lock exists to prevent, whose cost
is silent. `set_scalar` writes the top-level `default:`, which is not a section
and so has no writer among the others. Both take a `check`, the caller's own
judge, because `load` accepts a `default:` naming no model and only
`Config.resolve` knows a reference from a typo. `configfile` stays schema-blind:
the predicate is the caller's, and a refusal rolls the file back exactly as the
loader's own does.

**And the model config is live, which the other sections never had to be.** A
family's edit is live because the process that changes it is the process that
holds the connection. `providers:` has no such process: it is read by *two
others* — the agent server, which resolves a reference, and each LLM backend,
which holds the endpoint and the key — so the config became a thing that can be
re-read. `load_cached` answers the same `Config` object while the file is
untouched and a new one when it is not, which makes "did an edit happen" one `is`
comparison and gives both readers a stat on the common path instead of a parse.
The agent server builds a conversation from the config as it *is*, so an edit
reaches the next conversation and not the one running — "a conversation's model
is fixed when it starts", seen from the other side — and each backend re-reads
its provider table on the way into a call, dropping its cached SDK clients when a
key or an endpoint moved. The one case a re-read cannot cover is a provider added
for a protocol *nothing is serving*: no process can follow the file, so the tool
starts one through `slife2.libs.host.launcher`, the same call a cold start makes.

**`model_switch` is the one edit that is not left to the next conversation, and
it is where the rule above pays for itself.** A model that has stopped answering
is the ordinary reason to leave it, so a tool that only changed what the *next*
conversation starts on would leave the person who reached for it being answered
by the model they were leaving — v1's `switch_model` reloaded the loop, and the
window's `Ctrl+S` is the same key. It is done there rather than by the caller
because the agent server is the one holding the loops, which is the argument that
puts the four `model_*` tools on that server at all: the edit and the read that
honours it are one process, and the conversation moves without the reference
being a thing anybody else has to be told about. What it does not move is a turn
in flight — `loop_for` resolves the backend once when a turn starts, so that turn
finishes on the model it began on, and `Outcome.model` is taken at the same
boundary so the turn's record names the model that actually answered it. So "a
conversation's model is fixed when it starts" survives with one exception, and
the exception is the edit that exists to change it.

**And one thing was un-ported after a first pass left it out.** v1's
`_func_tool_unload` is back, as the tool that carries the budget. The first
version of this port had the gate itself drop the excess — no tool, no call, one
less thing to explain — and it was wrong for the reason this whole arrangement is
about: the model's tool list is what its next request carries, so a list that
quietly lost three tools between two turns is a model looking for a tool it
believes it has, and the harness is the only party that can say otherwise. What
the trim needed was not to be *removed* but to be *answered*: one call at a turn
boundary, naming what it took. **Who reads that answer is what changed next**,
and it is v1's arrangement rather than this port's first one: the harness was the
only reader, so the model still learned about the trim only by reaching for a
tool that was gone. The trim is now a pair in the conversation, which puts it in
front of both readers — and puts the tool in the model's list, because a pair
whose name is not declared is a request the backends refuse.

### 8.8 The three meta-parameters

**`_timeout`, `_async` and `_approve` are arguments no tool declares.** The model
may add any of them to any call; the loop reads all three off and strips them
before dispatch, so no tool server ever sees one — which is also why they are
reserved names: a name that is sometimes a meta-parameter and sometimes a
business argument is one no reader could interpret. The strip is a **copy**: what
the *tool* is dispatched with and what the *history* keeps are two dicts, and the
second still carries them, because removing them there would be rewriting what
the model said.

**They travel in the schema, and that is the opposite of v1's choice.** v1
declared them once in the system prompt, on the argument that re-describing three
parameters on sixty schemas is the largest per-request context tax the system
could pay. v2 pays it, for what the other arrangement costs *here*: the prompt is
the operator's file (`config.agents[x].system_prompt`), so a block the harness
appended there would be a second thing every operator's prompt had to be kept in
step with, and a tool's contract belongs beside the tool — a model reading *that*
tool's arguments finds it there. `slife2.libs.turn.tools.advertised` is the one place the
three are added, and `Tool.spec` stays the tool's own declaration, so
`declares_timeout` cannot mistake `_timeout` for a tool that enforces its own
bound.

**`_timeout` is a number the model states, and `0` is never "no bound".** It
overrides a tool's own default; a tool with a native `timeout` parameter is the
single enforcer (the loop does not wrap it — exactly one timer per call); a tool
without one is wrapped by the loop, and the sentence it writes on expiry is v1's.
A bare `timeout` the model appends to a tool whose schema does not declare one is
read as the bound *and removed*, because a tool server validates its parameters
and would refuse the whole call over one extra key — the failure v1 measured and
the reason the alias exists.

**"Exactly one timer" is a rule about the whole path, and the gateway was
quietly a second one.**  The link to somebody else's MCP server built its
`Client` with a per-request timeout pinned to the *connect* budget, and FastMCP
turns that into the session's read timeout — a total deadline applied to every
request over that connection.  So a call to an `mcp:` or `rest-api:` entry died
at 60 s whatever the model had asked for, while the loop's own timer was still
running and the tool was still working at the far end: the model was told
`ok=False` about a call that was going to succeed.  The client now carries the
connect bound and nothing else, which is where the owner-of-await rule puts it —
a library must not set a total for every caller that borrows it.  The two awaits
no caller bounds, a listing and the hub's ask for a plugin's sources, carry their
own at the call site; everything else is bounded by whoever asked for it, and
"otherwise none" is what §8.8 says a foreground call with no stated bound gets.

**`_async` hands the call to the background and the result comes back by
itself.** The model gets a `task_id` and a marker — `[AgentToolResult:{…}]`,
carrying the tool call's id, the tool, the task and the status — because the
answer arrives as a message of its own, in a later turn possibly, and without the
id in it the two are related only by the model's memory. The registry is the
process's (v1's: a task belongs to the *call*), it is bounded only by a cap on
finished entries, and a turn being cancelled does not cancel the task — which is
the whole of what background means. What is not v1's is the delivery: a finished
call's result is **handed back into the conversation it came from**
(`slife2.libs.harness.handover`: the window, and a failure if it is not there) rather than
being held for a poll, and a *cancellation* is handed back too — a
cancellation nobody hears about leaves a panel on screen that is running forever.
So `check_async` answers where the call **is**, and the one thing it still carries
is a result that could not be handed back, because the alternative to returning
it there is losing it.

**`_approve` asks the person, and the asking is the caller's to declare.** The
gate is model-driven — pure model judgment, no `requires_approval` on any tool —
and the loop pauses, emits `ToolApprovalRequested`, and waits on a future it is
handed. **The answer is a call** (`answer_approval`) rather than a reply on the
progress stream, because that stream runs one way; it deliberately does not take
the conversation's lock, because the turn that is waiting holds it. A caller that
cannot be asked says so (`send_message(..., watched=…)`) and the call simply
runs, which is v1's rule for a turn with nobody behind it; a refusal mounts no
tool panel at all, because a panel that appeared and then said "denied" would
have told a reader the tool ran.

### 8.9 What a tool answers, and what two vocabularies cost

**A model-facing tool answers an object, and refuses by raising.**
`_check_new_input` is the one exception, and it is the one that is not data: it
hands over a person's message mid-turn, so what it answers *is* the thing, and
wrapping it would make the model unwrap a message before reading what somebody
wrote. The rule is the designer's (`designer_notes.md`, 大模型工具review); this
section is what applying it to a build that had grown three vocabularies decided,
and what it cost.

**It had three, and none was wrong on its own.** `context`, `schedule`,
`agent_task` and `a2a` answered objects from the start — a page of turns, a
task's five fields, an agent card. `cabinet`, the two connection families, the
two document families, `media`, `wechat`, the file tunnel, the agent server's
own `model_*`/`subagent_*` tools and the harness's two async tools answered
*sentences*: "Saved `x` to …", "`a` is connected and offers 3 tool(s)", "There is
no note about …". And the hub's own three — the ones in every model's list from
the first turn — answered a third: a compact numbered listing, argued for at
length in `_results_as_text` ("a list whose entries cannot be counted or compared
is a list it has to guess at").

Each reads well on its own, and that is exactly the problem: the **model** cannot
learn one shape and use it. It re-reads every answer to find where the facts are,
and the terminal — the other reader — ended up parsing prose for a verdict.

**A listing is a page, and `total` is the half that was missing.** `turn_list`
and the cabinet's store already answered `{entries, total, limit, offset}`, which
is where the shape came from. The schedule's task listing had no bound at all and
its run listing had a `limit` that was a *cap* — a ledger that grows for as long
as a task lives, with no way back through it. Both are pages now, and the numbers
they answer with are the numbers the page was cut to: a page whose `limit` is what
was asked for rather than what was applied is a page a caller pages wrongly from
(`page_of`'s docstring, which is where the arithmetic lives for both listings).

**A refusal raises, and a miss with it.** §8.3's failure contract already said a
tool that fails is a value with `ok` false, read by the model as a failed call
with a reason. What this replaced was a sentence returned as a *success*, and it
had two costs: the transcript records the call as one that worked, which is a lie
the model has to detect from the text, and every reader that needs the verdict
has to parse it. One did.

**`[refused]` was that reader, and it is gone.** `libs/plugin/audience.py` held
the word and a predicate over it, because the terminal has to know whether
`model_switch` *happened* before adopting the reference it just asked for — a
prefix in the text, so that a client holding only a string could tell a refusal
from a note. The tool answers `{default, previous, moved}` and refuses by raising
now, so the client reads the call's status instead, and the marker and its
predicate were deleted rather than left as a second way of saying no. What the
window *draws* is its own sentence, composed from those facts (`tui/i18n.py`),
which is where every other line it shows comes from.

**What it cost, and what was deliberately not taken.**

* **Sentences inside fields are fine, and several remain.** The rule is about the
  *envelope*. `failed: [{name, reason}]` carries a reason a person could read,
  `attach_image` names what it refused, and the agent server's refusals are still
  the sentences they were — they are simply raised now rather than returned.
* **The schemas got longer, and that is the price of the answers being
  readable.**  Descriptions went from ~5,500 words to ~7,500 across the
  model-facing set, and the growth is almost all `Returns:` — the keys of the
  object, which a model that has only the *name* of a tool cannot guess and
  which §8.8 already argues belongs on the schema rather than in the prompt (it
  is what a model reading *that* tool's answer needs, and the prompt is the
  operator's file).  The alternative was leaving the shape to be discovered at
  call time, which is what the two vocabularies already cost.
* **A tool's own arguments were left alone where renaming would break a wire
  that mirrors them.** The WeChat marker's keys are named after `wechat_send_message`'s
  arguments, so `text` stays `text` beside `a2a_send_message`'s `message`. The
  designer's rule is about a model reading several schemas; a rename that changed
  a channel's payload would be this rule buying uniformity with a broken contract.
* **`text_to_speech` and `transcribe_audio` sit beside `generate_image` and
  `generate_video`**, which is two naming schemes in one family — and they stay,
  because the four are unambiguous, and because the method name *is* the media
  adapter's contract (`adapters.py`). A rename here would be uniformity for its
  own sake, paid for by every playbook that names them.
* **A search answer can still be long.** A row's description is never cut — it is
  what a model chooses by, and one catalogue row's is ninety lines — so
  `tool_search` can outgrow `tool_result.tokens` and be dumped like any other
  large output. Bounding it would mean truncating the one field the tool exists
  to show, and the dump path already answers that case with the whole of it.
* **`agent_task_list` answers `{tasks: […]}` with no page**, because the registry
  keeps at most `RETIRED` finished tasks plus the running ones, so the answer
  cannot outgrow the budget. A listing that cannot grow is not given paging
  arguments it would never use.

## 9. Deferred

Named so they are decisions rather than oversights. Each entry says what is
missing, where it would go, and what the current answer costs — a bullet that
cannot say all three is a wish rather than a decision.

**Two things v1 had are in here in the other sense: not deferred but dropped**,
and they are named first because that is the answer a reader arriving from v1 is
looking for. They are the same shape of decision — said out loud, with what
stands in their place and what that costs — but a reader who took them for
deferred work would be waiting for something nobody is building.

- **Jobs — the deterministic, code-defined call.** v1 kept plain `.py` files in
  `~/.slife/jobs/`, one public function to one `job-<fn>` tool, registered and
  reloaded live by `job-list` / `job-write` / `job-remove` / `job-run`, for work
  that is well specified and repeatable — translate, extract, classify — where a
  job that needed a model made **one** narrow `llm.chat` on a model configured
  apart from the conversation's, so it never touched the agent's prompt cache and
  no loop ever ran. **Dropped, and not coming back.** What stands in its place is
  an agent task or a scheduled run: the same work, done by a model that has the
  conversation, the tools and the prompt around it. What that costs is the thing
  jobs existed for — a loop where a job was one call, a system prompt and a
  context where a job had neither, and a temperature of the conversation's
  choosing rather than the job author's.
- **The autonomous heartbeat.** v1 could take a periodic turn while idle
  (`agent.heartbeat_interval`, off in the shipped config since it spends tokens
  on its own), skipped whenever the inbox was busy, rendered as `⚡ 自主` — the
  precondition, in its own words, for behaviour the agent starts by itself.
  **Dropped, and not coming back.** What stands in its place is the clock: a
  scheduled task is a *named* thing with a description, a ledger and a way to see
  what each run did, where a beat was an unnamed turn whose only output was a
  line in a window. What that costs is the one property a beat had and a schedule
  cannot have — it fires when nothing asked it to.

- **The listing tool.** "What is installed" is a page of rows with no ranking —
  every tool, one category, one server's, or the ones switched off — and it is
  what a model asks when it wants to know what exists rather than find a thing.
  `ToolStore.search` still takes all five filters and still browses on an empty
  query, which is where the tool will sit. **`tool_search` does not point at it**
  and must not until it exists: a refusal that ends "ask for the list" is a
  promise this build cannot keep, and in the log of a real turn a model followed
  it by calling `skill_use` with an invented name.
- **Markdown rendering.** The transcript shows model output as plain text, and
  that is a stance rather than an unfinished edge: nothing a model produces is
  parsed as markup (`slife2/tui/widgets.py`), so a `[bold]` a model wrote is
  seven characters on the screen rather than a formatting instruction. Rendering
  markdown would keep that property — it is a rendering pass, not a markup one —
  but it is a taste decision about the transcript rather than a gap in the
  system, and nobody has asked for it.
- **Drawing a message that cut in.** §3 has the behaviour — a message that
  arrives mid-turn is handed to that turn as a tool pair and is in the record
  from then on — and nothing draws it. Closing it is three changes: an event, an
  exception in `tui/restore.py:_is_harness`'s filter, and a line that shows a
  user message **without closing the open turn** (`ChatView.add_user` sets
  `_turn_open = False`, and reusing it would drop the rest of a streamed answer).
  Removing the filter alone would draw somebody's words as a tool panel.
- **Delta coalescing.** One progress notification per token. The seam is
  `ProgressObserver`.
- **The pass behind the ceiling.** §5.1's *selection* makes the context bounded
  by construction; what is missing is the mechanism behind it — at the ceiling,
  after a turn is saved, drop the oldest complete turns down to the floor and
  take their ids out of the live list. It catches the one thing no selection can
  see in advance: a turn whose tool results balloon *while it runs*. The list and
  the write that maintains it both exist, so this is the consumer.
- **`turn_search`, offered to the model.** `TurnStore.search` fuses two legs and
  the *rebuild* reads it through `recall`; a model cannot, on purpose. The order
  is v1's — the legs landed and were measured, then the harness used them, and
  offering them to a model changes what a prompt is made of. The scoring such a
  tool should expose (`similarity`, the per-leg ranks, a snippet) is a question
  about the tool, and the store's API is already all of what it needs.
- **A cap on how many loops exist.** The inbox is capped (`MAX_QUEUED`) because
  a client's call timeout starts when the call is made, so an unbounded queue is
  an unbounded wait — and a wait past the timeout closes the stream and cancels
  the turn, which is the way a message actually gets lost. Nothing caps the
  number of conversations.
- **The tool that runs a `cli:` entry.** The entry is a row (§8.2), so a search
  finds the command by what it does — and nothing executes one. The decision is
  already made: an entry becomes **a tool the operator's own config gave the
  model**, run as an argv rather than through a shell, so that arguments a model
  invents cannot become commands it invented. `slife2-cli` is the process it
  lands in. It is also what opens the approval question below.
- **The tool that runs a skill's scripts.** A skill is a `SKILL.md`, and what it
  often tells the model to do is run the script sitting beside it. Nothing here
  performs that step (`slife2/plugins/skills/playbooks.py`), so the last line of a playbook is an
  instruction no tool of ours carries out. It is the same shape as the `cli:`
  entry above — a model-facing tool, an argv rather than a shell — and the one
  thing that makes it skills-specific is `env:` injection: `skills:` in the
  config resolves a declared key through shell-then-credstore, and handing what
  it resolved to that process is the whole reason the section exists. What the
  current answer costs is a check with nothing behind it: `skill_use` reports
  whether the key and the program are at hand, and the step it is clearing the
  way for is one nobody takes.
- **A word to the model about the loaded set.** `tool_search` and
  `func_tool_load` explain themselves in their own descriptions and nothing else
  does. v1 carried a per-turn prompt saying how many tools were loaded; nobody
  has measured whether a model uses the mechanism without a nudge, and until
  somebody has, the descriptions are the nudge.
- **Selective re-embedding.** A model change re-embeds every turn and every
  tool. Per-row content hashing would let unchanged rows keep their vectors
  across a rules change, which is the one place the current answer is slower than
  it has to be.
- **`grep`.** v1 had a third search mode — a real regex over the text, bounded,
  unranked — for the queries neither index can describe: a partial spelling, a
  path, a symbol. v1's notes say the model reached for it most often.
- **A policy that forces approval.** The *model-driven* half is built (8.8): the
  model may attach `_approve` to any call, the terminal answers it, and a refusal
  runs nothing. What is still missing is the other direction — a call the model
  does **not** ask about and that must be confirmed anyway. v1 had no such thing
  either, and the shape it would take is the one §8.7 names: the hub, the one
  place that knows the whole set, holding a **named list of tool names** rather
  than a category, because a category is a rule somebody has to be right about
  and a name is a rule somebody wrote down. The cost of the gap is concrete:
  `skill_set` writes files whose paths a model chose and `mcp_set` writes a
  `command:` that slife2 will start, and neither is gated by anything but the
  model's own judgment about whether to ask.
- **Device-code OAuth for tool servers.** v1's gateway ran the whole RFC 8628
  flow, kept tokens in the OS keyring through `credstore`, and held the token
  beside the configured headers so a re-auth did not churn them. Not ported:
  `${VAR}` headers cover a token somebody already has, and that is the whole of
  what this build reaches. **There is no fallback behind it.** An earlier
  version of this entry said FastMCP's own `auth:` key reached the SDK's browser
  flow for the rest, and it does not: `_tool_server` reads the keys it knows
  into `ToolServerSettings`, so `auth:` is dropped at load rather than handed on
  (`slife2/clients/gateway.py`, which also names what closing this costs — a field on
  the settings and a line in the loader). So a tool server that needs a login
  works here only if that login can be had out of band and handed over as a
  header, and headless is the case that hurts, because the browser flow has no
  browser.
- **A window with a window of its own.** §5.2's discriminator is one model call
  whose input is the result *and* the conversation in hand, so a result larger
  than that model's context cannot be judged at all. It is parked rather than
  half-solved: the honest fix needs the provider's window in hand, which is a
  fact about the model server rather than about this module, and until it exists
  a result that big fails the turn loudly (the dump write is a step that must
  succeed) rather than being silently shortened.
- **What a model writes, as against what a tool answers.** §5.2 bounds a tool
  *result*; a tool call's **arguments** are the model's own words and nothing
  bounds them — a model that writes a hundred thousand characters into an
  argument has put them in the conversation, and in every request after it. The
  shape that would fix it is the same one (a window, a marker, the whole in a
  dump), and what makes it a change rather than an extension is that the text
  being shortened is the *model's*, so a selection of it would be a selection of
  its own instruction to a tool — a different thing to get wrong.
- **A task's progress, and a person's hand on one.** §1.3's four tools are the
  model's: a conversation can start an agent task, ask about it, list it and stop
  it, and nothing draws one. A window has no line for a task that is running and
  no key that stops one, so a person's only lever is to ask the model — which is
  the shape §1.2 rejects everywhere else. Drawing one is the same three changes
  §9's cut-in entry names — an event, `tui/restore.py`'s `_is_harness` filter,
  and a line that does not close the open turn — plus a read of the task
  registry, which no window has.
- **A bound on total agent-task work.** `agent-task: max_running` bounds how many
  run at once, and nothing bounds how many run over time: a task may start a
  task, and each one is a full loop. `agent_task_list` and `agent_task_cancel`
  make a runaway visible and stoppable, which is a mitigation and not a bound. A
  rate, or a depth, is the number still missing — and neither is worth building
  before somebody has watched one.
- **A bound on how many scheduled tasks exist.** `agent-task: max_running` is a
  cap; the clock has none, and v1 had none either. A task is a row, its tool set
  is the one catalogue and nothing filters it, and a cron is bounded by time —
  so a runaway here is many tasks each firing rarely rather than one loop, and
  `schedule_list` with `schedule_remove` is what makes it visible and stoppable.
  `agent-task:` is also the shape the number would take (a section, one key),
  which is why `schedule` has none yet.
- **A word to the agent about runs nobody closed.** A `missed` or `failed` run is
  recorded and findable with `schedule_runs`, and **nothing says so** (§1.7): the
  agent has to think to look. The seam is obvious and already carries one message
  — a trigger is handed over on `schedule:<name>` by `slife2.libs.harness.handover`, so this is
  one more template and one more call, from the poll that records a gap or the
  sweep that fails what a stopped process left. It is deferred because the ledger
  already holds the answer and nobody has watched a missed run go unnoticed yet.
- **A trigger nobody acted on.** A run whose turn ended without calling
  `schedule_run_now` has no background task, so the poll has nothing to ask about
  and the row stays `pending`. Making the dispatch a single synchronous call is
  what shrinks this to one turn's worth of inattention, and the rule that would
  close it — a `pending` run with no task id, older than some number of minutes,
  is `failed` — is the number still missing, for the same reason §9's other bounds
  are deferred: neither of them is worth guessing before somebody has watched one.
- **Subagents.** `Loop.records` exists, so a worker's round trips are the ones
  not written to the log, and `send_message` takes a `subagent`: a worker's
  conversation is a separate key with its own history, inbox and lock, in the
  same process. **What does not exist is anything that hangs a worker off its
  parent**, so a worker id is addressable from outside — any caller may name any
  `(agent, subagent)`. A spawn tool has to decide that first, and then how it
  collects results, whether a worker is multi-turn, how deep nesting goes, and
  whether cancelling a parent cancels its children.
- **Background work inside an agent task.** A task's loop **refuses `_async`**
  (`slife2.libs.turn.loop`'s `BACKGROUND_UNAVAILABLE`) rather than running the call quietly,
  because a task has nowhere to hand a result back to: it runs one turn, has no
  inbox and no second turn, and the road every other background result takes —
  the window, then the agent server (`slife2.libs.harness.handover`) — leads to the *agent's*
  conversation, which is not the one that made the call. So a model working in a
  task is told to call the tool again without `_async`, and the task's registry
  carries no `check_async`/`cancel_async` either (they are one decision: a
  registry with no way back has no business handing out task ids). Where it would
  go: the task's turn wants what the agent server's turn already has — a queue
  and a harness tool the pair can name — so that a result arriving mid-turn
  reaches that turn at its next step boundary. What the current answer costs: a
  tool that takes minutes cannot be started and forgotten inside a task, and a
  *nested* task (a task starting a task) is the same limitation seen from the
  other side — allowed today, bounded by `agent-task: max_running` rather than by
  any rule about nesting, which is what makes the whole family feel unfinished.
- **A client id on the turn record.** Nothing reads it yet, and adding a column
  to `turn` means deleting every existing `turns.db` (§5 has no migration layer).
  Getting it wrong is worse than the gap: the new INSERT would raise
  `OperationalError`, the store would report it as one caller's bad data, and
  every turn after the upgrade would run perfectly and never be recorded. Add it
  with the reader that needs it.
- **The window's wire is defined inside the window's server.** `Handover`, its
  codec, `ATTACH`/`SUBMIT`/`NOTE`, `NO_WINDOW` and the `refused` predicate are a
  vocabulary two processes agree on — a channel writes one, the terminal reads
  it, and the terminal answers — and they live in
  `plugins/tui/server.py`, which is one of the two ends. This system's rule is
  the other way round and it is stated twice in the tree: `libs/turn/events.py`
  keeps both halves of the turn-event wire together "so the format has one
  definition and one round-trip test", and `libs/model/wire.py` does the same for
  a chunk. It costs something concrete today: `libs/harness/handover.py` imports
  `plugins.tui.server` **at module level**, so every process that hands a message
  to a window drags a FastMCP *server* in — which is the rule `libs/host/config.py`
  states about `openai_server` ("importing a server drags a server into a process
  that must not have one"). It is the only module-level `libs → plugins` edge in
  the tree. Where it would go: `libs/harness/window.py`, next to `handover.py`,
  which is the module that already knows what a hand-over is; the plugin keeps
  the tool bodies and imports the words, the client and the app import the words,
  and `handover` stops importing a plugin at all. What the current answer costs:
  the wire has no test of its own — a round-trip test of `encode`/`decode` is the
  obvious thing to want and there is nowhere to put it that is not the server
  module — and the layering rule is broken in exactly one place, which is the
  place a reader will find when they go looking for whether it is a rule at all.
- **Two readers of the config file, with three wordings between them.**
  `config.load`, `configfile._read` and `configfile._document` each read a YAML
  mapping and each refuse a file that is not one, in their own words ("cannot
  read {path}", "is not YAML", "expected a mapping at the top level", "is not a
  mapping, so it has no sections"). All four are accurate and the difference is
  editorial, which is why this is deferred rather than fixed: the wording is
  asserted (`tests/libs/host/test_config.py` matches "cannot read") and the three
  readers differ for real reasons — one takes a path because it is the file
  `find_config_path` chose, the others take text because they round-trip the
  document they are about to write back. Where it would go: one `read_mapping`
  in `configfile`, called by `load`, with one pair of sentences. What the current
  answer costs: a reader who has seen one of these messages does not recognise
  the next as the same problem, which is the cost every duplicated sentence in
  this system carries.
---

## 10. Fallbacks

**A fallback is a design behaviour, and it is the one kind that is invisible
when it is wrong.** Every other section here says what the system does; this one
says what it does *instead* when the designed road is closed — and an error path
is the least-read code in any build, so a fallback nobody decided is found by
its symptom rather than by its line. The symptom has one shape: the system does
not fail, it *deforms*, and it deforms quietly. That is why this is a section
rather than a convention in a contributor's head — a deformation is a design
decision too, and a decision nobody wrote down is the one nobody can review.

**Falling back is allowed. Falling back undeclared is not.** Some failures
cannot be designed away — a machine with no keyring, a checkout that cannot
reach the tokenizer's vocabulary, a peer that is somebody else's process — and
refusing to run in those cases would buy correctness with the whole system. So
the rule is not "never". It is that **the designed path either happens, or the
failure is named where it is used, and between those two there is nothing but
what this section registers.**

**A registered fallback states four things**, and a bullet that cannot state all
four is a wish rather than a decision — the test §9 applies to a deferred gap:

- **what it does**, in one sentence;
- **what it gives up**, because a fallback that costs nothing is not one;
- **what it does not cover** — the boundary at which it hands back to a real
  failure, which is the half that decides whether it is safe;
- **where the failure goes**, so that it is still findable afterwards.

### 10.1 The four deformations

The shapes below are how a fallback nobody decided is found without reading
every error path, and each already had a rule somewhere in this file — written
for the site that needed it, and not carried to the next one. They are here as
shapes rather than as rules because a rule per site is how the same mistake
survives in four places.

- **A value that cannot be read is not a value that is absent.** A section that
  is not a mapping, a stored list whose JSON is damaged, a reply that is missing
  the key it was asked for: each is *damage*, and each has an obvious "empty" to
  be confused with. The confusion is not cosmetic, because the empty value is
  written back — a corrupt live-context list read as `[]` is a list one turn
  long on the next save, and the loss is permanent. The rule the code already
  states where it gets this right (`config._mapping`, `server._adopt`,
  `protocol.unreadable`) is: refuse, naming the file, the section and the
  row. Tolerance is for a value whose damage costs one message; it is never for
  a value that is structural.
- **A write that failed is not a write that happened.** A bookkeeping write is
  the one kind whose failure has no reader, so it is the kind that quietly
  becomes a wrong fact: a record that did not land makes a live process
  uncountable, and the count is what stops the shared servers. The rule is that
  a bookkeeping write either lands or is reported — "the caller is not prepared
  to fail over" is a reason to report it out of band, not a reason to say
  nothing.
- **A failure is not a verdict.** The dangerous form reads as a *confident
  negative*: a search whose answer could not be read rendered as "nothing
  matched", a tool list that could not be decoded served as no tools, a name
  that is not in the config answered as though it had been removed. A model acts
  on these rather than retrying them. So an unreadable answer is a failure, and
  only a *readable* one may be a verdict.
- **A weaker path is not a substitute for a missing capability.** Feature
  detection — a field that is only sometimes present, a dependency that may not
  be installed, a model whose window is unknown — must not silently select the
  degraded branch, because that branch is then indistinguishable from the normal
  one. Where the value can be asked for, ask; where it cannot, refuse; where it
  is genuinely optional, it belongs in §10.2.

### 10.2 The register

**A missing config file runs the built-in default.** `slife2.libs.host.config.load`
answers `default_config()` — one OpenAI-compatible provider — when there is no
`slife2.yaml`, and every other section is then absent. *Gives up:* the
operator's sections, which is why a *write* refuses to create the file
(`configfile.config_path`): a section written into a fresh file would take the
place of the whole of it. *Does not cover:* a file that exists and does not
parse, which is a `ConfigError` naming the line. *Failure goes:* `slife2 status`
prints the model in use.

**A secret resolves down a chain, and resolution never raises — except when a
store that is there fails.** `config.resolve_secret` tries an OS keyring URI,
then `${VAR}` from the environment, then `credstore`, then the entry's own
literal — and failing all four leaves the reference **verbatim**, which is a
credential that fails at the API call rather than at load. *Gives up:* load-time
certainty about a credential. *Does not cover:* a host with **no** credential
store, which is the only declared degrade; a store that is present but locked,
corrupt, unsupported, **or that simply does not hold the credential** raises
where the secret is resolved, because a value that cannot be read is not an
absent one (§10.1) — and the last of those is a `ConfigError` naming the
reference and the way to store it, since a credential nobody ever stored is a
mistake to be corrected rather than a secret to be carried verbatim to the API
call. (It was a bare `KeyError`, which `slife2`'s entry point and `family.py`
both let past their `except ConfigError`, so the one place that names the fix
printed a traceback instead.) *Failure goes:* the store's own error, out of
`resolve_secret`; a reference that resolves to nothing is refused by
`llm/server_common.unresolved_key`, naming it rather than sending it as a token.

**An identity the handshake cannot report is probed by tool presence.**
`mcp_server.identifies` compares the name the server advertised while
connecting, and falls back to asking whether the expected tool is in its list.
*Gives up:* exactness — a different server that happens to serve that tool is
waved through. *Does not cover:* a server that reports a name, which is compared
exactly. *Failure goes:* `open_server` raises `ConnectionError` naming the URL.

**A link that died mid-call is retried once, and only when the transport proved
the call never left** (§8.3). *Gives up:* one round trip on a link that is
already being rebuilt. *Does not cover:* a peer's refusal, and a call whose fate
is unknown — those are values the model reads, because repeating a call that may
have run is how one answer arrives twice. *Failure goes:* the retry's own
failure is the tool's error text.

**The context rebuild keeps the context it had** (§5.1). A timeout, a provider
failure and an unparsed reply are one answer — the turns in hand — because the
rebuild has nowhere to send a failure to. *Gives up:* the rebuild, for that
turn. *Does not cover:* a store that cannot be asked at all, which fails the
turn. *Failure goes:* the turn runs on the list it has, and the store's own list
is untouched.

**A token count that cannot fetch its vocabulary falls back to the script
measure** (§5.1, `slife2.libs.turn.tokens`). *Gives up:* accuracy, in both directions.
*Does not cover:* nothing — this is the floor, and measured usage is the
provider's own count either way. *Failure goes:* nowhere: the fallback is the
answer, remembered once per process so the fetch is attempted once.

**A tool failure is a message, not an exception** (§7). `ToolRegistry.execute`
answers `ok: false` with text the model reads. *Gives up:* a caller's ability to
tell a tool error from a tool result by type alone. *Does not cover:* the
harness's own failures — a missing hub, a missing store, an `unreadable` answer
from a plugin — which fail the turn.

**An image in a turn that has already died is replaced by a note naming it**
(§5). *Gives up:* the bytes, in a record that is text. *Does not cover:* the live
conversation, where the same substitution happens on the way in
(`slife2.libs.turn.attachments.drop`). *Failure goes:* the note is in the
transcript, which is the point of it.

**Text the database cannot encode is normalised, not dropped** (§5). A lone
surrogate becomes U+FFFD; a valid pair is put back together, which is how an
emoji survives. *Gives up:* the original code unit. *Does not cover:* nothing —
a failed write fails the turn. *Failure goes:* the replacement character is
visible in the record.

**A machine that cannot prove a pid is ours refuses to signal it** (§4,
`process_start_token` → `None`). *Gives up:* `slife2 down`'s reach, on a machine
with no way to read a start token. *Does not cover:* a pid it can prove, which
is signalled normally. *Failure goes:* `down` says it cannot prove the process
is its own rather than guessing.

**One section warns instead of refusing, and it is the file tunnel's provider
name** (§1.6). An unknown `file-tunnel: active:` degrades to ngrok, and
`tunnel_status` reports the provider actually in use. *Gives up:* the operator's
intent — the tunnel that runs is not the one named. *Does not cover:* a
malformed *shape* in the same section, which is refused. *Failure goes:* the
warning at load, and the provider name in `tunnel_status`.

**A turn whose stored body will not parse is one turn with nothing to read.**
`db._loads` answers the caller's default for a `turn.messages` that is not JSON,
so the turn reads as having no messages, the keyword index gives it no document,
and `_turns_without_vectors` counts it as owing no vector — which is exactly
what "nothing to embed" means, and why it is not offered to the model again.
*Gives up:* the turn, its text, and any vector it would have had. *Does not
cover:* a value that is **structural**, where an empty one would be written
back — the live-context list and the index's own reads refuse instead
(`_structural`), because a damaged list read as `[]` becomes a one-turn list on
the next save. *Failure goes:* a warning naming the row, which is the only trace
there is.

**A turn in the live context with no row is dropped, and counted.**
`TurnStore.turns_by_ids` drops an id the file has no row for rather than
refusing, because a rebuild that refused over one absent id would turn a partial
loss into a total one. *Gives up:* the conversation comes back shorter than it
was. *Does not cover:* an id whose row exists and is damaged, which comes back
as the empty record above. *Failure goes:* a warning saying how many ids and
which ones.

**A file holding the previous schema's `turns` table is reported once and run
on.** The store gains an empty `turn` table beside it and every read afterwards
honestly reports no history. *Gives up:* the turns on disk, which are invisible
— the reason the report exists at all is that nothing else looks wrong. *Does
not cover:* a file this build wrote. *Failure goes:* one `logger.error` per
process naming the file and saying to delete it; there is no migration layer
(§5).

**A stored tool schema that will not parse is served as a tool with no
arguments.** `toolhub._parameters` answers `{}` for a `schema` column that is
neither a JSON mapping nor the sentinel a store writes for "nothing to
declare", because the hub deliberately has no opinion about how absence is
spelled (`_row_of`). *Gives up:* the arguments — the model calls the tool with
none and reads the tool's own error several frames from the cause. *Does not
cover:* a schema that parses, used exactly as written. *Failure goes:* nowhere
today; telling the sentinel from the damage would need the store's spelling at
the hub, which is the opinion it must not have.

**A tool-call answer that cannot be read is an empty mapping, and the caller
must say so.** `mcp_server.tool_payload` answers `{}` for a result that carries
neither a structured payload nor JSON text, and every hop that reads one of our
peers is expected to check the keys it needs (`Embedder.embed` does; a caller
that reads `task_id` and proceeds does not). *Gives up:* the distinction at the
one place every caller shares. *Does not cover:* a payload that parses, which is
returned as written. *Failure goes:* the caller — which is the half of this
entry that the code does not enforce.

**A card with no `a2a-status` property reads as online.** `_presence_status`
answers `online` for an absent property, which is what a peer that does not
stamp presence means, and the readiness gate is armed by that default alone.
*Gives up:* telling a card that omits presence from one that claims it. *Does
not cover:* a *deleted* card, which carries no property either and is refused by
its empty payload before this is asked. *Failure goes:* a peer that is really
gone is retired by its retained offline card or its last will, not by this.

**A failed inbound-ledger write is an error, not a refusal.**
`InboundStore._save` catches `OSError`, logs an error naming the file, and lets
the turn go on. *Gives up:* the on-disk record of what this process holds, so a
restart reads those tasks as ids it has never seen and never warns their peers.
*Does not cover:* a partially written file — the temp file and `os.replace`
still leave a reader all of the old file or all of the new one. *Failure goes:*
the error naming the ledger path, which is the only place it can be seen.

**An unopenable `schedule.db` goes quiet loudly.** `schedule_server`'s ledger
walk skips a file `adopt` cannot open and logs an error naming it. *Gives up:*
every task in that one agent's ledger, for as long as the file is damaged, while
the clock reports healthy about everyone else. *Does not cover:* a ledger whose
*rows* are bad, which is caught per pass, and the ownerless-file skip beside it,
which is its own decision (§1.7). *Failure goes:* the error naming the path —
the model's `schedule_list` only ever reads the caller's own ledger.

**A cursor that could not be written is retried, not fatal.** `Wechat._persist`
catches `OSError`, warns naming the session file, and leaves `persisted` unset so
the next cursor move writes again. *Gives up:* this stream's place in that file,
so a restart re-ingests the bridge's replay window and answers those messages as
new turns. *Does not cover:* the in-memory cursor, which still acks for the life
of the process. *Failure goes:* the warning naming the session file.

**A bookkeeping write that fails is logged, not raised.** `runtime`'s
`_write_json_atomic` suppresses `OSError` so a full disk cannot crash `status`
or refuse a start. *Gives up:* the record, at the moment it was written — a
server that started but whose record never landed is invisible to `stop` and is
orphaned. *Does not cover:* nothing; the write either completes or is not
attempted. *Failure goes:* a warning naming the path, which is the only trace of
the orphan.

**A record that is missing, truncated or malformed reads as absent.**
`runtime._read_json_dataclass` answers `None` for damage, so bookkeeping can
never block a start. *Gives up:* the pid a damaged record held — the launcher
treats a running server as record-less and `slife2 down` has nothing to signal.
*Does not cover:* nothing; it is the floor, and a caller that needs the file to
exist still only learns that it does not. *Failure goes:* nowhere, which is why
the writer's warning above is the trace to look for.

**A job that forbids breakaway is retried without the flag.** `runtime.spawn`
starts a Windows daemon with `CREATE_BREAKAWAY_FROM_JOB` and, on `OSError`,
starts it without. *Gives up:* the daemon's survival of an enclosing
kill-on-close job — §4 states escaping it as a requirement, and this is the one
path that drops it. *Does not cover:* nothing; where breakaway is permitted it
is used. *Failure goes:* a warning saying the server will not outlive its job.

**A machine that is not recognised as a checkout keeps its data under
`~/.slife2`.** `paths.data_dir` uses the directory holding a `pyproject.toml`
that names slife2, else `SLIFE2_DATA_DIR`, else `~/.slife2`. *Gives up:* the
checkout's own data directory when the marker is absent or unrecognised, which
is why an unreadable marker now raises rather than answering "not a checkout".
*Does not cover:* an override, which wins. *Failure goes:* a database or a
config missing from where it was expected.

**A stale `media.defaults:` entry falls through to the first model of the
kind.** `MediaSettings.resolve` runs §1.5's four steps and then a fifth: a
reference that no longer resolves is a weaker statement than the caller's, so
the first model of that kind answers instead of the call stopping. *Gives up:*
the operator's `defaults:` intent for that kind. *Does not cover:* a reference
that names a model of the wrong kind or an unknown provider, both refused.
*Failure goes:* the answer itself, whose own `provider/model` names the
substitution.

**A `SKILL.md` whose frontmatter will not parse reads as a skill with no
header.** A header that is not YAML reads as no frontmatter, so `scan` names the
skill after its directory and serves its body unread; a `metadata` block that is
not a mapping declares nothing, which is the same decision one level down.
*Gives up:* the declared `name`, `description` and every `requires` — so a
damaged header also stops `readiness` reporting a missing prerequisite. *Does
not cover:* a header that is a mapping, parsed normally, and the body below the
fence, returned unread either way. *Failure goes:* a warning naming the parse
error; the file is the only place the lost declaration can be recovered from.

**A CLI tunnel resolves its binary on PATH and then by guessing.** `_which`
tries `shutil.which` and then the install locations a platform guess implies
(OpenSSH's `System32` path, cloudflared's installer roots). *Gives up:* the
certainty that the program on PATH is the one that ran — a guessed copy is used
when PATH is silent, which is what makes the feature work on Windows. *Does not
cover:* a binary named explicitly by the entry's own option, used as written and
never guessed, or one found nowhere, which is a terminal failure naming the
provider. *Failure goes:* the resolved path is reported in the provider's
`status()` (the `binary` key) and `binary_path`.

**A progress line that cannot be read is dropped, and a missing count is
zero.** `events.decode` answers `None` for anything it does not recognise, and
defaults every number it does parse (`int(payload.get("steps") or 0)`). The
stream is display only and one bad frame must not end a turn. *Gives up:* the
frame, and the difference between "not measured" and "zero" in what the window
draws. *Does not cover:* the terminal event, whose fields are the ones the
transcript keeps. *Failure goes:* nowhere; the wire is a second channel and a
turn's own record is the first.

**A turn is never sized below one token.** `tokens.estimate_turn_tokens` floors
its answer at one, because a turn that weighs nothing is a turn every budget can
afford, and a budget that can afford everything is not one. *Gives up:*
exactness for the smallest turns. *Does not cover:* measured usage, which is the
provider's own count and is untouched. *Failure goes:* nowhere — the floor is
the answer.

**A probe that threw is a peer that is not up.** `launcher.probe` answers `False`
for any failure, because the question it asks is yes-or-no about reachability.
*Gives up:* the difference between a connect that was refused and one that
failed mid-handshake, so the start path may try to spawn a server that is
already there and report the port conflict that follows. *Does not cover:* a
peer that answers and names another server, which is refused by identity (§4).
*Failure goes:* the probe's own log line, and the conflict the launcher reports.

**A `taskkill` that could not run does not fail the stop.** `runtime.terminate`
suppresses `OSError` and `SubprocessError` from `taskkill /F /T` and then polls
the pid, so a force that could not be launched raises nothing out of `slife2
down`. *Gives up:* why the force failed — the caller learns that the process is
still alive, not that `taskkill` was unavailable. *Does not cover:* a process
that survives the poll, which `terminate` reports as `False` and `down` reports
as a server it could not stop. *Failure goes:* the pid the caller then polls.

**A tool answer JSON cannot serialise is rendered as its `repr`, not dropped.**
`libs/turn/tools.as_tool_text` — the one renderer, used by the loop, by the hub's
own tools and by `server_common.render_tool_result` — hands a non-string,
non-`None` answer to `json.dumps(..., ensure_ascii=False, default=str)`, so an
object the encoder does not know becomes text instead of failing the turn or
emptying the field. *Gives up:* the object's exact shape — the model reads a
representation of the value, not the value. *Does not cover:* an answer JSON can
encode, which passes through byte for byte, and `None`, which is the empty
string. *Failure goes:* the rendered text is in the request and in the turn's own
record, so what was rendered is findable.

**A lone surrogate is escaped rather than failing the request.**
`as_tool_text` encodes what it rendered as UTF-8 with `backslashreplace`, because
`ensure_ascii=False` will put a surrogate a provider's JSON handed us straight
back into the string, and a surrogate cannot be encoded at all. *Gives up:*
nothing the model needs — the character becomes the escape `ensure_ascii=True`
would have produced. *Does not cover:* text that is not a surrogate, which is
untouched. *Failure goes:* nowhere, which is the point: the alternative was a
turn that dies on the transport.

**A post-turn trim is skipped when the hub cannot be reached.** The agent
server's trim and a task's trim both swallow a hub that is unreachable or
answers with a shape this build cannot read: the turn is over and has been
answered, and bookkeeping that runs after it must not turn a good turn into a
failed one. *Gives up:* the trim for that turn, so the loaded set stays over
budget — and the catalogue is one list for the whole system, so every
conversation's next request is oversized. *Does not cover:* the next turn, whose
tool list is asked for before the conversation is touched and fails loudly on a
hub that is really gone; and because both halves of the pair are read before
either is appended, a half-written pair cannot be left behind. *Failure goes:* a
warning naming the conversation, and an error naming the task — a task has one
turn and no next request for a dead hub to fail on.

**A bookkeeping file that is already gone is not an error to delete.**
`runtime`'s `clear_record`, `clear_claim` and `unregister_client` remove their
file with `missing_ok`, so a second `stop` — or a record the client sweep
already cleaned up on the way past — is not a failure. *Gives up:* the
distinction between a file that was there and one that was not, so a caller
cannot tell that it removed anything. *Does not cover:* an unlink that fails for
any other reason (permissions, a directory in the way), which is logged at
warning naming the path. *Failure goes:* that warning.

**A trim that cannot happen does not fail a turn.** `toolclient.unload_tools`
catches every failure from the hub's `_func_tool_unload` — a hub from an older
build with no such tool, a link that is gone, an answer this build cannot read —
and answers an empty payload with the reason logged. *Gives up:* the trim for
that turn, and the names the model would have been told it had lost. *Does not
cover:* a trim the hub answers, which is used as it comes. *Failure goes:* a
warning in the agent server's log, and the next `list_tools`, where a missing
hub cannot be missed.

**Closing a link suppresses the failure.** `gateway`'s `abandon`, `disconnect`,
`close` and `ready` suppress what a teardown raises, because every one of those
is a way out — an error path, a shutdown, a link already half gone. *Gives up:*
the report of a transport that did not tear down. *Does not cover:* the call
path, where a link that died mid-call is retried once and a refusal is a value
the model reads (§8.3). *Failure goes:* nowhere, which is the price of a
teardown that must not replace whatever the caller was doing.

**A tool result past the window whose discriminator cannot be read is replaced by
the reader's own first window** (§5.2).  *Gives up:* the selection — the
conversation carries the output's beginning rather than the lines this
conversation needed, so what a model reads first is the tool's own shape rather
than its answer, and the model must ask for the rest.  *Does not cover:* the
steps that must succeed — keeping the output whole, writing it, reading the
window back — which **fail the turn** instead, because a conversation carrying an
output nobody can read back has given up the bound *and* broken the address, and
it would look like success from the outside: a fallback there would be a hidden
bug rather than a design.  *Failure goes:* an `info` line naming the call, and the
marker the conversation carries, which says what this is and where the rest is.

**Everything not in this register is a defect.** The list is the whole of this
build's declared degradation; a path that substitutes a weaker behaviour and is
not here — and not argued at the site it happens or in §1.6, §5.1, §6, §7 or
§8.3 — is a defect rather than a design, and §10.1 is how to find it. The
register carries one obligation in the other direction as well: a fallback that
stops being needed is deleted rather than left standing, because a designed path
that never runs is a designed path that has stopped being tested.
