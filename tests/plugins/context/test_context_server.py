"""slife2-context over the wire.

Two halves, and they are tested together because they are one plugin: the **turn
log** — the tools that came over from `slife2-db` when that plugin stopped
being one — and the **two decisions about it**, `restore` and `rebuild`,
which is what makes this the thing that manages a conversation's context.

The tools are thin and that thinness is exactly what a payload mismatch hides
behind.  FastMCP validates the arguments it is handed, so the names the agent
server sends and the names the tools accept have to be checked against each
other rather than assumed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import slife2.plugins.context.server as context_server
from slife2.libs.host.config import default_config
from slife2.libs.host.paths import agent_dir
from slife2.libs.plugin.audience import client_meta
from slife2.libs.store.db import EMBED_TEXT_VERSION, TurnStore
from slife2.libs.store.index import MAX_PAGE
from slife2.libs.turn.context import turn_footnote, turn_note
from slife2.libs.turn.messages import Usage
from slife2.libs.turn.tokens import estimate_turn_tokens
from slife2.plugins.context.server import _ask_model, build_server
from tests.fakes import StubEmbedder

pytestmark = pytest.mark.unit

#: The logger every assertion about a reason below reads.
CONTEXT_LOGGER = "slife2.plugins.context.server"

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


# --- the turn log -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_goes_in_and_comes_back() -> None:
    """The write and the read are one contract, so they are tested as one."""
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
                "usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 45,
                    "cache_read": 150,
                    "cache_creation": 20,
                },
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
    assert record["what_model"] == "deepseek/deepseek-flash"
    # The read answers with the turn's own size, not the bill it cost or the
    # context it ran in; the stored row keeps both for the status bar.
    assert record["token_count"] == estimate_turn_tokens(messages)
    assert "context_tokens" not in record
    assert "who_helped" not in record
    assert record["created_at"] and record["completed_at"]
    # The bill's split is a fact about the turn that ran, so a detached read
    # keeps it — unlike the context size, which belongs to a context this read
    # does not return.
    assert record["usage"] == {
        "prompt_tokens": 200,
        "completion_tokens": 45,
        "cache_read": 150,
        "cache_creation": 20,
    }
    stored_row = TurnStore(agent_dir("jack") / "turns.db").turn(1)
    assert (
        stored_row.who_helped,
        stored_row.token_count,
        stored_row.context_tokens,
    ) == ("jack", 245, 135)
    assert stored_row.usage == Usage(
        prompt_tokens=200, completion_tokens=45, cache_read=150, cache_creation=20
    )


@pytest.mark.asyncio
async def test_a_saved_turn_joins_the_live_context() -> None:
    """The append is the store's, inside the turn's own transaction.

    Which is what makes the list trustworthy: a turn that is stored but not in
    the list is a turn the next restore silently drops, and the window in which
    that is true is exactly the window between two transactions.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        first = await client.call_tool(
            "remember", {"agent": "jack", "messages": _exchange("q0", "a0")}
        )
        await client.call_tool(
            "remember", {"agent": "jack", "messages": _exchange("q1", "a1")}
        )

    assert first.data["turn_id"] == 1
    store = TurnStore(agent_dir("jack") / "turns.db")
    assert store.context_turns() == [1, 2]


@pytest.mark.asyncio
async def test_an_agent_name_that_cannot_be_a_file_is_refused() -> None:
    """The one refusal the agent server tells apart from a dead transport.

    `server.py` reads a `ToolError` as "this one request, from this one caller"
    and keeps talking to the store; anything else it reads as the transport being
    gone.  That distinction only holds if a bad name really does come back as a
    `ToolError` rather than as a dropped connection.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("remember", {"agent": "..", "messages": []})


@pytest.mark.asyncio
async def test_the_model_tools_name_no_agent() -> None:
    """The schema is the boundary, not the system prompt.

    An `agent` argument would make "read my history" into "read anybody's", with
    nothing between the model and somebody else's turns but a sentence it was
    told to obey.  The identity reaches the server on the call instead
    (`slife2.libs.plugin.audience`), so there is no argument to get wrong — and this is the
    check that none comes back.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("turn_list", "turn_search", "turn_summarize", "turn_read"):
        assert "agent" not in tools[name].input_schema["properties"]
        assert "subagent" not in tools[name].input_schema["properties"]
    # And the ones the agent side calls still take it: `remember`, `restore` and
    # `rebuild` are called by code that knows whose conversation it is running.
    for name in ("remember", "restore", "rebuild", "forget"):
        assert "agent" in tools[name].input_schema["properties"]


@pytest.mark.asyncio
async def test_none_of_the_harness_tools_is_the_models() -> None:
    """The audience mark, and which side of it each tool is on.

    A model may read its own history and may not decide what its context is: the
    footnote on each rebuilt turn is how it says what it wants kept, and nothing
    it can call changes the selection.  That is a claim about the marks rather
    than about the code, so it is asserted here.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    for name in ("turn_list", "turn_search", "turn_summarize", "turn_read"):
        assert listed[name].meta.get("slife2/audience") == "assistant"
    for name in ("remember", "restore", "rebuild", "forget"):
        assert "slife2/audience" not in (listed[name].meta or {})


@pytest.mark.asyncio
async def test_a_model_reads_the_history_it_is_calling_from() -> None:
    """Two conversations, one server, and neither sees the other's turns."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        for agent, question in (
            ("jack", "jack's question"),
            ("jill", "jill's question"),
        ):
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
async def test_a_call_that_says_nobody_is_refused() -> None:
    """Better told than quietly served: the hub forwards an identity it was
    given, so a call arriving without one did not come through the hub.

    **And the sentence is the house one**, with this plugin's own tail — a call
    that named nobody, on a process that serves every agent, is the situation
    `slife2.libs.plugin.audience.UNADDRESSED` is one sentence with two holes for.
    What is this server's is the *why*: the road that always carries an identity
    is the hub, so a reader who is told that knows where to look.
    """
    from slife2.libs.plugin.audience import UNADDRESSED

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("turn_list", {})

    said = str(caught.value)
    assert UNADDRESSED.split("{")[0] in said, said
    assert "the toolhub" in said, said


@pytest.mark.asyncio
async def test_browsing_pages_and_reports_a_total() -> None:
    """What the model gets back for a page: four fields a turn and the count."""

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
async def test_the_page_reports_the_limit_that_built_it() -> None:
    """`limit` in the answer is the capped one, not the one asked for.

    The docstring tells a model to page with `offset + len(entries) < total`,
    and that arithmetic only holds against the size the page was actually built
    with: answering a request for 1000 with `limit: 1000` and two hundred rows
    makes the next request skip the eight hundred in between.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("q0", "a0"))
        page = await client.call_tool(
            "turn_list", {"limit": 1000}, meta=client_meta("jack")
        )

    assert page.data["total"] == 1
    assert page.data["limit"] == MAX_PAGE
    assert len(page.data["entries"]) == 1


@pytest.mark.asyncio
async def test_a_bound_the_grammar_does_not_know_is_a_refusal() -> None:
    """A `ToolError` the model reads and corrects, not an empty page."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="invalid since bound"):
            await client.call_tool(
                "turn_list", {"since": "whenever"}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_reading_a_turn_that_is_not_there_says_which_one() -> None:

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="no turn 7"):
            await client.call_tool(
                "turn_read", {"turn_id": 7}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_search_answers_with_the_browse_line_and_a_score() -> None:
    """One hit is `turn_list`'s line plus the meaning score, and nothing else.

    The line is the contract: a model that has read `turn_list` already knows
    what it is looking at, and `turn_read` takes the `turn_id` either tool gave.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(
            client,
            "jack",
            ("the trump rally", "a0"),
            ("unrelated chatter", "a1"),
        )
        found = await client.call_tool(
            "turn_search", {"query": "trump"}, meta=client_meta("jack")
        )

    entries = found.data["entries"]
    assert entries[0]["user_message"] == "the trump rally"
    assert entries[0]["similarity"] > 0
    assert set(entries[0]) == {
        "turn_id",
        "created_at",
        "user_message",
        "assistant_message",
        "token_count",
        "similarity",
    }
    assert found.data["limit"] == 10


@pytest.mark.asyncio
async def test_search_reads_only_the_callers_conversation() -> None:
    """The same boundary `turn_list` has: no argument names whose memory."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("jack's trump note", "a"))
        await _seed(client, "jill", ("jill's trump note", "a"))
        found = await client.call_tool(
            "turn_search", {"query": "trump"}, meta=client_meta("jack")
        )

    assert [e["user_message"] for e in found.data["entries"]] == ["jack's trump note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "***", "   "])
async def test_a_search_with_nothing_to_look_for_points_at_the_browse(
    query: str,
) -> None:
    """An empty query is not a browse; `turn_list` is."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="turn_list"):
            await client.call_tool(
                "turn_search", {"query": query}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_search_takes_sentences_as_well_as_words() -> None:
    """**The same two spellings the rebuild's recall has.**

    A sentence is what the meaning leg wants; the words are what the keyword
    leg wants.  A model that has only a sentence should not have to reduce it to
    a keyword phrase that the AND will then refuse.
    """
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("工具 工具", "1"), ("trump", "2"))
        answer = await client.call_tool(
            "turn_search", {"sentences": ["工具"]}, meta=client_meta("jack")
        )

    assert answer.data["entries"][0]["turn_id"] == 1


@pytest.mark.asyncio
async def test_search_honours_the_same_time_window_as_the_browse() -> None:
    """`since`/`until` reach the ranking, not only the listing."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await client.call_tool(
            "remember",
            {
                "agent": "jack",
                "messages": _exchange("old trump note", "a"),
                "created_at": "2020-01-01T00:00:00",
            },
        )
        await _seed(client, "jack", ("new trump note", "a"))
        found = await client.call_tool(
            "turn_search",
            {"query": "trump", "since": "2025-01-01"},
            meta=client_meta("jack"),
        )

    assert [e["user_message"] for e in found.data["entries"]] == ["new trump note"]


@pytest.mark.asyncio
async def test_search_finds_the_topic_a_person_names_in_chinese() -> None:
    """“昨天讨论的首经贸发展问题，我们再继续” has to reach that turn.

    The keyword leg splits CJK into characters (`textindex.normalize`) and the
    meaning leg embeds the same text, so a Chinese topic is one query rather
    than a translation step. `since` is the bound the sentence itself names.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(
            client,
            "jack",
            ("昨天讨论的首经贸发展问题", "我们决定先看数据。"),
            ("无关的闲聊", "好的。"),
        )
        found = await client.call_tool(
            "turn_search",
            {"query": "首经贸发展", "since": "yesterday"},
            meta=client_meta("jack"),
        )

    assert [e["user_message"] for e in found.data["entries"]][0] == (
        "昨天讨论的首经贸发展问题"
    )


@pytest.mark.asyncio
async def test_a_summary_makes_an_old_turn_findable_by_its_words() -> None:
    """`turn_summarize` rewrites the keyword index, not just the row."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("q0", "a0"))
        wrote = await client.call_tool(
            "turn_summarize",
            {"turn_id": 1, "summary": "the quarterly forecast", "tags": "planning"},
            meta=client_meta("jack"),
        )
        read = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=client_meta("jack")
        )
        found = await client.call_tool(
            "turn_search", {"query": "quarterly"}, meta=client_meta("jack")
        )

    assert wrote.data == {"status": "updated", "turn_id": 1}
    assert (read.data["summary"], read.data["tags"]) == (
        "the quarterly forecast",
        "planning",
    )
    assert [e["turn_id"] for e in found.data["entries"]][0] == 1


@pytest.mark.asyncio
async def test_the_current_turns_annotation_is_captured_not_written() -> None:
    """No id means the turn being run; it has no row yet to write."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        wrote = await client.call_tool(
            "turn_summarize",
            {"summary": "the current turn", "tags": "now"},
            meta=client_meta("jack"),
        )
        # Nothing was stored, so the id cannot resolve.
        with pytest.raises(ToolError, match="no turn 1"):
            await client.call_tool(
                "turn_read", {"turn_id": 1}, meta=client_meta("jack")
            )

    assert wrote.data["status"] == "captured"
    assert wrote.data["turn_id"] is None


@pytest.mark.asyncio
async def test_a_summary_with_nothing_to_write_is_refused() -> None:
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("q0", "a0"))
        with pytest.raises(ToolError, match="summary and/or tags"):
            await client.call_tool(
                "turn_summarize", {"turn_id": 1}, meta=client_meta("jack")
            )


@pytest.mark.asyncio
async def test_summarizing_a_turn_that_is_not_there_says_which_one() -> None:
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="no turn 7"):
            await client.call_tool(
                "turn_summarize",
                {"turn_id": 7, "summary": "nowhere"},
                meta=client_meta("jack"),
            )


# --- restore: the exit-time context, replayed ---------------------------------


@pytest.mark.asyncio
async def test_a_new_conversation_restores_its_own_head() -> None:
    """Nothing recorded yet: the caller gets back exactly what it sent.

    The honest answer for a key that is genuinely new, and the one that keeps
    `restore` from needing a branch at every call site in the agent server.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        answer = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )

    assert answer.data == {"messages": [SYSTEM], "turn_ids": [], "turns": []}


@pytest.mark.asyncio
async def test_a_restore_replays_the_list_and_names_its_turns() -> None:
    """The context at exit, in the list's order, with a footnote per turn.

    The footnote is how a turn id reaches the model at all, and therefore how a
    keep-list is expressible: the ids it writes back are the ids it read here.
    """

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
    assert (
        json.loads(messages[1]["content"].split("[TURN:")[1].rstrip("]"))["turn_id"]
        == ids[0]
    )


def test_the_footnote_a_screen_shows_is_the_one_the_model_reads() -> None:
    """One function, two readers, and the marker is the whole difference.

    A window that shows a turn's id beside a message is showing the id a
    keep-list will be written with, so the two spellings are one function with
    the envelope applied and not two that have to be kept in step.

    The marker says `TURN` because this build has one thing to put in that
    envelope: which turn a message opened.  `channel` is the
    second thing in it, and it is what an id alone cannot say — whose turn it is.
    """
    when = "2026-08-10T14:03:00+08:00"
    ended = "2026-08-10T14:05:00+08:00"

    assert turn_note(12, when, ended, "tui") == (
        f"[TURN:{turn_footnote(12, when, ended, 'tui')}]"
    )
    assert json.loads(turn_footnote(12, when, ended, "tui")) == {
        "turn_id": 12,
        "channel": "tui",
        "begin": "2026-08-10 14:03",
        # The same day, so the date is not repeated.
        "end": "14:05",
    }
    # With nothing to say but the id, which is the shape a seeded turn has, and
    # an absent channel rather than an empty one — no caller meant "unknown".
    assert turn_footnote(12, "", None) == '{"turn_id": 12}'


@pytest.mark.asyncio
async def test_a_restore_keeps_the_lists_order_and_not_the_rowids() -> None:
    """A selection need not be contiguous, so the list *is* the ordering.

    Written by hand rather than by a rebuild, because this is about what a read
    does with a list it did not produce: replaying it as it stands is the whole
    contract, and a read that re-sorted would silently undo every recall.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        ids = await _seed(client, "jack", ("first", "1"), ("second", "2"))
        TurnStore(agent_dir("jack") / "turns.db").set_context_turns(list(reversed(ids)))
        answer = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )

    said = [m["content"] for m in answer.data["messages"] if m["role"] == "user"]
    assert said[0].startswith("second")
    assert said[1].startswith("first")
    assert answer.data["turn_ids"] == list(reversed(ids))


@pytest.mark.asyncio
async def test_a_restore_drops_a_turn_that_is_no_longer_stored() -> None:
    """The answer is what the log can produce, and the caller adopts it.

    A list naming a row that is gone is a file that was rebuilt or pruned
    underneath, and a restore that refused to run over one absent id would turn
    a partial loss into a total one.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("kept", "yes"))
        TurnStore(agent_dir("jack") / "turns.db").set_context_turns([1, 404])
        answer = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )

    assert answer.data["turn_ids"] == [1]
    assert len(answer.data["messages"]) == 3


@pytest.mark.asyncio
async def test_a_restore_repairs_an_orphaned_tool_call() -> None:
    """The one history shape every provider rejects, fixed where it can appear.

    A tool call with no result is a 400, and the repair is idempotent — running
    it on a healthy list changes nothing — which is what makes it safe to run
    unconditionally rather than only where a problem is suspected.
    """

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
            "messages": [
                SYSTEM,
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "1"},
            ],
            "turn_ids": [1],
            "prompt": "and then?",
            **overrides,
        },
    )


@pytest.mark.asyncio
async def test_the_rebuild_uses_the_callers_homes_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared context daemon serves every home, and models are per home.

    The discriminator's model reference comes from the caller's conversation,
    but the daemon resolved it against the config it started with — a
    `ConfigError` in a second home, swallowed as "keep the context", so recall
    never ran there.
    """
    seen: list[Any] = []

    async def capture(config, *args, **kwargs) -> str:
        seen.append(config)
        return "{}"

    monkeypatch.setattr(context_server, "_ask_model", capture)
    mine, callers = default_config(), default_config()
    server = build_server(mine, embedder=EMBEDDER, source=lambda: callers)

    async with Client(server) as client:
        await _seed(client, "jack", ("one", "1"))
        await _rebuild(client, "{}")

    assert seen, "the rebuild asked the model"
    assert seen[0] is callers, "and asked with the caller's config"
    assert seen[0] is not mine


@pytest.mark.asyncio
async def test_a_decision_to_keep_everything_rebuilds_nothing() -> None:
    """The common case, and the one that keeps a costly call from being free.

    `{}` means the turns in hand are enough, so nothing is fetched, nothing is
    rendered and nothing is written — the answer is the caller's own list,
    handed back.
    """
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
    assert TurnStore(agent_dir("jack") / "turns.db").context_turns() == [1]


@pytest.mark.asyncio
async def test_a_clear_empties_the_context_and_is_published() -> None:
    """`"clear"` is one of the three things `context` can say, and it means none.

    The system prompt survives it — that is the head, not a turn — and the store
    is written, because a clear that only happened in memory is a restart away
    from not having happened.
    """

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
    assert TurnStore(agent_dir("jack") / "turns.db").context_turns() == []


@pytest.mark.asyncio
async def test_a_keep_list_intersects_with_what_is_in_hand() -> None:
    """A model that names a turn it can no longer see has made a mistake.

    There is no turn to refuse it with, so the id is dropped rather than obeyed
    — keeping a turn the conversation does not have would mean fetching one from
    outside the selection, which is a recall wearing a keep-list's clothes.
    """

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
async def test_a_recall_adds_to_what_was_kept() -> None:
    """The union, which is what makes "keep this and add that" expressible.

    Joined by id and sorted, so a turn both kept and recalled is one turn and
    the answer is always in time order.
    """

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
async def test_a_recall_does_not_bring_back_a_turn_already_kept() -> None:
    """Keep ∪ recall is joined by id, so a kept turn must not spend a recall place.

    `recall_limit` is the place count, and the kept turn ranks first here: with
    the exclusion the *next* turn comes back, which is the one the decision is
    actually asking for.  Without it the recall answers with the turn already in
    hand and the rebuild changes nothing.
    """
    config = default_config()
    config = replace(config, context=replace(config.context, recall_limit=1))
    reply = '{"context": [1], "recall": {"query": "工具"}}'

    async with Client(
        build_server(config, embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", ("工具 工具", "1"), ("工具 trump", "2"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [
                    SYSTEM,
                    {"role": "user", "content": "工具 工具"},
                    {"role": "assistant", "content": "1"},
                ],
                "turn_ids": [ids[0]],
                "prompt": "那另一条呢？",
            },
        )

    assert answer.data["turn_ids"] == ids
    assert answer.data["recalled"] == 1


@pytest.mark.asyncio
async def test_a_sentence_recall_also_asks_with_the_persons_input() -> None:
    """**The model's sentence is a paraphrase; the input is what it paraphrases.**

    A recall asked in sentences gets at least two questions — the sentence and
    the person's own words — so a paraphrase that misses cannot be the only way
    the subject is found.  The input is not in the message list yet; `rebuild`
    carries it separately for exactly this reason.
    """
    reply = '{"context": "clear", "recall": {"sentences": ["trump"]}}'

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", ("工具", "1"), ("trump", "2"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [SYSTEM],
                "turn_ids": [],
                "prompt": "工具",
            },
        )

    assert answer.data["turn_ids"] == ids
    assert answer.data["recalled"] == 2


@pytest.mark.asyncio
async def test_a_sentence_recall_can_drop_the_input_from_the_questions() -> None:
    """The model sees the input and the conversation; a greeting names nothing.

    `prompt_as_sentence: false` is the decision saying the sentence is the only
    question — what makes "hi" or "continue" safe beside a subject the
    conversation carries.
    """
    reply = (
        '{"context": "clear", "recall": {"sentences": ["trump"],'
        ' "prompt_as_sentence": false}}'
    )

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", ("工具", "1"), ("trump", "2"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [SYSTEM],
                "turn_ids": [],
                "prompt": "工具",
            },
        )

    assert answer.data["turn_ids"] == [ids[1]]
    assert answer.data["recalled"] == 1


@pytest.mark.asyncio
async def test_a_keyword_recall_keeps_the_query_as_its_one_meaning_question() -> None:
    """The input is not added beside a keyword phrase.

    A query is already the meaning fallback; the keyword phrase is the
    deliberate question, and a second raw question there can only reorder it.
    The phrase is what both halves read; the input is not a third question.
    """
    reply = '{"context": "clear", "recall": {"query": "trump"}}'

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", ("工具", "1"), ("trump", "2"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [SYSTEM],
                "turn_ids": [],
                "prompt": "工具",
            },
        )

    assert answer.data["turn_ids"] == [ids[1]]
    assert answer.data["recalled"] == 1


@pytest.mark.asyncio
async def test_a_period_recall_is_not_narrowed_by_the_input() -> None:
    """A period asks for a time, not a subject; the input does not filter it."""
    reply = '{"context": "clear", "recall": {"since": "yesterday"}}'

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", ("工具", "1"), ("trump", "2"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [SYSTEM],
                "turn_ids": [],
                "prompt": "工具",
            },
        )

    assert answer.data["turn_ids"] == ids
    assert answer.data["recalled"] == 2


@pytest.mark.asyncio
async def test_a_recall_runs_on_a_sentence_when_the_query_has_no_term() -> None:
    """**The words can fail without taking the question with them.**

    Dropping the whole recall because the *query* had no term was right while one
    string served both legs: with a period beside it, the recall would otherwise
    have run as a period recall.  A sentence is a question of its own, so only
    the unusable words are dropped and the meaning half is answered.
    """
    reply = '{"recall": {"query": "***", "sentences": ["the very first thing"]}}'

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
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
async def test_a_context_list_that_cannot_be_written_fails_the_turn(
    monkeypatch,
) -> None:
    """The list is authoritative, so a publish that did not land must not pass.

    The in-memory rebuild used to be returned with only a warning while the
    *persisted* list — the state a restart replays — did not move, so the next
    start came back one turn short, silently.  The caller adopts this answer as
    its context, so returning it while the write failed leaves the two
    disagreeing; the write is now unguarded and the failure reaches the caller
    (SDD §5.5: the list is authoritative).  The old guard did not even buy
    the tolerance it named — a busy or full disk raised out of `rebuild` anyway.
    """

    def refuse(self, turn_ids, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(TurnStore, "set_context_turns", refuse)

    async with Client(
        build_server(
            default_config(),
            embedder=EMBEDDER,
            ask=Replies('{"context": "clear"}'),
        )
    ) as client:
        ids = await _seed(client, "jack", ("one", "1"))
        with pytest.raises(ToolError, match="database is locked"):
            await client.call_tool(
                "rebuild",
                {
                    "agent": "jack",
                    "messages": [
                        SYSTEM,
                        {"role": "user", "content": "one"},
                        {"role": "assistant", "content": "1"},
                    ],
                    "turn_ids": ids,
                    "prompt": "hi",
                },
            )


@pytest.mark.asyncio
async def test_a_rebuild_that_lost_the_conversation_keeps_the_newer_list() -> None:
    """**A rebuild is a model call between a read and a write, and the writers
    are in different processes.**

    The agent server's turn and an agent task of the same agent both rebuild one
    conversation; the turn also saves while the task is deciding.  The decision
    is composed against the list `restore` answered with, so writing it
    unconditionally after the save erased the new turn from the live context —
    the row stayed in the log and the next restore no longer knew it, which is
    the silent loss `append_context` prevents on the save path.

    `expected` is the snapshot, and the write is a compare-and-set: the newer
    list stands and the caller keeps what it holds.  What is asserted here is
    both halves — the decision did not overwrite, and the turn saved while it ran
    is still in the context.
    """
    parked = asyncio.Event()
    release = asyncio.Event()

    async def deciding(
        agent: str, subagent: str, model: str, messages: list, prompt: str
    ) -> str:
        parked.set()
        await release.wait()
        return '{"context": [1]}'

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=deciding)
    ) as client:
        ids = await _seed(client, "jack", ("zero", "0"), ("one", "1"))
        answer = asyncio.create_task(
            client.call_tool(
                "rebuild",
                {
                    "agent": "jack",
                    "messages": [
                        SYSTEM,
                        {"role": "user", "content": "zero"},
                        {"role": "assistant", "content": "0"},
                        {"role": "user", "content": "one"},
                        {"role": "assistant", "content": "1"},
                    ],
                    "turn_ids": ids,
                    # What `restore` answered and what the store held when this
                    # caller took its snapshot; the write matches only while
                    # the conversation still holds exactly this.
                    "expected": ids,
                    "prompt": "and then?",
                },
            )
        )
        await asyncio.wait_for(parked.wait(), timeout=5)
        # The other writer: a turn of the agent's own, saved while the
        # discriminator is deciding.  The store appends it in its own
        # transaction.
        saved = await client.call_tool(
            "remember",
            {"agent": "jack", "messages": _exchange("two", "2")},
        )
        release.set()
        rebuilt = await answer

    assert rebuilt.data["changed"] is True, "the newer store list is what it adopted"
    assert rebuilt.data["turn_ids"] == [ids[0], ids[1], saved.data["turn_id"]]
    assert TurnStore(agent_dir("jack") / "turns.db").context_turns() == [
        ids[0],
        ids[1],
        saved.data["turn_id"],
    ], "the turn saved while the decision ran is still the conversation"
    said = [
        message["content"]
        for message in rebuilt.data["messages"]
        if message["role"] == "user"
    ]
    assert said[-1].startswith("two"), "the turn it had not seen is in its context"


@pytest.mark.asyncio
async def test_a_recall_query_with_no_term_is_dropped_rather_than_widened() -> None:
    """A query with no indexable term used to be turned into no query at all.

    With an `anchor` (or a `since`/`until`) beside it, that ran the recall as a
    *period* recall — a different selection from the one the model asked for, and
    a much larger one when the anchor named the oldest turns.  The whole recall
    is dropped instead, which is SDD §5.5's outcome when a rebuild cannot do its job:
    the decision's keep-list runs on its own and the context it had stands.
    """
    hold = [SYSTEM, {"role": "user", "content": "two"}]
    reply = '{"recall": {"query": "***", "anchor": "oldest"}}'

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", ("one", "1"), ("two", "2"))
        answer = await client.call_tool(
            "rebuild",
            {"agent": "jack", "messages": hold, "turn_ids": [ids[1]], "prompt": "hi"},
        )

    assert answer.data["changed"] is False
    assert answer.data["messages"] == hold
    assert answer.data["turn_ids"] == [ids[1]]


@pytest.mark.asyncio
async def test_a_model_the_config_cannot_resolve_is_loud_rather_than_sized_by_count() -> (
    None
):
    """`except Exception` around `config.resolve` could not tell a declared-absent
    window from a broken config.

    A model the config does not have is a *failure*, not a zero: folding the two
    together silently dropped the token cap and let a recall spend by count — the
    overfill the cap exists to stop.  A window nobody *declared* still keeps the
    count cap; a window that cannot be asked for is loud (SDD Appendix C: a
    weaker path is not a substitute for a missing capability).
    """

    async with Client(
        build_server(
            default_config(),
            embedder=EMBEDDER,
            ask=Replies('{"recall": {"query": "one"}}'),
        )
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        with pytest.raises(ToolError, match="no-such-provider"):
            await client.call_tool(
                "rebuild",
                {
                    "agent": "jack",
                    "messages": [SYSTEM, {"role": "user", "content": "one"}],
                    "turn_ids": [1],
                    "prompt": "hi",
                    "model": "no-such-provider/model",
                },
            )


@pytest.mark.asyncio
async def test_an_unreadable_reply_keeps_the_context() -> None:
    """Degrading, not retrying: the fallback is perfectly good.

    A timeout, a provider failure and a reply that is not the requested object
    all arrive as the same answer, and the price of guessing wrong is a context
    nobody decided on.
    """
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
async def test_a_recall_bound_the_grammar_does_not_know_is_dropped() -> None:
    """The same fallback as an unreadable reply, and for the same reason.

    A reply of the right *shape* whose `since` is in no grammar this build speaks
    — `whenever`, `sometime last year` — is a boundary case the two halves of the
    system answer differently: a bound the *model* passes to `turn_list` is a
    `ToolError` it reads and corrects (the test above), and a bound the
    *discriminator* writes is not, because nothing is left to read a refusal.
    Handed to the store it would raise from inside a query and fail the turn, so
    the reply is dropped where dropping is still something a caller can decide —
    and the fallback is the same perfectly good one.
    """
    hold = [SYSTEM, {"role": "user", "content": "one"}]
    reply = '{"context": "clear", "recall": {"query": "one", "since": "whenever"}}'

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        answer = await client.call_tool(
            "rebuild",
            {"agent": "jack", "messages": hold, "turn_ids": [1], "prompt": "hi"},
        )

    assert answer.data["changed"] is False
    assert answer.data["messages"] == hold


@pytest.mark.asyncio
async def test_the_carried_tail_survives_a_rebuild() -> None:
    """Messages no turn accounts for are put back verbatim.

    A cancelled turn leaves the user's own message in the list with no row
    behind it, and a rebuild that replaced the list wholesale would delete it —
    silently, and only for the user whose turn was interrupted.
    """
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
async def test_forget_clears_the_context_and_keeps_the_turns() -> None:
    """A reset must not restore the conversation it just forgot.

    Dropping the in-memory loop is not enough any more, because the next message
    under that key restores — so the list is cleared and the log is left alone.
    """

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
    assert restored.data == {"messages": [SYSTEM], "turn_ids": [], "turns": []}
    assert still_readable.data["messages"][0]["content"] == "one"


# --- startup: the index is brought up to date with the model ------------------


@pytest.mark.asyncio
async def test_startup_leaves_a_current_index_alone() -> None:
    """The ordinary start costs nothing: the identity matches, so no embedding.

    Worth asserting because it is the difference between a server that starts in
    milliseconds and one that re-embeds a history every time it is launched.
    """
    store = TurnStore(agent_dir("jack") / "turns.db")
    for said in ("工具", "trump"):
        await store.save_turn(
            messages=[{"role": "user", "content": said}], embedder=EMBEDDER
        )

    again = StubEmbedder()
    async with Client(build_server(default_config(), embedder=again)):
        pass

    assert again.calls == [], "a current index was rebuilt anyway"


@pytest.mark.asyncio
async def test_startup_reindexes_every_turn_when_the_model_changed() -> None:
    """The cost of changing the embedding model, paid once at start.

    Every turn has to be embedded again — vectors from two models cannot be
    ranked against each other — and the thing that decides it is the identity
    recorded in the file, not anything remembered across restarts.
    """
    first = StubEmbedder()
    store = TurnStore(agent_dir("jack") / "turns.db")
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
    assert recorded.startswith(f"{EMBED_TEXT_VERSION}|1|stub:two|"), recorded


@pytest.mark.asyncio
async def test_startup_survives_a_conversation_from_an_old_schema() -> None:
    """One unreadable conversation does not take the readable ones down.

    The startup pass scans **every known home** (`turn_dbs`), so letting a file
    from an older build stop it would refuse the whole plugin — every
    conversation, in every home — over one nobody may be using.  The pass names
    the file and carries on; what still fails is a call that actually uses it,
    and it fails with that same sentence.

    Served instead, this is the failure that was seen: `restore` died per row
    as SQLite's `No item with that key`, naming neither the file nor the
    remedy.
    """
    stale = agent_dir("jack") / "turns.db"
    with sqlite3.connect(stale) as connection:
        connection.execute("CREATE TABLE turn (messages TEXT NOT NULL DEFAULT '[]')")

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        # A conversation whose file is current is served normally.
        await client.call_tool(
            "remember",
            {"agent": "someone", "messages": [{"role": "user", "content": "hi"}]},
        )
        with pytest.raises(ToolError) as refused:
            await client.call_tool("restore", {"agent": "jack", "messages": []})

    assert str(stale) in str(refused.value)
    assert "summary" in str(refused.value)


def test_a_footnote_does_not_trim_the_message_it_is_added_to() -> None:
    """**The strip was for the empty case and reached the person's text.**

    `f"{content} {note}".strip()` strips the *whole* string, and the only end it
    could reach was the message's own leading whitespace — so a turn that opened
    with an indented block or a blank line was stored as it was written and read
    back by the model with that whitespace gone.  The row in the transcript and
    the message in the request were not the same message.
    """
    from slife2.libs.turn.context import with_note

    assert with_note("    indented\nsecond line", "[TURN:{}]").startswith(
        "    indented"
    )
    # The empty case is the one it is for: a turn with no text reads as the note.
    assert with_note("", "[TURN:{}]") == "[TURN:{}]"


def test_a_keep_list_of_stringly_ids_is_coerced_rather_than_refused() -> None:
    """**The one source `normalise_ids` exists for, and `parse` refused it.**

    `_keep` tested `isinstance(item, int)`, so a model that wrote `["1"]` — JSON
    of whatever the encoder wrote, which is the source `normalise_ids`'s own
    docstring names — made every id resolve to `REJECTED`.  `parse` then answered
    `None` and dropped the *other* half of the decision with it, so a valid
    `recall` beside the keep-list was ignored in silence.  What an id is, is
    `normalise_ids`'s to decide, and the two now read as one rule.
    """
    from slife2.libs.turn.context import Decision, parse

    assert parse('{"context": ["2", 1]}') == Decision(keep=[2, 1])
    # The whole reply survives, which is what "both halves" means.
    assert parse('{"context": ["1"], "recall": {"query": "the very first"}}') == (
        Decision(
            keep=[1],
            recall={
                "query": "the very first",
                "sentences": None,
                "prompt_as_sentence": None,
                "since": None,
                "until": None,
                "anchor": None,
            },
        )
    )
    # And a value that is not an id is still a reply this build does not obey —
    # including `1e999`, valid JSON that arrives as `inf`, whose `int()` raises.
    for not_an_id in (
        '{"context": ["clear"]}',
        '{"context": [null]}',
        '{"context": [true]}',
        '{"context": [1.5]}',
        '{"context": [1e999]}',
    ):
        assert parse(not_an_id) is None, not_an_id


def test_a_recall_carries_sentences_as_separate_questions() -> None:
    """**The meaning half has a shape of its own.**  `query` is the words the
    keyword leg ANDs; a sentence has to go to the meaning leg alone, because a
    six-word sentence asked for exactly matches nothing.  Blank sentences are
    nothing asked, and a value that is not a list of strings is a reply the
    build does not obey."""
    from slife2.libs.turn.context import Decision, parse

    assert parse(
        '{"recall": {"sentences": ["the dean", "the anniversary"]}}'
    ) == Decision(
        recall={
            "query": None,
            "sentences": ["the dean", "the anniversary"],
            "prompt_as_sentence": None,
            "since": None,
            "until": None,
            "anchor": None,
        }
    )
    blank = parse('{"recall": {"sentences": ["  "]}}')
    assert blank is not None and blank.recall is None
    for not_sentences in (
        '{"recall": {"sentences": "the dean"}}',
        '{"recall": {"sentences": [1]}}',
        '{"recall": {"sentences": [null]}}',
    ):
        assert parse(not_sentences) is None, not_sentences


def test_a_recall_can_say_the_input_is_not_a_question() -> None:
    """**The model sees the input and the conversation; this judgement is its.**

    The input is read beside the sentences by default.  A greeting or a
    follow-up names nothing, so the decision can say so — the flag alone is not
    a question, and a value that is not a boolean is a reply this build does
    not obey.
    """
    from slife2.libs.turn.context import Decision, parse

    assert parse(
        '{"recall": {"sentences": ["the other school"], "prompt_as_sentence": false}}'
    ) == Decision(
        recall={
            "query": None,
            "sentences": ["the other school"],
            "prompt_as_sentence": False,
            "since": None,
            "until": None,
            "anchor": None,
        }
    )
    for flag_only in (
        '{"recall": {"prompt_as_sentence": false}}',
        '{"recall": {"prompt_as_sentence": true}}',
    ):
        decision = parse(flag_only)
        assert decision is not None and decision.recall is None
    for not_a_bool in (
        '{"recall": {"prompt_as_sentence": "false"}}',
        '{"recall": {"prompt_as_sentence": 0}}',
    ):
        assert parse(not_a_bool) is None, not_a_bool


# --- the four ways a rebuild keeps the context, and what the log says ---------


@pytest.mark.asyncio
async def test_the_four_ways_a_rebuild_keeps_the_context_are_four_reasons(
    caplog,
) -> None:
    """**Four ways to keep the context, and the log may not fold any two of them.**

    `rebuild`'s `_in_hand` is one answer for four situations — a discriminator
    that decided nothing, a decision that asks for nothing, a decision that
    equals what is held, and a selection that cannot be fetched.  The line it
    writes is the only sight anybody gets of the discriminator, and it reported
    the first as the second: "no decision at all" was logged
    `reason=context_sufficient`, so every timeout and every unreadable reply —
    each named on the line above with its own reason — counted as the model
    having decided the context was enough.  Whoever counts decisions by that
    word was counting failures.

    The docstring named a fifth exit that does not exist, "the switch is off":
    `ContextSettings` has no such field and nothing here reads one, so the four
    below are the whole of it.
    """
    caplog.set_level(logging.INFO, logger=CONTEXT_LOGGER)
    hold = [SYSTEM, {"role": "user", "content": "one"}]
    # One server, one scripted discriminator, four calls: an unreadable reply,
    # a decision that asks for nothing, a decision equal to what is held, and a
    # keep-list naming a turn the log does not have.
    replies = Replies(
        "I could not decide.", "{}", '{"context": [1]}', '{"context": [99]}'
    )
    answers = []
    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=replies)
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        answers.append(
            await client.call_tool(
                "rebuild",
                {"agent": "jack", "messages": hold, "turn_ids": [1], "prompt": "hi"},
            )
        )
        answers.append(
            await client.call_tool(
                "rebuild",
                {"agent": "jack", "messages": hold, "turn_ids": [1], "prompt": "hi"},
            )
        )
        answers.append(
            await client.call_tool(
                "rebuild",
                {"agent": "jack", "messages": hold, "turn_ids": [1], "prompt": "hi"},
            )
        )
        answers.append(
            await client.call_tool(
                "rebuild",
                {
                    "agent": "jack",
                    # 99 is in the caller's list and nowhere in the log, which
                    # is the disagreement this exit exists for.
                    "messages": [SYSTEM, {"role": "user", "content": "one"}],
                    "turn_ids": [1, 99],
                    "prompt": "hi",
                },
            )
        )

    for answer in answers:
        assert answer.data["changed"] is False, "nothing was fetched or written"
        assert answer.data["recalled"] == 0
    said = [record.getMessage() for record in caplog.records]
    assert "recall_discriminator_unparsed" in said, "the failure is named"
    assert "recall_not_needed reason=no_decision" in said, "and it is not a decision"
    assert "recall_not_needed reason=context_sufficient" in said
    assert "recall_not_needed reason=context_unchanged" in said
    assert "recall_abandoned reason=unfetchable" in said
    # It stayed [1] throughout: `_in_hand` is the answer that writes nothing.
    assert TurnStore(agent_dir("jack") / "turns.db").context_turns() == [1]


# --- the transport, which the discriminator cannot see from where it sits ------


class NoStructuredContent:
    """A model server from another build: it answers with no wire format.

    FastMCP leaves `CallToolResult.data` as `None` whenever the far end sent no
    structured content, which is exactly what this returns — `self`, whose
    `data` is `None`.
    """

    data: Any = None

    async def call_tool(self, *_args: Any, **_kwargs: Any) -> Any:
        return self


@pytest.mark.asyncio
async def test_a_model_answer_carrying_nothing_is_refused_not_read_as_empty() -> None:
    """**An unreadable answer is not an empty one**, on this hop too.

    `StreamChatResult.from_wire(None)` builds a result with no text, so a model
    server from another build reached `_discriminate` as "the model answered
    with nothing": it logged `recall_discriminator_unparsed` and kept the
    context, and so did every rebuild for the rest of the session — pointing
    whoever read the log at the model's output rather than at the transport.
    `slife2.clients.model` raises for this on the loop's own hop, and this is
    the same check with the same sentence.
    """
    config = default_config()
    _, provider, _ = config.resolve("")
    # Seeded, so no client is ever opened: what is under test is the shape of
    # the answer, and a real dial would make the test about somebody's daemon.
    clients: dict[str, Any] = {config.server(provider.api).url: NoStructuredContent()}

    with pytest.raises(ConnectionError, match="another build"):
        await _ask_model(
            config, clients, asyncio.Lock(), "jack", "", "", [], "and then?"
        )
