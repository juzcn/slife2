"""Finding a turn by what it was about: the two legs, the fusion, the indexes.

The parts worth testing are the ones a reader would otherwise have to take on
faith.  That a Chinese word is found at all — FTS5's tokenizer sees a run of
them as one token, so this only works because `slife2.libs.store.textindex` separated the
characters, and the test that says so is the one that fails if anybody removes
that step.  That the save is atomic, because a save that raised and stored a row
anyway is the failure this design exists to prevent.  And that the event loop
keeps running while a turn is embedded, because `async` alone does not make that
true.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from slife2.helpers.timeutil import InvalidTimeBound
from slife2.libs.store import db, textindex
from slife2.libs.store.db import (
    TurnStore,
    embed_text,
    searchable_text,
)
from slife2.libs.store.index import (
    EMBED_CHUNK_CHARS,
    VectorIndexUnavailable,
    embed_chunks,
    fuse_ranked,
    rows_in_order,
)
from slife2.libs.store.textindex import EmptyQuery
from tests.fakes import FailingEmbedder, StubEmbedder

pytestmark = pytest.mark.unit

#: Three turns that between them cover what these tests ask about: a Chinese
#: word, a Latin one, a number, and a turn that shares no word with the others.
CORPUS = [
    [
        {"role": "user", "content": "你现在有哪些工具"},
        {"role": "assistant", "content": "工具很多，按类别如下"},
    ],
    [
        {"role": "user", "content": "google搜索一下近期trump的活动"},
        {"role": "assistant", "content": "找到了三条"},
    ],
    [
        {"role": "user", "content": "99*8=？"},
        {"role": "assistant", "content": "792"},
    ],
]


async def _corpus(store: TurnStore, embedder: StubEmbedder) -> None:
    for messages in CORPUS:
        await store.save_turn(messages=messages, embedder=embedder, channel="tui")


def _ids(records) -> list[int]:
    return [record.turn_id for record in records]


# --- what gets indexed --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_keyword_index_holds_the_normalized_text(tmp_path) -> None:
    """The stored string is the one a query is built to match, not the original.

    Which is the whole reason the index stores its own text instead of reading
    `turn` back: a CJK character has to be a token of its own, and no column of
    `turn` holds that.
    """
    path = tmp_path / "jack.turn.db"
    store = TurnStore(path)
    await store.save_turn(messages=CORPUS[0], embedder=StubEmbedder())

    with sqlite3.connect(path) as connection:
        indexed = connection.execute("SELECT search FROM turn_fts").fetchone()[0]

    assert indexed == "你 现 在 有 哪 些 工 具 工 具 很 多 ， 按 类 别 如 下"


@pytest.mark.asyncio
async def test_a_term_that_survives_only_in_a_summary_is_still_found(tmp_path) -> None:
    """`summary` is a retrieval hook, and indexing it is what makes it one.

    Written after the turn, which is why the index stores its text and can have
    one row replaced: a contentless index has nothing to replace and would have
    to be rebuilt from scratch for every summary.
    """
    path = tmp_path / "jack.turn.db"
    store = TurnStore(path)
    embedder = StubEmbedder()
    await store.save_turn(messages=CORPUS[0], embedder=embedder)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE turn SET summary = ? WHERE rowid = 1", ("关于水电站的会议",)
        )
    store._sync_text_index()

    # The keyword leg by itself, because the semantic leg would find the only
    # turn in the file whatever it held.
    expression = textindex.match_expression("水电站")
    assert store._keyword_hits(expression, 10, [], []) == [1]


def test_a_tool_result_and_its_call_are_not_what_a_turn_is_about() -> None:
    """The turn's own voice is what it is about; evidence is what it was made from.

    Tool results are already out — they are 56–99% of a turn's text.  The
    call's argument head followed them after the chunked index made
    the cost visible: on this repository's live log, with 30 discriminator-style
    subject queries, dropping it raised MRR from 0.83 to 0.88, and keeping the
    full arguments cost 2.7x the chunks to recover one query.  A result is still
    in the turn, which is what `turn_read` reads.
    """
    messages = [
        {"role": "user", "content": "算一下"},
        {
            "role": "assistant",
            "content": "算好了",
            "tool_calls": [
                {"id": "c1", "function": {"name": "calc", "arguments": '{"e": "99*8"}'}}
            ],
        },
        {"role": "tool", "content": "x" * 5000, "tool_call_id": "c1"},
    ]

    text = embed_text(messages)

    assert "算一下" in text and "算好了" in text
    assert "calc" not in text and "99*8" not in text
    assert "xxxx" not in text


def test_the_reasoning_is_part_of_what_a_turn_is_about() -> None:
    """**The only prose a tool-calling assistant message usually has.**

    `thinking` lives on the message and not in `content`, so `message_text`
    never sees it; measured on the live log it was 83.6k characters against a
    73.9k semantic text and reached neither leg.  Every assistant message's
    reasoning is taken, not only the last: the one beside a tool call says what
    the model is doing, and the one beside the answer says what it concluded.
    """
    messages = [
        {"role": "user", "content": "看看磁盘"},
        {
            "role": "assistant",
            "content": "",
            "thinking": "我先查一下挂载点。",
            "tool_calls": [{"id": "c1", "function": {"name": "df", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "…", "tool_call_id": "c1"},
        {"role": "assistant", "content": "空间充足。", "thinking": "还剩 40%。"},
    ]

    text = embed_text(messages)

    assert "看看磁盘" in text
    assert "我先查一下挂载点。" in text and "还剩 40%。" in text
    assert "空间充足。" in text


def test_a_long_turn_becomes_several_chunks_overlapping_by_a_paragraph() -> None:
    """One vector for a long turn describes all of it a little and none of it
    well; the overlap is so a sentence on a boundary is held by one of them."""
    # Long enough to need more than one chunk: four paragraphs of about eight
    # hundred characters each, against a chunk of two thousand.
    paragraphs = [f"段落{number}" + "内容" * 400 for number in range(4)]
    chunks = embed_chunks("\n\n".join(paragraphs))

    assert len(chunks) > 1
    assert all(len(chunk) <= EMBED_CHUNK_CHARS for chunk in chunks)
    # The last paragraph of one chunk opens the next.
    first_lines = [chunk.split("\n\n")[0] for chunk in chunks[1:]]
    assert all(line.startswith("段落") for line in first_lines)
    assert len(set(chunks)) == len(chunks)


def test_a_limit_below_the_chunk_size_bounds_the_packing_and_loses_nothing() -> None:
    """**A small limit is the window's bound, not a cut taken off its end.**

    Every caller passes the embedder's `max_chars` — the endpoint's own input
    limit — so an endpoint with a small one used to pack a window of
    `EMBED_CHUNK_CHARS` and slice it to the limit afterwards.  What the slice
    removed sat in **no chunk at all**: not in the vector index, and not in
    anything else built from these chunks, in silence, because the row still had
    a vector and still counted as indexed.  Four paragraphs against this limit
    came back as one chunk of five hundred characters with the last two
    paragraphs simply absent from the file's vectors.

    Both halves of the fix are here, because they trade against each other: a
    paragraph that fits beside the carried overlap keeps it, and one that does
    not gets the chunk to itself — giving up the overlap, which is a repetition
    the previous chunk already holds, rather than the paragraph, which is text
    that would then be nowhere.
    """
    # Three hundred characters each against a limit of five hundred and twelve:
    # two of them do not fit in one chunk, and neither does the overlap.
    long_paragraphs = [f"段落{number}" + "内容" * 149 for number in range(4)]
    long_chunks = embed_chunks("\n\n".join(long_paragraphs), limit=512)

    assert all(len(chunk) <= 512 for chunk in long_chunks), long_chunks
    # One paragraph each, and each *whole*: the pairing is what says nothing was
    # cut, and the count is what says nothing was dropped.
    assert len(long_chunks) == len(long_paragraphs)
    assert all(
        paragraph in chunk
        for paragraph, chunk in zip(long_paragraphs, long_chunks, strict=True)
    )

    # A hundred and thirteen characters each: several fit, so the overlap fits
    # too and is still carried — the property the chunker exists for.
    short_paragraphs = [f"段落{number}" + "内容" * 55 for number in range(6)]
    short_chunks = embed_chunks("\n\n".join(short_paragraphs), limit=512)
    packed = "\n\n".join(short_chunks)

    assert all(len(chunk) <= 512 for chunk in short_chunks), short_chunks
    assert len(short_chunks) > 1
    assert all(paragraph in packed for paragraph in short_paragraphs)
    assert short_chunks[0].split("\n\n")[-1] == short_chunks[1].split("\n\n")[0]


def test_a_turn_with_nothing_to_embed_gets_no_vector() -> None:
    """A turn whose only message is a tool result renders as nothing.

    It is not an error and not a stall — there is simply nothing to say about
    it, and the keyword leg still holds whatever it contains.
    """
    messages = [{"role": "tool", "content": "4", "tool_call_id": "c1"}]

    assert embed_text(messages) == ""
    assert embed_chunks(embed_text(messages)) == []


@pytest.mark.asyncio
async def test_a_turn_with_nothing_to_embed_is_not_pending(tmp_path) -> None:
    """**And it does not make the index unready, which it used to.**

    "No vector" was the whole of the state a pending turn had, so a turn with
    nothing to embed was pending for ever: no sync could place a vector for it,
    `index_status` never came back clean, and the context plugin refuses to
    serve an index that is not.  One bare `send_message(agent, "")` the model
    answered with neither text nor a tool call was enough to keep the whole
    plugin down.  What a turn owes is a vector for its *text*.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    await store.save_turn(
        messages=[{"role": "tool", "content": "4", "tool_call_id": "c1"}],
        embedder=embedder,
    )

    status = store.index_status(embedder)

    assert status["pending"] == 0
    assert status["ready"] is True, status["problems"]


def test_the_two_indexes_do_not_hold_the_same_text() -> None:
    """The keyword leg keeps the summary; the vector leg must not.

    A vector that depended on a summary would have to be rebuilt every time one
    was written, and summaries are written long after the turn.
    """
    messages = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "答案"},
    ]

    # Normalized, so the characters are separated — that is what makes them
    # tokens, and it is why the assertion is spelled with the spaces in it.
    assert "水 电 站" in searchable_text(messages, summary="水电站")
    assert "水电站" not in embed_text(messages)


def test_tool_calls_and_results_are_what_a_turn_is_found_by() -> None:
    """**The exact leg's material, and the reason the vector does not hold it.**

    A path, an error string, a command or a hash is named literally by the query
    that wants it; an embedding of machine output is a poor handle on any of
    them.  So the keyword text keeps the calls and the results whole, while
    `embed_text` keeps the turn's own voice — the two legs are complements.
    """
    messages = [
        {"role": "user", "content": "算一下"},
        {
            "role": "assistant",
            "content": "算好了",
            "tool_calls": [
                {"id": "c1", "function": {"name": "calc", "arguments": '{"e": "99*8"}'}}
            ],
        },
        {"role": "tool", "content": "792", "tool_call_id": "c1"},
    ]

    text = searchable_text(messages)

    assert "calc" in text and "99*8" in text and "792" in text
    assert "calc" not in embed_text(messages)
    assert "99*8" not in embed_text(messages) and "792" not in embed_text(messages)


# --- the keyword leg ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_two_character_chinese_word_is_found(tmp_path) -> None:
    """The case FTS5 cannot do on its own: its tokenizer makes a run one token."""
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    await _corpus(store, embedder)

    assert 1 in _ids(await store.search("工具", embedder=embedder))


@pytest.mark.asyncio
async def test_an_english_word_and_a_number_are_found(tmp_path) -> None:
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    await _corpus(store, embedder)

    assert 2 in _ids(await store.search("trump", embedder=embedder))
    assert 3 in _ids(await store.search("792", embedder=embedder))


@pytest.mark.asyncio
async def test_a_term_that_survives_only_in_a_tool_result_is_found(tmp_path) -> None:
    """The exact leg's half of the complement: a result term is its to find.

    The vector deliberately gives the result up — it is evidence, not what the
    turn was about — and the keyword index keeps it whole, so a query naming a
    value, a path or a command prints the turn back.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    await store.save_turn(
        messages=[
            {"role": "user", "content": "算一下"},
            {
                "role": "assistant",
                "content": "算好了",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "calc", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "content": "792", "tool_call_id": "c1"},
        ],
        embedder=StubEmbedder(),
    )

    assert store._keyword_hits(textindex.match_expression("792"), 10, [], []) == [1]
    assert store._keyword_hits(textindex.match_expression("calc"), 10, [], []) == [1]


@pytest.mark.asyncio
async def test_characters_that_are_not_adjacent_do_not_match(tmp_path) -> None:
    """A term is a phrase, so adjacency is required — and only adjacency.

    Without this the index would degrade into "these characters are somewhere in
    the row", which on a Chinese history matches nearly everything.

    Asserted on the keyword leg, because the *search* answers from two legs and
    the semantic one has no threshold: a nearest-neighbour query always returns
    something, so a whole-search assertion here would be a test of the stub.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    await store.save_turn(
        messages=[{"role": "user", "content": "我 工"}], embedder=StubEmbedder()
    )

    expression = textindex.match_expression("工具")
    assert store._keyword_hits(expression, 10, [], []) == []
    # The two characters on their own are found, which is what says the miss
    # above is about adjacency rather than about the characters being absent.
    assert store._keyword_hits(textindex.match_expression("我 工"), 10, [], []) == [1]


@pytest.mark.asyncio
async def test_every_term_has_to_be_present(tmp_path) -> None:
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    await _corpus(store, embedder)

    both = textindex.match_expression("工具 类别")
    narrower = textindex.match_expression("工具 类别 水电站")

    assert store._keyword_hits(both, 10, [], []) == [1]
    # `水电站` is in no turn, so the AND has nothing to stand on.
    assert store._keyword_hits(narrower, 10, [], []) == []


@pytest.mark.asyncio
async def test_a_query_with_no_term_is_refused(tmp_path) -> None:
    store = TurnStore(tmp_path / "jack.turn.db")
    with pytest.raises(EmptyQuery):
        await store.search("***", embedder=StubEmbedder())


@pytest.mark.asyncio
async def test_a_bound_in_no_grammar_is_refused(tmp_path) -> None:
    """The same rule the browse follows: an unreadable bound is not "no
    results"."""
    store = TurnStore(tmp_path / "jack.turn.db")
    with pytest.raises(InvalidTimeBound):
        await store.search("工具", embedder=StubEmbedder(), since="上个月")


@pytest.mark.asyncio
async def test_a_window_narrows_a_search(tmp_path) -> None:
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    await store.save_turn(
        messages=CORPUS[0], embedder=embedder, created_at="2026-01-01T10:00:00+08:00"
    )

    assert _ids(await store.search("工具", embedder=embedder)) == [1]
    assert await store.search("工具", embedder=embedder, since="2026-06-01") == []


# --- the semantic leg ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_query_no_keyword_can_match_still_returns_turns(tmp_path) -> None:
    """The semantic leg is not a refinement of the keyword one — on a Chinese
    history it is often the leg that answers, because a term the text does not
    contain literally cannot be matched at all."""
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    await _corpus(store, embedder)

    hits = await store.search("计算", embedder=embedder)

    assert hits, "the semantic leg contributed nothing"
    assert store._keyword_hits(textindex.match_expression("计算"), 10, [], []) == []


@pytest.mark.asyncio
async def test_the_nearest_turn_comes_first(tmp_path) -> None:
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    await _corpus(store, embedder)

    nearest = await store._semantic_hits(["工具"], embedder, 3)

    # A mapping, in nearest-first order: the *order* is what the fusion reads and
    # the value is the similarity a recall gates on, so both halves are asserted
    # — the first turn is the one about this, and the similarities only ever
    # fall.  (`list(nearest) == [1, *list(nearest)[1:]]` stood here, which is
    # `nearest[0] == 1` written out at length, and the line below it says that.)
    assert next(iter(nearest)) == 1
    assert nearest[1] > 0.0
    assert list(nearest.values()) == sorted(nearest.values(), reverse=True)


@pytest.mark.asyncio
async def test_a_turn_with_several_chunks_is_a_hit_once(tmp_path) -> None:
    """`k` counts chunks, and the answer is turns — so the dedup is load-bearing.

    A vector search has no `GROUP BY` to do it in, which is why it happens in
    Python and why this is worth pinning down.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    long_turn = [
        {"role": "user", "content": "\n\n".join("工具" * 400 for _ in range(4))},
        {"role": "assistant", "content": "工具"},
    ]
    await store.save_turn(messages=long_turn, embedder=embedder)

    with sqlite3.connect(store.path) as connection:
        chunks = connection.execute("SELECT COUNT(*) FROM turn_chunk").fetchone()[0]
    assert chunks > 1

    hits = await store.search("工具", embedder=embedder)
    assert _ids(hits) == [1]


@pytest.mark.asyncio
async def test_a_second_sentence_can_only_add_a_turn(tmp_path) -> None:
    """Several questions are several chances; one cannot cost another's find.

    Each sentence is embedded on its own and a turn keeps its best similarity
    across them, so a turn only the second sentence names is still found — and
    the first sentence's turn is not lowered or removed by the second.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    for said in ("工具", "trump", "计算"):
        await store.save_turn(
            messages=[{"role": "user", "content": said}], embedder=embedder
        )

    ranked = await store.recall(sentences=["工具", "trump"], embedder=embedder)

    found = [turn_id for turn_id, _ in ranked]
    assert 1 in found and 2 in found, found


@pytest.mark.asyncio
async def test_a_recall_leaves_out_the_turns_already_in_hand(tmp_path) -> None:
    """A keep-list and a recall are one decision; a turn in both is a place spent twice.

    The keyword leg filters in SQL and the semantic leg's answer is filtered
    after fusion — a `vec0` KNN has no turn id to filter on — so the excluded
    turn is absent from both legs' contribution, and `limit` places go to turns
    that are actually missing.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    await store.save_turn(
        messages=[{"role": "user", "content": "工具 工具"}], embedder=embedder
    )
    await store.save_turn(
        messages=[{"role": "user", "content": "工具 trump"}], embedder=embedder
    )

    ranked = await store.recall(query="工具", embedder=embedder, limit=1, exclude=[1])

    assert [turn_id for turn_id, _ in ranked] == [2]


@pytest.mark.asyncio
async def test_a_period_recall_leaves_out_the_turns_already_in_hand(tmp_path) -> None:
    """The time-ranked branch has no similarity, so it filters the same way."""
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    for said in ("一", "二", "三"):
        await store.save_turn(
            messages=[{"role": "user", "content": said}], embedder=embedder
        )

    ranked = await store.recall(anchor="newest", limit=2, exclude=[2, 3])

    assert [turn_id for turn_id, _ in ranked] == [1]


# --- fusion, as a pure function -----------------------------------------------


def test_a_turn_both_legs_found_outranks_one_leg() -> None:
    fused = fuse_ranked({"keyword": [1, 2], "semantic": [2, 3]})
    assert [turn_id for turn_id, _ in fused][0] == 2


def test_one_leg_alone_still_answers() -> None:
    fused = fuse_ranked({"keyword": [7, 8], "semantic": []})
    assert [turn_id for turn_id, _ in fused] == [7, 8]
    assert fuse_ranked({"keyword": [], "semantic": []}) == []


def test_a_tie_falls_to_the_newer_turn() -> None:
    """Deliberately arbitrary, and deliberately stable: both legs agreeing on
    the same order is how ties actually arise."""
    fused = fuse_ranked({"keyword": [1, 2], "semantic": [1, 2]})
    assert [turn_id for turn_id, _ in fused] == [1, 2]
    assert fused == fuse_ranked({"keyword": [1, 2], "semantic": [1, 2]})


def test_agreement_between_the_legs_outweighs_one_first_place() -> None:
    """Second in both legs beats first in one: 2/62 against 1/61.

    Which is the property fusion by rank is *for* — a turn both legs put near
    the top is the one most likely to be what was meant, and no scoring scale
    had to be agreed on to say so.  (Worth stating because the intuition runs
    the other way: a first place feels like it should win.)
    """
    fused = dict(fuse_ranked({"keyword": [1, 2, 3], "semantic": [4, 2]}))
    assert fused[2] > fused[4]


def test_the_keyword_leg_is_fused_into_each_question() -> None:
    """**The words are evidence about a question, not about the merged list.**

    The keyword leg is one list and every question sees it; a turn keeps the
    score of its best question, so rank 1 in one question beats rank 2 in
    another.  A tie falls to the newer turn, the order `fuse_ranked` uses.
    """
    # Two questions, no words: the two rank-1 turns tie at 1/61, and the newer
    # one comes first; the rank-2 turns follow, newest of them first.
    assert db._fuse_questions([], [[1, 2], [3, 4]]) == [3, 1, 4, 2]
    # The words are one list, present in every question.
    assert db._fuse_questions([5], [[1, 2], [3, 4]]) == [5, 3, 1, 4, 2]


# --- the index is written with the turn, or not at all ------------------------


@pytest.mark.asyncio
async def test_saving_a_turn_writes_the_turn_both_indexes(tmp_path) -> None:
    path = tmp_path / "jack.turn.db"
    store = TurnStore(path)
    await store.save_turn(messages=CORPUS[0], embedder=StubEmbedder())

    with sqlite3.connect(path) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("turn", "turn_fts", "turn_chunk")
        }

    assert counts == {"turn": 1, "turn_fts": 1, "turn_chunk": 1}


@pytest.mark.asyncio
async def test_a_save_that_cannot_embed_stores_nothing(tmp_path) -> None:
    """The property that makes a failed save worth trusting.

    The hazard of embedding on the save path is a raise that had in fact stored
    the row, leaving nobody able to say whether it had.  Atomic means the
    question does not arise: if it raised, there is nothing.
    """
    path = tmp_path / "jack.turn.db"
    store = TurnStore(path)
    embedder = StubEmbedder()
    await store.save_turn(messages=CORPUS[0], embedder=embedder)

    with pytest.raises(RuntimeError, match="endpoint is down"):
        await store.save_turn(messages=CORPUS[1], embedder=FailingEmbedder())

    with sqlite3.connect(path) as connection:
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("turn", "turn_fts", "turn_chunk")
        ]
    assert counts == [1, 1, 1], counts
    assert store.count() == 1


@pytest.mark.asyncio
async def test_changing_the_model_reindexes_every_turn(tmp_path) -> None:
    """The cost of a new model, and the reason it is paid at startup rather than
    discovered at search time."""
    store = TurnStore(tmp_path / "jack.turn.db")
    first = StubEmbedder()
    await _corpus(store, first)

    second = StubEmbedder(identity="stub:two")
    await store.sync_indexes(second)

    assert sum(len(call) for call in second.calls) == len(CORPUS)
    assert store._turns_without_vectors() == []
    status = store.index_status(second)
    assert status["ready"] and status["vectors"] == len(CORPUS)


@pytest.mark.asyncio
async def test_a_changed_vector_text_contract_rebuilds_the_index(
    tmp_path, monkeypatch
) -> None:
    """**The text contract is part of the vector identity, and it has to be.**

    `textindex.RULES_VERSION` governs the keyword grammar and says nothing about
    which messages `embed_text` chooses; a vector is a function of its text, so
    changing the composition without changing the identity would leave old and
    new vectors ranked against each other.  The recorded identity carries
    `EMBED_TEXT_VERSION`, and a difference takes the same rebuild a changed
    model takes.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    embedder = StubEmbedder()
    await _corpus(store, embedder)
    embedded = sum(len(call) for call in embedder.calls)

    monkeypatch.setattr(db, "EMBED_TEXT_VERSION", "next")
    status = store.index_status(embedder)
    assert not status["ready"]
    assert any("text contract" in problem for problem in status["problems"])

    await store.sync_indexes(embedder)

    assert sum(len(call) for call in embedder.calls) > embedded
    assert store.index_status(embedder)["ready"]


@pytest.mark.asyncio
async def test_a_changed_keyword_text_contract_rebuilds_the_index(
    tmp_path, monkeypatch
) -> None:
    """**The fields are part of the keyword stamp, not only the tokenizer.**

    `textindex.RULES_VERSION` says what a term is; it does not say that a row
    now holds the tool calls and results.  Without the second half of the stamp,
    an old file would keep rows built from the old fields and a term only a
    result holds would be unsearchable for ever — silently, since the row count
    still matches the turn count.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    await store.save_turn(messages=CORPUS[0], embedder=StubEmbedder())
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE turn_fts SET search = 'stale'")
        connection.execute(
            "INSERT OR REPLACE INTO index_meta (key, value)"
            " VALUES ('text_version', 'old')"
        )

    monkeypatch.setattr(db, "SEARCH_TEXT_VERSION", "next")
    store._sync_text_index()

    with sqlite3.connect(store.path) as connection:
        indexed = connection.execute("SELECT search FROM turn_fts").fetchone()[0]
        stamp = connection.execute(
            "SELECT value FROM index_meta WHERE key = 'text_version'"
        ).fetchone()[0]
    assert indexed == searchable_text(CORPUS[0])
    assert stamp == f"next|{textindex.RULES_VERSION}"


@pytest.mark.asyncio
async def test_a_rebuild_embeds_the_turns_in_batches(tmp_path, monkeypatch) -> None:
    """**A changed model makes every turn missing**, so one request per turn is a
    round trip per turn of the log.

    `SYNC_BATCH`'s own docstring reads as if it bounds this path, and both other
    stores keep it (`ToolStore.sync_indexes`, `CabinetStore.sync`); this loop was
    the one that awaited `embed` once per missing turn.  The division stays what
    the other two state — the store bounds a call and the caller finishes the
    index — but the call carries a batch rather than one turn.
    """
    monkeypatch.setattr(db, "SYNC_BATCH", 2)
    store = TurnStore(tmp_path / "jack.turn.db")
    await _corpus(store, StubEmbedder())
    second = StubEmbedder(identity="stub:two")

    await store.sync_indexes(second)

    assert [len(call) for call in second.calls] == [2, 1], "three turns, two requests"
    assert store.index_status(second)["ready"]


@pytest.mark.asyncio
async def test_a_bound_list_longer_than_sqlite_takes_is_bound_in_chunks(
    tmp_path,
) -> None:
    """**The cap is on the statement, not on the answer**, and this is the query
    that binds it.

    Nothing the caller passes bounds a search's shortlist: every question brings
    up to `limit * 4` ids of its own and the keyword leg as many again, so about
    forty sentences reach SQLite's ceiling for bound variables (32766, measured)
    and raise `OperationalError: too many SQL variables` — a search that does not
    answer at all.  All three stores read their page through this one query,
    which is where the cap therefore is, and the answer is chunks rather than a
    shorter list: the ranking is already complete when the ids get here, so a
    page that had quietly lost its tail would be the one thing a caller cannot
    tell from a short page.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    await store.save_turn(messages=CORPUS[0], embedder=StubEmbedder())
    await store.save_turn(messages=CORPUS[1], embedder=StubEmbedder())

    with store._connect() as connection:
        rows = rows_in_order(
            connection,
            [1, *range(3, 40_000), 2],
            select="SELECT rowid AS turn_id, * FROM turn",
            column="rowid",
            key="turn_id",
            limit=5,
        )

    # The order is the caller's, and an id past the first chunk is found like
    # any other: 2 sits at the end of a list forty thousand long.
    assert [int(row["turn_id"]) for row in rows] == [1, 2]


@pytest.mark.asyncio
async def test_changing_the_width_rebuilds_the_table(tmp_path) -> None:
    path = tmp_path / "jack.turn.db"
    store = TurnStore(path)
    await _corpus(store, StubEmbedder())

    narrow = StubEmbedder(identity="stub:three", vocab=("工具", "trump"))
    await store.sync_indexes(narrow)

    with sqlite3.connect(path) as connection:
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'turn_vec'"
        ).fetchone()[0]
    assert f"float[{narrow.dimension}]" in ddl
    assert "distance_metric=cosine" in ddl
    assert store.index_status(narrow)["ready"]


@pytest.mark.asyncio
async def test_index_status_names_what_is_missing(tmp_path) -> None:
    """Readiness is looked at, not remembered — a sync that died halfway leaves
    nothing to remember it by."""
    path = tmp_path / "jack.turn.db"
    store = TurnStore(path)
    embedder = StubEmbedder()
    await _corpus(store, embedder)
    assert store.index_status(embedder) == {
        "turns": 3,
        "vectors": 3,
        "keyword": 3,
        "pending": 0,
        "ready": True,
        "problems": [],
    }

    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM turn_chunk WHERE turn_id = 2")

    status = store.index_status(embedder)
    assert not status["ready"]
    assert status["problems"] == ["1 of 3 turns have no vector"]


# --- and the loop is not held while it happens --------------------------------


@pytest.mark.asyncio
async def test_the_writes_happen_on_another_thread(tmp_path) -> None:
    """`async` is not what keeps the loop free; the thread hop is.

    SQLite here is blocking, and this server answers every agent instance from
    one loop — so an insert issued on that loop stalls an unrelated
    conversation.  Asserting the thread rather than the timing is deliberate: it
    is the mechanism, and it does not depend on how slow the machine is.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    seen: dict[str, int] = {}
    original = store._write_turn

    def spy(record, vectors):
        seen["writes"] = threading.get_ident()
        return original(record, vectors)

    store._write_turn = spy  # type: ignore[method-assign]
    seen["loop"] = threading.get_ident()
    await store.save_turn(messages=CORPUS[0], embedder=StubEmbedder())

    assert seen["writes"] != seen["loop"]


@pytest.mark.asyncio
async def test_a_model_that_answers_nothing_is_not_a_finished_index(tmp_path) -> None:
    """**An empty answer is a failure, not the end of the work.**

    The build loop had an arm that read "the model answered with nothing for
    this batch" as "the index is as complete as it will get" and stopped: the
    turns that were owed a vector stayed owed, `index_status` counted the
    missing ones, and the loop that was supposed to place them had already
    returned as though it were done.  `embed_rows` refuses a short answer, so
    that arm was unreachable — and what it *said* was the one thing an empty
    answer does not mean.
    """

    class Silent(StubEmbedder):
        async def embed(self, texts):
            return []

    store = TurnStore(tmp_path / "jack.turn.db")
    await store.save_turn(messages=CORPUS[0], embedder=StubEmbedder())

    with pytest.raises(RuntimeError) as raised:
        await store.sync_indexes(Silent(identity="stub:silent"))

    assert "searchable only by keyword" in str(raised.value)


@pytest.mark.asyncio
async def test_a_partial_embedding_is_refused_before_the_turn_is_written(
    tmp_path,
) -> None:
    """**A short answer at the save path is a hole `sync_indexes` cannot see.**

    `embed_rows` refuses an answer with fewer vectors than it asked for, and so
    does every other embedding path — but `save_turn` wrote whatever it was
    handed.  A *partial* answer is the shape that matters: the turn gets
    `turn_chunk` rows for some of its chunks, and `_turns_without_vectors`
    selects turns with no chunk row at all, so nothing would ever select this one
    again — `index_status` would report `ready: True` over an index that is
    missing vectors, which is exactly the hole the sibling's check names.  The
    check has to sit before `_write_turn`, so a refused save is a save that
    stored nothing.
    """

    class Partial(StubEmbedder):
        """One vector short, so a chunk row is written for only some chunks."""

        async def embed(self, texts):
            return await super().embed(texts[:-1])

    store = TurnStore(tmp_path / "jack.turn.db")
    # Two paragraphs over the chunk size, so the turn is more than one chunk —
    # without that a short answer is the whole answer and the row stays
    # repairable.
    messages = [
        {"role": "user", "content": "x" * 1200},
        {"role": "assistant", "content": "y" * 1200},
    ]

    with pytest.raises(RuntimeError) as raised:
        await store.save_turn(messages=messages, embedder=Partial())

    assert "searchable only by keyword" in str(raised.value)
    assert store.count() == 0, "a save that raised stored nothing"


@pytest.mark.asyncio
async def test_the_loop_keeps_running_while_a_turn_is_embedded(tmp_path) -> None:
    """Stated the way it matters: something else gets to run during a save.

    **Coordination rather than a stopwatch.**  This counted heartbeat ticks
    against a wall clock — at least three of them inside a fifty-millisecond
    sleep — which says what it means only on an idle machine: under `-n auto` on
    a busy one the heartbeat is scheduled late, the count falls short, and the
    red test is about the runner rather than about the loop.  The embedder now
    waits for the heartbeat to have run, so passing does not depend on how fast
    the machine is, and a loop that really is held fails on a bound no amount of
    slowness can reach.
    """
    ran = asyncio.Event()

    class Slow(StubEmbedder):
        async def embed(self, texts):
            # Nothing here completes until another task has run on this loop,
            # and five seconds is not a timing: it is the point past which the
            # only explanation left is a held loop, since a free one wakes the
            # heartbeat in milliseconds however loaded it is.
            await asyncio.wait_for(ran.wait(), timeout=5)
            return await super().embed(texts)

    async def heartbeat() -> None:
        await asyncio.sleep(0.005)
        ran.set()

    store = TurnStore(tmp_path / "jack.turn.db")
    beat = asyncio.create_task(heartbeat())
    try:
        await store.save_turn(messages=CORPUS[0], embedder=Slow())
    finally:
        beat.cancel()

    assert ran.is_set(), "the loop was held while the turn was embedded"


def test_a_python_that_cannot_load_the_extension_says_which_it_is(
    tmp_path, monkeypatch
) -> None:
    """The hard dependency failing, and failing legibly.

    Two causes look the same from the outside — an interpreter built without
    extension support, and a wheel that never made it into the environment —
    and they need different things done about them.  So the message is not
    allowed to be whatever `sqlite3` says about a function that does not exist.
    """
    import sqlite_vec

    def explode(connection):
        raise OSError("cannot open shared object file")

    monkeypatch.setattr(sqlite_vec, "load", explode)

    with pytest.raises(VectorIndexUnavailable, match="cannot load sqlite-vec"):
        TurnStore(tmp_path / "jack.turn.db")
