# slife2

[中文](README.zh-CN.md)

**slife2 is a terminal-based AI agent.** It runs a conversation with a language
model, exposes tools to that model over the Model Context Protocol (MCP), and
reaches the outside world through local plugin processes. The terminal window is
only a client: the agent loop, the tool hub, the model backends, memory, and
every capability are MCP servers that slife2 starts and shares.

This file is the user manual. The design — architecture, plugin contract,
decisions, and the measurements behind them — is in [SDD.md](SDD.md).

**Contents**

- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Command-line interface](#command-line-interface)
- [Using the terminal UI](#using-the-terminal-ui)
- [Configuration](#configuration)
- [Capabilities](#capabilities)
- [Data locations](#data-locations)
- [Development](#development)
- [Known limitations](#known-limitations)
- [License](#license)

### Terminology

| Term | Meaning |
|---|---|
| **Plugin** | One of slife2's own MCP servers. It is started and shared by the launcher and implements the plugin contract. An external MCP server is not a plugin. |
| **Agent** | An identity named by `--agent NAME`. One name has one live holder on a machine. |
| **Client id** | `(agent, subagent)`: the identity a call is made on behalf of. A server adds the caller's home to the key when it serves more than one. |
| **Conversation** | One client id's history. |
| **Turn** | One exchange: the input, the model's answer, and the tool calls it made, stored as one row. |
| **Channel** | Where a message arrived: `tui`, `wechat`, `peer_agent:<peer>`, `agent:<name>`, `subagent:<name>`, `agent-task:<name>`, `async-tool:<name>`, or `schedule:<name>`. On the local network `agent:` names the maker and `subagent:` a subagent, so a subagent can tell whose message it is answering. |
| **Marker** | The machine-readable prefix a channel adds to a delivered message, e.g. `[Wechat:{…}]`. Markers are stored with the message and removed only for display. |
| **Family** | A plugin that owns one configuration section or folder and declares the rows it holds to the hub. |
| **Source** | The name a plugin files rows under: an `mcp:` server, a `rest-api:` document, or the whole of a `skills`, `cli`, or `job` collection. |
| **Catalogue** | The machine-readable list of every tool, playbook, job, and command the model may discover. |
| **Job** | Code the model wrote for the home: one Python function in `jobs/`, exposed as the tool `job__<name>`. |
| **Dump** | A tool output stored whole in the cabinet because it was too large for the conversation. |
| **Home** | A data directory: configuration, `AGENTS.md`, skills, jobs, agents, and their files. |
| **Runtime directory** | The machine-wide records, locks, and logs of the shared servers. |

## Requirements

- **Python 3.13 or newer**
- **[uv](https://docs.astral.sh/uv/)** for dependency management and execution

slife2 is tested on Windows, macOS, and Linux.

The installation script below installs the external programs the current
`slife2.yaml` requires, upgrades existing ones, and verifies the result — and
prints the configured install line for a missing `cli:` command it cannot
provide. A manual installation uses Python 3.13 and uv alone.

### SQLite extension support

The vector index is a SQLite extension (`sqlite-vec`). The Python interpreter
must be able to load SQLite extensions.

Windows and Linux builds normally have this support. On macOS, neither uv's
managed CPython nor Apple's `/usr/bin/python3` can load extensions. Use
Homebrew's Python instead:

```bash
brew install python@3.13
uv venv --python "$(brew --prefix python@3.13)/bin/python3.13"
uv sync
```

Without extension loading, the database cannot be opened, no plugin starts, and
the first command fails with:

```text
this Python's sqlite3 cannot load extensions
```

### External programs (required when configured)

Every program named by a server, a tunnel or a skill is required. The installer
provides each one at the user level and stops with the configuration entry named
when a program is unavailable.

A `cli:` entry is the exception: it records a program the machine may already
have, and its `install:` line is a prompt rather than an instruction. When such
a program is missing and the installer has no way to provide it, the run prints
that line at the end — beside the `credstore set` hints — and still succeeds.
A `cli:` entry with no `install:` line is still a required program.

| Program | Named by | Provided by the installer |
|---|---|---|
| `mosquitto` | `peer_agent:` / `subagent:` broker | A running broker on `127.0.0.1:1883` is used directly. Debian/Ubuntu packages unpack under `~/.local`; macOS uses Homebrew; Windows uses a running broker or prints the manual install command. |
| `cloudflared` | `file-tunnel.providers.cloudflare` | The official binary in `~/.local/bin` (or `%LOCALAPPDATA%\slife2\tools`); Homebrew on macOS |
| `ssh` | `file-tunnel.providers.localhost.run` | The system OpenSSH client, included with Windows 10+, macOS, and Linux |
| `npx` | `mcp:` entries with `command: npx` | The official Node.js LTS archive under `~/.local/lib/nodejs` (or `%LOCALAPPDATA%\slife2\tools\node`); its packages download on first use |
| `uvx` | `mcp:` entries with `command: uvx`, and `rest-api:` entries | uv |
| `yt-dlp` | `cli:yt-dlp` | `uv tool install yt-dlp` |
| `browser-harness` | `cli:browser-harness` | `uv tool install --python 3.12 browser-harness` |

Skills may also declare required programs in their headers (`requires.bins`);
those programs are required as well. The included `baidu-search` skill, for
example, declares `python3`. The installer ensures the `npx` and `uvx` launchers
exist; the packages those launchers fetch are downloaded on first use.

On Windows, `python3` comes from uv's `--default` launchers in
`%USERPROFILE%\.local\bin`.

## Installation

### One-click installation

macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.sh | bash
```

Windows 10/11 (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.ps1 | iex"
```

The command downloads the latest `main`, installs slife2 into an isolated
environment, and installs the external programs required by the current
configuration. Data and configuration live in `~/.slife2`; binaries and
libraries live in `~/.local` (`%LOCALAPPDATA%\slife2` on Windows). Run the same
command again to upgrade.

Python 3.13 is installed only when the machine has none. A later run leaves the
interpreter as it is — `uv python upgrade 3.13` moves it to a newer patch —
because uv keeps the patch it replaces for the virtualenvs that may point into
it, and re-running the installer should not add another ~100 MB copy each time.

The installer also seeds the bundled `skills/` and `jobs/` into the data
directory, **overwriting files of the same name**; a skill or job you added
under another name stays untouched. Editing a seeded file means editing a
copy — the next install replaces it.

`AGENTS.md`, the model's guide to its own tools, is placed beside `slife2.yaml`
and is **never overwritten**: it is a document you edit, so a changed shipped
copy is written as `AGENTS-default.md` for you to compare and merge. The same
rule protects your `slife2.yaml`.

From a source checkout, run the installer in that directory:

```bash
./scripts/install.sh
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

`scripts\install.cmd` also starts the installer. A source directory uses its own
`slife2.yaml` and `.venv`.

| Option | Meaning |
|---|---|
| `--dry-run` | Report the planned actions. |
| `--help` | Show usage and options. |

To uninstall:

```bash
curl -fsSL https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.sh | bash -s -- uninstall
```

```powershell
powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.ps1))) uninstall"
```

`uninstall` removes the components the installer added; components that were
present before the installation stay in place. A component that could not be
removed stays recorded, and a later `uninstall` retries it.

### Manual installation

To install from source manually:

```bash
git clone https://github.com/juzcn/slife2.git
cd slife2
uv sync
```

Store the API key of the provider you intend to use. It can be placed in the OS
keyring through `credstore`, or exported as an environment variable:

```bash
uv run credstore set DEEPSEEK_API_KEY
# or
export DEEPSEEK_API_KEY=...
```

`slife2.yaml` is required: slife2 has no built-in configuration to run without
one. The installer seeds the shipped file into a new home, and a checkout uses
the `slife2.yaml` beside its code.

`AGENTS.md` is required too: it is the model's guide to its tools, read from
the data directory when a conversation starts. The installer seeds the shipped
one, and a checkout uses the `AGENTS.md` beside its code.

### Docker

A container distribution is built from a checkout. It runs the project's own
installer and uses the shipped `slife2.yaml` unchanged, including
`desktop-commander` and the rest of the configured tools.

```bash
docker build -f docker/Dockerfile -t slife2:local .
```

The image runs as the non-root user `slife2`; its checkout and data directory
are `/opt/slife2`, and its runtime directory is
`/home/slife2/.slife2/runtime`. It is about 3 GB because it contains the
programs the full configuration names.

The image has no OS keyring, so it keeps its keys in credstore's encrypted file
inside a volume. Create it once and store every key the configuration names —
one `credstore set` per key:

```bash
docker volume create slife2-cred
docker run --rm -it -v slife2-cred:/home/slife2/.credstore \
  --entrypoint bash slife2:local
# in that shell:
credstore set-password            # choose the master password
credstore set DEEPSEEK_API_KEY    # then every line the build log listed
```

The image's build runs the same `scripts/install.sh` as a host install, so its
log lists every `credstore set <KEY>` line the shipped configuration needs —
run those in the shell above. Run one prompt and exit; the image asks for the
master password you set above, and every `${VAR}` in the configuration resolves
from the file:

```bash
docker run --rm -it -v slife2-cred:/home/slife2/.credstore \
  slife2:local 'Use the shell tool to run pwd and report the output.'
```

Or start the agent headless and keep it running (it prompts the same way):

```bash
docker run --rm -it -v slife2-cred:/home/slife2/.credstore slife2:local
```

Keys are configured in the container's credstore, not on the command line. The
default provider needs `DEEPSEEK_API_KEY`; the rest of the configuration's
`${VAR}` references need their own keys for the full tool set, which is why the
credstore file is the path. A mounted `slife2.yaml` whose values are literals
(mounted over `/opt/slife2/slife2.yaml`) is the other supported source. Mount
`/opt/slife2/agents` and `/home/slife2/.slife2` if conversations and logs should
outlive the container.

See [docker/README.md](docker/README.md) for the credential setup, the one-shot
prompt, and the container-specific notes.

## Quick start

After the one-click installer, the dependencies are already synced and
`credstore` is installed. Set the key and start the window:

```bash
credstore set DEEPSEEK_API_KEY
slife2
```

From a source checkout, start it with `uv run slife2` instead.

A manual installation starts one step earlier:

```bash
uv sync
uv run credstore set DEEPSEEK_API_KEY
uv run slife2
```

`slife2` starts the MCP servers the configuration needs, reuses any that are
already running, opens the terminal UI, and stops the servers when the last
instance exits.

```bash
slife2 status    # what this configuration's servers are doing
slife2 down      # stop them
```

## Command-line interface

```text
slife2 [COMMAND] [OPTIONS]

Commands:
  run       Ensure the servers are available, then run the TUI (default).
  status    Report what each server named by the configuration is doing.
  down      Stop the servers named by the configuration.
  logs      Read a server's log, or list the logs that exist.
  probe     Ask a running agent one task over the peer network.
```

`down` stops the shared servers and the broker; it does not close windows that
are already open. A window left open reconnects on its next prompt once the
servers are started again, and its conversation is restored from the turn log.
Its agent name stays claimed until the window exits, so a new instance started
with the same `--agent` name is refused until then. `down` also refuses while
another live client is using the servers; `--force` stops them anyway and
interrupts that client's in-flight turns.

Options:

| Option | Meaning |
|---|---|
| `--data-dir DIR` | Use `DIR` as the data directory. Overrides `SLIFE2_DATA_DIR`. |
| `--agent NAME` | Run as `NAME`, or ask `NAME` with `probe`. Default: `slife2`. One live instance per name per machine. |
| `--model PROVIDER/MODEL` | Start on this model. A bare provider selects its first model. Default: the configuration's `default`. `run` only. |
| `--url URL` | Connect the window to an agent server at `URL` instead of the configured one. |
| `--a2a-broker HOST:PORT` | Use this broker for the peer-agent network for this run. It overrides `peer_agent.broker` only, never the subagent network's broker. It cannot enable a network that has no `peer_agent:` section, and it is refused when the A2A server is already running on another broker; stop the stack first. |
| `--keep-servers` | Leave the shared servers running after this instance exits. |
| `--headless` | Run the same window, servers, connections, and turns without a terminal: nothing is drawn, and the start-up report is the only output. The agent remains reachable on the peer network. Exit with Ctrl+C. |
| `--lang {en,zh}` | Set the window's language. Without the flag, the language is detected from the operating system. |
| `--json` | Print `status` as JSON. The object carries `ok`, `servers`, and `broker`; each server row has its log path. |
| `--force` | Stop the shared servers even when another client is using them. `down` only. |
| `--prompt TEXT` | The task to ask a running agent. `probe` only. |
| `--timeout SECONDS` | How long `probe` waits for the task's completed reply. Default: 3600. `probe` only. |
| `--channel {a2a,tui}` | Which road a `probe` asks on: the A2A peer network (default), or an agent task's delivered answer. `probe` only. |
| `--lines N`, `-n N` | How many log lines `logs` shows; `0` reads the whole log. Default: 50. |
| `--all` | With `logs`, print every configured server's log under its own header. |
| `--version` | Print the version and exit. |

`slife2 logs` lists each configured server's log file and whether it exists.
`slife2 logs agent` prints the end of one, and `slife2 logs --all` prints every
one. The name is the one `status` prints (`agent`, `llm:openai-completions`,
`a2a-broker`); the MCP name a server advertises also works.

`status` and `down` exit 0 only when the stack is fully usable: `status`
returns 2 if any configured server or the broker is not running, and `down`
returns 2 if a server or the broker did not stop or the command was refused
because another client is live. `run` returns 2 when a server cannot start.

`--headless` does not create a different kind of agent. It runs the same window
with no display, and the system prompt is told that nobody is watching so the
model can adjust its behavior.

`slife2 probe --agent NAME --prompt '…'` asks a running agent — a headless one or
an open TUI window — one task. `--channel a2a` (the default) is a mock peer: it
sends a `task_request` on the peer network, the target window runs the turn, and
the model's `task_response` is the answer; it requires a `peer_agent:` section.
`--channel tui` asks through an agent task: the task runs one loop of the
agent's own (no inbox, no cut-ins), and the task's delivered
`agent-task:<name>` message is the answer — not the window's assistant text,
which answers everything folded into its turn. The TUI road needs a window for
the delivery. Both modes exit 0 on a completed answer, 2 on a usage error, and
3 when no completed answer comes back. The container's one-shot mode runs the
default A2A road.

`--lang` affects the window only: labels, harness lines, panels, the model
picker, the approval row, and the status bar. System prompts, tool schemas, and
log lines remain English. External text, such as tool results and peer messages,
is passed through in its original language.

### Plugin executables

Each plugin can also be started directly. This is useful for a process manager
or for debugging a single server.

| Executable | Server | Default port |
|---|---|---|
| `slife2-agent` | Agent loop and conversations | 24000 |
| `slife2-llm-openai` | OpenAI-compatible chat completions | 24001 |
| `slife2-llm-anthropic` | Anthropic Messages | 24002 |
| `slife2-llm-openai-responses` | OpenAI Responses | 24003 |
| `slife2-llm-embeddings` | Embeddings | 24004 |
| `slife2-tui` | Terminal inbox, one session per agent | 24005 |
| `slife2-context` | Turn log and context decision | 24010 |
| `slife2-toolhub` | Tool catalogue and model tool list | 24020 |
| `slife2-wechat` | WeChat login and messaging | 24030 |
| `slife2-skills` | Playbooks and `skill_use` | 24031 |
| `slife2-cli` | The `cli:` section | 24032 |
| `slife2-jobs` | Code-defined tools (`job__<name>`) | 24042 |
| `slife2-mcp-tools` | The `mcp:` section | 24033 |
| `slife2-restapi-tools` | The `rest-api:` section | 24034 |
| `slife2-cabinet` | Notes, diary, files, reports, dumps | 24035 |
| `slife2-media` | Image, video, speech, transcription | 24036 |
| `slife2-agent-task` | Background agent tasks | 24037 |
| `slife2-file-tunnel` | Public HTTPS URL for a local file | 24038 |
| `slife2-schedule` | Scheduled tasks and their ledger | 24039 |
| `slife2-a2a` | Peer-agent network over MQTT | 24040 |

The subagent network is served by the same module on port 24041 and is started
as `python -m slife2.plugins.a2a.local_server`; it has no separate console
script.

**On WSL2 with `networkingMode=mirrored`, the WSL instance shifts these ports
by 100** (agent 24100, toolhub 24120). The local MQTT broker is not shifted:
both stacks share it, which is what makes them peers. Mirrored mode
makes the Windows and WSL loopbacks one, so a WSL stack on the default ports
would be visible to — and silently reused by — a Windows instance. The Windows
side keeps the documented ports. `wsl_port_offset` in the WSL `slife2.yaml`
changes the shift: it defaults to 100, may be `0` to disable it, and is
ignored outside WSL's mirrored mode.

## Using the terminal UI

### Keys

| Key | Action |
|---|---|
| `Enter` | Send the message. |
| `Shift+Enter` | Insert a line break. |
| `Esc` | Interrupt the running turn. |
| `Ctrl+C` | Copy when something is selected; otherwise interrupt the running turn; otherwise quit. |
| `Ctrl+S` | Open the model picker. `↑`/`↓` move, `Enter` selects, `Esc` closes. |
| `Ctrl+O` | Open the subagent picker. Choosing one opens a read-only view of its conversation; `Esc` closes it. |
| `Ctrl+Q` | Quit (the standard Textual binding). It is not shown in the status bar because terminals with flow control may consume it before the application sees it. |
| `PageUp` / `PageDown` | Page the transcript. |
| `Home` / `End` | Jump to the start or end of the transcript. |
| `Ctrl+A` / `Ctrl+E` | Move to the start or end of the draft. |

The mouse wheel scrolls the transcript wherever the pointer is. Scrolling up
holds the view in place while new tokens arrive; sending a message returns it to
the end.

### Sending messages

If a message is sent while a turn is running, it is queued. The running turn
finishes, then the queued message runs. The status bar shows the queue depth.
`Esc` interrupts the running turn but leaves the queue intact.

A message that arrives while a turn is running is delivered to that turn at the
next iteration boundary, not held for a later turn. This applies to text typed
in the window, messages handed over by another channel, and input to a headless
window.

### Model selection

`Ctrl+S` lists every model under `providers:` and marks the one in use.
Selecting a model calls the same `model_switch` tool the model itself can call.
The conversation keeps its history and moves to the new model on its next turn;
a turn already in progress finishes on the model it started with. The status bar
updates its context window and capability marks.

### Watching a subagent

`Ctrl+O` lists the subagents this agent has made and opens a read-only view of
the one you choose. The view shows the stored conversation and then the live
turn — the message that woke it, its tool calls, its reasoning, and its answer —
as it happens, including a turn already in progress when you open it. `Esc`
returns to the window; the window's own conversation kept running underneath and
is unchanged.

The view is read-only on purpose: a subagent has no window, and watching does
not make you one. The prompt is not shown, and an approval request is not put to
you — a subagent's turns run unattended, while its maker (or a peer) gets the
answer as a `Subagent(<name>)>` message in its own conversation. In the watched
conversation the message that woke it wears the maker's bubble,
`Agent(<name>)>`; a message from a sibling subagent wears `Subagent(<name>)>`.

### Restoring a conversation

Closing and reopening the window, restarting the servers, or waiting long enough
for the idle sweep does not start a new conversation. The transcript is restored
with its original timestamps, tool calls, and results. Reasoning is folded away
where it was folded in the live conversation.

The status bar also reports the context size left by the last model call and
its fraction of the model's window. Before each turn, a note names what the
context decision kept and what it recalled from the turn log.

It reports `tools 26/358` as well: what the hub's loaded set holds, then how
many entries the catalogue has. The total includes every category, so skills,
jobs and CLI entries are counted and so are the tools of a server that is down,
while the loaded half is what the next request carries. Both numbers come from the same
ask, so they are as current as the last model call, and the start-up line names
the same pair before the window opens.

### Images

Name an image file in a sentence:

```text
What is wrong with this layout? D:\shots\screenshot.png
```

The model calls `attach_image` for it. There is no special syntax. A call can
take a local path, an `http(s)` URL, or a `data:` URI, and can take several at
once. A source that cannot be read is named in the answer without failing the
rest. A URL is passed to the provider, which fetches it.

Images are available only when the model's entry lists `image` under `input`.
The live conversation carries the pixels; the turn log keeps the sentence, the
tool call and answer naming the file, and an `[Image:…]` reference to a cabinet
copy keyed by its content digest. The pixels never enter the row. A restored
conversation is therefore text plus that reference, and a later turn that needs
the picture calls `attach_image` with the reference's key — the cabinet answers
a `data:` URL — or with the image's URL. A provider that refuses an image drops
it and says so; that turn's row is note-only.

## Configuration

### Location

The configuration file is `slife2.yaml` inside the data directory. A data
directory is the checkout when running from a checkout, otherwise
`~/.slife2`. Use `--data-dir DIR` or `SLIFE2_DATA_DIR` to override it.

The file is required, and it must name at least one provider under `providers:`
and one embedding provider under `embeddings:`.
A missing configuration file stops the start with a one-line error naming the
directory that was searched, and so does a file that exists but cannot be
loaded, naming the file and the mistake.

The installer may write `slife2-default.yaml` beside an existing configuration
as a reference. slife2 reads only `slife2.yaml`; the reference is for a person
to compare and merge by hand.

`AGENTS.md` sits beside the configuration and is required as well. It is the
model's guide to its tools, included in the system prompt every conversation
opens with; edit it by hand, and the next conversation reads the new text. The
installer seeds it under the configuration's rule, writing `AGENTS-default.md`
when your copy differs.

`slife2.yaml` is safe to check in. It should contain no secrets; every
credential is a `${VAR}` or `keyring:` reference resolved at runtime.

### Sections

| Section | Purpose |
|---|---|
| `servers:` | Address and port of each plugin server. Defaults are used when absent. |
| `providers:` | Chat providers and their models. |
| `default:` | The model a new conversation starts on, as `provider/model`. |
| `agent:` | The iteration cap and the system-prompt template. |
| `agent-task:` | `max_running`, the number of background tasks per agent. |
| `subagent:` | `max_running`, and the broker and `org` for the local subagent network. |
| `embeddings:` | Embedding providers used by the turn, tool, and cabinet indexes. |
| `context:` | Limits for the context decision. |
| `tool_load:` | The loaded-tool cap, and the tools that start loaded and are never evicted. |
| `tool_result:` | Token budget for a single tool result. |
| `mcp:` | External MCP servers (stdio or HTTP). |
| `rest-api:` | OpenAPI documents exposed as tools. |
| `cli:` | Commands already installed on the machine. |
| `skills:` | Environment for playbooks under `skills/`. |
| `media:` | Image, video, speech, and transcription providers. |
| `file-tunnel:` | The public-URL provider for local files. |
| `peer_agent:` | The remote peer-agent network. |

There is no `wechat:`, `cabinet:`, `schedule:`, or `tui:` section. A WeChat login
is created by scanning a QR code and stored under the agent's directory. The
cabinet, scheduler, and terminal inbox are enabled by their servers and by the
existence of the data they hold.

### Secrets

A value may be:

- a literal string;
- `${VAR}`, resolved through the shell environment, then
  [credstore](https://pypi.org/project/credstore/), then an optional literal
  default (`${VAR:-default}`);
- a `keyring:<service>/<key>` reference, resolved directly against the OS
  keyring.

**Only credstore-managed secrets are protected from output.** A credential
slife2 resolves from credstore — a `keyring:` reference, or a `${VAR}` the
credential store answers — is shown in its masked form (the first four and last
four characters when at least as many stay hidden, otherwise `***`) in tool
results, tool listings, and log lines. A value shorter than eight characters is
not registered at all: masking it would mangle every line it appeared in, and no
real provider key is that short. An environment variable or a literal
value is the operator's own, and slife2 does not try to recognise it. Keep every secret in credstore: it is the one store
whose values slife2 masks everywhere they would otherwise appear.

Resolution is lazy and lenient. An unresolved reference is left verbatim, so a
missing key fails at the API call where the error message can name it rather
than at startup.

### Example

```yaml
agent:
  max_iterations: 0          # 0 or absent means no cap
  # system_prompt: prompts/jack.j2

agent-task:
  max_running: 4

subagent:
  max_running: 32
  provider: mosquitto
  broker: {host: localhost, port: 1883}

context:
  ceiling: 0.8               # fraction of the model's window
  floor: 0.2
  recall_limit: 40
  min_similarity: 0.45
  timeout: 20.0              # seconds for the discriminator call

tool_load:
  threshold: 100             # loaded tools before the least recently used go
  autoload: [tool_search, func_tool_load, _func_tool_unload,
             _check_new_input, skill_use, check_async, cancel_async,
             turn_read]                                # and never evicted

tool_result:
  tokens: 8000               # per-result budget; minimum 128

mcp:
  arxiv:
    url: https://arxiv.mcp.brunosan.de/mcp
  serper:
    command: npx
    args: [-y, serper-search-scrape-mcp-server]
    env:
      SERPER_API_KEY: ${SERPER_API_KEY}

rest-api:
  github:
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.yaml
    base_url: https://api.github.com
    api_key: ${GITHUB_TOKEN}

cli:
  yt-dlp:
    command: yt-dlp
    description: Download video from 1000+ sites, subtitles and playlists.
    install: uv pip install yt-dlp

skills:
  baidu-search:
    env:
      BAIDU_API_KEY: ${BAIDU_API_KEY}
```

The shipped `slife2.yaml` is annotated in place and is the reference for every
key.

### Editing the configuration

The configuration is edited by hand or by the model. Each section or folder has
its own tools:

| Section or folder | Tools |
|---|---|
| `mcp:` | `mcp_set`, `mcp_list`, `mcp_list_tools`, `mcp_remove`, `mcp_set_enabled` |
| `rest-api:` | `rest_api_set`, `rest_api_list`, `rest_api_list_tools`, `rest_api_remove`, `rest_api_set_enabled` |
| `cli:` | `cli_set`, `cli_list`, `cli_remove`, `cli_set_enabled` |
| `skills:` | `skill_draft`, `skill_set`, `skill_edit`, `skill_list`, `skill_remove`, `skill_set_enabled` |
| `jobs/` (a folder) | `job_set`, `job_list`, `job_read`, `job_run`, `job_remove` |
| `providers:` and `default:` | `model_set`, `model_list`, `model_remove`, `model_switch` |

All writes go through one module (`slife2.libs.host.configfile`) under a
cross-process lock. The write is a document edit that preserves comments, and
the result is validated by the same loader used at startup. An entry the loader
refuses is put back and the caller is told why.

`set` writes the whole entry: a field that is left out is removed. `set_enabled`
changes only the `enabled` field. There is no approval gate before a write; in
particular, `mcp_set` with a `command:` records a program slife2 will start.

## Capabilities

### Conversation and memory

A conversation does not accumulate an unbounded context. Before each turn,
`slife2-context` makes one model call that decides which turns to keep and which
to recall from the turn log; the turn runs on the two selections in time order.
If a recall matches more than fits, only what fits is returned. A failed decision
keeps the context in hand; a missing turn store fails the turn.

The model browses its history with `turn_list`, searches it by what a turn was
about with `turn_search`, reads one whole with `turn_read`, and writes a summary
and tags with `turn_summarize`. `turn_list` is newest first, one line per turn,
with `since`/`until` accepting an ISO date or a phrase such as `yesterday`,
`last month`, or `3 days ago`, and `limit`/`offset` paging against `total`.
`turn_search` takes the same time window and returns the same line plus a
`similarity` score; its `query` words rank exact matches first but are not a
filter. None of the tools takes an agent argument: the call carries the
conversation, so a model reads its own history only.

Each turn opens with a `[TURN-PROMPT:{…}]` marker carrying `now` and
`token-usage`; it is prompt text for the turn being answered and is not
stored. Each turn in a rebuilt context also carries a `[TURN:{…}]` footnote
naming its id, channel, and span — a record of when that turn ran, so its date
can be older than today. Capitalized markers are harness annotations; a marker
that reads as a word, such as `[Wechat:{…}]`, was written by a sender and is
stored with the message.

The status bar reports the context size left by the last model call and its
fraction of the model's window. Before each turn, a note names what the context
decision kept and what it recalled.

### Tools

`slife2-toolhub` owns the model's tool list. `slife2-mcp-tools` holds the `mcp:`
connections and `slife2-restapi-tools` the `rest-api:` connections; the hub
merges what they declare, names the tools, gates them, and routes calls back.

The hub serves three set-level tools:

- `tool_search` searches the whole catalogue by keyword and by meaning. Pass
  `unloaded_only` to leave out the tools already in the list; skills and CLI
  entries have no load state, and an unloaded job is not loaded yet, so all of
  them are still included.
- `func_tool_load` puts one or more tools into the list shown to the model.
- `_func_tool_unload` removes tools from that list. The harness also calls it
  at a turn boundary to enforce the budget.

A model can call a name returned by `tool_search` without loading it, but
loading is what keeps it in the list sent with every request.
`tool_load.autoload` names the tools that start loaded, by the name the model
calls them — a plugin's bare name (`turn_read`) or an external server's
`{server}__{tool}`. Everything else is discovered with `tool_search` and loaded
with `func_tool_load`.

A call for a tool whose catalogue is still starting answers "not in the
catalogue yet"; retry it in a few seconds rather than searching for a
replacement.

The loaded list is bounded by `tool_load.threshold` (default 100). Over the
limit, the least recently called tools are unloaded; nothing in
`tool_load.autoload` is ever evicted. The trim happens at a turn boundary and is
written into the conversation as a tool call and its result, so the model learns
that the list shrank.

Tool names:

- A tool from an `mcp:` entry is `{server}__{tool}`, for example
  `arxiv__arxiv_search_papers`.
- A plugin's own tools use bare names, for example `turn_read`.

A tool result is a JSON object, except for the two answers whose text is the
thing itself: `_check_new_input` returns a person's message verbatim, and
`skill_use` returns the playbook text. A listing answers
`{"<noun>": [], "total": N, "limit": L, "offset": O}`; `total` says whether
another page exists, and `L` is the page size the answer applied. A read answers
the row's own fields. A refusal or a miss raises a `ToolError`, which the harness
reports as a failed call with the reason.

No answer is intended to exceed `tool_result.tokens`. Listings that can grow use
`limit`/`offset`; an output that cannot be bounded is stored as a dump (see
[Cabinet](#cabinet)).

#### Tool call options

Every function tool accepts three meta-parameters. They are added to every
advertised schema and stripped before dispatch, so no tool server sees them.

| Parameter | Effect |
|---|---|
| `_timeout` | Seconds for this call. It overrides the tool's default. `0` or a negative value means "use the default", never "no limit". A tool that declares its own `timeout` argument enforces it itself. If neither applies, the call runs until it finishes. There is no default timeout. |
| `_async` | Run the call in the background. The model receives a `task_id`; the result is delivered back into the conversation with an `[AgentToolResult:{…}]` marker and drawn on the `async-tool:<tool>` channel. `check_async` reports whether the call is running, finished, failed, or cancelled; `cancel_async` abandons it. Backgrounding removes the wait, not the result budget. An agent task's loop has no `_async`. |
| `_approve` | Ask the user before running the call. The terminal draws an inline row (`y` approves, `n` or `Esc` denies) and answers with `answer_approval`. If the channel has no person behind it — WeChat, a scheduled run, another agent, or a client that cannot draw the prompt — the call runs without asking. A caller that can answer says so with `send_message(..., watched=true)`. |

#### Skills and commands

Playbooks live in `<data>/skills/<name>/SKILL.md`. `slife2-skills` serves
`skill_use`, which returns the playbook's text preceded by its directory.
Installing a skill is dropping a directory into `skills/`; the tool reads the
disk on every call, so no restart is needed.

A skill can declare environment requirements in its header. The values come from
the `skills:` section, resolved through the same chain as any other secret.
`skill_use` reports a missing key before the model runs an instruction that
depends on it. `skill_set` writes that entry's `env:` when it is given one —
omit it on a reinstall to keep what is there — and `skill_remove` deletes the
entry along with the folder.

`skill_draft` turns a workflow into a `SKILL.md` without writing anything: its
answer is the `files` argument `skill_set` or `skill_edit` takes, so a procedure
the model just followed can be saved, read back and corrected before it is
installed. A new skill is `skill_set`; a change to one already installed is
`skill_edit`, which writes only the files it names and keeps the rest — the safe
way to fix a step without deleting the scripts beside it.

`cli:` entries describe programs already installed on the machine. Every entry is
a catalogue row (`cli:yt-dlp`), so `tool_search` can find it by what it does.
The row is not a callable tool and has no load state. No tool executes a `cli:`
entry yet; `func_tool_load` reports that. An entry's `install:` line is what is
shown when the program is missing: the catalogue row carries it, and so does
the installer's closing hint.

`enabled: false` keeps an entry configured but never connects it. The value must
be a real YAML boolean: `no`, `off`, and `'false'` are strings under YAML 1.2
and are refused rather than guessed. A disabled entry's tools remain in the
catalogue, marked `disabled`, so searches still find them.

#### Jobs

`job_set` writes a **job** — one Python file in `<data>/jobs/`, one module-level
function named after the file — and that function becomes a tool the model calls:
`job__<name>`. The docstring is the tool's description and the typed signature is
its parameter schema; there is no manifest to keep in step. The same call
replaces a job: the new file is loaded before the swap, and the previous version
is restored if it does not load. `job_remove` deletes the file and the tool;
`job_list` / `job_read` / `job_run` list, read and test one. A broken file stays
visible as an `error` row rather than disappearing.

A job may be a small deterministic tool or a workflow — its code is plain
Python, and it composes the rest of the system through two generic handles from
`slife2.job`:

- `call_tool(name, arguments)` — the hub's catalogue, by the name the model
  calls: a plugin's own tool, an external `{server}__{tool}`, or another job.
- `call(server, tool, arguments)` — one of slife2's servers by its `servers:`
  key: an agent turn (`send_message`), a raw model call (`stream_chat`), or any
  plugin's own tool.

Nothing in the runtime enumerates the capabilities. `servers()` reads the
config's one table, so a plugin or tool another change adds is usable from a job
without changing the job runtime. The code runs in the jobs plugin with the
caller's identity; it is not sandboxed, and the top-level tool call is where a
person can be asked to approve it.

The `job-coding` skill is the authoring guide: it tells the model to run the
project's `ruff` and `pyright` over the file it wrote and fix the findings
before it calls the job done. Both tools are normal dependencies, so a
shell/terminal capability is all the check needs.

### Media

The `media:` section provides four capabilities:

- `generate_image` — draw an image from a prompt.
- `generate_video` — render a video from a prompt or an image.
- `text_to_speech` — write speech to an audio file.
- `transcribe_audio` — transcribe an audio file, paged by `offset` and `limit`.

Each model declares what it is for:

```yaml
media:
  defaults:
    image: bailian_personal/wan2.7-image
    video: bailian_personal/happyhorse-1.1-t2v
  providers:
    bailian_personal:
      api: dashscope-aigc
      base_url: https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1
      api_key: ${QWEN_TOKEN_PLAN_PERSONAL}
      models:
        - {model: wan2.7-image, kind: image}
        - {model: happyhorse-1.1-t2v, kind: video, params: {resolution: 720P, duration: 5}}
        - {model: qwen-audio-3.0-tts-plus, kind: tts, voice: longanhuan_v3.6}
        - {model: qwen-audio-3.0-asr-flash, kind: asr}
```

The supported `api:` values are `dashscope-aigc` (images, video, speech,
transcription) and `openai-images` (image generation only). Choosing a model
that does not support the requested kind is refused.

Generated files are written under `<data>/agents/<agent>/media/`. A `folder`
argument creates a subdirectory inside that root; a path outside it is refused.
A long-running video call that exceeds its timeout is refused with the
provider's job id rather than its result.

### File tunnel

`slife2-file-tunnel` publishes a local file at a public HTTPS URL so a service
that cannot read the local disk can fetch it:

```text
expose_local_image(path)      -> https://<tunnel>/share/<token>
unexpose_local_image(token)   -> the URL stops working
```

The file is served from its original location and is not copied. A link stops
working when:

- the file changes (the token is pinned to the file's identity and metadata);
- `unexpose_local_image` is called with the token;
- the process exits.

A share outlives the turn that created it; it is withdrawn by the model or the
operator. Private keys, `.env` files, and saved passwords are refused by name.

Three providers are supported. Exactly one is active; the others may stay
configured and inert.

| Provider | Requirement | URL behavior |
|---|---|---|
| `ngrok` | `NGROK_AUTHTOKEN` | One stable development domain per account. The free tier shows an interstitial page to browsers. |
| `localhost.run` | An `ssh` client | A random `*.lhr.life` hostname that rotates every few hours. TLS terminates at the provider, which relays the bytes. |
| `cloudflare` | The `cloudflared` binary | A `*.trycloudflare.com` hostname that is stable for the process's lifetime. |

The built-in default is `ngrok`. The shipped `slife2.yaml` selects `cloudflare`,
which needs no account and relays the bytes to nobody; set `active:` to
`localhost.run` if `cloudflared` is not installed.

A tunnel that is not up, or that has published a URL that cannot be reached, is
refused rather than handed out. The `tunnel_status` tool reports the state, URL,
reachability, edge address, and the binary in use; it is an internal diagnostic
tool and is not exposed to the model.

### Cabinet

The cabinet is a per-agent store of notes, diary entries, saved files, reports,
dumps, and images.

| Kind | Key | A second write |
|---|---|---|
| `note` | A subject chosen by the user | Replaces the note. |
| `diary` | A date | Appends a section. |
| `file` | Its path inside the cabinet | Copies the file in. |
| `report` | A title | Appends a section. |
| `dump` | The tool call that produced the output | Written by the harness. |
| `image` | Its content digest | Written by the harness; bytes in `pictures/`. |

Notes, files and images are documents: the second write is their content, which
is why only notes and files have remove tools. A diary and a report are logs:
two entries under one date or title are two facts.

A dump is a tool output too large for the conversation. The output is stored
whole, keyed by the tool call id, and the conversation carries a selection of the
output's own lines within `tool_result.tokens`; nothing is paraphrased. The
model reads any part back with `dump_read`, a searching reader: `pattern` finds
lines, `context` adds their neighbours, `offset` reads from a line (negative
counts from the end), and `limit` reads fewer lines. A search does not see dumps
unless `kind` is `dump`.

An image is stored whole under `pictures/`, keyed by its content digest, and the
turn row carries an `[Image:…]` reference instead of the pixels. `image_source`
resolves the reference and `attach_image` is how the model sees the picture
again; an `http(s)` image is fetched by the provider, not here. A no-kind search
sees images.

Every text document — a note, diary entry, report, or dump — is written twice: a
row in the cabinet index and a Markdown file under `agents/<agent>/cabinet/`
(`notes/`, `diary/`, `reports/`, `dumps/`), which can be opened, edited, or
deleted without slife2. A saved file's bytes live under the cabinet and are not
indexed, and it has no Markdown mirror. `file_read` returns a file's text and
refuses a PDF, image, or archive by saying what it is. Text in another encoding
is decoded and the encoding is named in the answer.

`cabinet_search` accepts `keywords` and `sentences` like `tool_search`.
`cabinet_summarize` writes a description for a document; writing one for a file
or an image re-embeds it. `file_save` copies files already on the machine and
does not fetch URLs. The cabinet has no `grep` search leg.

### WeChat

`slife2-wechat` connects one WeChat account per agent through iLink's hosted
bridge. A login is scanned rather than configured. `wechat_login` returns a QR
code; scanning it saves a token to `agents/<agent>/wechat.yaml`, where the next
start reads it. A login lasts about a day; `wechat_check_status` reports the
remaining time.

One WeChat account holds one live login. Scanning the same account for a second
agent or on a second machine silently retires the first login, which then
receives `session timeout` from the bridge. The first agent's window shows
`[✗ Wechat offline]`, and `wechat_check_status` gives the reason. Scanning again
takes the account back.

| Tool | Purpose |
|---|---|
| `wechat_login` | Fetch a QR code and start watching for the scan. |
| `wechat_check_status` | Report connection state, remaining time, and the peer to reply to. |
| `wechat_send_message` | Send a text message to a peer. |
| `wechat_logout` | Forget this agent's login. |

The connection's arrival and departure are drawn as harness lines, not turns:
`[⚡ Wechat online]` and `[✗ Wechat offline]`. The reasons remain available
through `wechat_check_status`.

A message from WeChat becomes a turn in that agent's conversation and is drawn in
the window showing that conversation. It is recorded with channel `wechat` and a
`[Wechat:{…}]` prefix naming the sender. Replying is the model's job: nothing
auto-dispatches an answer. The model calls `wechat_send_message` with the peer
and the `context_token` from the marker. While the turn runs, the other person
sees a typing indicator.

Each agent logs in separately and has its own file and poll loop. A subagent
does not: WeChat is an external identity, and a subagent is internal to its
maker. The plugin is one shared process.

### Agent tasks

An agent task is a separate loop of the same agent, started with a name and a
description and answered later by a message in the same conversation.

| Tool | Purpose |
|---|---|
| `agent_task_start` | Start a task and return its id immediately. |
| `agent_task_status` | Report what a task is doing. Never returns its result. |
| `agent_task_list` | List this agent's tasks, running first. |
| `agent_task_cancel` | Stop a task and report its state after the call. |

A task runs as the agent, with the same system prompt, model, tools, and
context decision. It does not have the turn that started it, so the description
must restate the relevant context. Its answer arrives as a message with an
`[AgentTaskResult:{…}]` marker on the `agent-task:<name>` channel and is drawn as
`AsyncTask(<name>)>`. If no window is showing the conversation, the answer is
reported as `undelivered` and the log says so.

```yaml
agent-task:
  max_running: 4
```

`max_running` is the only key the section owns. Everything else comes from
`agent:`. A task may start another task, so `max_running` bounds concurrency but
not the total amount of work over time.

### Scheduled tasks

A scheduled task is a name, a description, and a cron expression written by the
model. `slife2-schedule` fires it while slife2 is running. When a task comes due,
the agent receives it as a turn in the window showing that conversation. That
turn calls `schedule_run_now` with the due time it carries. A fire that reaches
no window ends the run as `failed` with the reason recorded.

| Tool | Purpose |
|---|---|
| `schedule_set` | Write or replace a task: name, description, cron, timezone. |
| `schedule_remove` | Remove a task and all of its runs. |
| `schedule_list` | List tasks and their next due times. |
| `schedule_runs` | List the ledger: one row per fire. |
| `schedule_run_now` | Start this run's work now; with `due_at`, backfill a run. |
| `schedule_run_skip` | Close a run that will not happen, with a reason. |

A run is one fire, named by the moment it was due. Its state moves from
`pending` to `ran`, `failed`, `missed`, or `skipped`. The row is written before
the work is handed over, so a fire cannot repeat on the next poll. A hand-over
that is refused marks the run `failed`. A run whose work could not be started is
closed `failed` with the reason rather than left open.

Tasks fire only while slife2 is running. On startup, the sweep closes runs left
open by a stopped process, and the poll records the newest missed fire for each
task. `schedule_runs(status="missed")` lists them. A missed run can be
backfilled with `schedule_run_now(name, due_at)` or closed with
`schedule_run_skip`.

The ledger is `<data>/agents/<agent>/schedule.db`. There is no configuration
section. A task's `timezone` is an IANA name; leaving it out uses the machine's
current clock, which does not follow a daylight-saving change.

### Peer agents

An agent's peers are other agents, reached over the published A2A-over-MQTT
profile. A person writes `peer_agent:` in `slife2.yaml`; the model sees
`peer_agent_*` tools. The underlying protocol name does not appear in anything
the model reads.

| Tool | Purpose |
|---|---|
| `peer_agent_send_message` | Send a task, complete a received task, or send a message. `message_type` selects the operation. |
| `peer_agent_cancel_task` | Withdraw a task this agent sent. |
| `peer_agent_list` | List this agent first, then every peer by name. |
| `peer_agent_task_list` | List open tasks: those waiting for an answer and those still owed one. |
| `peer_agent_broadcast` | Send a fire-and-forget event to every peer. |
| `peer_agent_group_set` | Create a group and pull in the members you name; you become its owner. Calling it again as owner replaces the roster. |
| `peer_agent_group_list` | List the groups this conversation knows, with owner and your role. |
| `peer_agent_group_member_list` | List a group's owner and members. |
| `peer_agent_group_remove` | Disband a group you own. |
| `peer_agent_group_leave` | Leave a group on your own; if you are the owner, ownership passes to the first remaining member. |
| `peer_agent_group_send_message` | Send a fire-and-forget message to every other member of a group. `@name` and `@all` address members, as in a chat group. |

A `task_request` returns an id immediately; the result arrives later as a
message with a `[PeerAgent:{"from": …, "type": "task_response", "task_id": …}]`
marker. The id and `status: "unverified"` describe acceptance, not delivery; a
send that nothing answers comes back as an unreachable notice when the requester
retry ladder runs out. There is nothing to poll. `task_response` completes a
task this agent was sent, using the `task_id` from the marker. The direction is
carried by `from`; `agent_name` is the agent's own identity.

There is one mesh per agent: the agent name is its MQTT identity, so two agents
on one machine are two peers, and a second live instance under one name is
refused before it joins. A subagent is not on this network; it is reached only
through its maker's local subagent network.

Presence is drawn as a harness line, not a turn: `[⚡ jill online]`. The roster is
available through `peer_agent_list`. An agent is on the mesh while a window holds
its `join` call; `--headless` holds the same call without a window. In-flight
tasks are held in memory, so a restart starts with none. `slife2 probe --agent
NAME --prompt '…'` is that road from a shell: one `task_request`, and the
model's `task_response` on stdout.

A group is a named roster with an owner, created by `peer_agent_group_set`; no
invitation or acceptance is involved. Group messages are fire-and-forget events
to every other member — never back to the sender — and arrive with a
`[PeerAgentGroup:{"from": …, "group": …, "owner": …}]` marker on the
`peer_agent_group:<group>:<sender>` channel. Viewing the members is a separate
read (`peer_agent_group_member_list`). **A group chat is casual**: no member owes
an answer, and silence is not `pending` or `undelivered`; `sent` confirms the
send, not that anyone read it. Use the direct send tool when you need an
answer. Each member keeps what it has learned in
`agents/<agent>/groups.yaml`, so groups survive a restart. A member that is
offline when a message is sent misses it; `peer_agent_group_list` is the local
truth, not a live check. Only the owner can change the roster or disband the
group; a member's way out is `peer_agent_group_leave`.

The broker is started automatically when the configured one is local and absent,
and stopped with the stack only if slife2 started it. A system broker or one
started by hand is left alone. A broker that cannot serve fails startup with its
name and log. `provider: mosquitto` is the only supported value.

```yaml
peer_agent:
  provider: mosquitto
  broker:
    host: localhost
    port: 1883
  org: default
  unit: default
```

If `broker:` is omitted, the peer network uses the subagent network's broker
address. Omitting the `peer_agent:` section keeps the machine off the network
entirely.

A restart drops every in-flight task. A peer that is still waiting sees the task
in its own `peer_agent_task_list` and re-sends it, which arrives as a new
request.

### Subagents

A subagent is an agent that belongs to another agent. It has the maker's tool
set, its own role-specific system prompt, and its own conversation, but no
process, no window, and no claim on an agent name. It also has no independent
external identity: no WeChat login and no presence on the peer-agent network. It
is reached only on the local subagent network, and it runs when something sends
it a message. Its two membership tools, `subagent_set` and `subagent_remove`,
refuse a subagent caller.

A subagent is created with `subagent_set` and removed with `subagent_remove`.
Once created, it is addressed over the local network with the same operations
as a peer agent, under `subagent_*` names: `subagent_send_message`,
`subagent_cancel_task`, `subagent_list`, `subagent_task_list`,
`subagent_broadcast`, and the six group tools `subagent_group_set`,
`subagent_group_list`, `subagent_group_member_list`, `subagent_group_remove`,
`subagent_group_leave`, and `subagent_group_send_message`. The membership is the
directory set, so a send naming anything that is not a member is refused before
it is published; `subagent_list` reads the roster. Group traffic on this network
wears the `[SubagentGroup:…]` marker and the `subagent_group:<group>:<sender>`
channel, whether the sender is the maker or a sibling, and each member keeps its
own `groups.yaml`.

| Key | Meaning |
|---|---|
| `name` | The subagent's name and directory. |
| `model` | `provider/model`; empty uses the maker's model. |
| `role` | A word included in the subagent's prompt. |
| `fork` | Copy the maker's current conversation as the subagent's starting point. A fork's turns are not stored, and it lives only for the current session. |

A non-fork subagent lives in `agents/<maker>/subagents/<name>/` with its own
`turns.db`, `subagent.yaml`, cabinet, and media directory. A fork has only the
`subagent.yaml` record: it stores no turns, keeps its conversation in memory as
a copy of the maker's, reads the maker's turn log for context, and shares the
maker's cabinet and media. A fork is session-lived; when the agent server next
starts it removes every fork record left by the previous session, so the name is
free again. **A fork cannot be pulled into a group**: it is temporary, so a
roster naming it would keep addressing a name nobody answers;
`subagent_group_set` refuses one, and group traffic for a fork is dropped.
Scheduling stays with the agent. The tree stops at one level: a subagent cannot
create or remove a subagent, because the local network holds a place for the
maker's own subagents and nothing holds a place for a level below them.

Removing a subagent first tells the local network to release its place, then
stops its loop, then deletes its directory. If the network notification cannot be
made, the subagent is left unchanged. A window watching the subagent is told the
conversation is gone, and its view ends with that sentence.

The window reaches subagents through two read-only management tools on the agent
server: `subagents` lists the maker's directories, and `watch` streams a
conversation's turns (history first, then the live turn) until the call is
cancelled. Neither starts, steers, or answers a turn; the local network and the
maker's own tools do all of that.

The local network is configured under `subagent:`. It needs a `broker`; a
`peer_agent:` section with no broker uses the same address. The `unit` is always
the agent's name, and writing one is refused. `org` may be set to group meshes.

```yaml
subagent:
  max_running: 32
  provider: mosquitto
  broker: {host: localhost, port: 1883}
  # org: subagent
```

## Data locations

A data directory (a **home**) contains:

```text
<data>/
  slife2.yaml                  configuration; required
  AGENTS.md                    the model's guide to its tools; required
  skills/                      <name>/SKILL.md playbooks
  jobs/                        <name>.py code-defined tools
  agents/
    tools.db                   the shared tool catalogue for this home
    <agent>/
      turns.db                 the conversation and its live context
      schedule.db              scheduled tasks and their run ledger
      cabinet/                 notes/, diary/, reports/, saved files, .index.db
      media/                   generated images, video, and audio
      wechat.yaml              this agent's WeChat login
      subagents/
        <name>/                a non-fork subagent's turns.db, subagent.yaml,
                               cabinet/ and media/; a fork has only the record
```

The tool catalogue sits beside the agent directories because it belongs to the
whole home, not to one agent.

### The runtime directory

The machine's shared-server bookkeeping is kept in `~/.slife2/runtime`:

```text
<runtime>/
  servers/        one record per running server
  logs/           each server's output
  locks/          per-endpoint start locks
  agents/         agent-name claims
  clients/        which clients are using the shared servers
  homes/          data directories this machine has used
```

`SLIFE2_RUNTIME_DIR` overrides the location. The runtime directory is
reconstructible: deleting it causes the next start to rebuild what it needs.

Each server logs at INFO by default. `SLIFE2_LOG_LEVEL=DEBUG` raises the level
for every server started with it; per-call durations, tool calls and connection
detail live there, and a value that is not a level falls back to INFO with a
line saying so. A log that outgrows its ceiling is rotated to `<log>.1` at the
next start rather than deleted. An external `mcp:` entry's stdio child also
writes its own file under `logs/` (`mcp-<name>-<digest>.log`), so a server's own
start-up chatter and crashes are attributable instead of interleaved into the
toolhub's log.

### Multiple data directories

A data directory may hold several agents. They share its `slife2.yaml`, its
`skills/`, and its `agents/tools.db`, so they run on the same tool set. A tool
loaded by one is loaded for the other.

Agent identity is independent of the directory. `--agent jack --data-dir D`
names the agent and says where its files go. The name is claimed once per
machine, so a second live `jack` in another data directory is refused. Which
directory a name used previously is not remembered.

Servers are shared across data directories. A server started for one home serves
every home on the machine, and each call is resolved against the home it came
from. An external `mcp:` entry is one server per name regardless of how many
homes configure it; if two homes give the same name different commands, the log
reports the disagreement and the running server is used.

## Development

From a checkout:

```bash
uv run pytest                  # fast lane: unit and integration, parallel
uv run pytest -m unit          # unit tests only
uv run pytest -m integration   # tests that bind a socket or spawn a process
uv run pytest -m e2e           # full stack as processes
uv run ruff check . && ruff format --check .
uv run pyright
```

On Windows, `uv run pytest` cannot run beside a live stack: `uv` rewrites the
entry points in `.venv\Scripts\` on every run, and a running instance holds
`slife2.exe` open (`os error 32`). Use the environment's interpreter instead:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The test lanes, CI matrix, project structure, and the plugin registry are in
[SDD.md](SDD.md) §13 and Appendix B.

## Known limitations

- `cli:` entries are catalogued and discoverable, but no tool executes them yet.
- Playbook scripts are not executed by slife2; `skill_use` returns the playbook
  text, and the model runs any commands itself.
- Jobs are code: they run in `slife2-jobs` with the caller's identity and are
  not sandboxed. The top-level tool call is the approval point.
- The cabinet's `file_save` does not fetch URLs, and the cabinet has no `grep`
  search leg.
- There is no approval gate before a configuration edit, including `mcp_set`
  with a `command:` that records a program slife2 will start.
- An agent task may start another agent task; `agent-task.max_running` bounds
  concurrency, not total work over time.

## License

MIT. See [LICENSE](LICENSE).
