"""`model_*`: the model config tools, on the server that resolves the model.

v1's four, ported to the process that turns `default:` and `providers:` into a
conversation's model — the edit and the read that honours it are one process, so
a switch needs no reload protocol between them.

What these tests are about is what makes that safe: what is written is the file
the operator reads, what is refused is refused before anything is written, and a
model added to a provider does not lose the ones beside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from slife2.config import load, load_cached
from slife2.configfile import read_scalar, read_section
from slife2.launcher import Outcome, Status
from slife2.server.server import build_server

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
default: local/big
"""


def write_config(isolated_runtime: Path, text: str = CONFIG) -> Path:
    isolated_runtime.mkdir(parents=True, exist_ok=True)
    path = isolated_runtime / "slife2.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def server_for(isolated_runtime: Path, text: str = CONFIG):
    """The agent server over the config on disk, reading it as `main` does."""
    write_config(isolated_runtime, text)
    return build_server(load(), source=load_cached)


async def ask(client: Client, tool: str, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    return "".join(getattr(block, "text", "") for block in result.content or [])


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

    assert "local/big — Big" in listed and "[default]" in listed
    assert "local/small — Small" in listed
    assert "openai-completions" in listed and "https://local.test/v1" in listed
    assert "sk-live-do-not-print-me" not in listed
    assert "${LOCAL_KEY}" not in listed


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
            provider="local",
            model="third",
            name="Third",
            context_window=8000,
        )
        listed = await ask(client, "model_list")

    entry = section(isolated_runtime)["local"]
    assert [one["model"] for one in entry["models"]] == ["third", "big", "small"]
    assert entry["api"] == "openai-completions", "the provider's own fields stay"
    assert entry["base_url"] == "https://local.test/v1"
    assert entry["api_key"] == "${LOCAL_KEY}", "and the reference, not the key"
    assert "added to `local`" in answer
    assert "local/third — Third" in listed


@pytest.mark.asyncio
async def test_setting_an_existing_model_replaces_its_own_entry(
    isolated_runtime,
) -> None:
    """A `set` is that model's whole entry — the rule the four sections'
    `*_set` tools already have, over a list instead of a mapping."""
    async with Client(server_for(isolated_runtime)) as client:
        answer = await ask(
            client, "model_set", provider="local", model="small", name="Tiny"
        )

    assert "updated on `local`" in answer
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
        answer = await ask(client, "model_set", provider="fresh", model="m", name="M")

    assert answer.startswith("[refused]") and "new provider" in answer
    assert written(isolated_runtime) == before


@pytest.mark.asyncio
async def test_an_unknown_wire_protocol_is_refused(isolated_runtime) -> None:
    write_config(isolated_runtime)
    before = written(isolated_runtime)

    async with Client(server_for(isolated_runtime)) as client:
        answer = await ask(
            client,
            "model_set",
            provider="local",
            model="m",
            name="M",
            api="carrier-pigeon",
        )

    assert answer.startswith("[refused]") and "wire protocol" in answer
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

    monkeypatch.setattr("slife2.launcher.ensure", fake_ensure)

    async with Client(server_for(isolated_runtime)) as client:
        answer = await ask(
            client,
            "model_set",
            provider="claude",
            model="sonnet",
            name="Sonnet",
            api="anthropic-messages",
            base_url="https://anthropic.test",
            api_key="ANTHROPIC_KEY",
        )

    assert "new provider `claude`" in answer and "backend is up" in answer
    assert [spec.name for spec in started] == ["llm:anthropic-messages"]
    assert section(isolated_runtime)["claude"]["api"] == "anthropic-messages"


# --- removing and switching ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_default_model_cannot_be_removed(isolated_runtime) -> None:
    """v1's rule, and the reason for it: the next conversation needs a model."""
    write_config(isolated_runtime)
    before = written(isolated_runtime)

    async with Client(server_for(isolated_runtime)) as client:
        answer = await ask(client, "model_remove", ref="local/big")

    assert answer.startswith("[refused]") and "default model" in answer
    assert written(isolated_runtime) == before


@pytest.mark.asyncio
async def test_removing_a_model_leaves_the_ones_beside_it(isolated_runtime) -> None:
    async with Client(server_for(isolated_runtime)) as client:
        await ask(client, "model_switch", ref="local/small")
        answer = await ask(client, "model_remove", ref="local/big")

    assert "removed" in answer and "no other models" not in answer
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
default: local/big
"""
    write_config(isolated_runtime, two)

    async with Client(server_for(isolated_runtime, two)) as client:
        answer = await ask(client, "model_remove", ref="aside/only")

    assert "removed" in answer and "no other models" in answer
    assert "aside" not in section(isolated_runtime)
    assert "local" in section(isolated_runtime), "its neighbour is untouched"


@pytest.mark.asyncio
async def test_the_last_model_in_the_config_cannot_be_removed(isolated_runtime) -> None:
    """Emptying `providers:` would quietly bring the built-in ones back.

    `slife2.config` substitutes its own providers for an empty section, so the
    answer would say "removed" while a phantom provider went live.
    """
    one = """\
providers:
  local:
    api: openai-completions
    base_url: https://local.test/v1
    api_key: ${LOCAL_KEY}
    models:
      - model: only
"""
    write_config(isolated_runtime, one)
    before = written(isolated_runtime)

    async with Client(server_for(isolated_runtime, one)) as client:
        answer = await ask(client, "model_remove", ref="local/only")

    assert answer.startswith("[refused]") and "last model" in answer
    assert written(isolated_runtime) == before


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
        assert "was `local/big`" in answer

        before = written(isolated_runtime)
        refused = await ask(client, "model_switch", ref="nobody/knows")

    assert refused.startswith("[refused]")
    assert written(isolated_runtime) == before, "rolled back"
    assert "providers" in written(isolated_runtime), "the file survived the refusal"


@pytest.mark.asyncio
async def test_the_four_tools_are_marked_for_the_model(isolated_runtime) -> None:
    """**The mark is what makes them reachable**, and it is the only thing here
    that a unit test can check about the hub.

    A plugin's own tools arrive by `tools/list` and are offered to the model only
    if they declare themselves the model's (`slife2.audience`) — `_check_new_input`
    is the precedent, and the agent server is one of `Config.plugins()`, so these
    four arrive by exactly the same road.
    """
    from slife2.audience import for_the_model

    async with Client(server_for(isolated_runtime)) as client:
        tools = {one.name: one for one in await client.list_tools()}

    for name in ("model_list", "model_set", "model_remove", "model_switch"):
        assert for_the_model(tools[name].meta), name
    assert not for_the_model(tools["send_message"].meta), (
        "the client's tools stay its own"
    )
