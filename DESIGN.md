# slife2 — Design Decisions

This document records the decisions behind slife2: what was chosen, what was
rejected, and the measurements that decided the choice. It is the argument
behind the two normative documents:

- [README.md](README.md) — what the system offers and how to use it;
- [sdd.md](sdd.md) — how the system is built.

This document is explanatory, not normative. Where it and the code disagree,
the code is authoritative. Its purpose is to make a change safe: each section
states the reason a mechanism exists, what breaks if it is removed, and what was
measured before it was chosen.

## Contents

1. [Architecture](#1-architecture)
2. [Conversation and context](#2-conversation-and-context)
3. [Model backends](#3-model-backends)
4. [Tools](#4-tools)
5. [Memory and search](#5-memory-and-search)
6. [Inbound work](#6-inbound-work)
7. [Terminal](#7-terminal)
8. [Operations](#8-operations)
9. [Testing and timeouts](#9-testing-and-timeouts)
10. [Deferred capabilities](#10-deferred-capabilities)
11. [Fallback register](#11-fallback-register)

---

## 1. Architecture

### 1.1 Plugin or library

**Decision.** A capability that holds a credential, a provider SDK, or another
party's connection is a **plugin**: a process. A capability that holds only a
file is a **library**, imported by the process that owns it. A mechanism shared
by more than one store is a library.

| Holds | Form | Reason |
|---|---|---|
| A credential, an SDK, somebody else's connection | Plugin | A library would put the key and the SDK in every importer. |
| A file, and nothing else | Library | A file needs no process, and one SQLite writer is already a lock. |
| A mechanism more than one store is made of | Library | A copy per store is a bug that must be found and fixed twice. |

Consequences:

- The agent loop imports no provider SDK. Its backend speaks MCP, so switching
  providers changes an address and a configuration entry.
- A provider key exists only in the model server that calls the provider.
- A tool server's key exists only in the plugin that owns the connection.
- The tool hub decides what the model may call and holds no credential.

The cost of a library is the error boundary: a missing plugin fails the turn
with a named peer; a file that will not open raises where it is used. That is
the right trade for a file and the wrong one for a service.

### 1.2 One plugin, one job

Each plugin has one job:

| Plugin | Job |
|---|---|
| `slife2-agent` | The conversation loops |
| `slife2-context` | The turn log and the context decision |
| `slife2-toolhub` | The tool set |
| `slife2-llm-*` | One wire protocol per process |
| `slife2-mcp-tools` | The `mcp:` connections |
| `slife2-restapi-tools` | The `rest-api:` connections |
| `slife2-skills` | Playbooks |
| `slife2-cli` | The `cli:` registry |
| `slife2-cabinet` | Private documents and dumps |
| `slife2-media` | Image, video, speech, transcription |
| `slife2-file-tunnel` | A public URL for a local file |
| `slife2-wechat` | A WeChat login per agent |
| `slife2-agent-task` | Background loops of an agent |
| `slife2-schedule` | Timers and the run ledger |
| `slife2-a2a` | The peer-agent network |
| `slife2-subagent` | The local subagent network |
| `slife2-tui` | The terminal inbox |

A provider is a row in a backend's configuration, not a process of its own.
Three providers that speak two protocols are two model processes, not three:
the smallness is in what each process does.

### 1.3 One registry, everything derived

**Decision.** Every fact about a plugin that is a fact about the plugin is one
row in `libs/host/config.py:PLUGINS`.

A row holds:

- the key in `servers:`;
- the module `python -m` runs;
- the MCP name;
- the probe tool;
- the default port;
- whether it is a model API backend.

Everything else is derived:

- the start order;
- default addresses;
- `*_SERVER_NAME` constants;
- launcher specs;
- probe construction.

Adding a plugin is writing the module and adding a row. Removing one is
deleting both.

The module path is a string rather than an import because importing a server to
read its name would pull a provider SDK into the terminal and the agent loop.
One test imports each row’s module and checks that it binds the name the row
claims.

### 1.4 Shared servers

**Decision.** Servers are machine-wide daemons. A second slife2 instance
attaches to the running set instead of starting another. The last client out
stops the servers it shares.

The lifecycle rests on five rules:

1. **A probe decides whether a server is alive.** The probe checks the server’s
   identity, not just whether a port accepts a connection. A stale record is
   never used as proof of liveness.
2. **A kernel lock is a lock.** A Windows named mutex or a POSIX `flock` is
   released by the operating system when the holder dies. There is no
   stale-lock protocol.
3. **A pid is not an identity.** Records carry a process start token. `down`
   refuses to signal a pid it cannot prove is ours.
4. **A pid that exists is not a running process.** On POSIX a zombie answers
   signal 0. Liveness reads the process state as well. A state that cannot be
   read is not treated as a zombie.
5. **A child must escape its parent’s job on Windows.** Without
   `CREATE_BREAKAWAY_FROM_JOB`, a daemon dies with a kill-on-close job owned by
   a CI runner or an IDE task host.

Startup is a wave order (`NEEDS`). Everything in a wave that has no unmet
dependency starts at once. A server whose dependency failed is not attempted;
it is reported with the name of what it was waiting for, because a second
failure describing the first is noise and a doomed start is a process to clean
up.

The machine-level bookkeeping lives in `~/.slife2/runtime`:

- server records;
- logs;
- start locks;
- agent-name claims;
- registered homes;
- client registrations.

It is machine-wide because the servers are. A copy per data directory would
disagree with the others: a second directory would report a stack it could not
see, two launchers could cold-start one port, and a client could be counted as
the last one out while another home was working.

The runtime is reconstructible. Deleting it causes the next start to rebuild
what it needs.

### 1.5 Client identity and home

**Decision.** A call carries:

- a **client id**: `(agent, subagent)`;
- its **home**: the data directory the call came from.

Both travel out of band in `_meta`. A model cannot name either.

The reason is the same for both: servers are shared, so state cannot live on a
connection and files cannot be resolved against the daemon’s own directory.

MCP at the 2026-07-28 revision has no protocol-level sessions. Measured against
the installed library:

```text
in-memory transport:   call 1 -> fba941ad-...   call 2 -> 719b5744-...
real HTTP, same client: call 1 -> 8e0a6a61-...   call 2 -> 3c80209a-...
incoming headers:      {}   <- no mcp-session-id
```

`session_id` is a fresh UUID per request. Keying state on it is keying state on
a header that no longer exists.

The alternative recommended by the session-removal SEP is a server-minted handle
passed as an argument. That was rejected because anything a caller must keep can
be lost, expired, or wrong after a restart, and every caller then needs a path
for that. The client id is a name the caller already has. It cannot go stale. A
server creates state on first use, and an idle sweep may drop it; the next call
starts it again and the caller cannot tell.

The price is that a name is guessable. Here there is no authorization to
subvert: every server listens on loopback, and the only other party is a process
the user started.

### 1.6 Configuration is the description

**Decision.** One `slife2.yaml` inside a home is the only description of what
exists. It names providers, model defaults, servers, tool sections, embeddings,
media backends, the tunnel, and the two networks.

The launcher derives its server set from it. A protocol no provider speaks is
not started. A missing file uses built-in defaults. A malformed file stops the
start with a one-line error naming the file.

The configuration is read per turn for everything a conversation can see, and
cached by identity: an edit is detected with one comparison rather than a
watcher or a lock.

`libs/host/configfile.py` is the only writer for model edits. It:

- takes a cross-process lock;
- edits the document, preserving comments;
- writes atomically;
- hands the result to the same loader used at startup.

A refused entry is restored and the caller is told why. The loader is the judge,
so an entry a model writes is an entry an operator could have written.

### 1.7 The second cost: message size

MCP `tools/call` is request/response. The client SDK refuses an SSE event over
1 MiB (`DEFAULT_MAX_SSE_EVENT_SIZE`). A real session reached it with
`send_message`.

The ceiling is entirely on the client side. The server bounds neither a body nor
an event.

**Decision.** Our own hops use a transport with no ceiling. The gateway to
external servers keeps the SDK’s ceiling.

The ceiling protects a process from a peer making it buffer an unbounded
message. A daemon this system started is not such a peer; the largest message it
can produce is bounded by the model’s context, because `rebuild` and `transcript`
send exactly that list. An external server is a peer, so its ceiling stays.

The cost is that an overrun in our own hop becomes a memory failure rather than
a named SSE error. That trade is acceptable only because the peer is a process
this system started.

## 2. Conversation and context

### 2.1 The loop is a pure function

**Decision.** `libs/turn/loop.py` runs one turn over a message list. It owns no
conversation, socket, provider, or clock. The agent server owns the
conversations.

The loop receives:

- a backend with one streaming method;
- a registry of tools, refreshed by a callback before every model call;
- an observer that may drop, delay, or coalesce display events.

The server owns the state and the loop owns the algorithm. That split is what
lets a turn be tested with no server, no model, and no port.

The iteration cap is opt-in. `agent.max_iterations: 0` and an absent key both
mean no cap. A number nobody wrote is a bound nobody chose. A turn that reaches
a stated cap ends normally with `stop_reason: "max_iterations"`, not with an
exception.

### 2.2 A turn is an object, not a call

A synchronous `run_turn(messages, prompt)` is enough for one client that
displays the conversation it is running. It is not enough when a message arrives
mid-turn, because the new message must be able to wait rather than displace the
turn in flight. It is also not enough when a caller is not displaying the
conversation, such as a subagent.

So a conversation is an object keyed by its client id. The agent server exposes:

```text
send_message(agent, subagent, prompt) -> {
    text, usage, iterations, stop_reason, model, refused
}
reset(agent, subagent)
transcript(agent, subagent) -> {turns}
```

Every key is always present, including `refused` on the path where nothing could
be refused. A caller that has to test for a key gets a `KeyError` on the one
branch nobody exercised.

The server keeps:

- a per-conversation lock, because two turns can race over one list;
- an inbox, which is the same lock seen from the other side;
- the loop itself, created on first use and dropped by the idle sweep.

A message for a busy loop waits in arrival order. Nothing is dropped and nothing
is cancelled. The waiter is answered from the absorbing turn’s result, with
`injected` set. A turn that produced no result writes nothing, and the message
then runs its own turn.

### 2.3 The mid-turn message is a tool pair

A message that arrives while a turn is running is written into the running turn
at the next iteration boundary as a call to `_check_new_input` and a result
carrying the message’s own words.

It is deliberately not appended as a user message. The loop re-reads the list at
every iteration; a user message would be read as the turn’s own input, arriving
silently, and in the middle of an unanswered tool exchange it is a 400 from
several providers. The model reads the pair as what it is: something that
arrived while it was working.

The pair is drawn inside the open turn, without closing the answer still being
streamed. A rebuilt conversation draws the same line from the pair in the turn
log.

Every source follows one rule: typing in the window, a channel’s hand-over, and
a headless window’s input are all submitted immediately. The agent server’s
inbox is the only queue.

### 2.4 The context is selected, not accumulated

**Decision.** Before each turn, one discriminator model call decides:

- which turns in hand to keep;
- which turns to recall from the log.

The turn runs on the two selections, in time order.

Two mechanisms use this decision:

- `restore` replays the stored live-context list when a conversation starts;
- `rebuild` runs the discriminator before each turn.

The live-context list is an ordered array of turn ids in the turn file. It is
written in the same transaction as the turn that changed it. Its order is
authoritative: reads replay it as written and never re-sort, because the list
already encodes what was kept, dropped, and recalled, and a selection is not
always contiguous.

Kept and recalled turns are joined by id and never reconciled. An empty recall
is harmless; an overriding selection would discard the context it replaced.

The discriminator reads the conversation in hand, not only the new input,
because a follow-up usually names its subject through the turns above it. It
answers with turn ids, never with text. The harness cuts the log at those ids.

The common answer is `{}`: a decision that asks for exactly what is in hand
rebuilds nothing. That is what keeps an expensive call from being paid for
nothing.

Measurements on the live turn log:

| Input shape | Keyword leg result |
|---|---|
| A sentence-shaped query | Fired on 0 of 16 queries; most reached only the turn they were typed into. |
| A word-shaped query | Fired on 10 of 15; found the target on 9. |
| The meaning leg | Found the target in 32 of 32 recall queries, first in 25. |

So the instruction asks for a word-shaped query. The meaning leg is unharmed by
it: a compact word list ranks the same as the sentence it came from (36/36 and
the same MRR on the 130-tool catalogue).

**Failure policy.** A timeout, provider error, or unreadable reply is not a
decision. The turn runs on the context it already has. A missing store fails the
turn. There is no switch to turn the decision off: an append-only context is
bounded by nothing, and a note saying `kept 12, recalled 0` would describe a
decision no discriminator made.

**Restore.** A restored list is replayed verbatim, with no ceiling re-slicing.
Each turn carries a `[TURN:{…}]` footnote naming its id, channel, and span. The
footnote is added when a message list is built and never stored, so it cannot
drift from its row. Capitals mark a harness annotation, never the conversation’s
own words.

The newest turn is annotated after it is saved, with the same timestamps and
channel the row was written with, so the live spelling and the rebuilt spelling
are byte-identical. A difference would cost a prompt-cache miss on every
rebuild.

### 2.5 Cancellation and atomic batches

A cancelled turn leaves the user’s message and nothing the model produced,
because an assistant message whose tool calls are only partly answered is a
history state every provider rejects.

The batch is written atomically:

1. run every call;
2. fit every result;
3. append the assistant message and its results with no `await` between them.

Running a call cannot raise; the registry turns failure into text. Fitting a
result can: the fit is the caller’s callback, and a cabinet that will not open
or a discriminator that stalled raises there. Writing the assistant message
before fitting leaves exactly the rejected history behind. A failed turn keeps
its messages, so that conversation would fail every later turn.

A cancelled turn’s row is still written. The rule is not “remember to record the
cancel path”; a write conditional on how a turn ended is one somebody can
forget. The cancelled row is a user message with no answer, which is exactly
what the turn was.

A write from a cancelled handler is detached, because a plain `await` in a
cancelled scope is cancelled again at its next checkpoint. The ordinary write
happens inside the lock, so a queued turn cannot start before the write that
maintains the live-context list has landed.

### 2.6 Images

An attached image is live state, not record. The turn log keeps:

- the sentence that named the file;
- the tool answer naming it;
- a note saying the image was attached and how large it was.

The bytes never enter the row. A rebuilt conversation is therefore text; a turn
that needs the picture again attaches it again from the named path.

A failed image attachment is dropped from the live conversation with the same
note the record keeps, and the error says so. A provider that refuses an image
refuses every request that carries it; a failed turn’s messages are re-sent by
every later turn, so one bad attachment could otherwise make the conversation
unusable.

## 3. Model backends

### 3.1 One process per wire protocol

Three chat protocols are supported:

- OpenAI chat completions;
- OpenAI Responses;
- Anthropic Messages.

Responses is a protocol, not a flag on chat completions. It has different input
items, tool shapes, and streaming events. Merging the two would put two adapters
in one file and make the choice a branch inside the server rather than a fact
about the configuration.

There is also an embeddings server. It speaks `/embeddings` and nothing else,
and it is separate because no chat provider can serve an embedding model.

### 3.2 Streaming

A model backend serves one `stream_chat` tool:

- provider output is pushed down the MCP progress channel as it arrives;
- the assembled message is returned as the tool result.

**The result is authoritative; the chunks are not.** Progress notifications can
be dropped, delayed, or coalesced by the transport. The loop appends the
returned `StreamChatResult`.

The pattern is chosen over `Context.log()` for five reasons:

1. progress is scoped to the call that caused it, so two turns in flight cannot
   be confused;
2. delivery is opt-in: a client that supplied no progress token costs the server
   nothing;
3. logging is filtered by a negotiated level, so a client that never negotiated
   one loses deltas silently;
4. FastMCP mirrors log messages into the server’s own log, so every token would
   be logged at DEBUG;
5. the 2026-07-28 revision deprecates `logging`/`notifications/message` and
   requires a server not to emit a log notification for a request that did not
   ask for one.

A stream that goes quiet is a stall, not a long answer. The clock is on the
chunk, never on the call: a model that streams for an hour is fine. The
watchdog is armed by iterating chunks, so every caller must read the stream and
not only await its result. `open_backend` passes no timeout.

A provider that does not stream the answer has still answered. Each adapter
back-fills what its terminal event carries:

- Responses reads the assembled text from `response.completed`;
- chat completions reads a gateway’s deprecated `delta.function_call`.

A part a request cannot carry is named, not silently dropped. A system turn
whose content is a list of parts contributes its text and warns about the rest.

### 3.3 Provider differences are configuration

`thinking`, `reasoning`, `store`, `temperature`, `top_p`, `max_tokens`,
`stream_usage`, `input`, and `context_window` are fields of a model’s entry.
Each server sends only what its configuration asked for. An unknown value is
refused by name where it is read, not passed to a provider error.

### 3.4 Protocol compatibility

Four facts about the 2026-07-28 MCP revision are load-bearing:

1. **`ping` is gone.** A conforming server answers it with `Method not found`,
   and the specification names no replacement liveness probe. Identity comes
   from the connection handshake: the client reads `server_info.name`, falling
   back to a `tools/list` membership check only when a server reports no name.
   A bare connection test would accept a different server on the same port.
2. **There are no sessions.** `Mcp-Session-Id` and `initialize` were removed
   from Streamable HTTP; a request carries its version and capabilities in
   `_meta`. This is why state is keyed by a client id (§1.5).
3. **Change notifications are opt-in.** `notifications/tools/list_changed` now
   reaches only a client that has opened `subscriptions/listen`. Against
   FastMCP 4.0.11, `Client` exposes no call that opens one, and a FastMCP
   server advertises `tools.listChanged: false` and answers the listen request
   with `Method not found`. The hub therefore re-reads an aged listing instead
   of waiting for a notification.
4. **A result that is not pushed must ride its request.** With no push channel,
   a queued caller waits on its own call, and the inbox is capped because that
   wait has a client-side timeout.

### 3.5 Usage and cache accounting

`Usage` counts the cached prefix inside `prompt_tokens`:

- Anthropic’s `input_tokens` is the uncached remainder, so its adapter adds the
  cache fields back in;
- OpenAI-compatible wires already include cached tokens in the prompt count.

`cache_read` and `cache_creation` are parts of the prompt, never additions.

`stream_chat` takes a `kind` word:

- `turn` for the loop’s own calls;
- `discriminator` for the context decision.

One line is logged per call with the cache breakdown. A cache hit rate
distinguishes a rebuild from an append; an average across the two lineages could
not say which one missed.

OpenAI sends token counts on a trailing chunk with an empty `choices` list.
DeepSeek attaches them to the final chunk that still carries a choice. An
adapter that handles only the documented OpenAI shape reports zero tokens
against a real DeepSeek endpoint; a live call caught that after synthetic
OpenAI-shaped unit tests had passed.

## 4. Tools

### 4.1 The hub owns the set

`slife2-toolhub` is the only process that decides which tools the model may see.
It owns:

- which tools exist;
- what the model is holding;
- what a name resolves to;
- what the budget takes back;
- who may call what.

Every one of those is a question about the whole list, so it is answered in one
place. The hub holds no connection and no credential. Connections live with the
plugins that own their configuration sections.

A source declares its rows. The hub merges the declarations, gates them, names
them, and routes calls back. The declaration is the whole list and is refreshed
before every search, so a deleted entry stops being a hit and a dropped
directory takes effect at the next search.

Our plugins are required sources: if one cannot be reached, the listing refuses
rather than serving a shorter list. A model that has quietly lost `turn_read` is
a failure nobody can see. An external source is optional: its absence is a model
with fewer tools.

### 4.2 Names

A tool from an external source is `{server}__{tool}`. The double separator is
used because a single underscore is a character an upstream name may contain.

A plugin’s own tool keeps its bare name. Which plugin serves a tool is a fact
about the system, not about the tool, and it does not belong in the one thing
the model reads on every request.

All of our tools share one namespace. Two plugins cannot offer one name; the
catalogue refuses rather than letting a name mean two things.

A skill and a command are catalogue rows but not callable tools. They have no
load state and never enter the model’s list. Calling a skill row answers with
the step that reaches it (`skill_use`); calling a command row says that nothing
runs one yet.

### 4.3 The loaded set and the budget

**Decision.** The model’s list is the tools it has loaded, not every tool that
exists. It is re-read from the hub before every model call.

Rules:

- every tool served by slife2’s own plugins starts loaded;
- an external source starts loaded only with `autoload: true`;
- over `tool_load.threshold` (100), the least recently called tools are
  unloaded;
- our own tools and `autoload` sources are never evicted;
- a tool never called since loading is ordered by its load time.

The trim happens at a turn boundary, not mid-turn. Dropping the excess mid-turn
would take away a tool the model had just loaded and was about to use.

The trim is written into the conversation as a tool pair. The model reads that
the list shrank from the transcript, rather than reaching for a tool that is no
longer there. Nothing is written when nothing moved.

Recency is learned by the hub, not reported by the model: the hub is the process
that routes the call, so it stamps `last_used` beside the call it just routed,
including refused calls. The stamp is written before the answer is returned, so
it cannot land after the trim.

The threshold is 100 and has not been re-measured against a real twenty-server
configuration. On-demand loading is the mechanism; the observation that would
move the number has not been taken.

### 4.4 Search

`tool_search` takes two inputs because the two legs want different things:

- **`keywords`** are matched exactly and must all be present. This is the leg
  for names, paths, and symbols.
- **`sentences`** are ranked by meaning.

A sentence-shaped query against the keyword leg demands every word at once and
matches nothing. That is why the single `query` input was split.

Measurements on the live catalogue:

| Measurement | Result |
|---|---|
| Split inputs vs one string (253 tools) | 20/20 in the page vs 18/20. |
| Keywords-only reaching the meaning leg | 34/36 vs 23/36 for the keyword leg alone; nine empty answers became none. |
| Word list vs sentence on the meaning leg (130 tools) | Identical: 36/36 and the same MRR. |
| Two sentences ranked by fused score | An unrelated second sentence moved a first question’s fifth answer to rank 9. |
| Keyword leg at 130 vs 253 tools | At 130 it added nothing; at 253 it found one row the meaning leg missed and improved MRR. |

Two sentences are two questions. Ranking across questions with `1/(k+rank)` is
wrong: a row present in both lists outranks a row that is first in one. The page
is therefore ordered by:

1. rows the caller’s own words matched;
2. then by meaning, descending.

The words come first because cosines cannot separate a near-duplicate cluster:
`set_table_column_width` and `..._widths` measure 0.63 and 0.62. Only the word
match can say which one was named.

Chinese text is indexed character by character and queried as a phrase. FTS5’s
tokenizer would otherwise treat a contiguous Chinese run as one token. Character
tokens and bigrams measured the same on the catalogue’s Chinese documents.

The answer names its confidence in three tiers because the measurements overlap:

- answerable queries had a best-on-page meaning no lower than 0.477;
- unanswerable queries had no value higher than 0.512;
- the overlap is 0.035, so no single cutoff is exact.

Every row carries the meaning score and a word-match marker. The marker says
whether the match was in the visible name or description, or behind it in the
body or parameters. A mark whose evidence is not on the page is a mark the
reader has to take on faith.

### 4.5 Result contract

A model-facing tool answers a JSON object.

- A listing is a page: `{"<noun>": [...], "total": N, "limit": L, "offset": O}`.
  `total` counts what the filters match.
- A read answers the row’s own fields.
- A refusal or a miss raises `ToolError`, one sentence saying what is wrong and
  what to do instead. A refusal returned as a success is a call whose text and
  status disagree, and the party that cannot check is the model.
- `_check_new_input` is the one exception: it hands over a person’s message, so
  what it answers is the thing itself.
- No answer exceeds `tool_result.tokens` (default 8,000; minimum 128).

A field may contain a sentence, because a person reads some answers too. The
rule is about the envelope: one shape, so a reader that knows one tool knows them
all.

The switch from prose answers to objects was paid for in schema length. The
model-facing descriptions grew from about 5,500 to about 7,500 words, almost all
of it `Returns:` sections naming the keys of the object. That is the price of a
model being able to use an answer without inspecting it first.

### 4.6 Meta-parameters

Every tool accepts three parameters it does not declare:

| Parameter | Meaning |
|---|---|
| `_timeout` | Seconds for this call. `0` or negative means “use the default”, never “no limit”. A tool that declares its own `timeout` enforces it itself; otherwise the loop wraps the call. |
| `_async` | Run in the background. The result is handed back into the conversation. |
| `_approve` | Ask the user before running. |

They are added to every advertised schema and stripped before dispatch. The
strip is a copy: the tool is dispatched with one dict and the history keeps the
original, because removing them from history would rewrite what the model said.

`_async` is bounded by the same `tool_result.tokens` budget as a foreground
result. Backgrounding removes the wait, not the bound.

`_approve` is model-driven. There is no `requires_approval` flag on any tool;
the model decides when to ask. If no person is behind the channel, the call
runs. A caller that can answer declares `watched=true`.

## 5. Memory and search

### 5.1 Turn store

**Decision.** One SQLite file per conversation (client id): the agent's own at
`agents/<agent>/turns.db`, a non-fork subagent's at
`agents/<agent>/subagents/<name>/turns.db`. A fork has no file of its own: it is
a copy held in memory, and its context reads the maker's turn log. It writes
nothing there. Its `subagent.yaml` record is the one thing it leaves on disk,
and the server removes it at the next start: a fork lives for the session that
made it.

The file holds:

- the turn log;
- the live-context list;
- the indexes.

A turn is stored as the message list the model saw. What was asked is the first
user message; what was answered is the last assistant message with prose. There
is no second copy of either in a column.

There is no migration layer. The tables are created on a file that is not this
build’s. A file this build cannot read is reported and rebuilt, not upgraded in
place, because an upgrade path is a permanent second way to build the tables.

Agents are isolated by file. No query can reach another agent’s turns because
there are no other agent’s turns in the file. The home the call came from
selects which of the agent’s directories is read or written.

### 5.2 Index machinery

Three corpora use the same machinery:

- turns;
- the tool catalogue;
- the cabinet.

Each has two legs:

- FTS5 over a derived, normalized string;
- `sqlite-vec` over chunks of a different derived string.

The legs are fused by rank. `bm25` is unbounded and a cosine depends on the
embedding model; a rank is comparable by construction.

Rules:

1. **What a row is found by is not what it is about.** The keyword leg indexes
   every field worth looking a row up by. The vector is built from a text that
   describes what the row is. For a turn, that text is what was asked, what was
   answered, and which tools were called with what arguments—not what the tools
   answered. Tool results were measured at 56–99% of a turn’s text; embedding
   them makes every turn describe “an agent ran tools” instead of what it was
   about.
2. **A chunk carries its vector.** The vector is written first, through the
   chunk that names it, because a `vec0` row has its own rowid and a vector left
   behind is a row the next write collides with.
3. **The vector width is discovered.** A rebuild is decided by an identity
   recorded in the file: model, width, normalization rule. `vec0` fixes a
   table’s width in its DDL, and a wrong width fails silently.
4. **One normalization rule** applies to stored text and to queries alike.
5. **A list the caller sized is bound in chunks.** SQLite has a limit on the
   number of values in a statement.
6. **One request carries a batch, and a short answer is a refusal.** A row is
   written with its vectors in the same transaction, so a short answer is a hole
   no later pass can see.

Embeddings are a hard dependency. A turn is written with its vector in one
transaction, so a save that cannot embed stores nothing. That is why there is no
background drainer and no “index not ready” state.

The token estimator uses `tiktoken`’s `o200k_base` when the vocabulary can be
fetched, and a script-aware character measure when it cannot. The character
measure was measured at about 25% high on English and 60% high on Chinese, which
is a bias rather than a tolerance. Measured usage is unaffected either way: the
provider’s own count is what the record keeps.

### 5.3 The cabinet

**Decision.** Five kinds — note, diary, file, report, dump — in one table and
one corpus.

One table because the kinds are one thing to search. Four tables plus a union
view would be four corpora, and a rank only means something inside the corpus
that produced it.

Per-kind differences are a `SPECS` row:

- which column is the key;
- which is the time axis;
- whether a second write appends;
- where the markdown mirror goes.

Every document is written twice:

- a row in the index;
- a markdown file under `cabinet/`.

The mirror’s path is stored rather than derived, so a naming-rule change does
not orphan existing files.

A subject that is not already lowercase carries a short digest in its filename.
`Python.md` and `python.md` are one file on Windows and macOS; two notes sharing
one file would let the second write replace the first’s markdown while both rows
stayed correct. The readable half is kept and a digest is added.

A file’s bytes are never indexed. What is indexed is what the file is: its name,
the paths that place it, and a summary once one is written. A file embedded from
its summary alone left every summary-less file with no vector, which the sync
pass then reported forever as a document it could never catch up.

### 5.4 Oversized results

**Decision.** A tool result over `tool_result.tokens` is stored whole as a dump
and the conversation carries a selection of the output’s own lines.

The dump is keyed by the tool call id, which both sides already carry. There is
no column to write at save time and nothing that must exist before the turn is
saved.

The selection is made by a discriminator model call. It receives:

- the result;
- the conversation in hand;
- the current input.

It answers with line ranges and nothing else. The harness cuts the original at
those numbers, so the text the conversation carries is the tool’s own words or
it is nothing. A wrong selection costs a re-read, not a fabricated fact.

A non-text result is dumped and not shortened: there are no lines to choose. The
model’s judgement is recorded as the row’s MIME; the harness does not sniff the
payload.

`dump_read` is a searching reader:

- `pattern` finds lines literally;
- `context` brings neighbours;
- `offset` reads from a line, negative from the end;
- `limit` reads fewer.

Every answer stays within the same token budget. A reader that could return
more than the shortening removed would have its own answer shortened next.

Measurements from the turn log:

| Date | Turns | Results | Median | p90 | Largest |
|---|---|---|---|---|---|
| 2026-10-15 | 181 | 347 | 48 | 1,556 | 5,284 |
| 2026-10-16 | 182 | 349 | 48 | 1,587 | 5,284 |

At a 4,000-token window, 7 of 349 results (2.0%) would be shortened. At 8,000,
none. The mechanism costs one model call carrying the whole conversation and
saves at most about 1,284 tokens on this log. A model call to save a kilobyte is
the wrong trade, and a ~1M-token window makes it worse: the largest result ever
produced here is 0.5% of such a window.

The window is meant to fire rarely. Ordinary results are under 6,000 tokens; the
case it exists for is the tool bound to be long, such as a 10,000-word essay at
about 13,000 tokens. 8,000 is between them.

The pattern is text, not a regular expression. A regex over a 240,000-line
output is an unbounded program running in a shared server. A literal substring
is what the common case wants, and a model that needs a regex has the file,
which every answer names.

### 5.5 Why there is no turn-store plugin

The turn store is a library opened by the context plugin. The tool catalogue is
a library opened by the hub. The cabinet store is a private library of the
cabinet plugin.

A store has one writer, and one writer is what a SQLite file already is. Making
each store a plugin would add a hop, a reconnect path, and a failure mode
without adding an isolation boundary that the file system does not already
provide.

## 6. Inbound work

### 6.1 One road in

A message from outside the conversation becomes a turn through one road: the
window showing that conversation. A channel hands the message to
`slife2-tui`, which holds one session per agent. The window runs the message as
if it had been typed there.

The hand-over has three answers:

- **written**: the words were put on the window’s stream;
- **refused**: the window looked up its session before posting, so nothing was
  written and a retry is safe;
- **unknown**: the call did not come back, so the words may already be on the
  stream. Repeating it is how one message becomes two turns.

A channel that cannot reach a conversation says so and stops. It does not try a
second road, because “carry on without the window” is the fallback that hides a
broken system.

Presence transitions are notes, not turns. A peer coming online is a line in the
window; nothing runs and the model is not told. The roster is a tool the model
can call.

### 6.2 WeChat

WeChat has no direct API for a program. The plugin speaks iLink’s hosted bridge:
the QR handshake, the long poll, and two error envelopes whose meaning differs
from their status.

It is the plugin that owns no configuration section and declares no catalogue
rows. What it holds is a credential and a live conversation per agent, which is
the plugin side of the plugin/library table, and nothing else. Its four tools
are its own model-facing tools.

A login is per agent and stored in `agents/<agent>/wechat.yaml`. A subagent has
none: WeChat is an external identity, and a subagent is internal to its maker.
The file carries the raw agent name because the directory carries the sanitized
one; a session keyed by the directory name would be a second key for one login,
adopt the same token twice, and deliver every message twice.

One WeChat account has one live login. Scanning the account for a second agent
retires the first login; the bridge answers it with `session timeout`, a
rejection wearing HTTP 200. Both error envelope shapes are read, so the first
loop ends, the window is told, and the model is given the bridge’s own words.

There is no `wechat:` config section. A machine that should not talk to WeChat
is a machine where nobody scans a code; there is no saved login to pick up.

### 6.3 Peer agents over A2A/MQTT

The wire is the published A2A-over-MQTT profile and its SDK:

- topic tree;
- retained agent card and presence;
- JSON-RPC envelopes and their MQTT properties;
- task lifecycle and per-task deduplication;
- requester retry ladder.

What slife2 writes is the driver around it. Interoperability follows from the
wire being somebody else’s; a self-built binding would be a second protocol
wearing the name.

The agent name is the MQTT client id and the last segment of the request topic.
A subagent is not on this network: it has no external identity, and its only
network is the local one under its maker. Two processes answering to one name
trade the session back and forth, and every task sent to it runs twice. So:

- one mesh per agent;
- a second live instance under one name is refused before joining;
- the collision check reads the retained card on the name’s discovery topic.

An agent is on the mesh while a window holds its `join` call. `--headless` holds
the same call with no screen. A `peer_agent_*` call also joins the agent and
keeps it, because what follows a send is a reply the agent is waiting for.

There is no persisted inbound record. The in-flight table is in memory, per
mesh. A restart starts empty. A peer that is still waiting sees the task in its
own `peer_agent_task_list` and re-sends it; the re-send arrives as a fresh
request with a fresh bridge.

A task is completed by the model’s own call. `peer_agent_send_message` with
`message_type="task_response"` settles the inbound bridge before the call
returns. A turn that ends in prose sends nothing; the model’s failure stays
visible.

A withdrawal outranks a late answer. Cancelling records the cancellation, and
the task store refuses to let a later result or failure rewrite an ending the
caller was already told about.

The broker is started when the configured one is local and absent, and stopped
with the stack only if slife2 started it. A system broker or one started by hand
is left alone. A broker that will not serve fails startup with its name and log,
rather than leaving five tools attached to nothing.

A wildcard broker address (`0.0.0.0`, `::`, or empty) is refused. It is not the
machine’s loopback broker and not another machine’s; it is every interface, and
starting mosquitto there with anonymous access would publish every topic and
accept tasks from anybody on the network.

`provider:` is a hard constraint. Mosquitto is the only broker this build
speaks; any other value is a configuration error, not a silently disabled
feature.

### 6.4 Agent tasks

An agent task is a second loop of the same agent, started with a name and a
description and answered later by a message.

It is a separate mechanism from a subagent:

- a task runs on the agent’s own context;
- it has no address of its own;
- it records nothing;
- it accepts no second message.

The task’s context is a snapshot with an edge: `restore` reads the stored list,
and a turn is written only when it ends. The turn that called
`agent_task_start` is not in the snapshot. The description must restate the last
exchange; everything older is in the log.

A task runs one turn, so it makes one rebuild. Zero would make it the one turn
in the system whose context nobody decided.

A task’s turns are not written to the database. What the conversation gains is
the answer, which arrives as a message and is recorded by the agent server.
Writing the task’s own round trip as well would put the same answer in the
transcript twice.

`_async` is refused inside a task. The loop runs one turn and has no second turn
for a background result to arrive in, so it says so rather than running the call
quietly.

The answer arrives on the `agent-task:<name>` channel with an
`[AgentTaskResult:{…}]` marker. If no window is showing the conversation, the
answer is `undelivered`; it is not run for nobody.

A task may start another task, because the tool set is one catalogue and
nothing filters it. `agent-task.max_running` bounds concurrency, not total work
over time.

### 6.5 Scheduled tasks

A scheduled task is durable, timed, and repeatable. An agent task is ephemeral
and one-shot. The distinction is the whole reason they are separate:

- a scheduled task’s definition and ledger are the feature;
- an agent task deliberately records nothing and is allowed to vanish on
  restart.

The ledger is a task table and a run table. A run is one fire, named by the
moment it was due, with states:

- `pending`;
- `ran`;
- `failed`;
- `missed`;
- `skipped`.

The row is written before the trigger is handed over, so a fire cannot repeat on
the next poll. A hand-over that is refused marks the run `failed`. A hand-over
that does not come back leaves it `pending`, because an unknown fate must not be
reported as either.

The trigger is delivered to the window as a turn with one instruction: call
`schedule_run_now` with the due time. That call dispatches the work to the
agent-task plugin. The schedule plugin is the caller, so it receives the task
id, and its poll can close the run when the task ends.

Making the plugin the caller is the design. A prompt that told the model to
start the work and close the run later would ask it to remember across turns; a
run nobody closes is `pending` forever, which is the one state the ledger cannot
account for.

A fire is the only thing the clock says to a conversation. A missed run is
recorded, and `schedule_runs` lists it; nothing announces it. That is a cost,
and it is deliberate: the ledger holds the answer, and no plugin reaches into
another plugin’s prompt.

A trigger wears no marker. The marker vocabulary matches a result to what
produced it; a trigger is not a result. The channel carries which task, and the
template’s first line says whose ledger the run is in.

Cron is `croniter`. The five fields are trivial and the corners are not:

- `day_or`: restricted day-of-month and day-of-week are a union;
- the DST rule;
- `strict` cross-field validation, which rejects `0 9 31 2 *` before it is
  stored as an enabled task that can never fire.

One module wraps the library and owns the timezone policy and error contract.
A fire follows the wall clock of its task’s timezone. A repeated local hour is
the same fire only if its wall-clock reading equals the anchor’s; comparing
instants alone would fire twice in a fall-back hour.

The ledger is one file per agent. The shared process finds agents by walking
`agents/*/schedule.db` across every known home. A ledger carries the home it
came from, because the home is a property of the request a shared process is
serving, never of the process.

### 6.6 Subagents

A subagent is a peer with no process. The decision is about cost, measured:

| Object | Cost |
|---|---|
| A `--headless` agent instance | 99.5 MB private (120 MB working set), 5–11 threads, ~260 handles, three held calls, two MQTT connections |
| A conversation held in the shared agent server | 500 turns: 2.35 MB of text, ~1.5 MB of objects |
| The fixed machine stack | ~2.9 GB, because servers are shared |

Fifty headless instances or a thousand loops. A thousand agents is reachable
only on the second path; a thousand processes would be a cluster, not a machine.

Three shapes were considered:

1. **A subagent as a headless instance.** Smallest change, but it is the 100 MB
   path, and an instance is a window that draws nothing while a subagent has no
   window at all.
2. **A `slife2-subagent` plugin holding the loops.** Rejected because the cost
   is not the loops but the transport to them: a new plugin means a new protocol
   hop for every message, and a subagent’s identity is already the client id.
3. **A loop in the shared agent server, addressed by the second half of the
   client id.** Chosen. The loop table already exists and is keyed by
   `(home, agent, subagent)`; the storage change is a directory.

The client id is the mechanism; the directory nesting is the consequence.
`agents/jack/subagents/researcher/` says who made whom because it is inside the
maker’s directory.

A subagent:

- has the maker’s tool set, except that `subagent_set` and `subagent_remove`
  refuse a subagent caller;
- has its own system prompt (`subagent.j2`), because the operator’s prompt for
  their own agent cannot say what a subagent is, whose it is, or where its work
  comes from;
- has its own `subagent.yaml`; a non-fork subagent also has its own stored
  conversation, cabinet, and media directory; a fork stores no turns, reads the
  maker’s turn log for context without writing it, and shares the maker’s
  cabinet and media, because it is a session-lived copy of the maker’s
  conversation — its record is removed when the server next starts;
- has no independent external identity: no WeChat login and no peer-agent
  membership; it is reached only on the local subagent network;
- shares the maker’s schedule ledger, because scheduling stays with the agent;
- has no process, window, or name claim;
- is addressed by `(maker, name)`.

The tree stops at one level. The local network holds a place for the maker’s own
subagents and nothing holds a place for a level below them.

A subagent’s place on the local network is held by the local network’s process.
An inbound message becomes a turn in the subagent’s own conversation through
`send_message`; a message from the subagent to its maker is a turn in the
maker’s conversation, wearing the `subagent:<name>` channel.

A turn that ends in prose is not a reply. The send tool’s description says a
reply is a call, and it is the only road out: a task answered in prose stays
pending, which is the model’s failure made visible.

A fork is a copy of the maker’s conversation, cut back to the last completed
exchange. The copy must be sendable: the maker is mid-turn when it calls
`subagent_set`, so its live list ends with an assistant message whose results
have not come back. A fork records nothing and has no restore or rebuild; a
restart ends it — the server removes every fork record it finds and the name is
free again.

Removing a subagent tells the local network first, then stops the loop, then
deletes the directory. If the network notification cannot be made, the subagent
is left unchanged. A place on the network outlives the directory it was held
for.

## 7. Terminal

### 7.1 The window is a client

The terminal window serves nothing. It:

- sends prompts to the agent server;
- holds an `attach` call open to the terminal inbox plugin;
- holds a `join` call open to the mesh while the agent is online.

The inbox is an ordinary plugin with one session per agent. Two windows are two
sessions, and neither is more special than the other. A session lives exactly as
long as the call that opened it; closing the stream cancels the handler, so a
killed window stops counting.

The window keeps no conversation state. A restored transcript is read from the
server; a failed read is not an empty conversation.

### 7.2 Rendering

- Markers are stripped for display by the reader that knows the vocabulary.
- The channel prefix is decided in one function.
- Harness lines come from one place.
- A tool panel has four states: `running`, `done`, `error`, `interrupted`.
  `interrupted` is about the turn, not the call: the turn ended while the window
  was waiting. It is separate from `error` because the window never learned how
  the call ended.
- A connection has two states on screen: online and offline. The reasons live
  in the status tool.
- A rebuilt conversation uses the live drawing calls, not a second rendering
  vocabulary, so live and restored turns are the same thing.

Model output is plain text. Nothing a model produces is parsed as markup; a
`[bold]` a model wrote is seven characters on screen, not a formatting
instruction. Markdown rendering could preserve that property, but it is a taste
decision about the transcript rather than a missing capability.

### 7.3 Approval

The model attaches `_approve` to a call. The loop parks the turn and emits
`ToolApprovalRequested`; the terminal draws an inline row and answers through
`answer_approval`.

The answer is a call of its own because the turn’s progress channel runs one
way. It does not take the conversation lock, because the waiting turn holds it.

Losing the race is not an error: the turn may have ended a moment earlier. A
refusal mounts no tool panel, because a panel that appeared and then said
“denied” would tell a reader the tool ran.

### 7.4 The unattended agent

An agent is on the peer network while a window holds its `join` call. Everything
else already works without a terminal: a channel’s hand-over goes to the window,
a scheduled trigger goes to the window, and a task’s answer goes to the window.
An agent nobody holds is simply offline, and that is the one thing a terminal
was required for.

`--headless` runs the same `SlifeApp` with Textual’s headless driver:

- the same connections;
- the same queue;
- the same turns;
- the same prompts;
- nothing drawn and nothing printed.

No branch of the window is conditional on the flag. It is a mode, not a second
kind of agent.

The one thing that must be told to the model is that nobody is there. A model
may otherwise ask for a confirmation nobody can give and spend the turn on the
question. The flag is written into the agent-name claim, which the agent server
already reads to render the system prompt, and the template receives it as
`headless`. No instance at all renders the same way: a peer tasking an agent
nobody is running is the same situation.

The output is deliberately nothing. Turns are recorded, so a window opened later
restores the conversation, and presence is on the mesh where peers see it.
Ctrl+C stops a headless run; a kill is honest too, because the broker’s last
will publishes the offline card.

### 7.5 Language boundary

The window is bilingual. The rest of the system is English.

Detection is the operating system’s answer, read once at import. `--lang`
overrides it before anything is built. The translation layer is one accessor
over a two-language table; there are no catalog files and no dependency.

Always English:

- the system prompt;
- tool schemas;
- job result strings;
- log lines;
- external text;
- tool labels.

Key caps (`Esc`, `Ctrl+S`) remain English in both languages because a translated
key label breaks the key-to-action scan.

A daemon is shared and was not started with `--lang`, so the lines it composes
follow the OS locale. A machine whose OS language and `--lang` agree—every
machine that does not pass the flag—never sees the difference. Passing the
language to the daemons, or moving those sentences into the window, was
considered and rejected as a second source of truth or a change to the mesh’s
hand-over vocabulary.

## 8. Operations

### 8.1 The file tunnel

The tunnel exists for one thing a local path cannot do: a vision model or a chat
API that cannot read the machine’s disk. `expose_local_image` returns a public
HTTPS URL for a file.

The plugin holds a registry of token to file, pinned to the file’s device,
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
`tunnel_status` reports the provider actually in use. A malformed shape is still
refused. The tunnel is a subordinate dependency; a name this build does not have
must not keep the plugin from loading.

A published URL is not a working URL. A transport that has lost the edge keeps
its URL and answers HTTP 530, so reachability is checked separately and the tool
refuses rather than handing out a dead link.

A wrong “dead” costs a hostname. The health monitor gives a merely unreachable
transport a heal window before respawning it, and an unanswerable liveness probe
is not an outage.

The tunnel publishes the MCP endpoint as well as `/share/{token}`. Any caller who
knows the hostname can call `expose_local_image` and read any file the process
can read. What stands between that and a stranger is the hostname’s entropy.
Closing it properly requires an authorization story MCP does not carry here. A
tunnel is a decision to make this machine reachable.

### 8.2 Media

A media provider is not a chat provider. Its models are declared per kind:
image, video, tts, asr. A vendor may serve several kinds, so `kind` is a fact
about the model and `api:` selects the wire adapter.

Artifacts are written under the conversation’s work directory: the agent’s
`media/`, or a non-fork subagent’s `subagents/<name>/media/`. A fork writes to
its maker’s. They never go to the process’s working directory: the daemon’s
working directory is the runtime directory, which is documented as
reconstructible, and a generated file left there is the one file certain to be
lost.

`folder=` is contained inside that root. It is a model-written string;
`folder="../../.."` would be a model choosing where on the operator’s machine to
write a file.

Two adapters:

- `dashscope-aigc`: images, video, speech, transcription;
- `openai-images`: image generation only; it refuses the other three by name,
  which is better than a vendor 404.

### 8.3 Config edits are capabilities

A model may edit the sections it can read. What bounds the edit is the path, not
the author. Every management tool writes through the single config writer, which
locks, preserves comments, validates with the same loader a start uses, and
restores a refused entry.

A `set` writes the whole entry, not a patch. A field left out is a field the
entry does not have. That is what makes switching an entry from `url` to
`command` possible: a merge would keep the stale field and the loader would
refuse two transports. `set_enabled` is the operation that changes one field and
reads the entry first.

The model configuration is live. The agent server resolves a reference per turn,
so an edit reaches the next conversation. `model_switch` is the one edit that
moves the current conversation: the agent server holds the loops, so the edit
and the read that honours it are one process. A turn in flight finishes on the
model it started with.

There is no approval gate before a write. `mcp_set` with a `command:` records a
program slife2 will start. The write is legible—it lands in the operator’s file
in the section’s own format—but it is not gated. The model’s `_approve` is the
only confirmation mechanism, and it is model-driven.

### 8.4 Secrets

A value is a literal, `${VAR}` (shell, then credstore, then optional default),
or `keyring:<service>/<key>`.

Resolution is lazy and lenient. An unresolved reference stays verbatim, so a
missing key fails at the API call where the error can name it. A store that is
present and refuses raises; a credential the store does not hold is a
`ConfigError` naming the reference and the command that stores it.

A reference must never travel as the secret. `config.unresolved_reference` is the
single reader of “this is still a reference”.

### 8.5 Data and runtime layout

A home contains:

- `slife2.yaml`;
- `skills/`;
- `agents/tools.db` (one catalogue per home);
- `agents/<agent>/` with turns, schedule ledger, cabinet, media, WeChat login,
  and subagents.

The runtime contains machine-wide server records, logs, locks, claims, client
registrations, and known homes.

A call’s home travels with the call. Everything a plugin derives per home—the
catalogue, the declarations, the model list—is kept per home. The plugin
addresses are not: they are the shared servers.

An external `mcp:` or `rest-api:` entry is one server per name, not per home.
The connection and its listing are machine-level; the switch, description, and
declarations are the home’s. Two homes naming one entry with different commands
are reported, and the running one is used.

### 8.6 Agent identity

An agent name is claimed once per machine with a kernel lock. The claim is by
name alone, not by home: a second `jack` in another data directory is refused.
Which home a name used is not remembered.

The name creates no port, process, or config section. It is an identity that
reaches shared servers as part of the client id.

A sanitized name becomes a filename. A name that reduces to nothing is refused;
writing to a surprising path is worse than saying no.

### 8.7 Daemons outlive builds

Reuse is by identity: the launcher attaches to whatever answers to the expected
name. A daemon from a previous build can answer the name check and still have the
old tool signatures, so every turn fails until the stack is restarted. `slife2
down` is the remedy. This is why a probe checks identity and why a rename of a
server or a tool must be accompanied by a stop.

### 8.8 Text encoding

A failed database write does not fail the turn; it loses the text. Content
SQLite refuses is normalised first:

- a valid UTF-16 surrogate pair is put back together;
- a lone surrogate becomes U+FFFD.

The UTF-16 codec is the one in the standard library that distinguishes the two.
Encoding to UTF-8 with `errors="replace"` would turn an emoji into `??` and a
lone surrogate into a question mark indistinguishable from a typed one.

Every timestamp comes from one place: local time with an offset, seconds
precision. One format is what keeps ordering and range bounds correct.

## 9. Testing and timeouts

### 9.1 Two lanes

The fast lane:

- `uv run pytest`;
- runs every module except `e2e`;
- uses `-n auto`;
- needs no API key, no running server, and no fixed port;
- drives servers over FastMCP’s in-memory transport.

The e2e lane:

- `uv run pytest -m e2e`;
- starts the real stack: a launcher, seventeen daemons, a configuration on disk,
  and a stub OpenAI-compatible provider;
- proves the launch order, cross-process turns, tool routing, progress
  notifications, large-output handling, and restore.

The model is the only thing faked in either lane, and it is faked at its own
protocol boundary: a stub backend in the fast lane, a stub HTTP endpoint in the
e2e lane. A change to what the system does with a model cannot pass unseen by
changing only the fake.

Shared doubles live in `tests/fakes.py`. A test module must not import another
test module for a fixture; that is how two lanes disagree about the shape of a
double.

The suite is the gate. A bug found in the field arrives as a test that fails
without the fix.

### 9.2 Timeouts

Every await that can block has a bound, and the bound belongs to the layer that
awaits it. A callee never sets a total for its caller.

There is no turn deadline and no default tool-call budget. A single number
applied to every tool is wrong for most of them. What bounds a call is:

- the model’s `_timeout`, or
- the tool’s own declared timeout, or
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

## 10. Deferred capabilities

Each entry states what is missing, where it would go, and what the current
answer costs.

| Capability | Why it is deferred | Current cost |
|---|---|---|
| A tool that runs a `cli:` entry | The entry is already a catalogue row; execution is an argv, not a shell. | A command is discoverable but nothing runs it. |
| A tool that runs a skill’s scripts | Same shape as the `cli:` entry, plus `skills:` environment injection. | `skill_use` reports whether the key and program exist; the step it clears for is not taken. |
| A listing tool | “What is installed” is a browse, not a search; the store still has the filters. | A model must search instead of listing, and `tool_search` must not promise a list it cannot give. |
| Markdown rendering | A taste decision about the transcript, not a system gap. | Model output is plain text. |
| Delta coalescing | One progress notification per token is acceptable on loopback. | More display traffic than necessary. |
| The pass behind the context ceiling | The selection is bounded by construction; a turn whose tool results balloon while it runs is not caught. | A very large turn can exceed the ceiling until the next selection. |
| A cap on total agent-task work | `max_running` bounds concurrency; a rate or depth is the missing number. | A task can start tasks without a total bound. |
| A cap on the number of conversations | The inbox is capped because a queued call has a client-side timeout; the number of loops is not. | A client can create conversations without a machine-wide limit. |
| A visible agent-task progress line | The four tools are the model’s; the window has no line for a running task and no key that stops one. | A person’s only lever is to ask the model. |
| A word to the model about the loaded set | The two tool descriptions explain themselves; no per-turn prompt reports how many tools are loaded. | A model may not use loading unless it reads the descriptions. |
| A cap on scheduled tasks | A cron is bounded by time; a runaway is many rarely firing tasks. | `schedule_list`/`schedule_remove` make it visible and stoppable. |
| A word to the agent about unclosed runs | The ledger holds the answer and nothing has watched a missed run go unnoticed. | The agent must think to call `schedule_runs`. |
| A trigger nobody acted on | A run with no task id stays `pending`; the closing rule needs a measured timeout. | One turn’s inattention leaves a pending row. |
| Background work inside an agent task | A task runs one turn and has no second turn for a result. | `_async` is refused with an explanation. |
| A client id on the turn record | Adding a column means deleting every existing `turns.db`; add it with the reader. | No per-turn client id in the record. |
| The window wire in a library | The vocabulary lives in the terminal plugin; moving it is a layering repair. | `libs/harness/handover.py` imports a server module. |
| One config reader | Three readers differ in wording for real reasons. | A reader may not recognize the same problem twice. |
| Selective re-embedding | Per-row content hashing would keep unchanged vectors across a rules change. | A model change re-embeds every row. |
| A `grep` search leg | A bounded literal reader exists for dumps; a system-wide regex leg is a larger change. | Partial spellings and symbols rely on the two existing legs. |
| A policy that forces approval | The model-driven `_approve` exists; a named list of tools that always ask does not. | `skill_set` and `mcp_set` rely on model judgment. |
| Device-code OAuth for tool servers | `${VAR}` headers cover a token somebody already has. | A server that needs a browser login cannot be configured. |
| A discriminator result larger than its own window | The fix needs the provider’s window, a fact about the model server. | Such a result fails the turn loudly. |
| A bound on tool-call arguments | Arguments are the model’s own words; shortening them is a different problem. | A model can write an unbounded argument into the conversation. |

## 11. Fallback register

A fallback is a design behaviour, and it is the one kind that is invisible when
wrong. Falling back is allowed; falling back undeclared is not.

A registered fallback states:

1. what it does;
2. what it gives up;
3. what it does not cover;
4. where the failure goes.

### 11.1 Four shapes of an undeclared fallback

- **A value that cannot be read is not a value that is absent.** Damage and
  emptiness are different. The empty value is often written back, which makes the
  loss permanent.
- **A write that failed is not a write that happened.** A bookkeeping write
  either lands or is reported. Silence becomes a wrong fact.
- **A failure is not a verdict.** A search whose answer could not be read is not
  “nothing matched”; a tool list that could not be decoded is not “no tools”.
- **A weaker path is not a substitute for a missing capability.** Feature
  detection must not silently select a degraded branch. Ask if the value can be
  asked for; refuse if it cannot; register it if it is genuinely optional.

### 11.2 The register

| Situation | Fallback | Gives up | Does not cover | Failure goes to |
|---|---|---|---|---|
| No `slife2.yaml` | Built-in default configuration | The operator’s sections | A file that exists and does not parse (refused) | `status` prints the model in use |
| A secret reference does not resolve | Left verbatim; fails at the API call | Load-time certainty | A store that is present and refuses (raises) | The API error naming the reference |
| A server reports no name | Probe by tool presence | Exact identity | A server that reports a name (compared exactly) | `open_server` raises, naming the URL |
| A call had no client to make it | Reconnect and make the first attempt | One round trip | A call that went out and then failed, which is never repeated | The tool’s error text |
| The context discriminator fails | Keep the context in hand | The rebuild for that turn | A store that cannot be asked (fails the turn) | The turn runs on its current list |
| Tokenizer vocabulary cannot be fetched | Script-aware character measure | Accuracy in both directions | Nothing; this is the floor | Remembered once per process |
| A tool fails | `ok: false` with text the model reads | A typed distinction between error and result | Harness failures, which fail the turn | The model reads the text |
| An image is refused by a provider | Drop the pixels, keep the note | The bytes in the live conversation | The record, which never had them | The note and the error |
| Text SQLite cannot encode | Normalise surrogates | The original code unit | Nothing; a failed write fails the turn | U+FFFD in the record |
| A pid cannot be proven ours | Refuse to signal it | `down`’s reach on that machine | A pid it can prove | `down` says it cannot prove ownership |
| Unknown `file-tunnel.active` | Degrade to ngrok with a warning | The operator’s intent | A malformed shape (refused) | The warning and `tunnel_status` |
| A stored turn body will not parse | Read as a turn with no messages | The turn’s text and vector | A structural value, which is refused | A warning naming the row |
| A live-context id has no row | Drop it and count it | The conversation is shorter | A row that exists but is damaged | A warning naming the ids |
| A turn file from an older schema | Create an empty table and run | The turns on disk | A file this build wrote | One `logger.error` naming the file |
| A stored tool schema will not parse | Serve the tool with no arguments | The arguments | A schema that parses | The tool’s own error |
| A tool answer cannot be read | Return `{}` | The distinction at the shared hop | A payload that parses | The caller must check the keys |
| A card has no presence property | Read as online | Telling omission from a claim | A deleted card, refused earlier | A retained offline card or last will |
| An unopenable `schedule.db` | Skip the ledger and log an error | Every task in that ledger while damaged | Bad rows, handled per pass | The error naming the path |
| A WeChat cursor write fails | Retry on the next move | This stream’s place on disk | The in-memory cursor, which still acks | A warning naming the session file |
| A bookkeeping write fails | Log and continue | The record | Nothing; the write is attempted or not | A warning naming the path |
| A record is missing or malformed | Read as absent | The pid the record held | Nothing; this is the floor | The writer’s warning is the trace |
| A Windows job forbids breakaway | Retry without the flag | Survival of the enclosing kill-on-close job | Nothing | A warning saying the server will not outlive its job |
| The checkout marker is absent | Use `~/.slife2` | The checkout’s data directory | An override, which wins | A missing database or config |
| A stale `media.defaults:` entry | Use the first model of the kind | The operator’s default | A wrong-kind or unknown-provider reference (refused) | The answer’s own `provider/model` |
| A `SKILL.md` header will not parse | Read as a skill with no header | The declared name, description, and requirements | A header that is a mapping | A warning naming the parse error |
| A CLI tunnel binary is not on PATH | Guess the platform’s install location | Certainty about which copy ran | An explicitly named binary, or one found nowhere | The resolved path in `tunnel_status` |
| A progress line cannot be read | Drop it; default counts to zero | The difference between unmeasured and zero | The terminal event, whose fields the transcript keeps | Nothing; the record is the first channel |
| A turn weighs less than one token | Floor at one | Exactness for the smallest turns | Measured usage, which is the provider’s | Nothing; the floor is the answer |
| A probe threw | Treat the peer as not up | Whether the connect was refused or failed mid-handshake | A peer that answers and names another server | The probe’s log and the launcher’s conflict report |
| `taskkill` could not run | Poll the pid and report | Why the force failed | A process that survives the poll | The pid the caller then polls |
| A tool answer is not JSON-serialisable | Render a string form of the value (`default=str`) | The object’s exact shape | An answer JSON can encode, or `None` | The rendered text in the record |
| A lone surrogate reaches tool text | Escape it | Nothing the model needs | Text that is not a surrogate | Nothing; the request survives |
| The post-turn trim cannot reach the hub | Skip the trim | The trim for that turn; the set stays over budget | The next turn, which fails loudly on a dead hub | A warning; an error for a task |
| A bookkeeping file is already gone | Delete with `missing_ok` | The distinction between present and absent | An unlink that fails for another reason | A warning naming the path |
| The trim tool is missing or unreadable | Answer empty, log the reason | The trim and the names the model would have seen | A trim the hub answers | A warning in the server log |
| Closing a link fails | Suppress the teardown error | The report of a transport that did not tear down | The call path, where failures are values | Nothing; a teardown must not replace the caller’s work |
| The dump discriminator reply is unreadable | Carry the first window | The selection | Keeping, writing, and reading the dump, which fail the turn | An info line naming the call |

Everything not in this register is a defect. A path that substitutes a weaker
behaviour and is not here is an undeclared fallback. A fallback that stops being
needed is deleted rather than left standing, because a designed path that never
runs is a designed path that has stopped being tested.
