from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.libs.harness.handover import submit_to
from slife2.libs.host.paths import agent_dir
from slife2.libs.store.db import message_text
from slife2.plugins.tui import server as control
from slife2.plugins.tui.server import NOTE

DEFAULT_TUI_URL = "http://127.0.0.1:24005/mcp"
TURN_TIMEOUT = float(os.environ.get("SLIFE2_BENCH_TURN_TIMEOUT", "3600"))
#: How long the window may take to open.  `slife2 run` opens it only after
#: every server is ensured and the catalogue is built, and the catalogue budget
#: is `work.catalogue` (240 s) because the external `mcp:` sources connect on
#: their own schedule; this gate has to outlast that, not a warm start's two
#: minutes.
READY_TIMEOUT = float(os.environ.get("SLIFE2_BENCH_READY_TIMEOUT", "420"))
#: After the window is up, how long the external tool servers get to connect
#: before the prompt goes in.  They start in the background and are listed as
#: they answer, so the window's own readiness does not include them; a minute is
#: the simple settle that covers the ones the tasks use.
TOOL_SETTLE = float(os.environ.get("SLIFE2_BENCH_TOOL_SETTLE", "60"))


class NotReady(RuntimeError):
    """No window opened within the readiness budget."""


def database_of(agent: str) -> Path:
    """The agent's turn store — the record its window writes while it runs."""
    return agent_dir(agent) / "turns.db"


def last_turn(path: Path) -> int:
    """The newest turn id, or 0 for a store that does not exist yet."""
    if not path.is_file():
        return 0
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return 0
    try:
        row = connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM turn").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def _answer(messages: list[dict]) -> str:
    """The turn's last assistant text, which is its answer."""
    for message in reversed(messages):
        if message.get("role") == "assistant":
            text = message_text(message.get("content"))
            if text.strip():
                return text
    return ""


def new_turn(path: Path, after: int) -> tuple[int, str] | None:
    """The first turn written after `after`, or None while it is still running."""
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT rowid, messages FROM turn WHERE rowid > ? ORDER BY rowid LIMIT 1",
            (after,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        messages = json.loads(row[1])
    except (TypeError, ValueError):
        return None
    if not isinstance(messages, list):
        return None
    return int(row[0]), _answer(messages)


async def wait_for_window(url: str, agent: str) -> None:
    """Wait until the agent's window is attached, without running a turn.

    A `note` takes the same road a `submit` does — the window's own stream — and
    is refused with the same "no window" answer, but it queues nothing, runs no
    model and records nothing.  Its acceptance is therefore the readiness probe:
    the window exists, which is the first moment `slife2 run` has finished
    ensuring its servers and building the catalogue.
    """
    deadline = asyncio.get_running_loop().time() + READY_TIMEOUT
    while True:
        try:
            async with Client(url, timeout=30) as window:
                await window.call_tool(
                    NOTE,
                    {"agent": agent, "text": "bench: waiting for the tool set"},
                    timeout=10,
                )
            return
        except ToolError as exc:
            if not control.refused(exc):
                raise
        except Exception:  # noqa: BLE001 - the plugin is part of the same start
            pass
        if asyncio.get_running_loop().time() >= deadline:
            raise NotReady(
                f"no window became available for {agent!r} within {READY_TIMEOUT:.0f}s"
            )
        await asyncio.sleep(2)


async def submit(url: str, agent: str, prompt: str) -> None:
    """Hand the prompt to the agent's window, which is what runs the turn.

    **The window is the conversation** (`slife2.libs.harness.handover`), so a
    caller outside it does not run a turn of its own: it submits the words and
    the window's own client sends them.  That is why there is no second
    `send_message` client here — one agent has one window, and its turn store is
    a file rather than a shared service.

    A refusal means no window is attached yet — `slife2 run` opens the window
    only after it has ensured every server and built the catalogue, so a
    refusal is a stack still becoming ready.  It is the one answer that proves
    nothing was written, so it is retried; a call that did not come back is not
    (`submit_to` says which is which).  The retry *is* the readiness wait:
    there is no second status probe here.
    """
    deadline = asyncio.get_running_loop().time() + READY_TIMEOUT
    while True:
        try:
            async with Client(url, timeout=30) as window:
                handed = await submit_to(window, agent, prompt, "bench")
        except Exception:  # noqa: BLE001 - the plugin is part of the same start
            handed = False
        if handed is True:
            return
        if handed is None:
            raise RuntimeError("the hand-over to the agent's window did not come back")
        if asyncio.get_running_loop().time() >= deadline:
            raise NotReady(
                f"no window became available for {agent!r} within {READY_TIMEOUT:.0f}s"
            )
        await asyncio.sleep(2)


async def wait_for_turn(path: Path, after: int) -> tuple[int, str]:
    """Wait for the window to record its turn, and answer with its text."""
    deadline = asyncio.get_running_loop().time() + TURN_TIMEOUT
    while True:
        found = await asyncio.to_thread(new_turn, path, after)
        if found is not None:
            return found
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                f"no turn was recorded at {path} within {TURN_TIMEOUT:.0f}s"
            )
        await asyncio.sleep(0.5)


async def main() -> int:
    prompt = (
        " ".join(sys.argv[1:]).strip() or os.environ.get("SLIFE2_PROMPT", "").strip()
    )
    if not prompt:
        print("no prompt given", file=sys.stderr)
        return 2

    agent = os.environ.get("SLIFE2_AGENT", "slife2")
    url = os.environ.get("SLIFE2_BENCH_TUI_URL", DEFAULT_TUI_URL)
    path = database_of(agent)
    before = await asyncio.to_thread(last_turn, path)
    try:
        await wait_for_window(url, agent)
    except NotReady as exc:
        print(f"no window became available: {exc}", file=sys.stderr)
        return 3
    # The window is the stack's readiness; the external tool servers are not
    # part of it and are still connecting.  Give them the minute.
    await asyncio.sleep(TOOL_SETTLE)
    try:
        await submit(url, agent, prompt)
    except NotReady as exc:
        print(f"no window became available: {exc}", file=sys.stderr)
        return 3
    turn_id, text = await wait_for_turn(path, before)

    print(text)
    print(f"agent={agent} turn={turn_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
