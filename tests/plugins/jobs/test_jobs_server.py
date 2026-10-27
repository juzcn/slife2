"""`slife2-jobs` — code the model wrote, turned into tools.

A job is one file in `<data>/jobs/`; the file's function is the tool.  What
these tests are about is the loop the whole idea rests on: write → it is a row
the model can call → edit → the row changes → remove → it is gone, with no
restart and no second registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from slife2.libs.host.config import load
from slife2.libs.plugin.protocol import CALL_SOURCE, LIST_SOURCES
from slife2.plugins.jobs.server import CATEGORY, SERVER_NAME, SOURCE, build_server
from tests.fakes import config_file

pytestmark = pytest.mark.unit

#: A config the loader accepts.  The plugin reads it for the homes and for the
#: handles' addresses; these tests never make a real hop.
CONFIG = """\
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    api_key: ${JOBS_TEST_KEY:-none}
    models:
      - model: big
        context_window: 100000
        max_tokens: 4000
embeddings:
  providers:
    local:
      base_url: http://127.0.0.1:17347/v1
      model: big-emb
default: local/big
"""

#: A pure job: a new deterministic tool, no handles, no hops.
SLUGIFY = '''\
def slugify(title: str, sep: str = "-") -> str:
    """Convert a title into a URL slug.

    Args:
        title: The title to slugify.
        sep: Separator between words.
    """
    import re
    return sep.join(re.findall(r"[a-z0-9]+", title.lower()))
'''


def config_on_disk(data_dir: Path) -> Path:
    return config_file(data_dir, CONFIG)


async def ask(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert isinstance(result.data, dict), result
    return result.data


async def declared(client: Client) -> dict[str, Any]:
    result = await client.call_tool(LIST_SOURCES, {})
    assert isinstance(result.data, dict), result
    (source,) = result.data["sources"]
    return source


@pytest.mark.asyncio
async def test_a_job_set_is_a_tool_the_catalogue_can_call(
    isolated_runtime: Path,
) -> None:
    """**The whole loop, in one test.**  `job_set` writes the file; the source's
    next declaration already lists it; the hub names it `job__<name>`; the code
    runs with the arguments the schema describes; `job_remove` takes it back.
    """
    config_on_disk(isolated_runtime)
    async with Client(build_server(load())) as client:
        made = await ask(client, "job_set", name="slugify", code=SLUGIFY)
        source = await declared(client)
        listed = await ask(client, "job_list")
        ran = await ask(
            client,
            "job_run",
            name="slugify",
            arguments={"title": "Hello, World!"},
        )
        called = await client.call_tool(
            CALL_SOURCE,
            {
                "source": SOURCE,
                "tool": "slugify",
                "arguments": {"title": "Hello, World!"},
            },
        )
        read = await ask(client, "job_read", name="slugify")
        removed = await ask(client, "job_remove", name="slugify")
        after = await declared(client)

    assert made == {
        "name": "slugify",
        "tool": "job__slugify",
        "status": "enabled",
    }
    assert (source["name"], source["category"], source["up"]) == (
        SOURCE,
        CATEGORY,
        True,
    )
    (row,) = source["rows"]
    assert (row["name"], row["remote_name"], row["status"]) == (
        "job__slugify",
        "slugify",
        "enabled",
    )
    schema = json.loads(row["schema"])
    assert set(schema["properties"]) == {"title", "sep"}
    assert listed["jobs"][0]["tool"] == "job__slugify"
    assert ran == {"name": "slugify", "ok": True, "text": "hello-world"}
    assert called.data == {"text": "hello-world", "ok": True}
    assert read["code"] == SLUGIFY
    assert "def slugify" in read["code"]
    assert removed == {"name": "slugify", "removed": True}
    assert after["rows"] == []
    assert not (isolated_runtime / "jobs" / "slugify.py").exists()


@pytest.mark.asyncio
async def test_a_broken_write_restores_the_previous_job(
    isolated_runtime: Path,
) -> None:
    """**The installed tool is never a broken file.**  The write lands, the load
    fails, the previous code goes back — and the tool that was working still
    works, from the same call."""
    config_on_disk(isolated_runtime)
    async with Client(build_server(load())) as client:
        await ask(client, "job_set", name="slugify", code=SLUGIFY)
        with pytest.raises(ToolError, match="not installed"):
            await client.call_tool(
                "job_set", {"name": "slugify", "code": "def slugify(:\n"}
            )
        ran = await ask(
            client, "job_run", name="slugify", arguments={"title": "Still works"}
        )

    assert ran["text"] == "still-works"
    assert (isolated_runtime / "jobs" / "slugify.py").read_text(
        encoding="utf-8"
    ) == SLUGIFY


@pytest.mark.asyncio
async def test_a_new_job_that_does_not_load_writes_nothing(
    isolated_runtime: Path,
) -> None:
    config_on_disk(isolated_runtime)
    async with Client(build_server(load())) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "job_set", {"name": "broken", "code": "def broken(:\n"}
            )
        listed = await ask(client, "job_list")

    assert listed["jobs"] == []
    assert not (isolated_runtime / "jobs" / "broken.py").exists()
    assert not (isolated_runtime / "jobs" / ".broken.installing").exists()


@pytest.mark.asyncio
async def test_a_job_must_be_the_function_named_after_it_and_say_what_it_is_for(
    isolated_runtime: Path,
) -> None:
    config_on_disk(isolated_runtime)
    async with Client(build_server(load())) as client:
        with pytest.raises(ToolError, match="named"):
            await client.call_tool(
                "job_set",
                {
                    "name": "one",
                    "code": 'def two() -> str:\n    """Wrong name."""\n    return "x"\n',
                },
            )
        with pytest.raises(ToolError, match="docstring"):
            await client.call_tool(
                "job_set",
                {"name": "one", "code": "def one() -> str:\n    return 'x'\n"},
            )

    assert not (isolated_runtime / "jobs" / "one.py").exists()


@pytest.mark.asyncio
async def test_job_names_are_python_identifiers_and_not_the_plugins_own(
    isolated_runtime: Path,
) -> None:
    config_on_disk(isolated_runtime)
    async with Client(build_server(load())) as client:
        for name in ("job_list", "with-dash", "_private", "../outside"):
            with pytest.raises(ToolError):
                await client.call_tool("job_set", {"name": name, "code": SLUGIFY})


@pytest.mark.asyncio
async def test_a_file_that_does_not_load_stays_visible_and_is_not_callable(
    isolated_runtime: Path,
) -> None:
    """**A broken file is a row, not a disappearance.**  The folder says a job is
    installed; hiding it would send the model looking for a tool it can see on
    disk, so the row says `error`, `job_read` shows the code, and the per-job
    tool refuses with the reason.
    """
    config_on_disk(isolated_runtime)
    folder = isolated_runtime / "jobs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    async with Client(build_server(load())) as client:
        source = await declared(client)
        listed = await ask(client, "job_list")
        read = await ask(client, "job_read", name="broken")
        refused = await client.call_tool(
            CALL_SOURCE,
            {"source": SOURCE, "tool": "broken", "arguments": {}},
        )
        with pytest.raises(ToolError, match="not loaded"):
            await client.call_tool("job_run", {"name": "broken"})

    (row,) = source["rows"]
    assert (row["name"], row["status"], row["schema"]) == (
        "job__broken",
        "error",
        "",
    )
    assert listed["jobs"][0]["status"] == "error"
    assert "file" not in listed["jobs"][0], "a path is not the model's to act on"
    assert read["code"] == "def broken(:\n"
    assert read["error"], "the broken line's reason is the answer"
    assert "file" not in read
    assert refused.data["ok"] is False
    assert "not loaded" in refused.data["text"]


@pytest.mark.asyncio
async def test_a_missing_job_is_a_refusal_not_an_empty_answer(
    isolated_runtime: Path,
) -> None:
    config_on_disk(isolated_runtime)
    async with Client(build_server(load())) as client:
        with pytest.raises(ToolError, match="no job called") as raised:
            await client.call_tool("job_run", {"name": "nothing"})
        assert "nothing close is installed" in str(raised.value)
        assert "job_set" in str(raised.value)
        with pytest.raises(ToolError, match="no job called"):
            await client.call_tool("job_read", {"name": "nothing"})
        with pytest.raises(ToolError, match="no job called"):
            await client.call_tool("job_remove", {"name": "nothing"})


def test_the_plugin_is_named_where_a_client_can_find_it() -> None:
    assert SERVER_NAME == "slife2-jobs"


@pytest.mark.asyncio
async def test_a_misspelled_job_suggests_the_one_it_meant(
    isolated_runtime: Path,
) -> None:
    """A typo and an absence are different answers.  The close name is the
    correction; the per-job tool's refusal carries the same suggestion."""
    config_on_disk(isolated_runtime)
    async with Client(build_server(load())) as client:
        await ask(client, "job_set", name="slugify", code=SLUGIFY)
        with pytest.raises(ToolError, match="did you mean") as raised:
            await client.call_tool("job_read", {"name": "slugifi"})
        called = await client.call_tool(
            CALL_SOURCE,
            {"source": SOURCE, "tool": "slugifi", "arguments": {}},
        )

    assert "'slugify'" in str(raised.value)
    assert called.data["ok"] is False
    assert "did you mean" in called.data["text"]
    assert "slugify" in called.data["text"]
