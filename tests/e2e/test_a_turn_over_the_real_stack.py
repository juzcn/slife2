"""End to end: the whole stack as processes, and a provider that is a stub.

**What is real here, and what is not.**  Every plugin the system needs to run a
turn is a *process*, started by the real launcher, answering on a real port over
Streamable HTTP — the agent, the tool hub, the context store, the embeddings
server and the model backend, each in its own interpreter, spawned exactly as
`slife2` spawns them.  The one thing that is not real is the far end of the
provider hop: an OpenAI-compatible endpoint served by this process, so a turn
costs nothing and needs no key.

That is the seam worth having.  Every other test in this suite reaches one
server with the in-memory transport, or drives a server's own `build_server`
directly, which cannot show the things this exists for: that the launcher brings
the set up in an order that works, that a config written into a data directory
is the one every child reads, that a turn crosses five process boundaries, that
its answer comes back over the progress channel *and* as a result, and that the
conversation it produced is still there for the next turn.

**It is the slow lane.**  Seventeen daemons come up and go down, so it is marked
`e2e` and kept out of the default run (`-m e2e` is how it is asked for); see
README's Tests section.  Nothing here is timing-sensitive beyond the launcher's
own readiness probe, which is what a real start waits on too.
"""

from __future__ import annotations

import json
import re
import socket
import threading
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import pytest_asyncio
from fastmcp import Client

from slife2.libs.host import launcher
from slife2.libs.host.config import load
from slife2.libs.host.paths import DATA_ENV_VAR, data_dir
from slife2.libs.plugin.audience import client_meta
from slife2.libs.turn.tokens import estimate_text_tokens
from slife2.plugins.cabinet.store import DUMP, index_path, store_of
from slife2.tui.client import MCPAgentClient
from tests.fakes import StubEmbedder

pytestmark = pytest.mark.e2e

#: The key the config names.  Not a `${VAR}`: a literal is resolved to itself,
#: and this way the test does not have to own an environment variable that a
#: stray `os.environ` inherited from the developer's shell could shadow.
API_KEY = "e2e-not-a-real-key"

#: The model's answer, and the one string the whole test turns on.  Two chunks,
#: so the streaming path carries more than one event.
ANSWER = "It is 42."

#: The embedding model's name, and the input limit its listing reports.  The
#: limit is not optional: see `_ProviderHandler.do_GET`.
MODEL = "e2e-embed"
INPUT_TOKENS = 8192

#: How much of one tool output a model may be handed, in the config this test
#: writes.  Small on purpose: the window is what the reader is asserted
#: against, and a window near the default would be a window no test can see.
WINDOW = 300


def _free_port() -> int:
    """A port nothing is on, for a config that has to name one.

    The config cannot say `port: 0` (`slife2.libs.host.config._server` refuses
    it, for the reason a port is how a launcher finds a server again), so a test
    that must not collide with a developer's running stack picks them itself.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _ProviderHandler(BaseHTTPRequestHandler):
    """Two routes of an OpenAI-compatible API, answered in a thread.

    **A thread and not this process's event loop, and that is not tidiness.**
    The launcher's `ensure_all` is a generator consumed *synchronously*: each
    `next` blocks on a readiness probe while a child process starts, and a child
    that is starting needs this endpoint to answer (the embeddings server probes
    the model's width before it can serve).  A stub on the test's own loop is a
    stub that cannot answer while the text that started it is blocked, and the
    first thing to notice is a `tools/call` timeout in the context daemon.
    """

    provider: ClassVar[StubProvider]

    def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        if self.path.endswith("/chat/completions"):
            self.provider.calls.append(list(body.get("messages") or []))
            self._answer(body=_sse(self.provider.answer), kind="text/event-stream")
        elif self.path.endswith("/embeddings"):
            count = len(body.get("input") or [])
            self._answer(body=_json(_embeddings(count)))
        else:
            self._answer(body=_json({"error": f"no {self.path}"}), status=404)

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        """The model listing, and nothing else.

        Not decoration: the embeddings server *refuses* a model whose endpoint
        reports no input limit rather than guessing one (DESIGN.md §10.1), and
        the limit is one field of this listing.  A stub that 404s here makes the
        context daemon fail at startup — which is exactly what it did, and the
        refusal named this route.
        """
        if self.path.endswith("/models"):
            self._answer(
                body=_json(
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": MODEL,
                                "object": "model",
                                "owned_by": "e2e",
                                "max_input_tokens": INPUT_TOKENS,
                            }
                        ],
                    }
                )
            )
            return
        self._answer(body=_json({"error": f"no {self.path}"}), status=404)

    def _answer(
        self, *, body: bytes, status: int = 200, kind: str = "application/json"
    ):
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silence: a stub that writes an access log into the test's output."""


class StubProvider:
    """An OpenAI-compatible endpoint, in this process.

    Two routes, because two plugins reach a provider and neither is the model:
    `/chat/completions` streams the answer the agent loop reads, and
    `/embeddings` answers the vectors the context store's index is built from.
    Both are the *wire* shapes the real SDKs parse — a stub that answered a
    simplified shape would prove nothing about the server in between.
    """

    def __init__(self) -> None:
        self.answer = ANSWER
        #: Every chat call it answered, in order, as the messages it was sent —
        #: which is where "the history reached the model" is read from.
        self.calls: list[list[dict]] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def chats(self) -> int:
        return len(self.calls)

    @property
    def last_messages(self) -> list[dict]:
        return self.calls[-1] if self.calls else []

    @property
    def base_url(self) -> str:
        server = self._httpd
        assert server is not None, "not started"
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    def start(self) -> None:
        handler = type("_Bound", (_ProviderHandler,), {"provider": self})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def _json(body: dict) -> bytes:
    return json.dumps(body).encode()


def _sse(text: str) -> bytes:
    """`text` as the two streaming chunks a chat-completions client reads.

    The finish chunk is not decoration: it is what `finish_reason: "stop"`
    reaches `StreamChatResult.stop_reason` through, and a stream that ended
    without it would test a path no provider produces.
    """

    def chunk(delta: dict, finish: str | None) -> bytes:
        body = {
            "id": "e2e",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "e2e-stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(body)}\n\n".encode()

    return (
        chunk({"role": "assistant", "content": text}, None)
        + chunk({}, "stop")
        + b"data: [DONE]\n\n"
    )


def _embeddings(count: int) -> dict:
    return {
        "object": "list",
        "model": "e2e-stub",
        "data": [
            {"object": "embedding", "index": index, "embedding": [0.1] * 8}
            for index in range(count)
        ],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


def _config_text(provider_url: str, ports: dict[str, int]) -> str:
    servers = "\n".join(f"  {name}: {{port: {port}}}" for name, port in ports.items())
    return f"""\
# Written by the e2e test: no sections a turn does not need, and every address
# on a port this process picked.
servers:
{servers}

# **Deliberately not the default.**  One number bounds both sides of the
# arrangement — the harness shortening an output too large for a conversation,
# and a tool of ours handing text back — and a test that read it from the
# default would prove nothing about the config file every child reads.
tool_result:
  tokens: {WINDOW}

providers:
  stub:
    api: openai-completions
    base_url: {provider_url}
    api_key: {API_KEY}
    models:
      - model: small
        name: Stub
        context_window: 8000
        max_tokens: 1000

embeddings:
  providers:
    stub:
      base_url: {provider_url}
      model: {MODEL}
      api_key: {API_KEY}

default: stub/small
"""


@pytest_asyncio.fixture(loop_scope="function")
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict]:
    """The whole stack, up: a stub provider, a config, every daemon, and a client.

    Torn down in the reverse order it came up, and *stopped* rather than left to
    the last-client rule: these daemons belong to a data directory under
    `tmp_path`, and a test that leaked them would leave seventeen processes
    holding ports for the rest of the session.
    """
    provider = StubProvider()
    provider.start()
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    ports = {
        name: _free_port()
        for name in (
            "openai-completions",
            "embeddings",
            "agent",
            "context",
            "toolhub",
            "skills-server",
            "cli-server",
            "mcp-tools",
            "restapi-tools",
            "wechat",
            "cabinet",
            "media",
            "file-tunnel",
            "tui",
            "agent-task",
            "schedule",
            "a2a",
        )
    }
    (tmp_path / "slife2.yaml").write_text(
        _config_text(provider.base_url, ports), encoding="utf-8"
    )
    config = load()
    try:
        outcomes = list(launcher.ensure_all(config))
        failed = [one for one in outcomes if not one.ok]
        assert not failed, [
            (one.spec.name, one.status.value, one.detail) for one in failed
        ]
        yield {"config": config, "provider": provider, "data": tmp_path}
    finally:
        provider.stop()
        # **Consumed, not merely called.**  `stop` is a generator for
        # `ensure_all`'s reason — "nothing here runs until the first `next`, so
        # a caller that drops the iterator has started nothing at all" — and
        # dropping it stops nothing at all either: this fixture leaked
        # seventeen daemons on every run while both tests went on passing, and
        # the only trace was eighteen python processes per run holding ports
        # whose data directory pytest had already deleted.  The assertion is the
        # half that makes the next leak a failure instead of a mess.
        stopped = list(launcher.stop(config))
        assert all(one.status is launcher.Status.STOPPED for one in stopped), [
            (one.spec.name, one.status.value, one.detail) for one in stopped
        ]


def _files(data: Path) -> list[str]:
    return sorted(
        str(path.relative_to(data)) for path in data.rglob("*") if path.is_file()
    )


@pytest.mark.asyncio
async def test_a_turn_runs_across_the_whole_stack_and_is_remembered(stack) -> None:
    """One message in, one answer out, and a conversation left behind.

    The assertions are the things only a real stack can show: the answer came
    back over the progress channel (not just as a result), the request that
    produced it carried the prompt, the conversation is readable through a
    *second* connection, and it is on disk, written by the context process.

    **Two provider calls and not one**, which is worth knowing before reading
    the count: the context store's discriminator is a model call of its own
    (`slife2-context` decides what a turn runs on before it runs), and it comes
    through the same provider as the turn.
    """
    config = stack["config"]
    events: list[str] = []
    client = MCPAgentClient(config.agent.server.url, agent="e2e")
    await client.connect()
    try:
        answer = await client.run_turn(
            "what is 6*7?", lambda event: events.append(type(event).__name__)
        )
    finally:
        await client.close()

    assert answer == ANSWER
    assert events[0] == "ContextChosen", events
    assert "TextDelta" in events and events[-1] == "TurnFinished"
    assert stack["provider"].chats == 2, stack["provider"].calls
    assert stack["provider"].last_messages[-1]["content"] == "what is 6*7?"

    # The conversation outlives the client that ran it: the transcript is the
    # server's, and a *second* connection reads the turn back.
    again = MCPAgentClient(config.agent.server.url, agent="e2e")
    await again.connect()
    try:
        turns = await again.transcript()
    finally:
        await again.close()
    assert len(turns) == 1, turns
    # A turn is stored as the message list the model saw, so both halves are in
    # there and neither is a column of its own (`TurnRecord.to_wire`).
    assert "what is 6*7?" in json.dumps(turns[0]["messages"])
    assert ANSWER in json.dumps(turns[0]["messages"])

    # And it is on disk, written by the context plugin rather than by anybody in
    # this process.
    store = data_dir() / "agents" / "e2e" / "turns.db"
    assert store.is_file(), _files(data_dir())


@pytest.mark.asyncio
async def test_a_second_turn_runs_on_the_first(stack) -> None:
    """The history reaches the model, over a hop that has to carry it.

    A conversation that is only *stored* is a transcript; one that is *run* is
    the feature.  The stub answers whatever it is asked, so what is asserted is
    the request: the second call carries both messages, which is what the
    context process decided to rebuild from.
    """
    config = stack["config"]
    client = MCPAgentClient(config.agent.server.url, agent="e2e-history")
    await client.connect()
    try:
        await client.run_turn("remember the number 41", lambda _event: None)
        await client.run_turn("what number?", lambda _event: None)
    finally:
        await client.close()

    assert stack["provider"].last_messages[-1]["role"] == "user"
    assert stack["provider"].last_messages[-1]["content"] == "what number?"
    assert any(
        "remember the number 41" in json.dumps(message)
        for message in stack["provider"].last_messages[:-1]
    ), stack["provider"].last_messages


async def _save_dump(agent: str, call: str, body: str) -> None:
    """A dump, put in the cabinet the way the harness will put one there.

    The write is the half of the arrangement that is not built yet — the harness
    owns it, and this test reaching the plugin's own store is the closest thing
    to it that exists.  Everything after this line is the real thing: a second
    process reads the row, over a socket.
    """
    store = store_of(index_path(agent))
    embedder = StubEmbedder()
    await store.sync(embedder)
    await store.write(DUMP, call, body=body, embedder=embedder)


async def _read(client: Client, agent: str, **arguments: Any) -> str:
    """One `dump_read`, on behalf of a conversation, as the model reads it."""
    result = await client.call_tool("dump_read", arguments, meta=client_meta(agent))
    return "".join(getattr(block, "text", "") for block in result.content or [])


@pytest.mark.asyncio
async def test_a_dump_is_read_back_over_the_real_stack_within_its_window(stack) -> None:
    """**The rare case, forced** — and the loop it must not close.

    A turn in real use seldom returns an output past the window, so nothing here
    waits for one to happen: the dump is put where the harness will put it, and
    everything after that is real. A second process owns the corpus, the call
    crosses a socket, and the window is the number in the config file every
    child read.

    What is under test is a loop that must not exist. The harness shortens an
    output too large for a conversation; a reader that could hand back more than
    the window would have its *own* answer shortened next, which is this
    system's machinery feeding itself. So every answer is measured here, from
    the model's side of the hop, with the same estimator the harness spends —
    including the answers built out of what the caller asked for, which is the
    path where a window is easiest to lose.
    """
    lines = 4000
    body = chr(10).join(
        f"line {number} of a tool output" for number in range(1, lines + 1)
    )
    await _save_dump("e2e", "call_e2e", body)
    config = stack["config"]
    budget = config.tool_result.tokens
    assert budget == WINDOW, "the window came from the config file"

    async with Client(config.server("cabinet").url) as client:
        first = await _read(client, "e2e", call="call_e2e")
        following = re.search(r"offset (\d+)", first)
        assert following is not None, first
        offset = int(following.group(1))
        second = await _read(client, "e2e", call="call_e2e", offset=offset)
        found = await _read(client, "e2e", call="call_e2e", pattern="line 39")
        huge = await _read(client, "e2e", call="call_e2e", pattern="z" * 100_000)

    for answer in (first, second, found, huge):
        assert estimate_text_tokens(answer) <= budget, answer[:300]

    # A window, and the address of the next one: the numbers in it are lines of
    # the output rather than of the page it was shown.
    assert "line 1 of a tool output" in first
    assert f"of {lines}" in first and "the next window starts at offset" in first
    assert f"line {offset} of a tool output" in second
    # Finding is the same reader, and it counts the whole output rather than the
    # window it fitted.
    assert "line 39 of a tool output" in found and "lines containing" in found
    # **The path where a window is easiest to lose**: an argument that is not a
    # pattern at all is answered with rather than echoed.
    assert "do not fit" in huge, "a 100,000-character pattern is clipped, not quoted"

    # And the whole of it is a file, which is what the marker will promise.
    dumps = data_dir() / "agents" / "e2e" / "cabinet" / "dumps"
    assert dumps.is_dir() and list(dumps.iterdir()), _files(data_dir())
    assert "cabinet" in first, first[:200]
