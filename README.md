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
slife2 [--agent NAME] [--keep-servers]   ensure the servers, then run the TUI
slife2 status                            what is running, and where
slife2 down                              stop the servers this config names
```

**The servers are shared.** A second `slife2` — under any `--agent` — finds them
already running and reuses them; nothing is started twice and nothing is
per-agent. They are daemons, so closing one TUI does not interrupt another, and
when the **last** instance exits it stops what is running. `--keep-servers`
leaves them up instead.

`--agent NAME` (default `slife2`) names the instance. It is a label: it titles
the window, it is passed through to the agent server, and it is **exclusive** —
two live instances may not share a name. It does not partition anything; the
servers stay shared. Isolation, where it is ever needed, belongs inside an MCP
server rather than in the process layout.

Each server is also its own console script, so a process manager can run one
without passing an argument:

```bash
uv run slife2-agent            # the agent loop,             :8000
uv run slife2-llm-openai       # an OpenAI-compatible model, :8001
uv run slife2-llm-anthropic    # the Anthropic Messages API, :8002
```

In the TUI: **Enter** sends, **Shift+Enter** breaks the line, **Ctrl+C** cancels
a running turn (and quits when there is none), **Ctrl+N** starts a new
conversation, **Ctrl+Q** quits.

## Configuration

One `slife2.yaml`, read by all four processes, each taking its own section — so
addresses are written once and cannot drift apart. It is checked in and
documented in place, because **it holds no secrets**: every key in it is a
`${VAR}` reference resolved at runtime. Discovery is `--config PATH` →
`$SLIFE2_CONFIG` → `./slife2.yaml` → built-in defaults, so a checkout works
without any of them.

Secrets are never written in the file. `${VAR}` resolves through shell env →
[credstore](https://pypi.org/project/credstore/) → literal default, and a
`keyring:<service>/<key>` value resolves against the OS keyring directly.
Resolution is lenient — an unresolved reference stays verbatim, so a missing key
fails at the API call where the message can name it, rather than at startup.

## Layout

```
slife2/
├─ launcher.py        # which servers are needed, and attaching to the running ones
├─ runtime.py         # daemon records, logs, and the kernel-backed locks
├─ config.py          # slife2.yaml, and the ${VAR} / keyring: resolution chain
├─ messages.py        # the neutral message model — what crosses `stream_chat`
├─ events.py          # the turn event vocabulary, and its progress encoding
├─ tools.py           # the tool registry, plus `now` and `calc`
├─ loop.py            # AgentLoop.run_turn — the turn algorithm
├─ llm/
│  ├─ base.py         # Chunk, Stream, LLMBackend  (no I/O)
│  ├─ wire.py         # Chunk <-> progress payload (no I/O)
│  ├─ client.py       # MCPBackend: the agent loop's only backend
│  ├─ server_common.py# what the two model servers share, incl. tool-call assembly
│  ├─ openai_server.py    # slife2-llm-openai     <- imports openai
│  └─ anthropic_server.py # slife2-llm-anthropic  <- imports anthropic
├─ server/server.py   # slife2-agent: FastMCP, one stateless `run_turn` tool
└─ tui/
   ├─ app.py          # the Textual App
   ├─ client.py       # AgentClient protocol + the MCP implementation
   ├─ widgets.py      # Transcript, PromptInput, StatusBar
   └─ app.tcss
```

The dependency direction is one-way and is what makes each layer testable alone:
`tui/` → MCP → `server/` → `loop.py` → {`llm/base.py`, `tools.py`, `events.py`,
`messages.py`} → `config.py`. The loop imports neither `server/` nor `tui/`.

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
