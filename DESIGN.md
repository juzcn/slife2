# slife2 — design

**A terminal agent whose plugins are MCP servers.**

This document records the decisions behind the first cut: what was chosen, what
was rejected, and — where it matters — the measurement that forced the choice.
It is written to be read before changing the code, because several of these
decisions look arbitrary until you know what happens if you undo them.

---

## 1. The shape

**"Plugin" is the word for one of ours, and it names an implementation rather
than a protocol.** A plugin is an MCP server over Streamable HTTP — the same
transport, the same JSON-RPC and the same tool listing as the servers under
`tools:` — plus the *plugin contract*, which is the part this project adds on
top: state keyed by a client id rather than by a session, a tool that says who
may call it, and an identity another process checks before it reuses what it
found (`slife2.mcp_server`). The word is worth having because "MCP server" does
not separate the servers slife2 starts from the twenty somebody else wrote, and
that line is load-bearing everywhere below: a missing plugin fails the turn,
where a missing entry under `tools:` is a model with fewer tools.

Every plugin is an MCP server but the TUI — all of them brought up on demand
by `slife2` and shared by every instance (see §4):

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
  │                                 │  the tool catalogue, in-process (`slife2.db.ToolStore`)
  │                                 ├── :8031/mcp ──▶ slife2-skills        (`skill_use`, the playbooks)
  │                                 ├── :8032/mcp ──▶ slife2-cli           (the `cli:` entries, as rows)
  │                                 ├── :8033/mcp ──▶ slife2-mcp-tools     (holds the `tools:` entries)
  │                                 │                  └── MCP ──▶ external tool servers (stdio or http)
  │                                 └── :8034/mcp ──▶ slife2-restapi-tools (holds the `rest-api:` ones)
  │                                                    └── MCP ──▶ the OpenAPI proxy an entry expands into
  ├── HTTP 127.0.0.1:8001/mcp ──▶ slife2-llm-openai             (openai SDK, holds keys)
  ├── HTTP 127.0.0.1:8002/mcp ──▶ slife2-llm-anthropic          (anthropic SDK, holds keys)
  └── HTTP 127.0.0.1:8003/mcp ──▶ slife2-llm-openai-responses   (openai SDK, holds keys)
```

Two things reach the embeddings server — the context store, for a turn's
vectors, and the hub, for a tool row's — because what needs a vector is an index
and there are two of them. They share one *client* (`slife2.embedder`) and one
server, which is the arrangement that keeps them ranking against the same model:
two copies of "ask and check the shape of the answer" is how the two indexes
would come to disagree about what an embedding is.

**One plugin, one job, and the granularity is deliberate.**  A model backend
speaks one wire protocol; the context store keeps a conversation's turns and
decides which of them it runs on; the hub is where the tools come from; skills
reads the playbooks, cli owns the command registry, and mcp-tools and
restapi-tools hold the servers those two sections name; the agent loop runs
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
  switching providers is changing a URL. `grep -r "import openai\|import anthropic"
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

## 1.1 Plugins and libraries

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

So `db` is a library — it holds the turn files and the tool catalogue, and
nothing else — while the embeddings server stays a plugin because it reads an
endpoint and a key off `embeddings:` and links `openai`. The `mcp-tools` and
`restapi-tools` families stay plugins under the same rule, pointed at somebody
else's processes rather than at a file: `tools:` and `rest-api:` are config
sections, and the plugin that owns a section is the one that holds the
connections its entries describe (§8). The gateway underneath them
(`slife2.gateway`) is a library, and always was. **And the one *writer* of
`slife2.yaml` is a library too** (`slife2/configfile`), which is the same row of
that table rather than a new one: it holds a file and nothing else, the four
plugins that own the four sections import it, and the cross-process lock it
takes is the other half of "one writer" — a SQLite lock for a database, a kernel
mutex for a file both a human and four daemons edit.

The cost of a library is the error boundary: a plugin that is gone fails the
turn with a named peer, and a file that will not open raises where it is used.
That is the right trade for a file — the traceback lands in the process that can
describe it — and the wrong one for a remote service. And it does not change
with the number of agents: `--agent` is a label, the servers are shared by every
instance (§4), so the multiplier for a library is the number of *processes* that
import it, not the number of agents.

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

**This server used to be a function, and now it is not.** The first cut was
`run_turn(messages, prompt)`: the caller sent the whole conversation and got
back what to remember. That was the right shape while its premise held — "here
there is one client, and the state is the conversation it is already
displaying". The premise stopped holding. A loop has to be addressable in its
own right, for two reasons that are really one: so that a message arriving while
it is busy can **wait** for it rather than displace what is running, and so that
a caller which is not displaying the conversation can still continue one. (A
subagent is that caller — see §9.)

So a conversation is an object, and the surface is three tools:

```
send_message(agent, subagent, prompt) → {text, usage, steps, stop_reason, model}
reset(agent, subagent)
transcript(agent, subagent)           → {turns}   the conversation, for a screen
```

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

**The ordinary path writes inside the lock**, and that reverses an earlier
decision. It used to run after the lock was released, on the argument that a
queued turn waiting on a network call is a wait with no reason behind it — and
the argument held while the write was a store's alone. It stopped holding when
the write started maintaining the *live-context list*: a rebuild reads that list,
so a queued turn starting before the write landed would be handed a context
missing the turn it is following up on. The price is one loopback before a queued
turn starts. What is given up is nothing: the ordering hazard the detached path
still has is confined to a cancelled turn, whose messages are deliberately left
*unbacked* and carried verbatim by the next rebuild (§5.1) rather than counted
twice.

**The id travels with the call, at every hop.** It is not only the agent server
that receives it: the write to the store and the model call are both made under the
same `(agent, subagent)`, so no hop in this system is anonymous. What the model
servers do *not* do with it is keep a conversation — see §6, and the note there
about why the history cannot live on the far side of a protocol-specific hop.

**`--agent` exclusivity is now the only thing keeping one agent to one
conversation.** The first cut enforced it in the server, because a caller
arriving under a name had to be given *its* loop rather than the previous one's.
A key is created on demand, so there is no previous one to hand over: two live
clients under one name would now genuinely share a conversation. `slife2.launcher`
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

Four decisions carry that:

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

**A child must escape its parent's job.** A process created without
`CREATE_BREAKAWAY_FROM_JOB` inherits its parent's job object, and if the parent
is inside a kill-on-close job — a CI runner, some terminal hosts — a merely
"detached" child dies with it. v1 does the opposite on purpose: it assigns
children to a kill-on-close job so they die with their parent. Here the
requirement is exactly inverted, and it is a requirement about *survival*, so it
is worth knowing which way it points.

On top of that sits one piece of bookkeeping: each client registers itself, and
the **last one out** stops the servers. Without it, the daemon rule would mean an
ordinary exit leaves the servers running until the next reboot. The count is
by pid liveness rather than a counter, so a client that was killed — and so
never deregistered — cannot keep them alive forever.

`--agent NAME` does **not** participate in any of this. It is a label: it titles
the window, it is passed through to the agent server, and it is exclusive (two
live instances may not share a name). It creates no port, no process, and no
config section. Isolation, if it ever appears, belongs inside an MCP server —
which is why the label reaches one.

## 5. The context store

**A conversation's turns, and the decision about which of them it runs on.**  One
plugin, `slife2-context`, and this is v1's `memdb` ported: the turn log, the two
tools a model reads it with, the live-context list, the recall, and the rebuild
and restore that put a context together. The store underneath is a library
(`slife2.db`), for the reason §1.1 gives — it holds files and nothing else — and
that is v1's arrangement too, where the headless host restores a session
straight from `SessionStore` with no MCP transport in the path.

The **second** thing this module keeps is the tool catalogue, which is the
*other* thing a file is enough for. It lives here and is opened by the toolhub
rather than served by anybody (§8).

**Kept as it happened, and nothing else.**  The store does not summarise, does
not decide what mattered, and until this change put nothing back into a
conversation. The schema is v1's `turn` table, minus one column — one row per
turn, columns for the two token counts, the two timestamps and the identity that
v1 arrived at by using it. The missing column is `user_message`: v1 keeps the user's half beside the
assistant's so it can be searched and embedded apart from the answer, and here
it is `messages[0]` instead, because a turn is one list of messages and a column
holding the first element of it would be a second copy to keep in step.
Everything is stored as it happened, with two deliberate
exceptions, both announced rather than silent: an oversized tool result becomes
a head-and-tail digest, and an attached image becomes a note saying it was
there. Nothing is lost by the second that the row was the only copy of — the
file is still named in the prompt, because v1's `@` marker stays in the text
where the user put it, so attaching it again is what sends it again. What the
rule buys is that a turn is text a model can read back: a ten-megabyte
screenshot in the row would be megabytes carried into every later read of that
turn, to say what one line already says.

So a question the schema cannot answer today can be asked of the same rows later
without a migration — and the columns the later features need already exist,
because adding a column to a table with rows in it is the one change this schema
has no mechanism for.

**Agents are isolated by file.** `agent="jack"` reads and writes
`jack.turn.db`, and no query can reach another agent's turns because there are
no other agent's turns in the file. Isolation as a property of the filesystem
beats isolation as a `WHERE` clause somebody can forget to write — and it is the
one place `--agent` partitions anything, since the servers themselves stay
shared.

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
and `RECALL_REPLY` say what the query is and `slife2.db` is left alone: one row
of rank is not worth a second field that every reply has to get right.

**The decision is reported, because it is the one thing in a turn nobody sees.**
`rebuild` answers with two counts — what survived of the turns in hand, and how
many came back from the log — and the agent server turns them into a
`ContextChosen` event **before the turn's first token**: it is a fact about what
the conversation is about to be, and a display that learned it afterwards would be
showing one turn's context under another turn's answer. The window draws it as a
note between the prompt and the answer, in the same shape and the same dim
styling as `[restored N turns]`, because both say what the harness did rather than
what anyone in the conversation said.

**And there is no switch for this.** There used to be one — `context.rebuild`,
whose `false` grew the context append-only — and it went for the two reasons a
switch that is never off is worth removing: the step is not optional (a context
that grows append-only is bounded by nothing, which is the trim §9 still owes), so
the flag configured a system nobody runs; and the one thing it could still do was
make the note lie, since `kept 12, recalled 0` is a true sum describing a decision
that no discriminator made.

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
copy of its first message carrying a `[TURN: {…}]` footnote naming its id, its
channel and its span, which is **how a turn id reaches the model at all** and
therefore how a keep-list is expressible. The footnote is added when a message
list is built and never stored, so it cannot drift from the row it describes.

**And there is a second place it is written, because a rebuild is not the only
way a turn enters the list.** The turn that has just run was appended by the loop
and is in no rebuilt list, so annotating only at build time leaves exactly the
newest turns unaddressable — the ones a keep-list would most often want, and
whose absence is silent. So the agent server writes the same footnote onto the
message that opened the turn once the turn is saved (`annotate_turn`) and the id
is known: after the save and never into it, and with the timestamps and channel
the row was written with, because the rebuilt spelling and the in-memory one have
to agree to the character or every rebuild costs a prompt-cache miss.

v1 spells the marker `[INFO: …]` and shares that envelope with a second thing —
the trim note — that this build does not have (§9). With one occupant the
envelope can say what it holds, so it says `TURN`.

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

That required one thing of the record. The model's reasoning is part of what
happened — a transcript read back that has lost it has a hole exactly where the
reader was looking — so it is kept on the message, and it is the one key on that
message no provider is guaranteed to know. The way out is v1's: the neutral
message (`Message.to_wire`) is what *we* pass a conversation around in, and each
adapter builds its own provider request from it, so a field can be renamed or
dropped at the edge. `slife2.llm.openai_server` renames it to DeepSeek's
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

Two things are deliberately not carried over from v1, and both are named in §9
rather than half-built here: **the trim** — which with the rebuild on is a
*guard* behind the selection rather than the mechanism the context is normally
held to — and **the real-BPE token count**, for the reason `slife2.tokens`
argues: it would be exact about a vocabulary none of these endpoints use.

**A missing server is a broken system, not a degraded one.** `slife2` starts
every plugin together and refuses to start at all if one of them will not come
up — before it draws anything, so the failure is two lines rather than a terminal
that can never connect. A peer that goes missing *later* takes the same answer:
the turn fails where the peer is used, rather than being answered and not
recorded.

That rule lives in one place, `slife2.mcp_server.open_server` — connect, prove the
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
  probe's expected tool name (`slife2.launcher.AGENT_SERVER`) has to move in the
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
bound that falls inside the same second. So there is `slife2/clock.py` and one
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
travels in the call's `_meta` rather than in its arguments (`slife2.audience`).
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
`<data>/slife2.db/tools.db`, beside the turn files and for the same reason: the
store, the embedder, the text normalization and the vector index are already
here, and a catalogue needs all four. It is served over MCP by this plugin
(`tool_merge`, `tool_injectable`, `tool_search`, …) and §8 has what it is for.

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

**`status` is the other column a boot rewrites, and the pass that does it is
told every name the config carries.** A source the config no longer names is one
nothing will ever speak for again, so its rows are marked `error` on the way up
(`ToolStore.reset`); a source the config still names is one the hub is about to
ask about, and whether it answers is the runtime's to say. So the set of names
that pass is handed is load-bearing — and it is not the plugins, which is the
only list `Config.plugins()` gives. The entries under `tools:` and `rest-api:`,
and the two document sources, are named in the config too. Handed the plugins
alone, a restart read every one of them as a source that had gone, and the truth
came back only once the holding plugin had started, reached the entry and
declared it — tens of seconds, for a section of twenty `npx` servers — and in
between a `tool_search` called those tools unusable and `func_tool_load`
refused them as "its owner is not answering" about a server that answers. Every
row was then written back, so a restart of the real catalogue logged `0 added,
239 changed` about a config nobody had edited: the two halves of that are
`slife2.toolhub.configured_sources` and a family that does not stamp a verdict
of its own on a row (`slife2.toolfamily.Held._row`).

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

**The keyword leg is not a `LIKE` fallback, and the difference is the whole
reason `slife2.textindex` exists.** FTS5's tokenizer sees a contiguous run of
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
  exact, and costs no round trip. See `slife2.mcp_server.identifies`.
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
  `Connection` re-reads an aged listing instead.

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
for step in 1..max_steps:
    Phase A: exhaust the model stream, forwarding deltas       ← no tool runs here
    append the assistant message, from the *result*
    if no tool calls: done
    Phase B: run each call, feed every result back             ← never raises
```

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
- **The observer is one method on a tagged union**, not v1's eight-method
  handler. Adding an event to a union breaks only the observers that want
  exhaustiveness; adding a method breaks every observer in the codebase. A
  broken observer cannot end a turn — the loop swallows its exceptions, but
  deliberately lets `CancelledError` through.

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
                             └── in-process ──▶ `slife2.db.ToolStore`  (the catalogue's own file)
```

### 8.1 What the hub owns, and how a tool gets into the set

**What the hub owns is the set, and it owns all of it.** Which tools exist, what
the model is holding, what a name resolves to, what the budget takes back, and
who may call what — every one of those is a question about the *whole* list, so
it is answered in one place or it is answered twice and differently. What the hub
does not own any more is a *connection*: the servers under `tools:` and
`rest-api:` are held by the two plugins named after those sections, and the hub
is told about them. `slife2.gateway` is the link itself — connect, list, call,
say whether it is answering — and it knows no catalogue, no category and no
config section, which is what lets one implementation serve the hub and both
families.

**Two ways in, and they are not the same kind of claim.** A *plugin's own tools*
arrive by `tools/list` and have to declare themselves the model's
(`slife2.audience`), because they belong to that plugin's code — `remember`
writes into a conversation's log and `send_message` drives another conversation,
and those are exactly the tools a model would reach for if it could read their
descriptions. What a plugin *holds* arrives by declaration instead, and is not
gated, because it is the operator's configuration: a `tools:` entry, a playbook,
a command. The entry is the opt-in; there was never a mark to forget. So the
default is still the safe half — a forgotten mark costs a tool that is absent,
not a tool that is dangerous — and the rule that keeps the two apart is that a
declaration may not use the category `plugin`, which means *the servers slife2
starts*, the one the gate decides about.

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
`tools:` entry's do — `skill_use` and `arxiv__search` alike. The alternative, a
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
would be reported as one whose tools the model still has.

**A listing also ages, and that is new.** The sentence above used to name a
third trigger — the peer's `tools/list_changed` — and at 2026-07-28 that trigger
stopped firing on its own: change notifications moved to `subscriptions/listen`,
a stream the client must open, and against FastMCP 4.0.11 there is no way to open
one (no such call on `Client`; a FastMCP *server* advertises
`tools.listChanged: false` and answers listen with `Method not found`, so our own
plugins never send one either). So `Connection` re-reads a listing that is older
than `RELIST_AFTER_SECONDS` on the next ask, and the notification — where a
legacy peer still sends one — is the fast path over that bound rather than the
only path. **The age costs a round trip and never a tool**: a stale listing stays
usable and stays in the model's list while it is replaced, and it is replaced
inside the same ask that noticed, because `list_tools` already waits briefly for
work it started.

**Two failures that look alike and are not.** The hub distinguishes a *transport*
failure from a peer's *refusal*, and does opposite things with them. A refusal —
an unknown tool, bad arguments, a permission it will not grant — is a value the
model reads and acts on, and rebuilding the link would only be told the same
thing again. A link that died mid-call is retried, **once**: a tool that never
ran is worth a second attempt, and a server that is down must not turn every
call into two timeouts. FastMCP makes the split visible for free —
`call_tool(..., raise_on_error=False)` returns `is_error` where the raising form
throws — which is one of the reasons this port is a few hundred lines where v1's
was three thousand.

**Three kinds of missing, and only one of them is ours.** A missing *hub* is a
plugin gone and fails the turn, like the store. A missing *source* — a plugin
that is not answering, a `tools:` entry whose process will not start — is not the
same kind of thing, and is said in its own words rather than reported as "that
tool server is broken". A missing *upstream* is the operator's configuration and
somebody else's process: reported by `servers()`, its rows left in the catalogue
with `error` on them, and retried on the next ask. An upstream *refusing a call*
is one caller's bad data. Collapsing these is how a config mistake becomes an
outage, and separating them is most of what the module's prose is about.

### 8.4 The catalogue, and the search over it

**The catalogue is a file this process opens, not a plugin it asks.**  It was
the second half of `slife2-db` and that plugin is gone, for the test in §1.1: a
store has one writer — this process — and "one writer" is what a SQLite lock
already is. So `Catalogue` in `slife2.toolhub` is a thin face over
`slife2.db.ToolStore`, opened on first use with its indexes brought up to date,
and what it replaces is a hop, a kept MCP client, a reconnect path and a
`CatalogueUnavailable` — the last of which was a class for a failure that can no
longer happen. What is left of the old split is the one that mattered and is not
about processes at all: **the hub decides, the record remembers.** A refusal is
the record saying no to *this* data (`_refused`, a `ValueError`), which is the
calling source's own problem; anything else is the catalogue itself failing to
work, which fails the tool list rather than quietly shortening it.

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

**A keywords-only call reaches both legs anyway** (`local_tools.search`): given
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
the vectors the index already holds; a row the *words* found used to carry no
number at all, so the row the caller named outright read as the weakest thing
there. `matched your words` is the other half and is not a score: it is certain
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
own three plus `skill_use`, the one entry there that a plugin serves
(`ALWAYS_LOADED`, and `_func_tool_unload` is the third of the hub's because the
harness's trim is a pair under its name). A plugin's tools start loaded, because
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
looks like. That rule is what the `required` flag used to carry, and it now
carries the other half of the same fact: the only links this process holds are
the ones it is allowed to be strict about, because everything else is behind a
plugin that declares what it holds and can be reported without failing a turn.
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
edit takes rather than who takes it.** This paragraph used to say the opposite —
that v1's `mcp_set`/`mcp_remove` were deliberately not ported, because "a
language model choosing [a program] is the same capability with a worse author" —
and the reasoning is worth keeping rather than deleting, because most of it still
holds. What holds: an entry under `tools:` names a program slife2 will *start*,
so writing one is a capability, and the operator's file was the thing bounding
it. What changed is the answer to "bounded by what". Not the author of the bytes,
but the road they travel: every one of the tools writes through
`slife2.configfile`, the single writer of `slife2.yaml`, which holds the file's
cross-process lock across a read-modify-write, edits the *document* so the
file's own explanation survives, and hands the result to `slife2.config.load` as
the judge — a file the loader refuses is put back and the caller told why. The
parsers that decide what an entry may say are the same ones a start uses
(`_tool_server`, `_rest_api`, `_cli_tool`), so an entry written by a model is an
entry an operator could have written, and the same names are refused
(`toolfamily.refusal`: a name the hub is already connected under, or one the two
document families file their rows beneath).

**The one thing that is genuinely new, and it is not a permission.** A `tools:`
entry is a process, so a model that can write one can name a program that gets
spawned — and the argument above does not make that safe, it makes it *legible*:
it lands in the operator's file, in the section the section's comment explains,
and a person reading it sees `command: npx …` in the place they would have
written it. What the system does not have yet is a gate *before* the write, and
that is §9's tool approval rather than this paragraph's problem — which is a
change from when that bullet was written: the question used to be hypothetical,
and the four families of management tools are what made it a real one. The five
tools are v1's, per family, and they are the model's (`FOR_THE_MODEL`), because
the alternative — an operator-only path — is a second way to write the same file
and therefore a second thing to keep in step. **And where two families are one
mechanism, so is their code.**  `mcp_set` takes a command and `rest_api_set` a
spec, so those stay apart — the parameter list *is* the schema the model reads.
The other four (`list`, `remove`, `set_enabled`, `list_tools`) have one control
flow between the two families, and one set of sentences in which only the words
change: a `FamilyWords` per section supplies `tools`/`rest-api`, `server`/`API`,
`tool`/`operation`, and `slife2.toolfamily` writes the sentence once.  Two copies
of "is not in `…:` — `…_set` adds it" is two chances for a model to learn two
vocabularies for one road.

**What a management tool does not do is write the catalogue, and the bill
arrives later.** None of them touches `tools.db`: a management tool writes a
config entry and the family holds the connection, and what writes the *catalogue*
is `refresh_declared`, at the next ask — and asking is what a search does. So
adding a server is cheap and the model's **next search** is what pays: the whole
of that server's tool list is merged then, one row each, and embedded in one
request. Nothing splits that request — `ToolStore._embed` chunks *documents* by
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
it is.** `slife2/configfile` edits `slife2.yaml` in four steps, and each answers
one way the promise could break: it takes the *kernel's* lock on the file (the
same `slife2.runtime.exclusive` the launcher uses for a cold start), so four
daemons and a person cannot interleave a read-modify-write; it loads the file in
ruamel's round-trip mode and edits the *document*, so every comment, quote style
and blank line around the change is still exactly where the operator put it —
which is not decoration in a file that is mostly explanation; it swaps the result
in atomically, preserving the file's mode; and it hands the whole thing to
`slife2.config.load` before the write counts, putting the old text back if the
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

## 9. Deferred

Named so they are decisions rather than oversights:

- **The listing tool, and the filters `tool_search` used to take.** §8 is the
  argument for splitting them out; what is missing is the tool. "What is
  installed" is a page of rows with no ranking — every tool, or one category, or
  one server's, or the ones switched off — and it is what a model asks when it
  wants to know what exists rather than find a thing. Nothing needs building on
  the store's side: `ToolStore.search` still takes all five filters and still
  browses on an empty query, which is where the tool will sit. **And `tool_search`
  no longer points at it.** Its refusal used to end "ask for the list", which read
  as an instruction to a model that then called `skill_use` with an invented name
  and got "no skill called `__list__`" — a refusal naming a tool that does not
  exist is a promise it cannot keep. When this bullet stops being deferred, the
  sentence can name the tool and be true.
- **Markdown rendering.** The transcript shows model output as plain text.
- **Drawing a message that cut in.** §3 has the behaviour: a message that arrives
  while a turn is running is handed to that turn as a tool pair, and it is then in
  the record and in every request the model makes. Nothing *draws* it. That is the
  current state rather than a defect — the live transcript is rendered from
  `TurnEvent`s and an auto-invoked call emits none, and a rebuilt one drops every
  `_`-prefixed call by name (`tui/restore.py:_is_harness`), which is right for the
  trim and wrong for this one: its result is something a person said. Closing it
  is three changes and the third is the real one — an event, an exception in that
  filter, and a line that shows a user message **without closing the open turn**,
  because `ChatView.add_user` sets `_turn_open = False` and reusing it would
  silently drop the rest of the answer being streamed. Removing the filter alone
  would draw it as a tool panel, which is the one thing it is not. Until then a
  person's words can be in the record and off the screen, which is worth knowing
  before a window is wired to deliver immediately.
- **`thinking` deltas, and the display decision has since been made.** Both SDKs
  expose them cheaply, and rendering a model's private reasoning as its *answer*
  would be worse than not showing it — so the rule that landed is neither
  "drop" nor "print": they cross as their own event kind (`ThinkingDelta`),
  never merged into the text, and the transcript folds them away behind a
  toggle. This bullet used to read "dropped until there is a display decision",
  and the code moved past it.
- **Delta coalescing.** One notification per token. The seam is
  `ProgressObserver`.
- **The trim, which is now a guard rather than the bound.** This bullet used to
  read "history trimming, and now it is the server's problem" and it was the
  first thing to do next; the *selection* landed instead (§5.1), which is v1's
  default and makes the context bounded by construction — every turn is rebuilt
  to fit the window before it runs, and a recall that would overfill it is cut by
  the recall's own budget. What is missing is the mechanism *behind* that: at the
  ceiling, after the turn is saved, drop the oldest complete turns down to the
  floor and take their ids out of the live list. What it catches is the one thing
  no selection can see in advance — a turn whose tool results balloon *while it
  runs* — and what this plugin already owes it is the list and the write that
  maintains it, which both exist. The `context.ceiling` and `context.floor`
  fractions are read today; the pass that consumes them is not written.
- **The real token count.** v1 counts with `tiktoken`'s `o200k_base` and
  provisions the vocabulary at install time. `slife2.tokens` is a script-aware
  character measure instead, for the reason its docstring gives: this system's
  endpoints are DeepSeek, Qwen, Ollama and whatever gateway the operator points
  at, and `o200k_base` is not one of their vocabularies either — so the
  dependency would buy exactness about a tokenizer nobody here is using. What it
  costs is tolerance: the budget is a fraction of a window, so an estimate that
  is 10% wrong spends 10% of a window sized for it. Measured usage is untouched
  by this — `context_tokens` is the provider's own count, and it is what the
  status bar and any future ceiling check read.
- **`turn_search`, offered to the model.** `TurnStore.search` fuses a keyword leg
  with a semantic one, and the *rebuild* reads it through `recall` — but a model
  cannot: there is still no `turn_search` tool, on purpose. The order matters and
  it is v1's: the two legs landed and were measured, then the harness started
  using them, and offering them to a model is a third step that changes what a
  prompt is made of. The scoring such a tool should expose (`similarity`, the
  per-leg ranks, a snippet) is a question about the tool rather than about the
  store, and the store's API is already the whole of what it would need.
- **Bounds.** A loop caps its inbox (`MAX_QUEUED`), because a client's call
  timeout starts when the call is made and an unbounded queue is therefore an
  unbounded wait — and a wait longer than the timeout closes the stream and
  cancels the turn, which is the very way a message gets lost. Nothing yet caps
  how many loops exist.
- **The tool that runs a `cli:` entry.** The entry is a row now (§8's
  declaration), so a search finds the command by what it does — but nothing
  executes one, and `func_tool_load` says so in as many words ("a command
  already installed on this machine … nothing runs one yet"). Those entries are
  `tools.yaml`'s other half served: what a call would do is the change, not the
  row — and where it goes is now decided, because the family has a process:
  `slife2-cli`, the plugin whose only job today is declaring the rows.
- **A word to the model about the loaded set.** `tool_search` and
  `func_tool_load` explain themselves in their own descriptions and nothing else
  does. v1 also carried a per-turn prompt saying how many tools were loaded;
  nobody has measured whether a model uses the mechanism without a nudge, and
  until somebody has, the descriptions are the nudge.
- **Recall, offered.** The store now decides relevance — `TurnStore.search`
  fuses a keyword leg with a semantic one — but **nothing yet offers it to a
  model**: there is no `turn_search` tool, on purpose, so the two legs could
  land and be measured before a tool made them part of a prompt. The store's API
  is the whole of what exists today; the tool is the next change, and the
  scoring it should expose (`similarity`, the per-leg ranks, a snippet) is a
  question about that tool rather than about this one.
- **Selective re-embedding.** A model change re-embeds every turn. Per-turn
  content hashing would let unchanged rows keep their vectors across a rules
  change, which is the one place the current answer is slower than it needs to
  be.
- **`grep`.** v1 had a third search mode — a real regex over the text, run in
  Python over a bounded scan, unranked — for the queries neither index can
  describe: a partial spelling, a path, a symbol. It is not here, and it is the
  mode v1's notes say the model reached for most often.
- **Tool approval, and the trigger is no longer hypothetical.** This cut was
  deferrable while slife2's own tools were side-effect-free — and then the four
  families of management tools landed (§8), which are the tools
  this design was deferring the question for: `skill_set` writes files a model
  chose the paths of, `mcp_set` writes a `command:` that slife2 will start, and
  neither asks anybody first. What §8 argues there is that the write is
  *legible* — one writer, the loader as judge, the result in the file a person
  reads — and legible is not the same as gated, so the gap this bullet names is
  now open rather than closed by construction. v1 answered it with a
  model-driven `_approve` parameter, which is the shape to be suspicious of: the
  model being asked is the model asking. §8 still says where the answer goes —
  the hub is the one place that knows the whole set and the only one that could
  hold a per-tool policy without the agent learning what a tool server is — and
  what that answer has to cover is now a named list of tool names rather than a
  category.
- **Skills, and the CLI registry.** v1 had two families that were never quite
  tools, and each is now a plugin of its own (§8). A **skill** is a playbook: a
  directory with a
  `SKILL.md` that the model reads on demand (`skill_list` → `skill_use`) instead
  of calling, which is progressive disclosure and worked. The **`cli:` section**
  is a registry of programs already on the machine — `yt-dlp`, a browser
  harness — that v1 recorded in `tools.yaml` and then ran with `execute_shell`,
  a third family that was never ported either. **The reading half has landed**:
  `cli:` in `slife2.yaml`, parsed and refused when an entry names no command;
  `<data>/skills/` becoming the folder a skill is installed by dropping in; and
  `skill_use`, which `slife2-skills` serves (§8) and which reads one playbook by
  name — its header for the name and the description, and its whole body after
  one line saying what the paths in it are relative to. With it, the credential
  half: a skill declares in its own header what it needs (`requires.env`,
  `requires.bins` — the block these skills already carry for other hosts, read
  out of whichever namespace it is filed under), `skills:` in `slife2.yaml` says
  where the value comes from, and `skill_use` reports the difference before the
  model acts on instructions that would fail. **And the writing half has landed
  too**, which reverses what this bullet used to say ("Nothing writes, and that
  is not an oversight"): `skill_set`, `skill_remove` and `cli_set` are back, one
  set per family, on §8's argument — an edit by a model is an edit through the
  same writer, the same parsers and the same refusals, and what a person gets in
  exchange is a file that says what happened. A skill is *still* installed by
  putting a directory in a folder; the tool is what writes the directory, and
  the only paths it accepts are ones that resolve inside that skill's own
  (`slife2.skills._within`), because a playbook is written by a model and one of
  its `path` values becomes a path on the operator's machine — the same boundary
  §8 draws, in the one family where the model names a file rather than a field.
  What is left of the *reading* family is one tool per `cli:` entry, and that is
  the family's real decision, already made: an entry becomes **a tool the
  operator's own config gave the model**, run as an argv rather than through a
  shell so that the arguments a model invents cannot become commands it
  invented. It is also what opens the approval question above — `yt-dlp` writes
  files — and §8 already says where that answer goes.
- **A measurement of the tool budget.** This bullet used to name a missing
  feature — everything enabled in `tools:` in every model call's list, with
  `enabled: false` as the only lever. On-demand loading landed (§8, and the
  README's own section): `tool_search`, `func_tool_load`, `tool_load.threshold`,
  `autoload: true`. What is left is a measurement rather than a gap: nobody has
  watched a real twenty-server config against the budget, and the threshold of
  100 is still v1's number.
- **Device-code OAuth for tool servers.** v1's gateway ran the whole RFC 8628
  flow, kept tokens in the OS keyring through `credstore`, and held the token
  beside the configured headers so a re-auth did not churn them. Not ported:
  `${VAR}` headers cover a token somebody already has, and FastMCP's own `auth:`
  key passes through to the SDK's browser flow for the rest. The gap is the
  headless case, where the browser flow has no browser.
- **Digesting an oversized tool result.** A tool can return more text than the
  conversation can hold, and nothing here bounds it. The store already has the
  pattern for the turn record — an oversized result becomes an announced
  head-and-tail digest — and the same rule belongs on the way *into* the model,
  not only on the way into the record.
- **Subagents.** One seam of the two this paragraph used to claim is actually
  in: `Loop.records` exists (`slife2.server.server.Loop`), so a worker's round
  trips are the ones not written to the log, and `send_message` already takes a
  `subagent` — a worker's conversation is a separate key with its own history,
  its own inbox and its own lock, in the same process. **`Loop.children` does
  not exist**, and neither does anything that hangs a worker off its parent, so
  a worker id *is* addressable from outside today: any caller may name any
  `(agent, subagent)`. That is the first thing a spawn tool has to decide, and
  it is the reason this bullet is a shape and not a seam list. What is not
  decided beyond it: how a spawn tool collects results, whether a worker can be
  multi-turn, how deep nesting may go, and whether cancelling a parent cancels
  its children.
- **Server-initiated push.** Not available at this revision through FastMCP:
  `subscriptions/listen` is the only push channel, it carries four
  change-notification types, and FastMCP 4.0.11 registers no handler for it at
  all. So an answer can only ride the request that asked for it — which is why a
  queued caller waits on its own call rather than being told later.
- **A client id on the turn record.** Nothing reads it yet, and adding a column
  to `turn` means deleting every existing `*.turn.db` (there is no migration
  layer, §5). The failure mode of getting it wrong is worse than the gap: the
  new INSERT would raise `OperationalError`, the store would report it
  as one caller's bad data, and every turn after the upgrade would run perfectly
  and never be recorded. Add it with the reader that needs it.
