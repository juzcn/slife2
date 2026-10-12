# slife2

**Terminal-based AI agent — slife v2, a clean-slate rebuild.**

**A plugin is an MCP server over Streamable HTTP plus a small contract** — an
implementation of the protocol, not one of its own. The TUI is a client and
nothing else; the agent loop is a server and a client; each model backend is its
own server, and so is the hub the tools come from — and so is the terminal's own
inbox, which is a plugin like the rest. The word separates the servers slife2 starts from
the ones somebody else wrote under `tools:`, and the difference is load-bearing:
**a plugin is required** — a missing one fails the turn — where an entry under
`tools:` is optional and its absence just means fewer tools.

Two properties follow, and they are the point of the arrangement:

- **A key exists only inside the process that needs it.** A provider's API key
  lives in the LLM server that calls it; a tool server's token lives in the
  plugin that holds that server — `slife2-mcp-tools` for `tools:`,
  `slife2-restapi-tools` for `rest-api:`. The agent loop cannot leak either,
  because it never has one.
- **The agent loop imports no provider SDK.** Switching providers is changing a
  URL, and adding tools is adding an entry to a config file.

**A plugin is a capability; a library is its implementation.** What holds a
credential, an SDK or somebody else's connection is a process; what holds a
*file* is not, because one writer is what a SQLite lock already is. So
`slife2.db` — the turn log and the tool catalogue — is a library, imported by the
two plugins that own its two halves, `slife2-context` and `slife2-toolhub`.
DESIGN §1.1 is the test and what it costs.

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

**On macOS the Python has to be one that can load SQLite extensions**, and
neither of the two you are most likely to be using is. The vector index is a
SQLite extension (`sqlite-vec`), and an interpreter built without
`--enable-loadable-sqlite-extensions` has no way to load one: that is uv's own
managed CPython, which links SQLite statically, and Apple's `/usr/bin/python3`,
whose system SQLite is compiled without it for sandboxing reasons. Nothing here
can work around it — with no index the database will not open, so no plugin
starts and the first command fails with `this Python's sqlite3 cannot load
extensions`. The builds on Windows and Linux have the feature. Homebrew's Python
does too:

```bash
brew install python@3.13
uv venv --python "$(brew --prefix python@3.13)/bin/python3.13"
uv sync
```

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
partition is inside servers that stay shared: each agent's turns go in their own
database, and each agent is its own session on `slife2-tui` — which is how a
message from a phone reaches *your* terminal and not the one next to it.

The **context** is chosen rather than accumulated: one model call before every
turn decides what to keep of the conversation in hand and what to recall from the
log, and the turn runs on the two together. There is no switch that turns that
off. A conversation that restarts, or that sat idle long enough for the loop to
be reclaimed, comes back on the context it had at exit. The next section is the
whole of it.

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
uv run slife2-skills           # the playbooks,              :8031
uv run slife2-cli              # the cli: registry,          :8032
uv run slife2-mcp-tools        # holds the tools: servers,   :8033
uv run slife2-restapi-tools    # holds the rest-api: ones,   :8034
uv run slife2-wechat           # WeChat, a login per agent,  :8030
uv run slife2-cabinet          # notes, diary, files,        :8035
uv run slife2-media            # images, video, speech,      :8036
uv run slife2-file-tunnel      # a public URL for a file,    :8038
uv run slife2-schedule         # scheduled tasks, the clock, :8039
uv run slife2-a2a              # the A2A mesh over MQTT,    :8040
uv run slife2-tui              # the terminal's inbox,       :8005
uv run slife2-context          # turns, and the context,  :8010
uv run slife2-llm-openai       # the OpenAI-compatible API,  :8001
uv run slife2-llm-anthropic    # the Anthropic Messages API, :8002
uv run slife2-llm-openai-responses  # the OpenAI Responses API, :8003
uv run slife2-llm-embeddings   # vectors for both indexes,  :8004
```

In the TUI: **Enter** sends, **Shift+Enter** breaks the line, **Esc** cancels a
running turn, **Ctrl+C** cancels one too and quits when there is none (copying
instead when something is selected), and **Ctrl+S** opens the model picker —
`↑`/`↓` move, `Enter` picks, `Esc` leaves it. Those are the keys the status bar
names, and it names only those: **Ctrl+Q** quits too, as it does in any Textual
app, and it is left unsaid because a terminal with flow control takes it as XON
before the window ever sees it.

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

**Ctrl+S switches the model.** The picker lists every model under `providers:`
with the one in use marked, and choosing one calls the same `model_switch` the
model itself calls, so a person switching a model and a model switching one are
one behaviour. The conversation keeps its history and moves onto the new model
from its next turn — a turn already in flight finishes on the model it started
on — and the status bar takes the new model's window and capability marks with
it. **Nothing in that path needs the model that is failing**: it is a config edit
and a line to the agent server, which is the point of the key, since a model that
has stopped answering is not something to argue with.

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
back with its thinking folded away exactly where a live one had it.

**The window also says what the context is doing.** A restored conversation shows
its size in the status bar — the count the last call left the conversation at, not
a running total, and what fraction of the model's window that is. Each turn is
then preceded by a note naming what the *discriminator* decided for it: how much
of the conversation in hand it kept, and how much it went back to the turn log
for. That is one model call per turn that nothing else would show you, and it is
the mechanism that keeps a long conversation from growing without bound —
`slife2-context`'s own judgement, argued in DESIGN §5.1.

Name an image and the model attaches it:

```
what is wrong with this layout? D:\shots\screenshot.png
```

**That is the whole convention, and it is not a syntax** — the path is a path in
a sentence, and the model calls `attach_image` for it. There are no markers to
learn: what a person writes is what a person means. Three kinds of source work
in one call — a local path, an `http(s)` URL, and a `data:` URI — and several at
once, with a source that cannot be read named in the answer rather than failing
the rest. A URL is passed to the provider, which fetches it; nothing here does.

Only when the model's config lists `image` under `input`: a model that cannot
read images is told so, and says so, rather than quietly ignoring what you
named.

The picture goes to the model and no further. What the log keeps is the sentence
and the tool's answer, which names every file — plus a note where the image was
saying it was there and how big it was — so a rebuilt conversation is text, and
a turn that needs the picture again attaches it again from that name.
Attaching it again is what sends it again — the file is named in the prompt, so
a turn read back a month later still says which picture it was about.

**A picture the provider will not read is the one thing that could take the
conversation with it**, because the bytes it refused would be sent again by every
turn that follows, and each one would fail the same way. So a turn that dies
there leaves the pixels behind: the live conversation drops them, with the same
note the log keeps and a word in the error saying so. Nothing is lost that the
prompt does not already name.

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
turns in hand it is still working from. Every turn it reads carries a `[TURN:{…}]`
footnote naming its id, its channel and its span, which is where those ids come
from — and every turn the process ran itself gets one too, written the moment it
is saved, so the newest turns are as addressable as the restored ones. The
capitals are the tell, by convention: a name in capitals is the harness's own
annotation, never part of the record, where a marker that reads as a word —
`[Wechat:{…}]`, on a turn somebody sent over WeChat — was said by somebody and is
stored with what they said.

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

### The config

```yaml
# Every section below is written by hand *and* by the model: each family serves
# the tools that edit its own (`mcp_set`, `rest_api_set`, `cli_set`, `skill_set`
# with `remove` / `list` / `set_enabled` beside them, v1's names).  `providers:`
# and `default:` at the bottom are edited the same way by `model_set` /
# `model_remove` / `model_switch`, which live on the agent server rather than on
# a family plugin of their own (DESIGN.md §8).  Either way
# the write goes through `slife2/configfile` — one writer, the file's comments
# kept, and the loader that reads this file at every start as the judge.

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

skills:                                 # what one playbook in skills/ is given
  baidu-search:
    env:
      BAIDU_API_KEY: ${BAIDU_API_KEY}
    # enabled: false                    # installed, and out of the way

tool_load:                              # how many tools the model may hold
  threshold: 100

context:                                # how a conversation's context is decided
  ceiling: 0.8
  floor: 0.2
```

**There is no `wechat:` section**: a WeChat login is scanned rather than
configured, and it lives in `agents/<agent>/wechat.yaml` — see "WeChat" above.

`command` starts a process and talks over its standard input; `url` connects to
somebody else's over the network. A `rest-api` entry is the same thing said
shorter — it is expanded into the `uvx mcp-openapi-proxy` invocation that serves
it, so the plugin that holds it has one mechanism rather than two. `${VAR}`
resolves through the same chain as a provider key.

A `cli` entry is the odd one out: there is no process to start and no URL to
connect to, because the program is already installed. It is written down so the
model can be told it exists — and written *here*, in the operator's file,
because an entry is the opt-in, exactly as an entry under `tools:` is. `install`
is what a person is told when the command turns out not to be on `PATH`, which
`cli_set` checks for while somebody is looking. **Every entry is a row in the
tool catalogue** (`cli:yt-dlp`), so `tool_search` finds a command by what it does
rather than by its name — being findable is the half that landed. **Nothing runs
one yet**: the tool that does is the next change (DESIGN.md §9), and until then
`func_tool_load` says exactly that. **`slife2-cli`** declares the rows and serves
the `cli_*` tools that write the section.

The playbooks in `skills/` are catalogued the same way and *are* readable —
`skill_use`, which **`slife2-skills`** serves, along with `skill_list` and the
`skill_*` tools that install, remove and switch a playbook. It is a plugin for
the same reason, and it holds what the `skills:` section resolved, because a
skill that declares `requires.env` needs one process that knows the answer.

**The model can edit these sections, and that is the thing to read this
paragraph for.** Each of the four families serves its own management tools —
`mcp_set` / `rest_api_set` / `cli_set` / `skill_set`, with `remove`, `list` and
`set_enabled` beside them, v1's names — so "written *here*, in the operator's
file" is no longer the same as "written by the operator". **A fifth editor is
the model config itself**: `model_list` / `model_set` / `model_remove` /
`model_switch` over `providers:` and the top-level `default:`, served by
`slife2-agent` rather than by a family plugin — the agent server is what turns a
provider and a model into a conversation's model, so the edit and the read that
honours it are one process (DESIGN.md §8). A `set` writes the
*whole* entry rather than patching it: a field left out is dropped, and
`set_enabled` is the one that changes a single field and leaves the rest. What
is unchanged is
the road an edit travels: the file is written by one module
(`slife2/configfile`) under a cross-process lock, as a document edit that keeps
every comment, and the result is handed to the same parsers and the same loader
a *start* uses — an entry the loader refuses is put back and the caller told
why. So what can be written is what an operator could write, and it lands where
they would have written it. What is *not* here yet is a gate before the write:
`mcp_set` with a `command:` names a program slife2 will start, and nothing asks
first (DESIGN.md §9, "Tool approval").

### What the model is handed

A tool's name carries the server it came from when there is one to carry: an
entry under `tools:` reaches the model as `{name}__{tool}` —
`arxiv__arxiv_search_papers`. **Ours do not.** `turn_read` and `tool_search` are
slife2's tools, and `context__turn_read` was this system's own arrangement leaking
into the one thing the model reads on every request: which plugin serves a tool
is a fact about us, and `servers()` reports it to whoever is debugging. So our
tools are bare, and they are one namespace — two plugins cannot offer one name
between them — which the catalogue refuses loudly rather than resolving.

**What the model is handed is the tools it has loaded, not the tools that
exist.** The list is re-read from the hub before *every model call*, and what
starts in it is slife2's own: `tool_search`, `func_tool_load`,
`_func_tool_unload`, `skill_use`, the two history tools, `attach_image`, the
harness's `_check_new_input`, and the twenty-two tools that edit the five config
sections — that last group is why the list is noticeably longer than it was, and
it is the one group whose being always-loaded is worth a second look. Everything else is loaded on demand — a
server with ninety tools costs nothing until one of them is wanted.
`tool_search` searches the whole catalogue by keyword *and* by meaning, one
hybrid search; `func_tool_load` puts one or several names in the list, and they
are there from the next step of the same turn. The catalogue is
`<data>/agents/tools.db`, one file for the data directory rather than one per
agent: a tool loaded in one conversation is loaded for the next, and still loaded
after a restart.

Two things decide what starts loaded. **Ours always do** — a model that has
quietly lost `turn_read` is a failure nobody can see — and somebody else's
do when their entry says `autoload: true`, which is how a server whose tools are
wanted every turn is written down as such. Everything else arrives on demand.

### Three arguments every tool takes

**Every tool accepts three meta-parameters the model may add to any call.** They
are not in any tool's schema of its own — they are added to every schema on its
way out (`slife2.tools.advertised`), and the loop reads them off and strips them
before dispatch, so no tool server ever sees one.

```yaml
# any tool call, from the model
{ "e": "6*7", "_timeout": 30 }              # this one call may take 30 s
{ "prompt": "a cat", "_async": true }        # run it in the background
{ "path": "notes.md", "_approve": true }     # ask the user first
```

- **`_timeout`** — seconds. It overrides the tool's own default; `0` or less
  means "use the default", **never** "no limit". A tool that declares its own
  `timeout` argument enforces it itself (exactly one timer per call); with
  neither, the call runs until it finishes. There is **no default budget**: a
  number applied to every tool is wrong for most of them.
- **`_async`** — run it in the background. The model gets a `task_id` and the
  result is **handed back into the conversation** when it arrives, wearing
  `[AsyncToolCallResult:{"call_id":…,"tool":…,"task_id":…,"status":…}]`; the
  window shows it and the reply itself is a message on the `async-tool` channel.
  `check_async` answers where the call *is* (running, finished, failed,
  cancelled), and `cancel_async` abandons it. **An agent task's own loop has no
  `_async`** — it runs one turn, so there would be nowhere to hand a result back
  to, and it says so rather than pretending (DESIGN.md §9).
- **`_approve`** — ask the user before the call runs. The terminal draws an
  inline row (`y` approve, `n`/`Esc` deny) and answers with `answer_approval`;
  the loop waits on that answer and refuses the call if it is denied, mounting no
  tool panel. **A channel with nobody behind it does not ask and the call simply
  runs** — WeChat, a scheduled run, another agent, and any client that has not
  been taught to draw the prompt (a caller that *can* answer says so with
  `send_message(..., watched=true)`, which the terminal does).

### The budget, and the pairs the harness writes

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

**A message that arrives while a turn is running is handed to that turn.** At
the next step boundary the harness calls `_check_new_input` and writes the call
and the message's own words into the running turn — v1's *cut-in*, always on — so
what you say while the agent is working reaches it inside the turn it arrived in
rather than one turn later. Like the trim, it is a tool pair: it is in the record
and in every request that follows, and the model reads it as something that
arrived while it was working. **No screen shows it yet.** That is a deferred
display decision rather than a half-built mechanism — the transcript draws from
events and this call raises none, and a rebuilt one skips `_`-prefixed calls by
name (DESIGN.md §9) — and the conversation has it either way.

### How a plugin contributes

Which of a plugin's tools the model may call is said on the tool —
`@mcp.tool(meta=FOR_THE_MODEL)`, which `skill_use` carries and the
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

**A declaration is the whole list, and it is asked for again before every
search.** The whole list is what makes a deleted entry stop being a hit — a merge
reads an absent name as a row the source no longer has — and asking again is what
makes dropping a directory into `skills/`, or editing the config, take effect at
the next search rather than at the next restart. The hub stays the only writer of
the catalogue either way: a source declares, the hub merges.

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

**Everything the model may call is a row in the tool catalogue**, the hub's own
three included. The hub decides what tools *are* — which sources, the naming
rule above, who may call one — and the record is a file the hub itself opens,
`<data>/agents/tools.db` through `slife2.db.ToolStore`: rows and two indexes
over them, and the same machinery the turns use, because a store has one writer
and one writer is what a SQLite file already is. It answers the questions about
the rows — which are loaded, what one is called at the far end, and the two
search legs — and nothing in it knows what a proxy name is.

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
every request. `servers()` reports it as `off`, so "why is my tool missing" has
an answer even for an entry that was switched off before it ever ran — and the
tools it offered while it was on stay in the catalogue, marked `disabled`, so a
search still finds them. `slife2 down` takes those a step further — a plugin it
stops takes the child processes that plugin started with it.

A plugin is required and everything in `tools:` is optional: the hub asks
each plugin for a tool list, and one that cannot answer fails the turn rather
than quietly continuing with fewer tools, while a server that is somebody else's
is reported and left out. A model that has quietly lost `turn_read` is a
failure nobody can see, and that is what makes the rule worth having.

### Media

Four capabilities behind one config section: **draw an image, render a video,
say something out loud, write down what an audio file says**.

```yaml
media:
  defaults:                              # which model a call gets unnamed
    image: bailian_personal/wan2.7-image
    video: bailian_personal/happyhorse-1.1-t2v
  providers:
    bailian_personal:
      api: dashscope-aigc                # the wire, not a brand
      base_url: https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1
      api_key: ${QWEN_TOKEN_PLAN_PERSONAL}
      models:
        - { model: wan2.7-image, kind: image }
        - { model: happyhorse-1.1-t2v, kind: video, params: { resolution: 720P, duration: 5 } }
        - { model: qwen-audio-3.0-tts-plus, kind: tts, voice: longanhuan_v3.6 }
        - { model: qwen-audio-3.0-asr-flash, kind: asr }
```

**What a model is *for* is a fact about the model**, which is the one place this
differs from `providers:` and `embeddings:` — a vendor may serve all four kinds
and its entries say which is which, so "switch to the cheap image model" is a
config edit rather than a different call. `defaults:` is which one a call gets
when it names none, and a call may always name its own.

**`api:` selects the wire**, and there are two today: `dashscope-aigc` (Bailian:
images, video, speech and transcription) and `openai-images` (an OpenAI-compatible
`/images/generations`, which draws pictures and refuses the other three by name —
better than a 404 from the vendor).

Generated files are written under **`<data>/agents/<agent>/media/`** and nowhere
else. Per agent because what it holds is what that agent generated, and under
`agents/` rather than `runtime/` because a daemon's working directory is
documented as reconstructible — a generated image left there would be the one
file certain to be lost. They are work products rather than cabinet documents:
nothing registers them and the answer is a path. `folder=` sorts them into
subdirectories *inside* that root and a path outside it is refused, so a model
does not get to choose where on your disk a file is written. A video can
take minutes and the wait is bounded: past `timeout` the call is refused with the
provider's job id rather than its result, because the render carries on there and
asking again would pay for it twice.

### The file tunnel

**A public HTTPS URL for a local file**, so something that cannot read your disk
can fetch one anyway — a vision model asked about a picture, a chat API that
takes a URL and nothing else.

```
expose_local_image(path)      → https://<tunnel>/share/<token>
unexpose_local_image(token)   → that URL stops working
```

The file is not moved or copied: it is served from where it is, for as long as
the tunnel is up. The link stops working the moment the **file changes** (so a
path swapped underneath a link can never serve new bytes to whoever holds the
old one) and when the process exits. A private key, a `.env` or a saved password
is refused by name — a share URL is public while the tunnel is up, so that would
be publishing it.

**A tunnel is somebody else's process, and the config says whose.** Three
providers, exactly one active; the others stay configured and inert:

```yaml
file-tunnel:
  active: cloudflare
  providers:
    ngrok:        { token: ${NGROK_AUTHTOKEN} }
    localhost.run: { binary: ${SSH_BINARY:-ssh}, user: nokey, remote_port: 80 }
    cloudflare:   { binary: ${CLOUDFLARED_BINARY:-cloudflared}, protocol: http2 }
```

| provider | needs | the URL | what a *browser* sees |
|---|---|---|---|
| `ngrok` | an `NGROK_AUTHTOKEN` credential | one stable dev domain per account | on the free tier, an interstitial splash page (ngrok answers any `Mozilla` User-Agent with it, and forbids suppressing it server-side) |
| `localhost.run` | an `ssh` client — Windows 10+ ships one | a random `*.lhr.life` hostname that rotates every few hours | the file; TLS ends at their edge. Bytes are relayed by a third party |
| `cloudflare` | the `cloudflared` binary, which nothing installs | a `*.trycloudflare.com` hostname, stable for the process's life | the file |

Programmatic fetches — the multimodal-LLM path this exists for — are unaffected
by ngrok's splash page, which is why ngrok is the default when nothing is
configured. The config above ships with **`cloudflare` active**, because it
needs no account and relays the bytes to nobody; if you have not installed
`cloudflared`, set `active:` to `localhost.run` and the OpenSSH client Windows
already ships is the whole requirement. **A tunnel that is not up, or that has
published a URL nothing can reach, is refused rather than handed out**: a link
that answers HTTP 530 is worse than an error, because the model that cannot
fetch it cannot say why. `tunnel_status` is where the detail is — the state, the
URL, whether it is reachable, and the edge address it is dialing.

### The cabinet

Your own notes, a diary, saved files and reports, in one place that a search
covers all at once and a person can read in an editor. Four kinds, and the
difference between them is what a *document* is:

| kind | keyed by | a second write |
|---|---|---|
| **note** | a subject you choose | replaces it |
| **diary** | the date | appends a section |
| **file** | its path inside the cabinet | copies the file in |
| **report** | a title | appends a section |

A note and a saved file are documents — the second write *is* what it holds —
and a day and a title are logs, because two entries under one date are two facts
about it and nothing in the later text says which part of the earlier one it
replaces. That is why only the two document kinds have a remove tool.

**Everything is written twice**, and one of the two is for you: the row is what
`cabinet_search` reads, and the same document lands as markdown under
`agents/<agent>/cabinet/` — `notes/Python.md`, `diary/2026-10-11.md`,
`reports/weekly.md` — which is what you open, edit or delete without asking
anybody. A saved file's bytes live there too (`<category>/`) and are never
put in the index: `file_read` returns a file's *text*, and refuses a PDF or an
image by saying what it is rather than decoding it into a page of replacement
characters. Text in another encoding is read and the encoding named on the first
line, because a *silent* wrong decode is the one outcome worth refusing.

**`cabinet_search` takes words or sentences**, the same two inputs
`tool_search` takes: `keywords` are words that must all be there (a name, a
path, a symbol), and `sentences` are questions matched by meaning — several are
several questions, and each one can only add results. A file has no text of its
own, so `cabinet_summarize` is how it becomes findable by what it *is* rather
than by its name; writing a summary for a file re-embeds it, because its
description is part of what its vector was built from.

Two things it does not do, both by name: **`file_save` does not fetch a URL**
(it copies files already on this machine — v1's URL half came with an SSRF guard
that is a change of its own), and **there is no `grep` leg** (DESIGN.md §9
defers v1's third search mode for the whole system, and the cabinet inherits
that rather than growing a second regex engine).

### WeChat

`slife2-wechat` connects one WeChat account per agent, over iLink's hosted
bridge. Nothing about it is configured: a login is *scanned*, so
`wechat_login` puts a QR code in the model's reply, somebody points the WeChat
app at it, and the token that comes back is saved to
`agents/<agent>/wechat.yaml` where the next start picks it up. A login lasts
about a day; `wechat_check_status` says how much of it is left.

Four tools, and the model uses all of them:

| tool | what it does |
|---|---|
| `wechat_login` | Fetch a QR code and start watching for the scan |
| `wechat_check_status` | Connected? How long left? And **who to reply to** |
| `wechat_send_message` | Send a text to a peer |
| `wechat_logout` | Forget this agent's login |

A message somebody sends arrives as a **turn of that agent's own conversation —
in the window**. The plugin hands it to the terminal showing that conversation,
which runs it exactly as if it had been typed there: the same queue, the same
turn, the same transcript. So the message sent from a phone appears on screen,
its answer streams there, and both are in the record afterwards.

For that, the message goes to **`slife2-tui`**, the terminal's own inbox: a
plugin like every other one — an address under `servers:`, started and shared by
`slife2`, listed by `slife2 status` — which keeps **a session per agent**. The
window attaches to it when it opens and holds that call for as long as it is
showing a conversation, which is what lets a call say *which* terminal it means;
two windows at once are two sessions, and neither is more special than the other
(DESIGN.md §4).

The turn is recorded with `channel=wechat` and a
`[Wechat:{"peer_wechat_id": …}]` prefix naming who it is from. The name is
deliberately not in capitals — a convention rather than a
rule: **capitals mark an annotation that is not part of the conversation**,
which is what `[TURN:{…}]` is, the footnote a turn carries when the model reads
its context back (added when the list is built, never stored) — while this marker
is part of the message and goes into the turn's log with it. Nothing checks the
case; it is there to be read. A window shows the channel rather than the marker:
the line wears `Wechat> ` instead of `You> ` and the two ids inside the marker
come off, because they are what a reply needs and not what a reader does.
Answering is the model's job and only the
model's: nothing here auto-dispatches a reply, so a turn that says nothing sends
nothing — call `wechat_send_message` with the peer and the `context_token` the
marker carried. While the turn runs the person on the other end sees *typing…*,
and it stops when the reply goes out.

Each agent logs in separately and gets its own account, its own file and its own
poll loop; the plugin is one shared process (DESIGN.md §1.2). There is no
`wechat:` section to write: a login is scanned rather than configured, and the
`agents/<agent>/wechat.yaml` files are the whole of what the plugin resumes. A machine
that should not talk to WeChat is a machine where nobody scans a code.

### The agent task

A turn ends when the model stops calling tools, so anything long holds that turn
open: the person watches a prompt nothing can be typed into. **An agent task** is
that work gone elsewhere — one loop of the agent's own, started with a name and a
description and answered later, by a message, in the same conversation.

The model has four tools over them, and **not one of them waits**:

| tool | what it does |
|---|---|
| `agent_task_start` | Start one, and answer at once with its id |
| `agent_task_status` | What one is doing — never its result, which arrives by itself |
| `agent_task_list` | What this agent has, running ones first |
| `agent_task_cancel` | Stop one, answering with its state *after* the call |

```
agent_task_start(name="research", description="Find the retry policy, and say what it is")
→ {"task_id": "agent-task:research", "name": "research", "status": "running", ...}
```

**It runs as the agent, on the agent's own context.** The same system prompt, the
same model, the same tools — and `restore` and `rebuild` under the agent's own
key, so a task starts from what this conversation has been about rather than from
nothing. What it does *not* have is the turn that started it: `restore` reads the
*stored* list and a turn is written when it ends, so the description is where the
last exchange has to be restated. Nothing is recorded on its behalf — the answer
reaching the conversation as a message is what the record gains — and its own
reply is handed back on its own, so there is nothing to poll for and no way to
fetch one.

The answer arrives on the `agent-task:<name>` channel wearing
`[AsyncTaskResult:{…}]` — the task's id, name, state and times — so the window
shows `AsyncTaskResult(research)> ` and the model learns the id its next call
needs. It goes to the terminal showing that conversation; when no window is
showing it, the agent server is given it instead, and the turn is recorded and
drawn on the next restore.

```yaml
agent-task:
  max_running: 4   # tasks running at once, per agent
```

**That is the only key a task owns.** Everything else it is made of comes out of
`agent:` above, because a task *is* the agent working — the one section in this
file with two readers. `max_running` is the one bound the feature has: the tool
set is the single catalogue and nothing filters it, so a task may start a task,
and what that leaves unbounded is how much work happens over time rather than how
much happens at once (DESIGN.md §9).

### Scheduled tasks

**The one thing here that starts work nobody asked for at the moment it starts.**
A scheduled task is a name, a description and a cron expression, written down by
the model; from then on `slife2-schedule` fires it, and when it comes due the
agent is handed the task as a turn of its own — through the terminal, or through
the agent server when no window is showing that conversation. **That turn has one
instruction**: call `schedule_run_now` with the due time it carries. A task that
has come due is **not the agent's to decline**; `schedule_run_skip` exists for the
runs that did not happen at all.

| tool | what it does |
|---|---|
| `schedule_set` | Write a task (or replace one) — name, description, cron, timezone |
| `schedule_remove` | Forget it, and everything it ever did |
| `schedule_list` | What is scheduled, and when each next comes due |
| `schedule_runs` | What it has actually done — one row per fire |
| `schedule_run_now` | Start this run's work now; with a `due_at`, backfill that run |
| `schedule_run_skip` | Close a run that is not going to happen, and say why |

```
schedule_set(name="daily", description="Write the day's entry from the turns.",
             cron="0 22 * * *", timezone="Asia/Shanghai")
→ {"name": "daily", "cron": "0 22 * * *", "next_run": "2026-10-12T22:00:00+08:00", ...}
```

**`schedule_run_now` is the plugin starting the work, not the agent doing it.**
The call hands the run to `agent-task` as a task of the agent's own — the same
prompt, model and tools, the same context — and returns at once. Because the
*plugin* is the caller it is handed the task's id, so the clock's own poll can ask
what became of it: `finished` closes the run `ran` at the task's own `finished_at`,
a failure or a cancellation closes it `failed`, and nothing has to be remembered
in a later turn. The answer itself is delivered back to the conversation by
`agent-task`, wearing `[AsyncTaskResult:…]` like any other background task's.

**The ledger is the feature.** A *run* is one fire, named by the moment it was
**due** rather than by when it ran, and it moves `pending → ran | failed | missed |
skipped`. The row is written before the trigger is handed over, so a fire cannot
repeat on the next poll; a hand-over that is refused marks the run `failed`; one
that does not come back leaves it `pending`, because a run whose fate is unknown
is not reported as either. A run whose work **could not be started** is closed
`failed` with the reason, rather than left open with nothing behind it.

**Tasks fire only while slife2 is running**, and what was missed is not hidden:
coming up, the sweep fails anything a stopped process left open and the poll
records the newest fire each task missed — `schedule_runs(status="missed")` is
where you find them — so a run can be backfilled with `schedule_run_now(name,
due_at)` or closed with `schedule_run_skip`. Nothing announces them: **a trigger
is the only thing the clock ever says to a conversation**, and the ledger is where
"did it run?" is answered (DESIGN.md §1.7, §9). A trigger wears no marker either,
because the marker vocabulary is for matching a *result* to what produced it and a
trigger is not a result — the `schedule:<task>` channel and the template's first
line carry what a reader needs.

A ledger lives at `<data>/agents/<agent>/schedule.db`, and there is **no config
section** — an address is the whole of what is written down about the plugin. A
task's `timezone` is an IANA name, and leaving it out means the machine's clock
*as it is now*, which does not follow a daylight-saving change.

```yaml
# nothing to configure: a machine that should not run scheduled tasks is a
# machine where nobody writes one
```

### The A2A mesh

**Agent-to-agent traffic over MQTT**, on the published A2A-over-MQTT profile —
so anything else that speaks it can talk to you. The wire, the topics, the agent
cards and the task lifecycle are that SDK's; this is the driver and the plugin
around it. v1's mesh, ported.

| tool | what it does |
|---|---|
| `a2a_send_message` | Send a task, complete one you were sent, or just talk — `message_type` decides which |
| `a2a_cancel_task` | Withdraw a task you sent |
| `a2a_list_agents` | The roster: you first, then every peer, by name |
| `a2a_broadcast` | A fire-and-forget event to every peer |

```
a2a_send_message(agent="jill", message="Summarise the release notes.")
→ {"peer": "jill", "task_id": "61ab0c8a…", "message_type": "task_request",
   "note": "The result will arrive in this conversation."}
```

**Nothing waits.** A `task_request` returns an id immediately and the result
arrives later on its own as a message wearing `[A2A:{"from": "jill", "type":
"task_response", "task_id": "…"}]`. There is nothing to poll. `task_response`
completes a task *you* were sent, with the `task_id` from its own marker; the
direction is in `from`, never `agent_name`, because that word is the agent's own
identity. Incoming messages read as `A2A(jill)> ` in the window, with the marker
taken back off.

**A peer may be on this machine or on another one**, and nothing tells them
apart — a local agent is just one whose mesh this process holds, published to the
broker under its own name. There is a **mesh per agent**, because the agent name
*is* the mesh identity: the MQTT client id and the last segment of the request
topic. Two agents on one machine are therefore two peers, and one agent reached
from two places is what the name-collision check refuses, before joining, rather
than fighting over.

**An agent joins the mesh by using it.** The ledger at
`<data>/agents/<agent>/a2a.yaml` is the record, so the first `a2a_*` call
enrolls that agent permanently and every later start resumes it — that file is
also how the shared plugin knows whose names to be reachable under.

**The broker is started for you if it is not there**, which is where this
departs from v1. Point `a2a.broker` at an address; if nothing is listening there
and it is *this* machine, `mosquitto` is started on it and left running. A
broker that will not serve — or one named on another machine that is down — fails
the startup, naming it and showing its log, rather than leaving you four tools
attached to nothing. `provider: mosquitto` is the only value; anything else is a
config error, not a silently disabled feature.

```yaml
a2a:
  provider: mosquitto
  broker:
    host: localhost      # another machine is reached the same way, and is
    port: 1883           # never started locally — that host's broker is theirs
  org: default           # the topic namespace: two groups on one broker are
  unit: default          # told apart by these
```

**A restart orphans every task that was in flight**, because the completion
bridge and the peer's reply topic both lived in the process that died. The ledger
draws the line between those and ids never seen, and the agent is **told once**,
at the start that found them, so it can answer each peer with a plain message —
the only reply those tasks can still get. **Omit the `a2a:` section entirely** to
stay off the mesh: a machine that should not be on it is a machine where nobody
writes one.

## Where things live

**One data directory holds everything** — the config that says what to run, the
runtime state of what is running, and the turns they produced:

```
<data>/                            --data-dir DIR, or $SLIFE2_DATA_DIR
  slife2.yaml                      the config; absent means the defaults
  runtime/                         records, locks, logs — reconstructible
  skills/                          <name>/SKILL.md — playbooks, written by you
  agents/                          everything that belongs to one agent
    tools.db                       the tool catalogue — the one shared file
    <agent>/                       one directory is the whole of one agent
      turns.db                     its conversation, and what it runs on
      schedule.db                  what it scheduled, and every run of it
      cabinet/                     notes/, diary/, reports/ as markdown and the
                                   saved files by category, with `.index.db`
                                   (the search index) inside
      media/                       what it generated
      wechat.yaml                  its WeChat login (~24h, written on a scan)
      a2a.yaml                     its A2A ledger: the inbound tasks whose
                                   completion bridge this process holds, which
                                   is also what puts it on the mesh
```

**Nothing inside an agent's directory repeats its name** — the directory says
whose it is — and the catalogue is the exception that proves the rule: it sits
beside the agent directories because it is everybody's and therefore nobody's.

**Where that folder is depends on what you are running.** In a checkout it is
the checkout itself — which is what makes the `slife2.yaml` in front of you the
one in use — and otherwise it is `~/.slife2`, per-user and independent of
wherever the command happened to be started. `--data-dir DIR` (or
`$SLIFE2_DATA_DIR`) overrides both.

A checkout therefore keeps generated state in the working tree, which is why
`.gitignore` covers `runtime/` and `agents/`. It does *not* cover `slife2.yaml`
or `skills/`: those are written by a person rather than produced by a run, and
they are the point of the arrangement.

The split that remains is the one that matters: deleting `runtime/` costs
nothing, deleting an agent's directory costs that agent — its conversation, its
cabinet and its logins, in one place, which is what makes backing one up or
moving one to another machine one operation. The catalogue is kept differently
on purpose and sits outside them: the tools are not anybody's, so one hub serves
them to every conversation, and a tool loaded in one is loaded for the next.

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
├─ toolclient.py      # the toolhub hop from the agent's side: the wire shape,
│                     #   a listed tool as one the loop can run, and the trim
│                     #   the harness makes before a turn is saved
├─ gateway.py         # the link to a server somebody else runs: connect, list,
│                     #   call, and say whether it is answering — no catalogue,
│                     #   no category, no config
├─ configfile.py      # the one writer of slife2.yaml: a section edit under a
│                     #   cross-process lock, as a document edit that keeps the
│                     #   file's comments, judged by the loader that reads it —
│                     #   and the top-level-scalar and locked read-modify-write
│                     #   writers the model config needs
├─ toolfamily.py      # the half of a "hold somebody else's servers" plugin that
│                     #   is shared: hold, declare, route a call back, and the
│                     #   management tools' sentences, which one `FamilyWords`
│                     #   per section supplies the words for
├─ mcp_tools.py       # slife2-mcp-tools: the `tools:` section — held, declared,
│                     #   and editable by the `mcp_*` tools
├─ restapi_tools.py   # slife2-restapi-tools: the `rest-api:` section, likewise
├─ wechat.py          # slife2-wechat: a WeChat login per agent, the four tools
│                     #   the model uses, and the poll loop that turns what
│                     #   arrives into a turn — the one plugin here that holds
│                     #   no section and declares no rows (DESIGN §1.2)
├─ ilink.py           # the iLink ClawBot protocol, as a library: the QR
│                     #   handshake, the long poll, and the two envelopes that
│                     #   mean something other than what their status says
├─ attachments.py     # how an image reaches the model: the three kinds of
│                     #   source, and the content parts they become
├─ media/             # slife2-media: generation, one process for every vendor
│  ├─ server.py       #   the four tools and the config's four-step search
│  └─ adapters.py     #   one contract, and the vendor HTTP behind it
├─ file_tunnel/       # slife2-file-tunnel: a public URL for a local file, and
│  ├─ providers.py    #   the only plugin here with a second HTTP route — the
│  │                  #   ngrok / localhost.run / cloudflared transports, their
│  │                  #   state machine, and the health monitor that keeps one up
│  └─ server.py       #   the token registry, `GET /share/<token>`, the tools
├─ a2a/               # slife2-a2a: the A2A mesh over MQTT, on the published
│  ├─ mesh.py         #   profile — one mesh per agent, because the agent name
│  │                  #   *is* the MQTT client id and the request topic's last
│  │                  #   segment: the two connections, the task lifecycle,
│  │                  #   presence, and the check that refuses a taken name
│  ├─ broker.py       #   the broker: probed, started when it is local and
│  │                  #   absent, and a startup failure if it still will not
│  │                  #   serve — or if it is another machine's and is down
│  ├─ inbound_store.py#   the per-agent ledger at <agent>/a2a.yaml: tasks whose
│  │                  #   completion bridge this process holds, which is what
│  │                  #   tells a restart's orphans from ids never seen
│  ├─ task_store.py   #   what this process sent, and where each send got to
│  └─ server.py       #   the plugin: the four tools, and the hand-over that
│                     #   puts what arrives into the conversation (DESIGN §1.8)
├─ cabinet.py         # slife2-cabinet: notes, a diary, saved files and reports
│                     #   as sixteen tools, and the byte half of a saved file —
│                     #   what is copied in, what is read back, what is refused
├─ cabinet_store.py   # the cabinet's index, as a library: one table for four
│                     #   kinds, one FTS index, one vector index, and the
│                     #   markdown mirror written in the same operation
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
│                     #   holds it: the plugin that owns the turns imports the
│                     #   first half, and the toolhub opens the catalogue
│                     #   in-process, because a file already has one writer
│                     #   (DESIGN §1.1)
├─ tokens.py          # how large a piece of a conversation is, when nobody has
│                     #   measured it — one estimator, every user of one
├─ embedder.py        # the client half of the embeddings server, as a library:
│                     #   one hop, shared by the two things with an index
├─ context.py         # a conversation's context as a pure function: the reply
│                     #   parser, the union, the two fit rules, the turn footnote
├─ context_server.py  # slife2-context: `remember`, `forget`, `turn_list`,
│                     #   `turn_read`, and the two decisions — `restore` and
│                     #   `rebuild` — which are the harness's, never the model's
├─ mcp_server.py      # what it takes to *be* one of our MCP servers — including
│                     #   the client id every one of them keys its state by,
│                     #   how a client proves which one it reached, and the
│                     #   `serve_plugin` start-up every plugin's `main` is
├─ audience.py        # who a tool is for, and whose behalf a call is on: the
│                     #   two `_meta` facts that keep `remember` out of a
│                     #   model's hands and one conversation out of another's
├─ skills.py          # the `skills/` folder: a playbook's header, its
│                     #   `requires` block, and what `skill_use` answers with
├─ skills_server.py   # slife2-skills: `skill_use` and the `skill_*` tools that
│                     #   install one, plus the catalogue rows a search finds a
│                     #   playbook by
├─ cli_server.py      # slife2-cli: the `cli:` section as catalogue rows — one
│                     #   per entry — and the `cli_*` tools that write it
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
├─ server/server.py   # slife2-agent: FastMCP, the conversations and the tools
│                     #   that drive them, keyed by (agent, subagent) — plus the
│                     #   `model_*` tools, which own `providers:` and `default:`
├─ templates/
│  ├─ system.j2       # the system prompt the distribution ships
│  ├─ recall.j2       # the recall instruction, rendered per turn
│  ├─ agent_task.j2   # what a task is told as it starts (DESIGN.md §1.3)
│  ├─ schedule.j2     # what a task that came due is told (§1.7)
│  └─ a2a_orphans.j2  # the inbound tasks a restart left nobody able to
│                     #   complete, and the one reply their peers can still
│                     #   get (§1.8)
└─ tui/
   ├─ app.py          # the Textual App
   ├─ client.py       # AgentClient + WindowClient protocols, and the MCP
   │                  #   implementations of both directions
   ├─ control.py      # slife2-tui: the terminal's inbox, a plugin with a session
   │                  #   per agent (`attach` for the window, `submit` for a
   │                  #   channel) — the window is a client of it
   ├─ restore.py      # a previous conversation, drawn back into the transcript
   ├─ widgets.py      # ChatView, HistoryInput, StatusBar
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
here, the two conventions (`house_server`) that would otherwise be copied into
each server, and the whole of a plugin's `main` (`serve_plugin`), which is
eleven lines every one of them would otherwise spell out. It is not
LLM-specific, which is why it is not under
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
uv run pyright                 # a dev dependency, wired into CI
```

Nothing in the suite needs an API key or a running server. The model is faked,
and the servers are driven over FastMCP's **in-memory transport** — real MCP
messages and real progress notifications, with no port bound. One `integration`
test does bind a socket, because the in-memory transport cannot prove that
progress notifications survive HTTP framing.
