# slife2 — design

**A terminal agent whose components are MCP servers.**

This document records the decisions behind the first cut: what was chosen, what
was rejected, and — where it matters — the measurement that forced the choice.
It is written to be read before changing the code, because several of these
decisions look arbitrary until you know what happens if you undo them.

---

## 1. The shape

Every component is an MCP server but the TUI — all of them brought up on demand
by `slife2` and shared by every instance (see §4):

```
slife2                    TUI, MCP client              (no provider key, no SDK)
  │  HTTP 127.0.0.1:8000/mcp
  ▼
slife2-agent              agent loop, MCP server       (no provider key, no SDK)
  │  MCP client
  ├── HTTP 127.0.0.1:8010/mcp ──▶ slife2-memory                 (one SQLite file per agent)
  ├── HTTP 127.0.0.1:8020/mcp ──▶ slife2-toolhub
  │                                 ├── :8030/mcp ──▶ slife2-builtins   (`echo`, `now`, `calc`)
  │                                 └── MCP ──▶    external tool servers, and REST via a proxy
  ├── HTTP 127.0.0.1:8001/mcp ──▶ slife2-llm-openai             (openai SDK, holds keys)
  ├── HTTP 127.0.0.1:8002/mcp ──▶ slife2-llm-anthropic          (anthropic SDK, holds keys)
  └── HTTP 127.0.0.1:8003/mcp ──▶ slife2-llm-openai-responses   (openai SDK, holds keys)
```

**One component, one job, and the granularity is deliberate.**  A model backend
speaks one wire protocol; memory keeps turns; the hub is where the tools come
from; the agent loop runs turns.  A provider is a row in a backend's config
rather than a process of its own, so three providers that happen to speak two
protocols are two model processes and not three — the smallness is in what each
process *does*, not in how many there are.  The count in the diagram is what one
config uses, not a fixed number: a protocol no provider speaks is not started at
all, and the hub is one process whether it fronts one tool server or twenty.

The builtins being a server of their own is the same rule applied to the one
place it looks like overkill: they have no credential and no network, and they
are still behind the hub, because "where the tools come from" is a job and a
component that is sometimes the answer to it is a component with a branch in it.
See §8.

The two OpenAI entries are the point worth checking, because they look like
duplication and are not.  **Responses is a different wire format, not a flag on
chat-completions** — different input items, differently-shaped tools, different
streaming events — so it is a protocol, and a protocol is a process.  Merging
them behind one `api` would put two adapters in one file and make the choice a
branch inside the server rather than a fact about the config.

Two properties fall out of this and are the reason for it:

- **A provider API key exists only inside the model server process that needs
  it.** The agent loop cannot leak one because it never has one. The same holds
  for tools, which is the second thing the hub buys: `SERPER_API_KEY` and
  `GITHUB_TOKEN` are read by `slife2-toolhub` and exported into the environment
  of a child process, and the agent that asks for the tool never sees either.
- **The agent loop imports no provider SDK.** Its only backend talks MCP, so
  switching providers is changing a URL. `grep -r "import openai\|import anthropic"
  slife2/` matches only files under `llm/` that are model servers — one per wire
  protocol, so three of them now, and nothing else in the tree.

The cost is one JSON-RPC hop per token on loopback. That is small and it is the
price of the architecture; `ProgressObserver` is where a coalescing fix goes if
it ever stops being small.

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
the returned value. It is also why `slife2.tui.widgets.Transcript` *discards*
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

So a conversation is an object, and the surface is two tools:

```
send_message(agent, subagent, prompt) → {text, usage, steps, stop_reason, model}
reset(agent, subagent)
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
  a busy loop *waits*, in arrival order, and becomes a turn of its own. Nothing
  is dropped and nothing is cancelled. The message is held in the loop's inbox
  and **not** in its messages until its turn begins — the loop re-reads the
  message list at every step, so a message appended on arrival would be read by
  the model mid-turn, which is steering nobody asked for.
- **Cancellation repair**, which the plan called the sharpest correctness edge in
  the system and which statelessness had deleted. A cancelled turn can leave an
  assistant message whose tool calls are only partly answered, and *that* list is
  a 400 from every provider — so the repair truncates back to the user's own
  message. The `+ 1` is load-bearing: the loop appends the user's message itself,
  so truncating to the snapshot would delete the very message this arrangement
  exists to not lose.
- **A conversation store that can grow without bound**, which is now the
  server's problem rather than the caller's. See §9.

**Every turn of a loop that has memory is recorded, cancelled ones included.**
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
created outside the cancelled scope does land, so the cancel path detaches the
write and logs its failure instead of dropping it. What that gives up is
ordering: if a queued turn follows immediately, it may write first. That is a
cosmetic inversion in `recent`, and it is the cheaper half of the trade — the
alternative is every queued turn waiting on the memory server.

**The id travels with the call, at every hop.** It is not only the agent server
that receives it: the memory write and the model call are both made under the
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

**A probe decides whether a server is alive.** Only `tools/list` answering with
the tool we expect proves that *our* server is up; a listening port proves
something is there, and a record file proves something was there once. So the
record is never consulted for liveness, and a stale one cannot wedge a start.

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

## 5. Memory

A component with one job: keep what was said. It does not summarise, does not
decide what mattered, and puts nothing back into a conversation. The schema is
v1's `turn` table, minus one column — one row per turn, columns for the two
token counts, the two timestamps and the identity that v1 arrived at by using
it. The missing column is `user_message`: v1 keeps the user's half beside the
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

**A missing server is a broken system, not a degraded one.** `slife2` starts
every component together and refuses to start at all if one of them will not come
up — before it draws anything, so the failure is two lines rather than a terminal
that can never connect. A peer that goes missing *later* takes the same answer:
the turn fails where the peer is used, rather than being answered and not
recorded.

That rule lives in one place, `slife2.mcp_server.open_server` — connect, prove the
server is the one you meant, raise otherwise. It is there because it had four
implementations and one of them was its own opposite: the LLM backend and the TUI
each probed and raised, and the agent server's memory client swallowed the
failure and latched itself off, which made the same situation fatal at startup
and silent a minute later.

One thing is deliberately not fatal: **a `ToolError` is not absence.** It means
the memory server answered and refused *this* request — an agent name that cannot
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
Both are the *model's* tools, both are the reason memory is a component the hub
asks rather than ours alone, and both are windows over `created_at` with the
grammar v1's `timeutil` implemented (ISO, `yesterday`, `last month`,
`3 days ago`), ported whole so a window means the same thing on both sides of
the schema. A bound in no known grammar is an error rather than an empty
result: SQLite answers an unrecognised string with no rows, and "no rows" is an
answer a caller believes.

**And a model reads only its own history, which is not something an argument can
say.** V1's memory server ran one process per agent, so the connection *was* the
identity. This one is shared — one process serves every client id, the way one
model server serves every provider — so the identity has to travel, and it
travels in the call's `_meta` rather than in its arguments (`slife2.audience`).
The agent binds the conversation it is running for when it builds the loop, the
hub forwards what it was given without reading it, and memory answers about the
conversation the call came from. A model that could name an agent could read
somebody else's memory, and the only thing standing in the way would be a
sentence in its own system prompt — which is an instruction, not a boundary.

Recall in the other sense is still deliberately absent: nothing yet decides
which past turns are *relevant*. Adding an index is a change to this file rather
than a change to what was kept.

## 6. Compatibility notes

Three things about the 2026-07-28 revision that the code depends on, all
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

**Where the model's tools come from, and the only process that holds their
credentials.** It is a port of v1's `mcp-gateway`, and the shape that survived
the port is the whole of it:

```
slife2-agent  ──MCP──▶  slife2-toolhub  ──MCP──▶  components           (ours; `builtins`, `memory`, …)
                          list_tools        └──▶  external tool servers (stdio or http)
                          call_tool
                          servers
```

**Two sources, and the hub is the only thing that knows both.** The *components*
are the servers slife2 starts — `Config.components()`, which is also what the
launcher starts, so there is no list of them here to go stale. The *tool servers*
are everybody else's, from `tools:` and `rest-api:`. Which source a tool came
from is not what decides who may call it; which *caller* it is for does, and that
is said on the tool itself (`slife2.audience`) rather than in its name, in the
config, or in the hub. A component's tools belong to that component's own code
until one of them declares itself the model's — `remember` writes into any
agent's database and `send_message` drives another conversation, and those are
exactly the tools a model would reach for if it could read their descriptions —
while an entry under `tools:` needs no mark, because the operator opted in by
writing it down. The default is the safe half on purpose: a forgotten mark costs
a tool that is absent, not a tool that is dangerous.

**A call can say who it is on behalf of, and the hub passes that on without
reading it.** Memory is the case that needs it: the model may browse its own
history and must not browse anybody else's, and one memory server serves every
conversation in the system. So the conversation rides in the call's `_meta`
rather than in its arguments — off the schema the model reads, out of reach of a
prompt that asks for somebody else's turns — and the hub, which cannot act on it
and could not use it, forwards exactly that key and nothing else of `_meta` (the
protocol's own keys name *this* request's progress stream, and a proxy has no
business passing those on). §5 has the rest.

**The hub's own tools are the agent's API and never the model's.** Like the
memory server's `remember` and `recent`, the model never sees `list_tools`,
`call_tool` or `servers`; it sees the *proxied* tools, under `{server}__{tool}`
names. That indirection is what keeps the hub's surface constant: a server
coming and going changes what the model may call without changing anything about
the hub's own protocol.

**Nothing is served by the hub process itself, and the builtins are why that is
worth saying.** `echo`, `now` and `calc` have no credential, no config and no
network, so a hop to reach them buys nothing — and they are behind one anyway,
served by `slife2-builtins` and reached through exactly the code path that
reaches arxiv. The alternative, a hub that serves a few tools itself, is the
second mechanism this whole arrangement exists to avoid: those tools would not be
in `servers()`, they would not have a connection that can fail, they would not be
in whatever a tool search is eventually built on, and the first thing to drift
would be the one place the tool table has a branch in it. What the hop costs is
one loopback call per model call; what it buys is that "where the tools come
from" has one answer and no exceptions.

**A tool list is one thing and it has one owner.** Provenance (whose tool is
this), the naming rule that keeps two servers' `search` apart, and — the first
time it appears — which tools may run without asking, are all questions about
the *set*, and a set assembled in two places disagrees with itself. That is why
the agent holds no registry of its own and why the builtins are not exempt from
it.

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
we asked. There is no connection state machine and no timer: the snapshot is
dropped when the peer says `tools/list_changed`, when a call fails at the
transport, or when a connect fails, and the next ask re-reads it.

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
component gone and fails the turn, like memory. A missing *upstream* is the
operator's configuration and somebody else's process: reported by `servers()`,
left out of the tool list, and retried on the next ask. An upstream *refusing a
call* is one caller's bad data. Collapsing these is how a config mistake becomes
an outage, and separating them is most of what the module's prose is about.

**A component is not an upstream, and the difference is a flag.** Everything
under `tools:` is somebody else's and optional; a component is started by slife2,
so a hub that cannot read its tool list *refuses to list anything* — because a
model that has quietly lost `now` and `calc` is a failure nobody can see, and a
shorter tool list is exactly what that failure looks like. It is one `required`
flag on the connection rather than a branch in the tool table, and it is read
twice: for that failure rule, and for whether the server's tools have to declare
themselves the model's. The builtins are the worked example of it being ordinary
— the URL, the connection, the snapshot, the naming and the mark are all
arxiv's, or would be if arxiv had anything to declare.

There is deliberately no list of "components worth asking". The hub asks all of
them, including the three model backends and the agent server, which have
nothing to offer: which tools a server has is not knowable without asking, and a
second list is a list that goes stale the first time somebody adds a tool.

**REST APIs are not a second mechanism.** A `rest-api:` entry is expanded *by
the config layer* into the stdio command that serves it — `uvx mcp-openapi-proxy`
with the environment it reads — so what reaches the hub is an ordinary upstream
and nothing in the hub knows that REST exists. That wrapper is v1's, kept because
it is what the ecosystem publishes and because writing an OpenAPI-to-tools
converter here would be a large feature that is wrong in interesting ways.

What was deliberately **not** ported: v1's `mcp_set`/`mcp_remove` tools, which
let the model write its own `tools.yaml`. slife2's config is one file read by
every process and by the launcher, and the launcher already refuses to let a
command line name an arbitrary program; a language model choosing one is the
same capability with a worse author.

## 9. Deferred

Named so they are decisions rather than oversights:

- **Markdown rendering.** The transcript shows model output as plain text.
- **`thinking` deltas.** Both SDKs expose them cheaply; rendering a model's
  private reasoning as its answer would be worse than not showing it, so they
  are dropped until there is a display decision.
- **Delta coalescing.** One notification per token. The seam is
  `ProgressObserver`.
- **History trimming, and now it is the server's problem.** A long conversation
  grows without bound. It used to grow in the caller's list, which made it
  something a caller could feel and bound; it now grows in the loop, and the
  caller re-sends nothing, so nothing feels it. This is the first thing to do
  next, not a note that can sit.
- **Bounds.** A loop caps its inbox (`MAX_QUEUED`), because a client's call
  timeout starts when the call is made and an unbounded queue is therefore an
  unbounded wait — and a wait longer than the timeout closes the stream and
  cancels the turn, which is the very way a message gets lost. Nothing yet caps
  how many loops exist, and nothing bounds a loop's history.
- **Recall.** Memory stores turns and returns them by time; nothing yet
  decides which past turns are *relevant* to the one in hand.
- **Tool approval.** `now` and `calc` are side-effect-free precisely so this cut
  does not have to answer it. A tool that writes a file reopens the question v1
  answered with a model-driven `_approve` parameter — and a tool that spawns a
  subagent is the first such tool this design has an obvious use for. §8 says why
  the hub is where the answer goes: it is the one place that knows the whole set,
  and the only one that could hold a per-tool policy without the agent learning
  what a tool server is.
- **A tool list that is too long.** Everything enabled in `tools:` is in every
  model call's list, and the working config enables twenty servers — hundreds of
  tools, which is a large request, a large bill, and a model choosing worse.
  v1's answer was on-demand loading: a `tool_search` the model runs, then
  `func_tool_load`, with `tool_load.threshold` (100 in v1's config) as the point
  where tools start being evicted and `autoload: true` to exempt one. That is a
  real feature and not a port, because it needs a catalog the hub does not have;
  until then `enabled: false` is the lever, and it is a per-server one.
- **Device-code OAuth for tool servers.** v1's gateway ran the whole RFC 8628
  flow, kept tokens in the OS keyring through `credstore`, and held the token
  beside the configured headers so a re-auth did not churn them. Not ported:
  `${VAR}` headers cover a token somebody already has, and FastMCP's own `auth:`
  key passes through to the SDK's browser flow for the rest. The gap is the
  headless case, where the browser flow has no browser.
- **Digesting an oversized tool result.** A tool can return more text than the
  conversation can hold, and nothing here bounds it. Memory already has the
  pattern for the turn record — an oversized result becomes an announced
  head-and-tail digest — and the same rule belongs on the way *into* the model,
  not only on the way into the database.
- **Subagents.** The shape is decided and the seams are in: one agent has one
  loop with memory, plus N worker loops that have none. `Loop.records` and
  `Loop.children` exist for it, workers hang off their parent so a worker id is
  not addressable from outside, and a worker's answer arrives as a tool result
  because tools already return text. What is not decided: how a spawn tool
  collects results, whether a worker can be multi-turn, how deep nesting may go,
  and whether cancelling a parent cancels its children (it depends on
  `children`, which is why that link had to be in from the start).
- **Server-initiated push.** Not available at this revision through FastMCP:
  `subscriptions/listen` is the only push channel, it carries four
  change-notification types, and FastMCP 4.0.11 registers no handler for it at
  all. So an answer can only ride the request that asked for it — which is why a
  queued caller waits on its own call rather than being told later.
- **A client id on the turn record.** Nothing reads it yet, and adding a column
  to `turn` means deleting every existing `*.turn.db` (there is no migration
  layer, §5). The failure mode of getting it wrong is worse than the gap: the
  new INSERT would raise `OperationalError`, the memory server would report it
  as one caller's bad data, and every turn after the upgrade would run perfectly
  and never be recorded. Add it with the reader that needs it.
