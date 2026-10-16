"""What the LLM MCP servers share.

Every one of them does the same job — expose one `stream_chat` tool, push
provider output down the progress channel, return the assembled message — and
they differ only in which wire format they speak.  This module holds everything
except that difference, so adding a backend means writing an adapter and a
`main`, not a server.

Serving a server at all — the flags, the HTTP transport, the record that says a
daemon is here — is *not* here, because it is not an LLM concern: it lives in
`slife2.libs.plugin.protocol`, alongside the other things every server in this system
agrees about.

It is also where **tool-call fragments are reassembled**, and that placement is
deliberate.  Every provider streams tool arguments as JSON text split at
arbitrary boundaries, and each indexes those fragments differently (OpenAI
repeats `tool_calls[].index`; Anthropic uses a content-block index).  Doing the
fold here means it happens once for both providers, and — more importantly —
that nothing downstream of this hop ever sees a fragment.  The agent loop
receives complete `ToolCall` objects and needs no accumulator of its own.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Context, FastMCP

from slife2.libs.host.config import (
    Config,
    ProviderSettings,
    find_config_path,
    load,
    load_cached,
    unresolved_reference,
)
from slife2.libs.model.base import Chunk, Finish, ProviderEvent, Streamer, ToolCallDelta
from slife2.libs.model.wire import encode_chunk
from slife2.libs.plugin.protocol import (
    configure_logging,
    house_server,
    parse_serve_args,
    serve,
)
from slife2.libs.turn.messages import (
    Message,
    StreamChatResult,
    ToolCall,
    ToolSpec,
    Usage,
)

logger = logging.getLogger(__name__)

#: What a model backend tells a caller that reads its instructions.
#:
#: The two things a caller cannot work out from the tool signature are what
#: `provider` selects and that the result — not the notifications — is
#: authoritative.  Nothing here restates a tool description; that is the spec's
#: guidance for this field, and it is also just true.
INSTRUCTIONS = (
    "A model backend. Call `stream_chat` with a configured provider and a model "
    "id; provider output arrives as progress notifications on the same request, "
    "and the result carries the complete assistant message. The result is "
    "authoritative — a caller that ignores the progress stream still gets the "
    "whole answer. This server keeps no state between calls."
)


@dataclass
class _PartialCall:
    """A tool call being assembled from fragments."""

    id: str = ""
    name: str = ""
    arguments: str = ""


class ToolCallAccumulator:
    """Folds tool-call fragments into complete calls.

    Fragments arrive out of order relative to each other and interleaved with
    text, so they are keyed by index and only assembled at the end.
    """

    def __init__(self) -> None:
        self._parts: dict[int, _PartialCall] = {}

    def add(self, delta: ToolCallDelta) -> None:
        """Absorb one fragment.

        `id` is typically sent only on the first fragment for an index, so it is
        kept rather than overwritten — a later fragment carrying none must not
        erase it, and a provider that repeats it is harmless because the value is
        the same.

        **`name` is accumulated, because a name that arrives in pieces has no
        other way to be read.**  Overwriting took the last fragment for the whole
        name: `read_` followed by `file` became a call to `file`, dispatched with
        the model's own arguments to whatever tool happened to be called that,
        and nothing anywhere compared the reassembled name against anything.  The
        arguments beside it have always been folded rather than replaced, for the
        same reason: a fragment is a piece of a value, never the value.

        The rule is prefix-aware, and the prefix is what keeps the case the
        overwrite got right.  A fragment that **starts with** what has already
        arrived is that name being *sent again* — the identical string, or a
        longer one restating it — so it replaces what is there; a fragment that
        does not begin with it is a continuation, and is appended.  The three
        wires this build speaks send the name whole (`function.name` on the first
        chat-completions delta, the `tool_use` block start, the `function_call`
        output item), so this only decides what a gateway in front of one that
        splits a name means — and the alternative to deciding is dispatching a
        name the model never asked for.
        """
        part = self._parts.setdefault(delta.index, _PartialCall())
        if delta.id:
            part.id = delta.id
        if delta.name:
            if delta.name.startswith(part.name):
                part.name = delta.name
            else:
                part.name += delta.name
        part.arguments += delta.arguments_delta

    def complete(self) -> list[ToolCall]:
        """Assemble the calls, ordered by index.

        A call whose arguments did not parse **fails the turn** rather than
        becoming an empty argument dict: `_parse_arguments` refuses it there,
        naming the call, because a truncated or corrupt fragment stream is the
        provider's fault and reading it as `{}` re-attributes it to the model —
        which then sees a validation error about its own call and retries the
        call it never got wrong (§10.1: a value that cannot be read is not a
        value that is absent).  A call that genuinely took no arguments is
        unaffected: an empty string still parses to no arguments, which is a
        real absence rather than a damaged one.
        """
        calls = []
        for index in sorted(self._parts):
            part = self._parts[index]
            calls.append(
                ToolCall(
                    # The id must exist: the tool result message refers back to
                    # it, and a provider that never sent one leaves nothing to
                    # refer to.  A synthetic id keeps the pairing valid.
                    id=part.id or f"call_{index}",
                    name=part.name,
                    arguments=_parse_arguments(
                        part.arguments, part.name or f"call_{index}"
                    ),
                )
            )
        return calls


def _parse_arguments(raw: str, call: str) -> dict[str, Any]:
    """Parse a reassembled arguments string — or refuse it, naming the call.

    An empty string is a call with no arguments, which is a real value.  A
    non-empty one that is not JSON is damage: the stream was cut short or the
    provider mangled it, and answering `{}` for it hands the model an empty
    argument dict it never sent.  So the refusal is a `ValueError` naming the
    tool and the fragment, and the caller's turn dies with the fault attributed
    where it happened.

    **Valid JSON that is not an object is the same damage.**  `[]`, `"x"` and
    `null` all parse, and a call's arguments are an object by the protocol — so
    coercing one to `{}` is the very substitution refused above, just past the
    parser instead of through it (§10.1: a value that cannot be read is not a
    value that is absent).
    """
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"the arguments for tool call {call!r} did not parse as JSON "
            f"({exc}): {raw[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"the arguments for tool call {call!r} are not a JSON object but "
            f"{type(parsed).__name__}: {raw[:200]!r}"
        )
    return parsed


@dataclass
class _TurnBuilder:
    """Accumulates provider events into the complete assistant message."""

    text_parts: list[str] = field(default_factory=list)
    #: Accumulated on the same terms as the text, and for the same reason: the
    #: reasoning chunks are forwarded to the progress stream to be *watched*, and
    #: what the turn keeps is the completed message.  A consumer that reassembled
    #: this from the display channel would be reading the record out of a stream
    #: that is allowed to drop fragments.
    thinking_parts: list[str] = field(default_factory=list)
    calls: ToolCallAccumulator = field(default_factory=ToolCallAccumulator)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""
    #: Whether the provider said it was finished.  Not the same question as
    #: whether the stream ended — see `stream_chat_impl`, which refuses to call
    #: a stream that ended without this the whole answer.
    finished: bool = False

    def absorb(self, event: Chunk | Finish) -> Chunk | None:
        """Fold one event in, returning the chunk to forward (or None).

        A `Finish` is bookkeeping and is swallowed here — it never reaches the
        progress stream, because the agent loop learns the stop reason from the
        returned `StreamChatResult` instead.  It is *recorded* rather than only
        read, because it is the one event that says the provider meant to stop
        here, and that is a different fact from the stream having ended.
        """
        if isinstance(event, Finish):
            self.stop_reason = event.stop_reason
            self.finished = True
            return None

        if event.text:
            self.text_parts.append(event.text)
        if event.thinking:
            self.thinking_parts.append(event.thinking)
        for delta in event.tool_call_deltas:
            self.calls.add(delta)
        if event.usage is not None:
            self.usage = self.usage + event.usage
        return event

    def build(self) -> StreamChatResult:
        return StreamChatResult(
            text="".join(self.text_parts),
            tool_calls=tuple(self.calls.complete()),
            thinking="".join(self.thinking_parts),
            usage=self.usage,
            stop_reason=self.stop_reason,
        )


async def stream_chat_impl(
    streamer: Streamer,
    provider: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    ctx: Context,
) -> dict[str, Any]:
    """Run one streamed model call and return the assembled result.

    **A stream that ends is not the provider saying it finished**, and the two
    are compared here so that a cut generation cannot pass for a whole one.  The
    SDK's async iteration ends *normally* when the response body simply stops —
    a chat-completions reply whose proxy idle-timeout fired after the last
    content chunk and before the chunk carrying `finish_reason`, a server
    restarted mid-answer, an SSE body closed early — and with nothing reading
    the difference this returned the partial text as the answer, with
    `stop_reason: ""`, which `slife2.libs.turn.loop` maps to "stop".  So a
    truncated answer was appended to the history and shown as the turn's answer,
    and nothing anywhere said the provider had gone silent.

    All three wires do signal the end, which is what makes the question
    decidable: chat-completions sets `finish_reason` on its last chunk, Anthropic
    sends `message_delta` with a `stop_reason`, and the Responses API ends with
    `response.completed` or `response.incomplete` — each of which this module's
    adapters turn into a `Finish`, the event that exists to carry exactly this
    (`slife2.libs.model.base`).  A stream with no `Finish` is therefore one that
    was cut, and the turn fails rather than handing half an answer to a loop
    that would record it as a whole one.
    """
    builder = _TurnBuilder()
    reported = 0

    async for event in streamer(
        provider,
        [Message.from_wire(m) for m in messages],
        [ToolSpec.from_wire(t) for t in tools],
        model,
    ):
        chunk = builder.absorb(event)
        if chunk is None:
            continue
        reported += 1
        # A no-op when the client sent no progress token, which is the intended
        # behaviour: a caller that does not want a stream should not pay for
        # one, and gets the same result at the end.  See `slife2.libs.turn.events`.
        await ctx.report_progress(reported, None, encode_chunk(chunk))

    if not builder.finished:
        raise RuntimeError(
            f"{provider}: the provider's stream ended without a finish event, so "
            "the answer was cut short and is not returned as a whole one"
        )
    return builder.build().to_wire()


def read_int(source: Any, attribute: str) -> int:
    """Read an int attribute that may be absent, `None`, or a string.

    Two adapters need this and had a byte-identical copy each — the fields it
    reads (`input_tokens`, `output_index`) are the protocol's own and are
    consistent about being optional, and the `or 0` is what makes a field the
    SDK left as `None` a zero rather than a `TypeError` in the middle of a
    stream.
    """
    return int(getattr(source, attribute, 0) or 0)


def render_tool_result(content: Any) -> str:
    """A tool result as the text every wire here takes, whatever shape it arrived in.

    A tool result is text on every backend this build speaks, and both adapters
    that build their request field by field used to answer `""` for a result
    that was not already a string — beneath a comment saying a non-string result
    is a bug upstream, then the guess the comment warns against: the tool's
    whole output vanished from the request with nothing said, and the model
    answered a question it had never been handed the material for.  Anthropic
    makes that reachable rather than theoretical: `tool_result.content` is
    legitimately a block list, so a list is a shape a caller can really send.

    So the result is *rendered* — JSON text, which is what a structured result
    already means — and never invented.  Deliberately not the place that decides
    a list of image blocks should stay blocks: no wire here carries a picture in
    a tool result, and a JSON rendering is a fact the model can read where the
    empty string was a fact hidden.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=True, default=str)


class ProviderClients:
    """The SDK clients of one process, one per configured provider, on first use.

    **The three wire adapters need the same two things and differ in one.**  A
    provider looked up by the name a caller passed, with a message naming the
    ones that do exist; and a client built once and kept, because each carries
    its own `base_url` and key.  The only per-protocol fact is which SDK class
    is constructed, and that is the callable this takes.

    It is here rather than written out three times because everything *around*
    that one difference is advice a person acts on: the unresolved-key message
    says to export the variable or `credstore set` it, and three copies of a
    sentence like that are three places for it to drift apart.  The fourth
    adapter — embeddings — has one provider rather than a table, so it keeps
    its own cache and shares `unresolved_key` instead.

    Lazy for the reason the key is: a provider nobody calls never opens the OS
    keyring, and constructing a client outside a running event loop is not
    always safe.
    """

    def __init__(
        self,
        providers: Mapping[str, ProviderSettings],
        server_name: str,
        make_client: Callable[[ProviderSettings, str], Any],
    ) -> None:
        self._providers = providers
        self._server = server_name
        self._make_client = make_client
        self._clients: dict[str, Any] = {}
        #: Streams holding this pool, and whether the config has moved past it.
        #: A retired pool closes its SDK clients when its last holder is done —
        #: see `LiveProviders.lease`.
        self._holders = 0
        self._retired = False

    def hold(self) -> None:
        """Count one more stream as using this pool.

        **Synchronous on purpose**, and it must be: the hand-out and the count
        have to happen with no await between them, or a rebuild landing in the
        gap would find no holders and close a client a stream is about to use.
        """
        self._holders += 1

    async def release(self) -> None:
        """One holder is done with this pool, closing it if it was retired."""
        self._holders -= 1
        await self._close_if_retired()

    async def retire(self) -> None:
        """Say this pool will not be handed out again; close it once it is idle."""
        self._retired = True
        await self._close_if_retired()

    async def _close_if_retired(self) -> None:
        """Close every SDK client this pool built, once nothing is using it.

        **A client owns a connection pool and its socket, and nothing else ever
        closes it.**  `AsyncOpenAI` and `AsyncAnthropic` each hold an `httpx`
        client; `AsyncAPIClient` defines no `__del__` and `httpx` closes nothing
        at collection, so a client dropped without a close keeps its connections
        until the collector happens to reach it.  The one thing that drops a pool
        is the config-change rebuild — which is why the close lives here — and
        before this it dropped them open, one set per config read.

        **When it is safe matters as much as that it happens.**  The SDK's
        `close()` closes the connection *under* a stream that may still be
        reading it, so a pool the config has moved past waits for its last
        holder: an edit must not cut off a model call that is half answered.
        """
        if not self._retired or self._holders:
            return
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - a close on the way out
                logger.debug(
                    "%s: an SDK client did not close cleanly",
                    self._server,
                    exc_info=True,
                )

    def provider(self, name: str) -> ProviderSettings:
        """The named provider, or the error that says which ones there are."""
        try:
            return self._providers[name]
        except KeyError:
            known = ", ".join(sorted(self._providers)) or "(none)"
            raise RuntimeError(
                f"{self._server}: no provider {name!r} in this config (known: {known})"
            ) from None

    def client(self, name: str) -> Any:
        """The SDK client for one provider, created on first use."""
        client = self._clients.get(name)
        if client is not None:
            return client
        provider = self.provider(name)
        key = unresolved_key(self._server, name, provider)
        client = self._make_client(provider, key)
        self._clients[name] = client
        return client


class LiveProviders:
    """The providers of one wire protocol, re-read when the config changes.

    **A backend process holds its provider table for its whole life otherwise**,
    and a model config edit would never reach it: `stream_chat` names a provider
    and a model, and this is the table those names are looked up in.  So the
    table follows the file — `load_cached` hands back the *same* `Config` object
    while the file is untouched and a new one when it is not, which makes "did
    an edit happen" one `is` comparison and needs no watcher.

    **The whole pool is rebuilt on a change, and that is what drops the cached
    SDK clients with it** — necessary, because a provider whose `base_url` or key
    changed needs a client built from the new values, and cheap, because a client
    nobody has called for has not been built yet.

    **Dropped, but not abandoned**: the pool being replaced is *closed* rather
    than left to the collector, which is what `lease` and `ProviderClients` are
    for.  An SDK client that is neither closed nor collected holds its connection
    to the provider open, and this rebuild is the only thing that ever lets go of
    one.
    """

    def __init__(
        self,
        api: str,
        server_name: str,
        make_client: Callable[[ProviderSettings, str], Any],
    ) -> None:
        self._api = api
        self._server = server_name
        self._make_client = make_client
        self._config: Config | None = None
        self._clients: ProviderClients | None = None

    @contextlib.asynccontextmanager
    async def lease(self) -> AsyncIterator[ProviderClients]:
        """The pool for the config as it is now, held for the length of one stream.

        **The read and the hold are one step, and the hold is what makes the
        close safe.**  A config change retires the pool it replaces, but an SDK
        client in it may be half way through a stream somebody else is still
        reading, and closing it would cut that answer off — so a retired pool
        closes its clients when the last stream holding it is done.  Handing the
        pool out and counting it have to happen with no await between them, which
        is why this is a context manager: a `clients()` call plus a `release()`
        the caller must remember would leave the gap open.
        """
        config = load_cached()
        retired: ProviderClients | None = None
        if self._clients is None or config is not self._config:
            retired = self._clients
            self._clients = ProviderClients(
                {
                    name: provider
                    for name, provider in config.providers.items()
                    if provider.api == self._api
                },
                self._server,
                self._make_client,
            )
            self._config = config
        clients = self._clients
        clients.hold()
        try:
            if retired is not None:
                await retired.retire()
            yield clients
        finally:
            await clients.release()


def unresolved_key(server_name: str, name: str, provider: Any) -> str:
    """The provider's key, or the error naming the reference that did not resolve.

    A reference the config could not resolve is left **verbatim** rather than
    emptied (`slife2.libs.host.config`), so the value reaching here is either a
    key or the literal reference, and saying which reference is the whole of
    what a person needs to fix it.  Handing the literal to the SDK instead
    produces a 401 that says nothing about which name is missing.

    **`unresolved_reference` and not `startswith("${")`**, which is what this
    was: that test does not recognise `keyring:NAME`, so on a host with no
    credstore installed the reference itself went out as the bearer token —
    `Bearer keyring:DEEPSEEK_API_KEY` — which is the exact deformation the
    config module names (DESIGN.md §10.1) and which the other three readers of
    this same answer already refused.
    """
    key = provider.api_key
    if key and not unresolved_reference(key):
        return key
    raise RuntimeError(
        f"{server_name}: the API key for provider {name!r} "
        f"({provider.base_url!r}) did not resolve "
        f"(config value {provider.api_key_ref!r}). Export it, or "
        f"store it with `credstore set <NAME>`."
    )


#: How one stream is opened: the SDK client, and the request body to send it.
StartStream = Callable[[Any, dict[str, Any]], Awaitable[Any]]
#: How one provider event becomes zero or more of ours.
Translate = Callable[[Any], Iterable[ProviderEvent]]
#: A fresh translation for one stream.  A factory and not a function, because a
#: translation carries state across the stream it is for.
Translator = Callable[[], Translate]


def sdk_client_factory(
    module: str, attribute: str
) -> Callable[[ProviderSettings, str], Any]:
    """The one per-protocol fact of client construction: which SDK class.

    **Three adapters wrote this closure out, and the two OpenAI ones were
    byte-identical.**  Everything around the class is the same — a client built
    from the provider's `base_url` and the key resolved for it — and the class is
    the only thing a protocol decides, which is why it is named here as two
    strings rather than spelled out by each adapter.  A change that belongs to
    every backend (a timeout, a retry policy) then has one site, the way
    `sdk_streamer` gives the rest of the frame one.

    **The import is at call time on purpose.**  `import_module` here is what an
    in-function `from openai import AsyncOpenAI` did in each adapter: the module
    stays importable, and a process that serves nothing from it never loads the
    SDK.  It also leaves the class named once, in the `sdk_streamer` call that
    uses it.
    """

    def make_client(provider: ProviderSettings, key: str) -> Any:
        client_class = getattr(importlib.import_module(module), attribute)
        return client_class(api_key=key, base_url=provider.base_url)

    return make_client


def sdk_streamer(
    *,
    api: str,
    server_name: str,
    sdk_client: Callable[[ProviderSettings, str], Any],
    build_request: Callable[[list[Message], list[ToolSpec], Any], dict[str, Any]],
    start: StartStream,
    translator: Translator,
) -> Streamer:
    """The frame every model backend's streamer is, with four holes in it.

    **Four servers wrote this out, and they differed in four places.**  Which SDK
    class is constructed, which method opens the stream, how the request is
    built, and how a provider event becomes one of ours.  Everything around
    those — the pool that re-reads the config per call, the resolved settings,
    the loop over the response — is one thing, and it was four copies of it.

    `translator` is a *factory* rather than a function because a translation
    carries state across one stream: which calls have streamed their arguments,
    how many output tokens have already been counted.  Made per stream, so two
    concurrent calls cannot share a counter.

    `build_request` is passed in rather than called here because one wire has a
    flag that has to be closed over before the request exists (`stream_usage`),
    and closing over it is the caller's business.
    """
    pool = LiveProviders(api, server_name, sdk_client)

    async def stream(
        provider: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        # Held for the whole stream, not just to look the provider up: the
        # client is what carries the response being read below, and a config
        # edit landing mid-answer must not close it out from under the stream
        # (`LiveProviders.lease`).
        async with pool.lease() as clients:
            settings = clients.provider(provider).model(model)
            request = build_request(messages, tools, settings)
            response = await start(clients.client(provider), request)
            translate = translator()
            async for event in response:
                for out in translate(event):
                    yield out

    return stream


def build_backend_server(
    *, name: str, streamer: Streamer | None, make: Callable[[], Streamer]
) -> FastMCP:
    """`build_llm_server` with the injectable default every backend spells out.

    Three servers wrote the same four lines: a `streamer` a test may pass in, and
    otherwise the one this process builds.  It is small, and it is exactly the
    thing a fourth backend gets wrong — forget the parameter and the test seam
    closes silently, which is a whole test file that stops testing anything.
    """
    return build_llm_server(
        name=name, streamer=streamer if streamer is not None else make()
    )


def build_llm_server(*, name: str, streamer: Streamer) -> FastMCP:
    """Build the FastMCP server exposing `stream_chat` over `streamer`.

    The streamer is injected rather than built here so tests can drive the whole
    server through the in-memory transport with a scripted provider — no API
    key, no network, no mocking library.
    """
    mcp = house_server(name, instructions=INSTRUCTIONS)

    @mcp.tool
    async def stream_chat(
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        ctx: Context,
        agent: str = "",
        subagent: str = "",
    ) -> dict[str, Any]:
        """Stream one chat completion.

        Provider output arrives as `notifications/progress` on the calling
        request, one notification per chunk.  The return value is the complete
        assistant message and is authoritative: a client that ignores the
        progress stream still gets the whole answer.

        Args:
            provider: Which configured provider to call.  One process serves
                every provider that speaks its wire format, and this says whose
                credentials and model list to use.
            model: The model id, as that provider names it.
            messages: OpenAI-shaped chat messages — **the whole conversation, sent
                afresh every call.**  This server keeps nothing between calls, and
                that is deliberate rather than unfinished: it speaks one wire
                protocol, so a conversation living here would be lost the moment
                it moved to a provider that speaks another.  The history belongs
                one hop up, where the server is protocol-agnostic.
            tools: OpenAI-shaped tool definitions; empty for no tools.
            agent: Whose conversation this call is serving.  Nothing is keyed on
                it — this server has no state for it to key — but it travels with
                every call so a log line, and anything that later wants to account
                for one agent's usage, can say *whose* call was served rather than
                leaving every hop anonymous.
            subagent: Which of that agent's conversations, empty for its own.
        """
        logger.debug(
            "stream_chat %s/%s for %s",
            provider,
            model,
            f"{agent}/{subagent}" if subagent else agent or "(unstated)",
        )
        return await stream_chat_impl(streamer, provider, messages, tools, model, ctx)

    return mcp


def serve_backend(
    argv: list[str] | None,
    *,
    api: str,
    server_name: str,
    build: Callable[[], FastMCP],
    logger: logging.Logger,
) -> int:
    """The `main` every model server shares.

    One process per wire protocol, so the only thing that genuinely differs
    between any two of them is which protocol they serve and which SDK builds
    the streamer.  Everything else here was byte-identical in the first two,
    which is the kind of duplication that drifts one branch at a time — and a
    third copy would have made it three.

    **`build` takes nothing**: the server it returns reads the providers itself,
    through `LiveProviders`, so a model added while this process is running
    arrives without a restart.  What is read *here* is only the boot question —
    whether this config has a provider for this protocol at all — and the
    address.

    A config with no provider for this protocol stops here, with a one-line
    message and exit code 2, rather than starting a server that can answer
    nothing.

    `logger` is the caller's, so a line still lands under the name of the server
    that wrote it rather than under this module's.
    """
    args = parse_serve_args(argv, server_name)
    configure_logging()
    config_path = find_config_path()
    config = load()

    providers = {
        name: provider
        for name, provider in config.providers.items()
        if provider.api == api
    }
    if not providers:
        print(f"{server_name}: this config has no {api} provider")
        return 2

    address = config.server(api)
    logger.info(
        "serving %s for %s on http://%s:%d%s (providers: %s)",
        server_name,
        api,
        args.host or address.host,
        args.port or address.port,
        address.path,
        ", ".join(sorted(providers)),
    )
    serve(build(), address, args, name=server_name, config_path=config_path)
    return 0
