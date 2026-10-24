from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from fastmcp.exceptions import ToolError

from slife2.libs.harness.handover import submit_to
from slife2.libs.host.config import load_cached
from slife2.libs.host.paths import agent_dir
from slife2.libs.plugin.protocol import close_server, open_server, tool_payload
from slife2.libs.store.db import message_text
from slife2.plugins.tui import server as control
from slife2.plugins.tui.server import NOTE

TURN_TIMEOUT = float(os.environ.get("SLIFE2_BENCH_TURN_TIMEOUT", "3600"))
#: How long the window may take to open.  `slife2 run` opens it only after
#: every server is ensured and the catalogue is built; the launcher's own
#: budgets are `ready.plugin_start` (300 s) and `work.catalogue` (360 s), and
#: this gate has to outlast both rather than a warm start's two minutes.
READY_TIMEOUT = float(os.environ.get("SLIFE2_BENCH_READY_TIMEOUT", "900"))
#: How long the external `mcp:`/`rest-api:` sources may take to reach `ready`.
#: Unlike a person's home — where the controllers stay warm and the sources are
#: connected from the previous run — a task container starts every one of them
#: from cold, so the bench blocks on their real states instead of guessing a
#: settle.  See `wait_for_tools`.
TOOL_READY_TIMEOUT = float(os.environ.get("SLIFE2_BENCH_TOOL_READY_TIMEOUT", "900"))


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
        window = None
        try:
            # **The caller's home has to travel with the call.**  A shared stack
            # serves many homes; a plain MCP client says nothing, and the server
            # would answer for its own home.  `open_server` is slife2's client —
            # it puts this process's `SLIFE2_DATA_DIR` on every call.
            window = await open_server(url, fallback_tool=NOTE, timeout=30)
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
        finally:
            if window is not None:
                await close_server(window)
        if asyncio.get_running_loop().time() >= deadline:
            raise NotReady(
                f"no window became available for {agent!r} within {READY_TIMEOUT:.0f}s"
            )
        await asyncio.sleep(2)


async def wait_for_tools(url: str, expected: dict[str, str], deadline: float) -> None:
    """Block until every configured external source says `ready`.

    **The set is read off the config, not off the answer.**  `servers` reports
    the sources a plugin has *declared*; a plugin still installing them (a cold
    container's `mcp-tools`, starting twenty `npx`/`uvx` servers) has not
    declared at all, and its entries are absent — which "every row is ready"
    would read as success.  So each enabled entry under `mcp:`/`rest-api:` is
    awaited by name: absent is `missing`, and `connecting`/`failed`/`idle` are
    all not `ready`.  `off` is not a wait — a switched-off entry is a decision.

    The call itself refreshes the declarations (the hub's `servers` does), so a
    poll is a fresh answer and a source that became ready between two polls is
    seen.  A source that never becomes ready fails the trial at the deadline:
    no prompt is sent against a tool set that is still changing.
    """
    loop = asyncio.get_running_loop()
    while True:
        hub = None
        try:
            # The toolhub answers for the caller's home, so the client must say
            # which one — see `wait_for_window`.
            hub = await open_server(
                url, fallback_tool="servers", timeout=TOOL_READY_TIMEOUT
            )
            payload = tool_payload(
                await hub.call_tool("servers", {}, timeout=TOOL_READY_TIMEOUT)
            )
        except Exception as exc:  # noqa: BLE001 - the hub is part of the same start
            payload = {}
            print(f"the toolhub is not answering yet: {exc}", file=sys.stderr)
        finally:
            if hub is not None:
                await close_server(hub)
        rows = {
            str(row.get("name")): row
            for row in payload.get("servers") or []
            if isinstance(row, dict)
        }
        waiting = {
            name: str((rows.get(name) or {}).get("state") or "missing")
            for name in expected
            if str((rows.get(name) or {}).get("state")) != "ready"
        }
        if not waiting:
            return
        if loop.time() >= deadline:
            raise NotReady(
                "external tool servers not ready: "
                + ", ".join(
                    f"{name}={state}" for name, state in sorted(waiting.items())
                )
            )
        await asyncio.sleep(5)


def endpoints() -> tuple[str, str]:
    """The TUI and toolhub addresses the stack is actually serving on.

    **The config's, not this file's.**  Both ports are configurable
    (`servers.tui`, `servers.toolhub`) and `wsl_port_offset` shifts every one of
    them on a mirrored network — a default compiled in here would dial an
    address nothing bound, and the failure would read as a stack that never
    became ready.  The environment variables stay as an override for a caller
    that mapped the ports somewhere else, and are read first because that is
    what an override is.
    """
    config = load_cached()
    return (
        os.environ.get("SLIFE2_BENCH_TUI_URL") or config.server("tui").url,
        os.environ.get("SLIFE2_BENCH_TOOLHUB_URL") or config.server("toolhub").url,
    )


def external_sources() -> dict[str, str]:
    """The enabled `mcp:` and `rest-api:` entries, by name, with their kind."""
    config = load_cached()
    entries = {**config.mcp, **config.rest_apis}
    return {
        name: settings.kind for name, settings in entries.items() if settings.enabled
    }


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
        window = None
        try:
            window = await open_server(url, fallback_tool="submit", timeout=30)
            handed = await submit_to(window, agent, prompt, "bench")
        except Exception:  # noqa: BLE001 - the plugin is part of the same start
            handed = False
        finally:
            if window is not None:
                await close_server(window)
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
    url, hub_url = endpoints()
    path = database_of(agent)
    before = await asyncio.to_thread(last_turn, path)
    try:
        await wait_for_window(url, agent)
        # **The bench is not a warm home.**  A person's stack has the external
        # sources connected from the previous run; a task container starts them
        # all from cold.  Block on their real state so the first turn sees the
        # tool set the config names, rather than racing the npx installs.
        tools_deadline = asyncio.get_running_loop().time() + TOOL_READY_TIMEOUT
        await wait_for_tools(hub_url, external_sources(), tools_deadline)
    except NotReady as exc:
        print(f"the stack did not become ready: {exc}", file=sys.stderr)
        return 3
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
