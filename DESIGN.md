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
  ├── HTTP 127.0.0.1:8001/mcp ──▶ slife2-llm-openai             (openai SDK, holds keys)
  ├── HTTP 127.0.0.1:8002/mcp ──▶ slife2-llm-anthropic          (anthropic SDK, holds keys)
  └── HTTP 127.0.0.1:8003/mcp ──▶ slife2-llm-openai-responses   (openai SDK, holds keys)
```

**One component, one job, and the granularity is deliberate.**  A model backend
speaks one wire protocol; memory keeps turns; the agent loop runs turns.  A
provider is a row in a backend's config rather than a process of its own, so
three providers that happen to speak two protocols are two model processes and
not three — the smallness is in what each process *does*, not in how many there
are.  The count in the diagram is what one config uses, not a fixed number:
a protocol no provider speaks is not started at all.

The two OpenAI entries are the point worth checking, because they look like
duplication and are not.  **Responses is a different wire format, not a flag on
chat-completions** — different input items, differently-shaped tools, different
streaming events — so it is a protocol, and a protocol is a process.  Merging
them behind one `api` would put two adapters in one file and make the choice a
branch inside the server rather than a fact about the config.

Two properties fall out of this and are the reason for it:

- **A provider API key exists only inside the model server process that needs
  it.** The agent loop cannot leak one because it never has one.
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
caller:  tools/call run_turn(...)
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

1. **Request scoping.** Progress carries `related_request_id`, so an event is
   attributable to the call that caused it. `notifications/message` has no
   request correlation at all — with two turns in flight the stream is
   uninterpretable.
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

## 3. Statelessness

**Every server is stateless; the caller owns the conversation.**

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

So the server became a function:

```
run_turn(messages, prompt) → {text, new_messages, usage, steps, stop_reason}
```

The caller sends what was said and gets back what to remember. Three things
disappeared rather than being solved:

- a conversation store that could grow without bound,
- a per-conversation lock, because two turns can no longer race over one list,
- **cancellation repair**, which existed only to stop an interrupted turn leaving
  a corrupt shared history. There is no shared history to corrupt: a cancelled
  turn returns nothing and the caller's history is whatever it already had. The
  plan called this the sharpest correctness edge in the system; statelessness
  deletes it.

`stateless_http=True` follows from the same decision. So does the absence of a
`reset` tool — the client clears its own list, and the MCP surface is exactly
one tool.

**Rejected:** server-minted opaque handles (the pattern the 2026-07-28 migration
guidance recommends for cross-call state, e.g. `create_basket() → basket_id`).
It is the right answer when state must outlive a client or be shared between
them. Here there is one client, the state is the conversation it is already
displaying, and a handle would add a store, a lifetime policy, and an
expiry-error path to buy nothing.

## 4. Shared servers, and the launcher

The servers are **shared infrastructure**, not four terminals a user babysits.
`slife2` brings up what its config needs and attaches to whatever is already
running, so a second instance is one command and no duplicate process appears.

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
v1's `turn` table, taken whole — one row per turn, the user's message in a column
of its own so it can be searched and embedded apart from the answer, and columns
for the two token counts, the two timestamps and the identity that v1 arrived at
by using it. Everything is stored as it happened, with one deliberate exception
(an oversized tool result becomes an announced head-and-tail digest), so a
question the schema cannot answer today can be asked of the same rows later
without a migration — and the columns the later features need already exist,
because adding a column to a table with rows in it is the one change this schema
has no mechanism for.

**Agents are isolated by file.** `agent="jack"` reads and writes
`jack.turn.db`, and no query can reach another agent's turns because there are
no other agent's turns in the file. Isolation as a property of the filesystem
beats isolation as a `WHERE` clause somebody can forget to write — and it is the
one place `--agent` partitions anything, since the servers themselves stay
shared.

**A write failure never fails a turn.** Memory is an enhancement, not part of
correctness: a store that is down, a disk that is full, a name that cannot be a
filename — none is a reason for a conversation that succeeded to be reported as
failed. Every failure is logged and swallowed.

Two things about that turned out to need care:

- **Absence has to be remembered, not re-tested.** The write being free
  *logically* did not make the attempt free: building an MCP client against a
  dead port spends a couple of seconds inside the transport's own retries, on
  the first answer of every session. So the port is asked before the protocol —
  a refused TCP connect returns at once — and the answer is kept for the life of
  the process. Measured, 2.15s to 0.27s.
- **A `ToolError` is not absence.** It means the memory server answered and
  refused this one request — an agent name that cannot be a filename, say — and
  that is one caller's problem rather than a reason to stop recording for
  everyone.
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

Recall is deliberately absent. Retrieval is by time — `recent` — which is the
honest thing for a component that stores without judging, and adding an index is
a change to the file rather than a change to what was kept.

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

## 8. Deferred

Named so they are decisions rather than oversights:

- **Markdown rendering.** The transcript shows model output as plain text.
- **`thinking` deltas.** Both SDKs expose them cheaply; rendering a model's
  private reasoning as its answer would be worse than not showing it, so they
  are dropped until there is a display decision.
- **Delta coalescing.** One notification per token. The seam is
  `ProgressObserver`.
- **Sender-side history trimming.** A long conversation grows without bound
  because the caller re-sends it every turn. The server could return a
  compacted history instead.
- **Recall.** Memory stores turns and returns them by time; nothing yet
  decides which past turns are *relevant* to the one in hand.
- **Tool approval.** `now` and `calc` are side-effect-free precisely so this cut
  does not have to answer it. A tool that writes a file reopens the question v1
  answered with a model-driven `_approve` parameter.
