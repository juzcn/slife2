# slife2

**Terminal-based AI agent — slife v2, a clean-slate rebuild.**

Every component is an MCP server. The TUI is a client; the agent loop is a server
and a client; each model backend is its own server. Two properties follow from
that, and they are the point of the whole arrangement:

- **A provider API key exists only inside the LLM server process that needs it.**
  The agent loop cannot leak one because it never has one.
- **The agent loop imports no provider SDK.** Switching providers is changing a
  URL.

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
partition is memory: each agent's turns go in their own database. The servers
themselves stay shared.

Each component is also its own console script, so a process manager can run one
without passing an argument:

```bash
uv run slife2-agent            # the agent loop,             :8000
uv run slife2-memory           # turns, one db per agent,    :8010
uv run slife2-llm-openai       # the OpenAI-compatible API,  :8001
uv run slife2-llm-anthropic    # the Anthropic Messages API, :8002
uv run slife2-llm-openai-responses  # the OpenAI Responses API, :8003
```

In the TUI: **Enter** sends, **Shift+Enter** breaks the line, **Ctrl+C** cancels
a running turn (and quits when there is none), **Ctrl+N** starts a new
conversation, **Ctrl+Q** quits.

Send a second message while one is still being answered and it **queues**: the
turn already running finishes, yours runs next, and the status bar says how many
are waiting. Ctrl+C stops the turn in flight and leaves the queue alone — the
message you gave up on and the one you are still waiting for are not the same
message.

Name an image with `@` and it goes with the prompt:

```
what is wrong with this layout? @screenshot.png
```

The marker stays in the transcript, so the record shows what was sent. Only
local files, and only when the model's config lists `image` under `input` — a
model that cannot read images says so rather than quietly ignoring what you
attached.

## Where things live

**One data directory holds everything** — the config that says what to run, the
runtime state of what is running, and the turns they produced:

```
<data>/                            --data-dir DIR, or $SLIFE2_DATA_DIR
  slife2.yaml                      the config; absent means the defaults
  runtime/                         records, locks, logs — reconstructible
  turns/                           <agent>.turn.db — not reconstructible
```

**Where that folder is depends on what you are running.** In a checkout it is
the checkout itself — which is what makes the `slife2.yaml` in front of you the
one in use — and otherwise it is `~/.slife2`, per-user and independent of
wherever the command happened to be started. `--data-dir DIR` (or
`$SLIFE2_DATA_DIR`) overrides both.

A checkout therefore keeps generated state in the working tree, which is why
`.gitignore` covers `runtime/` and `turns/`. It does *not* cover `slife2.yaml`:
that file is the point of the arrangement.

The split that remains is the one that matters: deleting `runtime/` costs
nothing, deleting `turns/` costs the memory.

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
├─ tools.py           # the tool registry, plus `now` and `calc`
├─ loop.py            # AgentLoop.run_turn — the turn algorithm
├─ memory.py          # TurnStore: one SQLite file per agent
├─ mcp_server.py      # what it takes to *be* one of our MCP servers — including
│                     #   the client id every one of them keys its state by —
│                     #   and how a client proves which one it reached
├─ memory_server.py   # slife2-memory: `remember` and `recent`
├─ llm/
│  ├─ base.py         # Chunk, Stream, LLMBackend  (no I/O)
│  ├─ wire.py         # Chunk <-> progress payload (no I/O)
│  ├─ client.py       # MCPBackend: the agent loop's only backend
│  ├─ server_common.py# what the model servers share, incl. tool-call assembly
│  ├─ openai_server.py    # slife2-llm-openai             <- imports openai
│  ├─ openai_responses_server.py # slife2-llm-openai-responses <- imports openai
│  └─ anthropic_server.py # slife2-llm-anthropic          <- imports anthropic
├─ server/server.py   # slife2-agent: FastMCP, the conversations and their two
│                     #   tools, keyed by (agent, subagent)
├─ templates/system.j2# the system prompt the distribution ships
└─ tui/
   ├─ app.py          # the Textual App
   ├─ client.py       # AgentClient protocol + the MCP implementation
   ├─ widgets.py      # Transcript, PromptInput, StatusBar
   ├─ attachments.py  # reading `@path` images out of a prompt
   ├─ theme.py        # the palette and glyphs, defined once
   └─ app.tcss
```

The dependency direction is one-way and is what makes each layer testable alone:
`tui/` → MCP → `server/` → `loop.py` → {`llm/base.py`, `tools.py`, `events.py`,
`messages.py`} → `config.py`. The loop imports neither `server/` nor `tui/`.

`mcp_server.py` is a leaf every server sits on: it holds what being one of our
servers means — the flags, the HTTP transport, the record that says a daemon is
here, and the two conventions (`house_server`) that would otherwise be copied
into each server. It is not LLM-specific, which is why it is not under
`llm/`: the memory server and the agent server are not LLM components, and the
scaffold they serve on should not come out of the LLM package.

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
