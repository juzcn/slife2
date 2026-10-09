"""slife2-context over the wire.

Two halves, and they are tested together because they are one plugin: the **turn
log** — v1's `memdb`, the tools that came over from `slife2-db` when that plugin
stopped being one — and the **two decisions about it**, `restore` and `rebuild`,
which is what makes this the thing that manages a conversation's context.

The tools are thin and that thinness is exactly what a payload mismatch hides
behind.  FastMCP validates the arguments it is handed, so the names the agent
server sends and the names the tools accept have to be checked against each
other rather than assumed.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.audience import client_meta
from slife2.config import default_config
from slife2.context_server import build_server
from slife2.db import MAX_PAGE, TurnStore
from slife2.paths import DATA_ENV_VAR, db_dir
from tests.fakes import StubEmbedder

pytestmark = pytest.mark.unit

#: The store embeds every turn it writes, so driving it needs an embedder.  A
#: stub rather than an endpoint: what these tests are about is the plugin's own
#: behaviour, and a real call would make them about somebody else's service
#: being up.
EMBEDDER = StubEmbedder()

SYSTEM = {"role": "system", "content": "You are jack."}


def _exchange(question: str, answer: str) -> list[dict]:
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]


class Replies:
    """A discriminator that answers with whatever it was told to.

    The seam is the *decision*, not the transport, so what a test varies is what
    the model said — which is the only thing a rebuild does with the answer.
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.asked: list[str] = []

    async def __call__(
        self, agent: str, subagent: str, model: str, messages: list, prompt: str
    ) -> str:
        self.asked.append(prompt)
        return self.replies.pop(0) if self.replies else "{}"


async def _seed(client: Client, agent: str, *exchanges: tuple[str, str]) -> list[int]:
    """Write those turns under `agent`, and answer with their ids."""
    written: list[int] = []
    for question, answer in exchanges:
        stored = await client.call_tool(
            "remember", {"agent": agent, "messages": _exchange(question, answer)}
        )
        written.append(stored.data["turn_id"])
    return written


# --- the turn log, which is v1's memdb ----------------------------------------


@pytest.mark.asyncio
async def test_a_turn_goes_in_and_comes_back(tmp_path, monkeypatch) -> None:
    """The write and the read are one contract, so they are tested as one."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    messages = [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": "It is 42."},
    ]

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        stored = await client.call_tool(
            "remember",
            {
                "agent": "jack",
                "messages": messages,
                "token_count": 245,
                "context_tokens": 135,
                "who_helped": "jack",
                "what_model": "deepseek/deepseek-flash",
            },
        )
        read = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=client_meta("jack")
        )

    assert stored.data["turn_id"] == 1
    record = read.data
    assert record["turn_id"] == 1
    assert record["messages"] == messages
    assert record["who_helped"] == "jack"
    assert record["what_model"] == "deepseek/deepseek-flash"
    assert (record["token_count"], record["context_tokens"]) == (245, 135)
    assert record["created_at"] and record["completed_at"]


@pytest.mark.asyncio
async def test_a_saved_turn_joins_the_live_context(tmp_path, monkeypatch) -> None:
    """The append is the store's, inside the turn's own transaction.

    Which is what makes the list trustworthy: a turn that is stored but not in
    the list is a turn the next restore silently drops, and the window in which
    that is true is exactly the window between two transactions.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        first = await client.call_tool(
            "remember", {"agent": "jack", "messages": _exchange("q0", "a0")}
        )
        await client.call_tool(
            "remember", {"agent": "jack", "messages": _exchange("q1", "a1")}
        )

    assert first.data["turn_id"] == 1
    store = TurnStore(db_dir() / "jack.turn.db")
    assert store.context_turns() == [1, 2]


@pytest.mark.asyncio
async def test_an_agent_name_that_cannot_be_a_file_is_refused(
    tmp_path, monkeypatch
) -> None:
    """The one refusal the agent server tells apart from a dead transport.

    `server.py` reads a `ToolError` as "this one request, from this one caller"
    and keeps talking to the store; anything else it reads as the transport being
    gone.  That distinction only holds if a bad name really does come back as a
    `ToolError` rather than as a dropped connection.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("remember", {"agent": "..", "messages": []})


@pytest.mark.asyncio
async def test_the_model_tools_name_no_agent(tmp_path, monkeypatch) -> None:
    """The schema is the boundary, not the system prompt.

    An `agent` argument would make "read my history" into "read anybody's", with
    nothing between the model and somebody else's turns but a sentence it was
    told to obey.  The identity reaches the server on the call instead
    (`slife2.audience`), so there is no argument to get wrong — and this is the
    check that none comes back.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("turn_list", "turn_read"):
        assert "agent" not in tools[name].input_schema["properties"]
        assert "subagent" not in tools[name].input_schema["properties"]
    # And the ones the agent side calls still take it: `remember`, `restore` and
    # `rebuild` are called by code that knows whose conversation it is running.
    for name in ("remember", "restore", "rebuild", "forget"):
        assert "agent" in tools[name].input_schema["properties"]


@pytest.mark.asyncio
async def test_none_of_the_harness_tools_is_the_models(tmp_path, monkeypatch) -> None:
    """The audience mark, and which side of it each tool is on.

    A model may read its own history and may not decide what its context is: the
    footnote on each rebuilt turn is how it says what it wants kept, and nothing
    it can call changes the selection.  That is a claim about the marks rather
    than about the code, so it is asserted here.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    for name in ("turn_list", "turn_read"):
        assert listed[name].meta.get("slife2/audience") == "assistant"
    for name in ("remember", "restore", "rebuild", "forget"):
        assert "slife2/audience" not in (listed[name].meta or {})


@pytest.mark.asyncio
async def test_a_model_reads_the_history_it_is_calling_from(
    tmp_path, monkeypatch
) -> None:
    """Two conversations, one server, and neither sees the other's turns."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        for agent, question in (("jack", "jack's question"), ("jill", "jill's question")):
            await client.call_tool(
                "remember", {"agent": agent, "messages": _exchange(question, "…")}
            )
        # A third id, on the same server: `subagent` is part of the identity
        # rather than a label on it, and it names a file of its own.
        await client.call_tool(
            "remember",
            {
                "agent": "jill",
                "subagent": "worker",
                "messages": _exchange("the worker's question", "…"),
            },
        )

        jack = await client.call_tool("turn_list", {}, meta=client_meta("jack"))
        jill = await client.call_tool("turn_list", {}, meta=client_meta("jill"))
        # Turn 1 exists in all three files, and is a different turn in each.
        read = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=client_meta("jack")
        )
        scoped = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=client_meta("jill", "worker")
        )

    assert [e["user_message"] for e in jack.data["entries"]] == ["jack's question"]
    assert [e["user_message"] for e in jill.data["entries"]] == ["jill's question"]
    assert read.data["messages"][0]["content"] == "jack's question"
    assert scoped.data["messages"][0]["content"] == "the worker's question"


@pytest.mark.asyncio
async def test_a_call_that_says_nobody_is_refused(tmp_path, monkeypatch) -> None:
    """Better told than quietly served: the hub forwards an identity it was
    given, so a call arriving without one did not come through the hub."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="did not say whose"):
            await client.call_tool("turn_list", {})


@pytest.mark.asyncio
async def test_browsing_pages_and_reports_a_total(tmp_path, monkeypatch) -> None:
    """What the model gets back for a page: four fields a turn and the count."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("q0", "a0"), ("q1", "a1"), ("q2", "a2"))
        page = await client.call_tool(
            "turn_list", {"limit": 2, "offset": 1}, meta=client_meta("jack")
        )

    assert page.data["total"] == 3
    assert (page.data["limit"], page.data["offset"]) == (2, 1)
    assert [e["user_message"] for e in page.data["entries"]] == ["q1", "q0"]
    assert [e["assistant_message"] for e in page.data["entries"]] == ["a1", "a0"]


@pytest.mark.asyncio
async def test_the_page_reports_the_limit_that_built_it(tmp_path, monkeypatch) -> None:
    """`limit` in the answer is the capped one, not the one asked for.

    The docstring tells a model to page with `offset + len(entries) < total`,
    and that arithmetic only holds against the size the page was actually built
    with: answering a request for 1000 with `limit: 1000` and two hundred rows
    makes the next request skip the eight hundred in between.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("q0", "a0"))
        page = await client.call_tool(
            "turn_list", {"limit": 1000}, meta=client_meta("jack")
        )

    assert page.data["total"] == 1
    assert page.data["limit"] == MAX_PAGE
    assert len(page.data["entries"]) == 1


@pytest.mark.asyncio
async def test_a_bound_the_grammar_does_not_know_is_a_refusal(
    tmp_path, monkeypatch
) -> None:
    """A `ToolError` the model reads and corrects, not an empty page."""
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="invalid since bound"):
            await client.call_tool(
                "turn_list", {"since": "whenever"}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_reading_a_turn_that_is_not_there_says_which_one(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="no turn 7"):
            await client.call_tool("turn_read", {"turn_id": 7}, meta=client_meta("jack"))


# --- restore: the exit-time context, replayed ---------------------------------


@pytest.mark.asyncio
async def test_a_new_conversation_restores_its_own_head(tmp_path, monkeypatch) -> None:
    """Nothing recorded yet: the caller gets back exactly what it sent.

    The honest answer for a key that is genuinely new, and the one that keeps
    `restore` from needing a branch at every call site in the agent server.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        answer = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )

    assert answer.data == {"messages": [SYSTEM], "turn_ids": []}


@pytest.mark.asyncio
async def test_a_restore_replays_the_list_and_names_its_turns(
    tmp_path, monkeypatch
) -> None:
    """The context at exit, in the list's order, with a footnote per turn.

    The footnote is how a turn id reaches the model at all, and therefore how a
    keep-list is expressible: the ids it writes back are the ids it read here.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        ids = await _seed(client, "jack", ("first", "one"), ("second", "two"))
        answer = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )

    messages = answer.data["messages"]
    assert answer.data["turn_ids"] == ids
    assert messages[0] == SYSTEM
    assert [m["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert messages[1]["content"].startswith("first")
    assert json.loads(messages[1]["content"].split("[INFO: ")[1].rstrip("]"))[
        "turn_id"
    ] == ids[0]


@pytest.mark.asyncio
async def test_a_restore_keeps_the_lists_order_and_not_the_rowids(
    tmp_path, monkeypatch
) -> None:
    """A selection need not be contiguous, so the list *is* the ordering.

    Written by hand rather than by a rebuild, because this is about what a read
    does with a list it did not produce: replaying it as it stands is the whole
    contract, and a read that re-sorted would silently undo every recall.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        ids = await _seed(client, "jack", ("first", "1"), ("second", "2"))
        TurnStore(db_dir() / "jack.turn.db").set_context_turns(list(reversed(ids)))
        answer = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )

    said = [m["content"] for m in answer.data["messages"] if m["role"] == "user"]
    assert said[0].startswith("second")
    assert said[1].startswith("first")
    assert answer.data["turn_ids"] == list(reversed(ids))


@pytest.mark.asyncio
async def test_a_restore_drops_a_turn_that_is_no_longer_stored(
    tmp_path, monkeypatch
) -> None:
    """The answer is what the log can produce, and the caller adopts it.

    A list naming a row that is gone is a file that was rebuilt or pruned
    underneath, and a restore that refused to run over one absent id would turn
    a partial loss into a total one.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("kept", "yes"))
        TurnStore(db_dir() / "jack.turn.db").set_context_turns([1, 404])
        answer = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )

    assert answer.data["turn_ids"] == [1]
    assert len(answer.data["messages"]) == 3


@pytest.mark.asyncio
async def test_a_restore_repairs_an_orphaned_tool_call(tmp_path, monkeypatch) -> None:
    """The one history shape every provider rejects, fixed where it can appear.

    A tool call with no result is a 400, and the repair is idempotent — running
    it on a healthy list changes nothing — which is what makes it safe to run
    unconditionally rather than only where a problem is suspected.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await client.call_tool(
            "remember",
            {
                "agent": "jack",
                "messages": [
                    {"role": "user", "content": "run it"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "now", "arguments": "{}"},
                            }
                        ],
                    },
                ],
            },
        )
        answer = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )

    roles = [m["role"] for m in answer.data["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert answer.data["messages"][-1]["tool_call_id"] == "call_1"
    assert "interrupted" in answer.data["messages"][-1]["content"]


# --- rebuild: keep ∪ recall, one model call ------------------------------------


async def _rebuild(client: Client, reply: str, **overrides):
    return await client.call_tool(
        "rebuild",
        {
            "agent": "jack",
            "messages": [SYSTEM, {"role": "user", "content": "one"},
                         {"role": "assistant", "content": "1"}],
            "turn_ids": [1],
            "prompt": "and then?",
            **overrides,
        },
    )


@pytest.mark.asyncio
async def test_a_decision_to_keep_everything_rebuilds_nothing(
    tmp_path, monkeypatch
) -> None:
    """The common case, and the one that keeps a costly call from being free.

    `{}` means the turns in hand are enough, so nothing is fetched, nothing is
    rendered and nothing is written — the answer is the caller's own list,
    handed back.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    hold = [SYSTEM, {"role": "user", "content": "one"}]

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies("{}"))
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": hold,
                "turn_ids": [1],
                "prompt": "and then?",
            },
        )

    assert answer.data["changed"] is False
    assert answer.data["messages"] == hold
    assert answer.data["turn_ids"] == [1]
    assert TurnStore(db_dir() / "jack.turn.db").context_turns() == [1]


@pytest.mark.asyncio
async def test_a_clear_empties_the_context_and_is_published(
    tmp_path, monkeypatch
) -> None:
    """`"clear"` is one of the three things `context` can say, and it means none.

    The system prompt survives it — that is the head, not a turn — and the store
    is written, because a clear that only happened in memory is a restart away
    from not having happened.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(
        build_server(
            default_config(), embedder=EMBEDDER, ask=Replies('{"context": "clear"}')
        )
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [SYSTEM, {"role": "user", "content": "one"}],
                "turn_ids": [1],
                "prompt": "ignore all that",
            },
        )

    assert answer.data["messages"] == [SYSTEM]
    assert answer.data["turn_ids"] == []
    assert answer.data["changed"] is True
    assert TurnStore(db_dir() / "jack.turn.db").context_turns() == []


@pytest.mark.asyncio
async def test_a_keep_list_intersects_with_what_is_in_hand(
    tmp_path, monkeypatch
) -> None:
    """A model that names a turn it can no longer see has made a mistake.

    There is no turn to refuse it with, so the id is dropped rather than obeyed
    — keeping a turn the conversation does not have would mean fetching one from
    outside the selection, which is a recall wearing a keep-list's clothes.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(
        build_server(
            default_config(),
            embedder=EMBEDDER,
            ask=Replies('{"context": [2, 99]}'),
        )
    ) as client:
        await _seed(client, "jack", ("one", "1"), ("two", "2"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [
                    SYSTEM,
                    {"role": "user", "content": "one"},
                    {"role": "user", "content": "two"},
                ],
                "turn_ids": [1, 2],
                "prompt": "the second one",
            },
        )

    assert answer.data["turn_ids"] == [2]
    said = [m["content"] for m in answer.data["messages"] if m["role"] == "user"]
    assert len(said) == 1
    assert said[0].startswith("two"), said[0]


@pytest.mark.asyncio
async def test_a_recall_adds_to_what_was_kept(tmp_path, monkeypatch) -> None:
    """The union, which is what makes "keep this and add that" expressible.

    Joined by id and sorted, so a turn both kept and recalled is one turn and
    the answer is always in time order.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(
        build_server(
            default_config(),
            embedder=EMBEDDER,
            ask=Replies('{"recall": {"query": "the very first"}}'),
        )
    ) as client:
        ids = await _seed(
            client, "jack", ("the very first thing", "1"), ("something else", "2")
        )
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [
                    SYSTEM,
                    {"role": "user", "content": "something else"},
                    {"role": "assistant", "content": "2"},
                ],
                "turn_ids": [ids[1]],
                "prompt": "what did we start with?",
            },
        )

    assert answer.data["turn_ids"] == ids
    assert answer.data["recalled"] == 1


@pytest.mark.asyncio
async def test_an_unreadable_reply_keeps_the_context(tmp_path, monkeypatch) -> None:
    """Degrading, not retrying: the fallback is perfectly good.

    A timeout, a provider failure and a reply that is not the requested object
    all arrive as the same answer, and the price of guessing wrong is a context
    nobody decided on.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    hold = [SYSTEM, {"role": "user", "content": "one"}]

    async with Client(
        build_server(
            default_config(), embedder=EMBEDDER, ask=Replies("I could not decide.")
        )
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        answer = await client.call_tool(
            "rebuild",
            {"agent": "jack", "messages": hold, "turn_ids": [1], "prompt": "hi"},
        )

    assert answer.data["changed"] is False
    assert answer.data["messages"] == hold


@pytest.mark.asyncio
async def test_the_carried_tail_survives_a_rebuild(tmp_path, monkeypatch) -> None:
    """Messages no turn accounts for are put back verbatim.

    A cancelled turn leaves the user's own message in the list with no row
    behind it, and a rebuild that replaced the list wholesale would delete it —
    silently, and only for the user whose turn was interrupted.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    tail = {"role": "user", "content": "a message nobody answered"}

    async with Client(
        build_server(
            default_config(), embedder=EMBEDDER, ask=Replies('{"context": "clear"}')
        )
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [SYSTEM, {"role": "user", "content": "one"}, tail],
                "turn_ids": [1],
                "prompt": "still there?",
                "carried": 1,
            },
        )

    assert answer.data["messages"] == [SYSTEM, tail]


@pytest.mark.asyncio
async def test_the_rebuild_can_be_turned_off(tmp_path, monkeypatch) -> None:
    """The switch an operator on a metered endpoint wants.

    Off, the turn runs on the context it already has and the discriminator is
    never called — which is the append-only mode v1 keeps behind the same flag.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    from dataclasses import replace

    from slife2.config import ContextSettings

    config = replace(default_config(), context=ContextSettings(rebuild=False))
    replies = Replies('{"context": "clear"}')
    hold = [SYSTEM, {"role": "user", "content": "one"}]

    async with Client(
        build_server(config, embedder=EMBEDDER, ask=replies)
    ) as client:
        answer = await client.call_tool(
            "rebuild",
            {"agent": "jack", "messages": hold, "turn_ids": [1], "prompt": "hi"},
        )

    assert answer.data["changed"] is False
    assert replies.asked == [], "the model was asked anyway"


@pytest.mark.asyncio
async def test_forget_clears_the_context_and_keeps_the_turns(
    tmp_path, monkeypatch
) -> None:
    """A reset must not restore the conversation it just forgot.

    Dropping the in-memory loop is not enough any more, because the next message
    under that key restores — so the list is cleared and the log is left alone.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("one", "1"))
        forgotten = await client.call_tool("forget", {"agent": "jack"})
        restored = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )
        still_readable = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=client_meta("jack")
        )

    assert forgotten.data == {"turn_ids": []}
    assert restored.data == {"messages": [SYSTEM], "turn_ids": []}
    assert still_readable.data["messages"][0]["content"] == "one"


# --- startup: the index is brought up to date with the model ------------------


@pytest.mark.asyncio
async def test_startup_leaves_a_current_index_alone(tmp_path, monkeypatch) -> None:
    """The ordinary start costs nothing: the identity matches, so no embedding.

    Worth asserting because it is the difference between a server that starts in
    milliseconds and one that re-embeds a history every time it is launched.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    store = TurnStore(db_dir() / "jack.turn.db")
    for said in ("工具", "trump"):
        await store.save_turn(
            messages=[{"role": "user", "content": said}], embedder=EMBEDDER
        )

    again = StubEmbedder()
    async with Client(build_server(default_config(), embedder=again)):
        pass

    assert again.calls == [], "a current index was rebuilt anyway"


@pytest.mark.asyncio
async def test_startup_reindexes_every_turn_when_the_model_changed(
    tmp_path, monkeypatch
) -> None:
    """The cost of changing the embedding model, paid once at start.

    Every turn has to be embedded again — vectors from two models cannot be
    ranked against each other — and the thing that decides it is the identity
    recorded in the file, not anything remembered across restarts.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    first = StubEmbedder()
    store = TurnStore(db_dir() / "jack.turn.db")
    for said in ("工具", "trump", "792"):
        await store.save_turn(
            messages=[{"role": "user", "content": said}], embedder=first
        )

    second = StubEmbedder(identity="stub:two")
    async with Client(build_server(default_config(), embedder=second)):
        pass

    assert sum(len(call) for call in second.calls) == 3
    assert store._turns_without_vectors() == []
    assert store.index_status(second)["ready"]
    with sqlite3.connect(store.path) as connection:
        recorded = connection.execute(
            "SELECT value FROM index_meta WHERE key = 'vector_identity'"
        ).fetchone()[0]
    assert recorded.startswith("1|stub:two|"), recorded
