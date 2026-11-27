"""slife2-context over the wire.

Two halves, and they are tested together because they are one plugin: the **turn
log** tools, and the **two decisions about it**, `restore` and `rebuild`,
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
from slife2.helpers.timeutil import BOUND_GRAMMAR
from slife2.libs.host.config import default_config, load_cached
from slife2.libs.host.paths import agent_dir, register_home, subagent_path
from slife2.libs.plugin.audience import conversation_meta
from slife2.libs.store.db import EMBED_TEXT_VERSION, TurnStore
from slife2.libs.store.index import MAX_PAGE
from slife2.libs.turn.context import (
    LIVE_REPLY,
    RECALL_REPLY,
    turn_footnote,
    turn_note,
)
from slife2.libs.turn.messages import StreamChatResult, ToolCall, Usage
from slife2.libs.turn.tools import (
    CONTEXT_REBUILD,
    CONTEXT_REBUILD_TEXT,
    TURN_COMPACT,
    TURN_COMPACT_TEXT,
)
from slife2.plugins.context.server import _ask_model, build_server
from tests.fakes import StubEmbedder, config_file

pytestmark = pytest.mark.unit

#: The logger every assertion about a reason below reads.
CONTEXT_LOGGER = "slife2.plugins.context.server"

#: The store embeds every turn it writes, so driving it needs an embedder.  A
#: stub rather than an endpoint: what these tests are about is the plugin's own
#: behaviour, and a real call would make them about somebody else's service
#: being up.
EMBEDDER = StubEmbedder()


class RerankingEmbedder(StubEmbedder):
    """The stub embedder with a cross-encoder that scores by one marker.

    The document a reranked turn carries is its exchange (`_rerank_text`), so
    a turn whose text says "high" scores 1.0 and every other candidate 0.1.
    `fail` is the optional stage failing, which must be the same answer as a
    provider that serves no reranker: keep the fused order.
    """

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.asked: list[tuple[str, list[str]]] = []

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        self.asked.append((query, list(documents)))
        if self.fail:
            raise RuntimeError("the reranker is down")
        scored = [
            (index, 1.0 if "high" in document else 0.1)
            for index, document in enumerate(documents)
        ]
        return sorted(scored, key=lambda pair: -pair[1])


class GradedReranker(StubEmbedder):
    """A cross-encoder with the two score bands the shipped floor separates.

    The live log's real turns scored 0.69–0.81 and the keyword incidentals
    0.0007–0.006; `0.8` and `0.05` are those bands, so the default
    `rerank_min_score` (0.3) keeps one and drops the other.
    """

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[tuple[str, list[str]]] = []

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        self.asked.append((query, list(documents)))
        scored = [
            (index, 0.8 if "real" in document else 0.05)
            for index, document in enumerate(documents)
        ]
        return sorted(scored, key=lambda pair: -pair[1])


SYSTEM = {"role": "system", "content": "You are jack."}


def _exchange(question: str, answer: str) -> list[dict]:
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]


class Replies:
    """A decision model that answers with whatever it was told to.

    The seam is the *decision*, not the transport, so what a test varies is what
    the model said.  There is one call and no repair: an answer the tool cannot
    use is the tool's error result, which the agent reads.
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.asked: list[str] = []

    async def __call__(
        self,
        agent: str,
        subagent: str,
        model: str,
        messages: list,
        *,
        usage: str = "",
    ) -> str:
        self.asked.append(usage)
        return (
            self.replies.pop(0)
            if self.replies
            else '{"keep": {"action": "keep"}, "recall": {}}'
        )


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
            "turn_read", {"turn_id": 1}, meta=conversation_meta("jack")
        )

    assert stored.data["turn_id"] == 1
    record = read.data
    assert record["turn_id"] == 1
    assert record["messages"] == messages
    assert record["what_model"] == "deepseek/deepseek-flash"
    # The read is the turn itself; the bill and the context size are the
    # status bar's, and the stored row keeps both.
    assert "token_count" not in record
    assert "context_tokens" not in record
    assert "who_helped" not in record
    assert "usage" not in record, "a bill is the operator's, not the model's"
    assert record["created_at"] and record["completed_at"]
    # The bill's split is still the stored row's, for the status bar — the
    # model's read is the turn itself, not what it cost.
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
    for name in ("remember", "restore", "rebuild"):
        assert "agent" in tools[name].input_schema["properties"]


@pytest.mark.asyncio
async def test_none_of_the_harness_tools_is_the_models() -> None:
    """The audience mark, and which side of it each tool is on.

    A model may read its own history; the context plugin's own selection tools
    are the harness's, not the model's — a model's selection reaches it through
    the agent server's `_context_rebuild`/`_turn_compact`, which carry the
    decision schema.  That is a claim about the marks rather than about the
    code, so it is asserted here.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    for name in ("turn_list", "turn_search", "turn_summarize", "turn_read"):
        assert listed[name].meta.get("slife2/audience") == "assistant"
    for name in ("remember", "restore", "rebuild"):
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

        jack = await client.call_tool("turn_list", {}, meta=conversation_meta("jack"))
        jill = await client.call_tool("turn_list", {}, meta=conversation_meta("jill"))
        # Turn 1 exists in all three files, and is a different turn in each.
        read = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=conversation_meta("jack")
        )
        scoped = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=conversation_meta("jill", "worker")
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
            "turn_list", {"limit": 2, "offset": 1}, meta=conversation_meta("jack")
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
            "turn_list", {"limit": 1000}, meta=conversation_meta("jack")
        )

    assert page.data["total"] == 1
    assert page.data["limit"] == MAX_PAGE
    assert len(page.data["entries"]) == 1


@pytest.mark.asyncio
async def test_an_offset_past_every_row_is_the_empty_page() -> None:
    """A page start beyond SQLite's integer range is still "past the end".

    `page_window` clamps it to the largest bindable value — binding the
    original would raise `OverflowError` rather than answer — and the response
    carries the offset it applied, so the caller's `offset + len < total`
    arithmetic still says there is nothing more.  Every listing shares that
    helper, so this is the rule rather than this tool's.
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("q0", "a0"))
        page = await client.call_tool(
            "turn_list", {"offset": 2**63}, meta=conversation_meta("jack")
        )

    assert page.data["entries"] == []
    assert page.data["total"] == 1
    assert page.data["offset"] == 2**63 - 1


@pytest.mark.asyncio
async def test_a_bound_the_grammar_does_not_know_is_a_refusal() -> None:
    """A `ToolError` the model reads and corrects, not an empty page."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="invalid since bound"):
            await client.call_tool(
                "turn_list", {"since": "whenever"}, meta=conversation_meta("jack")
            )


@pytest.mark.asyncio
async def test_reading_a_turn_that_is_not_there_says_which_one() -> None:

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="no turn 7"):
            await client.call_tool(
                "turn_read", {"turn_id": 7}, meta=conversation_meta("jack")
            )


@pytest.mark.asyncio
async def test_a_turn_id_no_row_can_have_is_a_refusal_not_an_internal_error() -> None:
    """SQLite binds a signed 64-bit integer, and a bigger one raises at the bind.

    A model can write more digits than that; the tools' contract is the
    sentence naming the id it could not find, which is what a model that
    invented one needs — not `OverflowError` out of the query.
    """
    huge = 2**63

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("q0", "a0"))
        with pytest.raises(ToolError, match=f"no turn {huge}"):
            await client.call_tool(
                "turn_read", {"turn_id": huge}, meta=conversation_meta("jack")
            )
        with pytest.raises(ToolError, match=f"no turn {huge}"):
            await client.call_tool(
                "turn_summarize",
                {"turn_id": huge, "summary": "nowhere"},
                meta=conversation_meta("jack"),
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
            "turn_search", {"keywords": ["trump"]}, meta=conversation_meta("jack")
        )

    results = found.data["results"]
    assert results[0]["user_message"] == "the trump rally"
    assert results[0]["similarity"] > 0
    assert set(results[0]) == {
        "turn_id",
        "created_at",
        "user_message",
        "assistant_message",
        "unreadable",
        "similarity",
    }
    # The envelope is the one all three searches give.
    assert set(found.data) == {"results", "best", "confidence", "browsed", "limit"}
    assert found.data["best"] == results[0]["similarity"]
    assert found.data["confidence"] == "sure"
    assert found.data["browsed"] is False
    assert found.data["limit"] == 10


@pytest.mark.asyncio
async def test_an_exact_word_match_is_evidence_even_when_meaning_also_scored_it() -> (
    None
):
    """**`worded` was read off a missing similarity, which is not the same fact.**

    A turn found by both legs carries its meaning score, so a page whose every
    hit does contain the caller's words has no `None` similarity — and the old
    answer reported `confidence: none` for an exact match whose cosine happened
    to sit under the weak floor.  The keyword leg itself is asked now, over the
    page's own ids, which is the only answer that is about the words.

    The turn is built to have both: it carries `trump` (so the keyword leg
    finds it) and repeats other vocabulary words heavily, which pulls its
    cosine under the floor while leaving the word match exact.
    """
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("trump 计算 计算 计算 计算", "a0"))
        found = await client.call_tool(
            "turn_search", {"keywords": ["trump"]}, meta=conversation_meta("jack")
        )

    hit = found.data["results"][0]
    assert hit["similarity"] is not None, (
        "the premise: meaning scored this hit, so its similarity is not the "
        "word-evidence signal"
    )
    assert found.data["best"] < 0.48, (
        "the premise: the cosine is under the weak floor, so only the word "
        "evidence can make this page weak rather than none"
    )
    assert found.data["confidence"] == "weak"


@pytest.mark.asyncio
async def test_a_page_the_words_do_not_reach_is_not_worded() -> None:
    """**The words are evidence only when the keyword leg finds them.**

    `worded` is asked of the leg over the page's own ids.  An `async def` left
    on the blocking side of the thread hop answered with an un-awaited
    coroutine — truthy whatever it found — so every search that passed words
    reported evidence, and a meaning-only page under the weak floor read
    `weak` where the honest tier is `none`.  The turn below shares no word
    with the query and is orthogonal to it, so the meaning leg still brings it
    with a cosine of zero.
    """
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("计算 计算", "a0"))
        found = await client.call_tool(
            "turn_search", {"keywords": ["trump"]}, meta=conversation_meta("jack")
        )

    assert [hit["similarity"] for hit in found.data["results"]] == [0.0], (
        "the premise: the page was measured, and measured below both floors"
    )
    assert found.data["best"] == 0.0
    assert found.data["confidence"] == "none"


@pytest.mark.asyncio
async def test_search_reads_only_the_callers_conversation() -> None:
    """The same boundary `turn_list` has: no argument names whose memory."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("jack's trump note", "a"))
        await _seed(client, "jill", ("jill's trump note", "a"))
        found = await client.call_tool(
            "turn_search", {"keywords": ["trump"]}, meta=conversation_meta("jack")
        )

    assert [e["user_message"] for e in found.data["results"]] == ["jack's trump note"]


@pytest.mark.asyncio
async def test_search_reads_the_callers_keyword_weight() -> None:
    """The search fuses on the same `context.keyword_weight` the rebuild does.

    A weight of zero removes the keyword leg.  The keyword hit is the newer
    turn, and the meaning leg's page holds the older one; with the default
    weight the keyword hit wins the tie, and with the caller's zero it is the
    only turn left.
    """
    mine, callers = default_config(), default_config()
    callers = replace(callers, context=replace(callers.context, keyword_weight=0.0))
    server = build_server(mine, embedder=EMBEDDER, source=lambda: callers)

    async with Client(server) as client:
        await _seed(client, "jack", ("gamma", "1"), ("alpha beta 工具", "2"))
        answer = await client.call_tool(
            "turn_search",
            {"keywords": ["alpha", "beta"], "limit": 1},
            meta=conversation_meta("jack"),
        )

    assert [entry["turn_id"] for entry in answer.data["results"]] == [1]
    # The premise: with the default weight the keyword hit leads the fusion.
    store = TurnStore(agent_dir("jack") / "turns.db")
    ranked = await store.recall(query="alpha beta", embedder=EMBEDDER, limit=1)
    assert [turn_id for turn_id, _ in ranked] == [2, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", ["", "***", "   "])
async def test_a_search_with_nothing_to_look_for_points_at_the_browse(
    keyword: str,
) -> None:
    """An empty search is not a browse; `turn_list` is."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="turn_list"):
            await client.call_tool(
                "turn_search", {"keywords": [keyword]}, meta=conversation_meta("jack")
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
            "turn_search", {"sentences": ["工具"]}, meta=conversation_meta("jack")
        )

    assert answer.data["results"][0]["turn_id"] == 1


@pytest.mark.asyncio
async def test_a_search_can_leave_the_context_out() -> None:
    """**The live list is a choice, not the tool's rule.**

    The default is the whole log: a turn the conversation is already running on
    is still one the model may want to read or name.  `exclude_context` drops
    just those — the rebuild's rule, asked for deliberately.
    """
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        ids = await _seed(client, "jack", ("trump one", "1"), ("trump two", "2"))
        TurnStore(agent_dir("jack") / "turns.db").set_context_turns([ids[0]])
        whole = await client.call_tool(
            "turn_search", {"sentences": ["trump"]}, meta=conversation_meta("jack")
        )
        outside = await client.call_tool(
            "turn_search",
            {"sentences": ["trump"], "exclude_context": True},
            meta=conversation_meta("jack"),
        )

    assert {entry["turn_id"] for entry in whole.data["results"]} == set(ids)
    assert [entry["turn_id"] for entry in outside.data["results"]] == [ids[1]]


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
            {"keywords": ["trump"], "since": "2025-01-01"},
            meta=conversation_meta("jack"),
        )

    assert [e["user_message"] for e in found.data["results"]] == ["new trump note"]


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
            {"keywords": ["首经贸发展"], "since": "yesterday"},
            meta=conversation_meta("jack"),
        )

    assert [e["user_message"] for e in found.data["results"]][0] == (
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
            meta=conversation_meta("jack"),
        )
        read = await client.call_tool(
            "turn_read", {"turn_id": 1}, meta=conversation_meta("jack")
        )
        found = await client.call_tool(
            "turn_search", {"keywords": ["quarterly"]}, meta=conversation_meta("jack")
        )

    assert wrote.data == {"status": "updated", "turn_id": 1}
    assert (read.data["summary"], read.data["tags"]) == (
        "the quarterly forecast",
        "planning",
    )
    assert [e["turn_id"] for e in found.data["results"]][0] == 1


@pytest.mark.asyncio
async def test_an_annotation_keeps_the_half_it_did_not_name() -> None:
    """`None` is "not named"; `""` is "clear this field".

    The tool coerced an omitted argument to `""`, and the store reads `""` as
    an explicit clear — so a tags-only call erased the summary (and vice
    versa), and the word the summary was findable by went out of the keyword
    index with it.
    """
    meta = conversation_meta("jack")
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("q0", "a0"))
        await client.call_tool(
            "turn_summarize",
            {"turn_id": 1, "summary": "the quarterly forecast", "tags": "planning"},
            meta=meta,
        )
        await client.call_tool(
            "turn_summarize", {"turn_id": 1, "tags": "budget"}, meta=meta
        )
        tags_only = await client.call_tool("turn_read", {"turn_id": 1}, meta=meta)
        await client.call_tool(
            "turn_summarize", {"turn_id": 1, "summary": "the annual plan"}, meta=meta
        )
        summary_only = await client.call_tool("turn_read", {"turn_id": 1}, meta=meta)

    assert (tags_only.data["summary"], tags_only.data["tags"]) == (
        "the quarterly forecast",
        "budget",
    )
    assert (summary_only.data["summary"], summary_only.data["tags"]) == (
        "the annual plan",
        "budget",
    )


@pytest.mark.asyncio
async def test_the_current_turns_annotation_is_captured_not_written() -> None:
    """No id means the turn being run; it has no row yet to write."""

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        wrote = await client.call_tool(
            "turn_summarize",
            {"summary": "the current turn", "tags": "now"},
            meta=conversation_meta("jack"),
        )
        # Nothing was stored, so the id cannot resolve.
        with pytest.raises(ToolError, match="no turn 1"):
            await client.call_tool(
                "turn_read", {"turn_id": 1}, meta=conversation_meta("jack")
            )

    assert wrote.data["status"] == "captured"
    assert wrote.data["turn_id"] is None


@pytest.mark.asyncio
async def test_a_summary_with_nothing_to_write_is_refused() -> None:
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("q0", "a0"))
        with pytest.raises(ToolError, match="summary and/or tags"):
            await client.call_tool(
                "turn_summarize", {"turn_id": 1}, meta=conversation_meta("jack")
            )


@pytest.mark.asyncio
async def test_summarizing_a_turn_that_is_not_there_says_which_one() -> None:
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        with pytest.raises(ToolError, match="no turn 7"):
            await client.call_tool(
                "turn_summarize",
                {"turn_id": 7, "summary": "nowhere"},
                meta=conversation_meta("jack"),
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

    assert answer.data == {
        "messages": [SYSTEM],
        "turn_ids": [],
        "turns": [],
        "missing": [],
        "unreadable": [],
    }


@pytest.mark.asyncio
async def test_a_read_of_a_conversation_that_was_never_written_creates_nothing() -> (
    None
):
    """**A removed subagent is a name with no record, and a read must not revive it.**

    `agent_dir` makes a conversation's directory, and `TurnStore` makes its file,
    so a `restore`, a browse or a search against a stale name used to leave a
    directory with no `subagent.yaml` behind — which `subagent_create` and
    `subagent_remove` then both refuse until somebody cleans the filesystem by
    hand.  A conversation that has genuinely never run is the same absent file,
    and the empty answer is the honest one for both.
    """
    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        restored = await client.call_tool(
            "restore",
            {"agent": "jack", "subagent": "ghost", "messages": [SYSTEM]},
        )
        listed = await client.call_tool(
            "turn_list", {}, meta=conversation_meta("jack", "ghost")
        )
        found = await client.call_tool(
            "turn_search",
            {"keywords": ["ghost"]},
            meta=conversation_meta("jack", "ghost"),
        )
        with pytest.raises(ToolError, match="no turn 1"):
            await client.call_tool(
                "turn_read", {"turn_id": 1}, meta=conversation_meta("jack", "ghost")
            )

    assert restored.data == {
        "messages": [SYSTEM],
        "turn_ids": [],
        "turns": [],
        "missing": [],
        "unreadable": [],
    }
    assert listed.data["entries"] == []
    assert found.data["results"] == []
    assert not subagent_path("jack", "ghost").exists(), "the read made its directory"


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
async def test_a_restore_reports_a_turn_that_is_no_longer_stored() -> None:
    """**The missing row is reported, not dropped.**

    A list naming a row that is gone is a file that was rebuilt or pruned
    underneath.  The window's transcript reader can draw the rows it has and
    name the damage; the live-context caller refuses the restore, so a shorter
    conversation never becomes a decision (`TurnRuntime.restore`).
    """

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("kept", "yes"))
        TurnStore(agent_dir("jack") / "turns.db").set_context_turns([1, 404])
        answer = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )

    assert answer.data["turn_ids"] == [1]
    assert answer.data["missing"] == [404]
    assert answer.data["unreadable"] == []
    assert len(answer.data["messages"]) == 3


@pytest.mark.asyncio
async def test_a_damaged_body_is_reported_and_never_read_as_empty() -> None:
    """**A row whose body will not parse is damage, not an empty turn.**

    `restore` reports it so the window can draw the damage and the live context
    can refuse it; `turn_read` refuses the row by name; a rebuild that selected
    it fails before writing a context assembled from the empty value.
    """
    import sqlite3

    async with Client(build_server(default_config(), embedder=EMBEDDER)) as client:
        await _seed(client, "jack", ("the question", "the answer"))
        path = agent_dir("jack") / "turns.db"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE turn SET messages = ? WHERE rowid = 1", ('"not messages"',)
            )
        restored = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )
        listed = await client.call_tool("turn_list", {}, meta=conversation_meta("jack"))
        with pytest.raises(ToolError, match="cannot be read"):
            await client.call_tool(
                "turn_read", {"turn_id": 1}, meta=conversation_meta("jack")
            )

    assert restored.data["unreadable"] == [1]
    assert restored.data["missing"] == []
    assert restored.data["turn_ids"] == [], "the damaged row is not a context"
    assert listed.data["entries"][0]["unreadable"] is True


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
            **overrides,
        },
    )


@pytest.mark.asyncio
async def test_the_rebuild_uses_the_callers_homes_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared context daemon serves every home, and models are per home.

    The decision model's model reference comes from the caller's conversation,
    but the daemon resolved it against the config it started with — a
    `ConfigError` in a second home, swallowed as "keep the context", so recall
    never ran there.
    """
    seen: list[Any] = []

    async def capture(config, *args, **kwargs) -> str:
        seen.append(config)
        return '{"keep": {"action": "keep"}, "recall": {}}'

    monkeypatch.setattr(context_server, "_ask_model", capture)
    mine, callers = default_config(), default_config()
    server = build_server(mine, embedder=EMBEDDER, source=lambda: callers)

    async with Client(server) as client:
        await _seed(client, "jack", ("one", "1"))
        await _rebuild(client, '{"keep": {"action": "keep"}, "recall": {}}')

    assert seen, "the rebuild asked the model"
    assert seen[0] is callers, "and asked with the caller's config"
    assert seen[0] is not mine


@pytest.mark.asyncio
async def test_the_recall_policy_comes_from_the_callers_home() -> None:
    """The floors and the budget are the caller's `context:`, not the daemon's.

    The same shared process serves every home, and the numbers are sized against
    the window and the model the caller's own config names — so they have to be
    read from the caller's file too.  A daemon started on one home's
    `rerank_min_score` must not apply it to another home's recall.
    """
    mine, callers = default_config(), default_config()
    mine = replace(mine, context=replace(mine.context, rerank_min_score=0.9))
    callers = replace(callers, context=replace(callers.context, rerank_min_score=0.0))
    server = build_server(
        mine,
        embedder=RerankingEmbedder(),
        source=lambda: callers,
        ask=Replies(
            '{"keep": {"action": "clear"}, "recall": {"sentences": ["trump"]}}'
        ),
    )

    async with Client(server) as client:
        await _seed(client, "jack", ("trump one", "1"), ("trump two", "2"))
        answer = await _rebuild(client, "", turn_ids=[])

    assert answer.data["recalled"] == 2, "the caller's floor kept both"


def _config_with_window(window: int):
    """`default_config()` with the shipped model's window set for a budget test.

    `max_tokens` is cleared with it: the prompt budget is the window minus that
    completion request, and the shipped 384k would leave a 1000-token test
    window with no prompt space at all.
    """
    config = default_config()
    provider = config.providers["deepseek"]
    shipped = provider.models["deepseek-flash"]
    return replace(
        config,
        providers={
            "deepseek": replace(
                provider,
                models={
                    "deepseek-flash": replace(
                        shipped, context_window=window, max_tokens=None
                    )
                },
            )
        },
    )


@pytest.mark.asyncio
async def test_the_recall_budget_is_the_headroom_below_the_ceiling() -> None:
    """**The ceiling is the one total the keep and the recall share.**

    The kept turns are estimated first and the recall spends what they leave
    below it.  There is no per-recall fraction of its own: a candidate the
    headroom can hold is recalled even when it is larger than any fixed share
    would have allowed.
    """
    config = _config_with_window(1000)
    # The kept turn leaves ~780 of the 800-token ceiling; the candidate is
    # ~300 tokens, so it fits the headroom and would not fit a 0.2 share (200).
    candidate = "trump " + "word " * 250

    async with Client(
        build_server(
            config,
            embedder=EMBEDDER,
            ask=Replies('{"keep": {"action": "keep"}, "recall": {"query": "trump"}}'),
        )
    ) as client:
        await _seed(client, "jack", ("trump " + "word " * 10, "1"), (candidate, "2"))
        answer = await _rebuild(client, "")

    assert answer.data["recalled"] == 1


@pytest.mark.asyncio
async def test_a_context_over_the_ceiling_is_fitted_and_the_recall_spends_it() -> None:
    """**The ceiling is a hard bound, and it binds the kept turns too.**

    A kept turn larger than the whole budget cannot be sent, so it is cut from
    the selection's oldest end (`fitted`) and the freed budget is what the
    recall may spend.  The old per-recall fraction could not guarantee this:
    it only refused to *add* to an over-full context, so the over-full base
    stayed and the next call was a provider 400.
    """
    config = _config_with_window(1000)
    kept = "trump " + "word " * 1200
    candidate = "trump " + "word " * 250

    async with Client(
        build_server(
            config,
            embedder=EMBEDDER,
            ask=Replies('{"keep": {"action": "keep"}, "recall": {"query": "trump"}}'),
        )
    ) as client:
        await _seed(client, "jack", (kept, "1"), (candidate, "2"))
        answer = await _rebuild(client, "")

    assert answer.data["changed"] is True
    assert answer.data["fitted"] == 1, "the kept turn could not fit the budget"
    assert answer.data["recalled"] == 1, "the recall spent what the fit freed"
    assert answer.data["turn_ids"] == [2]


@pytest.mark.asyncio
async def test_a_kept_context_over_the_budget_is_fitted_and_published() -> None:
    """**A no-op decision still fits a selection that cannot be sent.**

    The live failure this rule exists for: a rebuild published a ~772k-token
    selection beside a 384k completion request, and the turn's first call was a
    provider 400.  A decision that keeps everything is no longer a no-op when
    the list is over the prompt budget — the oldest turns are cut, the answer
    says how many, and the live-context list is the fitted one so the next
    start replays something a request can carry.
    """
    config = _config_with_window(1000)

    async with Client(
        build_server(
            config,
            embedder=EMBEDDER,
            ask=Replies('{"keep": {"action": "keep"}, "recall": {}}'),
        )
    ) as client:
        ids = await _seed(client, "jack", ("word " * 1200, "1"), ("trump " * 10, "2"))
        answer = await _rebuild(client, "", turn_ids=ids, messages=[SYSTEM])
        restored = await client.call_tool(
            "restore", {"agent": "jack", "messages": [SYSTEM]}
        )

    assert answer.data["changed"] is True
    assert answer.data["fitted"] == 1, "the older turn cannot fit the budget"
    assert answer.data["turn_ids"] == [ids[1]]
    assert (answer.data["kept"], answer.data["in_hand"]) == (1, 2)
    assert answer.data["recalled"] == 0
    assert restored.data["turn_ids"] == [ids[1]], "the fitted list is what persists"


@pytest.mark.asyncio
async def test_a_keep_list_is_fitted_in_time_order_not_the_models_array_order() -> None:
    """**The fit's axis is time, whatever order the JSON array came in.**  A
    keep-list preserved the model's order and `fit_recent` walks from the end
    assuming time order, so a list written newest-first cut the newest turn and
    kept the oldest — the opposite of what the ceiling is for."""
    config = _config_with_window(1000)
    # The store is fresh for this test, so the two seeds are turns 1 and 2; the
    # decision names them newest-first.
    reply = '{"keep": {"action": "ids", "turn_ids": [2, 1]}, "recall": {}}'

    async with Client(
        build_server(config, embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", ("word " * 1200, "1"), ("trump " * 10, "2"))
        answer = await _rebuild(client, "", turn_ids=ids, messages=[SYSTEM])

    assert ids == [1, 2]
    assert answer.data["fitted"] == 1
    assert answer.data["turn_ids"] == [ids[1]], "the newest turn survives the cut"


@pytest.mark.asyncio
async def test_a_candidate_below_the_rerank_floor_is_not_recalled() -> None:
    """The cross-encoder is the last precision stage: what it rejects stays out.

    RRF brings both `trump` turns to the shortlist; the reranker reads the
    question beside each exchange and scores one above the floor and one below.
    """
    base = default_config()
    config = replace(
        base,
        context=replace(base.context, rerank_min_score=0.5),
    )
    reranker = RerankingEmbedder()

    async with Client(
        build_server(
            config,
            embedder=reranker,
            ask=Replies(
                '{"keep": {"action": "clear"}, "recall": {"sentences": ["trump"]}}'
            ),
        )
    ) as client:
        await _seed(client, "jack", ("trump high", "1"), ("trump low", "2"))
        # Nothing in hand, so the selection is exactly what the reranker keeps.
        answer = await _rebuild(client, "", turn_ids=[])

    assert answer.data["recalled"] == 1
    assert answer.data["turn_ids"] == [1]
    assert answer.data["rerank"] == "", "the ranker ran"
    assert reranker.asked, "the cross-encoder was asked"
    assert "trump" in reranker.asked[0][0], "the question, not the conversation"


@pytest.mark.asyncio
async def test_a_failing_reranker_keeps_the_fused_order() -> None:
    """An optional precision stage that is down is not a failed recall."""
    base = default_config()
    config = replace(
        base,
        context=replace(base.context, rerank_min_score=0.5),
    )
    reranker = RerankingEmbedder(fail=True)

    async with Client(
        build_server(
            config,
            embedder=reranker,
            ask=Replies(
                '{"keep": {"action": "clear"}, "recall": {"sentences": ["trump"]}}'
            ),
        )
    ) as client:
        await _seed(client, "jack", ("trump high", "1"), ("trump low", "2"))
        answer = await _rebuild(client, "", turn_ids=[])

    assert answer.data["recalled"] == 2, "the fused order stands"
    assert answer.data["rerank"] == "failed", "and the answer says why"


@pytest.mark.asyncio
async def test_the_shipped_rerank_floor_drops_a_keyword_incidental() -> None:
    """The default floor is the stage's own filter, not just a build-time knob.

    Both turns contain the query word, so both reach the pool.  The ranker
    separates the real exchange (0.8) from the incidental one (0.05), and the
    shipped floor of 0.3 is what turns that separation into a smaller context.
    A floor of 0.0 — which this test would pass under — would keep both and
    make the stage a reorderer with no membership effect.
    """
    reranker = GradedReranker()

    async with Client(
        build_server(
            default_config(),
            embedder=reranker,
            ask=Replies(
                '{"keep": {"action": "clear"}, "recall": {"sentences": ["trump"]}}'
            ),
        )
    ) as client:
        await _seed(client, "jack", ("trump real", "1"), ("trump noise", "2"))
        answer = await _rebuild(client, "", turn_ids=[])

    assert default_config().context.rerank_min_score == 0.3
    assert answer.data["recalled"] == 1
    assert answer.data["turn_ids"] == [1]
    assert answer.data["rerank"] == "", "the ranker ran"


@pytest.mark.asyncio
async def test_a_recall_has_no_count_cap() -> None:
    """Membership is the floors' and the budget's; no count cuts the selection.

    Every one of these is a genuine match and the window is wide enough for all
    of them, so all 41 are recalled.
    """
    async with Client(
        build_server(
            default_config(),
            embedder=EMBEDDER,
            ask=Replies(
                '{"keep": {"action": "clear"}, "recall": {"sentences": ["trump"]}}'
            ),
        )
    ) as client:
        ids = await _seed(
            client, "jack", *[(f"trump {index}", str(index)) for index in range(41)]
        )
        answer = await _rebuild(client, "", turn_ids=[])

    assert len(ids) == 41
    assert answer.data["recalled"] == 41, "the 41st turn is not cut by a count"


@pytest.mark.asyncio
async def test_the_similarity_floor_drops_a_diluted_candidate() -> None:
    """The floor filters; a candidate above it is recalled wherever it ranks.

    Turn 2 is the keyword leg's best — its summary repeats `trump` — but its
    meaning vector is diluted by the other vocabulary words in its reasoning,
    so its cosine is below the floor.  Turn 1 is the meaning leg's best, and
    the floor leaves it as the only candidate the budget may buy.
    """
    base = default_config()
    config = replace(base, context=replace(base.context, min_similarity=0.52))

    async with Client(
        build_server(
            config,
            embedder=EMBEDDER,
            ask=Replies('{"keep": {"action": "clear"}, "recall": {"query": "trump"}}'),
        )
    ) as client:
        await client.call_tool(
            "remember",
            {
                "agent": "jack",
                "messages": _exchange("trump", "1"),
            },
        )
        await client.call_tool(
            "remember",
            {
                "agent": "jack",
                "messages": [
                    {"role": "user", "content": "trump"},
                    {
                        "role": "assistant",
                        "content": "2",
                        "thinking": "工具 计算 测试",
                    },
                ],
                # The summary is in the keyword leg's text and not in the
                # vector's, so it makes turn 2 the keyword winner without
                # raising the cosine the floor measures.
                "summary": "trump trump",
            },
        )
        answer = await _rebuild(client, "", turn_ids=[])

    assert answer.data["turn_ids"] == [1]
    assert answer.data["recalled"] == 1
    # The premise: the fused order puts the floor-dropped turn first, and the
    # similarity the floor measures is genuinely below it.
    store = TurnStore(agent_dir("jack") / "turns.db")
    ranked = await store.recall(query="trump", embedder=EMBEDDER, limit=10)
    assert [turn_id for turn_id, _ in ranked] == [2, 1]
    assert ranked[0][1] is not None and ranked[0][1] < 0.52


def test_a_long_question_does_not_cut_the_answer_out_of_the_rerank_document() -> None:
    """The pair is the signal; clipping the join would throw half of it away."""
    limit = context_server.RERANK_DOC_CHARS
    text = context_server._rerank_text(
        [
            {"role": "user", "content": "q" * (limit * 2)},
            {"role": "assistant", "content": "the answer"},
        ]
    )

    assert "the answer" in text
    assert len(text) <= limit


@pytest.mark.asyncio
async def test_a_provider_without_a_cross_encoder_reports_it_in_the_answer() -> None:
    """No `rerank_model` is a capability the caller is told about, not silence.

    A report that did not say so would look exactly like one whose ranker ran —
    the two are different answers to "why is this order this order".
    """
    async with Client(
        build_server(
            default_config(),
            embedder=EMBEDDER,
            ask=Replies(
                '{"keep": {"action": "clear"}, "recall": {"sentences": ["trump"]}}'
            ),
        )
    ) as client:
        await _seed(client, "jack", ("trump one", "1"))
        answer = await _rebuild(client, "", turn_ids=[])

    assert answer.data["recalled"] == 1
    assert answer.data["rerank"] == "unavailable"


@pytest.mark.asyncio
async def test_a_decision_to_keep_everything_rebuilds_nothing() -> None:
    """The common case, and the one that keeps a costly call from being free.

    `mode: keep` means the turns in hand are enough, so nothing is fetched,
    nothing is rendered and nothing is written — the answer is the caller's own
    list, handed back.
    """
    hold = [SYSTEM, {"role": "user", "content": "one"}]

    async with Client(
        build_server(
            default_config(),
            embedder=EMBEDDER,
            ask=Replies('{"keep": {"action": "keep"}, "recall": {}}'),
        )
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": hold,
                "turn_ids": [1],
            },
        )

    assert answer.data["changed"] is False
    assert answer.data["messages"] == hold
    assert answer.data["turn_ids"] == [1]
    assert answer.data["decision"] == "", "the model answered"
    assert answer.data["reason"] == "asked", (
        "the report says which no-op this was, not merely that nothing moved"
    )
    assert TurnStore(agent_dir("jack") / "turns.db").context_turns() == [1]


@pytest.mark.asyncio
async def test_a_recall_that_finds_nothing_says_which_no_op_it_was() -> None:
    """**A search that ran and found nothing is not a search nobody made.**

    The counts cannot tell the two apart — both are `changed: false,
    recalled: 0` — so the answer carries the exit, and the report can say the
    recall ran rather than leave the model to read silence as a refusal.
    """
    hold = [SYSTEM, {"role": "user", "content": "工具"}]
    reply = '{"keep": {"action": "keep"}, "recall": {"sentences": ["计算"]}}'

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", ("工具", "1"), ("trump", "2"))
        answer = await client.call_tool(
            "rebuild",
            {"agent": "jack", "messages": hold, "turn_ids": [ids[0]]},
        )

    assert answer.data["changed"] is False
    assert answer.data["decision"] == "", "the decision answered"
    assert answer.data["reason"] == "recalled"
    assert answer.data["recalled"] == 0
    assert answer.data["outside"] == 1, "the search had a turn to draw from"


@pytest.mark.asyncio
async def test_a_keep_list_that_names_no_known_turn_is_a_tool_error() -> None:
    """**A miscopied id is not a clear.**

    The keep-list is an intersection with the turns in hand, so a list the
    conversation no longer holds has an empty intersection — and publishing
    that would empty the context on a typo.  The explicit clear is the empty
    list; a decision that names nothing the conversation has cannot be acted
    on, so it is the tool's error with the reason, and the context is not
    written (SDD §5.5, Appendix C).
    """
    hold = [
        SYSTEM,
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "1"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "2"},
    ]

    async with Client(
        build_server(
            default_config(),
            embedder=EMBEDDER,
            ask=Replies('{"keep": {"action": "ids", "turn_ids": [99]}, "recall": {}}'),
        )
    ) as client:
        ids = await _seed(client, "jack", ("one", "1"), ("two", "2"))
        with pytest.raises(ToolError, match="the keep-list named no turn in hand"):
            await _rebuild(client, "", messages=hold, turn_ids=ids)

    assert TurnStore(agent_dir("jack") / "turns.db").context_turns() == ids


async def _down(*arguments: Any, **keywords: Any) -> str:
    """A decision model whose model server never answers."""
    raise RuntimeError("the model server is down")


@pytest.mark.parametrize(
    ("ask", "reason"),
    [
        (_down, "RuntimeError: the model server is down"),
        (
            Replies("a reply this build cannot read"),
            "the reply is not one JSON object",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_decision_that_decided_nothing_says_the_recall_failed(
    ask: Any, reason: str
) -> None:
    """**A decision that did not answer is the tool's error, with its reason.**

    `recalled: 0` is the same count for an empty log and for a decision that
    never arrived, so the failure is named — the provider's own failure text, or
    the parser's refusal, in the one spelling the turn's own failed-call line
    uses (`failure_text`).  The context is not written; the turn keeps what it
    had.  There is one ask and no repair.
    """
    hold = [SYSTEM, {"role": "user", "content": "one"}]

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=ask)
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        with pytest.raises(ToolError) as refused:
            await client.call_tool(
                "rebuild",
                {
                    "agent": "jack",
                    "messages": hold,
                    "turn_ids": [1],
                },
            )

    assert reason in str(refused.value)
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
            default_config(),
            embedder=EMBEDDER,
            ask=Replies('{"keep": {"action": "clear"}, "recall": {}}'),
        )
    ) as client:
        await _seed(client, "jack", ("one", "1"), ("two", "2"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [SYSTEM, {"role": "user", "content": "one"}],
                "turn_ids": [1],
            },
        )

    assert answer.data["messages"] == [SYSTEM]
    assert answer.data["turn_ids"] == []
    assert answer.data["changed"] is True
    assert answer.data["kept"] == 0, "the clear dropped the only turn in hand"
    assert answer.data["in_hand"] == 1, "and the report says what it was kept out of"
    assert answer.data["outside"] == 1, "the second turn is the recall's domain"
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
            ask=Replies(
                '{"keep": {"action": "ids", "turn_ids": [2, 99]}, "recall": {}}'
            ),
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
            ask=Replies(
                '{"keep": {"action": "keep"}, "recall": {"query": "the very first"}}'
            ),
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
            },
        )

    assert answer.data["turn_ids"] == ids
    assert answer.data["recalled"] == 1


@pytest.mark.asyncio
async def test_a_recall_does_not_bring_back_a_turn_already_kept() -> None:
    """Keep ∪ recall is joined by id, so a kept turn must not spend the budget.

    The kept turn ranks first and is deliberately sized to take the headroom
    the recall has left, while the turn the decision is actually asking for is
    a little larger.  Without the exclusion the recall would buy the kept turn
    again — the union collapses it, nothing changes, and the other turn never
    fits; with it the budget goes to the turn not already in hand.
    """
    config = _config_with_window(1000)
    kept = "工具 工具 " + "word " * 240
    wanted = "工具 trump " + "word " * 280
    reply = '{"keep": {"action": "ids", "turn_ids": [1]}, "recall": {"query": "工具"}}'

    async with Client(
        build_server(config, embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", (kept, "1"), (wanted, "2"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [
                    SYSTEM,
                    {"role": "user", "content": kept},
                    {"role": "assistant", "content": "1"},
                ],
                "turn_ids": [ids[0]],
            },
        )

    assert answer.data["turn_ids"] == ids
    assert answer.data["recalled"] == 1
    assert answer.data["decision"] == "", "the decision answered"
    # The premise: the kept turn ranks first, so without the exclusion it would
    # be the first thing the recall spends the headroom on.
    store = TurnStore(agent_dir("jack") / "turns.db")
    ranked = await store.recall(query="工具", embedder=EMBEDDER, limit=10)
    assert [turn_id for turn_id, _ in ranked] == [1, 2]


@pytest.mark.asyncio
async def test_a_recall_does_not_bring_back_a_turn_the_decision_dropped() -> None:
    """**Every turn in hand is excluded, not only the kept ones.**

    The decision model read the whole conversation, so a turn it left out of the
    keep-list was left out deliberately: a recall that answered a query with it
    would overturn the decision it was made in.  The turn outside the
    conversation is still recalled — that is what the recall is for.
    """
    reply = '{"keep": {"action": "clear"}, "recall": {"sentences": ["trump"]}}'

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", ("trump one", "1"), ("trump two", "2"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [
                    SYSTEM,
                    {"role": "user", "content": "trump one"},
                    {"role": "assistant", "content": "1"},
                ],
                "turn_ids": [ids[0]],
            },
        )

    assert answer.data["turn_ids"] == [ids[1]], "the dropped turn stays dropped"
    assert answer.data["recalled"] == 1


@pytest.mark.asyncio
async def test_a_sentence_recall_reads_the_decisions_sentences() -> None:
    """The decision's sentences are the meaning leg's questions.

    There is no separate input to add: the decision model read the conversation,
    and a sentence it wrote is the subject it chose to ask the log for.
    """
    reply = '{"keep": {"action": "clear"}, "recall": {"sentences": ["trump"]}}'

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
            },
        )

    assert answer.data["turn_ids"] == [ids[1]]
    assert answer.data["recalled"] == 1


@pytest.mark.asyncio
async def test_a_keyword_recall_keeps_the_query_as_its_one_meaning_question() -> None:
    """A keyword recall asks one meaning question: the query.

    A query is already the meaning fallback; the keyword phrase is the
    deliberate question, and the decision's sentences would be a second one.
    """
    reply = '{"keep": {"action": "clear"}, "recall": {"query": "trump"}}'

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
            },
        )

    assert answer.data["turn_ids"] == [ids[1]]
    assert answer.data["recalled"] == 1


@pytest.mark.asyncio
async def test_a_period_recall_is_not_narrowed_by_the_input() -> None:
    """A period asks for a time, not a subject; the input does not filter it."""
    reply = '{"keep": {"action": "clear"}, "recall": {"since": "yesterday"}}'

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
    reply = '{"keep": {"action": "keep"}, "recall": {"query": "***", "sentences": ["the very first thing"]}}'

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
            },
        )

    assert answer.data["turn_ids"] == ids
    assert answer.data["recalled"] == 1


@pytest.mark.asyncio
async def test_a_context_list_that_cannot_be_written_fails_the_rebuild(
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
            ask=Replies('{"keep": {"action": "clear"}, "recall": {}}'),
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
                },
            )


@pytest.mark.asyncio
async def test_a_private_rebuild_decides_without_moving_the_conversation() -> None:
    """**An agent task's rebuild is a clone.**

    The task records nothing, so its pre-turn decision must not move the
    conversation it stands in for: `publish=False` returns the selection and
    writes nothing.  The conversation moving while the decision runs does not
    change that — the task keeps its own answer and the store keeps the
    conversation's list.
    """
    parked = asyncio.Event()
    release = asyncio.Event()

    async def deciding(
        agent: str, subagent: str, model: str, messages: list, *, usage: str = ""
    ) -> str:
        parked.set()
        await release.wait()
        return '{"keep": {"action": "clear"}, "recall": {}}'

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
                        *_exchange("zero", "0"),
                        *_exchange("one", "1"),
                    ],
                    "turn_ids": ids,
                    "publish": False,
                },
            )
        )
        await asyncio.wait_for(parked.wait(), timeout=5)
        # The conversation moves while the decision runs; a clone does not care.
        saved = await client.call_tool(
            "remember",
            {"agent": "jack", "messages": _exchange("two", "2")},
        )
        release.set()
        rebuilt = await answer

    assert rebuilt.data["turn_ids"] == [], "the task adopts its own decision"
    assert rebuilt.data["changed"] is True
    assert TurnStore(agent_dir("jack") / "turns.db").context_turns() == [
        ids[0],
        ids[1],
        saved.data["turn_id"],
    ], "the conversation's list is the agent's, not the task's"


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
    reply = (
        '{"keep": {"action": "keep"}, "recall": {"query": "***", "anchor": "oldest"}}'
    )

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        ids = await _seed(client, "jack", ("one", "1"), ("two", "2"))
        answer = await client.call_tool(
            "rebuild",
            {"agent": "jack", "messages": hold, "turn_ids": [ids[1]]},
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
    together silently dropped the token cap and let a recall spend by retrieval
    width — the overfill a declared window's budget exists to stop.  A window
    nobody *declared* still bounds the recall by the pool's width; a window that
    cannot be asked for is loud (SDD Appendix C: a weaker path is not a
    substitute for a missing capability).
    """

    async with Client(
        build_server(
            default_config(),
            embedder=EMBEDDER,
            ask=Replies('{"keep": {"action": "keep"}, "recall": {"query": "one"}}'),
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
                    "model": "no-such-provider/model",
                },
            )


@pytest.mark.asyncio
async def test_an_unreadable_reply_is_a_tool_error() -> None:
    """**One call, then the error.**  There is no repair.

    The decision model is asked once; an answer that is not the forced call is
    reported as the tool's error, and the context is not written.  The agent can
    call the tool itself with a selection, which is where a correction belongs.
    """
    hold = [SYSTEM, {"role": "user", "content": "one"}]
    replies = Replies("I could not decide.")

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=replies)
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        with pytest.raises(ToolError) as refused:
            await client.call_tool(
                "rebuild",
                {"agent": "jack", "messages": hold, "turn_ids": [1]},
            )

    assert len(replies.asked) == 1, (
        "the decision is asked once; the failure is reported"
    )
    assert "the reply is not one JSON object" in str(refused.value)
    assert TurnStore(agent_dir("jack") / "turns.db").context_turns() == [1]


@pytest.mark.asyncio
async def test_a_failed_call_is_not_asked_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing to correct: no answer came back, and a second ask would only
    spend a call on a provider that has already failed once.

    The failure line carries the provider's own words, not only the exception
    class: `reason=RuntimeError` is the same line for a dead endpoint, a 400 and
    a timeout, and the operator is owed the difference.
    """
    calls: list[str] = []
    caplog.set_level(logging.INFO, logger=context_server.logger.name)

    async def down(*arguments: Any, **keywords: Any) -> str:
        calls.append(keywords.get("repair", ""))
        raise RuntimeError("the model server is down")

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=down)
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        with pytest.raises(ToolError) as refused:
            await _rebuild(client, "")

    assert calls == [""], "one ask, and the repair never ran"
    assert "RuntimeError: the model server is down" in str(refused.value), (
        "the error carries the same words the log does"
    )
    assert any(
        record.message
        == "recall_decision_failed reason=RuntimeError: the model server is down"
        for record in caplog.records
    ), "the provider's own message is in the line"


@pytest.mark.asyncio
async def test_the_decision_is_a_forced_tool_call(monkeypatch) -> None:
    """The conversation plus one system instruction, answered by the one tool.

    The instruction is appended as a system message; the decision tool is forced
    and thinking is off (a thinking endpoint rejects the forced choice).  The
    live conversation is not otherwise touched.
    """
    seen: list[dict[str, Any]] = []

    async def capture(
        config: Any,
        clients: Any,
        opening: Any,
        agent: str,
        subagent: str,
        model: str,
        asked: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str,
        thinking: str,
        kind: str,
    ) -> StreamChatResult:
        seen.append(
            {
                "asked": asked,
                "tools": tools,
                "tool_choice": tool_choice,
                "thinking": thinking,
                "kind": kind,
            }
        )
        return StreamChatResult(
            tool_calls=(
                ToolCall(
                    id="call_1",
                    name=CONTEXT_REBUILD,
                    arguments={"context": "clear"},
                ),
            ),
            stop_reason="tool_calls",
        )

    monkeypatch.setattr(context_server, "_stream_chat", capture)
    hold = [SYSTEM, {"role": "user", "content": "test the tools yourself"}]

    answer = await _ask_model(
        object(), {}, asyncio.Lock(), "jack", "", "m", hold, usage="12,345 (1.2%)"
    )
    assert answer == '{"context": "clear"}', "the tool arguments are the decision"
    call = seen[0]
    assert call["kind"] == "decision"
    assert call["asked"][:-1] == hold, (
        "the conversation is the input, unchanged by the call"
    )
    assert call["asked"][-1]["role"] == "system"
    assert "Decision" in call["asked"][-1]["content"]
    assert "12,345 (1.2%)" in call["asked"][-1]["content"], "the status bar's figure"
    tool = call["tools"][0]["function"]
    assert tool["name"] == CONTEXT_REBUILD
    assert tool["description"] == CONTEXT_REBUILD_TEXT
    assert tool["parameters"] == RECALL_REPLY
    assert call["tool_choice"] == CONTEXT_REBUILD
    assert call["thinking"] == "disabled"


@pytest.mark.asyncio
async def test_the_decision_reads_the_conversation_as_it_runs(monkeypatch) -> None:
    """**The live context is the input, tool exchanges included.**

    `_context_rebuild` is a turn-level decision, and a turn's tool exchanges are
    part of the conversation it reads.  Nothing is removed before the call: the
    messages go out as the turn holds them, and the decision instruction is the
    one message appended.
    """
    seen: list[list[dict[str, Any]]] = []

    async def capture(
        _config: Any,
        _clients: Any,
        _opening: Any,
        _agent: str,
        _subagent: str,
        _model: str,
        asked: list[dict[str, Any]],
        **_options: Any,
    ) -> StreamChatResult:
        seen.append(asked)
        return StreamChatResult(
            tool_calls=(
                ToolCall(
                    id="call_1",
                    name=CONTEXT_REBUILD,
                    arguments={"keep": {"action": "keep"}, "recall": {}},
                ),
            ),
            stop_reason="tool_calls",
        )

    monkeypatch.setattr(context_server, "_stream_chat", capture)
    old_call = {
        "id": "old",
        "type": "function",
        "function": {
            "name": CONTEXT_REBUILD,
            "arguments": '{"context": [1], "recall": {}}',
        },
    }
    search_call = {
        "id": "search",
        "type": "function",
        "function": {"name": "tool_search", "arguments": '{"query": "x"}'},
    }
    hold = [
        SYSTEM,
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [old_call, search_call],
        },
        {"role": "tool", "tool_call_id": "old", "content": "the old report"},
        {"role": "tool", "tool_call_id": "search", "content": "found"},
        {"role": "assistant", "content": "", "tool_calls": [dict(old_call, id="old2")]},
        {"role": "tool", "tool_call_id": "old2", "content": "another old report"},
        {"role": "user", "content": "two"},
    ]

    await _ask_model(
        object(), {}, asyncio.Lock(), "jack", "", "m", hold, usage="12,345 (1.2%)"
    )

    assert seen[0][:-1] == hold, "the conversation goes out as it runs"
    assert seen[0][-1]["role"] == "system"
    assert "Decision call" in seen[0][-1]["content"]


@pytest.mark.asyncio
async def test_a_call_to_another_tool_is_not_a_decision(monkeypatch) -> None:
    """**Only the forced tool's arguments are the answer.**

    A provider that ignores `tool_choice` can return a call the conversation
    already asked for — a harness tool's `{}` would parse as "keep
    everything", and the decision model would be credited with an answer it
    never gave.  The name is part of the decision, so a different one is a
    failed call (keep the context), not a decision.
    """

    async def capture(*_args: Any, **_kwargs: Any) -> StreamChatResult:
        return StreamChatResult(
            tool_calls=(ToolCall(id="c1", name=TURN_COMPACT, arguments={}),),
            stop_reason="tool_calls",
        )

    monkeypatch.setattr(context_server, "_stream_chat", capture)

    with pytest.raises(RuntimeError, match="not '_context_rebuild'"):
        await _ask_model(object(), {}, asyncio.Lock(), "jack", "", "m", [SYSTEM])


@pytest.mark.asyncio
async def test_the_selection_is_a_forced_tool_call(monkeypatch) -> None:
    """The live selector is the same forced call, over the numbered view."""
    seen: list[dict[str, Any]] = []

    async def capture(
        config: Any,
        clients: Any,
        opening: Any,
        agent: str,
        subagent: str,
        model: str,
        asked: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str,
        thinking: str,
        kind: str,
    ) -> StreamChatResult:
        seen.append(
            {
                "asked": asked,
                "tools": tools,
                "tool_choice": tool_choice,
                "thinking": thinking,
                "kind": kind,
            }
        )
        return StreamChatResult(
            tool_calls=(
                ToolCall(
                    id="call_2",
                    name=TURN_COMPACT,
                    arguments={"compact": [1]},
                ),
            ),
            stop_reason="tool_calls",
        )

    monkeypatch.setattr(context_server, "_stream_chat", capture)
    hold = [
        SYSTEM,
        {
            "role": "user",
            "content": 'test the tools yourself [MESSAGE:{"id": "cur.0"}]',
        },
    ]

    answer = await context_server._ask_selector(
        object(), {}, asyncio.Lock(), "jack", "", "m", hold, ["cur.0"]
    )
    assert answer == '{"compact": [1]}'
    call = seen[0]
    assert call["kind"] == "compactor"
    assert call["asked"][1]["content"].endswith('[MESSAGE:{"id": "cur.0"}]'), (
        "the view is numbered by its caller"
    )
    assert call["asked"][-1]["role"] == "system"
    assert "Decision" in call["asked"][-1]["content"]
    tool = call["tools"][0]["function"]
    assert tool["name"] == TURN_COMPACT
    assert tool["description"] == TURN_COMPACT_TEXT
    assert tool["parameters"] == LIVE_REPLY
    assert call["tool_choice"] == TURN_COMPACT
    assert call["thinking"] == "disabled"


@pytest.mark.asyncio
async def test_a_recall_bound_the_grammar_does_not_know_is_a_tool_error() -> None:
    """The same answer as an unreadable reply, and for the same reason.

    A reply of the right *shape* whose `since` is in no grammar this build
    speaks — `whenever`, `sometime last year` — is refused before the store sees
    it.  Handed to the store it would raise from inside a query and fail the
    turn, so the selection is the tool's error instead: it names the grammar,
    and the agent can correct it.  Nothing is written.
    """
    hold = [SYSTEM, {"role": "user", "content": "one"}]
    reply = (
        '{"keep": {"action": "clear"}, "recall": {"query": "one", "since": "whenever"}}'
    )

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        with pytest.raises(ToolError) as refused:
            await client.call_tool(
                "rebuild",
                {"agent": "jack", "messages": hold, "turn_ids": [1]},
            )

    assert "recall.since" in str(refused.value)
    assert "not a time I can place" in str(refused.value)
    assert TurnStore(agent_dir("jack") / "turns.db").context_turns() == [1]


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
            default_config(),
            embedder=EMBEDDER,
            ask=Replies('{"keep": {"action": "clear"}, "recall": {}}'),
        )
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        answer = await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [SYSTEM, {"role": "user", "content": "one"}, tail],
                "turn_ids": [1],
                "carried": 1,
            },
        )

    assert answer.data["messages"] == [SYSTEM, tail]


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


@pytest.mark.asyncio
async def test_startup_survives_a_home_whose_config_this_build_cannot_read(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """A home from another build must not take the daemon down at start-up.

    The start-up pass scans **every known home** (`turn_dbs`) and loads each
    home's `slife2.yaml` to get that home's embedding model.  A home this build
    refuses — an older data directory, a config with a typo — must therefore be
    reported and skipped, not turned into a refusal for every other home: the
    home being started is the caller's, and a read of *its* history must not
    depend on a file in a directory nobody is using.  The skipped home's own
    calls still fail, with the same sentence, when they arrive.

    This is the failure that was seen: a dev checkout, whose own config was
    valid, would not start because a recorded production home's config had
    drifted a release behind — and the sentence did not even say which file.
    """
    old = tmp_path / "old-home"
    config_file(old, "default: not-a-section\n")
    turns = old / "agents" / "jack" / "turns.db"
    turns.parent.mkdir(parents=True)
    turns.write_bytes(b"")
    register_home(old)

    with caplog.at_level(logging.WARNING, logger=CONTEXT_LOGGER):
        async with Client(
            build_server(default_config(), embedder=EMBEDDER, source=load_cached)
        ) as client:
            # A conversation whose home is current is served normally.
            await client.call_tool(
                "remember",
                {"agent": "someone", "messages": [{"role": "user", "content": "hi"}]},
            )
            with pytest.raises(ToolError) as refused:
                await client.call_tool(
                    "restore",
                    {"agent": "jack", "messages": []},
                    meta=conversation_meta("jack", home=str(old)),
                )

    assert any(str(old) in record.getMessage() for record in caplog.records), (
        "the skipped home was not named in the log"
    )
    assert str(old / "slife2.yaml") in str(refused.value)


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


def test_the_turn_prompt_is_the_status_bar_figure_and_the_clock() -> None:
    """**A prompt for the turn running, not a record of one.**

    `[TURN-PROMPT:{…}]` carries the two facts a turn needs and no later turn
    should remember: the clock (a restored footnote is history, not now) and the
    context figure the status bar shows.  The figure is `context_usage`'s, the
    same function `StatusBar` calls, so the marker cannot round differently from
    the bar it mirrors.
    """
    from slife2.libs.turn.context import (
        TURN_PROMPT_PREFIX,
        context_usage,
        turn_prompt_note,
    )

    text, used = context_usage(140_127, 1_000_000)
    assert text == "140,127 (14.0%)"
    assert round(used, 1) == 14.0
    # No declared window: the count alone, and nothing for an empty one.
    assert context_usage(140_127, 0) == ("140,127", 0.0)
    assert context_usage(0, 0) == ("", 0.0)

    note = turn_prompt_note(
        {
            "now": "2026-10-27T20:40:34+08:00",
            "plan": [{"text": "x", "status": "active"}],
        }
    )
    assert note.startswith(TURN_PROMPT_PREFIX)
    assert note.endswith("]")
    payload = json.loads(note[len(TURN_PROMPT_PREFIX) : -1])
    assert payload == {
        "now": "2026-10-27T20:40:34+08:00",
        "plan": [{"text": "x", "status": "active"}],
    }

    # A fact the caller does not have is absent, not empty.
    only_time = turn_prompt_note({"now": "2026-10-27T20:40:34+08:00"})
    assert json.loads(only_time[len(TURN_PROMPT_PREFIX) : -1]) == {
        "now": "2026-10-27T20:40:34+08:00"
    }


def test_the_turn_prompt_lead_is_one_builder_for_the_clock_and_the_plan() -> None:
    """**Two servers, one marker.**  The agent server and the agent-task server
    each assembled this lead; a rule that decides what it carries cannot be two
    rules, or a task's turn and an agent's would disagree about the same
    conversation.

    **No context figure.**  The last measured call is the previous turn's, and
    a rebuild can change the list before this turn's first call — the number
    the turn runs on is `_context_rebuild`'s report.
    """
    from slife2.libs.turn.context import TURN_PROMPT_PREFIX, turn_prompt_lead

    note = turn_prompt_lead()
    assert note.startswith(TURN_PROMPT_PREFIX)
    payload = json.loads(note[len(TURN_PROMPT_PREFIX) : -1])
    assert payload["now"], "a turn always knows when it started"
    assert "token-usage" not in payload, "the stale figure is gone"

    # The plan rides the same marker, and an empty one is omitted like every
    # fact the caller does not have.
    plan = [{"text": "read the config", "status": "active"}]
    with_plan = json.loads(turn_prompt_lead(plan)[len(TURN_PROMPT_PREFIX) : -1])
    assert with_plan["plan"] == plan


def test_a_turn_prompt_does_not_trim_the_message_it_is_added_to() -> None:
    """**The lead's inverse is exact**, so the person's text arrives as written.

    The marker is a prefix on the live user message; `without_lead` takes back
    exactly the marker and its single joining space, not `lstrip` — a turn that
    opened with an indented block survives the round trip.  The pair is what
    keeps the marker out of the row without altering the words it rode in on.
    """
    from slife2.libs.turn.context import with_lead, without_lead

    note = '[TURN-PROMPT:{"now":"2026-10-27T20:40:34+08:00"}]'
    content = "    indented\nsecond line"
    assert with_lead(content, note) == f"{note} {content}"
    assert without_lead(with_lead(content, note), note) == content
    assert with_lead("", note) == note
    assert without_lead(note, note) == ""

    parts = [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "https://example.test/x.png"}},
    ]
    assert with_lead(parts, note)[0] == {"type": "text", "text": note}
    assert without_lead(with_lead(parts, note), note) == parts

    # A message that does not start with this note is left alone — a turn may
    # have been folded or repaired before the strip runs.
    assert without_lead("no marker here", note) == "no marker here"
    assert without_lead(parts, note) == parts

    # **A lead merged into the first part is still the lead.**  The turn opens
    # with a string; a mid-turn `attach_image` turns that string into content
    # parts, with the note joined to the person's text in one part.  Requiring
    # an exact match left the marker in the row, the index and every rebuild —
    # the one place SDD §5.5 says it never reaches.
    merged = [
        {"type": "text", "text": f"{note} what is this?"},
        {"type": "image_url", "image_url": {"url": "https://example.test/x.png"}},
    ]
    assert without_lead(merged, note) == parts


def test_a_keep_list_of_stringly_ids_is_coerced_rather_than_refused() -> None:
    """**The one source `normalise_ids` exists for, and `parse` refused it.**

    `_keep` tested `isinstance(item, int)`, so a model that wrote `["1"]` — JSON
    of whatever the encoder wrote, which is the source `normalise_ids`'s own
    docstring names — made every id resolve to `REJECTED`.  `parse` then answered
    `None` and dropped the *other* half of the decision with it, so a valid
    `recall` beside the keep-list was ignored in silence.  What an id is, is
    `normalise_ids`'s to decide, and the two now read as one rule.
    """
    from slife2.libs.turn.context import Decision, parse_with_reason

    def parse(reply: str) -> Decision | None:
        """The decision half of the one reader — `parse` was superseded by
        `parse_with_reason`, and these assertions are about the decision."""
        return parse_with_reason(reply)[0]

    assert parse('{"context": ["2", 1]}') == Decision(keep=[2, 1])
    # The whole reply survives, which is what "both halves" means.
    assert parse('{"context": ["1"], "recall": {"query": "the very first"}}') == (
        Decision(
            keep=[1],
            recall={
                "query": "the very first",
                "sentences": None,
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
    from slife2.libs.turn.context import Decision, parse_with_reason

    def parse(reply: str) -> Decision | None:
        """The decision half of the one reader — `parse` was superseded by
        `parse_with_reason`, and these assertions are about the decision."""
        return parse_with_reason(reply)[0]

    assert parse(
        '{"recall": {"sentences": ["the dean", "the anniversary"]}}'
    ) == Decision(
        recall={
            "query": None,
            "sentences": ["the dean", "the anniversary"],
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


def test_a_refusal_names_the_field_a_repair_has_to_fix() -> None:
    """**The reason is the model's instruction, not a log word.**

    Every refusal is fed back with the reply it refused, so it has to say what
    the field is and what shape it takes — "unparsed" would be a repair with
    nothing to act on.
    """
    from slife2.libs.turn.context import parse_with_reason

    for reply, fragment in (
        ("no object here", "not one JSON object"),
        ('{"context": [true]}', "turn ids"),
        ('{"recall": {"sentences": "one"}}', "list of strings"),
        ('{"recall": {"anchor": "first"}}', '"newest" or "oldest"'),
        ('{"recall": {"since": "whenever"}}', BOUND_GRAMMAR),
        ('{"context": [], "why": "because"}', '"why"'),
    ):
        decision, reason = parse_with_reason(reply)
        assert decision is None, reply
        assert fragment in reason, (reply, reason)


# --- the four ways a rebuild keeps the context, and what the log says ---------


@pytest.mark.asyncio
async def test_the_ways_a_rebuild_can_fail_and_change_nothing_are_logged_apart(
    caplog,
) -> None:
    """**A failure and a no-op are different outcomes, and the log says which.**

    An unreadable reply and a selection that cannot be fetched are the tool's
    errors now, each named on its own line; a decision that keeps exactly what
    is in hand is a success that writes nothing, and its own line says so.  The
    log may not fold the failures into "the context was sufficient".
    """
    caplog.set_level(logging.INFO, logger=CONTEXT_LOGGER)
    hold = [SYSTEM, {"role": "user", "content": "one"}]
    # One server, one scripted decision model, four calls: an unreadable reply,
    # a decision that asks for nothing, a decision equal to what is held, and a
    # keep-list naming a turn the store does not have.
    replies = Replies(
        "I could not decide.",
        '{"keep": {"action": "keep"}, "recall": {}}',
        '{"keep": {"action": "ids", "turn_ids": [1]}, "recall": {}}',
        '{"keep": {"action": "ids", "turn_ids": [99]}, "recall": {}}',
    )
    answers = []
    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=replies)
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        with pytest.raises(ToolError):
            await client.call_tool(
                "rebuild",
                {"agent": "jack", "messages": hold, "turn_ids": [1]},
            )
        answers.append(
            await client.call_tool(
                "rebuild",
                {"agent": "jack", "messages": hold, "turn_ids": [1]},
            )
        )
        answers.append(
            await client.call_tool(
                "rebuild",
                {"agent": "jack", "messages": hold, "turn_ids": [1]},
            )
        )
        with pytest.raises(ToolError) as missing:
            await client.call_tool(
                "rebuild",
                {
                    "agent": "jack",
                    # 99 is in the caller's list and nowhere in the log, which
                    # is the disagreement this exit exists for.
                    "messages": [SYSTEM, {"role": "user", "content": "one"}],
                    "turn_ids": [1, 99],
                },
            )

    for answer in answers:
        assert answer.data["changed"] is False, "nothing was fetched or written"
        assert answer.data["recalled"] == 0
    said = [record.getMessage() for record in caplog.records]
    assert any(line.startswith("recall_decision_unreadable") for line in said), (
        "the failure is named, on the one ask the decision call makes"
    )
    assert "recall_not_needed reason=context_sufficient" in said
    assert "recall_not_needed reason=context_unchanged" in said
    # A selection that names a row the store does not have is the tool's error,
    # and the sentence names the id and the repair.  The failure is raised where
    # the selection is costed, before any context is written.
    assert "99" in str(missing.value) and "repair" in str(missing.value)
    # The fields the outcome cannot report.  The unreadable reply has no
    # `recall_decided` line at all; the other three say what they were.
    assert "recall_decided keep=keep recall=none" in said
    assert "recall_decided keep=1 recall=none" in said
    assert "recall_decided keep=99 recall=none" in said
    assert len([line for line in said if line.startswith("recall_decided")]) == 3, (
        "one line per decision, and none for the unreadable reply"
    )
    # It stayed [1] throughout: a failed selection writes nothing.
    assert TurnStore(agent_dir("jack") / "turns.db").context_turns() == [1]


@pytest.mark.asyncio
async def test_the_decisions_fields_are_logged(caplog) -> None:
    """**The outcome cannot say what was decided.**

    `recall_rebuilt turns=0 recalled=0 published=true` is the same line for a
    model that cleared the turns and one that never had any, and a recall's
    condition —
    the sentences, the words, the period — is consumed and dropped.  A live
    rebuild raised exactly that question and no artifact could answer it, so
    the decision is logged before its outcome, in the reply's own vocabulary.

    The raw reply is DEBUG because it is the model's text: the parsed line is
    what an operator reads, and the text is there for whoever is already
    looking at the protocol.
    """
    caplog.set_level(logging.DEBUG, logger=CONTEXT_LOGGER)
    reply = '{"keep": {"action": "clear"}, "recall": {"sentences": ["the code review prompt"]}}'

    async with Client(
        build_server(default_config(), embedder=EMBEDDER, ask=Replies(reply))
    ) as client:
        await _seed(client, "jack", ("one", "1"))
        await client.call_tool(
            "rebuild",
            {
                "agent": "jack",
                "messages": [SYSTEM, {"role": "user", "content": "one"}],
                "turn_ids": [1],
            },
        )

    said = [record.getMessage() for record in caplog.records]
    decided = next(
        line for line in said if line.startswith("recall_decided keep=clear ")
    )
    recall = json.loads(decided.partition("recall=")[2])
    assert recall["sentences"] == ["the code review prompt"]
    assert any(line.startswith("recall_decision_reply ") for line in said), (
        "the raw reply is kept for the shape the parser read"
    )


# --- the transport, which the decision model cannot see from where it sits ------


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
    server from another build reached `_decide` as "the model answered with
    nothing": it logged `recall_decision_unparsed` and kept the
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
        await _ask_model(config, clients, asyncio.Lock(), "jack", "", "", [])
