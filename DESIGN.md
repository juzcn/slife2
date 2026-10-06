# slife2 — design

**A terminal agent whose components are MCP servers.**

This document records the decisions behind the first cut: what was chosen, what
was rejected, and — where it matters — the measurement that forced the choice.
It is written to be read before changing the code, because several of these
decisions look arbitrary until you know what happens if you undo them.

---

## 1. The shape

Four processes, three of them MCP servers:

```
slife2                    TUI, MCP client              (no provider key, no SDK)
  │  HTTP 127.0.0.1:8000/mcp
  ▼
slife2-agent              agent loop, MCP server       (no provider key, no SDK)
  │  MCP client
  ├── HTTP 127.0.0.1:8001/mcp ──▶ slife2-llm-openai     (openai SDK, holds a key)
  └── HTTP 127.0.0.1:8002/mcp ──▶ slife2-llm-anthropic  (anthropic SDK, holds a key)
```

Two properties fall out of this and are the reason for it:

- **A provider API key exists only inside the LLM server process that needs it.**
  The agent loop cannot leak one because it never has one.
- **The agent loop imports no provider SDK.** Its only backend talks MCP, so
  switching providers is changing a URL. `grep -r "import openai\|import anthropic"
  slife2/` matches exactly two files, both LLM servers.

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

## 4. Compatibility notes

Two things about the 2026-07-28 revision that the code depends on, both verified
by running against the real server rather than by reading:

- **`ping` is gone.** The protocol-level ping was removed, and a conforming
  server answers it with `MCPError: Method not found`. Both connection paths
  therefore probe with `tools/list`, which the migration guidance recommends as
  the liveness replacement and which additionally catches being pointed at the
  wrong MCP server.
- **`stream_options` and usage.** OpenAI sends token counts on a trailing chunk
  with an empty `choices` list. DeepSeek attaches them to the final chunk that
  *still carries a choice*. An adapter that handles only the documented OpenAI
  shape reports zero tokens against a real DeepSeek endpoint — which is exactly
  what happened here, and what a live call caught after the unit tests, built
  from synthetic OpenAI-shaped chunks, had all passed.

## 5. The agent loop

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

## 6. Deferred

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
- **Persistence.** Nothing survives a restart, on either side.
- **Tool approval.** `now` and `calc` are side-effect-free precisely so this cut
  does not have to answer it. A tool that writes a file reopens the question v1
  answered with a model-driven `_approve` parameter.
