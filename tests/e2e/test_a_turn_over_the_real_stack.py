"""End to end: the whole stack as processes, and a provider that is a stub.

**What is real here, and what is not.**  Every plugin the system needs to run a
turn is a *process*, started by the real launcher, answering on a real port over
Streamable HTTP — the agent, the tool hub, the context store, the embeddings
server and the model backend, each in its own interpreter, spawned exactly as
`slife2` spawns them.  The one thing that is not real is the far end of the
provider hop: an OpenAI-compatible endpoint served by this process, so a turn
costs nothing and needs no key.

That is the seam worth having.  Every other test in this suite reaches one
server with the in-memory transport, or drives a server's own `build_server`
directly, which cannot show the things this exists for: that the launcher brings
the set up in an order that works, that a config written into a data directory
is the one every child reads, that a turn crosses five process boundaries, that
its answer comes back over the progress channel *and* as a result, and that the
conversation it produced is still there for the next turn.

**It is the slow lane.**  Nineteen daemons come up and go down, so it is marked
`e2e` and kept out of the default run (`-m e2e` is how it is asked for); see
README's Tests section.  A test starts from the stack the fixture hands it —
every process serving, and the hub linked to every plugin it requires — rather
than from whichever parts happened to be up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from slife2.libs.harness.markers import strip as strip_marker
from slife2.libs.host.paths import data_dir
from slife2.libs.plugin.audience import conversation_meta
from slife2.libs.turn.context import TURN_PROMPT_PREFIX
from slife2.libs.turn.tokens import estimate_text_tokens
from slife2.plugins.cabinet.store import DUMP, UPSERT, index_path, store_of
from slife2.tui.client import MCPAgentClient
from tests.e2e.edges import ANSWER, WINDOW, ChatAnswer, ToolAsk
from tests.fakes import StubEmbedder

pytestmark = pytest.mark.e2e

#: What the tool-call test's model asks a *tool* to write, and the string its
#: assertions read back off disk.  Its own constant because it is a different
#: conversation's fact: the note is written by the cabinet process, into the
#: agent's cabinet, and reading it back is how this side of the stack proves the
#: plugin ran.
NOTE_BODY = "the note the model asked a tool in another process to write"


def _files(data: Path) -> list[str]:
    return sorted(
        str(path.relative_to(data)) for path in data.rglob("*") if path.is_file()
    )


def _prompt_words(content: str) -> str:
    """A live user message as the person wrote it, the turn's prompt taken off.

    `[TURN-PROMPT:{…}]` — the clock and the status bar's figure — is added to
    the message after the recall has chosen the conversation and removed before
    the turn is saved.  The model is its only reader, so a request read back
    from the stub carries it while the row, the transcript and every later
    rebuild do not.  What the two agree on is the words underneath.
    """
    return strip_marker(content, TURN_PROMPT_PREFIX)


@pytest.mark.asyncio
async def test_a_turn_runs_across_the_whole_stack_and_is_remembered(stack) -> None:
    """One message in, one answer out, and a conversation left behind.

    The assertions are the things only a real stack can show: the answer came
    back over the progress channel (not just as a result), the request that
    produced it carried the prompt, the conversation is readable through a
    *second* connection, and it is on disk, written by the context process.

    **Two provider calls and not one**, which is worth knowing before reading
    the count: the context store's discriminator is a model call of its own
    (`slife2-context` decides what a turn runs on before it runs), and it comes
    through the same provider as the turn.
    """
    config = stack["config"]
    events: list[str] = []
    client = MCPAgentClient(config.agent.server.url, agent="e2e")
    await client.connect()
    try:
        answer = await client.run_turn(
            "what is 6*7?", lambda event: events.append(type(event).__name__)
        )
    finally:
        await client.close()

    assert answer.text == ANSWER
    assert answer.injected is False
    assert events[0] == "ContextChosen", events
    assert "TextDelta" in events and events[-1] == "TurnFinished"
    assert stack["provider"].chats == 2, stack["provider"].calls
    # The model is the marker's only reader: the request carries it, across
    # every process between the agent and the stub.
    sent = stack["provider"].last_messages[-1]["content"]
    assert sent.startswith(TURN_PROMPT_PREFIX), sent
    assert _prompt_words(sent) == "what is 6*7?"

    # The conversation outlives the client that ran it: the transcript is the
    # server's, and a *second* connection reads the turn back.
    again = MCPAgentClient(config.agent.server.url, agent="e2e")
    await again.connect()
    try:
        turns = await again.transcript()
    finally:
        await again.close()
    assert len(turns) == 1, turns
    # A turn is stored as the message list the model saw, so both halves are in
    # there and neither is a column of its own (`TurnRecord.to_wire`).
    assert "what is 6*7?" in json.dumps(turns[0]["messages"])
    assert ANSWER in json.dumps(turns[0]["messages"])
    # ...and the prompt marker stayed out of the row: it is text for the turn
    # that ran, not a fact about it, so search and recall never see it.
    assert TURN_PROMPT_PREFIX not in json.dumps(turns[0]["messages"])

    # And it is on disk, written by the context plugin rather than by anybody in
    # this process.
    store = data_dir() / "agents" / "e2e" / "turns.db"
    assert store.is_file(), _files(data_dir())


@pytest.mark.asyncio
async def test_a_second_turn_runs_on_the_first(stack) -> None:
    """The history reaches the model, over a hop that has to carry it.

    A conversation that is only *stored* is a transcript; one that is *run* is
    the feature.  The stub answers whatever it is asked, so what is asserted is
    the request: the second call carries both messages, which is what the
    context process decided to rebuild from.
    """
    config = stack["config"]
    client = MCPAgentClient(config.agent.server.url, agent="e2e-history")
    await client.connect()
    try:
        await client.run_turn("remember the number 41", lambda _event: None)
        await client.run_turn("what number?", lambda _event: None)
    finally:
        await client.close()

    assert stack["provider"].last_messages[-1]["role"] == "user"
    assert (
        _prompt_words(stack["provider"].last_messages[-1]["content"]) == "what number?"
    )
    assert any(
        "remember the number 41" in json.dumps(message)
        for message in stack["provider"].last_messages[:-1]
    ), stack["provider"].last_messages


@pytest.mark.asyncio
async def test_a_turn_calls_a_tool_in_another_process_and_reads_its_answer(
    stack,
) -> None:
    """**The round trip the whole arrangement exists for**, over every boundary.

    The model asks for a tool, the loop dispatches it, the hub routes it by
    name, a *different process* runs it, and what that process answered comes
    back to the model as a message — which is the one path nothing else in this
    suite can show.  Every other tool test stops at one edge of it: the loop
    tests stop at the dispatch, the hub tests at a stubbed upstream, and the
    plugin tests at the plugin's own server.  Only a real stack has a model on
    one side and a plugin process on the other with the hub and the catalogue in
    between, and the pieces in between are exactly what a rearrangement breaks.

    What keeps it from being a demonstration is that both ends are read where
    this process cannot have written them: the note is a file the cabinet's own
    process put on disk, and the third request the provider received carries the
    tool's sentence rather than anything this test composed.

    **Three calls, and the first is not the turn's.**  The context store's
    discriminator decides what a turn runs on before it runs, and it is a model
    call of its own; the second is the turn's first iteration, which asks for the
    tool; the third is the iteration after the tool has answered.
    """
    provider = stack["provider"]
    provider.script = [
        # The discriminator.  `{}` is "nothing changes" — what the context
        # plugin's own tests answer with, and the one answer that leaves the
        # conversation this test is about alone.
        ChatAnswer(text="{}"),
        ChatAnswer(
            tool=ToolAsk(
                id="call_e2e_note",
                name="note_write",
                arguments=json.dumps(
                    {"subject": "e2e verification", "content": NOTE_BODY}
                ),
            )
        ),
        ChatAnswer(text="Noted."),
    ]
    config = stack["config"]
    client = MCPAgentClient(config.agent.server.url, agent="e2e-tool")
    await client.connect()
    try:
        answer = await client.run_turn("make a note of that", lambda _event: None)
    finally:
        await client.close()

    assert answer.text == "Noted.", provider.calls
    # Step by step, so a turn that took a different number of calls says which
    # one it took rather than failing on a total.
    assert provider.chats == 3, [len(call) for call in provider.calls]

    # The model was *offered* the tool it called, by the bare name our own
    # plugins use — so the catalogue reached the model and the name the model
    # wrote is the name the hub routes by.  Read from the turn's own call and
    # not the discriminator's, which is a completion and carries no tools.
    assert "note_write" in provider.tool_names[1], provider.tool_names[1]

    # And the tool's answer came back into the conversation: the third request
    # carries a `tool` message the loop built out of what the cabinet process
    # said.  The loop pairs it with the call by id, which is what the model
    # needs to see which answer belongs to which request.
    result = [message for message in provider.calls[2] if message.get("role") == "tool"]
    assert len(result) == 1, provider.calls[2]
    assert result[0]["tool_call_id"] == "call_e2e_note", result[0]
    # The cabinet's own JSON, rendered by `as_tool_text` on its way into the
    # request: the row it wrote, not a sentence about it.
    assert json.loads(result[0]["content"])["key"] == "e2e verification", result[0]

    # **Written by the cabinet's process**, which is the half this one cannot
    # fake: nothing here holds that store.  The file is the mirror of the note
    # the model asked for, and its contents are the model's own words.
    notes = data_dir() / "agents" / "e2e-tool" / "cabinet" / "notes"
    written = sorted(notes.glob("*.md")) if notes.is_dir() else []
    assert len(written) == 1, _files(data_dir())
    assert NOTE_BODY in written[0].read_text(encoding="utf-8")


async def _save_dump(agent: str, call: str, body: str) -> None:
    """A dump, put in the cabinet the way the harness will put one there.

    The write is the half of the arrangement that is not built yet — the harness
    owns it, and this test reaching the plugin's own store is the closest thing
    to it that exists.  Everything after this line is the real thing: a second
    process reads the row, over a socket.
    """
    store = store_of(index_path(agent))
    embedder = StubEmbedder()
    await store.sync(embedder)
    await store.write(DUMP, call, mode=UPSERT, body=body, embedder=embedder)


async def _read(
    client: Client, agent: str, tool: str = "dump_read", **arguments: Any
) -> dict[str, Any]:
    """One dump reader, on behalf of a conversation, as the model reads it.

    The answer is an object (`cabinet.server`), and `data` is how a tool's JSON
    reaches this side of the hop.
    """
    result = await client.call_tool(tool, arguments, meta=conversation_meta(agent))
    answer = getattr(result, "data", None)
    assert isinstance(answer, dict), answer
    return answer


@pytest.mark.asyncio
async def test_a_dump_is_read_back_over_the_real_stack_within_its_window(stack) -> None:
    """**The rare case, forced** — and the loop it must not close.

    A turn in real use seldom returns an output past the window, so nothing here
    waits for one to happen: the dump is put where the harness will put it, and
    everything after that is real. A second process owns the cabinet, the call
    crosses a socket, and the window is the number in the config file every
    child read.

    What is under test is a loop that must not exist. The harness truncates an
    output too large for a conversation; a reader that could hand back more than
    the window would have its *own* answer truncated next, which is this
    system's machinery feeding itself. So every answer is measured here, from
    the model's side of the hop, with the same estimator the harness spends —
    including the answers built out of what the caller asked for, which is the
    path where a window is easiest to lose.
    """
    lines = 4000
    body = chr(10).join(
        f"line {number} of a tool output" for number in range(1, lines + 1)
    )
    await _save_dump("e2e", "call_e2e", body)
    config = stack["config"]
    budget = config.tool_result.tokens
    assert budget == WINDOW, "the window came from the config file"

    async with Client(config.server("cabinet").url) as client:
        first = await _read(client, "e2e", call="call_e2e")
        offset = first.get("next")
        assert isinstance(offset, int), first
        second = await _read(client, "e2e", call="call_e2e", offset=offset)
        found = await _read(
            client, "e2e", tool="dump_grep", call="call_e2e", pattern="line 39"
        )
        huge = await _read(
            client, "e2e", tool="dump_grep", call="call_e2e", pattern="z" * 100_000
        )
        chars = await _read(
            client, "e2e", call="call_e2e", mode="char", offset=0, limit=40
        )

    for answer in (first, second, found, huge, chars):
        assert estimate_text_tokens(json.dumps(answer)) <= budget, str(answer)[:300]

    # A window, and the address of the next one: the numbers in it are lines of
    # the output rather than of the page it was shown.
    assert "line 1 of a tool output" in first["text"]
    assert first["lines"] >= lines, "the whole output is counted, not the window"
    assert f"line {offset} of a tool output" in second["text"]
    assert chars["text"] == "line 1 of a tool output\nline 2 of a tool", (
        "character mode is the same file from character zero"
    )
    # Finding counts the whole output rather than the window it fitted, and
    # every match carries both addresses.
    assert found["matched"] and found["matches"][0]["text"].endswith(
        "line 39 of a tool output"
    )
    assert isinstance(found["matches"][0]["char"], int)
    # **The path where a window is easiest to lose**: an argument that is not a
    # pattern at all is answered with rather than echoed.  Nothing the caller
    # wrote is quoted back any more, so a 100,000-character pattern is simply a
    # pattern this output does not contain.
    assert huge["matched"] == 0

    # And the whole of it is a file, which is what the marker will promise.
    dumps = data_dir() / "agents" / "e2e" / "cabinet" / "dumps"
    assert dumps.is_dir() and list(dumps.iterdir()), _files(data_dir())
    # The file is the cabinet's; the answer is the window's.  The model reads it
    # through `dump_read`/`dump_grep` by the call id, and the path in the marker
    # — relative to the data directory — is what the agent's own tools open.
    assert "where" not in first, first
