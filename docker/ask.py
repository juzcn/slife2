from __future__ import annotations

import asyncio
import os
import sys

from fastmcp import Client

DEFAULT_URL = "http://127.0.0.1:24000/mcp"


async def main() -> int:
    prompt = (
        " ".join(sys.argv[1:]).strip() or os.environ.get("SLIFE2_PROMPT", "").strip()
    )
    if not prompt:
        print("no prompt given", file=sys.stderr)
        return 2

    agent = os.environ.get("SLIFE2_AGENT", "slife2")
    url = os.environ.get("SLIFE2_BENCH_AGENT_URL", DEFAULT_URL)
    timeout = float(os.environ.get("SLIFE2_BENCH_TURN_TIMEOUT", "3600"))

    async with Client(url, timeout=timeout) as client:
        result = await client.call_tool(
            "send_message",
            {
                "agent": agent,
                "prompt": prompt,
                "watched": False,
                "channel": "container",
            },
        )

    data = result.data
    if not isinstance(data, dict):
        print(f"unexpected send_message payload: {data!r}", file=sys.stderr)
        return 1

    print(data.get("text", ""))
    print(
        f"agent={agent} iterations={data.get('iterations')} "
        f"stop_reason={data.get('stop_reason')} usage={data.get('usage')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
