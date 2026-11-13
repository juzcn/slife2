"""The turn runtime: the middle both turn-running servers are built on.

Everything is driven by doubles at the runtime's two protocol seams — the
context store's `call_tool`, and `open_server`/`open_backend` — so the claims
are about how an answer is read and how the caches are keyed, not about a live
daemon.  The two plugin suites are the behaviour's gate; these pin the rules
that moved into the library.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeBackend, text_turn

from slife2.clients.model import MCPBackend
from slife2.libs.host.config import (
    CONTEXT_SERVER_NAME,
    TOOLHUB_SERVER_NAME,
    default_config,
)
from slife2.libs.plugin.protocol import UnreadableAnswer
from slife2.libs.turn.events import ContextChosen
from slife2.libs.turn.messages import Message
from slife2.libs.turn.runtime import ModelRef, TurnRuntime
from slife2.libs.turn.tools import ToolRegistry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

IDENTITY = ("jack", "")
REF = ModelRef(
    "deepseek/deepseek-flash",
    "deepseek",
    "openai-completions",
    "deepseek-flash",
)


class Store:
    """A context plugin answering by tool name, and remembering what it asked."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return SimpleNamespace(data=self.answers[name])


class Hub:
    """A toolhub whose one answer is fixed, counted by call."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.calls = 0

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> Any:
        self.calls += 1
        return SimpleNamespace(data=self.answer)


def wired(store: Store) -> TurnRuntime:
    return TurnRuntime(default_config(), context_client=store)


# ── restore ──────────────────────────────────────────────────────────────────


async def test_restore_reports_the_conversation_and_its_size() -> None:
    """The answer's two halves, the count they cover, and the bar's figure."""
    head = Message(role="system", content="you are jack")
    store = Store(
        restore={
            "messages": [
                {"role": "system", "content": "you are jack"},
                {"role": "user", "content": "hello"},
            ],
            "turn_ids": [7],
            "turns": [{"turn_id": 7, "context_tokens": 1200}],
        }
    )

    restored = await wired(store).restore(IDENTITY, [head])

    assert [message.role for message in restored.messages] == ["system", "user"]
    assert restored.turn_ids == [7]
    assert restored.covered == 2, "a restore carries nothing in front of it"
    assert restored.context_tokens == 1200
    assert store.calls == [
        (
            "restore",
            {
                "agent": "jack",
                "subagent": "",
                "messages": [head.to_wire()],
            },
        )
    ]


@pytest.mark.parametrize(
    ("answer", "sentence"),
    [
        ({"turn_ids": [], "turns": []}, "message list"),
        ({"messages": [], "turns": []}, "turn-id list"),
        ({"messages": [], "turn_ids": []}, "stored turns"),
    ],
)
async def test_restore_refuses_an_answer_it_cannot_read(
    answer: dict[str, Any], sentence: str
) -> None:
    """Every half of one answer is read strictly, and in the asserted order.

    `messages` and `turn_ids` are one context; `turns` is part of the same
    shape, written even when empty.  Reading a missing half as absent would
    make a store from another build indistinguishable from a decision.
    """
    with pytest.raises(UnreadableAnswer, match=sentence):
        await wired(Store(restore=answer)).restore(IDENTITY, [])


@pytest.mark.parametrize(
    "turns",
    [
        [],
        [{"turn_id": 1}],
        [{"turn_id": 1, "context_tokens": "big"}],
        [{"turn_id": 1, "context_tokens": True}],
        ["not a row"],
    ],
)
async def test_restore_reads_an_unmeasurable_size_as_zero(
    turns: list[Any],
) -> None:
    """A display fact is not a reason to fail a restore."""
    store = Store(restore={"messages": [], "turn_ids": [], "turns": turns})
    assert (await wired(store).restore(IDENTITY, [])).context_tokens == 0


# ── rebuild ──────────────────────────────────────────────────────────────────


async def test_rebuild_carries_the_unbacked_tail_and_reports_the_decision() -> None:
    """`carried` is the run no turn covers, and the counts are the decision.

    The store rebuilds from its snapshot and re-appends the caller's tail
    verbatim; the returned `covered` counts the rebuilt turns only.
    """
    store = Store(
        rebuild={
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "kept"},
                {"role": "assistant", "content": "answer"},
            ],
            "turn_ids": [7, 8],
            "kept": 2,
            "recalled": 1,
            "rerank": "unavailable",
        }
    )
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="old"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="unbacked"),
    ]

    rebuilt = await wired(store).rebuild(
        IDENTITY, messages, [7, 8], 2, "what now", REF.reference
    )

    assert rebuilt.covered == 1, "three rebuilt messages minus the two carried"
    assert rebuilt.turn_ids == [7, 8]
    assert rebuilt.chosen == ContextChosen(kept=2, recalled=1, rerank="unavailable")
    name, arguments = store.calls[0]
    assert name == "rebuild"
    assert arguments == {
        "agent": "jack",
        "subagent": "",
        "messages": [message.to_wire() for message in messages],
        "turn_ids": [7, 8],
        "expected": [7, 8],
        "prompt": "what now",
        "model": REF.reference,
        "carried": 2,
    }


@pytest.mark.parametrize(
    ("answer", "sentence"),
    [
        ({"turn_ids": [], "kept": 0, "recalled": 0, "rerank": ""}, "message list"),
        ({"messages": [], "kept": 0, "recalled": 0, "rerank": ""}, "turn-id list"),
        ({"messages": [], "turn_ids": []}, "counts"),
        (
            {"messages": [], "turn_ids": [], "kept": 0, "recalled": 0},
            "rerank status",
        ),
    ],
)
async def test_rebuild_refuses_an_answer_it_cannot_read(
    answer: dict[str, Any], sentence: str
) -> None:
    """A reply missing the counts is not a rebuild of zero of each."""
    with pytest.raises(UnreadableAnswer, match=sentence):
        await wired(Store(rebuild=answer)).rebuild(
            IDENTITY, [], [], 0, "q", REF.reference
        )


async def test_a_malformed_rebuild_leaves_the_callers_list_alone() -> None:
    """Everything is read before anything is adopted.

    The store may have replaced its list, but this turn did not take the
    answer: the list the caller holds is still the list it held.  The next
    turn's `expected` compare-and-set recovers the store's side.
    """
    store = Store(
        rebuild={
            "messages": [{"role": "user", "content": "other"}],
            "turn_ids": [1],
        }
    )
    messages = [Message(role="system", content="sys")]

    with pytest.raises(UnreadableAnswer, match="counts"):
        await wired(store).rebuild(IDENTITY, messages, [], 0, "q", REF.reference)

    assert messages == [Message(role="system", content="sys")]


# ── trim ─────────────────────────────────────────────────────────────────────


async def test_the_trim_writes_one_pair_and_names_what_left() -> None:
    hub = Hub({"unloaded": ["a__one", "a__two"], "text": "Unloaded 2 tools."})
    runtime = TurnRuntime(default_config(), hub_client=hub)
    messages: list[Message] = []

    unloaded = await runtime.trim(messages)

    assert unloaded == ["a__one", "a__two"]
    assert [message.role for message in messages] == ["assistant", "tool"]
    call = messages[0].tool_calls[0]
    assert call.name == "_func_tool_unload"
    assert call.id.startswith("_harness_")
    assert messages[1].tool_call_id == call.id
    assert messages[1].content == "Unloaded 2 tools."


async def test_nothing_is_written_when_nothing_left() -> None:
    """An empty pair every turn would be a record of something that did not
    happen."""
    runtime = TurnRuntime(
        default_config(), hub_client=Hub({"unloaded": [], "text": ""})
    )
    messages: list[Message] = []

    assert await runtime.trim(messages) == []
    assert messages == []


async def test_a_trim_that_cannot_be_read_raises_and_writes_nothing() -> None:
    """The caller decides how to report it; the pair is never half-written."""
    runtime = TurnRuntime(
        default_config(),
        hub_client=Hub({"refused": [], "not_loaded": [], "text": "the list is fine"}),
    )
    messages: list[Message] = []

    with pytest.raises(UnreadableAnswer, match="names it unloaded"):
        await runtime.trim(messages)

    assert messages == []


# ── the model ────────────────────────────────────────────────────────────────


async def test_the_cache_shares_a_client_and_keys_each_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One connection per model server, one backend per reference, one key per
    conversation — and the key is a view, not a split of the cache."""
    import slife2.libs.turn.runtime as runtime_module

    opened: list[tuple[str, str]] = []
    client = SimpleNamespace(name="client")

    async def fake_open(
        url: str,
        model: str,
        *,
        provider: str = "",
        name: str = "mcp",
        server_name: str = "",
    ) -> tuple[Any, MCPBackend]:
        opened.append((url, name))
        return client, MCPBackend(client, model, provider=provider, name=name)

    monkeypatch.setattr(runtime_module, "open_backend", fake_open)
    runtime = TurnRuntime(default_config())

    first = await runtime.model(("jack", ""), REF)
    again = await runtime.model(("jack", ""), REF)
    other = await runtime.model(("jill", ""), REF)

    assert len(opened) == 1, "one connection for one model server"
    assert first._client is again._client is other._client  # type: ignore[attr-defined]
    assert first is not again, "each conversation gets its own view"
    assert first._key == ("jack", "")  # type: ignore[attr-defined]
    assert other._key == ("jill", "")  # type: ignore[attr-defined]

    elsewhere = ModelRef("deepseek/other", "deepseek", "openai-completions", "other")
    another = await runtime.model(("jack", ""), elsewhere)
    assert len(opened) == 1, "one connection per model server, even for a second model"
    assert another._client is first._client  # type: ignore[attr-defined]
    assert another is not first, "a second reference is a second backend"
    assert another.name == elsewhere.reference  # type: ignore[attr-defined]
    assert first.name == REF.reference  # type: ignore[attr-defined]


async def test_an_injected_backend_is_returned_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test's backend has no model server to name a conversation to."""
    import slife2.libs.turn.runtime as runtime_module

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the model cache must not be reached")

    monkeypatch.setattr(runtime_module, "open_backend", refuse)
    backend = FakeBackend(text_turn("ok"))
    runtime = TurnRuntime(default_config(), backend=backend)

    assert await runtime.model(IDENTITY, REF) is backend


# ── the peers, and the loop's construction ───────────────────────────────────


async def test_each_endpoint_gets_its_own_context_and_hub_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The address is read per call, and the cache is keyed by it.**

    A shared server may serve two homes whose `servers:` blocks name different
    context or hub daemons.  One cached client would send the second home's
    turns into the first home's store; the model path already keys its clients
    by URL, and these two follow it.
    """
    import slife2.libs.turn.runtime as runtime_module

    opened: list[tuple[str, str]] = []

    async def fake_open(url: str, *, name: str, **_kwargs: Any) -> Any:
        opened.append((url, name))
        return SimpleNamespace(name=name, url=url)

    monkeypatch.setattr(runtime_module, "open_server", fake_open)

    def home(port: int) -> Any:
        return SimpleNamespace(
            server=lambda name: SimpleNamespace(url=f"http://127.0.0.1:{port}/{name}")
        )

    current = [home(1)]
    runtime = TurnRuntime(default_config(), source=lambda: current[0])

    first = await runtime.memory()
    assert first is await runtime.memory()
    first_hub = await runtime.hub()

    current[0] = home(2)
    second = await runtime.memory()
    second_hub = await runtime.hub()

    assert first is not second
    assert first_hub is not second_hub
    assert [url for url, _name in opened] == [
        "http://127.0.0.1:1/context",
        "http://127.0.0.1:1/toolhub",
        "http://127.0.0.1:2/context",
        "http://127.0.0.1:2/toolhub",
    ]
    assert [name for _url, name in opened] == [
        CONTEXT_SERVER_NAME,
        TOOLHUB_SERVER_NAME,
        CONTEXT_SERVER_NAME,
        TOOLHUB_SERVER_NAME,
    ]


async def test_close_lets_its_peers_reopen_and_leaves_injected_ones_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed client must also be forgotten, or the next session is handed
    one already shut; an injected client belongs to whoever handed it in."""
    import slife2.libs.turn.runtime as runtime_module

    opened: list[str] = []
    closed: list[Any] = []

    async def fake_open(url: str, *, name: str, **_kwargs: Any) -> Any:
        opened.append(name)
        return SimpleNamespace(name=name)

    async def fake_close(client: Any) -> None:
        closed.append(client)

    monkeypatch.setattr(runtime_module, "open_server", fake_open)
    monkeypatch.setattr(runtime_module, "close_server", fake_close)
    runtime = TurnRuntime(default_config())

    memory = await runtime.memory()
    hub = await runtime.hub()
    await runtime.close()

    assert opened == [CONTEXT_SERVER_NAME, TOOLHUB_SERVER_NAME]
    assert closed == [memory, hub]

    again = await runtime.memory()
    assert again is not memory
    assert opened.count(CONTEXT_SERVER_NAME) == 2

    injected_context = SimpleNamespace(name="injected-context")
    injected_hub = SimpleNamespace(name="injected-hub")
    given = TurnRuntime(
        default_config(),
        context_client=injected_context,
        hub_client=injected_hub,
    )
    assert await given.memory() is injected_context
    assert await given.hub() is injected_hub
    closed.clear()
    await given.close()
    assert closed == [], "an injected client is not this runtime's to close"


async def test_make_loop_opens_the_model_before_tools_and_refreshes_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The initial list and the per-call refresh are two asks.

    The agent server's first build carries no observer, so the two factories
    are deliberately distinct; the loop asks the refresh one before every model
    call.  `max_iterations` and the delivery policy travel to the loop.
    """
    import slife2.libs.turn.runtime as runtime_module

    client = SimpleNamespace(name="client")

    async def fake_open(
        url: str,
        model: str,
        *,
        provider: str = "",
        name: str = "mcp",
        server_name: str = "",
    ) -> tuple[Any, MCPBackend]:
        return client, MCPBackend(client, model, provider=provider, name=name)

    monkeypatch.setattr(runtime_module, "open_backend", fake_open)
    asked: list[str] = []

    def factory(label: str):
        async def build() -> ToolRegistry:
            asked.append(label)
            return ToolRegistry([])

        return build

    runtime = TurnRuntime(default_config(), backend=FakeBackend(text_turn("done")))
    loop = await runtime.make_loop(
        IDENTITY,
        REF,
        factory("first"),
        refresh=factory("again"),
        max_iterations=3,
    )

    assert asked == ["first"]
    result = await loop.run_turn([], "hi")
    assert result.text == "done"
    assert asked == ["first", "again"]
