"""`model_*`: the model config tools, on the server that resolves the model.

The four model config tools live in the process that turns `default:` and
`providers:` into a conversation's model — the edit and the read that honours it
are one process, so a switch needs no reload protocol between them.

What these tests are about is what makes that safe: what is written is the file
the operator reads, what is refused is refused before anything is written, and a
model added to a provider does not lose the ones beside it.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.libs.host.config import DEFAULT_AGENT, load, load_cached
from slife2.libs.host.configfile import read_scalar, read_section
from slife2.libs.host.launcher import Outcome, Status
from slife2.libs.model.base import Chunk, StreamChatResult
from slife2.libs.plugin.audience import client_meta
from slife2.libs.turn.messages import ToolCall
from slife2.plugins.agent.server import build_server
from slife2.plugins.context.server import build_server as build_context
from slife2.plugins.toolhub.server import build_server as build_hub
from tests.fakes import (
    FakeBackend,
    ScriptedTurn,
    StubEmbedder,
    config_file,
    keep_the_context,
    plugin_transports,
    text_turn,
)

pytestmark = pytest.mark.unit

#: A config with two models on one provider, so "add one" has something to
#: preserve and "remove one" has something to leave behind.
CONFIG = """\
providers:
  local:
    api: openai-completions
    base_url: https://local.test/v1
    api_key: ${LOCAL_KEY}
    models:
      - model: big
        name: Big
        context_window: 100000
      - model: small
        name: Small
embeddings:
  providers:
    local:
      base_url: http://127.0.0.1:17347/v1
      model: big-emb
default: local/big
"""


def write_config(data_dir: Path, text: str = CONFIG) -> Path:
    """This module's config, written where the loader looks for it."""
    return config_file(data_dir, text)


def server_for(isolated_runtime: Path, text: str = CONFIG):
    """The agent server over the config on disk, reading it as `main` does."""
    write_config(isolated_runtime, text)
    return build_server(load(), source=load_cached)


async def ask(client: Client, tool: str, **arguments: Any) -> Any:
    """One model tool's answer, which is the JSON every model-facing tool gives.

    `result.data` rather than the content blocks: the shape is what these tools
    are, and a test that read the rendered text would pass on a sentence.
    """
    result = await client.call_tool(tool, arguments)
    return result.data


def dumped(answer: Any) -> str:
    """An answer as the model reads it, for the "and never this" assertions."""
    return json.dumps(answer, default=str)


def written(isolated_runtime: Path) -> str:
    return (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")


def section(isolated_runtime: Path) -> dict[str, Any]:
    return read_section("providers", path=isolated_runtime / "slife2.yaml")


# --- what the listing shows ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_listing_shows_the_file_and_never_the_key(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Not even the reference**, let alone what it resolves to.

    What this process *holds* is the resolved settings, so a listing built from
    those would print the operator's live key into the conversation and the
    transcript.  It reads the file instead — and prints neither the key nor the
    `${VAR}` that stands in for it, because a listing is about models.
    """
    monkeypatch.setenv("LOCAL_KEY", "sk-live-do-not-print-me")

    async with Client(server_for(isolated_runtime)) as client:
        listed = await ask(client, "model_list")

    assert listed["total"] == 2
    assert listed["default"] == "local/big"
    rows = {(row["provider"], row["model"]): row for row in listed["models"]}
    assert rows[("local", "big")]["name"] == "Big"
    assert rows[("local", "big")]["marks"] == ["default"]
    assert rows[("local", "big")]["context_window"] == 100000
    assert rows[("local", "small")]["name"] == "Small"
    assert rows[("local", "small")]["marks"] == []
    assert "sk-live-do-not-print-me" not in dumped(listed)
    assert "${LOCAL_KEY}" not in dumped(listed)


# --- adding one ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_adding_a_model_keeps_the_ones_beside_it(isolated_runtime) -> None:
    """**The read-modify-write, which is why `update_entry` exists.**

    A provider entry holds a *list* of models, and `upsert` writes exactly what
    it is handed — so writing "the new model" through it would delete its
    siblings.  What must survive is both the other models and the provider's own
    fields.
    """
    async with Client(server_for(isolated_runtime)) as client:
        answer = await ask(
            client,
            "model_set",
            ref="local/third",
            name="Third",
            context_window=8000,
        )
        listed = await ask(client, "model_list")

    entry = section(isolated_runtime)["local"]
    assert [one["model"] for one in entry["models"]] == ["third", "big", "small"]
    assert entry["api"] == "openai-completions", "the provider's own fields stay"
    assert entry["base_url"] == "https://local.test/v1"
    assert entry["api_key"] == "${LOCAL_KEY}", "and the reference, not the key"
    assert answer == {
        "ref": "local/third",
        "created": True,
        "provider_created": False,
    }, "added, on a provider that was already there and needs no server started"
    assert any(
        row["model"] == "third" and row["name"] == "Third" for row in listed["models"]
    )


@pytest.mark.asyncio
async def test_setting_an_existing_model_replaces_its_own_entry(
    isolated_runtime,
) -> None:
    """A `set` is that model's whole entry — the rule the four sections'
    `*_set` tools already have, over a list instead of a mapping."""
    async with Client(server_for(isolated_runtime)) as client:
        answer = await ask(client, "model_set", ref="local/small", name="Tiny")

    assert answer["created"] is False, "one the file already had"
    assert answer["provider_created"] is False
    entry = section(isolated_runtime)["local"]
    assert [one["model"] for one in entry["models"]] == ["small", "big"]
    assert "context_window" not in entry["models"][0], (
        "the field the caller did not name is gone"
    )


@pytest.mark.asyncio
async def test_a_new_provider_needs_an_endpoint_and_a_key(isolated_runtime) -> None:
    write_config(isolated_runtime)
    before = written(isolated_runtime)

    async with Client(server_for(isolated_runtime)) as client:
        with pytest.raises(ToolError) as refused:
            await ask(client, "model_set", ref="fresh/m", name="M")

    assert "new provider" in str(refused.value)
    assert "base_url" in str(refused.value), "and what it needs instead"
    assert written(isolated_runtime) == before


@pytest.mark.asyncio
async def test_an_unknown_wire_protocol_is_refused(isolated_runtime) -> None:
    write_config(isolated_runtime)
    before = written(isolated_runtime)

    async with Client(server_for(isolated_runtime)) as client:
        with pytest.raises(ToolError) as refused:
            await ask(
                client,
                "model_set",
                ref="local/m",
                name="M",
                api="carrier-pigeon",
            )

    assert "wire protocol" in str(refused.value)
    assert written(isolated_runtime) == before


@pytest.mark.asyncio
async def test_a_provider_on_a_new_protocol_starts_its_backend(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case a re-read cannot cover: nothing is serving that protocol.

    Stubbed rather than taken, because the real call starts a process — what is
    under test is that it is asked for, on the loop, and that its answer is
    reported rather than raised.
    """
    started: list[Any] = []

    def fake_ensure(spec: Any, *, config_path: Any = None) -> Outcome:
        started.append(spec)
        return Outcome(spec, Status.STARTED)

    monkeypatch.setattr("slife2.libs.host.launcher.ensure", fake_ensure)

    async with Client(server_for(isolated_runtime)) as client:
        answer = await ask(
            client,
            "model_set",
            ref="claude/sonnet",
            name="Sonnet",
            api="anthropic-messages",
            base_url="https://anthropic.test",
            api_key="ANTHROPIC_KEY",
        )

    assert answer["provider_created"] is True and answer["created"] is True
    assert answer["ready"] is True and "error" not in answer
    assert [spec.name for spec in started] == ["llm:anthropic-messages"]
    assert section(isolated_runtime)["claude"]["api"] == "anthropic-messages"


@pytest.mark.asyncio
async def test_a_model_server_that_does_not_start_is_reported_not_raised(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit stands either way; what the model reads is why the model is not usable yet.

    A start that fails must not read as a write that failed — the config has the
    provider and the model, and a restart retries the server.  So the answer
    carries `ready` and the reason beside it, and the caller can tell "written,
    not reachable yet" from "not written".
    """

    def fake_ensure(spec: Any, *, config_path: Any = None) -> Outcome:
        return Outcome(spec, Status.FAILED, detail="port 41873 is taken")

    monkeypatch.setattr("slife2.libs.host.launcher.ensure", fake_ensure)

    async with Client(server_for(isolated_runtime)) as client:
        answer = await ask(
            client,
            "model_set",
            ref="claude/sonnet",
            name="Sonnet",
            api="anthropic-messages",
            base_url="https://anthropic.test",
            api_key="ANTHROPIC_KEY",
        )

    assert answer["provider_created"] is True and answer["created"] is True
    assert answer["ready"] is False
    assert "port 41873 is taken" in answer["error"]
    assert section(isolated_runtime)["claude"]["api"] == "anthropic-messages"


# --- removing and switching ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_default_model_cannot_be_removed(isolated_runtime) -> None:
    """The rule, and the reason for it: the next conversation needs a model."""
    write_config(isolated_runtime)
    before = written(isolated_runtime)

    async with Client(server_for(isolated_runtime)) as client:
        with pytest.raises(ToolError) as refused:
            await ask(client, "model_remove", ref="local/big")

    assert "default model" in str(refused.value)
    assert written(isolated_runtime) == before


@pytest.mark.asyncio
async def test_removing_a_model_leaves_the_ones_beside_it(isolated_runtime) -> None:
    async with Client(server_for(isolated_runtime)) as client:
        await ask(client, "model_switch", ref="local/small")
        answer = await ask(client, "model_remove", ref="local/big")

    assert answer == {"ref": "local/big", "provider_removed": False}
    assert [one["model"] for one in section(isolated_runtime)["local"]["models"]] == [
        "small"
    ]


@pytest.mark.asyncio
async def test_removing_a_providers_last_model_takes_its_entry_too(
    isolated_runtime,
) -> None:
    """A provider with no models is one the loader refuses, so it cannot stay.

    The provider's entry goes with its last model rather than being left empty —
    which is `update_entry` answering `None`, the same shape `remove` uses.
    """
    two = """\
providers:
  local:
    api: openai-completions
    base_url: https://local.test/v1
    api_key: ${LOCAL_KEY}
    models:
      - model: big
        name: Big
      - model: small
        name: Small
  aside:
    api: openai-completions
    base_url: https://aside.test/v1
    api_key: ${ASIDE_KEY}
    models:
      - model: only
embeddings:
  providers:
    local:
      base_url: http://127.0.0.1:17347/v1
      model: big-emb
default: local/big
"""
    write_config(isolated_runtime, two)

    async with Client(server_for(isolated_runtime, two)) as client:
        answer = await ask(client, "model_remove", ref="aside/only")

    assert answer == {"ref": "aside/only", "provider_removed": True}
    assert "aside" not in section(isolated_runtime)
    assert "local" in section(isolated_runtime), "its neighbour is untouched"


@pytest.mark.asyncio
async def test_the_last_model_in_the_config_cannot_be_removed(isolated_runtime) -> None:
    """Emptying `providers:` would leave a file the loader refuses.

    A home runs on the providers its file names and there is no built-in one to
    fall back to (SDD §4.1), so a `model_remove` that took the last model would
    write `providers: {}` — a config the next start refuses — over an edit that
    reported success.
    """
    one = """\
providers:
  local:
    api: openai-completions
    base_url: https://local.test/v1
    api_key: ${LOCAL_KEY}
    models:
      - model: only
embeddings:
  providers:
    local:
      base_url: http://127.0.0.1:17347/v1
      model: big-emb
"""
    write_config(isolated_runtime, one)
    before = written(isolated_runtime)

    async with Client(server_for(isolated_runtime, one)) as client:
        with pytest.raises(ToolError) as refused:
            await ask(client, "model_remove", ref="local/only")

    assert "last model" in str(refused.value)
    assert written(isolated_runtime) == before


@asynccontextmanager
async def answering_server(isolated_runtime: Path, backend: FakeBackend | None = None):
    """`server_for`'s server, plus the two peers a turn needs to run.

    The real context and hub servers over the in-memory transport, exactly as
    `test_server.py` builds them for its own turns, and a model that is a script
    rather than an endpoint.  A switch is about conversations that are *running*,
    so the test needs a conversation that can: asserting on the file after a
    switch is not the same claim, and it is the one the whole tool exists for.

    **The agent itself is wired into the hub**, which is `test_server.py`'s
    `wired` and its reason: a message that arrives mid-turn is folded in by a
    call to `_check_new_input`, that call is made through the hub, and a hub
    holding `blank_plugin` for the agent would hand the loop a registry with no
    such tool — so the fold would silently not happen.

    An ordinary context manager rather than a fixture, like `server_for` beside
    it: it is entered inside a test rather than around one, and what it stands up
    is what the `async with` tears down.
    """
    write_config(isolated_runtime)
    live = load()
    holder: dict[str, Any] = {}
    hub_client = Client(
        build_hub(
            live,
            transports=plugin_transports(
                live, {"agent": lambda _settings: holder["agent"]}
            ),
            embedder=StubEmbedder(),
        )
    )
    async with Client(
        build_context(live, embedder=StubEmbedder(), ask=keep_the_context)
    ) as context:
        server = build_server(
            live,
            context_client=context,
            hub_client=hub_client,
            backend=backend or FakeBackend(text_turn("one"), text_turn("two")),
            source=load_cached,
        )
        # Before the hub is entered, because entering it is what opens the
        # client for the agent transport above.
        holder["agent"] = server
        async with hub_client:
            yield server


async def send(
    server: Any, prompt: str, *, home: Path | None = None, **arguments
) -> Any:
    """One turn, on a client of its own — the same shape `test_server.py` uses.

    A fresh client each time, deliberately: the conversation lives in the
    *server*, and a second caller while a turn is running is a second connection
    rather than a second call on the first one's session.
    """
    async with Client(server) as client:
        return await client.call_tool(
            "send_message",
            {"agent": DEFAULT_AGENT, "prompt": prompt, **arguments},
            meta=client_meta(DEFAULT_AGENT, home=str(home)) if home else None,
        )


def calling(name: str, arguments: dict, *, delay: float = 0.0) -> ScriptedTurn:
    """A scripted turn where the model calls one tool — here, a iteration boundary."""
    return ScriptedTurn(
        result=StreamChatResult(
            text="checking",
            tool_calls=(ToolCall(id="c1", name=name, arguments=arguments),),
            stop_reason="tool_calls",
        ),
        chunks=[Chunk(text="checking")],
        delay=delay,
    )


async def wait_for_streams(backend: FakeBackend, wanted: int = 1) -> None:
    """Block until the backend has been asked for that many model streams."""
    for _ in range(1000):
        if len(backend.calls) >= wanted:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"the model was asked for {len(backend.calls)} stream(s), not {wanted}"
    )


@pytest.mark.asyncio
async def test_switching_moves_the_conversations_already_running(
    isolated_runtime,
) -> None:
    """**The half that makes `model_switch` the way out of a model that is down.**

    Writing `default:` alone changes what the *next* conversation starts on — so
    a window switching away from a model that has stopped answering would go on
    being answered, or not answered, by the one it was trying to leave.  The
    conversation keeps its key and its history; what moves is the model it
    reaches, from its next turn.
    """
    async with answering_server(isolated_runtime) as server, Client(server) as client:
        first = await client.call_tool(
            "send_message", {"agent": DEFAULT_AGENT, "prompt": "one"}
        )
        switched = await ask(client, "model_switch", ref="local/small")
        second = await client.call_tool(
            "send_message", {"agent": DEFAULT_AGENT, "prompt": "two"}
        )

    assert first.data["model"] == "local/big", "it starts on the default"
    assert switched["moved"] == 1, "and the answer says what it moved"
    assert switched["previous"] == "local/big"
    assert second.data["model"] == "local/small", "the same conversation moved"


@pytest.mark.asyncio
async def test_switching_writes_the_scalar_and_refuses_a_stranger(
    isolated_runtime,
) -> None:
    """The default is one top-level scalar, and the writer's judge is `resolve`."""
    async with Client(server_for(isolated_runtime)) as client:
        answer = await ask(client, "model_switch", ref="local/small")
        assert read_scalar("default", path=isolated_runtime / "slife2.yaml") == (
            "local/small"
        )
        assert answer["previous"] == "local/big"
        assert answer["moved"] == 0, "nothing was running on this server"

        before = written(isolated_runtime)
        with pytest.raises(ToolError) as refused:
            await ask(client, "model_switch", ref="nobody/knows")

    assert "not a model" in str(refused.value)
    assert written(isolated_runtime) == before, "rolled back"
    assert "providers" in written(isolated_runtime), "the file survived the refusal"


@pytest.mark.asyncio
async def test_the_four_tools_are_marked_for_the_model(isolated_runtime) -> None:
    """**The mark is what makes them reachable**, and it is the only thing here
    that a unit test can check about the hub.

    A plugin's own tools arrive by `tools/list` and are offered to the model only
    if they declare themselves the model's (`slife2.libs.plugin.audience`) — `_check_new_input`
    is the precedent, and the agent server is one of `Config.plugins()`, so these
    four arrive by exactly the same road.
    """
    from slife2.libs.plugin.audience import for_the_model

    async with Client(server_for(isolated_runtime)) as client:
        tools = {one.name: one for one in await client.list_tools()}

    for name in ("model_list", "model_set", "model_remove", "model_switch"):
        assert for_the_model(tools[name].meta), name
    assert not for_the_model(tools["send_message"].meta), (
        "the client's tools stay its own"
    )


# --- a switch during a turn ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_names_the_model_that_answered_it_not_the_one_it_moved_to(
    isolated_runtime,
) -> None:
    """**A switch lands under a turn that is already running, and the answer is
    still the old model's.**

    `model_switch` is used *from inside a turn* — it is the way out of a model
    that has stopped answering — and it takes no lock on the conversation, so the
    loop's reference moves while that turn is still in flight.  The reply must
    name what answered (`Outcome.model`, taken where `loop_for` resolves the
    backend), or it disagrees with the turn's own record and with itself: the
    caller is told the model that never saw its message.

    Both callers are asserted, because there are two and they are answered at
    different moments.  The turn's own caller reads its reply after the switch;
    a caller whose message was **folded in** is parked on the lock and answered
    even later, so reading the loop's reference there is the same mistake one
    step further out.
    """
    backend = FakeBackend(
        calling("skill_list", {}, delay=0.5),
        ScriptedTurn(
            result=StreamChatResult(text="the answer", stop_reason="stop"),
            chunks=[Chunk(text="the answer")],
            delay=0.5,
        ),
        # Scripted so that a second turn *can* run: a fold that did not happen
        # fails on the `injected` assertion below rather than on the backend
        # running out of turns.
        text_turn("two"),
    )

    async with answering_server(isolated_runtime, backend=backend) as server:
        first = asyncio.create_task(send(server, "one"))
        await wait_for_streams(backend)
        second = asyncio.create_task(send(server, "two"))
        # The message must be waiting before the iteration boundary, which is where
        # the running turn folds it in (a `delay` of half a second above).
        await asyncio.sleep(0.05)
        # ...and the switch must land while the absorbing turn is still in flight.
        await wait_for_streams(backend, 2)
        async with Client(server) as client:
            switched = await ask(client, "model_switch", ref="local/small")
        one, two = await asyncio.gather(first, second)

    assert switched["moved"] == 1, "the switch did land, mid-turn"
    assert one.data["model"] == "local/big", "the model that answered the turn"
    assert two.data["injected"] is True, "and the message really was folded in"
    assert two.data["text"] == one.data["text"]
    assert two.data["model"] == "local/big", "the same model, said to both callers"


# --- one process, several homes -----------------------------------------------


#: **The second home's config, and it deliberately lacks `local/small`.**  The
#: way the switch bug shows is that a loop belonging to another home is moved onto
#: a reference *that home's* file cannot name — so this home must not have it.
OTHER_HOME = """\
providers:
  local:
    api: openai-completions
    base_url: https://local.test/v1
    api_key: ${LOCAL_KEY}
    models:
      - model: big
        name: Big
embeddings:
  providers:
    local:
      base_url: http://127.0.0.1:17347/v1
      model: big-emb
default: local/big
"""


@pytest.mark.asyncio
async def test_a_switch_moves_only_the_conversations_of_the_callers_home(
    isolated_runtime,
) -> None:
    """**The loop table is keyed by home, and so is an edit to it.**

    One process serves every home on the machine, which is why a loop is filed
    under `(home, agent, subagent)` at all.  `current()` resolved the reference
    just written into the *caller's* config file — so a switch that moved every
    loop in the process would point another home's conversation at a model that
    home cannot name, and its store would record a foreign model from the next
    turn on.
    """
    mine, theirs = isolated_runtime, isolated_runtime.parent / "theirs"
    write_config(mine, CONFIG)
    write_config(theirs, OTHER_HOME)

    async with answering_server(mine) as server:
        # A conversation in the *other* home, started on the model this home is
        # about to switch away from.
        first = await send(server, "one", home=theirs)
        async with Client(server) as client:
            switched = await ask(client, "model_switch", ref="local/small")
        second = await send(server, "two", home=theirs)

    assert switched["moved"] == 0, "nothing of this home's was running"
    assert first.data["model"] == "local/big"
    assert second.data["model"] == "local/big", (
        "another home's switch did not reach that home's conversation"
    )


@pytest.mark.asyncio
async def test_one_home_cannot_block_a_removal_with_another_homes_conversation(
    isolated_runtime,
) -> None:
    """A false refusal, and it printed the other home's conversation names.

    `model_remove` refuses for a model a running conversation is on, and that
    walk has to be the same home-scoped one the registry is keyed by: the model
    here is one *this* home has a conversation on nowhere, so a refusal would be
    a refusal about somebody else's conversation — in a sentence a model reads.

    Both homes hold the same config here, deliberately: what is being shown is
    that the other home's conversation cannot block an edit this home is entitled
    to make, not that the two files differ.
    """
    mine, theirs = isolated_runtime, isolated_runtime.parent / "theirs"
    write_config(mine, CONFIG)
    write_config(theirs, CONFIG)

    async with answering_server(mine) as server:
        # A conversation in the other home, on the model this home will remove.
        await send(server, "one", home=theirs, model="local/small")
        async with Client(server) as client:
            answer = await ask(client, "model_remove", ref="local/small")

    assert answer["ref"] == "local/small", "removed, and not refused"
    assert [one["model"] for one in section(mine)["local"]["models"]] == ["big"]
    assert [one["model"] for one in section(theirs)["local"]["models"]] == [
        "big",
        "small",
    ], "the other home's file is not this call's to edit"
