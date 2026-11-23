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
from slife2.libs.plugin.protocol import UnreadableAnswer, unreadable
from slife2.libs.turn.compact import report
from slife2.libs.turn.messages import Message, Usage
from slife2.libs.turn.runtime import ModelRef, TurnRuntime, measured_tokens
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


@pytest.mark.parametrize("turn_ids", [[True], ["7"], [3.9], [None]])
async def test_restore_refuses_a_turn_id_that_is_not_one(
    turn_ids: list[Any],
) -> None:
    """A damaged id is not a turn, and it is refused before anything is adopted.

    `True` is an `int` to `isinstance` and names turn 1; `int("7")` and
    `int(3.9)` coerce rather than complain; `None` used to raise `TypeError`
    out of the restore.  Each is a store answer this build did not write, and
    an id that got in would be written back into the live-context list by the
    next publishing rebuild — the same rule `Recorder.remember_turn` and
    `select_live` keep for the ids they read.
    """
    store = Store(restore={"messages": [], "turn_ids": turn_ids, "turns": []})
    with pytest.raises(UnreadableAnswer, match="turn-id list"):
        await wired(store).restore(IDENTITY, [])


@pytest.mark.parametrize(
    "messages", [["not a message"], [{"role": "user", "content": "x"}, 7]]
)
async def test_restore_refuses_a_message_that_is_not_a_mapping(
    messages: list[Any],
) -> None:
    """A list of something else is not a short conversation.

    `Message.from_wire` reads a mapping; a string or a number in its place
    raised `AttributeError` out of the restore — an internal error where the
    sentence naming the build mismatch belongs.
    """
    store = Store(restore={"messages": messages, "turn_ids": [], "turns": []})
    with pytest.raises(UnreadableAnswer, match="message list"):
        await wired(store).restore(IDENTITY, [])


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
            "changed": True,
            "kept": 2,
            "in_hand": 2,
            "outside": 1,
            "recalled": 1,
            "fitted": 0,
            "rerank": "unavailable",
            "decision": "",
            "reason": "",
        }
    )
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="old"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="unbacked"),
    ]

    rebuilt = await wired(store).rebuild(
        IDENTITY,
        messages,
        [7, 8],
        2,
        REF.reference,
        context_tokens=1234,
        prompt_window=8192,
    )

    assert rebuilt.covered == 1, "three rebuilt messages minus the two carried"
    assert rebuilt.turn_ids == [7, 8]
    assert (
        rebuilt.changed,
        rebuilt.kept,
        rebuilt.in_hand,
        rebuilt.outside,
        rebuilt.recalled,
    ) == (True, 2, 2, 1, 1)
    assert (rebuilt.rerank, rebuilt.decision) == ("unavailable", "")
    name, arguments = store.calls[0]
    assert name == "rebuild"
    assert arguments == {
        "agent": "jack",
        "subagent": "",
        "messages": [message.to_wire() for message in messages],
        "turn_ids": [7, 8],
        "model": REF.reference,
        "carried": 2,
        "publish": True,
        "context_tokens": 1234,
        "prompt_window": 8192,
        "selection": None,
    }


async def test_rebuild_carries_the_live_ids_and_channels_back_unchanged() -> None:
    """**The hop's two derived fields survive the no-op echo.**

    `_mid` is the id a compaction names and `channel` is the bubble a folded
    cut-in draws; neither is stored and neither goes to a provider, so both are
    added to the rebuild's messages explicitly and `Message.from_wire` reads
    them back.  A hop that dropped them left the live list with no historical
    ids — a compaction could not name a saved turn — and with a cut-in's
    channel gone from the row that was next written.
    """
    echoed = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello", "_mid": "7.0"},
        {
            "role": "tool",
            "content": "cut in",
            "tool_call_id": "c1",
            "channel": "tui",
            "_mid": "7.1",
        },
    ]
    store = Store(
        rebuild={
            "messages": echoed,
            "turn_ids": [7],
            "changed": False,
            "kept": 1,
            "in_hand": 1,
            "outside": 0,
            "recalled": 0,
            "fitted": 0,
            "rerank": "",
            "decision": "",
            "reason": "",
        }
    )
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="hello", mid="7.0"),
        Message(
            role="tool",
            content="cut in",
            tool_call_id="c1",
            channel="tui",
            mid="7.1",
        ),
    ]

    rebuilt = await wired(store).rebuild(
        IDENTITY, messages, [7], 3, REF.reference, publish=False
    )

    _name, arguments = store.calls[0]
    sent = arguments["messages"]
    assert sent[1]["_mid"] == "7.0"
    assert sent[2]["channel"] == "tui", "the record's display metadata"
    assert sent[2]["_mid"] == "7.1"
    assert [message.mid for message in rebuilt.messages] == ["", "7.0", "7.1"]
    assert rebuilt.messages[2].channel == "tui"


async def test_a_private_rebuild_asks_for_no_write_and_adopts_its_own_answer() -> None:
    """An agent task decides on a clone: it publishes nothing.

    `publish=False` is the task's pre-turn work.  It asks the same question and
    reads the same answer, but the conversation's live-context list is not the
    task's to move.
    """
    store = Store(
        rebuild={
            "messages": [{"role": "system", "content": "sys"}],
            "turn_ids": [],
            "changed": True,
            "kept": 0,
            "in_hand": 0,
            "outside": 0,
            "recalled": 0,
            "fitted": 0,
            "rerank": "",
            "decision": "",
            "reason": "",
        }
    )

    rebuilt = await wired(store).rebuild(
        IDENTITY,
        [Message(role="system", content="sys")],
        [],
        0,
        REF.reference,
        publish=False,
    )

    name, arguments = store.calls[0]
    assert name == "rebuild"
    assert arguments["publish"] is False
    assert rebuilt.turn_ids == [], "the task adopts the clone's own decision"


@pytest.mark.parametrize(
    ("answer", "sentence"),
    [
        (
            {
                "turn_ids": [],
                "changed": True,
                "kept": 0,
                "in_hand": 0,
                "outside": 0,
                "recalled": 0,
                "fitted": 0,
                "rerank": "",
                "decision": "",
            },
            "message list",
        ),
        (
            {
                "messages": [],
                "changed": True,
                "kept": 0,
                "in_hand": 0,
                "outside": 0,
                "recalled": 0,
                "fitted": 0,
                "rerank": "",
                "decision": "",
            },
            "turn-id list",
        ),
        (
            {"messages": [], "turn_ids": [], "changed": True},
            "counts",
        ),
        (
            {
                "messages": [],
                "turn_ids": [],
                "changed": True,
                "kept": 0,
                "recalled": 0,
                "fitted": 0,
                "rerank": "",
                "decision": "",
            },
            "counts",
        ),
        (
            {
                "messages": [],
                "turn_ids": [],
                "changed": True,
                "kept": 0,
                "in_hand": 0,
                "recalled": 0,
                "fitted": 0,
                "rerank": "",
                "decision": "",
            },
            "counts",
        ),
        (
            {
                "messages": [],
                "turn_ids": [],
                "changed": True,
                "kept": 0,
                "in_hand": 0,
                "outside": 0,
                "recalled": 0,
                "fitted": 0,
            },
            "rerank status",
        ),
        (
            {
                "messages": [],
                "turn_ids": [],
                "changed": True,
                "kept": 0,
                "in_hand": 0,
                "outside": 0,
                "recalled": 0,
                "fitted": 0,
                "rerank": "",
            },
            "decision's status",
        ),
    ],
)
async def test_rebuild_refuses_an_answer_it_cannot_read(
    answer: dict[str, Any], sentence: str
) -> None:
    """A reply missing the counts is not a rebuild of zero of each."""
    with pytest.raises(UnreadableAnswer, match=sentence):
        await wired(Store(rebuild=answer)).rebuild(IDENTITY, [], [], 0, REF.reference)


@pytest.mark.parametrize("turn_ids", [[True], ["8"], [3.9]])
async def test_rebuild_refuses_a_turn_id_that_is_not_one(
    turn_ids: list[Any],
) -> None:
    """The rebuilt list is an id list too, and read the same way as a restore's."""
    answer = {
        "messages": [],
        "turn_ids": turn_ids,
        "changed": True,
        "kept": 0,
        "in_hand": 0,
        "outside": 0,
        "recalled": 0,
        "fitted": 0,
        "rerank": "",
        "decision": "",
    }
    with pytest.raises(UnreadableAnswer, match="turn-id list"):
        await wired(Store(rebuild=answer)).rebuild(IDENTITY, [], [], 0, REF.reference)


@pytest.mark.parametrize(("kept", "recalled"), [(True, 0), ("2", 1), (0, 2.0)])
async def test_rebuild_refuses_counts_that_are_not_ints(
    kept: Any, recalled: Any
) -> None:
    """A count is the note's whole content; a coercion is a figure nobody gave.

    `True` would draw `kept 1`, a numeric string would read as the number it
    spells, and a float is not a count of turns — the store writes ints, so
    anything else is another build speaking.
    """
    answer = {
        "messages": [],
        "turn_ids": [],
        "changed": True,
        "kept": kept,
        "in_hand": 0,
        "outside": 0,
        "recalled": recalled,
        "fitted": 0,
        "rerank": "",
        "decision": "",
    }
    with pytest.raises(UnreadableAnswer, match="counts"):
        await wired(Store(rebuild=answer)).rebuild(IDENTITY, [], [], 0, REF.reference)


async def test_a_malformed_rebuild_leaves_the_callers_list_alone() -> None:
    """Everything is read before anything is adopted.

    The store may have replaced its list, but this turn did not take the
    answer: the list the caller holds is still the list it held.  The store's
    list is what a restart replays; until then, this loop and the store may
    disagree about a decision this build could not read.
    """
    store = Store(
        rebuild={
            "messages": [{"role": "user", "content": "other"}],
            "turn_ids": [1],
            "changed": True,
        }
    )
    messages = [Message(role="system", content="sys")]

    with pytest.raises(UnreadableAnswer, match="counts"):
        await wired(store).rebuild(IDENTITY, messages, [], 0, REF.reference)

    assert messages == [Message(role="system", content="sys")]


# ── the live compaction ──────────────────────────────────────────────────────


def a_view() -> list[Message]:
    """A conversation with one saved turn behind the turn running."""
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="old question", mid="6.0"),
        Message(role="assistant", content="old answer", mid="6.1"),
        Message(role="user", content="now"),
        Message(role="assistant", content="still here"),
    ]


async def test_a_model_selection_is_read_by_the_selector_parser() -> None:
    """The model's own call and the selector's answer are one shape, one read."""
    hidden: set[str] = set()

    result = await wired(Store()).compact_turn(
        IDENTITY,
        a_view(),
        hidden,
        REF.reference,
        8192,
        selection=["6.0"],
    )

    assert (result.dropped, result.refused) == (1, "")
    assert hidden == {"6.0"}


async def test_a_selection_naming_an_id_the_view_does_not_offer_is_refused() -> None:
    """Filtering would apply half a selection; the whole answer is refused.

    A fixed message and an id no build wrote are ids the view did not offer; the
    caller turns the refusal into a tool error for the model's own call.
    """
    hidden: set[str] = set()

    result = await wired(Store()).compact_turn(
        IDENTITY,
        a_view(),
        hidden,
        REF.reference,
        8192,
        selection=["6.0", "9.9"],
    )

    assert result.dropped == 0
    assert "does not offer" in result.refused
    assert hidden == set(), "nothing named is applied when part cannot be"


async def test_an_already_hidden_id_is_not_an_error() -> None:
    """A model repeating a cumulative list is asking for the same view."""
    hidden = {"6.0"}

    result = await wired(Store()).compact_turn(
        IDENTITY,
        a_view(),
        hidden,
        REF.reference,
        8192,
        selection=["6.0"],
    )

    assert (result.dropped, result.refused) == (0, "")


async def test_a_malformed_model_selection_is_refused_as_unreadable() -> None:
    """A string is iterable; reading it as ids would hide the wrong messages."""
    hidden: set[str] = set()

    result = await wired(Store()).compact_turn(
        IDENTITY,
        a_view(),
        hidden,
        REF.reference,
        8192,
        selection="6.1",
    )

    assert result.dropped == 0
    assert "could not be read" in result.refused
    assert hidden == set()


async def test_a_compaction_with_no_selection_asks_the_selector() -> None:
    """The direct-caller seam: no selection, one `select_live` call.

    Both servers ask the selector before the call, so this is the context
    plugin's asking path kept for a caller that holds no selection — the same
    seam `rebuild` keeps.  The answer is read by `parse_live`, the model's own
    call's parser.
    """
    store = Store(select_live={"arguments": '{"compact": ["6.0"]}', "decision": ""})
    hidden: set[str] = set()

    result = await wired(store).compact_turn(
        IDENTITY, a_view(), hidden, REF.reference, 8192
    )

    assert (result.dropped, result.decision, result.refused) == (1, "", "")
    assert hidden == {"6.0"}
    name, arguments = store.calls[0]
    assert name == "select_live"
    assert arguments["selectable"] == ["6.0"]


async def test_a_selector_failure_is_reported_and_nothing_is_hidden() -> None:
    """A provider failure is a decision's absence, not an empty answer."""
    store = Store(select_live={"arguments": "", "decision": "RuntimeError: down"})
    hidden: set[str] = set()

    result = await wired(store).compact_turn(
        IDENTITY, a_view(), hidden, REF.reference, 8192
    )

    assert (result.dropped, result.decision) == (0, "RuntimeError: down")
    assert hidden == set()
    assert "did not answer" in report(result)


async def test_the_automatic_arguments_carry_a_failure_as_its_own_words() -> None:
    """One sentence for both servers: the failure is the tool's argument.

    The tool body refuses what it cannot read, and the reason the agent reads is
    the provider's own, not a status word.
    """
    runtime = wired(
        Store(select_live={"arguments": "", "decision": "RuntimeError: down"})
    )
    assert await runtime.compact_arguments(
        IDENTITY, a_view(), ["6.0"], REF.reference, prompt_window=1024
    ) == {"compact": "the selector did not answer: RuntimeError: down"}

    runtime = wired(Store(decide={"arguments": "", "decision": "TimeoutError: slow"}))
    assert await runtime.rebuild_arguments(
        IDENTITY, a_view(), REF.reference, prompt_window=1024
    ) == {"mode": "the decision model did not answer: TimeoutError: slow"}


class Down(Store):
    """A context plugin whose tool call never comes back."""

    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self.failure = failure

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise self.failure


async def test_a_decision_that_cannot_be_called_is_a_failure_selection() -> None:
    """A transport failure keeps the context; the turn is not failed."""
    runtime = TurnRuntime(
        default_config(), context_client=Down(RuntimeError("the provider is down"))
    )

    assert await runtime.rebuild_arguments(IDENTITY, a_view(), REF.reference) == {
        "mode": "the decision call failed: RuntimeError: the provider is down"
    }
    assert await runtime.compact_arguments(
        IDENTITY, a_view(), ["6.0"], REF.reference
    ) == {"compact": "the selector call failed: RuntimeError: the provider is down"}


async def test_an_unreadable_answer_fails_the_turn_rather_than_becoming_arguments() -> (
    None
):
    """A store from another build is a build mismatch, not a provider failure.

    Reading it as a decision would keep the context for a reason nobody can
    see; `UnreadableAnswer` is the type that fails the turn (SDD Appendix C).
    """
    failure = unreadable("the context store", "return the decision's arguments", {})
    runtime = TurnRuntime(default_config(), context_client=Down(failure))

    with pytest.raises(UnreadableAnswer):
        await runtime.rebuild_arguments(IDENTITY, a_view(), REF.reference)
    with pytest.raises(UnreadableAnswer):
        await runtime.compact_arguments(IDENTITY, a_view(), ["6.0"], REF.reference)


async def test_the_context_figure_falls_back_to_the_saved_measurement() -> None:
    """A turn with no completed call yet has no figure of its own."""
    live = SimpleNamespace(last_usage=Usage(prompt_tokens=300, completion_tokens=50))
    empty = SimpleNamespace(last_usage=Usage())

    assert measured_tokens(live, 999) == 350
    assert measured_tokens(empty, 999) == 999
    assert measured_tokens(None, 999) == 999


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
