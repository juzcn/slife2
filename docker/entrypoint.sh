#!/usr/bin/env bash
set -euo pipefail

cd /opt/slife2
export PATH="${HOME}/.local/bin:/opt/slife2/.venv/bin:${PATH}"
export SLIFE2_RUNTIME_DIR="${SLIFE2_RUNTIME_DIR:-${HOME}/.slife2/runtime}"
mkdir -p "${SLIFE2_RUNTIME_DIR}"

AGENT="${SLIFE2_AGENT:-slife2}"
SLIFE2=/opt/slife2/.venv/bin/slife2

# **The one-shot prompt is an argument, not an environment variable.**  A
# credential or a key must not be spelled `-e KEY=...` on the command line, and
# the prompt is the one thing a one-shot run has to say.  `SLIFE2_PROMPT` stays
# for a launcher that cannot pass an argument (Compose, a wrapper).
PROMPT="${SLIFE2_PROMPT:-}"
if [[ -z "${PROMPT}" && $# -gt 0 ]]; then
    PROMPT="$*"
fi

if [[ -z "${PROMPT}" ]]; then
    exec "${SLIFE2}" run --headless --agent "${AGENT}"
fi

"${SLIFE2}" run --headless --agent "${AGENT}" &
STACK_PID=$!

cleanup() {
    "${SLIFE2}" down --force >/dev/null 2>&1 || true
    kill "${STACK_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# **The one-shot is the CLI's probe.**  The stack above is what it asks; the
# task and its terminal reply are `slife2 probe`'s, so the driver is the same
# one a shell in the image runs and the bench calls.
probe=(--agent "${AGENT}" --prompt "${PROMPT}")
if [[ -n "${SLIFE2_BENCH_TURN_TIMEOUT:-}" ]]; then
    probe=(--timeout "${SLIFE2_BENCH_TURN_TIMEOUT}" "${probe[@]}")
fi
"${SLIFE2}" probe "${probe[@]}"
