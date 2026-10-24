# slife2 container distribution

This image is the **real slife2 distribution**, not a reduced bench build:

- the source and `slife2.yaml` come from the slife2 checkout unchanged;
- the project's own `scripts/install.sh` runs inside the image (Python 3.13, the
  venv, Node/npx, cloudflared, yt-dlp, browser-harness, mosquitto, the
  configured MCP servers' launchers);
- the shipped `slife2.yaml` is used as-is, including `desktop-commander`, the
  subagent/peer broker, media, file-tunnel, skills and the full tool autoload
  list.

The shell is Desktop Commander (`desktop-commander__start_process`), running
inside this container. That is why this image can be the environment a
Terminal-Bench task runs in: commands issued by the model see the task's
filesystem, not the host's.

## Build

From the slife2 checkout:

```bash
docker build -f docker/Dockerfile -t slife2:local .
```

`.dockerignore` keeps the virtualenv, agent data and test caches out of the
build context. The image is large (about 3 GB): it contains the full
installer's programs, not a minimal agent.

## Run

Set up the credstore volume once (see Credentials), then start the real agent
headless (the same window, without a display); the entrypoint asks for the
master password:

```bash
docker run --rm -it -v slife2-cred:/home/slife2/.credstore slife2:local
```

Run one prompt and exit (it asks for the same password):

```bash
docker run --rm -it -v slife2-cred:/home/slife2/.credstore \
  slife2:local 'Use the shell tool to run `pwd; echo CONTAINER-OK` and report the output.'
```

The one-shot mode starts the whole stack, hands the prompt to the headless
window through `slife2-tui`'s `submit` (the same road a WeChat message or an
agent task takes), prints the answer from the turn the window recorded, and
stops the stack. A `submit` refused because no window is attached yet is
retried: that retry is the readiness wait, because `slife2 run` opens the window
only after every server is ensured and the catalogue is built. The answer is on
stdout; the turn id is on stderr. The window is the only client of the
conversation: the one-shot driver does not call `send_message` itself.

**And the external sources are awaited before the prompt.** A user's home is
warm — the `mcp:`/`rest-api:` servers were connected on the previous run — but a
fresh container starts all of them from cold, and a prompt sent while they are
still `connecting` runs its first turn against a partial tool set. So the
driver asks the toolhub's `servers` until every enabled `mcp:`/`rest-api:` entry
reports `ready`; a source that never gets there fails the run rather than
sending the prompt anyway.

## Credentials

The container has no keyring, but it can create and read credstore's own
encrypted cred file. A volume keeps it across starts, and one master password
then resolves every `${VAR}` in the configuration:

```bash
docker volume create slife2-cred
docker run --rm -it -v slife2-cred:/home/slife2/.credstore \
  --entrypoint bash slife2:local
# in that shell:
credstore set-password            # choose the master password
credstore set DEEPSEEK_API_KEY    # then every line the build log listed
```

The build runs `scripts/install.sh`, whose log lists every
`credstore set <KEY>` line the shipped configuration names; run those in the
shell above. Start the stack with the same volume; the entrypoint asks for the
master password you chose:

```bash
docker run --rm -it -v slife2-cred:/home/slife2/.credstore slife2:local
```

A mounted `slife2.yaml` whose values are literals is the other supported
source. A launcher with no terminal sets `CREDSTORE_MASTER_PASSWORD` itself
instead of being prompted.

Useful environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `SLIFE2_PROMPT` | none | One-shot prompt for a launcher that cannot pass it as the command argument. Omit to keep the stack running. |
| `SLIFE2_AGENT` | `slife2` | The agent name (the headless window's claim). |
| `SLIFE2_BENCH_TUI_URL` | the config's `servers.tui.url` | The TUI plugin's endpoint; the prompt is handed to the window there. |
| `SLIFE2_BENCH_TOOLHUB_URL` | the config's `servers.toolhub.url` | The toolhub's endpoint; the readiness gate asks `servers` there. |
| `SLIFE2_BENCH_READY_TIMEOUT` | `900` | Seconds to wait for the window to open (the launcher's own budgets are 300 s per server and 360 s for the catalogue, because a fresh container's cold start is slower than a warm one). |
| `SLIFE2_BENCH_TOOL_READY_TIMEOUT` | `900` | Seconds to wait for every enabled `mcp:`/`rest-api:` source to report `ready`. |
| `SLIFE2_BENCH_TURN_TIMEOUT` | `3600` | Seconds to wait for the window to record the turn. |
| `CREDSTORE_MASTER_PASSWORD` | none | For a launcher with no terminal: the master password for the cred file in the volume. The interactive entrypoint asks for it instead. |

The image runs as the non-root user `slife2`. The checkout (and therefore the
data directory) is `/opt/slife2`; the runtime directory is
`/home/slife2/.slife2/runtime`. Mount volumes there if the data should outlive
the container.

## Notes

- The full configuration names many external services (Serper, Tavily, AMap,
  Baidu, SiliconFlow, media providers, ...). Missing credentials are resolved
  lazily: the stack starts, and a tool that needs a key fails at the call. Only
  `DEEPSEEK_API_KEY` is needed for the basic model turn.
- **Keys are configured in the container's credstore** (see Credentials above);
  a mounted `slife2.yaml` whose values are literals is the other supported
  source. The build runs `scripts/install.sh`, so credstore is installed and
  its `credstore set` hints are in the build log.
- The `slife2` command in the image is the checkout's `.venv/bin/slife2`; the
  installer's `uv run slife2` is equivalent.
- Every enabled `mcp:`/`rest-api:` source is **installed** at build time by
  `docker/prewarm.py`, from the shipped config rather than a list in the file:
  an `npx` source fills the same `_npx/<hash>` tree `npx -y <spec>` would use,
  a `uvx` source builds the same environment `uvx <pkg>` reuses. The packages'
  install scripts run with them, which is what pulls Chrome for Testing into
  `~/.cache/puppeteer`. Nothing is started, so a server that ignores EOF cannot
  hang the build, and a failed install fails the build.
- Both browser sources drive the **installed Google Chrome**, not a bundled
  Chromium: `@playwright/mcp` defaults to `channel: chrome` and
  `chrome-devtools-mcp` to `stable`, and both look for
  `/opt/google/chrome/chrome`. The image installs that package with
  `playwright install chrome`. Chrome's own sandbox needs a PID namespace the
  container runtime may refuse (`Failed to move to new namespace`); where it
  does, a browser call needs `--no-sandbox`, which is a runtime decision, not an
  image one. Starting the server and listing its tools do not need a browser.
- Docker Desktop's kernel reports as WSL in `/proc/version`, so credstore would
  pick its WSL backend and look for `powershell.exe`. `sitecustomize.py` makes
  it treat the container as native Linux. It is installed **into the venv's
  site-packages**, so it travels with a filesystem copy of this image (a task
  container carries no `ENV`) and needs no `PYTHONPATH`. Missing `${VAR}`
  references then stay unresolved, which is slife2's documented lenient
  behavior; pass real values through the environment.
- This is the image the benchmark should run **inside** the task container
  (or build its task image from). The host-side bare adapter in
  `slife2-bench` is not this path.
