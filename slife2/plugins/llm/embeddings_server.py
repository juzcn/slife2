"""slife2-llm-embeddings — the one process that calls an embeddings endpoint.

**A process of its own, like every other backend here, and for the same
reason**: the provider SDK is imported only inside a server process, so the
stores, the agent loop and the TUI stay free of `openai`.  It is not one of the chat
backends because it has nothing to do with them — an embedding endpoint is
reached at `{base_url}/embeddings`, speaks no chat protocol, and is configured
under its own `embeddings:` section rather than under `providers:`.  A chat
server built from the chat providers has no chat provider to attach it to.

**Two tools, neither of them the model's.**  `embed` and `describe` carry no
`meta=FOR_THE_MODEL`, so the toolhub never offers them to a model — the same
standing as the context plugin's `remember`.  They are the stores' plumbing:
every index needs vectors and needs to know how wide they are, and this is where
both questions are answered.

**The width is discovered and never configured.**  `vec0` fixes a table's width
in its DDL and cannot be altered afterwards, so the number has to be right
before the first table exists — and a wrong one is not a visible failure but a
silent one: inserts of a different length are rejected one by one while a
correct-looking index stays empty.  Three ways to learn it, tried in order:
a table of known models, the endpoint's own model listing, and finally a probe
embed of one character, whose length *is* the answer.

**The input limit is discovered too, and refused when it cannot be.**  The
width can be *measured*; an input limit cannot be measured without sending the
oversized input, which is the failure this module's history records — a rejected
chunk, and an indexer stalled on that turn for ever.  So the limit comes from
the table or the endpoint's listing, and a model whose endpoint reports neither
is refused rather than handed a default (SDD Appendix C).
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

import slife2.helpers.timeouts as _T
from slife2.libs.host.config import (
    EMBEDDINGS_SERVER_NAME,
    Config,
    EmbeddingProviderSettings,
)
from slife2.libs.plugin.protocol import house_server, serve_plugin
from slife2.plugins.llm.server_common import unresolved_key

logger = logging.getLogger(__name__)

SERVER_NAME = EMBEDDINGS_SERVER_NAME

#: This server's key in the config's `servers:` table.
CONFIG_KEY = "embeddings"

#: Widths and input limits of models whose answers are known, so the common
#: case costs no probe request.  Both numbers are per model, not per provider:
#: the same weights served by a local daemon and by a hosted API embed
#: identically, which is what makes this table a fact about the model id.
_KNOWN_MODELS: dict[str, tuple[int, int]] = {
    # model id: (dimension, max input tokens)
    "bge-m3": (1024, 8192),
    "BAAI/bge-m3": (1024, 8192),
    "text-embedding-3-small": (1536, 8191),
    "text-embedding-3-large": (3072, 8191),
    "text-embedding-ada-002": (1536, 8191),
    "nomic-embed-text": (768, 8192),
}

#: Characters per token, for turning that limit into a character budget.  One,
#: deliberately: an estimate of about four lets a thirty-thousand character line
#: of escaped JSON ride as a single chunk, and the model rejects it — after which
#: the indexer stalls on that turn forever.  Overestimating the budget costs a
#: shorter chunk; underestimating it costs the turn.
_CHARS_PER_TOKEN = 1


class EmbeddingClient:
    """One OpenAI-compatible embeddings endpoint, and its discoverable width.

    The SDK client is created on first use, not in the constructor, for the
    reason `slife2.plugins.llm.openai_server` gives: the key is resolved at that moment,
    so a server nobody calls never opens the OS keyring.
    """

    def __init__(self, provider: EmbeddingProviderSettings) -> None:
        self.provider = provider
        self._client: Any = None

    @property
    def _known(self) -> tuple[int, int] | None:
        return _KNOWN_MODELS.get(self.provider.model)

    def client(self) -> Any:
        """The SDK client, created on first use.

        Raises:
            RuntimeError: If the key did not resolve.  Named here rather than
                left to the SDK, because a placeholder arriving as a bearer
                token produces a 401 that says nothing about which `${VAR}` is
                missing.
        """
        if self._client is not None:
            return self._client

        key = unresolved_key(SERVER_NAME, self.provider.name, self.provider)
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            base_url=self.provider.base_url,
            api_key=key,
            timeout=_T.timeouts.transport.embed_api,
            max_retries=_T.timeouts.stream.retries,
        )
        return self._client

    async def describe(self) -> dict[str, Any]:
        """What a store needs to know before it can build an index.

        Both numbers are established once per process and remembered by the
        caller: the width by the three-step ladder in `_dimension_of`, and the
        input limit by `_input_limit_of`, which is table-or-listing and refuses
        rather than guessing.
        """
        dimension = await self._dimension_of()
        max_tokens = await self._input_limit_of()
        return {
            "provider": self.provider.name,
            "model": self.provider.model,
            "base_url": self.provider.base_url,
            "dimension": dimension,
            "max_chars": max_tokens * _CHARS_PER_TOKEN,
        }

    async def _input_limit_of(self) -> int:
        """The model's input limit, by table or by the endpoint's listing.

        **A model this module does not know is not assumed to accept 8192
        tokens.**  The width *can* be measured — a one-character probe's length
        is the answer — but an input limit cannot be measured without sending
        the oversized input, which is the exact failure this module's history
        records: a chunk past the limit is rejected, and the indexer stalls on
        that turn for ever.  So the limit is asked for, and a model whose
        endpoint does not report one is refused rather than handed a guess
        (SDD Appendix C).

        Raises:
            RuntimeError: If neither the table nor the endpoint answers — either
                because the endpoint reported no limit, or because its listing
                could not be read, and the message says which.
        """
        if self._known is not None:
            return self._known[1]

        reported = await self._listed_input_limit()
        if reported:
            logger.info(
                "%s: input limit %d read from the endpoint's model listing",
                self.provider.model,
                reported,
            )
            return reported

        raise RuntimeError(
            f"{SERVER_NAME}: {self.provider.model!r} is not a model with a known "
            f"input limit and {self.provider.base_url!r} does not report one; "
            f"point at an endpoint that reports an input limit or use a known "
            f"model"
        )

    async def _listed_input_limit(self) -> int:
        """The input limit the endpoint reports for its model, if it reports one.

        Read from the same listing as the width, and for the same reason: an
        endpoint with several models behind one base_url is where a guess goes
        wrong, and it is the endpoint that knows which of its models takes how
        much.  Most OpenAI-compatible endpoints report nothing here, which is
        why the caller refuses rather than defaulting.

        **A listing that could not be read is not a listing that reports no
        limit.**  The width can be measured without the endpoint's help, so an
        unreadable listing there falls through to the probe; an input limit
        cannot, so the two answers must stay apart and this one is raised rather
        than returned as a `0` the caller cannot tell from silence (SDD
        SDD Appendix C).

        Raises:
            RuntimeError: If the listing itself could not be read, naming the
                endpoint — which the caller's own refusal then distinguishes
                from an endpoint that answered and reports no limit.
        """
        try:
            listing = await self.client().models.list()
        except Exception as exc:  # noqa: BLE001 — named in the refusal below, not swallowed
            raise RuntimeError(
                f"{SERVER_NAME}: the input limit for {self.provider.model!r} "
                f"could not be read: the model listing at "
                f"{self.provider.base_url!r} did not answer ({exc})"
            ) from exc
        for entry in getattr(listing, "data", []) or []:
            if getattr(entry, "id", "") != self.provider.model:
                continue
            reported = getattr(entry, "max_input_tokens", None)
            if isinstance(reported, int) and reported > 0:
                return reported
        return 0

    async def _dimension_of(self) -> int:
        """The width, by table, by listing, or by measurement — in that order."""
        if self._known is not None:
            return self._known[0]

        listed = await self._listed_dimension()
        if listed:
            logger.info(
                "%s: width %d read from the endpoint's model listing",
                self.provider.model,
                listed,
            )
            return listed

        # Nothing left but to ask, with the cheapest question there is: one
        # character, whose vector is the width.
        answer = await self.embed(["."])
        if len(answer) != 1 or not answer[0]:
            # Named rather than left to `(vector,) = ...`, whose failure is a
            # bare ValueError about unpacking that says neither which endpoint
            # nor which model answered it — and a zero-length vector would
            # otherwise be accepted as a width of zero, which is the silent
            # failure this whole ladder exists to prevent.
            raise RuntimeError(
                f"{SERVER_NAME}: {self.provider.name!r} answered the probe with "
                f"{len(answer)} vector(s) for one input; cannot read a width "
                f"for {self.provider.model!r} from {self.provider.base_url!r}"
            )
        width = len(answer[0])
        logger.info(
            "%s: width %d measured with a probe embedding", self.provider.model, width
        )
        return width

    async def _listed_dimension(self) -> int:
        """The width the endpoint reports for its model, if it reports one.

        Most do not — the OpenAI protocol has no field for it — so this usually
        answers nothing and the probe runs.  It is kept because the endpoints
        that do report it are the ones with several models behind one base_url,
        which is exactly where guessing goes wrong.

        **Here an unreadable listing may be the silent one.**  The ladder this
        feeds measures the width without the endpoint's help, so a listing that
        could not be read and one that reports no width both fall to the probe —
        the difference the input limit cannot afford (SDD Appendix C).
        """
        try:
            listing = await self.client().models.list()
        except Exception as exc:  # noqa: BLE001 — the probe below measures the width instead
            logger.debug("%s: no model listing (%s)", self.provider.model, exc)
            return 0
        for entry in getattr(listing, "data", []) or []:
            if getattr(entry, "id", "") != self.provider.model:
                continue
            reported = getattr(entry, "dimension", None)
            if isinstance(reported, int) and reported > 0:
                return reported
        return 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One request, every text, in order.

        A whole list per request, as the protocol is shaped: the caller decides
        how much to put in one, and it decides one chunk at a time because a
        long document batched together can exceed the timeout on a service
        computing embeddings on a CPU.
        """
        if not texts:
            return []
        response = await self.client().embeddings.create(
            model=self.provider.model, input=texts
        )
        return [list(item.embedding) for item in response.data]


def provider_refusal(exc: Any) -> str:
    """One sentence for a provider that refused the call.

    **A refusal is not a fault.**  The SDK raises `APIStatusError` for a 4xx/5xx
    answer, and its `str()` carries the whole body — a person starting the stack
    needs the provider's own sentence and the setting that changes it, not a
    traceback.  The caller catches the SDK type; this reads its fields.

    The statuses with an action get one, and they are different actions.  A 402
    is the bill — and a free model does not exempt the account from it; a 401 is
    the credential; a 403 is the permission (the provider's usual answer when a
    model needs real-name verification).  Everything else keeps the provider's
    words and says nothing it cannot know.
    """
    status = int(getattr(exc, "status_code", 0) or 0)
    body = getattr(exc, "body", None)
    detail = str(body.get("message") or "") if isinstance(body, dict) else ""
    detail = detail or str(exc)
    if status == 402:
        hint = (
            "the account is in arrears — free models are gated too; top up or "
            "switch `embeddings.active_model`"
        )
    elif status == 401:
        hint = "check the provider's `api_key`"
    elif status == 403:
        hint = "check real-name verification and the key's permissions"
    else:
        hint = ""
    if hint:
        detail = f"{detail} — {hint}"
    return f"the embedding provider refused the call ({status}): {detail}"


def build_server(config: Config, *, client: EmbeddingClient | None = None) -> FastMCP:
    """Build the embeddings MCP server.  `client` is injectable for tests."""
    embedder = (
        client
        if client is not None
        else EmbeddingClient(config.embeddings.active_provider())
    )

    mcp: FastMCP = house_server(
        SERVER_NAME,
        instructions=(
            "Embeddings for the system's vector indexes. `describe` reports "
            "the width and input limit of the configured model; `embed` turns "
            "text into vectors. Neither is offered to the model."
        ),
    )

    @mcp.tool
    async def describe() -> dict[str, Any]:
        """The configured embedding model, and how wide its vectors are.

        The width is discovered rather than configured, because a vector table
        fixes its width in its DDL and cannot be altered after the fact.  Each
        store asks this once per process, before it builds or rebuilds anything.
        """
        from openai import APIStatusError

        try:
            return await embedder.describe()
        except APIStatusError as exc:
            raise ToolError(provider_refusal(exc)) from exc

    @mcp.tool
    async def embed(texts: list[str]) -> dict[str, Any]:
        """Embed texts, one vector each, in the order they were given.

        Args:
            texts: The chunks to embed.  One call is one request to the model, so
                the caller decides how much goes in it.
        """
        from openai import APIStatusError

        try:
            return {"vectors": await embedder.embed(texts)}
        except APIStatusError as exc:
            raise ToolError(provider_refusal(exc)) from exc

    return mcp


def main(argv: list[str] | None = None) -> int:
    """Serve the embeddings endpoint.  `slife2.libs.host.launcher` starts this.

    **The shared `main` and not this module's own**, which is what it used to be:
    the eleven lines below were spelled out here and in
    `slife2.plugins.agent.server`, and they were the same eleven lines every other
    plugin calls `serve_plugin` for.  What is this server's own is the `note` —
    which endpoint its vectors come from is the one fact a person cannot get
    from anywhere else on this machine, since the provider is resolved from a
    `embeddings:` section that no other process reads.
    """
    return serve_plugin(
        argv,
        server_name=SERVER_NAME,
        config_key=CONFIG_KEY,
        build=build_server,
        logger=logger,
        note=lambda config: (
            f"embeddings from {config.embeddings.active_provider().label} "
            f"at {config.embeddings.active_provider().base_url}"
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
