# slife2

[中文](README.zh-CN.md)

**slife2 is a terminal-based AI agent.** It runs a conversation with a language
model, exposes tools to that model over the Model Context Protocol (MCP), and
reaches the outside world through local plugin processes. The terminal window is
only a client: the agent loop, the tool hub, the model backends, memory, and
every capability are MCP servers that slife2 starts and shares.

This file is the user manual. The system design is described in
[sdd.md](sdd.md); the decisions and measurements behind it are recorded in
[DESIGN.md](DESIGN.md).

**Contents**

- [Overview](#overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Command-line interface](#command-line-interface)
- [Using the terminal UI](#using-the-terminal-ui)
- [Configuration](#configuration)
- [Capabilities](#capabilities)
- [Data locations](#data-locations)
- [Architecture](#architecture)
- [Development](#development)
- [Known limitations](#known-limitations)
- [License](#license)

## Overview

slife2 is built around four rules.

1. **A plugin is one of slife2's own MCP servers.** It speaks MCP over
   Streamable HTTP and implements a small additional contract: state is keyed by
   a client id rather than by a session, every model-facing tool declares its
   audience, a client can verify the server's identity, and the plugin has one
   row in the central registry. A plugin is required: if one cannot be reached,
   the turn fails. An external MCP server under `mcp:` is optional; if it cannot
   be reached, the model simply has fewer tools.

2. **Credentials stay in the process that uses them.** A provider key lives in
   the model server that calls the provider. A tool server's key lives in the
   plugin that owns the connection (`slife2-mcp-tools` for `mcp:`,
   `slife2-restapi-tools` for `rest-api:`). The agent loop imports no provider
   SDK and holds no key.

3. **Servers are shared machine-wide.** The launcher starts the servers a
   configuration needs and attaches to any that are already running. Servers are
   daemons, so closing one window does not interrupt another. The last client to
   exit stops the running servers, unless `--keep-servers` is used.

4. **`slife2.yaml` is the single description of what exists.** Providers,
   servers, tool sources, embeddings, media backends, tunnels, and network
   settings are read from that file. There is no second configuration store.

### Terminology

| Term | Meaning |
|---|---|
| **Plugin** | One of slife2's own MCP servers. It is started and shared by the launcher and implements the plugin contract. An external MCP server is not a plugin. |
| **Agent** | An identity named by `--agent NAME`. One name has one live holder on a machine. |
| **Client id** | `(agent, subagent)`: the identity a call is made on behalf of. Every server keys its state by it. |
| **Conversation** | One client id's history. |
| **Turn** | One exchange: the input, the model's answer, and the tool calls it made, stored as one row. |
| **Channel** | Where a message arrived: `tui`, `wechat`, `peer_agent:<peer>`, `subagent:<name>`, `agent-task:<name>`, `async-tool:<name>`, or `schedule:<name>`. |
| **Marker** | The machine-readable prefix a channel adds to a delivered message, e.g. `[Wechat:{…}]`. Markers are stored with the message and removed only for display. |
| **Family** | A plugin that owns a configuration section and declares the rows that section describes. |
| **Source** | One entry a plugin holds, such as an `mcp:` server, a playbook, or a command. |
| **Catalogue** | The machine-readable list of every tool, playbook, and command the model may discover. |
| **Dump** | A tool output stored whole in the cabinet because it was too large for the conversation. |
| **Home** | A data directory: configuration, skills, agents, and their files. |
| **Runtime directory** | The machine-wide records, locks, and logs of the shared servers. |

## Requirements

- **Python 3.13 or newer**
- **[uv](https://docs.astral.sh/uv/)** for dependency management and execution

slife2 is tested on Windows, macOS, and Linux.

The installation script below installs every external program required by the
current `slife2.yaml`, upgrades existing ones, and verifies the result. A manual
installation uses Python 3.13 and uv alone.

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

Every program named by the current configuration is required. The installer
provides each one at the user level and stops with the configuration entry named
when a program is unavailable.

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
present before the installation stay in place.

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

`slife2.yaml` is optional: before the file exists, slife2 uses built-in defaults,
including a single OpenAI-compatible provider.

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
```

Options:

| Option | Meaning |
|---|---|
| `--data-dir DIR` | Use `DIR` as the data directory. Overrides `SLIFE2_DATA_DIR`. |
| `--agent NAME` | Run as `NAME`. Default: `slife2`. One live instance per name per machine. |
| `--model PROVIDER/MODEL` | Start on this model. A bare provider selects its first model. Default: the configuration's `default`. |
| `--url URL` | Connect the window to an agent server at `URL` instead of the configured one. |
| `--a2a-broker HOST:PORT` | Use this broker for the peer-agent network for this run. It overrides `peer_agent.broker` only, never the subagent network's broker. It cannot enable a network that has no `peer_agent:` section, and it is refused when the A2A server is already running on another broker; stop the stack first. |
| `--keep-servers` | Leave the shared servers running after this instance exits. |
| `--headless` | Run the same window, servers, connections, and turns without a terminal: nothing is drawn and nothing is printed. The agent remains reachable on the peer network. Exit with Ctrl+C. |
| `--lang {en,zh}` | Set the window's language. Without the flag, the language is detected from the operating system. |

`--headless` does not create a different kind of agent. It runs the same window
with no display, and the system prompt is told that nobody is watching so the
model can adjust its behavior.

`--lang` affects the window only: labels, harness lines, panels, the model
picker, the approval row, and the status bar. System prompts, tool schemas, and
log lines remain English. External text, such as tool results and peer messages,
is passed through in its original language.

### Plugin executables

Each plugin can also be started directly. This is useful for a process manager
or for debugging a single server.

| Executable | Server | Default port |
|---|---|---|
| `slife2-agent` | Agent loop and conversations | 8000 |
| `slife2-llm-openai` | OpenAI-compatible chat completions | 8001 |
| `slife2-llm-anthropic` | Anthropic Messages | 8002 |
| `slife2-llm-openai-responses` | OpenAI Responses | 8003 |
| `slife2-llm-embeddings` | Embeddings | 8004 |
| `slife2-tui` | Terminal inbox, one session per agent | 8005 |
| `slife2-context` | Turn log and context decision | 8010 |
| `slife2-toolhub` | Tool catalogue and model tool list | 8020 |
| `slife2-wechat` | WeChat login and messaging | 8030 |
| `slife2-skills` | Playbooks and `skill_use` | 8031 |
| `slife2-cli` | The `cli:` section | 8032 |
| `slife2-mcp-tools` | The `mcp:` section | 8033 |
| `slife2-restapi-tools` | The `rest-api:` section | 8034 |
| `slife2-cabinet` | Notes, diary, files, reports, dumps | 8035 |
| `slife2-media` | Image, video, speech, transcription | 8036 |
| `slife2-agent-task` | Background agent tasks | 8037 |
| `slife2-file-tunnel` | Public HTTPS URL for a local file | 8038 |
| `slife2-schedule` | Scheduled tasks and their ledger | 8039 |
| `slife2-a2a` | Peer-agent network over MQTT | 8040 |

The subagent network is served by the same module on port 8041 and is started
as `python -m slife2.plugins.a2a.local_server`; it has no separate console
script.

## Using the terminal UI

### Keys

| Key | Action |
|---|---|
| `Enter` | Send the message. |
| `Shift+Enter` | Insert a line break. |
| `Esc` | Cancel the running turn. |
| `Ctrl+C` | Copy when something is selected; otherwise cancel the running turn; otherwise quit. |
| `Ctrl+S` | Open the model picker. `↑`/`↓` move, `Enter` selects, `Esc` closes. |
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
`Esc` cancels the running turn but leaves the queue intact.

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

### Restoring a conversation

Closing and reopening the window, restarting the servers, or waiting long enough
for the idle sweep does not start a new conversation. The transcript is restored
with its original timestamps, tool calls, and results. Reasoning is folded away
where it was folded in the live conversation.

The status bar also reports the context size left by the last model call and
its fraction of the model's window. Before each turn, a note names what the
context decision kept and what it recalled from the turn log.

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
The image goes to the model and no further. The turn log keeps the sentence, the
tool's answer naming the file, and a note recording that the image was attached
and how large it was. A restored conversation is therefore text; a later turn
that needs the picture attaches it again from the named path.

## Configuration

### Location and defaults

The configuration file is `slife2.yaml` inside the data directory. A data
directory is the checkout when running from a checkout, otherwise
`~/.slife2`. Use `--data-dir DIR` or `SLIFE2_DATA_DIR` to override it.

A missing configuration file is not an error: built-in defaults are used. A file
that exists but cannot be loaded stops the start with a one-line error naming
the file and the mistake.

The installer may write `slife2-default.yaml` beside an existing configuration
as a reference. slife2 reads only `slife2.yaml`; the reference is for a person
to compare and merge by hand.

The file is safe to check in. It should contain no secrets; every credential is
a `${VAR}` or `keyring:` reference resolved at runtime.

### Sections

| Section | Purpose |
|---|---|
| `servers:` | Address and port of each plugin server. Defaults are used when absent. |
| `providers:` | Chat providers and their models. |
| `default:` | The model a new conversation starts on, as `provider/model`. |
| `agent:` | The iteration cap and the system-prompt template. |
| `agent-task:` | `max_running`, the number of background tasks per agent. |
| `subagent:` | `max_running`, and the broker and `org` for the local subagent network. |
| `embeddings:` | Embedding providers used by the memory index. |
| `context:` | Limits for the context decision. |
| `tool_load:` | Maximum number of loaded model tools. |
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

The configuration is edited by hand or by the model. Each family serves tools
for its own section:

| Family | Tools |
|---|---|
| `mcp:` | `mcp_set`, `mcp_list`, `mcp_list_tools`, `mcp_remove`, `mcp_set_enabled` |
| `rest-api:` | `rest_api_set`, `rest_api_list`, `rest_api_list_tools`, `rest_api_remove`, `rest_api_set_enabled` |
| `cli:` | `cli_set`, `cli_list`, `cli_remove`, `cli_set_enabled` |
| `skills:` | `skill_set`, `skill_list`, `skill_remove`, `skill_set_enabled` |
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

A conversation does not accumulate an unbounded context. `slife2-context` stores
the turn log and, before each turn, makes one model call that decides:

- which turns already in the conversation to keep;
- which turns to recall from the log.

The turn runs on the two selections together, in time order. The call reads the
whole conversation in hand rather than only the new message, because a follow-up
usually names its subject through the turns above it. The answer is a
selection, never a dump; if a recall matches more than fits, only what fits is
returned.

Each turn read by the decision carries a `[TURN:{…}]` footnote naming its id,
channel, and span. The newest turns carry one too, so they are as addressable as
restored ones. Capitalized markers are annotations added by the harness and are
not part of the stored record. A marker that reads as a word, such as
`[Wechat:{…}]`, was written by a sender and is stored with the message.

The model can browse its history with `turn_list` and `turn_read`.
`turn_list` is newest first, one line per turn, with `since`/`until` accepting an
ISO date or a phrase such as `yesterday`, `last month`, or `3 days ago`, and
`limit`/`offset` paging against `total`. Neither tool takes an agent argument:
the call itself carries the conversation, so a model reads its own history and
nothing else.

Every turn is indexed twice when stored: a keyword index over the text by which
a turn is found, and a vector index over what it is about. Both indexes are
derived from the turns and can be rebuilt from them. Changing the embedding
model under `embeddings:` re-embeds the whole history on the next start.

Two harness decisions are not tools:

- `restore` replays the stored context when a conversation starts.
- `rebuild` runs the context decision before a turn.

A rebuild that decides to keep exactly what is in hand rebuilds nothing. A
discriminator that times out, a provider that errors, or an unreadable answer
keeps the existing context and runs the turn. A missing turn store fails the
turn, like any missing plugin.

The model can browse the turn log but cannot semantically search it; the recall
used by the context decision belongs to the store.

### Tools

`slife2-toolhub` is the only process that decides which tools the model may see.
It does not reach external servers itself. `slife2-mcp-tools` holds the `mcp:`
connections and `slife2-restapi-tools` holds the `rest-api:` connections; each
declares its sources and the tools they offer, and the hub merges the
declarations, names the tools, gates them, and routes calls back.

The hub serves three set-level tools:

- `tool_search` searches the whole catalogue by keyword and by meaning.
- `func_tool_load` puts one or more tools into the list shown to the model.
- `_func_tool_unload` removes tools from that list. The harness also calls it
  at a turn boundary to enforce the budget.

A model can call a name returned by `tool_search` without loading it, but loading
is what keeps it in the list sent with every request. The model's list is
re-read from the hub before every model call, so a tool loaded during a turn is
available from the next iteration.

Every tool served by slife2's own plugins starts loaded. An external source
starts loaded only when its entry sets `autoload: true`. Everything else is
discovered and loaded on demand.

The loaded list is bounded by `tool_load.threshold` (default 100). Over the
limit, the least recently called tools are unloaded, never slife2's own tools
and never an `autoload` source. A tool that has never been called is ordered by
when it was loaded. The trim happens at a turn boundary; the names removed are
written into the conversation as a tool call and its result, so the model learns
that the list shrank. Eviction costs a search and a load, not a capability.

Tool names:

- A tool from an `mcp:` entry is `{server}__{tool}`, for example
  `arxiv__arxiv_search_papers`.
- A plugin's own tools use bare names, for example `turn_read`. The plugin
  serving a tool is a fact about the system, not something the model needs in
  the name.

Every tool result is a JSON object. A listing answers:

```json
{"<noun>": [], "total": 0, "limit": 20, "offset": 0}
```

`total` is what says whether another page exists. A read answers the row's own
fields. A refusal or a miss raises a `ToolError`, which the harness reports to
the model as a failed call with the reason. `_check_new_input` is the one
exception: it returns a person's message verbatim.

No answer is intended to exceed `tool_result.tokens`. Listings that can grow use
`limit`/`offset`; an output that cannot be bounded is stored as a dump (see
[Cabinet](#cabinet)).

#### Tool call options

Every tool accepts three meta-parameters. They are added to every advertised
schema and stripped before dispatch, so no tool server sees them.

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
depends on it.

`cli:` entries describe programs already installed on the machine. Every entry is
a catalogue row (`cli:yt-dlp`), so `tool_search` can find it by what it does.
The row is not a callable tool and has no load state. No tool executes a `cli:`
entry yet; `func_tool_load` reports that.

`enabled: false` keeps an entry configured but never connects it. The value must
be a real YAML boolean: `no`, `off`, and `'false'` are strings under YAML 1.2
and are refused rather than guessed. A disabled entry's tools remain in the
catalogue, marked `disabled`, so searches still find them.

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
and dumps.

| Kind | Key | A second write |
|---|---|---|
| `note` | A subject chosen by the user | Replaces the note. |
| `diary` | A date | Appends a section. |
| `file` | Its path inside the cabinet | Copies the file in. |
| `report` | A title | Appends a section. |
| `dump` | The tool call that produced the output | Written by the harness. |

Notes and files are documents: the second write is their content, which is why
only those two kinds have remove tools. A diary and a report are logs: two
entries under one date or title are two facts, and nothing in the later text says
which part of the earlier one it replaces.

A dump is a tool output too large for the conversation. The output is stored
whole, keyed by the tool call id, and the conversation carries a selection of the
output's own lines within `tool_result.tokens`. Nothing is paraphrased. The
selection is made by a discriminator call that is given the output, the
conversation in hand, and the current input, and answers with line ranges. The
model reads any part of the dump back with `dump_read`, a searching reader:
`pattern` finds lines and `context` adds their neighbours, `offset` reads from a
line (negative counts from the end), and `limit` reads fewer lines. Every answer
stays within the same token budget. A search does not see dumps unless `kind` is
`dump`.

Every document is written twice: a row in the cabinet index and a Markdown file
under `agents/<agent>/cabinet/` (`notes/`, `diary/`, `reports/`), which can be
opened, edited, or deleted without slife2. A subject that is not already
lowercase carries a short digest in its filename (`notes/Python~6e3604888c.md`)
because Windows and macOS treat `Python.md` and `python.md` as one file. A saved
file's bytes live under the cabinet as well and are not indexed; `file_read`
returns a file's text and refuses a PDF, image, or archive by saying what it is.
Text in another encoding is decoded and the encoding is named in the answer.

`cabinet_search` accepts the same two inputs as `tool_search`: `keywords` are
words that must all be present, and `sentences` are questions matched by
meaning. Several sentences are several questions, and each can only add results.
`cabinet_summarize` writes a description for a document; writing one for a file
re-embeds it, because the description is part of what its vector is built from.

`file_save` copies files already on the machine. It does not fetch URLs. The
cabinet has no `grep` search leg.

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
context decision. It does not have the turn that started it: `restore` reads the
stored turn list, and a turn is written only when it ends, so the description
must restate the relevant context. The task's turns are not written to the
agent's conversation. Its answer is delivered as a message with an
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

A `task_request` returns an id immediately; the result arrives later as a
message with a `[PeerAgent:{"from": …, "type": "task_response", "task_id": …}]`
marker. There is nothing to poll. `task_response` completes a task this agent
was sent, using the `task_id` from the marker. The direction is carried by
`from`; `agent_name` is the agent's own identity.

There is one mesh per agent, because the agent name is the MQTT client id and the
last segment of the request topic. Two agents on one machine are two peers. An
agent reached from two places is refused by the name-collision check before it
joins. A subagent is not on this network: it has no external identity and is
reached only through its maker's local subagent network.

Presence is drawn as a harness line, not a turn: `[⚡ jill online]`. The roster is
available through `peer_agent_list`. An agent is on the mesh while a window holds
its `join` call; `--headless` holds the same call without a window. No inbound
record is persisted: in-flight tasks are held in memory, and a restart starts
with none.

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
Once created, it is addressed over the local network with the same five
operations as a peer agent, under `subagent_*` names: `subagent_send_message`,
`subagent_cancel_task`, `subagent_list`, `subagent_task_list`, and
`subagent_broadcast`.

| Key | Meaning |
|---|---|
| `name` | The subagent's name and directory. |
| `model` | `provider/model`; empty uses the maker's model. |
| `role` | A word included in the subagent's prompt. |
| `fork` | Copy the maker's current conversation as the subagent's starting point. A fork's turns are not stored, and it lives only for the current session. |

A non-fork subagent lives in `agents/<maker>/subagents/<name>/` with its own
`turns.db`, `subagent.yaml`, cabinet, and media directory. A fork has only the
`subagent.yaml` record there: it stores no turns, keeps its conversation in
memory as a copy of the maker's, and reads the maker's turn log for context. It
writes nothing there, and it also shares the maker's cabinet and media. A fork
is session-lived — a temporary stand-in for the maker — and when the agent
server next starts it removes every fork record left by the previous session,
so the name is free again. WeChat and the peer-agent network belong to the
agent, not the subagent; scheduling also stays with the agent. The tree stops at
one level: a subagent cannot create or remove a subagent, because the local
network holds a place for the maker's own subagents and nothing holds a place
for a level below them.

Removing a subagent first tells the local network to release its place, then
stops its loop, then deletes its directory. If the network notification cannot be
made, the subagent is left unchanged.

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
  slife2.yaml                  configuration; absent means built-in defaults
  skills/                      <name>/SKILL.md playbooks
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

## Architecture

### Layers

The package is divided by what a module is, not by its size:

| Directory | What it contains |
|---|---|
| `slife2/plugins/` | One process per plugin. Each plugin has a module with a `main` and a `SERVER_NAME`. |
| `slife2/clients/` | The client half of an MCP hop. |
| `slife2/libs/` | Libraries imported by more than one component. |
| `slife2/helpers/` | Small stateless helpers. |
| `slife2/tui/` | The terminal window. It is a client and serves nothing. |

A plugin directory may keep its own private library, such as
`plugins/cabinet/store.py` or `plugins/schedule/expressions.py`; `libs/` is for
code that more than one component imports.

The dependency direction is one-way:

```text
tui/ -> MCP -> plugins/agent/ -> libs/turn/loop.py
     -> {libs/model/base.py, libs/turn/tools.py, libs/turn/events.py,
         libs/turn/messages.py} -> libs/host/config.py
```

The loop imports neither the agent server nor the terminal. It is handed a
backend and a coroutine that returns a tool registry; where that registry comes
from is the agent server's business.

### Plugin registry

`libs/host/config.py` contains one row per plugin in `PLUGINS`. A row records:

- the key used in `servers:`;
- the module that serves it;
- the MCP name it answers to;
- the probe tool used to identify it;
- the default port;
- whether it is a model API backend.

Start order, default addresses, server-name constants, and launcher specs are
derived from this table. Adding a plugin means writing the module and adding a
row; removing one means deleting both.

`libs/plugin/protocol.py` provides the shared server scaffold
(`serve_plugin`). A server supplies three hooks: what to build, what to say about
its start, and what it requires. All servers use this path, including the model
backends, which reach it through a wrapper.

## Development

### Tests

```bash
uv run pytest                  # fast lane: unit and integration, parallel
uv run pytest -m unit          # unit tests only
uv run pytest -m integration   # tests that bind a socket or spawn a process
uv run pytest -m e2e           # full stack as processes (about 70 seconds)
uv run ruff check . && ruff format --check .
uv run pyright
```

The default run executes across all cores (`-n auto` from `addopts`) and excludes
the `e2e` marker.

The fast lane needs no API key and no running server. Models are faked, and
servers are driven over FastMCP's in-memory transport: real MCP messages and
progress notifications, with no port bound. Integration tests bind ephemeral
ports, so the suite can run beside a live stack.

The end-to-end lane starts the real stack as processes: the launcher, seventeen
daemons, a configuration on disk, and a stub OpenAI-compatible provider. It
exercises the launch order, cross-process turns, tool routing, progress
notifications, large-output handling, and conversation restore.

Shared test doubles live in `tests/fakes.py`: a stub model, a stub embedder, fake
window and mesh clients, and factories used by more than one test module.

### CI

CI runs on Ubuntu, macOS, and Windows with Python 3.13. It performs:

1. a read-only `install --dry-run` on every operating system;
2. a wheel build;
3. an assertion that the interpreter can load SQLite extensions;
4. `ruff check` and `ruff format --check`;
5. `pyright`;
6. the default test lane;
7. the end-to-end lane.

## Known limitations

- `cli:` entries are catalogued and discoverable, but no tool executes them yet.
- Playbook scripts are not executed by slife2; `skill_use` returns the playbook
  text, and the model runs any commands itself.
- The cabinet's `file_save` does not fetch URLs, and the cabinet has no `grep`
  search leg.
- The model can browse the turn log with `turn_list` and `turn_read` but cannot
  semantically search it.
- There is no approval gate before a configuration edit, including `mcp_set`
  with a `command:` that records a program slife2 will start.
- An agent task may start another agent task; `agent-task.max_running` bounds
  concurrency, not total work over time.

## License

MIT. See [LICENSE](LICENSE).
