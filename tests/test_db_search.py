"""Finding a turn by what it was about: the two legs, the fusion, the indexes.

The parts worth testing are the ones a reader would otherwise have to take on
faith.  That a Chinese word is found at all — FTS5's tokenizer sees a run of
them as one token, so this only works because `slife2.textindex` separated the
characters, and the test that says so is the one that fails if anybody removes
that step.  That the save is atomic, because a save that raised and stored a row
anyway is the failure v1 shipped.  And that the event loop keeps running while a
turn is embedded, because `async` alone does not make that true.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from slife2 import textindex
from slife2.db import (
    EMBED_CHUNK_CHARS,
    TurnStore,
    VectorIndexUnavailable,
    embed_chunks,
    embed_text,
    fuse_ranked,
    searchable_text,
)
from slife2.textindex import EmptyQuery
from slife2.timeutil import InvalidTimeBound
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
        await store.save_turn(messages=messages, embedder=embedder, channel="human")


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


def test_a_tool_result_is_not_what_a_turn_is_about() -> None:
    """The call is kept, the thing it answered is not.

    Measured on v1's turn log, tool results were 56–99% of a turn's text, so a
    vector built on them describes "an agent ran tools" and every turn lands in
    one narrow band.  The call's own name and arguments stay, because "the turn
    where it used the calculator" is a real question.
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
    assert "calc" in text and "99*8" in text
    assert "xxxx" not in text


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


def test_a_turn_with_nothing_to_embed_gets_no_vector() -> None:
    """A turn whose only message is a tool result renders as nothing.

    It is not an error and not a stall — there is simply nothing to say about
    it, and the keyword leg still holds whatever it contains.
    """
    messages = [{"role": "tool", "content": "4", "tool_call_id": "c1"}]

    assert embed_text(messages) == ""
    assert embed_chunks(embed_text(messages)) == []


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

    nearest = await store._semantic_hits("工具", embedder, 3)

    assert nearest[0] == 1


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

    v1's complaint about embedding on the save path was a raise that had in fact
    stored the row, leaving nobody able to say whether it had.  Atomic means the
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
async def test_the_loop_keeps_running_while_a_turn_is_embedded(tmp_path) -> None:
    """Stated the way it matters: something else gets to run during a save."""

    class Slow(StubEmbedder):
        async def embed(self, texts):
            await asyncio.sleep(0.05)
            return await super().embed(texts)

    store = TurnStore(tmp_path / "jack.turn.db")
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        await store.save_turn(messages=CORPUS[0], embedder=Slow())
    finally:
        beat.cancel()

    assert ticks >= 3, f"the loop was held: {ticks} ticks"


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
