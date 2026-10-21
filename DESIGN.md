# slife2 — Design Decisions

This is the working document behind the two normative documents:

- [README.md](README.md) — what the system offers and how to use it;
- [SDD.md](SDD.md) — how the system is built and behaves.

**SDD is authoritative; this document is the working record.**  It holds the
decision, the alternatives rejected, the measurement that settled it, and what
breaks if the mechanism is removed. Where a rule matters, it points at the SDD
section that states it rather than repeating it. Where this document and SDD
disagree, SDD wins; where SDD and the code disagree, the code wins.

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

**Rule:** SDD §2.4.

A capability that holds a credential, a provider SDK, or another party's
connection is a **plugin**: a process. A capability that holds only a file is a
**library**, imported by the process that owns it. A mechanism shared by more
than one store is a library.

The cost of a library is the error boundary: a missing plugin fails the turn
with a named peer; a file that will not open raises where it is used. That is
the right trade for a file and the wrong one for a service.

### 1.2 One plugin, one job

**Rule:** SDD §2.6 and Appendix B.

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

The plugin/job split is the reason a provider is a row rather than a process:
the smallness is in what each process does, not in how many providers it
serves.

### 1.3 One registry, everything derived

**Rule:** SDD §3.5.

The module path is a string rather than an import because importing a server to
read its name would pull a provider SDK into the terminal and the agent loop.
One test imports each row's module and checks that it binds the name the row
claims.

### 1.4 Shared servers

**Rule:** SDD §4.2; the runtime layout is SDD §11.4.

Three decisions are argued here rather than restated:

- **A kernel lock is a lock.** A Windows named mutex or a POSIX `flock` is
  released by the operating system when the holder dies. There is no
  stale-lock protocol.
- **A child must escape its parent's job on Windows.** Without
  `CREATE_BREAKAWAY_FROM_JOB`, a daemon dies with a kill-on-close job owned by
  a CI runner or an IDE task host.
- **A wave reports what it waited for.** A server whose dependency failed is
  not attempted and is reported with the name of what it was waiting for,
  because a second failure describing the first is noise and a doomed start is
  a process to clean up.

### 1.5 Client identity and home

**Rule:** SDD §3.2.

State is keyed by a `(agent, subagent)` client id that travels out of band in
`_meta`, because a server is shared and a model must not be able to name whose
conversation a call belongs to.

MCP at the 2026-07-28 revision has no protocol-level sessions. Measured against
the installed library:

```text
in-memory transport:   call 1 -> fba941ad-...   call 2 -> 719b5744-...
real HTTP, same client: call 1 -> 8e0a6a61-...   call 2 -> 3c80209a-...
incoming headers:      {}   <- no mcp-session-id
```

`session_id` is a fresh UUID per request. Keying state on it is keying state on
a header that no longer exists.

The alternative recommended by the session-removal SEP is a server-minted
handle passed as an argument. That was rejected because anything a caller must
keep can be lost, expired, or wrong after a restart, and every caller then
needs a path for that. The client id is a name the caller already has. It
cannot go stale. A server creates state on first use, and an idle sweep may
drop it; the next call starts it again and the caller cannot tell.

The price is that a name is guessable. Here there is no authorization to
subvert: every server listens on loopback, and the only other party is a
process the user started.

### 1.6 Configuration is the description

**Rule:** SDD §4.1 and §11.1.

The loader is the judge, so an entry a model writes is an entry an operator
could have written. A refused entry is restored and the caller is told why.

### 1.7 The second cost: message size

**Rule:** SDD §3.8.

MCP `tools/call` is request/response. The client SDK refuses an SSE event over
1 MiB (`DEFAULT_MAX_SSE_EVENT_SIZE`). A real session reached it with
`send_message`.

The ceiling is entirely on the client side. The server bounds neither a body nor
an event.

**Decision.** Our own hops use a transport with no ceiling. The gateway to
external servers keeps the SDK's ceiling.

The ceiling protects a process from a peer making it buffer an unbounded
message. A daemon this system started is not such a peer; the largest message it
can produce is bounded by the model's context, because `rebuild` and `transcript`
send exactly that list. An external server is a peer, so its ceiling stays.

The cost is that an overrun in our own hop becomes a memory failure rather than
a named SSE error. That trade is acceptable only because the peer is a process
this system started.

## 2. Conversation and context

### 2.1 The loop is a pure function

**Rule:** SDD §5.1.

The iteration cap is opt-in. `agent.max_iterations: 0` and an absent key both
mean no cap. A number nobody wrote is a bound nobody chose. A turn that reaches
a stated cap ends normally with `stop_reason: "max_iterations"`, not with an
exception.

### 2.2 A turn is an object, not a call

**Rule:** SDD §5.1 and §5.5.

A synchronous `run_turn(messages, prompt)` is enough for one client that
displays the conversation it is running. It is not enough when a message arrives
mid-turn, because the new message must be able to wait rather than displace the
turn in flight. It is also not enough when a caller is not displaying the
conversation, such as a subagent.

Every key in the answer is always present, including `refused` on the path
where nothing could be refused. A caller that has to test for a key gets a
`KeyError` on the one branch nobody exercised.

### 2.3 The mid-turn message is a tool pair

**Rule:** SDD §5.3.

It is deliberately not appended as a user message. The loop re-reads the list at
every iteration; a user message would be read as the turn's own input, arriving
silently, and in the middle of an unanswered tool exchange it is a 400 from
several providers. The model reads the pair as what it is: something that
arrived while it was working.

### 2.4 The context is selected, not accumulated

**Rule:** SDD §5.5.

The live-context list is an ordered array of turn ids in the turn file, written
in the same transaction as the turn that changed it. Its order is
authoritative: reads replay it as written and never re-sort, because the list
already encodes what was kept, dropped, and recalled, and a selection is not
always contiguous.

Kept and recalled turns are joined by id and never reconciled. An empty recall
is harmless; an overriding selection would discard the context it replaced.

The common answer is `{}`: a decision that asks for exactly what is in hand
rebuilds nothing. That is what keeps an expensive call from being paid for
nothing.

Measurements on the live turn log:

| Input shape | Keyword leg result |
|---|---|
| A sentence-shaped query | Fired on 0 of 16 queries; most reached only the turn they were typed into. |
| A word-shaped query | Fired on 10 of 15; found the target on 9. |
| The meaning leg | Found the target in 32 of 32 recall queries, first in 25. |

So the keyword field asks for a word-shaped query: a sentence handed to that
leg would AND every word it contains and match nothing. The meaning leg does not
need one — a compact word list ranked the same as the sentence it came from on
the catalogue (36/36 and the same MRR) — but it is no longer forced to read
one, because the recall now has two spellings: `sentences` are read by meaning,
`query` is the words the keyword leg ANDs and the meaning fallback when there
is no sentence. Several sentences are several questions: each is embedded on
its own, the keyword leg is fused into each question's list, and a turn keeps
its best question — a later sentence can only add a turn or raise one, never
cost a turn an earlier sentence found. The old shape fused one keyword list
against one merged meaning list, which credited a turn by its best similarity
across questions rather than by how well it answered the question the words
belonged to.
When the decision asks in sentences, the person's input is a second question —
the model's sentence is a paraphrase of what the input points at, and the
meaning leg reads the input as it stands — unless the decision says the input
names nothing (`prompt_as_sentence: false`, for "hi" or a follow-up), which is
a judgement only the model that sees both can make. A keyword-only recall keeps
its query as the one meaning question, and a period recall is not narrowed by
the input.

On the rebuilt log the 30-query set answered at hit@1 0.93, hit@10 0.97 and MRR
0.94. Ten hand-written hard sentences were less decisive — phrase-only MRR
0.82, sentence-only 0.71, both 0.76 — recovering the two artifact/output cases
and costing one English subject phrase. The sentence is a shape the model may
use, not one it must use.

**Failure policy.** A timeout, provider error, or unreadable reply is not a
decision. The turn runs on the context it already has. A missing store fails the
turn. There is no switch to turn the decision off: an append-only context is
bounded by nothing, and a note saying `kept 12, recalled 0` would describe a
decision no discriminator made.

**Restore.** A restored list is replayed verbatim, with no ceiling re-slicing.
Each turn carries a `[TURN:{…}]` footnote naming its id, channel, and span. The
footnote is added when a message list is built and never stored, so it cannot
drift from its row. Capitals mark a harness annotation, never the conversation's
own words.

The newest turn is annotated after it is saved, with the same timestamps and
channel the row was written with, so the live spelling and the rebuilt spelling
are byte-identical. A difference would cost a prompt-cache miss on every
rebuild.

### 2.5 Cancellation and atomic batches

**Rule:** SDD §5.6.

An interrupted turn keeps what it completed. A tool batch cut short is
*completed* rather than dropped: the calls that already returned keep their own
answers, and every call that produced no answer is answered
`(Tool execution interrupted)`. The assistant message is written together with
that complete set of answers, so the history never holds a tool call without a
result — the one state every provider rejects — and a later "continue" resumes
from the work that was done.

Running a call cannot raise; the registry turns failure into text. Fitting a
result can: the fit is the caller's callback, and a cabinet that will not open
or a discriminator that stalled raises there. Writing the assistant message
before fitting leaves exactly the rejected history behind. A failed turn keeps
its messages, so that conversation would fail every later turn.

An interrupted turn's row is still written. The rule is not "remember to record
the cancel path"; a write conditional on how a turn ended is one somebody can
forget. The row holds whatever the turn had completed when it was cut off — the
person's message alone, or the exchange up to the interrupted batch. Its id is
claimed on the next turn (`settle_record`), so the interrupted turn carries a
`[TURN:…]` footnote like any other and a keep-list can name it.

A write from a cancelled handler is detached, because a plain `await` in a
cancelled scope is cancelled again at its next checkpoint. The ordinary write
happens inside the lock, so a queued turn cannot start before the write that
maintains the live-context list has landed. **A write the store or the cabinet
refuses is a hard failure**: the turn is unrecorded, so its messages are rolled
back to the last backed prefix and the caller sees the refusal. There is no
silent turn that the transcript shows and no later turn can address.

### 2.6 Images

**Rule:** SDD §5.7.

An attached image is live state **and a cabinet document**. The live user message
keeps the `image_url` parts the model reads; when the turn is recorded, each part
becomes an `[Image:{…}]` reference to a cabinet copy (`image:` kind, keyed by its
content digest). The pixels never enter the turn row. A rebuilt conversation is
text plus those references, and a model that needs the picture again calls
`attach_image` with the reference's key — the cabinet answers a `data:` URL — or
with its source URL. A row never names an image the cabinet does not hold: a
cabinet refusal fails the turn like a store refusal.

A failed image attachment is dropped from the live conversation with the same
note the record keeps, and the error says so. A provider that refuses an image
refuses every request that carries it; a failed turn's messages are re-sent by
every later turn, so one bad attachment could otherwise make the conversation
unusable. Such a turn is note-only by design: the image was dropped before the
turn was recorded, so the cabinet never saw it.

## 3. Model backends

### 3.1 One process per wire protocol

**Rule:** SDD §6.1.

The embeddings server is separate because no chat provider can serve an
embedding model, and because it is the other component that holds a provider
SDK and a provider key.

### 3.2 Streaming

**Rule:** SDD §6.3.

The progress channel is chosen over `Context.log()` for five reasons:

1. progress is scoped to the call that caused it, so two turns in flight cannot
   be confused;
2. delivery is opt-in: a client that supplied no progress token costs the server
   nothing;
3. logging is filtered by a negotiated level, so a client that never negotiated
   one loses deltas silently;
4. FastMCP mirrors log messages into the server's own log, so every token would
   be logged at DEBUG;
5. the 2026-07-28 revision deprecates `logging`/`notifications/message` and
   requires a server not to emit a log notification for a request that did not
   ask for one.

### 3.3 Provider differences are configuration

**Rule:** SDD §6.4.

### 3.4 Protocol compatibility

**Rule:** SDD §3.8.

The revision was measured against FastMCP 4.0.11: `Client` exposes no call that
opens `subscriptions/listen`, and a FastMCP server advertises
`tools.listChanged: false` and answers the listen request with `Method not
found`. That is why the hub re-reads an aged listing instead of waiting for a
notification.

### 3.5 Usage and cache accounting

**Rule:** SDD §6.5.

OpenAI sends token counts on a trailing chunk with an empty `choices` list.
DeepSeek attaches them to the final chunk that still carries a choice. An
adapter that handles only the documented OpenAI shape reports zero tokens
against a real DeepSeek endpoint; a live call caught that after synthetic
OpenAI-shaped unit tests had passed.

## 4. Tools

### 4.1 The hub owns the set

**Rule:** SDD §7.1 and §7.2.

Every question about the tool set — what exists, what the model is holding, what
a name resolves to, what the budget takes back, who may call what — is a
question about the whole list, so it is answered in one place. A model that has
quietly lost `turn_read` is a failure nobody can see.

### 4.2 Names

**Rule:** SDD §7.3.

### 4.3 The loaded set and the budget

**Rule:** SDD §7.4.

Recency is learned by the hub, not reported by the model: the hub is the process
that routes the call, so it stamps `last_used` beside the call it just routed,
including refused calls. The stamp is written before the answer is returned, so
it cannot land after the trim.

The threshold is 100 and has not been re-measured against a real twenty-server
configuration. On-demand loading is the mechanism; the observation that would
move the number has not been taken.

**The catalogue's size is a second number and the status bar shows it.** The
list the model holds is a subset — skills and CLI entries are rows with no load
state at all — so a count of it answers a different question from "how many
tools are there". `list_tools` carries both: `tools` is the list, `total` is
every row in the file, a down source's included. Carrying it on that answer is
what keeps the number current without a timer: the ask that reads the list is
the ask that has just refreshed the declarations, and the hub's freshness rule
is "the next ask tries again" (`slife2.plugins.toolhub.server`). The window's
start-up ask returns the same total, so the bar has a number before a turn
exists, and each model call replaces it.

### 4.4 Search

**Rule:** SDD §7.5.

Measurements on the live catalogue:

| Measurement | Result |
|---|---|
| Split inputs vs one string (253 tools) | 20/20 in the page vs 18/20. |
| Keywords-only reaching the meaning leg | 34/36 vs 23/36 for the keyword leg alone; nine empty answers became none. |
| Word list vs sentence on the meaning leg (130 tools) | Identical: 36/36 and the same MRR. |
| Two sentences ranked by fused score | An unrelated second sentence moved a first question's fifth answer to rank 9. |
| Keyword leg at 130 vs 253 tools | At 130 it added nothing; at 253 it found one row the meaning leg missed and improved MRR. |

Two sentences are two questions. Ranking across questions with `1/(k+rank)` is
wrong: a row present in both lists outranks a row that is first in one. The page
is therefore ordered by:

1. rows the caller's own words matched;
2. then by meaning, descending.

The words come first because cosines cannot separate a near-duplicate cluster:
`set_table_column_width` and `..._widths` measure 0.63 and 0.62. Only the word
match can say which one was named.

Chinese text is indexed character by character and queried as a phrase. FTS5's
tokenizer would otherwise treat a contiguous Chinese run as one token. Character
tokens and bigrams measured the same on the catalogue's Chinese documents.

The answer names its confidence in three tiers because the measurements overlap:

- answerable queries had a best-on-page meaning no lower than 0.477;
- unanswerable queries had no value higher than 0.512;
- the overlap is 0.035, so no single cutoff is exact.

Every row carries the meaning score and a word-match marker. The marker says
whether the match was in the visible name or description, or behind it in the
body or parameters. A mark whose evidence is not on the page is a mark the
reader has to take on faith.

### 4.5 Result contract

**Rule:** SDD §7.6.

The switch from prose answers to objects was paid for in schema length. The
model-facing descriptions grew from about 5,500 to about 7,500 words, almost all
of it `Returns:` sections naming the keys of the object. That is the price of a
model being able to use an answer without inspecting it first.

**Two answers are text rather than JSON, and both are the thing itself.**
`_check_new_input` hands over a person's message; `skill_use` hands over the
skill's `SKILL.md`. Neither is data *about* something, so neither is wrapped. A
long playbook is not paged here: it is an ordinary oversized result, and the
dump discriminator chooses the lines the conversation carries — the same
backstop every tool result has.

### 4.6 Meta-parameters

**Rule:** SDD §7.7.

The strip is a copy: the tool is dispatched with one dict and the history keeps
the original, because removing them from history would rewrite what the model
said.

## 5. Memory and search

### 5.1 Turn store

**Rule:** SDD §8.1.

Agents are isolated by file. No query can reach another agent's turns because
there are no other agent's turns in the file. The home the call came from
selects which of the agent's directories is read or written.

### 5.2 Index machinery

**Rule:** SDD §8.2.

Two measurements decided the rules:

- What a turn is about is its own voice: the question, the model's reasoning,
  and the answer it settled on. Tool results were measured at 56–99% of a
  turn's text, so embedding them makes every turn describe "an agent ran
  tools"; with chunking the cost is that evidence chunks crowd the `k` a
  turn-level answer is chosen from. Tool-call arguments followed them, and this
  time the reason was measured rather than assumed: on 41 turns and 30
  discriminator-style subject queries, dropping the per-call argument head
  raised MRR from 0.83 to 0.88, adding the reasoning raised it to 0.92 (hit@1
  0.77 to 0.90), and keeping the full arguments cost 2.7x the chunks to recover
  one query. The reasoning had been invisible to both legs — 83.6k characters
  against a 73.9k semantic text. The evidence is not lost: the keyword index
  keeps the tool calls and results whole, because a path, an error string or a
  command is named literally by the query that wants it. The two legs are
  complements — exact things on one side, what the turn was about on the other.
- A change to a composition is an identity change: `EMBED_TEXT_VERSION` is
  folded into the vector identity, and `SEARCH_TEXT_VERSION` into the keyword
  stamp beside `RULES_VERSION`. Either way the index is rebuilt at startup
  rather than mixing rows made from two different texts.
- The token estimator uses `tiktoken`'s `o200k_base` when the vocabulary can be
  fetched, and a script-aware character measure when it cannot. The character
  measure was measured at about 25% high on English and 60% high on Chinese,
  which is a bias rather than a tolerance. Measured usage is unaffected either
  way: the provider's own count is what the record keeps.

Embeddings are a hard dependency. A turn is written with its vector in one
transaction, so a save that cannot embed stores nothing. That is why there is no
background drainer and no "index not ready" state.

### 5.3 The cabinet

**Rule:** SDD §8.3.

A subject that is not already lowercase carries a short digest in its filename.
`Python.md` and `python.md` are one file on Windows and macOS; two notes sharing
one file would let the second write replace the first's markdown while both rows
stayed correct. The readable half is kept and a digest is added.

A file's bytes are never indexed. What is indexed is what the file is: its name,
the paths that place it, and a summary once one is written. A file embedded from
its summary alone left every summary-less file with no vector, which the sync
pass then reported forever as a document it could never catch up.

### 5.4 Oversized results

**Rule:** SDD §5.2.4 and §8.3.

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

**Rule:** SDD §5.2.2, §5.4, and §9.2.

A channel that cannot reach a conversation says so and stops. It does not try a
second road, because "carry on without the window" is the fallback that hides a
broken system.

### 6.2 WeChat

**Rule:** SDD §9.1.

It is the plugin that owns no configuration section and declares no catalogue
rows. What it holds is a credential and a live conversation per agent, which is
the plugin side of the plugin/library table, and nothing else. Its four tools
are its own model-facing tools.

One WeChat account has one live login. Scanning the account for a second agent
retires the first login; the bridge answers it with `session timeout`, a
rejection wearing HTTP 200. Both error envelope shapes are read, so the first
loop ends, the window is told, and the model is given the bridge's own words.

There is no `wechat:` config section. A machine that should not talk to WeChat
is a machine where nobody scans a code; there is no saved login to pick up.

### 6.3 Peer agents over A2A/MQTT

**Rule:** SDD §9.2.

The agent name is the MQTT client id and the last segment of the request topic.
A subagent is not on this network: it has no external identity, and its only
network is the local one under its maker. Two processes answering to one name
trade the session back and forth, and every task sent to it runs twice. So:

- one mesh per agent;
- a second live instance under one name is refused before joining;
- the collision check reads the retained card on the name's discovery topic.

A wildcard broker address (`0.0.0.0`, `::`, or empty) is refused. It is not the
machine's loopback broker and not another machine's; it is every interface, and
starting mosquitto there with anonymous access would publish every topic and
accept tasks from anybody on the network.

`provider:` is a hard constraint. Mosquitto is the only broker this build
speaks; any other value is a configuration error, not a silently disabled
feature.

#### 6.3.1 Group chat

A group is a named roster with an owner, on either network. The design choice
that carries it is the **directed event topic**: `event/{org}/{unit}/member:<name>`,
one per recipient, subscribed by that member alone. It was chosen over three
alternatives:

- **a per-group topic** needs dynamic subscriptions and a per-group subscribe
  lifecycle in the reconnect loop, and a member that missed the invitation is
  not subscribed to the messages that would teach it the group;
- **the request path** gives every group message a task lifecycle and a reply
  ladder it has no use for — a group message expects no answer, and the ladder
  would report `UNREACHABLE` for a conversation nobody owed an answer;
- **the broadcast wildcard** would deliver every group's bytes to every member
  of the namespace, and they would be dropped on receipt rather than at the
  broker.

The directed topic needs no subscription state beyond the fixed
`member:<own name>`, so a member that missed a control still receives the next
group message. The payload carries the roster and a **gid**; that is what lets
a message teach a group the receiver never saw invited, while a delayed message
cannot resurrect one it left. A tombstone remembers `(name, gid)` for the
transition where this member stopped belonging; only an owner's `group_set`
clears it.

Delivery is QoS 0, and the consequences are stated rather than softened: a
member that is not listening misses the message, the sender is never a
recipient (both skipped in the fan-out and suppressed by MQTT No Local), and
`group_member_list` is what this conversation has persisted, not a live check. The
state is per member because there is no shared database to own it; every
cross-member fact has to be a wire event, and every member has to be able to
converge from what it receives.

**A group chat is casual.** No member owes an answer; a member that stays
silent is not `pending` or `undelivered`, and `sent` is only a publish count.
A group message is not a task — only a direct `task_request` has an expected
answer — so a model that reads silence as an open task is reading the wrong
vocabulary into the group.

Two smaller rules follow from the rest of the system:

- **A fork is never a member.** It is a temporary copy of its maker's
  conversation and ends with the next start; a roster naming one would keep
  addressing a name nobody answers. `group_set` refuses it before publishing,
  `place_for_group` will not wake it, and the receive path drops its traffic.
- **Mentions are text.** `@name` and `@all` are attention, as in a chat group;
  the roster decides delivery. Parsing them into a filter would make a
  message's audience depend on its words, which is the one thing a group's
  roster must decide.

### 6.4 Agent tasks

**Rule:** SDD §9.3.

It is a separate mechanism from a subagent:

- a task runs on the agent's own context;
- it has no address of its own;
- it records nothing;
- it accepts no second message.

The task's context is a snapshot with an edge: `restore` reads the stored list,
and a turn is written only when it ends. The turn that called
`agent_task_start` is not in the snapshot. The description must restate the last
exchange; everything older is in the log.

A task runs one turn, so it makes one rebuild. Zero would make it the one turn
in the system whose context nobody decided.

A task's turns are not written to the database. What the conversation gains is
the answer, which arrives as a message and is recorded by the agent server.
Writing the task's own round trip as well would put the same answer in the
transcript twice.

A task may start another task, because the tool set is one catalogue and
nothing filters it. `agent-task.max_running` bounds concurrency, not total work
over time.

### 6.5 Scheduled tasks

**Rule:** SDD §9.4.

A scheduled task is durable, timed, and repeatable. An agent task is ephemeral
and one-shot. The distinction is the whole reason they are separate:

- a scheduled task's definition and ledger are the feature;
- an agent task deliberately records nothing and is allowed to vanish on
  restart.

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
another plugin's prompt.

Cron is `croniter`. The five fields are trivial and the corners are not:

- `day_or`: restricted day-of-month and day-of-week are a union;
- the DST rule;
- `strict` cross-field validation, which rejects `0 9 31 2 *` before it is
  stored as an enabled task that can never fire.

One module wraps the library and owns the timezone policy and error contract.
A fire follows the wall clock of its task's timezone. A repeated local hour is
the same fire only if its wall-clock reading equals the anchor's; comparing
instants alone would fire twice in a fall-back hour.

The ledger is one file per agent. The shared process finds agents by walking
`agents/*/schedule.db` across every known home. A ledger carries the home it
came from, because the home is a property of the request a shared process is
serving, never of the process.

### 6.6 Subagents

**Rule:** SDD §4.5.

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
   hop for every message, and a subagent's identity is already the client id.
3. **A loop in the shared agent server, addressed by the second half of the
   client id.** Chosen. The loop table already exists and is keyed by
   `(home, agent, subagent)`; the storage change is a directory.

The client id is the mechanism; the directory nesting is the consequence.
`agents/jack/subagents/researcher/` says who made whom because it is inside the
maker's directory.

A fork is a copy of the maker's conversation, cut back to the last completed
exchange. The copy must be sendable: the maker is mid-turn when it calls
`subagent_set`, so its live list ends with an assistant message whose results
have not come back. A fork records nothing and has no restore or rebuild; a
restart ends it.

Removing a subagent tells the local network first, then stops the loop, then
deletes the directory. If the network notification cannot be made, the subagent
is left unchanged. A place on the network outlives the directory it was held
for.

**A message takes the agent's road, not a slower one.** The local network's
writer submits the turn to the agent server and reads the next arrival, exactly
as a window's `submit` does for an agent; the server appends to the inbox before
taking the lock, so a message that arrives while a turn is running is folded in
by `_check_new_input` at the next iteration. Awaiting the whole turn in the
writer was the one difference: the inbox stayed empty, the check had nothing to
hand over, and every group message became its own turn. A small in-flight cap
is the writer's backpressure, and one shared connection keeps the queue's order
at the server. The harness still only delivers; the model decides whether to
answer with `group_send_message`.

## 7. Terminal

### 7.1 The window is a client

**Rule:** SDD §10.1.

### 7.2 Rendering

**Rule:** SDD §10.2.

Model output is plain text. Nothing a model produces is parsed as markup; a
`[bold]` a model wrote is seven characters on screen, not a formatting
instruction. Markdown rendering could preserve that property, but it is a taste
decision about the transcript rather than a missing capability.

A rebuilt conversation uses the live drawing calls, not a second rendering
vocabulary, so live and restored turns are the same thing.

### 7.3 Approval

**Rule:** SDD §10.3.

A refusal mounts no tool panel, because a panel that appeared and then said
"denied" would tell a reader the tool ran.

### 7.4 The unattended agent

**Rule:** SDD §10.5.

It is a mode, not a second kind of agent: no branch of the window is conditional
on the flag. The model is told that nobody is there through the `headless` flag
in the name claim, because a model may otherwise spend the turn asking for a
confirmation nobody can give. No instance at all renders the same way.

### 7.5 Language boundary

**Rule:** SDD §10.4.

A daemon is shared and was not started with `--lang`, so the lines it composes
follow the OS locale. A machine whose OS language and `--lang` agree—every
machine that does not pass the flag—never sees the difference. Passing the
language to the daemons, or moving those sentences into the window, was
considered and rejected as a second source of truth or a change to the mesh's
hand-over vocabulary.

## 8. Operations

### 8.1 The file tunnel

**Rule:** SDD §12.1.

The tunnel exists for one thing a local path cannot do: a vision model or a chat
API that cannot read the machine's disk. A share outlives the turn that made it.

Two behaviours are deliberate rather than incidental:

- An unknown `active:` provider degrades to ngrok with a warning; a malformed
  shape is still refused. The tunnel is a subordinate dependency, and a name
  this build does not have must not keep the plugin from loading.
- A published URL is not a working URL: a transport that lost the edge keeps its
  URL and answers HTTP 530, so reachability is checked separately. A wrong
  "dead" costs a hostname, so a merely unreachable transport gets a heal window,
  and an unanswerable liveness probe is not an outage.

The security cost is the hostname's entropy: the tunnel publishes the MCP
endpoint as well as `/share/{token}`, so any caller who knows the hostname can
call `expose_local_image` and read any file the process can read. Closing it
properly requires an authorization story MCP does not carry here. A tunnel is a
decision to make this machine reachable.

### 8.2 Media

**Rule:** SDD §12.2.

Artifacts never go to the process's working directory: the daemon's working
directory is the runtime directory, which is documented as reconstructible, and
a generated file left there is the one file certain to be lost.

`folder=` is contained inside the conversation's work root because it is a
model-written string; `folder="../../.."` would be a model choosing where on the
operator's machine to write a file.

`openai-images` refuses the kinds it cannot serve by name, which is better than
a vendor 404.

### 8.3 Config edits are capabilities

**Rule:** SDD §4.1, §11.1, and §11.8.

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
program slife2 will start. The write is legible—it lands in the operator's file
in the section's own format—but it is not gated. The model's `_approve` is the
only confirmation mechanism, and it is model-driven.

### 8.4 Secrets

**Rule:** SDD §11.2.

**Protection is scoped to credstore.** v1 tried to protect every secret, and
the variety of shapes made the rules hard to maintain; v2 registers only what
the credential store resolves, and replaces those values in tool results, tool
descriptions, and logs. An environment value or a literal in the config is the
operator's own. The manual recommends credstore for every secret.

### 8.5 Data and runtime layout

**Rule:** SDD §11.3–§11.6.

### 8.6 Agent identity

**Rule:** SDD §4.3 and §11.3.

A sanitized name becomes a filename. A name that reduces to nothing is refused;
writing to a surprising path is worse than saying no.

### 8.7 Daemons outlive builds

**Rule:** SDD §12.5.

Reuse is by identity, so a daemon from a previous build can answer the name
check and still have the old tool signatures; every turn fails until the stack
is restarted. `slife2 down` is the remedy, and a rename of a server or a tool
must be accompanied by a stop.

### 8.8 Text encoding

**Rule:** SDD §12.3.

Encoding to UTF-8 with `errors="replace"` would turn an emoji into `??` and a
lone surrogate into a question mark indistinguishable from a typed one. That is
why the normalisation uses the UTF-16 codec.

## 9. Testing and timeouts

### 9.1 Two lanes

**Rule:** SDD §13.3.

### 9.2 Timeouts

**Rule:** SDD §12.4.

There is no turn deadline and no default tool-call budget: a single number
applied to every tool is wrong for most of them. Where work can legitimately be
long, the clock is an inactivity bound rather than a wall clock; the model
stream's 120 seconds of silence is the one case.

The gate is what makes the registry trustworthy: a time-style literal anywhere
in `slife2/` or `tests/` is rejected unless it is registered or carries a
`# timeouts:` note, and a registry lookup in a signature is rejected because it
reads the value once at import and is invisible to a patch.

## 10. Deferred capabilities

SDD §14.2 states the deferred items that change this version's behavior; the
table here adds why each is deferred and what the current answer costs.

| Capability | Why it is deferred | Current cost |
|---|---|---|
| A tool that runs a `cli:` entry | The entry is already a catalogue row; execution is an argv, not a shell. | A command is discoverable but nothing runs it. |
| A tool that runs a skill's scripts | Same shape as the `cli:` entry, plus `skills:` environment injection. | `skill_use` reports whether the key and program exist; the step it clears for is not taken. |
| A listing tool | "What is installed" is a browse, not a search; the store still has the filters. | A model must search instead of listing, and `tool_search` must not promise a list it cannot give. |
| Markdown rendering | A taste decision about the transcript, not a system gap. | Model output is plain text. |
| Delta coalescing | One progress notification per token is acceptable on loopback. | More display traffic than necessary. |
| The pass behind the context ceiling | The selection is bounded by construction; a turn whose tool results balloon while it runs is not caught. | A very large turn can exceed the ceiling until the next selection. |
| A cap on total agent-task work | `max_running` bounds concurrency; a rate or depth is the missing number. | A task can start tasks without a total bound. |
| A cap on the number of conversations | The inbox is capped because a queued call has a client-side timeout; the number of loops is not. | A client can create conversations without a machine-wide limit. |
| A visible agent-task progress line | The four tools are the model's; the window has no line for a running task and no key that stops one. | A person's only lever is to ask the model. |
| A word to the model about the loaded set | The two tool descriptions explain themselves; no per-turn prompt reports how many tools are loaded. | A model may not use loading unless it reads the descriptions. |
| A cap on scheduled tasks | A cron is bounded by time; a runaway is many rarely firing tasks. | `schedule_list`/`schedule_remove` make it visible and stoppable. |
| A word to the agent about unclosed runs | The ledger holds the answer and nothing has watched a missed run go unnoticed. | The agent must think to call `schedule_runs`. |
| A trigger nobody acted on | A run with no task id stays `pending`; the closing rule needs a measured timeout. | One turn's inattention leaves a pending row. |
| Background work inside an agent task | A task runs one turn and has no second turn for a result. | `_async` is refused with an explanation. |
| A client id on the turn record | Adding a column means deleting every existing `turns.db`; add it with the reader. | No per-turn client id in the record. |
| The window wire in a library | The vocabulary lives in the terminal plugin; moving it is a layering repair. | `libs/harness/handover.py` imports a server module. |
| One config reader | Three readers differ in wording for real reasons. | A reader may not recognize the same problem twice. |
| Selective re-embedding | Per-row content hashing would keep unchanged vectors across a rules change. | A model change re-embeds every row. |
| A `grep` search leg | A bounded literal reader exists for dumps; a system-wide regex leg is a larger change. | Partial spellings and symbols rely on the two existing legs. |
| A policy that forces approval | The model-driven `_approve` exists; a named list of tools that always ask does not. | `skill_set` and `mcp_set` rely on model judgment. |
| Device-code OAuth for tool servers | `${VAR}` headers cover a token somebody already has. | A server that needs a browser login cannot be configured. |
| A discriminator result larger than its own window | The fix needs the provider's window, a fact about the model server. | Such a result fails the turn loudly. |
| A bound on tool-call arguments | Arguments are the model's own words; shortening them is a different problem. | A model can write an unbounded argument into the conversation. |

## 11. Fallback register

**Rule:** SDD Appendix C.

The register itself is maintained in SDD, because a fallback that is not stated
there is a defect. This is the working note on how a fallback goes wrong when
nobody wrote it down.

### 11.1 Four shapes of an undeclared fallback

- **A value that cannot be read is not a value that is absent.** Damage and
  emptiness are different. The empty value is often written back, which makes the
  loss permanent.
- **A write that failed is not a write that happened.** A bookkeeping write
  either lands or is reported. Silence becomes a wrong fact.
- **A failure is not a verdict.** A search whose answer could not be read is not
  "nothing matched"; a tool list that could not be decoded is not "no tools".
- **A weaker path is not a substitute for a missing capability.** Feature
  detection must not silently select a degraded branch. Ask if the value can be
  asked for; refuse if it cannot; register it if it is genuinely optional.

