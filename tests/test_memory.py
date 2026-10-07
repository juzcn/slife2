"""The turn store, on its own.

Every other test reaches the store through two MCP hops and a scripted model, so
what they can assert is what survives the trip.  These are about the store
itself: the shape of the table, what a saved turn reads back as, and the two
rules that live on the save path rather than in any caller — tool results are
compacted, and a damaged row does not take the rest of the history with it.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from slife2.clock import now
from slife2.memory import (
    TOOL_RESULT_CHARS,
    TurnStore,
    compact_tool_results,
    safe_agent_name,
    store_for,
)
from slife2.paths import DATA_ENV_VAR
from slife2.timeutil import InvalidTimeBound

pytestmark = pytest.mark.unit


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


def test_a_saved_turn_reads_back_whole(tmp_path) -> None:
    """Every value that went in comes out, unmangled."""
    store = TurnStore(tmp_path / "jack.turn.db")
    messages = [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": "It is 42."},
    ]

    turn_id = store.save_turn(
        messages=messages,
        channel="human",
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
    assert record.channel == "human"
    assert record.who_helped == "jack"
    assert record.what_model == "deepseek/deepseek-flash"
    # The bill for the turn and the size it had grown to are different numbers,
    # which is the whole reason there are two columns.
    assert (record.token_count, record.context_tokens) == (245, 135)
    assert record.created_at == "2026-10-07T08:00:00+08:00"
    assert record.completed_at == "2026-10-07T08:00:02+08:00"
    assert (record.summary, record.tags) == ("", "")


def test_turns_are_newest_first(tmp_path) -> None:
    """Retrieval is by time, and the order is the rowid's — not the clock's.

    Two turns written in the same second are the common case, so a tie has to
    break on something monotonic rather than on a timestamp that cannot.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    for text in ("first", "second", "third"):
        store.save_turn(messages=[{"role": "user", "content": text}])

    records, total = store.turns()
    assert [r.messages[0]["content"] for r in records] == [
        "third",
        "second",
        "first",
    ]
    assert total == 3

    records, _ = store.turns(limit=2)
    assert [r.messages[0]["content"] for r in records] == ["third", "second"]


def test_a_damaged_row_does_not_hide_the_others(tmp_path) -> None:
    """One unreadable turn is one turn, not a history that cannot be read.

    Refusing to return anything because one row's JSON is half-written turns a
    small problem into a total one, and the rows either side of it are fine.
    """
    path = tmp_path / "jack.turn.db"
    store = TurnStore(path)
    store.save_turn(messages=[{"role": "assistant", "c": 1}])
    store.save_turn(messages=[{"role": "assistant", "c": 2}])

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE turn SET messages = 'not json' WHERE rowid = 1")

    records, _ = store.turns()
    assert [r.messages for r in records] == [[{"role": "assistant", "c": 2}], []]
    assert records[1].messages == []


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

    with caplog.at_level(logging.ERROR, logger="slife2.memory"):
        first = TurnStore(path)
        TurnStore(path)

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, errors
    assert str(path) in errors[0]
    # The count is what makes the report necessary: it is not an error, it is a
    # truthful zero.
    assert first.count() == 0


def test_surrogates_are_normalised_rather_than_losing_the_turn(tmp_path) -> None:
    """Text SQLite cannot encode must not cost the whole record.

    Not exotic input: a provider's token stream is JSON, and CPython's `json`
    leaves `\\uXXXX` pairs uncombined — so an emoji arrives as *two* surrogate
    characters, and an ordinary answer containing one would fail to bind and
    take the whole turn with it.  The pair is a character and wants putting back
    together; a lone surrogate is not one and becomes U+FFFD.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    emoji_as_escaped = json.loads('"\\ud83d\\ude00"')

    turn_id = store.save_turn(
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


def test_agents_are_isolated_by_file(tmp_path, monkeypatch) -> None:
    """Written to that agent's database, and reachable from no other.

    Isolation is a property of the filesystem here, not a `WHERE` clause, so
    there is no query that could forget to filter.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    jack, jill = store_for("jack"), store_for("jill")

    jack.save_turn(messages=[{"role": "user", "content": "mine"}])

    assert jack.path != jill.path
    assert jack.count() == 1
    assert jill.count() == 0


def test_an_oversized_tool_result_keeps_its_head_and_tail() -> None:
    """Tool output is reproducible, so the record keeps a digest — and says so.

    Truncation is announced rather than silent: a model reading this back later
    has to know it is shortened, and how to get the rest.  A digest that looked
    complete would be a lie it could reason from.
    """
    big = "A" * 5000 + "B" * 5000
    message = {"role": "tool", "content": big, "tool_call_id": "c1", "name": "calc"}

    (compacted,) = compact_tool_results([message], budget=100)

    assert compacted["content"].startswith("A")
    assert compacted["content"].endswith("B")
    assert len(compacted["content"]) < len(big)
    assert "compacted at save" in compacted["content"]
    assert "10000 chars" in compacted["content"]
    assert "calc" in compacted["content"]
    # The caller's message is untouched — the live conversation still needs the
    # whole result, and the model may still be reasoning about it.
    assert message["content"] == big


def test_a_result_that_fits_is_left_alone() -> None:
    small = {"role": "tool", "content": "42", "tool_call_id": "c1"}
    assert compact_tool_results([small]) == [small]
    assert len("42") < TOOL_RESULT_CHARS


def test_compaction_does_not_compound() -> None:
    """A turn that is saved twice is not compacted twice.

    The marker is the check, so it has to be distinctive enough that a tool
    which happened to return those words cannot be mistaken for one.
    """
    message = {"role": "tool", "content": "x" * 40000, "tool_call_id": "c1"}

    once = compact_tool_results([message], budget=100)
    twice = compact_tool_results(once, budget=100)

    assert twice[0]["content"] == once[0]["content"]


def test_only_tool_results_are_compacted() -> None:
    """The assistant's own words are the record's point; they are never cut."""
    message = {"role": "assistant", "content": "y" * 40000}
    assert compact_tool_results([message], budget=100) == [message]


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


def _turn(store: TurnStore, question: str, answer: str, *, at: str, tokens: int = 0):
    return store.save_turn(
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ],
        created_at=at,
        completed_at=at,
        token_count=tokens,
    )


def test_a_window_bounds_what_comes_back_and_what_total_counts(tmp_path) -> None:
    """`total` is the window's, not the table's.

    That is the number that answers "is there another page", and a total over
    the whole table would answer it wrongly for every window that is not the
    whole history.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    _turn(store, "old", "a", at="2026-01-01T10:00:00+08:00")
    _turn(store, "middle", "b", at="2026-06-01T10:00:00+08:00")
    _turn(store, "new", "c", at="2026-10-01T10:00:00+08:00")

    records, total = store.turns(since="2026-05-01", limit=5)
    assert [table(record) for record in records] == ["new", "middle"]
    assert total == 2

    records, total = store.turns(since="2026-05-01", until="2026-09-30", limit=5)
    assert [table(r) for r in records] == ["middle"] and total == 1

    records, total = store.turns(limit=5)
    assert [table(r) for r in records] == ["new", "middle", "old"] and total == 3


def table(record) -> str:
    return record.messages[0]["content"]


def test_a_relative_bound_is_resolved_before_it_reaches_sql(tmp_path) -> None:
    """A word a model wrote narrows the window like a date does."""
    store = TurnStore(tmp_path / "jack.turn.db")
    _turn(store, "long ago", "a", at="2001-01-01T10:00:00+08:00")
    _turn(store, "today", "b", at=now())

    records, total = store.turns(since="today", limit=5)
    assert [table(r) for r in records] == ["today"] and total == 1


def test_a_bound_in_no_grammar_is_refused_rather_than_matching_nothing(
    tmp_path,
) -> None:
    """The failure this window must never have: an answer that looks like one.

    SQLite compares an unknown string lexicographically and matches no rows, so
    a bound nobody understood used to read exactly like a history with nothing
    in it.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    _turn(store, "anything", "a", at=now())

    with pytest.raises(InvalidTimeBound):
        store.turns(since="上个月", limit=5)


def test_paging_walks_back_one_page_at_a_time(tmp_path) -> None:
    """Ten turns, three pages, each turn exactly once.

    Paging on `offset` is only safe because the order is the rowid's: the
    timestamps here are all identical, which is the case an ordering by
    `created_at` would resolve differently from one call to the next.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    for number in range(10):
        _turn(store, f"turn {number}", "a", at="2026-10-07T10:00:00+08:00")

    seen: list[str] = []
    for offset in (0, 3, 6, 9):
        records, total = store.turns(limit=3, offset=offset)
        seen.extend(table(record) for record in records)
        assert total == 10

    assert seen == [f"turn {number}" for number in reversed(range(10))]
    assert len(set(seen)) == 10


def test_a_listing_carries_both_halves_of_the_exchange(tmp_path) -> None:
    """What was asked and what was answered, which no column holds.

    v1 keeps the user's message in a column and lists rows straight out of SQL.
    Here the turn is one list of messages, so the browse has to read both ends
    of it out — and the answer is the *last* assistant message with prose in it,
    not the last assistant message, which on a turn that called a tool is a
    message with no text at all.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    store.save_turn(
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


def test_a_cut_message_says_it_was_cut(tmp_path) -> None:
    """`…`, because a silent cut is a short answer somebody acts on.

    The listing is what a caller decides from; it has no way to know the message
    continued except by being told.
    """
    store = TurnStore(tmp_path / "jack.turn.db")
    _turn(store, "x" * 5000, "y" * 5000, at=now())

    (record,), _ = store.turns(limit=1)
    listing = record.to_listing(chars=10)

    assert listing["user_message"] == "x" * 10 + "…"
    assert listing["assistant_message"] == "y" * 10 + "…"


def test_an_image_in_the_message_is_named_in_the_listing(tmp_path) -> None:
    """A model reading a one-line summary can act on `[image_url]`; it cannot
    act on the base64 that is actually stored, and it cannot act on silence."""
    store = TurnStore(tmp_path / "jack.turn.db")
    store.save_turn(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            },
            {"role": "assistant", "content": "A cat."},
        ],
        created_at=now(),
    )

    (record,), _ = store.turns(limit=1)
    listing = record.to_listing()

    assert "what is this?" in listing["user_message"]
    assert "[image_url]" in listing["user_message"]
    assert "base64" not in listing["user_message"]


def test_a_turn_is_read_back_by_its_id(tmp_path) -> None:
    store = TurnStore(tmp_path / "jack.turn.db")
    first = _turn(store, "first", "a", at=now())
    _turn(store, "second", "b", at=now())

    found = store.turn(first)
    assert found is not None and found.messages[0]["content"] == "first"
    assert store.turn(999) is None
