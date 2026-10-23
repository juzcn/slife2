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

Start the real agent headless (the same window, without a display):

```bash
docker run --rm -it -e DEEPSEEK_API_KEY=... slife2:local
```

Run one prompt and exit:

```bash
docker run --rm -e DEEPSEEK_API_KEY=... \
  -e SLIFE2_PROMPT='Use the shell tool to run `pwd; echo CONTAINER-OK` and report the output.' \
  slife2:local
```

The one-shot mode starts the whole stack, hands the prompt to the headless
window through `slife2-tui`'s `submit` (the same road a WeChat message or an
agent task takes), prints the answer from the turn the window recorded, and
stops the stack. A `submit` refused because no window is attached yet is
retried: that retry is the readiness wait, because `slife2 run` opens the window
only after every server is ensured and the catalogue is built. The answer is on
stdout; the turn id is on stderr. The window is the only client of the
conversation: the one-shot driver does not call `send_message` itself.

Useful environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | none | The shipped config's provider key; required for a turn. |
| `SLIFE2_PROMPT` | none | One-shot prompt. Omit to keep the stack running. |
| `SLIFE2_AGENT` | `slife2` | The agent name (the headless window's claim). |
| `SLIFE2_BENCH_TUI_URL` | `http://127.0.0.1:24005/mcp` | The TUI plugin's endpoint; the prompt is handed to the window there. |
| `SLIFE2_BENCH_READY_TIMEOUT` | `420` | Seconds to wait for the window to open (the launcher's catalogue budget is 240 s, because the external `mcp:` sources connect on their own schedule). |
| `SLIFE2_BENCH_TOOL_SETTLE` | `60` | Seconds after the window opens before the prompt goes in, so the background `mcp:`/REST tool servers can finish connecting. |
| `SLIFE2_BENCH_TURN_TIMEOUT` | `3600` | Seconds to wait for the window to record the turn. |

The image runs as the non-root user `slife2`. The checkout (and therefore the
data directory) is `/opt/slife2`; the runtime directory is
`/home/slife2/.slife2/runtime`. Mount volumes there if the data should outlive
the container.

## Notes

- The full configuration names many external services (Serper, Tavily, AMap,
  Baidu, SiliconFlow, media providers, ...). Missing credentials are resolved
  lazily: the stack starts, and a tool that needs a key fails at the call. Only
  `DEEPSEEK_API_KEY` is needed for the basic model turn.
- The `slife2` command in the image is the checkout's `.venv/bin/slife2`; the
  installer's `uv run slife2` is equivalent.
- Desktop Commander is pre-warmed at build time with the same `npx` line the
  shipped config uses. Without that, a fresh container can lose the first MCP
  handshake while npx downloads the package, and the shell is missing for that
  run. Other `mcp:` servers may still cold-start on first use.
- Docker Desktop's kernel reports as WSL in `/proc/version`, so credstore would
  pick its WSL backend and look for `powershell.exe`. `sitecustomize.py` makes
  it treat the container as native Linux. Missing `${VAR}` references then stay
  unresolved, which is slife2's documented lenient behavior; pass real values
  through the environment.
- This is the image the benchmark should run **inside** the task container
  (or build its task image from). The host-side bare adapter in
  `slife2-bench` is not this path.
