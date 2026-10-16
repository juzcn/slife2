"""`slife2-restapi-tools`' five tools: the model editing `rest-api:`.

The same set as the MCP family, over the other section — v1's split, and what
these tests are about is the part that is *not* the same: an API is written as a
spec and a base URL and expanded by the config layer into a proxy command, so
what the model writes and what the file holds are the declarative pair, and what
follows from that is a listing that shows a spec rather than `uvx`, a credential
stored as a reference rather than a secret, and one URL check before anything is
fetched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from slife2.libs.host.config import load
from slife2.libs.host.configfile import read_section
from slife2.plugins.restapi.server import build_server
from tests.fakes import answering, config_file

pytestmark = pytest.mark.unit

CONFIG = """\
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    api_key: ${SLIFE2_TEST_KEY:-none}
    models:
      - model: big
        context_window: 100000
        max_tokens: 4000
default: local/big

# ── REST APIs ───────────────────────────────────────────────────────────────
rest-api:
  # Already here, and this comment is the file's.
  registry:
    spec: https://registry.example.test/openapi.json
    base_url: https://registry.example.test
    description: A registry.
"""


def write_config(data_dir: Path, text: str = CONFIG) -> Path:
    """This module's config, written where the loader looks for it."""
    return config_file(data_dir, text)


def upstream() -> FastMCP:
    """The proxy's own server, with a countable number of operations."""
    server = FastMCP("proxy")

    @server.tool
    def list_things() -> str:
        """List the things."""
        return "[]"

    @server.tool
    def get_thing(identifier: str) -> str:
        """Fetch one thing."""
        return identifier

    return server


def family(isolated_runtime: Path, text: str = CONFIG) -> FastMCP:
    write_config(isolated_runtime, text)
    return build_server(load(), client_factory=answering(upstream()))


async def ask(client: Client, tool: str, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    return "".join(getattr(block, "text", "") for block in result.content or [])


def section(isolated_runtime: Path) -> dict[str, Any]:
    return read_section("rest-api", path=isolated_runtime / "slife2.yaml")


# --- what is written ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_api_is_written_as_the_spec_it_is(isolated_runtime) -> None:
    """**The difference from v1, and from the MCP family next door.**

    v1 wrote the expanded `uvx mcp-openapi-proxy` entry, because its config held
    what it ran.  Here the section is declarative and the config layer expands
    it (`_rest_api`), so the entry a model writes is the pair a person would
    write — which is also the only form that survives the proxy being swapped
    for something else.
    """
    async with Client(family(isolated_runtime)) as client:
        answer = await ask(
            client,
            "rest_api_set",
            name="github",
            spec="https://api.example.test/openapi.yaml",
            base_url="https://api.example.test",
            description="Issues and pull requests.",
        )

    entry = section(isolated_runtime)["github"]
    assert entry["spec"] == "https://api.example.test/openapi.yaml"
    assert entry["base_url"] == "https://api.example.test"
    assert "command" not in entry, "the expansion is the config layer's, not the file's"
    assert "serving" in answer and "2 operation(s)" in answer
    assert "tool(s)" not in answer, "the other family's word for it"


@pytest.mark.asyncio
async def test_the_credential_is_stored_as_a_name_and_never_as_a_value(
    isolated_runtime,
) -> None:
    """**The one place a model could put a secret into a file that gets shared.**

    `api_key` is the *variable name*, as in v1 — the file keeps `${GITHUB_TOKEN}`
    and the value stays in the credential store.  A caller that already knows
    the syntax is not second-guessed.
    """
    async with Client(family(isolated_runtime)) as client:
        await ask(
            client,
            "rest_api_set",
            name="github",
            spec="https://api.example.test/openapi.yaml",
            base_url="https://api.example.test",
            api_key="GITHUB_TOKEN",
        )
        written_as_given = await ask(
            client,
            "rest_api_set",
            name="other",
            spec="https://api.example.test/openapi.yaml",
            base_url="https://api.example.test",
            api_key="${SOME_OTHER_TOKEN}",
        )

    assert section(isolated_runtime)["github"]["api_key"] == "${GITHUB_TOKEN}"
    assert section(isolated_runtime)["other"]["api_key"] == "${SOME_OTHER_TOKEN}"
    assert "serving" in written_as_given
    assert "connected" not in written_as_given, (
        "the MCP family's word, in an answer about an API"
    )


@pytest.mark.asyncio
async def test_setting_an_api_again_replaces_the_whole_entry(isolated_runtime) -> None:
    """A `set` says what the entry *is*, so a field left out is gone.

    An API added with a credential and then set again without one keeps it no
    more.  `set_enabled` is the operation that changes one field and leaves the
    rest — a merge here would leave "stop using this key" unsayable.
    """
    async with Client(family(isolated_runtime)) as client:
        await ask(
            client,
            "rest_api_set",
            name="github",
            spec="https://api.example.test/openapi.yaml",
            base_url="https://api.example.test",
            api_key="GITHUB_TOKEN",
        )
        await ask(
            client,
            "rest_api_set",
            name="github",
            spec="https://api.example.test/openapi.yaml",
            base_url="https://api.example.test",
        )

    entry = section(isolated_runtime)["github"]
    assert entry["spec"] == "https://api.example.test/openapi.yaml"
    assert "api_key" not in entry, "the credential this call did not name is gone"


@pytest.mark.asyncio
async def test_a_key_nothing_resolves_says_so_at_the_moment_it_is_written(
    isolated_runtime,
) -> None:
    """**One line now, or a 401 from somebody else's server one turn later.**

    An unresolved `${VAR}` is not an error — the config keeps the reference and
    the failure surfaces where the value is used — but for an API key the place
    it surfaces names nothing, so the answer says it here instead.
    """
    async with Client(family(isolated_runtime)) as client:
        answer = await ask(
            client,
            "rest_api_set",
            name="github",
            spec="https://api.example.test/openapi.yaml",
            base_url="https://api.example.test",
            api_key="NOBODY_HAS_THIS_ONE",
        )

    assert "does not resolve" in answer
    assert "credstore set NOBODY_HAS_THIS_ONE" in answer
    assert section(isolated_runtime)["github"]["api_key"] == "${NOBODY_HAS_THIS_ONE}"


@pytest.mark.asyncio
async def test_a_keyring_reference_that_does_not_resolve_says_so_as_well(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The other half of that note, and the half that was a hole.**

    A `keyring:NAME` written as an API key is the shape `resolve_secret` leaves
    verbatim when `credstore` cannot be imported — its declared degrade — and the
    value then travels to somebody else's API *as the credential*
    (DESIGN.md §10.1).  The note above is what turns that into a sentence at the
    moment it is written, and it did not: the `keyring:` shape returned early
    with nothing to say, so the two halves of one rule disagreed and the leaking
    half was the silent one.  The rule is
    `slife2.libs.host.config.unresolved_reference`'s, which counts a keyring
    reference with no credstore behind it as unresolved.

    `credstore` is made unimportable rather than assumed absent: it is a real
    dependency and it is installed on the machine this suite runs on, so the
    degrade has to be stated.
    """
    monkeypatch.setitem(sys.modules, "credstore", None)

    async with Client(family(isolated_runtime)) as client:
        answer = await ask(
            client,
            "rest_api_set",
            name="github",
            spec="https://api.example.test/openapi.yaml",
            base_url="https://api.example.test",
            api_key="keyring:slife2/github",
        )

    assert "does not resolve" in answer
    # The name to store it under is the URI's own tail, not `${…}`'s arithmetic:
    # `reference[2:-1]` would have said `eyring:slife2/gith`.
    assert "credstore set slife2/github" in answer
    assert section(isolated_runtime)["github"]["api_key"] == "keyring:slife2/github"


@pytest.mark.asyncio
async def test_a_url_that_is_not_http_is_refused_before_anything_is_fetched(
    isolated_runtime,
) -> None:
    """**The proxy child fetches the spec, so this URL is a request it makes.**

    v1's check, kept for v1's reason: a `file://` URL or a scheme-less string
    handed to `spec` is a way to make a slife2 process read something nobody
    pointed it at — and the child does the reading, in a process nobody is
    watching.
    """
    write_config(isolated_runtime)
    before = (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")

    async with Client(family(isolated_runtime)) as client:
        local = await ask(
            client,
            "rest_api_set",
            name="sneaky",
            spec="file:///etc/passwd",
            base_url="https://api.example.test",
        )
        nowhere = await ask(
            client,
            "rest_api_set",
            name="sneaky",
            spec="api.example.test/openapi.yaml",
            base_url="https://api.example.test",
        )

    assert local.startswith("[refused]") and "must be an http(s) URL" in local
    assert nowhere.startswith("[refused]")
    assert (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8") == before
    assert "sneaky" not in section(isolated_runtime)


@pytest.mark.asyncio
async def test_a_private_address_is_not_refused(isolated_runtime) -> None:
    """**The half of that check that is a decision rather than a guard.**

    v1 allows private addresses on purpose — a local API is a legitimate thing
    to configure — so this pins the allowance rather than leaving it to be
    "hardened" into a block by whoever reads the check next.  A local spec is a
    spec; what is refused is a URL that is not http(s) at all.
    """
    async with Client(family(isolated_runtime)) as client:
        answer = await ask(
            client,
            "rest_api_set",
            name="localapi",
            spec="http://127.0.0.1:8080/openapi.json",
            base_url="http://127.0.0.1:8080",
        )

    assert not answer.startswith("[refused]")
    assert section(isolated_runtime)["localapi"]["spec"] == (
        "http://127.0.0.1:8080/openapi.json"
    )


@pytest.mark.asyncio
async def test_a_name_under_tools_is_refused_by_the_writer(isolated_runtime) -> None:
    """**The cross-section collision, caught by the loader and rolled back.**

    Both sections become `{name}__{tool}` in one tool list, so `slife2.libs.host.config`
    refuses a name in both — and it is the only place both are known.  What
    matters here is that the refusal reaches the caller *and* leaves the file
    alone: the write is validated before it counts.
    """
    text = (
        CONFIG
        + """\
mcp:
  clash:
    command: npx
"""
    )
    write_config(isolated_runtime, text)
    before = (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8")

    async with Client(family(isolated_runtime, text)) as client:
        answer = await ask(
            client,
            "rest_api_set",
            name="clash",
            spec="https://api.example.test/openapi.yaml",
            base_url="https://api.example.test",
        )

    assert (
        answer.startswith("[refused]") and "already configured under `mcp:`" in answer
    )
    assert (isolated_runtime / "slife2.yaml").read_text(encoding="utf-8") == before


# --- the listing --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_listing_shows_the_spec_and_not_the_proxy(isolated_runtime) -> None:
    """What a person configured, not what the config layer made of it.

    A listing built from the held settings would print `uvx
    mcp-openapi-proxy` and lose the only thing a reader came for — which API
    this is and where it lives.
    """
    async with Client(family(isolated_runtime)) as client:
        listed = await ask(client, "rest_api_list")

    assert "registry [ready]" in listed
    assert "spec     https://registry.example.test/openapi.json" in listed
    assert "base_url https://registry.example.test" in listed
    assert "A registry." in listed
    assert "mcp-openapi-proxy" not in listed


@pytest.mark.asyncio
async def test_the_operations_are_listed_and_capped(isolated_runtime) -> None:
    async with Client(family(isolated_runtime)) as client:
        all_of_them = await ask(client, "rest_api_list_tools", name="registry")
        capped = await ask(client, "rest_api_list_tools", name="registry", limit=1)

    assert "registry — 2 operation(s), showing 2:" in all_of_them
    assert "list_things" in all_of_them and "get_thing" in all_of_them
    assert "2 operation(s), showing 1" in capped and "1 more" in capped


@pytest.mark.asyncio
async def test_removing_and_switching_behave_as_they_do_next_door(
    isolated_runtime,
) -> None:
    """The two lifecycle answers, over this section's words."""
    async with Client(family(isolated_runtime)) as client:
        off = await ask(client, "rest_api_set_enabled", name="registry", enabled=False)
        listed = await ask(client, "rest_api_list")
        off_entry = section(isolated_runtime)["registry"]
        on = await ask(client, "rest_api_set_enabled", name="registry", enabled=True)
        removed = await ask(client, "rest_api_remove", name="registry")
        gone = await ask(client, "rest_api_list")

    assert "switched off" in off and off_entry["enabled"] is False
    assert "registry [off]" in listed
    assert "serving" in on
    assert "gone from `rest-api:`" in removed
    assert "registry" not in gone, gone
    assert "No REST APIs are configured" in gone, gone


@pytest.mark.asyncio
async def test_both_entry_tools_describe_a_live_api_the_same_way(
    isolated_runtime,
) -> None:
    """**One state, one sentence, this family's words** — and it was two.

    `rest_api_set` and `rest_api_set_enabled` reach a live API by different
    routes, and a model that got two accounts of the same connection would be
    reading two vocabularies for one mechanism — which is the failure
    `slife2.libs.plugin.family.FamilyWords` exists to make impossible, and which had
    happened anyway: the `set` answer was written out by hand with `connected`,
    `tool(s)` and `` `set_enabled` `` hardcoded into it, so this family
    described an API as *connected* with two *tools* and pointed at a tool that
    does not exist (`mcp_set_enabled` is the MCP family's).  The `set_enabled`
    answer went through `FamilyWords` and was right, which is exactly why every
    test that touched only *it* passed.

    Both are asked here in one test, because the defect was their disagreement:
    an assertion about either one alone cannot see it.
    """
    async with Client(family(isolated_runtime)) as client:
        written = await ask(
            client,
            "rest_api_set",
            name="other",
            spec="https://api.example.test/openapi.yaml",
            base_url="https://api.example.test",
        )
        switched = await ask(client, "rest_api_set_enabled", name="other", enabled=True)

    for answer in (written, switched):
        assert "serving" in answer, answer
        assert "operation(s)" in answer, answer
        assert "connected" not in answer, answer
        assert "tool(s)" not in answer, answer
