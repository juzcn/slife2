# slife2

[中文](README.zh-CN.md)

**slife2 is a terminal-based AI agent.** It runs a conversation with a language
model, exposes tools to that model over the Model Context Protocol (MCP), and
reaches the outside world through local plugin processes. The terminal window is
only a client: the agent loop, the tool hub, the model backends, memory, and
every capability are MCP servers that slife2 starts and shares.

This file is the user manual. The design is in [SDD.md](SDD.md). Every statement
here is something a reader can act on; an explanation that does not change what
a reader does belongs in the design document.

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
| **Plugin** | One of slife2's own MCP servers, started and shared by the launcher. An external MCP server is not a plugin. |
| **Agent** | An identity named by `--agent NAME`. One name has one live holder per machine. |
| **Conversation id** | `(agent, subagent)`: the identity a call is for. |
| **Conversation** | One agent's own history, or one of its subagents'. |
| **Turn** | One exchange: the input, the model's answer, and the tool calls it made. |
| **Plan** | The model's list of steps for one conversation, stored beside it and replayed every turn. |
| **Channel** | Where a message arrived: `tui`, `wechat`, `peer_agent:<peer>`, `agent:<name>`, `subagent:<name>`, `agent-task:<name>`, or `async-tool:<name>`. |
| **Marker** | The machine-readable prefix a sender adds to a message, e.g. `[Wechat:{…}]`. Stored with the message, removed only for display. |
| **Family** | A plugin that owns one configuration section or folder and declares its rows to the hub. |
| **Catalogue** | Every tool, playbook, job, and command the model may discover. |
| **Job** | One Python function in `jobs/`, exposed as the tool `job__<name>`. |
| **Dump** | A tool output stored whole in the cabinet because it was too large for the conversation. |
| **Home** | A data directory: configuration, `AGENTS.md`, skills, jobs, agents. |
| **Runtime directory** | The machine-wide records, locks, and logs of the shared servers. |

## Requirements

- **Python 3.13 or newer**
- **[uv](https://docs.astral.sh/uv/)** for dependency management and execution

slife2 is tested on Windows, macOS, and Linux.

### SQLite extension support

The vector index is a SQLite extension (`sqlite-vec`), and the interpreter must
be able to load extensions. Windows and Linux builds normally can. On macOS,
neither uv's managed CPython nor Apple's `/usr/bin/python3` can; use Homebrew's:

```bash
brew install python@3.13
uv venv --python "$(brew --prefix python@3.13)/bin/python3.13"
uv sync
```

Without extension loading the database cannot be opened, no plugin starts, and
the first command fails with `this Python's sqlite3 cannot load extensions`.

### External programs (required when configured)

Every program named by a configured server, tunnel, or skill is required. The
installer provides each one at the user level and stops with the configuration
entry named when a program is unavailable.

A `cli:` entry is the exception: its `install:` line is a prompt, not an
instruction. When such a program is missing and the installer cannot provide it,
the run prints that line at the end and still succeeds. A `cli:` entry with no
`install:` line is still required.

| Program | Named by | Provided by the installer |
|---|---|---|
| `mosquitto` | `peer_agent:` / `subagent:` broker | A broker already on `127.0.0.1:1883` is used; otherwise Debian/Ubuntu packages unpack under `~/.local`, macOS uses Homebrew, and Windows uses a running broker or prints the manual command. |
| `cloudflared` | `file-tunnel.providers.cloudflare` | The official binary in `~/.local/bin` (or `%LOCALAPPDATA%\slife2\tools`); Homebrew on macOS |
| `ssh` | `file-tunnel.providers.localhost.run` | The system OpenSSH client |
| `npx` | `mcp:` entries with `command: npx` | The official Node.js LTS archive under `~/.local/lib/nodejs` (or `%LOCALAPPDATA%\slife2\tools\node`); its packages download on first use |
| `uvx` | `mcp:` entries with `command: uvx`, and `rest-api:` entries | uv |
| `yt-dlp` | `cli:yt-dlp` | `uv tool install yt-dlp` |
| `browser-harness` | `cli:browser-harness` | `uv tool install --python 3.12 browser-harness` |

A skill may also declare programs in its header (`requires.bins`); those are
required as well. The included `baidu-search` skill, for example, declares
`python3`. On Windows, `python3` comes from uv's `--default` launchers in
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
environment, and installs the external programs the **shipped** configuration
requires. Data and configuration live in `~/.slife2`; binaries and libraries in
`~/.local` (`%LOCALAPPDATA%\slife2` on Windows). Running it again upgrades.

An already-open shell keeps the `PATH` it started with; the installer's last
lines print the `source` line to run, or open a new terminal.

Python 3.13 is installed only when the machine has none; `uv python upgrade 3.13`
moves it to a newer patch. The installer seeds the bundled `skills/` and `jobs/`
into the data directory, **overwriting files of the same name**; a skill or job
you added under another name stays untouched. `AGENTS.md` is overwritten too —
it is the system's guide to the model's tools, not a file to edit. `slife2.yaml`
is **never overwritten**: a changed shipped copy is written beside it as
`slife2-default.yaml` for you to compare and merge. What the installer installs
is read from the **shipped** `slife2.yaml`, never your home's file: an upgrade
does not wait for that merge.

From a source checkout, run the installer there:

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
present before installation stay. A component that could not be removed stays
recorded, and a later `uninstall` retries it.

### Manual installation

```bash
git clone https://github.com/juzcn/slife2.git
cd slife2
uv sync
```

Store the API key of the provider you intend to use, in the OS keyring through
`credstore` or as an environment variable:

```bash
uv run credstore set DEEPSEEK_API_KEY
# or
export DEEPSEEK_API_KEY=...
```

`slife2.yaml` is required — there is no built-in configuration. The installer
seeds the shipped file into a new home; a checkout uses the `slife2.yaml` beside
its code. `AGENTS.md` is required too; the installer writes it, and a reinstall
replaces it.

### Docker

A container distribution is built from a checkout. It runs the project's own
installer and uses the shipped `slife2.yaml` unchanged.

```bash
docker build -f docker/Dockerfile -t slife2:local .
```

The image runs as the non-root user `slife2`; its checkout and data directory
are `/opt/slife2`, and its runtime directory is
`/home/slife2/.slife2/runtime`. It is about 3 GB because it contains the
programs the full configuration names.

The image has no OS keyring, so its keys are environment variables. The build
runs the same `scripts/install.sh` as a host install, so its log lists every
credential the shipped configuration names; write those into an env file and
pass it with `--env-file` (not `-e KEY=...`, which puts the value in the process
table). Run one prompt and exit:

```bash
docker run --rm -it --env-file slife2.env \
  slife2:local 'Use the shell tool to run pwd and report the output.'
```

Or start the agent headless and keep it running:

```bash
docker run --rm -it --env-file slife2.env slife2:local
```

Keys are environment variables, not command-line arguments. A mounted
`slife2.yaml` whose values are literals is the other supported source. Mount
`/opt/slife2/agents` and `/home/slife2/.slife2` if conversations and logs
should outlive the container. See [docker/README.md](docker/README.md) for the
credential setup, the one-shot prompt, and the container-specific notes.

## Quick start

After the one-click installer, the dependencies are synced and `credstore` is
installed. Set the key and start the window:

```bash
credstore set DEEPSEEK_API_KEY
slife2
```

From a source checkout, start it with `uv run slife2`. A manual installation
starts one step earlier:

```bash
uv sync
uv run credstore set DEEPSEEK_API_KEY
uv run slife2
```

`slife2` starts the MCP servers the configuration needs, reuses any already
running, opens the terminal UI, and stops the servers when the last instance
exits.

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

`down` stops the shared servers and the broker; it does not close open windows.
A window left open reconnects on its next prompt once the servers are started
again, and its conversation is restored. Its agent name stays claimed until the
window exits, so a new instance with the same `--agent` name is refused until
then. `down` also refuses while another live client is using the servers;
`--force` stops them anyway and interrupts that client's in-flight turns.

Options:

| Option | Meaning |
|---|---|
| `--data-dir DIR` | Use `DIR` as the data directory. Overrides `SLIFE2_DATA_DIR`. |
| `--agent NAME` | Run as `NAME`, or ask `NAME` with `probe`. Default: `slife2`. One live instance per name per machine. |
| `--model PROVIDER/MODEL` | Start on this model. A bare provider selects its first model. Default: the configuration's `default`. `run` only. |
| `--url URL` | Connect the window to an agent server at `URL`. |
| `--a2a-broker HOST:PORT` | Use this broker for the peer-agent network for this run. It overrides `peer_agent.broker` only, never the subagent network's broker. It cannot enable a network with no `peer_agent:` section, and it is refused when the A2A server is already running on another broker. |
| `--keep-servers` | Leave the shared servers running after this instance exits. |
| `--headless` | Run the same window, servers, connections, and turns without a display. The agent remains reachable on the peer network. Exit with Ctrl+C. |
| `--lang {en,zh}` | Set the window's language. Without the flag, it is detected from the operating system. |
| `--json` | Print `status` as JSON (`ok`, `servers`, `brokers`; each row has its log path). |
| `--force` | Stop the shared servers even when another client is using them. `down` only. |
| `--prompt TEXT` | The task to ask a running agent. `probe` only. |
| `--timeout SECONDS` | How long `probe` waits for the completed reply. Default: 3600. |
| `--channel {a2a,tui}` | Which road a `probe` asks on: the A2A peer network (default), or an agent task's delivered answer. |
| `--lines N`, `-n N` | How many log lines `logs` shows; `0` reads the whole log. Default: 50. |
| `--all` | With `logs`, print every configured server's log under its own header. |
| `--version` | Print the version and exit. |

`status` and `down` exit 0 only when the stack is fully usable: `status` returns
2 if any configured server or the broker is not running, and `down` returns 2 if
a server or the broker did not stop, or the command was refused because another
client is live. `run` returns 2 when a server cannot start.

`--headless` is the same window with no display, not a different kind of agent.
The system prompt is told that nobody is watching.

`slife2 probe --agent NAME --prompt '…'` asks a running agent — headless or an
open window — one task. `--channel a2a` (the default) is a mock peer: it sends a
`task_request` on the peer network, the target runs the turn, and the model's
`task_response` is the answer; it requires a `peer_agent:` section. `--channel
tui` asks through an agent task, and the task's delivered `agent-task:<name>`
message is the answer — not the window's assistant text, which answers everything
folded into its turn. Both modes exit 0 on a completed answer, 2 on a usage
error, and 3 when no completed answer comes back. The container's one-shot mode
runs the default A2A road.

`--lang` affects the window only: labels, harness lines, panels, the model picker,
the approval row, and the status bar. System prompts, tool schemas, and log lines
remain English. External text passes through in its original language.

### Plugin executables

Each plugin can also be started directly, for a process manager or for debugging
one server.

| Executable | Server | Default port |
|---|---|---|
| `slife2-agent` | Agent loop and conversations | 24000 |
| `slife2-llm-openai` | OpenAI-compatible chat completions | 24001 |
| `slife2-llm-anthropic` | Anthropic Messages | 24002 |
| `slife2-llm-openai-responses` | OpenAI Responses | 24003 |
| `slife2-llm-embeddings` | Embeddings | 24004 |
| `slife2-tui` | Terminal inbox, one session per agent | 24005 |
| `slife2-context` | Turn log and context decisions | 24010 |
| `slife2-toolhub` | Tool catalogue and model tool list | 24020 |
| `slife2-wechat` | WeChat login and messaging | 24030 |
| `slife2-skills` | Playbooks and `skill_use` | 24031 |
| `slife2-cli` | The `cli:` section | 24032 |
| `slife2-jobs` | Code-defined tools (`job__<name>`) | 24042 |
| `slife2-mcp-tools` | The `mcp:` section | 24033 |
| `slife2-restapi-tools` | The `rest-api:` section | 24034 |
| `slife2-cabinet` | Notes, diary, files, reports; a turn's dumps and images | 24035 |
| `slife2-media` | Image, video, speech, transcription | 24036 |
| `slife2-agent-task` | Background agent tasks | 24037 |
| `slife2-file-tunnel` | Public HTTPS URL for a local file | 24038 |
| `slife2-schedule` | Scheduled tasks and their ledger | 24039 |
| `slife2-a2a` | Peer-agent network over MQTT | 24040 |

The subagent network runs the same implementation through its own entry module,
`python -m slife2.plugins.a2a.local_server`, on port 24041; it has no separate
console script.

**On WSL2 with `networkingMode=mirrored`, the WSL instance shifts these ports by
100** (agent 24100, toolhub 24120). The local MQTT broker is not shifted: both
stacks share it. Mirrored mode makes the Windows and WSL loopbacks one, so a WSL
stack on the default ports would be visible to — and silently reused by — a
Windows instance. `wsl_port_offset` changes the shift: default 100, `0` disables
it, and it is ignored outside mirrored mode.

## Using the terminal UI

### Keys

| Key | Action |
|---|---|
| `Enter` | Send the message. |
| `Shift+Enter` | Insert a line break. |
| `Esc` | Interrupt the running turn. |
| `Ctrl+C` | Copy when something is selected; otherwise interrupt; otherwise quit. |
| `Ctrl+S` | Open the model picker. `↑`/`↓` move, `Enter` selects, `Esc` closes. |
| `Ctrl+O` | Open the picker for subagents and running agent tasks; choosing one opens a read-only view. `Esc` closes it. |
| `Ctrl+Q` | Quit (Textual's binding; not shown because terminals with flow control may consume it). |
| `PageUp` / `PageDown` | Page the transcript. |
| `Home` / `End` | Jump to the start or end of the transcript. |
| `Ctrl+A` / `Ctrl+E` | Move to the start or end of the draft. |

The mouse wheel scrolls the transcript. Scrolling up holds the view while new
tokens arrive; sending a message returns it to the end.

### Sending messages

A message sent while a turn is running waits in the queue. The running turn
takes it at its next iteration boundary and folds it into that turn; a message
the turn does not reach runs as its own turn when its place comes. The status
bar shows the queue depth. `Esc` interrupts the running turn but leaves the
waiting messages intact.

### The plan

The model can keep a plan for the work in front of it. Each item is one line of
text with a status — `pending`, `active`, `done` or `blocked` — and the model
writes the whole list with `plan_write`. The list rides the `[TURN-PROMPT:{…}]`
marker at the start of every turn, so a context rebuild cannot drop it, and the
strip above the prompt shows it. The statuses are the model's own claims, not
the harness's observations. The plan is stored as `plan.yaml` in the
conversation's own directory; an empty write removes it.

### Model selection

`Ctrl+S` lists every model under `providers:` and marks the one in use.
Selecting one calls the same `model_switch` tool the model can call. The
conversation keeps its history and moves to the new model on its next turn; a
turn in progress finishes on the model it started with.

### Watching a subagent or agent task

`Ctrl+O` lists the subagents this agent has made and the agent tasks it is
running, and opens a read-only view of the one you choose. The view shows the
stored conversation and then the live turn — including a turn already in
progress when you open it. A task has no stored conversation: its view opens
with the description it was given, then its one turn. `Esc` returns. The view
is read-only: watching does not make you the subagent's window, the prompt is
not shown, and an approval request is not put to you. In the watched
conversation the message that woke it wears `Agent(<name>)>`; a message from a
sibling wears `Subagent(<name>)>`; a task's words wear `AsyncTask(<name>)>`.

Only running tasks are offered. A task that stops between the picker and the
choice opens on its final status and the sentence its ending carried. Watching
never makes a task finished: its answer still reaches the conversation by
hand-over.

### Restoring a conversation

Closing and reopening the window, restarting the servers, or an idle sweep does
not start a new conversation. The transcript is restored with its timestamps,
tool calls, and results; reasoning is folded where it was folded live.

The status bar reports the context size left by the last model call and its
fraction of the **prompt budget** (the model's window minus its `max_tokens`,
which the provider counts against the same window). At the start of each turn,
`_context_rebuild`'s answer names what the context decision kept and what it
recalled. It also reports `tools 26/358`: the hub's loaded set, then the
catalogue's size. Skills, jobs, and CLI entries count in the total, and so do
the tools of a server that is down; the loaded half is what the next request
carries. Both come from the same ask.

### Images

Name an image file in a sentence:

```text
What is wrong with this layout? D:\shots\screenshot.png
```

The model calls `attach_image` for it. There is no special syntax. A call can
take a local path, an `http(s)` URL, or a `data:` URI, and several at once. A
source that cannot be read is named in the answer without failing the rest. A
URL is passed to the provider, which fetches it.

Images are available only when the model's entry lists `image` under `input`.
The live conversation carries the pixels; the turn log keeps the sentence, the
tool call and answer, and an `[Image:…]` reference to a cabinet copy. A later
turn calls `attach_image` with the reference's key or the image's URL. A
provider that refuses an image drops it and says so; that turn's row is
note-only.

## Configuration

### Location

The configuration file is `slife2.yaml` inside the data directory. The data
directory is the working directory when it is this checkout — its
`pyproject.toml` names the project — otherwise `~/.slife2`. Use `--data-dir DIR`
or `SLIFE2_DATA_DIR` to override it.

The file is required, and it must name at least one provider under `providers:`
and one embedding provider under `embeddings:`. A missing file stops the start
with a one-line error naming the directory searched; a file that cannot be loaded
stops it naming the file and the mistake.

The installer may write `slife2-default.yaml` beside an existing configuration as
a reference. slife2 reads only `slife2.yaml`.

`AGENTS.md` sits beside the configuration and is required as well. It is the
model's guide to its tools, included in the system prompt every conversation
opens with. It is the system's deliverable, not a file to edit: a reinstall
replaces it, and there is no `AGENTS-default.md`. A prompt addition of your own
belongs in a template named by `agent.system_prompt`.

`slife2.yaml` is safe to check in. It should contain no secrets; every credential
is a `${VAR}` reference resolved at runtime.

### Sections

| Section | Purpose |
|---|---|
| `providers:` | Chat providers and their models; `providers.default:` names the model a new conversation starts on, as `provider/model`. |
| `embeddings:` | Embedding providers used by the turn, tool, and cabinet indexes; `embeddings.default:` selects the one in use, and each may name an optional `rerank_model` for the recall's rerank stage. |
| `media:` | Image, video, speech, and transcription providers. |
| `file-tunnel:` | The public-URL provider for local files; `file-tunnel.default:` selects it. |
| `mcp:` | External MCP servers (stdio or HTTP). |
| `rest-api:` | OpenAPI documents exposed as tools. |
| `cli:` | Commands already installed on the machine. |
| `skills:` | Environment for playbooks under `skills/`. |
| `agent:` | The iteration cap and the system-prompt template. |
| `context:` | Limits for the context decision. |
| `agent-task:` | `max_running`, the number of background tasks per agent. |
| `subagent:` | `max_running`, and the broker and `org` for the local subagent network. |
| `peer_agent:` | The remote peer-agent network. |
| `tool_load:` | The loaded-tool cap, and the tools that start loaded and are never evicted. |
| `tool_result:` | Size budget for a single tool result, in UTF-8 bytes. |
| `servers:` | Address and port of each plugin server. Defaults are used when absent. |

There is no `wechat:`, `cabinet:`, `schedule:`, or `tui:` section. A WeChat login
is created by scanning a QR code and stored under the agent's directory. The
cabinet, scheduler, and terminal inbox are enabled by their servers and by the
data they hold.

### Secrets

A value may be a literal string, used as written; or `${VAR}`, resolved through
the shell environment, then [credstore](https://pypi.org/project/credstore/)'s
OS keyring.

**Only credstore-managed secrets are protected from output.** A credential
slife2 resolves from credstore is shown masked — first four and last four
characters when at least as many stay hidden, otherwise `***` — in tool results,
tool listings, and log lines. A value shorter than eight characters is not
registered. An environment variable or a literal is the operator's own. Keep
every secret in credstore: it is the one store whose values slife2 masks
everywhere.

Resolution is lazy and lenient. A provider or media call refuses before use when
its credential did not resolve. A tool server's `env:` or `headers:` entry that
still holds its reference is omitted with a warning, so the child's own missing-key
policy decides: some servers run keyless, others refuse in their own words.

### Example

```yaml
agent:
  max_iterations: 0          # 0 or absent means no cap
  compact_at: 0.9            # of the prompt budget (window - max_tokens); 0 disables

context:
  ceiling: 0.8               # of the same budget
  recall_page_limit: 80
  min_similarity: 0.52
  keyword_weight: 1.0
  rerank_min_score: 0.3

mcp:
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
```

The shipped `slife2.yaml` is annotated in place and is the reference for every
key.

### Editing the configuration

The configuration is edited by hand or by the model. Each section or folder has
its own tools:

| Section or folder | Tools |
|---|---|
| `mcp:` | `mcp_get`, `mcp_set`, `mcp_list`, `mcp_remove`, `mcp_set_enabled` |
| `rest-api:` | `rest_api_get`, `rest_api_set`, `rest_api_list`, `rest_api_remove`, `rest_api_set_enabled` |
| `cli:` | `cli_get`, `cli_set`, `cli_list`, `cli_remove`, `cli_set_enabled` |
| `skills:` | `skill_draft`, `skill_set`, `skill_edit`, `skill_list`, `skill_remove`, `skill_set_enabled` |
| `jobs/` (a folder) | `job_set`, `job_list`, `job_read`, `job_run`, `job_remove` |
| `providers:` (including its `default:`) | `model_get`, `model_set`, `model_list`, `model_remove`, `model_switch` |

All writes go through one module (`slife2.libs.host.configfile`) under a
cross-process lock. The write preserves comments, and the result is validated by
the same loader used at startup. An entry the loader refuses is put back and the
caller is told why.

`set` writes the whole entry: a field left out is removed. For `model`, `mcp`,
`rest-api` and `cli`, read the matching `*_get` first: it returns the whole
object, `*_set` takes it back with every field required, and a field left out is
a schema error rather than a removal. `set_enabled` changes only `enabled`.
There is no approval gate before a write.

## Capabilities

### Conversation and memory

A conversation does not accumulate an unbounded context. At the first boundary
of each turn the harness asks the decision model which historical turns to keep
and which to recall from the log (`keep`/`recall`), then runs the turn on those
selections plus the turn itself. The model can call `_context_rebuild` itself.
The budget is the prompt budget (the window minus `max_tokens`); a selection over
`context.ceiling` is cut from its oldest end, and `fitted` says how many turns
went.

A turn over `agent.compact_at` (default 0.9) of that budget hides what it no
longer needs from its own view with `_turn_compact`; the log keeps everything,
and the next turn starts from the full conversation.

`turn_list`, `turn_search`, `turn_read` and `turn_summarize` browse the model's
own history. `turn_search`, `cabinet_search` and `tool_search` share one answer
shape: `keywords` and `sentences` in, `results`, `best`, `confidence`, `browsed`
and `limit` out.

A turn opens with `[TURN-PROMPT:{…}]` (`now`, and `plan` when there is one); a
rebuilt turn carries a `[TURN:{…}]` footnote. Capitalized markers are harness
annotations; a word-shaped marker such as `[Wechat:{…}]` was written by the
sender and is stored with the message.

### Tools

`slife2-toolhub` owns the model's tool list. `slife2-mcp-tools` holds the `mcp:`
connections and `slife2-restapi-tools` the `rest-api:` connections; the hub
merges what they declare, names the tools, gates them, and routes calls back.

The hub serves four set-level tools:

- `tool_search` searches the whole catalogue by keyword and meaning;
  `unloaded_only` leaves out tools already in the list.
- `tool_list` pages the catalogue with `category`/`source` filters.
- `func_tool_load` puts tools into the list shown to the model.
- `_func_tool_unload` removes them; a name in `tool_load.autoload` is refused.

A model can call any catalogued name without loading it: the harness loads it
and runs the call in the same turn, so a name it knows from its history still
works after the budget evicts it. The catalogue row is the authority that the
tool exists; the tool's own server judges the arguments, and its refusal is the
answer. Loading keeps the tool — and its current declaration — in the list sent
with every request. `tool_load.autoload` names the tools that start loaded;
everything else is discovered on demand.

The loaded set survives a restart. It is bounded by `tool_load.threshold`
(default 100); over the limit, the least recently used or loaded are unloaded,
and nothing in `tool_load.autoload` is ever evicted or unloadable by the model.

Tool names: an `mcp:` tool is `{server}__{tool}`; a plugin's own tools use bare
names. Two tools that would present one name are refused.

A tool result is a JSON object, except `_check_new_input` (a person's message
verbatim) and `skill_use` (the playbook text). A listing answers `{"<noun>": [],
"total": N, "limit": L, "offset": O}`; a refusal raises a `ToolError`. An
oversized output is stored as a dump (see [Cabinet](#cabinet)).

#### Tool call options

Every function tool accepts three meta-parameters, added to every advertised
schema and stripped before dispatch.

| Parameter | Effect |
|---|---|
| `_timeout` | Seconds for this call, overriding the tool's default. Any value that is not a positive finite number — `0`, a negative, text — fails the call; it is never read as "no limit" or as the default. A tool with its own `timeout` argument enforces it itself. |
| `_async` | Run the call in the background. The model receives a `task_id`; the result arrives with an `[AgentToolResult:{…}]` marker on the `async-tool:<tool>` channel. `check_async` reports the state; `cancel_async` abandons it. An agent task is one turn: its prompt tells the model not to use `_async` or start another task. |
| `_approve` | Ask the user before running. The terminal draws an inline row (`y` approves, `n` or `Esc` denies) and answers with `answer_approval`. If the channel has no person behind it, the call runs without asking. A caller that can answer says so with `watched=true`. |

#### Skills and commands

Playbooks live in `<data>/skills/<name>/SKILL.md`. `skill_use` returns the
playbook's text preceded by its directory. A skill is instructions, not code;
nothing in slife2 executes it. Its header may declare environment requirements,
resolved from the `skills:` section; `skill_use` reports a missing key before
the model depends on it. `skill_draft` turns a workflow into a `SKILL.md`
without writing anything; `skill_set` installs a skill, and `skill_edit` writes
only the files it names. `skill_remove` deletes the folder and its `skills:`
entry.

`cli:` entries describe programs already installed. Every entry is a catalogue
row (`cli:yt-dlp`) found by `tool_search`, but not a callable tool and with no
load state: the model runs the command through a shell tool in its own set (the
shipped configuration provides `desktop-commander`). An entry's `install:` line
is shown when the program is missing. `enabled: false` keeps an entry configured
but never connects it; the value must be a real YAML boolean (`no`, `off` and
`'false'` are strings under YAML 1.2 and are refused).

#### Jobs

`job_set` writes a **job** — one Python file in `<data>/jobs/`, one module-level
function named after the file — and that function becomes `job__<name>`. The
docstring and typed signature are the tool definition; there is no manifest. The
same call replaces a job, restoring the previous version if the new file does not
load. `job_remove` deletes it; `job_list`, `job_read` and `job_run` list, read
and test one.

A job reaches the system through `slife2.job`: `call_tool(name, arguments)`
reaches the hub's catalogue by the name the model calls, and `call(server, tool,
arguments)` reaches any server in `servers:` by its key. The code runs in the
jobs plugin with the caller's identity, is not sandboxed, and the top-level tool
call is the approval point. The `job-coding` skill is the authoring guide.

### Media

The `media:` section provides four capabilities:

- `generate_image` — draw an image from a prompt.
- `generate_video` — render a video from a prompt or an image.
- `text_to_speech` — say `text`, or read a text file named in `input`, into an audio file.
- `transcribe_audio` — transcribe the audio file named in `input`; `output` writes the transcript to the path you name, while without `output` the transcript is the answer and a long one is kept as a dump.

Each generation tool writes into the agent's media directory by default; `output`
names the file instead (a relative path is relative to this conversation's work
directory). Every tool answers with the file's path as text, or — for
`transcribe_audio` without `output` — the transcript.

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
transcription) and `openai-images` (image generation only). Choosing a model that
does not support the requested kind is refused.

Generated files are written under `<data>/agents/<agent>/media/` by default. Each
generation tool also takes an `output` path: a relative one is relative to the
conversation's work directory, an absolute one is used as written. A long-running
video call that exceeds its timeout is refused with the provider's job id rather
than its result.

### File tunnel

`slife2-file-tunnel` publishes a local file at a public HTTPS URL:

```text
expose_local_image(path)      -> https://<tunnel>/share/<token>
unexpose_local_image(token)   -> the URL stops working
```

The file is served from its original location. A link stops working when the file
changes, when `unexpose_local_image` is called, or when the process exits. A share
outlives the turn that created it. Private keys, `.env` files, and saved
passwords are refused by name. `path` is an absolute path, or one relative to the
data directory — where the agent's own tools run.

The tunnel is pointed at a loopback listener that serves only `/share/<token>`;
the MCP endpoint is not published, so knowing the hostname does not let a caller
run tools.

Three providers are supported. Exactly one is active; the others may stay
configured and inert.

| Provider | Requirement | URL behavior |
|---|---|---|
| `ngrok` | `NGROK_AUTHTOKEN` | One stable development domain per account; the free tier shows an interstitial page to browsers. |
| `localhost.run` | An `ssh` client | A random `*.lhr.life` hostname that rotates every few hours; TLS terminates at the provider. |
| `cloudflare` | The `cloudflared` binary | A `*.trycloudflare.com` hostname stable for the process's lifetime. |

The built-in default is `ngrok`. The shipped `slife2.yaml` selects `cloudflare`,
which needs no account; set `active:` to `localhost.run` if `cloudflared` is not
installed. A tunnel that is not up, or whose URL cannot be reached, is refused
rather than handed out. The `tunnel_status` tool reports the state, URL,
reachability, edge address, and binary in use; it is an internal diagnostic and
is not exposed to the model.

### Cabinet

The cabinet is a per-agent store of notes, diary entries, saved files, reports,
dumps, and images. The first four are its corpus; meaning search covers notes,
diary days and reports by their whole file text, while a saved file is found by
words only and dumps and images are addressed by the markers in the turn.

A note, diary day and report share four verbs: `{kind}_write` creates and refuses
an existing key (naming `_read` and `_update`), `{kind}_read` reads,
`{kind}_update` replaces the whole document, and `{kind}_remove` deletes it.

| Kind | Key | What a write means |
|---|---|---|
| `note` | A subject chosen by the user | Creates the note under that subject. |
| `diary` | A date | Creates that day's entry; one day is one document. |
| `file` | Its path inside the cabinet | `file_save` copies a local file or fetches an `http(s)` URL in; `file_read`, `file_grep` and `file_remove` read, search and delete it. |
| `report` | A title | Creates the report under that title. |
| `dump` | The tool call that produced the output | Written by the harness; a second write of one call replaces it. |
| `image` | Its content digest | Written by the harness; bytes in `pictures/`; one digest is one row. |

A replacement is the whole document; an omitted tag or period is cleared.

A dump is a tool output too large for the conversation. It is stored whole in its
file, keyed by the tool call id; the conversation carries a marked head within
`tool_result.bytes`. `dump_read` reads any part by line or character, and
`dump_grep` finds a literal within one line and answers both addresses.

A saved file is read and searched with the same readers (`file_read`,
`file_grep`) over its decoded text. Both stream, so memory is the window, not the
file; the file is never truncated and `next` asks for the following window. A
PDF, image or archive is shown as a bounded escaped rendering of its bytes.

An image is stored whole under `pictures/`, keyed by its content digest, and the
turn carries an `[Image:…]` reference; `attach_image` is how the model sees it
again.

A note, diary entry and report live once as a file under
`agents/<agent>/cabinet/`; the row is the address, and the file's text is what
the meaning leg embeds. Every answer carries `path`, the file relative to the
data directory, so the model can hand it to `pandoc` or `rg`.

`file_save` copies a local file (never moving it) or fetches an `http(s)` URL.
Each source stands alone: the answer lists what was saved and what failed. A
fetched URL reaches the public internet only — credentials, `localhost` and
non-globally-routable literal addresses are refused — and is capped at 50 MiB.

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

The connection's arrival and departure are drawn as harness lines (`[⚡ Wechat
online]`, `[✗ Wechat offline]`), not turns.

A message from WeChat becomes a turn in that agent's conversation and is drawn in
the window showing it. It is recorded with channel `wechat` and a `[Wechat:{…}]`
prefix naming the sender. Replying is the model's job: nothing auto-dispatches an
answer. The model calls `wechat_send_message` with the peer and the
`context_token` from the marker. While the turn runs, the other person sees a
typing indicator. Each agent logs in separately; a subagent does not, and the
plugin is one shared process.

### Agent tasks

An agent task is a separate turn of the calling conversation — the agent's own or
one of its subagents' — started with a name and a description and answered later
by a message. It is one turn: its prompt tells it not to use `_async` or start
another task, because a result arriving later has nowhere to go.

| Tool | Purpose |
|---|---|
| `agent_task_start` | Start a task and return its id immediately. |
| `agent_task_status` | Report what a task is doing. Never returns its result. |
| `agent_task_list` | List this agent's tasks, running first. |
| `agent_task_cancel` | Stop a task and report its state after the call. |

A task runs as the conversation that started it: an agent's task uses the agent's
system prompt, model, tools, and its own context decision; a subagent's task uses
the subagent's prompt, role, and model. **The context is a clone**: the task
reads the conversation's context and chooses what its turn runs on, and the
conversation's stored context is unchanged when the task ends. It does not have
the turn that started it, so the description must restate the relevant context.
Its answer arrives with an `[AgentTaskResult:{…}]` marker on the
`agent-task:<name>` channel and is drawn as `AsyncTask(<name>)>`. A subagent has
no window, so a subagent's task answer is delivered to its agent's conversation.
If no window is showing the agent when it stops, the task ends `failed` — the
answer is the product and is not kept — and the log says why.

```yaml
agent-task:
  max_running: 4
```

`max_running` bounds concurrency, not total work over time.

### Scheduled tasks

A scheduled task is a name, a description, and a cron expression written by the
model. `slife2-schedule` fires it while slife2 is running. When a task comes due,
the scheduler asks whether a window is showing that agent: a window that is not
there ends the run as `missed` — the answer would have nowhere to go — and a
window that is there starts the work directly as an agent task. Each run's work
is an agent task named `<task>@<due time>`, so two overlapping runs of one task
are two tasks rather than one name answering for both. No turn is opened and no
model call is spent. A terminal that cannot be asked, or a dispatch that fails,
ends the run as `failed` with the reason recorded.

| Tool | Purpose |
|---|---|
| `schedule_set` | Write or replace a task: name, description, cron, timezone. |
| `schedule_remove` | Remove a task and all of its runs. |
| `schedule_list` | List tasks and their next due times. |
| `schedule_runs` | List the ledger: one row per fire. |
| `schedule_run_now` | Start this run's work now; with `due_at`, backfill a missed or failed run. |
| `schedule_run_skip` | Close a run that will not happen, with a reason. |

A run is one fire, named by the moment it was due. Its state moves from
`pending` to `ran`, `failed`, `missed`, or `skipped`. `ran` means the task
finished and its answer reached the conversation; a task that could not deliver
is `failed` and can be re-run. The row is written before the window is asked, so
a fire cannot repeat on the next poll. The clock is a poll: a fire can start up
to 30 seconds after its due moment, and a finished run closes on the next pass;
`ran_at` is the task's own finish time.

Tasks fire only while slife2 is running. On startup, the sweep closes runs left
open by a stopped process — a run that never started is `missed`, one that names
a task is `failed` — and the poll records the newest missed fire for each task.
`schedule_runs(status="missed")` and `status="failed"` list them; either can be
re-run with `schedule_run_now(name, due_at)` or closed with `schedule_run_skip`.

The ledger is `<data>/agents/<agent>/schedule.db`. There is no configuration
section. A task's `timezone` is an IANA name; leaving it out uses the machine's
current clock, which does not follow a daylight-saving change.

### Peer agents

An agent's peers are other agents, reached over the published A2A-over-MQTT
profile (0.1, over A2A v1.0.0). A person writes `peer_agent:` in `slife2.yaml`;
the model sees `peer_agent_*` tools, and the protocol name appears in nothing it
reads.

| Tool | Purpose |
|---|---|
| `peer_agent_send_message` | Send a task (`task_request`) or a plain message; the result arrives later. |
| `peer_agent_task_response` | Settle a task a peer sent you: completed, failed, rejected, or the interrupted states that keep it open. |
| `peer_agent_continue_task` | Resume a task you sent that the peer paused for input. |
| `peer_agent_cancel_task` | Withdraw a task this agent sent. |
| `peer_agent_list` | List this agent first, then every peer by name. |
| `peer_agent_task_list` | List open tasks: those waiting for an answer and those still owed one. |
| `peer_agent_broadcast` | Send a fire-and-forget event to every peer. |
| `peer_agent_group_set` | Create a group and pull in the members you name; you become its owner. |
| `peer_agent_group_list` | List the groups this conversation knows, with owner and your role. |
| `peer_agent_group_member_list` | List a group's owner and members. |
| `peer_agent_group_remove` | Disband a group you own. |
| `peer_agent_group_leave` | Leave a group; as owner, ownership passes to the first remaining member. |
| `peer_agent_group_send_message` | Send a fire-and-forget message to every other member. `@name` and `@all`/`@所有人` address members, as in a WeChat group. |

A `task_request` returns an id immediately; the result arrives later with a
`[PeerAgent:…]` marker. The id and `status: "unverified"` describe acceptance,
not delivery; a send nothing answers ends as an unreachable notice. A peer that
accepted a task but could not hand it to its conversation answers `rejected`. A
peer may pause a task and ask for input: continue it with
`peer_agent_continue_task` or withdraw it with `peer_agent_cancel_task`; do not
send a new task. A task a peer sends you is settled with
`peer_agent_task_response` (`completed`, `failed` or `rejected`; the interrupted
states pause it).

There is one mesh per agent; a second live instance under one name is refused. A
subagent is not on this network. Presence is a harness line, not a turn, and the
roster is `peer_agent_list`; an agent is online while a window (or `--headless`)
holds its `join` call. Task state is in memory, so a restart starts with none.

A group is a named roster with an owner; no invitation is involved. Group
messages are fire-and-forget to every other member, on the
`peer_agent_group:<group>:<sender>` channel. **A group chat is casual**: no
member owes an answer and `sent` confirms the send, not that anyone read it; use
the direct send tool when you need an answer. A member offline when a message is
sent misses it.

The broker is started when the configured one is local and absent, and stopped
with the stack only if slife2 started it. `provider: mosquitto` is the only
supported value.

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
address; omitting `peer_agent:` keeps the machine off the network. A restart
drops working rounds and paused tasks alike; a peer still waiting sees the task
in its own `peer_agent_task_list` and can cancel or re-send it.

### Subagents

A subagent is an agent that belongs to another agent. It has the maker's tool
set, its own role-specific system prompt, and its own conversation, but no
process, no window, no claim on an agent name, and no external identity. It runs
on the local subagent network when something sends it a message. A subagent is a
persistent agent with a memory of its own; for one piece of work beside the
conversation, use an agent task.

A subagent is created with `subagent_create` and removed with `subagent_remove`.
Once created, it is addressed over the local network under `subagent_*` names:
`subagent_send_message`, `subagent_task_response`, `subagent_continue_task`,
`subagent_cancel_task`, `subagent_list`, `subagent_task_list`,
`subagent_broadcast`, and the six group tools.

| Key | Meaning |
|---|---|
| `name` | The subagent's name and directory. |
| `model` | `provider/model`; empty uses the maker's model. |
| `role` | A word included in the subagent's prompt. |

A subagent lives in `agents/<maker>/subagents/<name>/` with its own `turns.db`,
`subagent.yaml`, cabinet, and media directory. Scheduling stays with the agent.
A subagent cannot create or remove a subagent.

Removing a subagent first tells the local network to release its place, then
stops its loop, then deletes its directory. If the network notification cannot be
made, the subagent is left unchanged.

The local network is configured under `subagent:`. It needs a `broker`; a
`peer_agent:` section with no broker uses the same address. `unit` is always the
agent's name, and writing one is refused. `org` may be set to group meshes.

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
        <name>/                a subagent's turns.db, subagent.yaml,
                               cabinet/ and media/
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

Each server logs at INFO by default. `SLIFE2_LOG_LEVEL=DEBUG` raises the level for
every server started with it; per-call durations, tool calls and connection
detail live there, and a value that is not a level falls back to INFO with a line
saying so. A log that outgrows its ceiling is rotated to `<log>.1` at the next
start. An external `mcp:` entry's stdio child writes its own file under `logs/`.

### Multiple data directories

A data directory may hold several agents. They share its `slife2.yaml`, its
`skills/`, and its `agents/tools.db`, so they run on the same tool set. A tool
loaded by one is loaded for the other.

Agent identity is independent of the directory. `--agent jack --data-dir D` names
the agent and says where its files go. The name is claimed once per machine, so a
second live `jack` in another data directory is refused. Which directory a name
used previously is not remembered.

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

- A skill is instructions and a `cli:` entry is a command: slife2 serves no
  runner for either. The model reads the playbook and runs commands through the
  shell its tool set provides.
- Jobs are code: they run in `slife2-jobs` with the caller's identity and are
  not sandboxed. The top-level tool call is the approval point.
- There is no approval gate before a configuration edit, including `mcp_set`
  with a `command:` that records a program slife2 will start.
- `agent-task.max_running` bounds concurrent tasks, not total work over time.

## License

MIT. See [LICENSE](LICENSE).
