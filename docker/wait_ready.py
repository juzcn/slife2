from __future__ import annotations

import json
import subprocess
import sys
import time

SLIFE2 = "/opt/slife2/.venv/bin/slife2"
CORE = {
    "agent",
    "context",
    "toolhub",
    "mcp-tools",
    "skills-server",
    "cabinet",
    "cli-server",
    "embeddings",
}


def main() -> int:
    last = ""
    for _ in range(180):
        result = subprocess.run(
            [SLIFE2, "status", "--json"],
            capture_output=True,
            text=True,
        )
        last = result.stdout
        try:
            data = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}

        if data.get("ok") is True:
            return 0

        states = {
            row.get("name"): row.get("state")
            for row in data.get("servers", [])
            if isinstance(row, dict)
        }
        llm_ready = any(
            name.startswith("llm:") and state == "running"
            for name, state in states.items()
        )
        if llm_ready and all(states.get(name) == "running" for name in CORE):
            return 0

        time.sleep(2)

    print("slife2 did not become ready; last status follows", file=sys.stderr)
    print(last, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
