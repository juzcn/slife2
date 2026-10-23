#!/usr/bin/env bash
set -euo pipefail

cd /opt/slife2
export PATH="${HOME}/.local/bin:/opt/slife2/.venv/bin:${PATH}"
export SLIFE2_RUNTIME_DIR="${SLIFE2_RUNTIME_DIR:-${HOME}/.slife2/runtime}"
mkdir -p "${SLIFE2_RUNTIME_DIR}"

AGENT="${SLIFE2_AGENT:-slife2}"
SLIFE2=/opt/slife2/.venv/bin/slife2

if [[ -z "${SLIFE2_PROMPT:-}" ]]; then
    exec "${SLIFE2}" run --headless --agent "${AGENT}"
fi

"${SLIFE2}" run --headless --agent "${AGENT}" &
STACK_PID=$!

cleanup() {
    "${SLIFE2}" down --force >/dev/null 2>&1 || true
    kill "${STACK_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

/opt/slife2/.venv/bin/python /opt/slife2-bench/ask.py "${SLIFE2_PROMPT}"
