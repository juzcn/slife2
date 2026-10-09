# slife2

**Terminal-based AI agent — slife v2, a clean-slate rebuild.**

**A plugin is an MCP server over Streamable HTTP plus a small contract** — an
implementation of the protocol, not one of its own. The TUI is a client; the
agent loop is a server and a client; each model backend is its own server, and
so is the hub the tools come from. The word is what separates the servers slife2
starts from the ones somebody else wrote under `tools:`, and the difference is
load-bearing: a plugin is required and a missing one fails the turn, where an
entry under `tools:` is optional. Two properties follow from the arrangement,
and they are the point of the whole thing:

- **A key exists only inside the process that needs it.** A provider's API key
  lives in the LLM server that calls it; a tool server's token lives in the
  plugin that holds that server — `slife2-mcp-tools` for `tools:`,
  `slife2-restapi-tools` for `rest-api:`. The agent loop cannot leak either,
  because it never has one.
- **The agent loop imports no provider SDK.** Switching providers is changing a
  URL, and adding tools is adding an entry to a config file.

**A plugin is a capability; a library is its implementation, and they are not
alternatives.** The turn log is a *file*, so nothing has to be a process to hold
it: `slife2.db` is a library, and the two plugins that own its two halves import
it — `slife2-context` for the turns and the context, `slife2-toolhub` for the tool
catalogue. What stays a plugin is what holds something a process should own: a
credential, a provider SDK, or somebody else's connection. v1 draws the same line
the same way — its `memdb` is a plugin *and* a library, imported by the host and
by four other plugins. The test, and what it costs, is DESIGN §1.1.

[DESIGN.md](DESIGN.md) explains the decisions and the measurements behind them.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13.

```bash
uv sync
credstore set DEEPSEEK_API_KEY         # or export it
uv run slife2
```

That is the whole thing. `slife2` brings up the MCP servers its config needs,
attaches to any that are already running, and starts the TUI.

```
slife2 [--data-dir DIR] [--agent NAME] [--model PROVIDER/MODEL] [--url URL]
       [--keep-servers]                  ensure the servers, then run the TUI
slife2 status                            what is running, and where
slife2 down                              stop the servers this config names
```

`--model` picks the model this instance starts on, as `provider/model` (or a
bare provider, meaning its first model); without it the config's `default` is
used. `--url` overrides the agent server's endpoint, for the case where it is
already running somewhere this config does not name.

**The servers are shared.** A second `slife2` — under any `--agent` — finds them
already running and reuses them; nothing is started twice and nothing is
per-agent. They are daemons, so closing one TUI does not interrupt another, and
when the **last** instance exits it stops what is running. `--keep-servers`
leaves them up instead.

`--agent NAME` (default `slife2`) names the instance. It titles the window, it
signs the assistant's messages, and it renders the system prompt — and it is
**exclusive**, so two live instances may not share a name. Where it *does*
partition is the context store: each agent's turns go in their own database. The
servers themselves stay shared.

The **context** is chosen rather than accumulated. Before every turn, one model
call reads the conversation in hand and decides what to keep of it and what to
recall from the log; the turn then runs on the two together. That is v1's
mechanism, ported, and `context:` in the config is where it is tuned — including
the switch that turns it off, after which the context grows append-only. A
conversation that restarts, or that sat idle long enough for the loop to be
reclaimed, comes back on the context it had at exit.

That log is where the model can look back. `turn_list` browses it — newest
first, one line per turn, `since`/`until` taking an ISO date or a phrase a person
would write (`yesterday`, `last month`, `3 days ago`), paged with
`limit`/`offset` against a `total` — and `turn_read` returns one turn whole.
Neither takes an `agent`: the call carries the conversation it is on behalf of,
so a model reads its own history and nothing else, and no argument of its can
change that.

Every turn is also **indexed twice as it is stored** — a keyword index over the
text a turn is found by, and a vector index over what it was about — so a turn
can later be found by relevance rather than only by time. Both indexes are
derived from the turns and rebuilt from them, which is why changing the
embedding model (under `embeddings:` in the config) costs a re-embedding of the
whole history on the next start rather than a migration. A model can browse the
log but cannot *search* it yet: the recall the context decision uses is the
store's, and offering the same search as a tool is its own change (DESIGN §9).

Each plugin is also its own console script, so a process manager can run one
without passing an argument:

```bash
uv run slife2-agent            # the agent loop,             :8000
uv run slife2-toolhub          # the model's tools,          :8020
uv run slife2-builtins         # echo, now, calc,            :8030
uv run slife2-skills           # the playbooks,              :8031
uv run slife2-cli              # the cli: registry,          :8032
uv run slife2-mcp-tools        # holds the tools: servers,   :8033
uv run slife2-restapi-tools    # holds the rest-api: ones,   :8034
uv run slife2-context          # turns, and the context,  :8010
uv run slife2-llm-openai       # the OpenAI-compatible API,  :8001
uv run slife2-llm-anthropic    # the Anthropic Messages API, :8002
uv run slife2-llm-openai-responses  # the OpenAI Responses API, :8003
uv run slife2-llm-embeddings   # vectors for both indexes,  :8004
```

In the TUI: **Enter** sends, **Shift+Enter** breaks the line, **Ctrl+C** cancels
a running turn (and quits when there is none), **Ctrl+N** starts a new
conversation, **Ctrl+Q** quits.

**PageUp/PageDown** page the transcript and **Home/End** jump to its ends, and
the wheel scrolls it wherever the pointer happens to be — the transcript is what
those keys move, not the draft, so a long answer can be read while the next
message is being typed. Scrolling up holds the page there: streamed tokens do
not drag the view back down, and sending a message is what returns it to the
end. The draft keeps **Ctrl+A** and **Ctrl+E** for the ends of the line.

**Ctrl+C is still the copy key.** With something selected — in the prompt, or
with the mouse in the transcript — it copies, and nothing is cancelled; that is
what it does in every other program, and a terminal where it cannot copy is
worse than one where stopping a turn has a second spelling. **Esc** is that
second spelling, and it is only that: it cancels the turn in flight and will not
close the window however often it is pressed. With nothing selected, **Ctrl+C**
cancels the turn; with nothing running either, it quits.

Send a second message while one is still being answered and it **queues**: the
turn already running finishes, yours runs next, and the status bar says how many
are waiting. Esc stops the turn in flight and leaves the queue alone — the
message you gave up on and the one you are still waiting for are not the same
message.

**A restart is not a new conversation.** Close the window and open it again — or
restart the daemons, or leave it long enough for the idle sweep to let the
conversation go — and the transcript comes back: the same questions, the same
answers, the same tool calls with the results they returned, each line stamped
with the moment it was sent rather than with the moment the window opened. What
is put back is the conversation's *context*, restored by the agent server the
moment it builds the loop; the window asks for the turns that context was made of
and draws them, reasoning and all — a step that thought before it answered comes
back with its thinking folded away exactly where a live one had it. **Ctrl+N** is
what starts a genuinely new conversation.

**The window also says what the context is doing.** A restored conversation shows
its size in the status bar — the count the last call left the conversation at, not
a running total, and what fraction of the model's window that is. Each turn is
then preceded by a note naming what the *discriminator* decided for it: how much
of the conversation in hand it kept, and how much it went back to the turn log
for. That is one model call per turn that nothing else would show you, and it is
the mechanism that keeps a long conversation from growing without bound —
`slife2-context`'s own judgement, argued in DESIGN §5.1.

Name an image with `@` and it goes with the prompt:

```
what is wrong with this layout? @screenshot.png
```

The marker stays in the transcript, so the record shows what was sent. Only
local files, and only when the model's config lists `image` under `input` — a
model that cannot read images says so rather than quietly ignoring what you
attached.

The picture itself goes to the model and no further: what the log keeps is the
marker, plus a note where the image was saying it was there and how big it was.
Attaching it again is what sends it again — the file is named in the prompt, so
a turn read back a month later still says which picture it was about.

## Context

A conversation does not accumulate a context; it **chooses** one. `slife2-context`
keeps the turn log — one SQLite file per `(agent, subagent)`, exactly as before —
and before every turn makes **one model call** that reads the conversation in
hand and answers with two things: what to keep of the turns already in it, and
what to recall from the log. The turn runs on the two together, in time order.

The call reads the conversation rather than the new message alone, and that is
not a detail: the turn being recalled is usually a follow-up, and "what about the
other one" names its subject only through the turns above it. Its answer is a
selection, never a dump — the context is bounded, so a recall that matched more
than fits returns what fits, and the keep-list is how a model says which of the
turns in hand it is still working from. Every turn it reads carries a `[TURN: …]`
footnote naming its id, its channel and its span, which is where those ids come
from — and every turn the process ran itself gets one too, written the moment it
is saved, so the newest turns are as addressable as the restored ones.

**Nothing a model can call decides this.** The model has `turn_list` and
`turn_read` — reading its own history is the point of a log — and the two
decisions are the harness's: `restore`, which replays the context a conversation
had when it last stopped (a restarted process, or one the idle sweep let go), and
`rebuild`, which runs the decision above. A rebuild that decides to keep exactly
what is in hand rebuilds nothing at all, which is the common case and what keeps
a call this expensive from being paid for nothing.

It is tuned under `context:` in `slife2.yaml` — the ceiling and floor as
fractions of the model's own window, the recall's caps, and the timeout after
which the turn simply runs on the context it has:

```yaml
context:
  ceiling: 0.8         # what a kept context is measured against
  floor: 0.2
  recall_limit: 40
  min_similarity: 0.45 # gates a *measured* similarity; a keyword hit is exempt
  timeout: 20.0        # the discriminator call, after which the context stands
```

**A failure here is not a failed conversation.** A discriminator that times out,
a provider that errors, an answer that is not the shape asked for — all of them
keep the context and run the turn. A store that is *gone* is different and fails
the turn, like every other missing plugin.

## Tools

The model's tool list — the ones slife2 ships and the ones other people run —
is decided by **`slife2-toolhub`**, and it is the only process that decides it.
It is not the process that *reaches* any of those servers: `tools:` is held by
`slife2-mcp-tools` and `rest-api:` by `slife2-restapi-tools`, each of which
declares what it holds — the source, its tools, whether it is answering — and the
hub merges the rows, gates them, names them and routes the calls back. A plugin
contributes the same way whether it fronts somebody else's server or has no
connection at all: a playbook and a command are declared rows too, which is how
they are findable without being callable.

**The credentials live with the connections**, so a tool server's key is held by
the plugin that holds its entry — `SERPER_API_KEY` by `slife2-mcp-tools`, a
provider key by the model server that needs it, `BAIDU_API_KEY` by
`slife2-skills`. What the model is *handed* is the part of the list it has
loaded, which is the section after the config:

```yaml
# The tools slife2 ships, served by `slife2-builtins`: `echo`, `now`, `calc`.
# There is no config for them — adding one is a decorated function in
# `slife2/builtins.py`, and it arrives at the model the same way as everything
# below.

tools:                                  # other people's MCP servers
  arxiv:
    url: https://arxiv.mcp.brunosan.de/mcp
  serper:
    command: npx                        # stdio: a process slife2 starts
    args: [-y, serper-search-scrape-mcp-server]
    env:
      SERPER_API_KEY: ${SERPER_API_KEY}

rest-api:                               # OpenAPI documents, one tool per endpoint
  github:
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.yaml
    base_url: https://api.github.com
    api_key: ${GITHUB_TOKEN}

cli:                                    # programs already on this machine
  yt-dlp:
    command: yt-dlp
    description: Download video from 1000+ sites, subtitles and playlists.
    install: uv pip install yt-dlp

tool_load:                              # how many tools the model may hold
  threshold: 100

context:                                # how a conversation's context is decided
  ceiling: 0.8
  floor: 0.2
```

`command` starts a process and talks over its standard input; `url` connects to
somebody else's over the network. A `rest-api` entry is the same thing said
shorter — it is expanded into the `uvx mcp-openapi-proxy` invocation that serves
it, so the plugin that holds it has one mechanism rather than two. `${VAR}`
resolves through the same chain as a provider key.

A `cli` entry is the odd one out: there is no process to start and no URL to
connect to, because the program is already installed. It is written down so the
model can be told it exists — and written *here*, in the operator's file,
because an entry is the opt-in, exactly as an entry under `tools:` is. `install`
is what a person is told when the command turns out not to be on `PATH`.
**Every entry is a row in the tool catalogue** (`cli:yt-dlp`), so `tool_search`
finds a command by what it does rather than by its name — being findable is the
half that landed. **Nothing runs one yet**: the tool that does is the next
change (DESIGN.md §9), and until then `func_tool_load` says exactly that. The
rows are declared by **`slife2-cli`**, a plugin whose only job today is that —
which is why it exists before its tool does: one tool per entry is a tool the
model calls, and a tool needs a server to be served from.

The playbooks in `skills/` are catalogued the same way and *are* readable —
`skill_use`, which **`slife2-skills`** serves. It is a plugin for the same
reason, and it holds what the `skills:` section resolved, because a skill that
declares `requires.env` needs one process that knows the answer.

A tool's name carries the server it came from when there is one to carry: an
entry under `tools:` reaches the model as `{name}__{tool}` —
`arxiv__arxiv_search_papers`. **Ours do not.** `now`, `calc` and `turn_read` are
slife2's tools, and `builtins__now` was this system's own arrangement leaking
into the one thing the model reads on every request: which plugin serves a tool
is a fact about us, and `servers()` reports it to whoever is debugging. So our
tools are bare, and they are one namespace — two plugins cannot offer one name
between them — which the catalogue refuses loudly rather than resolving.

**What the model is handed is the tools it has loaded, not the tools that
exist.** The list is re-read from the hub before *every model call*, and it holds
`tool_search`, `func_tool_load`, `_func_tool_unload` and `skill_use` plus
whatever else the model has loaded — a server with ninety tools costs nothing
until one of them is wanted.
`tool_search` searches the whole catalogue by keyword *and* by meaning, one
hybrid search; `func_tool_load` puts one or several names in the list, and they
are there from the next step of the same turn. The catalogue is
`<data>/slife2.db/tools.db`, one file for the data directory rather than one per
agent: a tool loaded in one conversation is loaded for the next, and still loaded
after a restart.

Two things decide what starts loaded. **Ours always do** — a model that has
quietly lost `now` and `calc` is a failure nobody can see — and somebody else's
do when their entry says `autoload: true`, which is how a server whose tools are
wanted every turn is written down as such. Everything else arrives on demand.

The list is bounded, because it goes out with every request: over
`tool_load: threshold:` (100, in `slife2.yaml`) the least recently *called* tools
are unloaded, never ours and never an `autoload` one. Calling, not loading, is
what counts — a tool the model has been using all turn outlives one that was
just brought in — and a tool never called since it was loaded is ordered by when
it was loaded. That trim happens at a turn
boundary — the harness calls `_func_tool_unload` before it saves the turn, and
the names come back, so what the model just lost is something the log can say
rather than something nothing notices. **And the model reads it too**: the trim
is written into the conversation as a tool call and its result, so a model whose
list shrank learns that from the transcript rather than by reaching for a tool
that is no longer there. Nothing is written when nothing was taken. Being
evicted costs a search and a load, not a capability.

Which of a plugin's tools the model may call is said on the tool —
`@mcp.tool(meta=FOR_THE_MODEL)`, which `now`, `calc` and `echo` carry and the
context store's `remember` does not. A plugin's tools are its own code's until one of
them says otherwise, so a tool you forget to mark is invisible rather than
dangerous.

**The hub reaches our plugins, and it is *told* about everything else.** Two
kinds of thing arrive at it. A plugin's **own tools** come from `tools/list`, the
way it would answer anybody. A plugin's **sources** are declared instead: a
`tools:` entry, a `rest-api:` entry, a playbook, a command — none of which the
hub can reach — are answered for by the plugin that owns that section, in one
call to `list_sources`, and the hub merges the rows and routes every call back to
the plugin that holds them. So this process holds no connection to a server
somebody else runs and no key belonging to one: `slife2-mcp-tools` and
`slife2-restapi-tools` hold those, one connection per entry, and the hub owns the
tool *set* and nothing else.

The hub's own tools are what is left over, and they are exactly the set-level
work.
**`tool_search`, `func_tool_load` and `_func_tool_unload`** are served by the hub
because each is a question about the *whole* catalogue — what exists, what the
model is holding, what the budget takes back — so the process that owns the set
answers it and no plugin can. Their names carry no server because there is none
to name, which is how every one of our tools is named.

**`skill_use`** is not one of them any more: **`slife2-skills`** serves it, and
`slife2-cli` owns the `cli:` section. Both are plugins like any other, for a
reason that is easier to see from the other end — each family has a
model-facing tool still to come (DESIGN.md §9), and a tool needs a server to be
served from. A skill is a document in `<data>/skills/` and the tool reads it:
`skill_use(name="browser-harness")` returns that skill's `SKILL.md`, with the
folder it lives in in front of it so the paths in the body mean something.
Skills are installed by putting a directory in that folder — the tool reads the
disk on every call, so there is nothing to restart.

**A skill is also a row** (`skill:browser-harness`), and so is every `cli:`
entry — which is what lets `tool_search` answer "what can I do about a browser"
without the model already knowing to ask for `browser-harness` by name: the
playbook's whole document is what the search indexes, and "drive a browser"
reaching it is the entire point. The row is not a tool: it has no load state,
it never enters the model's list, and calling it answers with the step that does
reach the thing (`skill_use`) rather than a refusal. The name is namespaced
because `browser-harness` is a command *and* the skill documenting it, and one
name is one row.

**A family declares its rows and the hub merges them.** `slife2-skills` and
`slife2-cli` answer `list_sources` with their whole list — which is what makes a
deleted skill stop being a hit — and the hub merges it exactly as it merges the
tools a server listed. So the hub stays the only writer of the catalogue,
and every source is asked again before every search, which is why dropping a
directory into `skills/` is found at once and not at the next restart.

**Everything the model may call is a row in the tool catalogue**, the hub's own
three included. The hub decides what tools *are* — which sources, the naming
rule above, who may call one — and `slife2-context` keeps the record
and answers the questions: which rows are loaded, what one is called at the far
end, and the two search legs. Nothing in the hub opens a database, and nothing in
the catalogue knows what a proxy name is.

A skill is a document, and it can still need a key: `baidu-search` declares
`BAIDU_API_KEY` in its own header, and its first instruction runs a script that
dies without it. `skills:` is where the value comes from — the same chain as
every other secret, which is the point of writing it down, because a daemon
started days ago never saw the key you exported this morning.

```yaml
skills:
  baidu-search:
    env:
      BAIDU_API_KEY: ${BAIDU_API_KEY}   # shell env → credstore
```

`skill_use` reports the difference: read a skill whose key is not configured and
the answer says so, up front, instead of leaving the model to find out when the
command it was told to run fails. A skill that declares nothing says nothing.
Nothing runs those scripts yet (DESIGN.md §9) — which is exactly why the check
belongs in the reader.

`enabled: false` keeps an entry configured but never connects it, which is the
lever worth knowing: everything enabled is a process at startup and its tools in
every request. `slife2 down` takes those a step further — a plugin it stops takes
the child processes that plugin started with it.

A plugin is required and everything in `tools:` is optional: the hub asks
each plugin for a tool list, and one that cannot answer fails the turn rather
than quietly continuing with fewer tools, while a server that is somebody else's
is reported and left out. The builtins are the case that makes the rule worth
having — a model that has quietly lost `now` and `calc` is a failure nobody can
see.

## Where things live

**One data directory holds everything** — the config that says what to run, the
runtime state of what is running, and the turns they produced:

```
<data>/                            --data-dir DIR, or $SLIFE2_DATA_DIR
  slife2.yaml                      the config; absent means the defaults
  runtime/                         records, locks, logs — reconstructible
  slife2.db/                       <agent>.turn.db — a conversation's turns, and
                                   the context the next turn runs on (not
                                   reconstructible); tools.db — the catalogue,
                                   one file for the data directory
  skills/                          <name>/SKILL.md — playbooks, written by you
```

**Where that folder is depends on what you are running.** In a checkout it is
the checkout itself — which is what makes the `slife2.yaml` in front of you the
one in use — and otherwise it is `~/.slife2`, per-user and independent of
wherever the command happened to be started. `--data-dir DIR` (or
`$SLIFE2_DATA_DIR`) overrides both.

A checkout therefore keeps generated state in the working tree, which is why
`.gitignore` covers `runtime/` and `slife2.db/`. It does *not* cover `slife2.yaml`
or `skills/`: those are written by a person rather than produced by a run, and
they are the point of the arrangement.

The split that remains is the one that matters: deleting `runtime/` costs
nothing, deleting `slife2.db/` costs the record. The two files in there are kept
differently on purpose: a turn is one conversation's and there is one per agent,
while the tool catalogue is the whole data directory's — the tools are not
anybody's, so one hub serves them to every conversation, and a tool loaded in one
is loaded for the next.

`slife2.yaml` is checked in and documented in place, because **it holds no
secrets**: every key in it is a `${VAR}` reference resolved at runtime. A data
directory with no config in it uses the built-in defaults, so a fresh install
runs without one.

Secrets are never written in the file. `${VAR}` resolves through shell env →
[credstore](https://pypi.org/project/credstore/) → literal default, and a
`keyring:<service>/<key>` value resolves against the OS keyring directly.
Resolution is lenient — an unresolved reference stays verbatim, so a missing key
fails at the API call where the message can name it, rather than at startup.

## Layout

```
slife2/
├─ paths.py           # the one data directory, and what goes under it
├─ launcher.py        # which servers are needed, and attaching to the running ones
├─ runtime.py         # daemon records, logs, and the kernel-backed locks
├─ clock.py           # the one timestamp format every writer uses
├─ config.py          # slife2.yaml, the ${VAR} / keyring: chain, and the server table
├─ prompt.py          # the Jinja2 system prompt, rendered per turn
├─ messages.py        # the neutral message model — what crosses `stream_chat`
├─ events.py          # the turn event vocabulary, and its progress encoding
├─ tools.py           # the tool registry: what the loop can call, and how a
│                     #   failure reaches the model as text
├─ builtins.py        # slife2-builtins: `echo`, `now`, `calc`, and `calc`'s
│                     #   AST walker — a tool is one decorated function
├─ toolclient.py      # the toolhub hop from the agent's side: the wire shape,
│                     #   a listed tool as one the loop can run, and the trim
│                     #   the harness makes before a turn is saved
├─ gateway.py         # the link to a server somebody else runs: connect, list,
│                     #   call, and say whether it is answering — no catalogue,
│                     #   no category, no config
├─ toolfamily.py      # the half of a "hold somebody else's servers" plugin that
│                     #   is shared: hold, declare, route a call back
├─ mcp_tools.py       # slife2-mcp-tools: the `tools:` section, held
├─ restapi_tools.py   # slife2-restapi-tools: the `rest-api:` section, held
├─ toolhub.py         # slife2-toolhub: the model's tools and the *set* they
│                     #   belong to — the naming rule, the gate, the budget, and
│                     #   the calls, which it routes to whoever holds the source
├─ loop.py            # AgentLoop.run_turn — the turn algorithm
├─ textindex.py       # how a turn becomes searchable text, and how a query
│                     #   becomes a MATCH (no I/O)
├─ db.py              # the store, as a library: TurnStore, one SQLite file per
│                     #   client id, with the two derived indexes over its turns
│                     #   and the live-context list — and ToolStore, the tool
│                     #   catalogue, one file per data directory.  No server
│                     #   holds it: the two plugins that own its two halves
│                     #   import it (DESIGN §1.1)
├─ tokens.py          # how large a piece of a conversation is, when nobody has
│                     #   measured it — one estimator, every user of one
├─ embedder.py        # the client half of the embeddings server, as a library:
│                     #   one hop, shared by the two things with an index
├─ context.py         # a conversation's context as a pure function: the reply
│                     #   parser, the union, the two fit rules, the turn footnote
├─ context_server.py  # slife2-context: `remember`, `turn_list`, `turn_read`,
│                     #   and the two decisions — `restore` and `rebuild` —
│                     #   which are the harness's and never the model's
├─ mcp_server.py      # what it takes to *be* one of our MCP servers — including
│                     #   the client id every one of them keys its state by —
│                     #   and how a client proves which one it reached
├─ audience.py        # who a tool is for, and whose behalf a call is on: the
│                     #   two `_meta` facts that keep `remember` out of a
│                     #   model's hands and one conversation out of another's
├─ skills.py          # the `skills/` folder: a playbook's header, its
│                     #   `requires` block, and what `skill_use` answers with
├─ skills_server.py   # slife2-skills: `skill_use`, and the catalogue rows a
│                     #   search finds a playbook by
├─ cli_server.py      # slife2-cli: the `cli:` section as catalogue rows — one
│                     #   per entry, and no tools at all yet
├─ llm/
│  ├─ base.py                    # Chunk, Stream, LLMBackend  (no I/O)
│  ├─ wire.py                    # Chunk <-> progress payload (no I/O)
│  ├─ client.py                  # MCPBackend: the agent loop's only backend
│  ├─ server_common.py           # what the model servers share, incl. tool-call
│  │                             #   assembly
│  ├─ embeddings_server.py       # slife2-llm-embeddings        <- imports openai
│  ├─ openai_server.py           # slife2-llm-openai            <- imports openai
│  ├─ openai_responses_server.py # slife2-llm-openai-responses  <- imports openai
│  └─ anthropic_server.py        # slife2-llm-anthropic         <- imports anthropic
├─ server/server.py   # slife2-agent: FastMCP, the conversations and their two
│                     #   tools, keyed by (agent, subagent)
├─ templates/
│  ├─ system.j2       # the system prompt the distribution ships
│  └─ recall.j2       # the recall instruction, rendered per turn
└─ tui/
   ├─ app.py          # the Textual App
   ├─ client.py       # AgentClient protocol + the MCP implementation
   ├─ widgets.py      # ChatView, HistoryInput, StatusBar
   ├─ attachments.py  # reading `@path` images out of a prompt
   ├─ theme.py        # the palette and glyphs, defined once
   └─ app.tcss
```

The dependency direction is one-way and is what makes each layer testable alone:
`tui/` → MCP → `server/` → `loop.py` → {`llm/base.py`, `tools.py`, `events.py`,
`messages.py`} → `config.py`. The loop imports neither `server/` nor `tui/`, and
it does not import `toolhub.py` or `toolclient.py` either: it is handed a
coroutine that returns a registry, and where that registry came from is the
agent server's business.

`mcp_server.py` is a leaf every server sits on: it holds what being one of our
servers means — the flags, the HTTP transport, the record that says a daemon is
here, and the two conventions (`house_server`) that would otherwise be copied
into each server. It is not LLM-specific, which is why it is not under
`llm/`: the db server, the toolhub and the agent server are not LLM
plugins, and the scaffold they serve on should not come out of the LLM
package. The embeddings server is the one plugin under `llm/` that is not a
chat backend — it speaks `/embeddings` and nothing else — and it is there
because it is the other thing in this system that imports a provider SDK.

## Tests

```bash
uv run pytest                  # unit + integration, no network, no live model
uv run pytest -m unit          # fast only
uv run pytest -m integration   # the one test that binds a socket
uv run ruff check . && ruff format --check .
pyright                        # a uv tool, not a project dependency
```

Nothing in the suite needs an API key or a running server. The model is faked,
and the servers are driven over FastMCP's **in-memory transport** — real MCP
messages and real progress notifications, with no port bound. One `integration`
test does bind a socket, because the in-memory transport cannot prove that
progress notifications survive HTTP framing.
