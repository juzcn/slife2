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

**It is the slow lane.**  Eighteen daemons come up and go down, so it is marked
`e2e` and kept out of the default run (`-m e2e` is how it is asked for); see
README's Tests section.  Nothing here is timing-sensitive beyond the launcher's
own readiness probe, which is what a real start waits on too.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import pytest_asyncio
from fastmcp import Client

from slife2.libs.harness.markers import strip as strip_marker
from slife2.libs.host import launcher
from slife2.libs.host.config import LOCAL_SERVERS, load
from slife2.libs.host.paths import DATA_ENV_VAR, data_dir
from slife2.libs.plugin.audience import client_meta
from slife2.libs.turn.context import TURN_PROMPT_PREFIX
from slife2.libs.turn.tokens import estimate_text_tokens
from slife2.plugins.cabinet.store import DUMP, index_path, store_of
from slife2.tui.client import MCPAgentClient
from tests.fakes import StubEmbedder, a_home

pytestmark = pytest.mark.e2e

#: The key the config names.  Not a `${VAR}`: a literal is resolved to itself,
#: and this way the test does not have to own an environment variable that a
#: stray `os.environ` inherited from the developer's shell could shadow.
API_KEY = "e2e-not-a-real-key"

#: The model's answer, and the one string the whole test turns on.  Two chunks,
#: so the streaming path carries more than one event.
ANSWER = "It is 42."

#: What the tool-call test's model asks a *tool* to write, and the string its
#: assertions read back off disk.  Its own constant because it is a different
#: conversation's fact: the note is written by the cabinet process, into the
#: agent's cabinet, and reading it back is how this side of the stack proves the
#: plugin ran.
NOTE_BODY = "the note the model asked a tool in another process to write"

#: The embedding model's name, and the input limit its listing reports.  The
#: limit is not optional: see `_ProviderHandler.do_GET`.
MODEL = "e2e-embed"
INPUT_TOKENS = 8192

#: How much of one tool output a model may be handed, in the config this test
#: writes.  Small on purpose: the window is what the reader is asserted
#: against, and a window near the default would be a window no test can see.
WINDOW = 300


#: Where this worker's ports come from.  **A band per xdist worker, because the
#: obvious allocator is racy.**  Binding port 0, reading the number and closing
#: it hands back a port that belongs to nobody until somebody binds it, so two
#: workers drawing at the same moment can be given the same one — measured on
#: this machine at about one run in fifty with three workers.  The daemon that
#: loses the race cannot bind, and everything that depends on it is then
#: reported "not attempted: … did not come up", which names neither a port nor a
#: collision.  Bands cannot overlap, so the race is gone rather than unlikely.
_PORT_BASE = 20_000
_BAND_PORTS = 250


def _worker_band() -> int:
    """Which xdist worker this is.  A run with no xdist is band 0."""
    name = os.environ.get("PYTEST_XDIST_WORKER", "")
    return int(name[2:]) if name[:2] == "gw" and name[2:].isdigit() else 0


def _free_ports(names: Iterable[str]) -> dict[str, int]:
    """One port per name, all of them from this worker's own band.

    The config cannot say `port: 0` (`slife2.libs.host.config._server` refuses
    it, for the reason a port is how a launcher finds a server again), so a test
    that must not collide with a developer's running stack picks them itself.

    **Every socket is held until the whole set has been chosen**, so nothing
    else — another worker, or the next port this loop asks for — can be handed
    one of them.  Releasing each as it is found would leave the same race one
    call smaller.
    """
    start = _PORT_BASE + (_worker_band() % 32) * _BAND_PORTS
    end = start + _BAND_PORTS
    chosen: dict[str, int] = {}
    held: list[socket.socket] = []
    port = start
    try:
        for name in names:
            while True:
                if port >= end:
                    raise RuntimeError(
                        f"no free port left in this worker's band "
                        f"({start}-{end}); something else here is holding them"
                    )
                probe = socket.socket()
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    probe.close()
                    port += 1
                    continue
                held.append(probe)
                chosen[name] = port
                port += 1
                break
    finally:
        for probe in held:
            probe.close()
    return chosen


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
            self.provider.record(body)
            self._answer(body=self.provider.next_answer(), kind="text/event-stream")
        elif self.path.endswith("/embeddings"):
            count = len(body.get("input") or [])
            self._answer(body=_json(_embeddings(count)))
        else:
            self._answer(body=_json({"error": f"no {self.path}"}), status=404)

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        """The model listing, and nothing else.

        Not decoration: the embeddings server *refuses* a model whose endpoint
        reports no input limit rather than guessing one (SDD Appendix C), and
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


@dataclass
class ToolAsk:
    """One tool call, as the model writes it: an id, a name, and JSON arguments.

    The three fields a provider's stream carries, and carried here the way a
    real one sends them — the index, id and name first, the arguments after —
    because that split is exactly what the model server's accumulator has to
    fold back together.
    """

    id: str
    name: str
    arguments: str


@dataclass
class ChatAnswer:
    """What the stub answers one chat call with: text, or a request for a tool.

    Those are the two ways a model call ends on this wire, and a script of these
    is how a test states the shape of a conversation.
    """

    text: str = ""
    #: A tool call instead of an answer.  A call and an answer are different
    #: endings rather than different content, so this is a choice between them
    #: and not a field beside `text`.
    tool: ToolAsk | None = None

    def sse(self) -> bytes:
        if self.tool is not None:
            return _sse_tool_call(self.tool)
        return _sse(self.text)


class StubProvider:
    """An OpenAI-compatible endpoint, in this process.

    Two routes, because two plugins reach a provider and neither is the model:
    `/chat/completions` streams the answer the agent loop reads, and
    `/embeddings` answers the vectors the context store's index is built from.
    Both are the *wire* shapes the real SDKs parse — a stub that answered a
    simplified shape would prove nothing about the server in between.

    **It answers a script rather than one answer**, because one turn is more
    than one model call: the context store's discriminator is a call of its own
    before the turn runs, and a turn whose model asks for a tool has another
    after the tool has answered.  A test that scripts the sequence is stating
    what it expects to happen; a stub that sends the same answer to every call
    cannot tell the third call from the first, which is the whole of what a
    tool-call test is about.  The last entry repeats, so the single-answer
    script the other tests in this file write is this mechanism's one-entry
    case rather than a second one beside it.
    """

    def __init__(self, *script: ChatAnswer) -> None:
        self.script: list[ChatAnswer] = list(script) or [ChatAnswer(text=ANSWER)]
        #: Every chat call it answered, in order, as the messages it was sent —
        #: which is where "the history reached the model" is read from.
        self.calls: list[list[dict]] = []
        #: The tool names each of those calls was *offered*, in the same order.
        #: Kept apart from `calls` because a call naming a tool the model was
        #: never offered is a call the hub would route all the same, so a test
        #: meaning "the model chose from what it was handed" has to read this
        #: rather than assume it.
        self.tool_names: list[list[str]] = []
        self._served = 0
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def record(self, body: dict) -> None:
        """One chat request, as it arrived."""
        self.calls.append(list(body.get("messages") or []))
        self.tool_names.append(
            [
                str((tool.get("function") or {}).get("name") or "")
                for tool in body.get("tools") or []
            ]
        )

    def next_answer(self) -> bytes:
        """The next entry of the script — and the last one from then on."""
        answer = self.script[min(self._served, len(self.script) - 1)]
        self._served += 1
        return answer.sse()

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


def _chunk(delta: dict, finish: str | None) -> bytes:
    """One `chat.completion.chunk` frame, as the SSE event carrying it.

    The finish chunk is not decoration: it is what `finish_reason` reaches
    `StreamChatResult.stop_reason` through, and a stream that ended without one
    would test a path no provider produces.
    """
    body = {
        "id": "e2e",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "e2e-stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n".encode()


#: How a stream ends on this wire, after the finish chunk.
_DONE = b"data: [DONE]\n\n"


def _sse(text: str) -> bytes:
    """`text` as the two streaming chunks a chat-completions client reads."""
    return (
        _chunk({"role": "assistant", "content": text}, None)
        + _chunk({}, "stop")
        + _DONE
    )


def _sse_tool_call(ask: ToolAsk) -> bytes:
    """One tool call, as the chunks a real stream sends it in.

    **Two chunks and not one**, because a provider is free to split a call
    anywhere and the usual place is between the name and the arguments.  A stub
    sending the call whole would leave the folding in the model server untested,
    and the folding is where a name that arrives in pieces is either
    reassembled or mangled into the name of a different tool.
    """
    return (
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": ask.id,
                        "type": "function",
                        "function": {"name": ask.name, "arguments": ""},
                    }
                ]
            },
            None,
        )
        + _chunk(
            {"tool_calls": [{"index": 0, "function": {"arguments": ask.arguments}}]},
            "tool_calls",
        )
        + _DONE
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

# `note_save` is the tool the turn test asks the model for, so it is in the
# list the model is offered.  Every other cabinet tool is on demand, which is
# what a plugin's tools are now; the first eight are what the system works by.
tool_load:
  autoload:
    - tool_search
    - tool_list
    - func_tool_load
    - _func_tool_unload
    - _check_new_input
    - skill_use
    - check_async
    - cancel_async
    - note_save

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
async def stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_dialer: Any
) -> AsyncIterator[dict]:
    """The whole stack, up: a stub provider, a config, every daemon, and a client.

    Torn down in the reverse order it came up, and *stopped* rather than left to
    the last-client rule: these daemons belong to a data directory under
    `tmp_path`, and a test that leaked them would leave eighteen processes
    holding ports for the rest of the session.
    """
    # **The launcher's own probe is a real dial, and the guard has to be told.**
    # `no_live_stack` replaces `open_server` in every module that dials, the
    # launcher among them, so a test cannot reach this machine's running stack.
    # `_wait_ready` probes through exactly that name — and every server it is
    # waiting for is one *this fixture just started*, on a port this test
    # picked, so the danger the guard exists for is not here.  Without this,
    # every probe of a daemon that is already serving is refused with the
    # guard's `RuntimeError`, `probe` reports it as "the probe itself failed",
    # and the stack never finishes starting: the lane's watchdog kills the
    # worker and the lane reports a crash that names no server at all.
    real_dialer("slife2.libs.host.launcher")
    provider = StubProvider()
    provider.start()
    monkeypatch.setenv(DATA_ENV_VAR, str(a_home(tmp_path)))
    # **Derived from `LOCAL_SERVERS`, not written out.**  A hand-kept list is a
    # list that goes stale, and this one had: `subagent` was missing, so it
    # started on the built-in default port instead of a picked one — and since
    # these tests run concurrently, every worker's stack raced for that one
    # address.  The first to bind it won; the others found it answering, called
    # it "running (external)", and their teardown asserted on a server they had
    # never started.  The lane was red on every run and the assertion below said
    # nothing about ports.  Every plugin this config names now gets a port
    # nobody else is on, which is what `_config_text`'s own comment promises.
    ports = _free_ports((*LOCAL_SERVERS, "openai-completions"))
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
        # eighteen daemons on every run while both tests went on passing, and
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


def _prompt_words(content: str) -> str:
    """A live user message as the person wrote it, the turn's prompt taken off.

    `[TURN-PROMPT:{…}]` — the clock and the status bar's figure — is added to
    the message after the recall has chosen the conversation and removed before
    the turn is saved.  The model is its only reader, so a request read back
    from the stub carries it while the row, the transcript and every later
    rebuild do not.  What the two agree on is the words underneath.
    """
    return strip_marker(content, TURN_PROMPT_PREFIX)


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

    assert answer.text == ANSWER
    assert answer.injected is False
    assert events[0] == "ContextChosen", events
    assert "TextDelta" in events and events[-1] == "TurnFinished"
    assert stack["provider"].chats == 2, stack["provider"].calls
    # The model is the marker's only reader: the request carries it, across
    # every process between the agent and the stub.
    sent = stack["provider"].last_messages[-1]["content"]
    assert sent.startswith(TURN_PROMPT_PREFIX), sent
    assert _prompt_words(sent) == "what is 6*7?"

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
    # ...and the prompt marker stayed out of the row: it is text for the turn
    # that ran, not a fact about it, so search and recall never see it.
    assert TURN_PROMPT_PREFIX not in json.dumps(turns[0]["messages"])

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
    assert (
        _prompt_words(stack["provider"].last_messages[-1]["content"]) == "what number?"
    )
    assert any(
        "remember the number 41" in json.dumps(message)
        for message in stack["provider"].last_messages[:-1]
    ), stack["provider"].last_messages


@pytest.mark.asyncio
async def test_a_turn_calls_a_tool_in_another_process_and_reads_its_answer(
    stack,
) -> None:
    """**The round trip the whole arrangement exists for**, over every boundary.

    The model asks for a tool, the loop dispatches it, the hub routes it by
    name, a *different process* runs it, and what that process answered comes
    back to the model as a message — which is the one path nothing else in this
    suite can show.  Every other tool test stops at one edge of it: the loop
    tests stop at the dispatch, the hub tests at a stubbed upstream, and the
    plugin tests at the plugin's own server.  Only a real stack has a model on
    one side and a plugin process on the other with the hub and the catalogue in
    between, and the pieces in between are exactly what a rearrangement breaks.

    What keeps it from being a demonstration is that both ends are read where
    this process cannot have written them: the note is a file the cabinet's own
    process put on disk, and the third request the provider received carries the
    tool's sentence rather than anything this test composed.

    **Three calls, and the first is not the turn's.**  The context store's
    discriminator decides what a turn runs on before it runs, and it is a model
    call of its own; the second is the turn's first iteration, which asks for the
    tool; the third is the iteration after the tool has answered.
    """
    provider = stack["provider"]
    provider.script = [
        # The discriminator.  `{}` is "nothing changes" — what the context
        # plugin's own tests answer with, and the one answer that leaves the
        # conversation this test is about alone.
        ChatAnswer(text="{}"),
        ChatAnswer(
            tool=ToolAsk(
                id="call_e2e_note",
                name="note_save",
                arguments=json.dumps(
                    {"subject": "e2e verification", "content": NOTE_BODY}
                ),
            )
        ),
        ChatAnswer(text="Noted."),
    ]
    config = stack["config"]
    client = MCPAgentClient(config.agent.server.url, agent="e2e-tool")
    await client.connect()
    try:
        answer = await client.run_turn("make a note of that", lambda _event: None)
    finally:
        await client.close()

    assert answer.text == "Noted.", provider.calls
    # Step by step, so a turn that took a different number of calls says which
    # one it took rather than failing on a total.
    assert provider.chats == 3, [len(call) for call in provider.calls]

    # The model was *offered* the tool it called, by the bare name our own
    # plugins use — so the catalogue reached the model and the name the model
    # wrote is the name the hub routes by.  Read from the turn's own call and
    # not the discriminator's, which is a completion and carries no tools.
    assert "note_save" in provider.tool_names[1], provider.tool_names[1]

    # And the tool's answer came back into the conversation: the third request
    # carries a `tool` message the loop built out of what the cabinet process
    # said.  The loop pairs it with the call by id, which is what the model
    # needs to see which answer belongs to which request.
    result = [message for message in provider.calls[2] if message.get("role") == "tool"]
    assert len(result) == 1, provider.calls[2]
    assert result[0]["tool_call_id"] == "call_e2e_note", result[0]
    # The cabinet's own JSON, rendered by `as_tool_text` on its way into the
    # request: the row it wrote, not a sentence about it.
    assert json.loads(result[0]["content"])["key"] == "e2e verification", result[0]

    # **Written by the cabinet's process**, which is the half this one cannot
    # fake: nothing here holds that store.  The file is the mirror of the note
    # the model asked for, and its contents are the model's own words.
    notes = data_dir() / "agents" / "e2e-tool" / "cabinet" / "notes"
    written = sorted(notes.glob("*.md")) if notes.is_dir() else []
    assert len(written) == 1, _files(data_dir())
    assert NOTE_BODY in written[0].read_text(encoding="utf-8")


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


async def _read(client: Client, agent: str, **arguments: Any) -> dict[str, Any]:
    """One `dump_read`, on behalf of a conversation, as the model reads it.

    The answer is an object (`cabinet.server`), and `data` is how a tool's JSON
    reaches this side of the hop.
    """
    result = await client.call_tool("dump_read", arguments, meta=client_meta(agent))
    answer = getattr(result, "data", None)
    assert isinstance(answer, dict), answer
    return answer


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
        offset = first.get("next")
        assert isinstance(offset, int), first
        second = await _read(client, "e2e", call="call_e2e", offset=offset)
        found = await _read(client, "e2e", call="call_e2e", pattern="line 39")
        huge = await _read(client, "e2e", call="call_e2e", pattern="z" * 100_000)

    for answer in (first, second, found, huge):
        assert estimate_text_tokens(json.dumps(answer)) <= budget, str(answer)[:300]

    # A window, and the address of the next one: the numbers in it are lines of
    # the output rather than of the page it was shown.
    assert "line 1 of a tool output" in first["text"]
    assert first["total"] >= lines, "the whole output is counted, not the window"
    assert f"line {offset} of a tool output" in second["text"]
    # Finding is the same reader, and it counts the whole output rather than the
    # window it fitted.
    assert "line 39 of a tool output" in found["text"]
    assert found["matched"], "and says how many lines the pattern is on"
    # **The path where a window is easiest to lose**: an argument that is not a
    # pattern at all is answered with rather than echoed.  Nothing the caller
    # wrote is quoted back any more, so a 100,000-character pattern is simply a
    # pattern this output does not contain.
    assert huge["matched"] == 0

    # And the whole of it is a file, which is what the marker will promise.
    dumps = data_dir() / "agents" / "e2e" / "cabinet" / "dumps"
    assert dumps.is_dir() and list(dumps.iterdir()), _files(data_dir())
    # The file is the cabinet's; the answer is the window's.  A model cannot
    # open a path, and `dump_read` is the only reader it has.
    assert "where" not in first, first
