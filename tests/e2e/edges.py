"""The far ends the end-to-end stack expects, and a terminal to hand things to.

**Every double in this module stands where a network used to be**, and each is
the narrowest one the real stack can be driven against:

* :class:`StubProvider` is an OpenAI-compatible HTTP endpoint — the one thing
  both lanes fake, because a model costs money and a key.
* :class:`IlinkStub` is WeChat's hosted bridge, served over HTTP in a thread.
  The wechat plugin is a *process* in the e2e stack, so its `Http` seam is out
  of this process's reach; what reaches it is the address its saved login
  carries, and this is what answers there.
* :class:`MqttBroker` is a broker on a TCP port, speaking enough MQTT 5 for the
  mesh.  The alternative was requiring `mosquitto` to be installed on every CI
  runner — a native package on three operating systems — for a test whose
  subject is slife2's traffic, not the broker's.
* :class:`Window` is the terminal: it attaches to `slife2-tui` the way the app
  does, and runs every hand-over as a turn in the conversation it is showing.

:class:`Peer` is not a double: it is the SDK's own requester, in a thread so it
can have the selector loop aiomqtt needs on Windows.  What it stands in for is
not a protocol but a *place* — somewhere outside the stack that speaks it.

**Why they live here and not in `tests/fakes.py`.**  That module is the fast
lane's shared vocabulary and every test in the suite may import it.  These are
the e2e lane's edges: they bind ports and start threads, and a fast-lane test
that reached for one would have left its lane.

**The provider is a thread, and so is each stub.**  The e2e stack starts
through `launcher.ensure_all`, whose generator is consumed *synchronously*:
children starting need the provider to answer while the test's own loop is
blocked in that text, which is why this one cannot live on the test's loop.
The other two are on threads for the plainer reason that a daemon process dials
them and this process may be busy elsewhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlsplit

from a2a_over_mqtt import TERMINAL_KINDS, MqttConfig, Requester, TopicSpace
from fastmcp import Client

from slife2.libs.plugin.vocabulary import Handover
from slife2.tui.client import MCPAgentClient, MCPWindowClient, TurnReply


def _thread_loop() -> asyncio.AbstractEventLoop:
    """An event loop for a thread that needs `add_reader`, on every platform.

    **Windows' Proactor loop does not implement `add_reader`**, and aiomqtt
    drives paho through exactly that; `slife2.plugins.a2a.server` sets
    `WindowsSelectorEventLoopPolicy` for the same reason at its own start.  A
    pytest worker's loop is the Proactor one and cannot be replaced under a
    running test, so anything in a thread here builds its own selector loop
    directly rather than changing a global policy the rest of the suite would
    inherit.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


# ── the provider ─────────────────────────────────────────────────────────────

#: The model's answer, and the one string most scenarios turn on.
ANSWER = "It is 42."

#: The agent the stack's seeded WeChat login belongs to.  One name rather than
#: every test's: the wechat plugin serves every agent, and a login is a file, so
#: a second name would be a second login polling the same stub.
WECHAT_AGENT = "e2e-wechat"

#: How much of one tool output a model may be handed, in the config the lane
#: writes.  Small on purpose: the window is what the reader is asserted
#: against, and a window near the default would be a window no test can see.
#: It is the config's own floor: a smaller one cannot hold the dump marker's
#: address, so `load()` refuses it before a child ever reads the file.
WINDOW = 512

#: The embedding model's name, and the input limit its listing reports.  The
#: limit is not optional: see `_ProviderHandler.do_GET`.
MODEL = "e2e-embed"
INPUT_TOKENS = 8192


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
    after the tool has answered.  The last entry repeats, so the single-answer
    script the other tests write is this mechanism's one-entry case rather than
    a second one beside it.
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


# ── the iLink bridge ────────────────────────────────────────────────────────


class _IlinkHandler(BaseHTTPRequestHandler):
    """The six routes the wechat plugin calls, answered in a thread."""

    stub: ClassVar[IlinkStub]

    def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
        self._handle()

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        self._handle()

    def _handle(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        status, answer = self.stub.answer(urlsplit(self.path).path, body)
        data = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        """Silence, for `_ProviderHandler.log_message`'s reason."""


class IlinkStub:
    """iLink's bridge, as the wechat plugin meets it: a cursor, and messages.

    **The cursor is the bridge's whole contract here.**  A real bridge re-serves
    recent messages until the client tells it, through `get_updates_buf`, where
    it has read up to.  A stub that popped each message on its first read would
    lose it to `validate_session`, which polls the same endpoint at login and
    throws the messages away — so the queue keeps a message until the cursor
    has moved past it, exactly as the real one does.

    **Nothing is served until a test puts it there.**  The login is adopted at
    start-up, before a window is attached, so a message enqueued at start-up
    would be handed over to nobody and lost.  A test queues its message once
    its window is up.
    """

    def __init__(self) -> None:
        self._messages: list[tuple[int, dict[str, Any]]] = []
        self._cursor = 0
        self._lock = threading.Lock()
        #: Every `sendmessage` body the plugin sent, newest last.
        self.sends: list[dict[str, str]] = []
        self._httpd: ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        server = self._httpd
        assert server is not None, "not started"
        return int(server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def enqueue(self, text: str, *, peer: str, token: str) -> None:
        """Put one message where the next poll will read it."""
        message = {
            "from_user_id": peer,
            "context_token": token,
            "item_list": [{"text_item": {"text": text}}],
        }
        with self._lock:
            self._cursor += 1
            self._messages.append((self._cursor, message))

    def answer(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """One route's answer, as `(status, body)`."""
        if path.endswith("/ilink/bot/getupdates"):
            asked = str(body.get("get_updates_buf") or "")
            after = int(asked) if asked.isdigit() else 0
            with self._lock:
                messages = [
                    message for index, message in self._messages if index > after
                ]
                cursor = max(after, self._cursor)
            return 200, {"msgs": messages, "get_updates_buf": str(cursor)}
        if path.endswith("/ilink/bot/sendmessage"):
            msg = body.get("msg") or {}
            items = msg.get("item_list") or []
            first = items[0] if items else {}
            text = str(((first or {}).get("text_item") or {}).get("text") or "")
            with self._lock:
                self.sends.append(
                    {
                        "to": str(msg.get("to_user_id") or ""),
                        "token": str(msg.get("context_token") or ""),
                        "text": text,
                    }
                )
            return 200, {"ret": 0}
        if path.endswith("/ilink/bot/getconfig"):
            return 200, {"ret": 0, "typing_ticket": "e2e-typing-ticket"}
        if path.endswith("/ilink/bot/sendtyping"):
            return 200, {"ret": 0}
        return 404, {"error": f"no {path}"}

    async def wait_for_send(self, *, timeout: float = 60.0) -> dict[str, str]:
        """The first outbound message, waiting for the turn to get there."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                found = self.sends[0] if self.sends else None
            if found is not None:
                return found
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"the wechat plugin sent nothing within {timeout:.0f}s"
                )
            await asyncio.sleep(0.05)

    def start(self) -> None:
        handler = type("_Bound", (_IlinkHandler,), {"stub": self})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


# ── the MQTT broker ─────────────────────────────────────────────────────────
#
# MQTT 5, and only the part the mesh speaks: CONNECT/CONNACK, SUBSCRIBE/SUBACK,
# PUBLISH/PUBACK both ways, retained messages, No Local, PING, DISCONNECT.  What
# it deliberately does not do is keep sessions across a reconnect, honour a
# last will, or enforce keepalive: no test in this lane needs one, and each is
# a way for a stub to disagree with a broker about something nobody asked it.


def _varint(value: int) -> bytes:
    """One MQTT variable byte integer."""
    out = bytearray()
    while True:
        digit = value % 128
        value //= 128
        if value:
            digit |= 0x80
        out.append(digit)
        if not value:
            return bytes(out)


def _read_varint(data: bytes, at: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if at >= len(data):
            raise ValueError("truncated variable byte integer")
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7
        if shift > 21:
            raise ValueError("variable byte integer too long")


def _utf8(data: bytes, at: int) -> tuple[str, int]:
    if at + 2 > len(data):
        raise ValueError("truncated string length")
    size = int.from_bytes(data[at : at + 2], "big")
    at += 2
    if at + size > len(data):
        raise ValueError("truncated string")
    return data[at : at + size].decode("utf-8"), at + size


def _string(text: str) -> bytes:
    encoded = text.encode("utf-8")
    return len(encoded).to_bytes(2, "big") + encoded


def _matches(pattern: str, topic: str) -> bool:
    """Whether one MQTT topic filter matches one topic."""
    pattern_parts = pattern.split("/")
    topic_parts = topic.split("/")
    for index, part in enumerate(pattern_parts):
        if part == "#":
            return True
        if index >= len(topic_parts):
            return False
        if part != "+" and part != topic_parts[index]:
            return False
    return len(pattern_parts) == len(topic_parts)


class _Connection:
    """One client's socket, its subscriptions, and a lock around its writes."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.client_id = ""
        #: filter -> (granted qos, no_local)
        self.subscriptions: dict[str, tuple[int, bool]] = {}
        self._write_lock = asyncio.Lock()
        self._packet_id = 0

    def next_packet_id(self) -> int:
        self._packet_id = (self._packet_id % 65535) + 1
        return self._packet_id

    async def send(self, packet: bytes) -> None:
        async with self._write_lock:
            self.writer.write(packet)
            await self.writer.drain()

    async def publish(
        self,
        topic: str,
        payload: bytes,
        properties: bytes,
        *,
        qos: int,
        retain: bool,
    ) -> None:
        body = _string(topic)
        if qos:
            body += self.next_packet_id().to_bytes(2, "big")
        body += _varint(len(properties)) + properties + payload
        header = 0x30 | (qos << 1) | (1 if retain else 0)
        await self.send(bytes([header]) + _varint(len(body)) + body)


class MqttBroker:
    """A broker enough for the mesh, on a port, in a thread.

    **Retained messages are the mesh's discovery.**  An agent's card is
    published retained so a peer that subscribes after it appeared still learns
    it is there; a broker that dropped the retain bit would make the mesh work
    only when everything started in the right order, which is exactly the kind
    of accident a test must not depend on.

    **No Local is honoured**, because the outbound client subscribes the event
    topics its own publications land on.  slife2's handler also refuses an
    event naming itself, but the broker's promise is the layer the design
    names, and a stub that skipped it would leave the receiver's guard as the
    only thing keeping one client from reading its own news.
    """

    def __init__(self) -> None:
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[_Connection] = set()
        self._retained: dict[str, tuple[bytes, bytes]] = {}

    def start(self) -> None:
        ready = threading.Event()
        failure: list[BaseException] = []

        def run() -> None:
            loop = _thread_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                self._server = loop.run_until_complete(
                    asyncio.start_server(self._serve, "127.0.0.1", 0)
                )
                assert self._server.sockets
                self.port = int(self._server.sockets[0].getsockname()[1])
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                failure.append(exc)
            ready.set()
            if not failure:
                loop.run_forever()
            loop.close()

        self._thread = threading.Thread(target=run, name="e2e-mqtt-broker", daemon=True)
        self._thread.start()
        if not ready.wait(10):
            raise RuntimeError("the MQTT broker did not start")
        if failure:
            raise failure[0]

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None:
            return

        async def close() -> None:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
            for connection in list(self._connections):
                connection.writer.close()
            # A tick for the per-connection readers to see the close and
            # return; stopping the loop on top of them reports every one as a
            # task destroyed while pending, which reads like a failure.
            await asyncio.sleep(0.1)

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(5)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connection = _Connection(writer)
        self._connections.add(connection)
        try:
            while True:
                first = await reader.readexactly(1)
                remaining, _ = await _read_async_varint(reader)
                body = await reader.readexactly(remaining) if remaining else b""
                if not await self._dispatch(connection, first[0], body):
                    break
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            self._connections.discard(connection)
            with contextlib.suppress(Exception):
                writer.close()

    async def _dispatch(self, connection: _Connection, first: int, body: bytes) -> bool:
        kind = first >> 4
        if kind == 1:  # CONNECT
            connection.client_id = _connect_client_id(body)
            await connection.send(b"\x20\x03\x00\x00\x00")
        elif kind == 8:  # SUBSCRIBE
            await self._subscribe(connection, body)
        elif kind == 10:  # UNSUBSCRIBE
            await self._unsubscribe(connection, body)
        elif kind == 3:  # PUBLISH
            await self._publish(connection, first, body)
        elif kind == 12:  # PINGREQ
            await connection.send(b"\xd0\x00")
        elif kind == 14:  # DISCONNECT
            return False
        # PUBACK and anything else: nothing this stub has to do with them.
        return True

    async def _subscribe(self, connection: _Connection, body: bytes) -> None:
        packet_id = body[:2]
        at = 2
        properties_length, at = _read_varint(body, at)
        at += properties_length
        granted: list[bytes] = []
        wanted: list[tuple[str, int, bool, int]] = []
        while at < len(body):
            topic_filter, at = _utf8(body, at)
            options = body[at]
            at += 1
            qos = min(options & 0x03, 1)
            no_local = bool(options & 0x04)
            retain_handling = (options >> 4) & 0x03
            connection.subscriptions[topic_filter] = (qos, no_local)
            wanted.append((topic_filter, qos, no_local, retain_handling))
            granted.append(bytes([qos]))
        await connection.send(
            bytes([0x90])
            + _varint(2 + 1 + len(granted))
            + packet_id
            + b"\x00"
            + b"".join(granted)
        )
        for topic_filter, qos, _no_local, retain_handling in wanted:
            if retain_handling == 2:
                continue
            for topic, (properties, payload) in list(self._retained.items()):
                if _matches(topic_filter, topic):
                    await connection.publish(
                        topic, payload, properties, qos=qos, retain=True
                    )

    async def _unsubscribe(self, connection: _Connection, body: bytes) -> None:
        packet_id = body[:2]
        at = 2
        properties_length, at = _read_varint(body, at)
        at += properties_length
        reasons: list[bytes] = []
        while at < len(body):
            topic_filter, at = _utf8(body, at)
            connection.subscriptions.pop(topic_filter, None)
            reasons.append(b"\x00")
        await connection.send(
            bytes([0xB0])
            + _varint(2 + 1 + len(reasons))
            + packet_id
            + b"\x00"
            + b"".join(reasons)
        )

    async def _publish(self, connection: _Connection, first: int, body: bytes) -> None:
        qos = (first >> 1) & 0x03
        retain = bool(first & 0x01)
        at = 0
        topic, at = _utf8(body, at)
        packet_id = b""
        if qos:
            packet_id = body[at : at + 2]
            at += 2
        properties_length, at = _read_varint(body, at)
        properties = body[at : at + properties_length]
        at += properties_length
        payload = body[at:]
        if qos == 1:
            await connection.send(b"\x40\x04" + packet_id + b"\x00\x00")
        if retain:
            if payload:
                self._retained[topic] = (properties, payload)
            else:
                self._retained.pop(topic, None)
        for other in list(self._connections):
            for topic_filter, (sub_qos, no_local) in list(other.subscriptions.items()):
                if no_local and other is connection:
                    continue
                if _matches(topic_filter, topic):
                    await other.publish(
                        topic,
                        payload,
                        properties,
                        qos=min(qos, sub_qos),
                        retain=False,
                    )
                    break


def _connect_client_id(body: bytes) -> str:
    """The client id a CONNECT carries — the only field this broker reads."""
    at = 0
    _, at = _utf8(body, at)  # protocol name
    at += 1  # protocol level
    at += 1  # connect flags
    at += 2  # keep alive
    properties_length, at = _read_varint(body, at)
    at += properties_length
    client_id, _ = _utf8(body, at)
    return client_id


async def _read_async_varint(reader: asyncio.StreamReader) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = (await reader.readexactly(1))[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, shift
        shift += 7
        if shift > 21:
            raise ValueError("variable byte integer too long")


# ── a peer outside the stack ────────────────────────────────────────────────


class Peer:
    """One A2A task, asked from outside the stack — the SDK's real requester.

    **A thread with its own loop**, for `_thread_loop`'s reason: the requester
    is aiomqtt, and aiomqtt cannot run on a pytest worker's Proactor loop on
    Windows.  Being a thread is also what a peer is: nothing in this process's
    stack shares anything with it, which is the point of the network.

    The retry ladder is the SDK's, and its default first-reply wait is the
    profile's 15 s.  A test shortens it because the first rungs are the
    *readiness* wait — the window's `join` is still subscribing when the peer
    first publishes — and a longer wait buys nothing: a request that landed
    before the responder subscribed is simply lost.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        requester_id: str,
        org: str = "default",
        unit: str = "default",
        first_reply_timeout: float = 5.0,
        stream_idle_timeout: float = 60.0,
        max_attempts: int = 8,
        timeout: float = 180.0,
    ) -> None:
        self._requester = Requester(
            MqttConfig(host=host, port=port),
            TopicSpace(org=org, unit=unit),
            requester_id=requester_id,
            first_reply_timeout=first_reply_timeout,
            stream_idle_timeout=stream_idle_timeout,
            max_attempts=max_attempts,
        )
        self._timeout = timeout

    async def ask(
        self, agent: str, payload: str, task_id: str
    ) -> list[tuple[str, str]]:
        """Send one task, and answer with every reply frame up to its terminal."""
        return await asyncio.to_thread(self._ask, agent, payload, task_id)

    def _ask(self, agent: str, payload: str, task_id: str) -> list[tuple[str, str]]:
        loop = _thread_loop()
        try:
            return loop.run_until_complete(self._stream(agent, payload, task_id))
        finally:
            # The requester's client lives inside an async generator that the
            # early return leaves suspended until the loop closes it.  Close
            # and collect while the loop is still alive: paho's client reports
            # a socket it never saw closed from its destructor, and a
            # destructor that runs after `loop.close()` prints a traceback
            # into a passing test's output.
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(asyncio.sleep(0))
            gc.collect()
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()

    async def _stream(
        self, agent: str, payload: str, task_id: str
    ) -> list[tuple[str, str]]:
        seen: list[tuple[str, str]] = []
        async with asyncio.timeout(self._timeout):
            async for kind, content in self._requester.stream(agent, payload, task_id):
                seen.append((kind, content))
                if kind in TERMINAL_KINDS:
                    break
        return seen


# ── the terminal ────────────────────────────────────────────────────────────


class Window:
    """A terminal, as the stack sees one: it attaches, and runs what it is handed.

    **This is the window the design says the conversation belongs to**, and not
    a second way to read one.  It holds `attach` open exactly as `slife2.tui.app`
    does, and each message another channel hands over is submitted to the agent
    server with the channel it came in on — so a WeChat message, a peer's task
    or an agent task's answer runs the same turn a person's typing would, and a
    test reads the result from the same transcript the terminal reads.

    `run=False` is for a test whose subject is the delivery itself: the
    hand-over is still taken and acknowledged, and no turn is spent on it.
    """

    def __init__(self, config, agent: str, *, run: bool = True) -> None:
        self.config = config
        self.agent = agent
        self.run = run
        #: Every hand-over this window was handed, in arrival order.
        self.handovers: list[Handover] = []
        #: The turns it ran, as `(handover, reply)`, for the tests that read
        #: what the conversation answered.
        self.replies: list[tuple[Handover, TurnReply]] = []
        self.failures: list[BaseException] = []
        self._window = MCPWindowClient(config.server("tui").url, agent=agent)
        self._agent = MCPAgentClient(config.agent.server.url, agent=agent)
        self._attach: asyncio.Task[None] | None = None
        self._turns: set[asyncio.Task[None]] = set()
        self._changed = asyncio.Event()

    async def start(self, *, timeout: float = 60.0) -> None:
        """Connect both clients, attach, and wait until the server has us."""
        await self._window.connect()
        await self._agent.connect()
        self._attach = asyncio.create_task(self._window.attach(self._take))
        deadline = time.monotonic() + timeout
        async with Client(self.config.server("tui").url) as probe:
            while True:
                answer = await probe.call_tool("attached", {"agent": self.agent})
                if (answer.data or {}).get("attached") is True:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"the window for {self.agent} did not attach in {timeout:.0f}s"
                    )
                await asyncio.sleep(0.05)

    def _take(self, handover: Handover) -> None:
        """One message off the attach stream: remember it, and run it."""
        self.handovers.append(handover)
        self._changed.set()
        if not self.run or handover.kind != "turn":
            return
        task = asyncio.create_task(self._answer(handover))
        self._turns.add(task)
        task.add_done_callback(self._turns.discard)

    async def _answer(self, handover: Handover) -> None:
        try:
            reply = await self._agent.run_turn(
                handover.text, lambda _event: None, channel=handover.channel
            )
        except Exception as exc:  # noqa: BLE001 - the test reads it, not a log
            self.failures.append(exc)
            return
        self.replies.append((handover, reply))
        self._changed.set()

    async def wait_handover(
        self, *, channel: str = "", timeout: float = 120.0
    ) -> Handover:
        """The first hand-over — optionally on one channel — and when it came."""
        found = await self._wait_for(
            lambda: next(
                (
                    handover
                    for handover in self.handovers
                    if not channel or handover.channel == channel
                ),
                None,
            ),
            timeout=timeout,
            what=f"a hand-over{' on ' + channel if channel else ''}",
        )
        return found

    async def wait_reply(self, *, timeout: float = 120.0) -> TurnReply:
        """The first turn this window ran to its end."""
        reply = await self._wait_for(
            lambda: self.replies[0][1] if self.replies else None,
            timeout=timeout,
            what="the window's turn to answer",
        )
        return reply

    async def _wait_for(self, found, *, timeout: float, what: str) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            value = found()
            if value is not None:
                return value
            if self.failures:
                raise self.failures[0]
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no {what} within {timeout:.0f}s")
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), 0.1)

    async def stop(self) -> None:
        """Let the turns finish, then detach and close both connections."""
        if self._turns:
            await asyncio.gather(*self._turns, return_exceptions=True)
        attach, self._attach = self._attach, None
        if attach is not None:
            attach.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await attach
        await self._window.close()
        await self._agent.close()
