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

The one-shot mode starts the whole stack, waits for the core servers, sends
the prompt through the agent server's `send_message`, prints the answer, and
stops the stack. The answer is on stdout; the usage line is on stderr.

Useful environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | none | The shipped config's provider key; required for a turn. |
| `SLIFE2_PROMPT` | none | One-shot prompt. Omit to keep the stack running. |
| `SLIFE2_AGENT` | `slife2` | The agent name (the headless window's claim). |
| `SLIFE2_BENCH_AGENT_URL` | `http://127.0.0.1:24000/mcp` | The agent server's MCP endpoint. |
| `SLIFE2_BENCH_TURN_TIMEOUT` | `3600` | Seconds for one turn. |

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
