"""The turn store, on its own.

Every other test reaches the store through two MCP hops and a scripted model, so
what they can assert is what survives the trip.  These are about the store
itself: the shape of the table, what a saved turn reads back as, and the two
rules that live on the save path rather than in any caller — an image becomes a
note, and a damaged row does not take the rest of the history with it.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from slife2.helpers.clock import now
from slife2.helpers.timeutil import InvalidTimeBound
from slife2.libs.host.paths import DATA_ENV_VAR
from slife2.libs.store.catalogue import ToolStore
from slife2.libs.store.db import (
    TurnStore,
    message_text,
    safe_agent_name,
    store_for,
    strip_images,
)
from slife2.libs.store.index import _rowid
from slife2.libs.turn import attachments
from tests.fakes import StubEmbedder

pytestmark = pytest.mark.unit

#: Saving a turn now embeds it, and these tests are about what the store does
#: with a turn rather than about the model that produced the vector — so they
#: all save through the same stub.  It is deterministic, which is what lets a
#: semantic assertion mean something; see `tests.fakes.StubEmbedder`.
EMBEDDER = StubEmbedder()


def test_the_table_is_v1s_schema(tmp_path) -> None:
    """The columns are the contract — including the ones nothing writes yet.

    Adding a table later is what `CREATE TABLE IF NOT EXISTS` is for; adding a
    *column* to a table that already has rows is the thing this module has no
    mechanism for.  So `summary` and `tags` exist now, empty, rather than
    arriving with the feature that fills them.

    One divergence from v1: there is no `user_message`.  v1 keeps the user's
    half beside the assistant's; here the turn is one list and the user's
    message is its first element, so a column for it would be a second copy.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    with sqlite3.connect(store.path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(turn)")]

    assert columns == [
        "messages",
        "summary",
        "tags",
        "created_at",
        "completed_at",
        "channel",
        "who_helped",
        "what_model",
        "token_count",
        "context_tokens",
    ]


@pytest.mark.asyncio
async def test_a_saved_turn_reads_back_whole(tmp_path) -> None:
    """Every value that went in comes out, unmangled."""
    store = TurnStore(tmp_path / "jack.turn.db")
    messages = [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": "It is 42."},
    ]

    turn_id = await store.save_turn(
        embedder=EMBEDDER,
        messages=messages,
        channel="tui",
        who_helped="jack",
        what_model="deepseek/deepseek-flash",
        token_count=245,
        context_tokens=135,
        created_at="2026-10-07T08:00:00+08:00",
        completed_at="2026-10-07T08:00:02+08:00",
    )

    assert turn_id == 1
    assert store.count() == 1
    (record,), _ = store.turns()
    assert record.turn_id == turn_id
    assert record.messages == messages
    assert record.channel == "tui"
    assert record.who_helped == "jack"
    assert record.what_model == "deepseek/deepseek-flash"
    # The bill for the turn and the size it had grown to are different numbers,
    # which is the whole reason there are two columns.
    assert (record.token_count, record.context_tokens) == (245, 135)
    assert record.created_at == "2026-10-07T08:00:00+08:00"
    assert record.completed_at == "2026-10-07T08:00:02+08:00"
    assert (record.summary, record.tags) == ("", "")


@pytest.mark.asyncio
async def test_turns_are_newest_first(tmp_path) -> None:
    """Retrieval is by time, and the order is the rowid's — not the clock's.

    Two turns written in the same second are the common case, so a tie has to
    break on something monotonic rather than on a timestamp that cannot.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    for text in ("first", "second", "third"):
        await store.save_turn(
            embedder=EMBEDDER, messages=[{"role": "user", "content": text}]
        )

    records, total = store.turns()
    assert [r.messages[0]["content"] for r in records] == [
        "third",
        "second",
        "first",
    ]
    assert total == 3

    records, _ = store.turns(limit=2)
    assert [r.messages[0]["content"] for r in records] == ["third", "second"]


@pytest.mark.asyncio
async def test_a_damaged_row_does_not_hide_the_others(tmp_path) -> None:
    """One unreadable turn is one turn, not a history that cannot be read.

    Refusing to return anything because one row's JSON is half-written turns a
    small problem into a total one, and the rows either side of it are fine.
    """
    path = tmp_path / "jack.turn.db"
    store = TurnStore(path)
    await store.save_turn(embedder=EMBEDDER, messages=[{"role": "assistant", "c": 1}])
    await store.save_turn(embedder=EMBEDDER, messages=[{"role": "assistant", "c": 2}])

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE turn SET messages = 'not json' WHERE rowid = 1")

    records, _ = store.turns()
    assert [r.messages for r in records] == [[{"role": "assistant", "c": 2}], []]
    assert records[1].messages == []


def test_an_insert_that_recorded_nothing_is_not_a_rowid_of_zero() -> None:
    """**`lastrowid or 0` is a wrong fact, not a default.**

    A rowid is what names a row to the keyword index, the vector index and the
    live-context list, so an INSERT that recorded nothing used to leave all
    three agreeing on `0` — a turn nothing could ever find, in an index that
    counted as complete.  `lastrowid` is `None` only when the statement inserted
    nothing, and that is a fault to name rather than a case to carry on from.
    """

    class Nothing:
        lastrowid = None

    with pytest.raises(RuntimeError) as raised:
        _rowid(Nothing(), what="a turn")
    assert "a turn" in str(raised.value)


def test_a_file_from_the_previous_schema_is_reported(tmp_path, caplog) -> None:
    """An old file is not migrated, so it has to be said rather than discovered.

    There is no migration layer by design, and the file it leaves behind is the
    confusing kind of broken: the old `turns` table stays, an empty `turn`
    appears beside it, and every read afterwards honestly reports no history.
    Once per file, too — the store is built on every call, and the same
    complaint on every turn is a log nobody reads.
    """
    path = tmp_path / "jack.turn.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, prompt TEXT)")
        connection.execute("INSERT INTO turns (prompt) VALUES ('from before')")

    with caplog.at_level(logging.ERROR, logger="slife2.libs.store.db"):
        first = TurnStore(path)
        TurnStore(path)

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, errors
    assert str(path) in errors[0]
    # The count is what makes the report necessary: it is not an error, it is a
    # truthful zero.
    assert first.count() == 0


@pytest.mark.asyncio
async def test_surrogates_are_normalised_rather_than_losing_the_turn(tmp_path) -> None:
    """Text SQLite cannot encode must not cost the whole record.

    Not exotic input: a provider's token stream is JSON, and CPython's `json`
    leaves `\\uXXXX` pairs uncombined — so an emoji arrives as *two* surrogate
    characters, and an ordinary answer containing one would fail to bind and
    take the whole turn with it.  The pair is a character and wants putting back
    together; a lone surrogate is not one and becomes U+FFFD.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    emoji_as_escaped = json.loads('"\\ud83d\\ude00"')

    turn_id = await store.save_turn(
        embedder=EMBEDDER,
        messages=[
            {"role": "user", "content": "an emoji: " + emoji_as_escaped},
            {"role": "assistant", "content": "half a character: \ud800"},
        ],
    )

    assert turn_id == 1
    (record,), _ = store.turns()
    assert record.messages == [
        {"role": "user", "content": "an emoji: \U0001f600"},
        {"role": "assistant", "content": "half a character: �"},
    ]


@pytest.mark.asyncio
async def test_agents_are_isolated_by_file(tmp_path, monkeypatch) -> None:
    """Written to that agent's database, and reachable from no other.

    Isolation is a property of the filesystem here, not a `WHERE` clause, so
    there is no query that could forget to filter.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    jack, jill = store_for("jack"), store_for("jill")

    await jack.save_turn(
        embedder=EMBEDDER, messages=[{"role": "user", "content": "mine"}]
    )

    assert jack.path != jill.path
    assert jack.count() == 1
    assert jill.count() == 0


@pytest.mark.asyncio
async def test_an_oversized_tool_result_is_kept_whole(tmp_path) -> None:
    """**The record keeps what happened, and the second rule is gone.**

    A result over a size was saved as a head-and-tail digest, on the argument
    that tool output is reproducible — re-run the tool and the answer comes
    back.  Two things were wrong with that, and the second is the one that
    decided it: nothing checked the argument (a `git status` after a commit
    answers differently the second time), and *which* part of an output matters
    is the tool designer's judgement rather than the store's.

    What a model is shown is bounded where text enters a context — the harness
    windows a result, and the cabinet's `dump_read` reads back what it windowed
    out.  Bounding it here as well would be a second rule about the same thing,
    and one the model cannot read past.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    big = "A" * 50000

    turn = await store.save_turn(
        embedder=EMBEDDER,
        messages=[{"role": "tool", "content": big, "tool_call_id": "c1"}],
    )

    found = store.turn(turn)
    assert found is not None
    assert big in json.dumps(found.messages), "the whole of it, not a digest"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("jack", "jack"),
        ("jack/../jill", "jack_.._jill"),
        ("a/b", "a_b"),
        # Leading dots and slashes are stripped, so a traversal cannot survive
        # as a relative path.
        ("../../etc/passwd", "etc_passwd"),
        (" jack ", "jack"),
    ],
)
def test_an_agent_name_becomes_a_filename(name: str, expected: str) -> None:
    assert safe_agent_name(name) == expected


@pytest.mark.parametrize("name", ["..", "../", "...", "/", "", "  "])
def test_a_name_that_cannot_name_a_file_is_refused(name: str) -> None:
    """Saying no beats writing somewhere nobody intended."""
    with pytest.raises(ValueError):
        safe_agent_name(name)


# --- browsing: the window, the page, and the two halves -----------------------


async def _turn(
    store: TurnStore, question: str, answer: str, *, at: str, tokens: int = 0
):
    return await store.save_turn(
        embedder=EMBEDDER,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ],
        created_at=at,
        completed_at=at,
        token_count=tokens,
    )


@pytest.mark.asyncio
async def test_a_window_bounds_what_comes_back_and_what_total_counts(tmp_path) -> None:
    """`total` is the window's, not the table's.

    That is the number that answers "is there another page", and a total over
    the whole table would answer it wrongly for every window that is not the
    whole history.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    await _turn(store, "old", "a", at="2026-01-01T10:00:00+08:00")
    await _turn(store, "middle", "b", at="2026-06-01T10:00:00+08:00")
    await _turn(store, "new", "c", at="2026-10-01T10:00:00+08:00")

    records, total = store.turns(since="2026-05-01", limit=5)
    assert [table(record) for record in records] == ["new", "middle"]
    assert total == 2

    records, total = store.turns(since="2026-05-01", until="2026-09-30", limit=5)
    assert [table(r) for r in records] == ["middle"] and total == 1

    records, total = store.turns(limit=5)
    assert [table(r) for r in records] == ["new", "middle", "old"] and total == 3


def table(record) -> str:
    return record.messages[0]["content"]


@pytest.mark.asyncio
async def test_a_relative_bound_is_resolved_before_it_reaches_sql(tmp_path) -> None:
    """A word a model wrote narrows the window like a date does."""
    store = TurnStore(tmp_path / "jack.turn.db")
    await _turn(store, "long ago", "a", at="2001-01-01T10:00:00+08:00")
    await _turn(store, "today", "b", at=now())

    records, total = store.turns(since="today", limit=5)
    assert [table(r) for r in records] == ["today"] and total == 1


@pytest.mark.asyncio
async def test_a_bound_in_no_grammar_is_refused_rather_than_matching_nothing(
    tmp_path,
) -> None:
    """The failure this window must never have: an answer that looks like one.

    SQLite compares an unknown string lexicographically and matches no rows, so
    a bound nobody understood used to read exactly like a history with nothing
    in it.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    await _turn(store, "anything", "a", at=now())

    with pytest.raises(InvalidTimeBound):
        store.turns(since="上个月", limit=5)


@pytest.mark.asyncio
async def test_paging_walks_back_one_page_at_a_time(tmp_path) -> None:
    """Ten turns, three pages, each turn exactly once.

    Paging on `offset` is only safe because the order is the rowid's: the
    timestamps here are all identical, which is the case an ordering by
    `created_at` would resolve differently from one call to the next.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    for number in range(10):
        await _turn(store, f"turn {number}", "a", at="2026-10-07T10:00:00+08:00")

    seen: list[str] = []
    for offset in (0, 3, 6, 9):
        records, total = store.turns(limit=3, offset=offset)
        seen.extend(table(record) for record in records)
        assert total == 10

    assert seen == [f"turn {number}" for number in reversed(range(10))]
    assert len(set(seen)) == 10


@pytest.mark.asyncio
async def test_a_listing_carries_both_halves_of_the_exchange(tmp_path) -> None:
    """What was asked and what was answered, which no column holds.

    v1 keeps the user's message in a column and lists rows straight out of SQL.
    Here the turn is one list of messages, so the browse has to read both ends
    of it out — and the answer is the *last* assistant message with prose in it,
    not the last assistant message, which on a turn that called a tool is a
    message with no text at all.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    await store.save_turn(
        embedder=EMBEDDER,
        messages=[
            {"role": "user", "content": "what is 2+2?"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "4", "tool_call_id": "c1"},
            {"role": "assistant", "content": "It is 4."},
        ],
        created_at=now(),
        token_count=12,
    )

    (record,), _ = store.turns(limit=1)
    listing = record.to_listing()

    assert listing["user_message"] == "what is 2+2?"
    assert listing["assistant_message"] == "It is 4."
    assert listing["token_count"] == 12
    assert listing["created_at"] and listing["turn_id"] == record.turn_id


@pytest.mark.asyncio
async def test_a_cut_message_says_it_was_cut(tmp_path) -> None:
    """`…`, because a silent cut is a short answer somebody acts on.

    The listing is what a caller decides from; it has no way to know the message
    continued except by being told.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    await _turn(store, "x" * 5000, "y" * 5000, at=now())

    (record,), _ = store.turns(limit=1)
    listing = record.to_listing(chars=10)

    assert listing["user_message"] == "x" * 10 + "…"
    assert listing["assistant_message"] == "y" * 10 + "…"


@pytest.mark.asyncio
async def test_an_image_is_not_what_gets_stored(tmp_path) -> None:
    """The bytes stop at the model; the record keeps a note saying they were
    there.

    Ten megabytes of screenshot on the first message would be megabytes carried
    into every later read of the turn — and `turn_read` is a tool a model calls
    by itself.  What is *not* lost is which file it was: `@screenshot.png` is
    still in the text, because that is where the user put it.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    await store.save_turn(
        embedder=EMBEDDER,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this? @cat.png"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + "A" * 4000},
                    },
                ],
            },
            {"role": "assistant", "content": "A cat."},
        ],
        created_at=now(),
    )

    (record,), _ = store.turns(limit=1)
    text = message_text(record.messages[0]["content"])

    assert "@cat.png" in text, "the file is still named, so it can be sent again"
    assert "image/png" in text and "not stored" in text
    assert "~2 KB" in text, "how big it was, so a reader knows what it is missing"
    assert "A" * 100 not in text

    # And nothing anywhere in the row holds the payload.
    with sqlite3.connect(store.path) as connection:
        raw = connection.execute("SELECT messages FROM turn").fetchone()[0]
    assert "A" * 100 not in raw


@pytest.mark.asyncio
async def test_the_image_is_still_there_for_the_turn_that_used_it(tmp_path) -> None:
    """What the live conversation holds is not what the database holds.

    `save_turn` is called while the model that just read the picture may still
    be reasoning about it, so the caller's list is not the thing to rewrite.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this? @cat.png"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
    ]

    await store.save_turn(embedder=EMBEDDER, messages=messages, created_at=now())

    assert messages[0]["content"][1]["type"] == "image_url"


def test_the_stored_note_is_the_one_the_live_drop_leaves() -> None:
    """One sentence for one attachment, whichever side dropped the pixels.

    Two spellings would be two claims about the same picture, and the model has
    no way to tell which of them is the lie — the stored note is what a restored
    turn reads, and after a turn dies at the provider it is what the live
    conversation reads too.
    """
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}

    stored = strip_images([{"role": "user", "content": [part]}])

    assert stored[0]["content"][0]["text"] == attachments.image_note(part)


@pytest.mark.asyncio
async def test_a_turn_is_read_back_by_its_id(tmp_path) -> None:
    store = TurnStore(tmp_path / "jack.turn.db")
    first = await _turn(store, "first", "a", at=now())
    await _turn(store, "second", "b", at=now())

    found = store.turn(first)
    assert found is not None and found.messages[0]["content"] == "first"
    assert store.turn(999) is None


# --- the catalogue, and the argument it must be given -------------------------


def test_a_catalogue_nobody_told_the_config_keeps_its_rows(tmp_path) -> None:
    """**The destructive default**, which is what `known` is not allowed to be.

    `reset` withdraws the rows of a source the config no longer names — the
    rows, their vectors and their keyword documents — and a store told nothing
    cannot tell a stale row from one whose section it was never given.  That was
    the *default*: `known=()` meant "the config names nothing", so constructing
    a catalogue without the argument deleted every `plugin`/`mcp`/`rest` row in
    the file as it opened, and a `tool_search` afterwards answered about a
    catalogue nothing had gone wrong with.  A caller that has read the config
    passes the set (`slife2.plugins.toolhub.server.configured_sources`); a caller that has not
    passes nothing, and nothing is what the boot pass then does.
    """
    path = tmp_path / "tools.db"
    ToolStore(path, threshold=10, known={"arxiv"})  # creates the schema
    with sqlite3.connect(path) as connection:
        # Raw, because the point is the *open* and not the merge: one row of a
        # source the config does name.
        connection.execute(
            "INSERT INTO tool (name, category, source_id)"
            " VALUES ('arxiv__search', 'mcp', 'arxiv')"
        )

    ToolStore(path, threshold=10)  # ← the store nobody told anything

    with sqlite3.connect(path) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM tool")]
    assert names == ["arxiv__search"], "a store told nothing deleted its catalogue"


def test_a_catalogue_that_is_told_forgets_what_the_config_dropped(tmp_path) -> None:
    """The other half, so the fix is a distinction rather than a removal.

    A caller that *has* read the config says so by passing the set, and the boot
    pass keeps its meaning: a stored source the set does not name is one nothing
    will ever declare again, so it is deleted rather than marked — an entry the
    operator removed has to stop being a search hit at the next start as well as
    during the run.
    """
    path = tmp_path / "tools.db"
    ToolStore(path, threshold=10, known={"arxiv", "gone"})  # creates the schema
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO tool (name, category, source_id) VALUES"
            " ('arxiv__search', 'mcp', 'arxiv'), ('gone__thing', 'mcp', 'gone')"
        )

    ToolStore(path, threshold=10, known={"arxiv"})  # a restart, after `gone` went

    with sqlite3.connect(path) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM tool")]
    assert names == ["arxiv__search"]
