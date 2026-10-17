"""Config loading: providers, models, and the resolution chain for secrets.

The shape is slife v1's, so the tests are about the parts that shape has to get
right — several models per provider, a `provider/model` reference that is
unambiguous, and parameters that are absent rather than defaulted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import slife2.libs.host.config
from slife2.libs.host.config import (
    A2A_BROKER_ENV_VAR,
    API_BACKENDS,
    DEFAULT_TOOL_RESULT_TOKENS,
    LOCAL,
    MIN_TOOL_RESULT_TOKENS,
    PLUGINS,
    REMOTE,
    ConfigError,
    default_config,
    find_config_path,
    load,
    load_cached,
    net_settings,
    resolve_secret,
)
from slife2.libs.host.paths import DATA_ENV_VAR

A_PROVIDER = """
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    api_key: ${SLIFE2_TEST_KEY:-none}
    models:
      - model: big
        context_window: 100000
        max_tokens: 4000
        temperature: 0.3
      - model: small
default: local/small
"""


def write(tmp_path, text: str):
    path = tmp_path / "slife2.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# --- the built-in defaults ---------------------------------------------------


def test_the_defaults_are_one_working_provider() -> None:
    """No config file should still give something that can answer.

    Deliberately *not* equal to the checked-in `slife2.yaml`, which lists the
    providers this machine can reach — a config carrying somebody's provider
    table would be wrong for everybody else.
    """
    config = default_config()
    assert list(config.providers) == ["deepseek"]
    assert config.default == "deepseek/deepseek-flash"


def test_the_defaults_resolve() -> None:
    config = default_config()
    name, provider, model = config.resolve()
    assert name == "deepseek"
    assert provider.api in API_BACKENDS
    assert model.model == "deepseek-flash"
    # A provider has no address of its own: it is reached through the server for
    # its protocol, which one process speaks for every provider that uses it.
    assert config.url_for("deepseek/deepseek-flash").startswith("http://")


def test_every_api_has_a_backend_module() -> None:
    for api, module in API_BACKENDS.items():
        assert module.startswith("slife2.plugins.llm."), api


def test_every_plugin_row_describes_a_real_server() -> None:
    """A row of `PLUGINS` names a module, and that module answers to its name.

    **This used to be fifteen spelled-out assertions**, one per server, because
    each name was written twice — here and as a `SERVER_NAME` literal in the
    server's own module — and something had to notice when one of them drifted.
    The name has one home now, so what is left to check is the half a table
    cannot check about itself: that the module path is a module, and that the
    module agrees about the name it puts on the wire (`FastMCP(name)`), which is
    what a client compares against to prove it reached the server it meant
    (`slife2.libs.plugin.protocol.identifies`).

    Importing a server module is safe *here* and nowhere else in this file's
    neighbourhood: the rule the names exist for is that the TUI and the agent
    loop must not, and this is a test that does nothing else.
    """
    import importlib

    for one in PLUGINS:
        module = importlib.import_module(one.module)
        assert module.SERVER_NAME == one.name, one.key
        assert one.module.startswith("slife2.plugins."), one.key
        assert 0 < one.port < 65536, one.key
    assert len({one.key for one in PLUGINS}) == len(PLUGINS)
    assert len({one.name for one in PLUGINS}) == len(PLUGINS)


def test_a_name_the_model_may_not_unload_is_spelled_on_both_sides() -> None:
    """`skill_use` is served by a plugin and refused by the hub.

    The name crosses a process boundary — `slife2-skills` serves the tool, and
    `slife2.plugins.toolhub.server` keeps it on the list the model cannot unload, because the
    playbooks are what every session is meant to reach for — so it is written
    twice.  This is what notices when one side is renamed and the other is not,
    and the failure it prevents is silent in both directions: a hub spelling a
    name nothing serves protects nothing, and a server renamed out from under
    the hub hands the model a tool it can throw away.
    """
    from slife2.plugins.skills import playbooks as skills
    from slife2.plugins.skills.server import CONFIG_KEY, SOURCE
    from slife2.plugins.toolhub.server import ALWAYS_LOADED, SKILL_USE

    assert SKILL_USE == skills.USE_TOOL
    assert SKILL_USE in ALWAYS_LOADED

    # And the two ids a declared row must keep apart: the source is what the
    # catalogue files the rows under and the key is what the launcher starts.
    assert SOURCE != CONFIG_KEY


def test_a_document_source_has_one_spelling() -> None:
    """**One name, four readers, and no second copy to keep in step.**

    The boot pass needs these without asking anybody (`ToolStore.reset` withdraws
    the verdict on rows whose source the config does not name), the hub files
    rows under them, and the family plugins refuse a name that would collide with
    one — so four modules read `skills` and `cli`.

    This used to be three copies held together by this test, which is a test
    standing in for a language feature.  Now the copies are *aliases*, and what
    is asserted is that nothing has quietly grown a fourth.
    """
    from slife2.libs.plugin.protocol import CLI_SOURCE as CONTRACT_CLI
    from slife2.libs.plugin.protocol import SKILLS_SOURCE as CONTRACT_SKILLS
    from slife2.plugins.cli.server import SOURCE as CLI_SOURCE
    from slife2.plugins.skills.server import SOURCE as SKILLS_SOURCE
    from slife2.plugins.toolhub.server import CLI_SOURCE as HUB_CLI
    from slife2.plugins.toolhub.server import SKILLS_SOURCE as HUB_SKILLS

    assert HUB_SKILLS is SKILLS_SOURCE is CONTRACT_SKILLS
    assert HUB_CLI is CLI_SOURCE is CONTRACT_CLI


def test_the_cut_in_name_is_spelled_on_all_four_sides() -> None:
    """`_check_new_input` is written into a conversation, so four things must agree.

    One name crosses more boundaries than `skill_use` does, because this tool is
    not merely refused to the model — it is *called by the harness* and its pair
    is read back by the model:

    * `slife2.plugins.agent.server` declares it and writes the call,
    * `slife2.plugins.toolhub.server` keeps it on the list the model cannot unload,
    * `slife2.libs.store.db` is the gate that lets a `_`-prefixed name through at all, and
    * the request's `tools` array has to carry it, or the Responses and Messages
      backends reject every turn that follows the delivery.

    Each of those is a silent failure in a different direction — a pair naming a
    tool that is not declared is a 400, and a name the model can unload is a 400
    one turn later — so the spellings are held together here rather than left to
    a comment.
    """
    from slife2.libs.store.catalogue import MODEL_VISIBLE_HARNESS_TOOLS
    from slife2.plugins.agent.server import CHECK_NEW_INPUT
    from slife2.plugins.toolhub.server import ALWAYS_LOADED

    assert CHECK_NEW_INPUT == "_check_new_input"
    assert CHECK_NEW_INPUT in ALWAYS_LOADED
    assert CHECK_NEW_INPUT in MODEL_VISIBLE_HARNESS_TOOLS


# --- providers and models ----------------------------------------------------


def test_a_provider_carries_its_credentials_and_its_models(tmp_path) -> None:
    config = load(write(tmp_path, A_PROVIDER))
    provider = config.provider("local")
    assert provider.api == "openai-completions"
    assert provider.base_url == "https://example.test/v1"
    assert list(provider.models) == ["big", "small"]
    # ...and it is reached through the server for its protocol.
    assert config.url_for("local/big") == config.server("openai-completions").url


def test_a_model_keeps_only_what_was_configured(tmp_path) -> None:
    """Absent is a real value, not a missing one.

    `None` means "send nothing and let the gateway decide".  A gateway that
    rejects a temperature it did not ask for is a real thing, so a default
    quietly substituted here would be a bug in the field most likely to be
    blamed on the gateway.
    """
    provider = load(write(tmp_path, A_PROVIDER)).provider("local")
    big = provider.model("big")
    small = provider.model("small")

    assert (big.temperature, big.top_p, big.max_tokens) == (0.3, None, 4000)
    assert (small.temperature, small.top_p, small.max_tokens) == (None, None, None)
    assert small.context_window == 0


def test_a_model_name_falls_back_to_its_id(tmp_path) -> None:
    provider = load(write(tmp_path, A_PROVIDER)).provider("local")
    assert provider.model("small").label == "small"
    assert provider.model("small").input == ("text",)


def test_vision_is_opted_into_by_listing_image(tmp_path) -> None:
    path = write(
        tmp_path,
        """
providers:
  p:
    api: openai-completions
    base_url: https://example.test/v1
    models:
      - model: sees
        input: [text, image]
      - model: blind
""",
    )
    provider = load(path).provider("p")
    assert provider.model("sees").accepts_images is True
    assert provider.model("blind").accepts_images is False


def test_the_responses_store_flag_is_tri_state(tmp_path) -> None:
    """Absent must stay distinguishable from `false`.

    They are different requests: leaving `compat.store` out sends nothing and
    keeps whatever default the endpoint has — which for this API is to retain
    the response — while `store: false` asks for the opposite.  A `bool(...)` in
    the parser would collapse the two, and the bug would look like a config that
    quietly stopped working.
    """
    path = write(
        tmp_path,
        """
providers:
  p:
    api: openai-responses
    base_url: https://example.test/v1
    models:
      - model: refused
        compat: {store: false}
      - model: asked
        compat: {store: true}
      - model: silent
""",
    )
    provider = load(path).provider("p")
    assert provider.model("refused").store is False
    assert provider.model("asked").store is True
    assert provider.model("silent").store is None


def test_a_store_flag_on_another_protocol_is_carried_but_unused(tmp_path) -> None:
    """The field is on the neutral model, so any provider may set it.

    Nothing rejects it — the backends that do not read it simply ignore it,
    which is what `compat` is for.  What must *not* happen is a crash on a
    config that mentions it under the wrong `api`.
    """
    path = write(
        tmp_path,
        """
providers:
  p:
    api: openai-completions
    base_url: https://example.test/v1
    models:
      - model: m
        compat: {store: false}
""",
    )
    assert load(path).provider("p").model("m").store is False


def test_a_provider_with_nowhere_to_send_is_refused(tmp_path) -> None:
    """The hole the embedding provider always guarded, closed on the chat side.

    A provider entry carrying a key and no `base_url` makes the SDK fall back
    to its **own** default host — so the key, and every message of every
    conversation, is posted to somebody else's server.  The
    `embeddings.providers:` loader has refused this from the start; the chat
    loader accepted it.
    """
    path = write(
        tmp_path,
        """
providers:
  p:
    api: openai-completions
    api_key: sk-live
    models: [{model: m}]
""",
    )
    with pytest.raises(ConfigError, match="needs `base_url`"):
        load(path)


def test_an_explicit_zero_is_not_read_as_absent(tmp_path) -> None:
    """`int(x or default)` reads a written `0` as "not given".

    Two settings fail visibly when that happens: a threshold of zero is the
    configuration the guard exists to refuse, and was silently replaced by the
    default instead; and `port: 0` is a server bound somewhere the launcher
    will never look for it.  `max_iterations: 0` used to fail too — it was read as
    sixteen — and is the one that changed sides: a written zero is now the
    config's "no cap", so this reader keeping it is what makes the setting mean
    what it says.
    """
    path = write(tmp_path, "tool_load:\n  threshold: 0\n")
    with pytest.raises(ConfigError, match="would evict every tool"):
        load(path)

    path = write(tmp_path, "tool_load:\n  threshold: -5\n")
    with pytest.raises(ConfigError, match="would evict every tool"):
        load(path)

    path = write(tmp_path, "agent:\n  max_iterations: 0\n")
    assert load(path).agent.max_iterations == 0, "a written zero is a written zero"

    path = write(tmp_path, "servers:\n  context:\n    port: 0\n")
    with pytest.raises(ConfigError, match="is not a port"):
        load(path)


def test_no_cap_is_spelt_zero_or_not_at_all(tmp_path) -> None:
    """**A cap is something asked for**, so a config that does not ask for one
    does not have one: an absent key and a written `0` are one statement, and
    the loop is handed the same 0 either way.

    A negative is the other side of that.  It is not a number of iterations, and the
    one direction it could be read in — "no cap" — is the setting that never
    stops, so a config that missed a keystroke would get the most expensive of
    the three meanings rather than the one line every other bad value produces.
    """
    assert load(write(tmp_path, "agent: {}\n")).agent.max_iterations == 0
    assert (
        load(write(tmp_path, "agent:\n  max_iterations: 0\n")).agent.max_iterations == 0
    )

    with pytest.raises(ConfigError, match="is not an iteration count"):
        load(write(tmp_path, "agent:\n  max_iterations: -1\n"))


def test_a_server_path_that_is_not_a_path_is_refused(tmp_path) -> None:
    """**A path is the third field of an address, and it was the unchecked one.**

    `path: mcp` — no leading slash — was accepted, and `ServerSettings.url`
    puts the address and the path together with nothing between them, so the
    endpoint became `http://127.0.0.1:8005mcp`.  That is not a URL whose port
    can be read: the first thing to read one (`runtime.normalize_url`'s
    `urlsplit(...).port`, reached from `start_lock` inside the start-up loop)
    raised `ValueError`, after some servers were already up — where every other
    bad field in this file gets one sentence and nothing starts.  `port: 0`, one
    line above, is the same refusal for the same reason.
    """
    for written in ("mcp", "mcp/route"):
        with pytest.raises(ConfigError, match="servers.context.path"):
            load(write(tmp_path, f"servers:\n  context:\n    path: {written}\n"))

    path = write(tmp_path, "servers:\n  context:\n    path: /route\n")
    assert load(path).server("context").path == "/route"
    assert load(path).server("context").url.endswith("/route"), "one address, whole"


def test_a_server_host_that_is_not_a_host_is_refused(tmp_path) -> None:
    """**A host is the first of `_server`'s three fields, and it was the last one
    still unchecked.**

    `ServerSettings.url` writes the address as `host:port` with nothing around
    it, so `host: "::1"` built `http://::1:8010/mcp` — and that is not a URL
    whose port can be read: `urlsplit("http://::1:8010/mcp").port` raised
    `ValueError: Port could not be cast to integer value as ':1:8010'` out of
    `start_lock`, in the start-up loop, after some servers were already up.
    `host: 0.0.0.0/x` is the same mistake from the other side: the path moves
    into the address and the port is gone.
    """
    for written in ("'::1'", "0.0.0.0/x"):
        with pytest.raises(ConfigError, match=r"servers\.context\.host"):
            load(write(tmp_path, f"servers:\n  context:\n    host: {written}\n"))

    one = load(write(tmp_path, "servers:\n  context:\n    host: localhost\n"))
    assert one.server("context").host == "localhost"
    assert one.server("context").url.startswith("http://localhost:"), (
        "the address is the host and the port together"
    )


def test_a_value_that_is_not_a_number_is_a_config_error(tmp_path) -> None:
    """**A typo has to read like every other typo.**

    The numeric readers were a bare `int()`/`float()`, so `max_iterations: many`
    raised `ValueError` — which nothing above catches, since the whole load path
    reports `ConfigError` and the CLI's one handler is for that.  The user got a
    traceback out of the `slife2` console script instead of the one line naming
    the field.
    """
    for text, expected in [
        ("agent:\n  max_iterations: many\n", "agent.max_iterations"),
        ("tool_load:\n  threshold: lots\n", "tool_load.threshold"),
        ("tool_result:\n  tokens: lots\n", "tool_result.tokens"),
        ("context:\n  ceiling: high\n", "context.ceiling"),
        ("servers:\n  context:\n    port: eight\n", "servers.context.port"),
    ]:
        with pytest.raises(ConfigError, match=expected):
            load(write(tmp_path, text))


def test_the_window_is_one_number_for_both_sides(tmp_path) -> None:
    """A reader and the shortening it must not out-run share one setting.

    Two numbers would be two answers to "how much fits", and the failure they
    produce is a loop with this system's machinery at both ends: a tool of ours
    handing the harness's own shortening more than it shortened away, which the
    harness then shortens again.  So it is a section, and zero is refused for the
    reason `tool_load.threshold: 0` is — a window that holds nothing is not a
    configuration, it is a reader that answers nothing and a shortening that
    keeps nothing.
    """
    assert (
        load(write(tmp_path, "{}\n")).tool_result.tokens == DEFAULT_TOOL_RESULT_TOKENS
    )
    stated = load(write(tmp_path, "tool_result:\n  tokens: 500\n")).tool_result
    assert stated.tokens == 500

    path = write(tmp_path, "tool_result:\n  tokens: 0\n")
    with pytest.raises(ConfigError, match="smaller than the window's own address"):
        load(path)


def test_a_window_smaller_than_its_own_address_is_refused(tmp_path) -> None:
    """**Refused where it is written, not at the turn it would ruin.**

    A window over a shortened result opens with the line that says where the
    whole of it is, and that address costs 50-61 tokens for the call ids the
    providers emit — more than a small budget has in total.  `dumps.window`
    refuses to hand back a window whose address it had to cut, so such a budget
    can only ever fail a turn; the config is where that is worth saying, which
    is the argument this section already makes about zero.
    """
    with pytest.raises(ConfigError, match="smaller than the window's own address"):
        load(write(tmp_path, f"tool_result:\n  tokens: {MIN_TOOL_RESULT_TOKENS - 1}\n"))

    assert (
        load(
            write(tmp_path, f"tool_result:\n  tokens: {MIN_TOOL_RESULT_TOKENS}\n")
        ).tool_result.tokens
        == MIN_TOOL_RESULT_TOKENS
    )


def test_the_terminal_is_a_server_now(tmp_path) -> None:
    """`tui` is one of ours, addressed like every other one.

    It used to be a section of its own — an address slife2 never started — so
    this asserts the shape that replaced it: the name is in `LOCAL_SERVERS`
    (which is also what makes `servers.tui` legal to write at all), the default
    address is the port it always had, and a config can move it.
    """
    assert "tui" in slife2.libs.host.config.LOCAL_SERVERS
    assert slife2.libs.host.config.default_config().server("tui").port == 8005

    moved = load(write(tmp_path, "servers:\n  tui:\n    port: 9005\n"))
    assert moved.server("tui").url == "http://127.0.0.1:9005/mcp"


def test_a_leftover_tui_section_is_refused(tmp_path) -> None:
    """The old spelling parsed and was *silently* ignored, which is the reason
    this is an error rather than a migration.

    Every key in `tui:` is optional, so a config written before the move would
    load cleanly with its port discarded — a window that quietly listened
    somewhere else than the file said.
    """
    with pytest.raises(ConfigError, match="servers.tui"):
        load(write(tmp_path, "tui:\n  port: 9005\n"))


def test_a_leftover_tools_section_is_refused(tmp_path) -> None:
    """`tools:` is `mcp:` now, and this one is refused for the `tui:` reason
    with more force behind it.

    A top-level key nothing reads is not an error anywhere else in this loader,
    so a config that still said `tools:` would load with every one of those
    servers gone and nothing said — the operator's model would simply have
    fewer tools, and nothing would name the reason.  The section is named for
    the catalogue category its rows are declared under, and that is `mcp`.
    """
    with pytest.raises(ConfigError, match="tools: is `mcp:`"):
        load(write(tmp_path, "tools:\n  serper:\n    command: npx\n"))


def test_an_empty_providers_section_is_refused(tmp_path) -> None:
    """**Absent is the built-in provider; empty is a mistake.**

    `providers: {}` fell through the same `if not raw` test as no section at
    all, so a config that named no provider quietly ran on the built-in one and
    called api.deepseek.com — with the operator's own file saying, in the
    section they had just edited, that there was no such provider.
    """
    with pytest.raises(ConfigError, match="at least one provider"):
        load(write(tmp_path, "providers: {}\n"))

    # Absent is still the defaults, which is what a fresh checkout starts with —
    # and the invariant is the *table*, not "some provider": a loader that
    # answered with one stray entry would satisfy a bare truthiness test while
    # having lost the built-in provider a config-less machine runs on.
    config = load(write(tmp_path, "# nothing here\n"))
    assert set(config.providers) == {"deepseek"}
    assert config.default == "deepseek/deepseek-flash"


def test_the_defaults_are_one_object_while_there_is_no_file(
    tmp_path, monkeypatch
) -> None:
    """**Identity is promised to config-less installs too.**

    `load_cached`'s whole contract is that an unchanged file hands back the
    *same* `Config`, so a reader asks "did an edit happen" with `is`.  With no
    file it returned a fresh `default_config()` per call, and `LiveProviders`
    compares with `is` — so a machine with no `slife2.yaml`, which is every
    fresh checkout, rebuilt the provider table and every cached SDK client on
    every model call.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    monkeypatch.setattr(slife2.libs.host.config, "_latest", {})

    assert load_cached() is load_cached()

    # And a file appearing is still picked up rather than cached past.
    write(tmp_path, "agent:\n  max_iterations: 7\n")
    assert load_cached().agent.max_iterations == 7


def test_an_unknown_api_is_refused(tmp_path) -> None:
    path = write(
        tmp_path,
        "providers:\n  p:\n    api: carrier-pigeon\n    models: [{model: m}]\n",
    )
    with pytest.raises(ConfigError, match="carrier-pigeon"):
        load(path)


def test_a_provider_without_models_is_refused(tmp_path) -> None:
    path = write(
        tmp_path, "providers:\n  p:\n    api: openai-completions\n    models: []\n"
    )
    with pytest.raises(ConfigError, match="models"):
        load(path)


def test_a_model_entry_without_a_name_is_refused(tmp_path) -> None:
    path = write(
        tmp_path,
        "providers:\n  p:\n    api: openai-completions\n    models: [{name: x}]\n",
    )
    with pytest.raises(ConfigError, match="model"):
        load(path)


# --- references --------------------------------------------------------------


def test_a_reference_is_provider_slash_model(tmp_path) -> None:
    config = load(write(tmp_path, A_PROVIDER))
    name, provider, model = config.resolve("local/big")
    assert (name, model.model) == ("local", "big")
    assert config.url_for("local/big") == config.server("openai-completions").url


def test_a_bare_provider_name_means_its_first_model(tmp_path) -> None:
    """Someone typing `--model deepseek` means the obvious thing."""
    config = load(write(tmp_path, A_PROVIDER))
    assert config.resolve("local")[2].model == "big"


def test_an_unknown_provider_names_the_known_ones(tmp_path) -> None:
    config = load(write(tmp_path, A_PROVIDER))
    with pytest.raises(ConfigError, match="local"):
        config.resolve("nope/x")


def test_an_unknown_model_names_the_known_ones(tmp_path) -> None:
    config = load(write(tmp_path, A_PROVIDER))
    with pytest.raises(ConfigError, match="big"):
        config.resolve("local/nope")


def test_the_default_can_be_a_whole_reference(tmp_path) -> None:
    config = load(write(tmp_path, A_PROVIDER))
    assert config.default == "local/small"
    assert config.resolve()[2].model == "small"


def test_the_default_falls_back_to_the_first_model(tmp_path) -> None:
    """A config that names providers but no default still has to run."""
    path = write(
        tmp_path,
        """
providers:
  p:
    api: openai-completions
    base_url: https://example.test/v1
    models:
      - model: only
""",
    )
    assert load(path).default == "p/only"


# --- file discovery ----------------------------------------------------------


def test_missing_config_is_not_an_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Two claims, one behaviour — and it was two tests with one body.**

    A data directory with no `slife2.yaml` in it is the shipped configuration: a
    fresh checkout starts exactly here.  So the absence is a normal answer from
    `find_config_path` rather than a failure, and `load` answers the built-in
    defaults rather than refusing.  Written once because it is one path through
    the loader; written twice, the second copy was a name with no assertion of
    its own to make.
    """
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))

    assert find_config_path() is None
    assert load() == default_config()


def test_missing_named_config_is_an_error(tmp_path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "absent.yaml")


def test_env_var_names_the_data_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One knob: point it at a folder and the config is looked for inside it."""
    write(tmp_path, A_PROVIDER)
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    assert find_config_path() == tmp_path / "slife2.yaml"
    assert "local" in load().providers


def test_explicit_path_beats_the_env_var(tmp_path, monkeypatch) -> None:
    env_path = write(tmp_path, A_PROVIDER)
    other = tmp_path / "other.yaml"
    other.write_text("default: ''\n", encoding="utf-8")
    monkeypatch.setenv(DATA_ENV_VAR, str(env_path))
    # `other.yaml` has no providers, so it falls back to the defaults, which do
    # not include `local` — enough to tell the two apart.
    assert "local" not in load(other).providers


def test_malformed_yaml_is_fatal(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, "providers: [unclosed\n"))


def test_non_mapping_top_level_is_fatal(tmp_path) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load(write(tmp_path, "- one\n- two\n"))


def test_partial_file_keeps_the_other_defaults(tmp_path) -> None:
    """A file that sets one thing must not blank everything else."""
    config = load(write(tmp_path, "agent:\n  max_iterations: 5\n"))
    assert config.agent.max_iterations == 5
    assert config.providers  # the provider table survived
    assert config.agent.server.port == default_config().agent.server.port


# --- secrets -----------------------------------------------------------------


def test_plaintext_passes_through() -> None:
    assert resolve_secret("sk-literal") == "sk-literal"


def test_env_var_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLIFE2_TEST_KEY", "from-env")
    assert resolve_secret("${SLIFE2_TEST_KEY}") == "from-env"


def test_default_is_used_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLIFE2_TEST_KEY", raising=False)
    assert resolve_secret("${SLIFE2_TEST_KEY:-fallback}") == "fallback"


def test_unset_without_default_stays_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leniency is the contract: the failure lands where the value is *used*."""
    monkeypatch.delenv("SLIFE2_TEST_KEY", raising=False)
    assert resolve_secret("${SLIFE2_TEST_KEY}") == "${SLIFE2_TEST_KEY}"


def test_a_keyring_that_failed_is_not_read_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A credential nobody could read is not a credential that is absent.**

    The lookup swallowed *every* credential-store failure into None, so a keyring
    that exists but is locked or corrupt became "not found" and the `${VAR}` text
    then travelled as the secret itself.  The declared degrade is a host with no
    backend at all; a backend that is there and refuses raises, where the secret
    is resolved.
    """
    import credstore

    def locked(key: str) -> str | None:
        raise RuntimeError("the keyring is locked")

    monkeypatch.setattr(credstore, "get_credential", locked)
    monkeypatch.delenv("SLIFE2_TEST_KEY", raising=False)
    with pytest.raises(RuntimeError, match="locked"):
        resolve_secret("${SLIFE2_TEST_KEY}")


def test_no_keyring_at_all_still_degrades_to_the_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared degrade survives: with no backend, credstore answers None and
    the chain steps past it to the reference (`DESIGN.md` §10.2)."""
    import credstore

    monkeypatch.setattr(credstore, "get_credential", lambda key: None)
    monkeypatch.delenv("SLIFE2_TEST_KEY", raising=False)
    assert resolve_secret("${SLIFE2_TEST_KEY}") == "${SLIFE2_TEST_KEY}"


def test_a_keyring_uri_that_failed_is_not_left_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A keyring URI nobody could resolve is not a URI that is absent.**

    The `keyring:` branch swallowed *every* failure into `pass`, so a keyring
    that exists but is locked, or a credential that was never stored, left the
    `keyring:service/key` reference to travel as the secret itself.  Only a host
    with no `credstore` at all degrades to the reference; a backend that is
    there and refused raises where the secret is resolved.

    A store that was asked and refused is the case this test holds, and it is
    the one exception type `resolve_secret` deliberately does *not* translate —
    the machine's keyring being locked is not a mistake in the file.
    """
    import credstore

    def locked(value: str) -> str:
        raise RuntimeError("the keyring is locked")

    monkeypatch.setattr(credstore, "resolve_uri", locked)
    with pytest.raises(RuntimeError, match="locked"):
        resolve_secret("keyring:slife/deepseek")


def test_a_keyring_uri_nobody_stored_is_a_config_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**credstore's "not found" is a config mistake, and it has to read like one.**

    credstore raises a bare `KeyError` for a credential that was never stored,
    and `KeyError` is not what anything on the load path catches: `slife2`'s
    entry point and `family.py` each answer a bad config with their own one
    sentence for `ConfigError` alone, so
    `env: {TOKEN: 'keyring:slife/nothing-stored'}` printed a traceback out of the
    console script instead of the refusal naming the reference — at the one
    place where naming it is the whole of the fix.  Tested through `load` rather
    than through `resolve_secret` alone, because "out of `load()`" is the
    failure as it reaches a person.
    """
    import credstore

    def missing(value: str) -> str:
        raise KeyError("Credential 'slife/nothing-stored' not found in keyring")

    monkeypatch.setattr(credstore, "resolve_uri", missing)

    with pytest.raises(ConfigError) as raised:
        resolve_secret("keyring:slife/nothing-stored")
    assert "keyring:slife/nothing-stored" in str(raised.value)
    assert "credstore set slife/nothing-stored" in str(raised.value), (
        "the store is named, and so is the way to fill it"
    )

    text = (
        "mcp:\n"
        "  one:\n"
        "    command: npx\n"
        "    env:\n"
        "      TOKEN: keyring:slife/nothing-stored\n"
    )
    with pytest.raises(ConfigError, match="nothing-stored"):
        load(write(tmp_path, text))


def test_no_credstore_at_all_leaves_a_keyring_uri_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared degrade survives: with no backend, a `keyring:` reference is
    left exactly as written (`DESIGN.md` §10.2)."""
    monkeypatch.setitem(sys.modules, "credstore", None)
    assert resolve_secret("keyring:slife/deepseek") == "keyring:slife/deepseek"


def test_reference_embedded_in_a_larger_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLIFE2_TEST_HOST", "example.test")
    assert resolve_secret("https://${SLIFE2_TEST_HOST}/v1") == "https://example.test/v1"


def test_non_string_scalars_become_strings() -> None:
    assert resolve_secret(12345) == "12345"


def test_braces_that_are_not_references_are_untouched() -> None:
    assert resolve_secret("use ${1+1} here") == "use ${1+1} here"


def test_api_key_is_resolved_lazily(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw reference is what the config holds; the key is read on demand.

    This is what keeps the TUI process — which never needs a provider key —
    from opening the OS keyring at startup.
    """
    monkeypatch.setenv("SLIFE2_TEST_KEY", "resolved-later")
    provider = load(write(tmp_path, A_PROVIDER)).provider("local")
    assert provider.api_key_ref == "${SLIFE2_TEST_KEY:-none}"
    assert provider.api_key == "resolved-later"


# --- the tool sections --------------------------------------------------------
#
# `mcp:` is other people's MCP servers and `rest-api:` is other people's REST
# APIs, and the second is expanded into the first at load time — so what these
# are about is the expansion, and the refusals that keep an ambiguous entry from
# becoming a silent precedence rule.

MCP = """
mcp:
  filesystem:
    command: npx
    args: [-y, server-filesystem, .]
    description: Files on disk.
  serper:
    command: npx
    args: [-y, serper]
    env:
      SERPER_API_KEY: ${SLIFE2_TEST_KEY:-unset}
  arxiv:
    url: https://example.test/mcp
    headers:
      Authorization: Bearer ${SLIFE2_TEST_KEY:-unset}
  off:
    command: npx
    args: [-y, slow-thing]
    enabled: false
rest-api:
  registry:
    spec: https://example.test/openapi.json
    base_url: https://api.example.test
    api_key: ${SLIFE2_TEST_KEY:-unset}
"""


def test_a_stdio_entry_is_a_command(tmp_path) -> None:
    server = load(write(tmp_path, MCP)).mcp["filesystem"]
    assert (server.transport, server.command) == ("stdio", "npx")
    assert server.args == ("-y", "server-filesystem", ".")
    assert server.description == "Files on disk."


def test_an_entry_with_a_url_is_http(tmp_path) -> None:
    """Which field is set *is* the transport; there is no separate switch."""
    server = load(write(tmp_path, MCP)).mcp["arxiv"]
    assert server.transport == "http"
    assert server.url == "https://example.test/mcp"


def test_a_tool_servers_secrets_go_through_the_same_chain(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLIFE2_TEST_KEY", "resolved")
    config = load(write(tmp_path, MCP))
    serper = config.mcp["serper"]
    assert serper.env["SERPER_API_KEY"] == "${SLIFE2_TEST_KEY:-unset}", (
        "the load keeps the reference; the connection resolves it"
    )
    assert serper.resolved_env()["SERPER_API_KEY"] == "resolved"
    assert config.mcp["arxiv"].resolved_headers()["Authorization"] == "Bearer resolved", (
        "a header is a credential too"
    )


def test_a_tool_servers_reference_is_not_resolved_by_the_load(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every server loads this config; one makes the connection.

    Resolving a ``${VAR}`` at load made each server process open the credential
    store once per reference — on WSL that is a `powershell.exe` per lookup, and
    the whole stack paid it for keys only the hub uses.
    """
    calls: list[str] = []
    monkeypatch.delenv("SLIFE2_TEST_KEY", raising=False)
    monkeypatch.setattr(
        slife2.libs.host.config, "_credstore_lookup", lambda key: calls.append(key)
    )

    config = load(write(tmp_path, MCP))

    assert calls == []
    assert config.mcp["serper"].resolved_env()["SERPER_API_KEY"] == "unset"
    assert calls == ["SLIFE2_TEST_KEY"]


def test_a_tunnel_option_is_not_resolved_by_the_load(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider that understands an option is the one that pays for it."""
    calls: list[str] = []
    monkeypatch.delenv("SLIFE2_TEST_BINARY", raising=False)
    monkeypatch.setattr(
        slife2.libs.host.config, "_credstore_lookup", lambda key: calls.append(key)
    )

    section = load(
        write(
            tmp_path,
            "file-tunnel:\n  providers:\n    ssh:\n"
            "      binary: ${SLIFE2_TEST_BINARY:-ssh}\n",
        )
    ).file_tunnel

    assert calls == []
    assert section.options_for("ssh")["binary"] == "ssh"
    assert calls == ["SLIFE2_TEST_BINARY"]


def test_a_rest_api_is_expanded_into_the_proxy_that_serves_it(tmp_path) -> None:
    """The config layer is where a declarative entry becomes a runnable command.

    The entry becomes an ordinary stdio upstream here, so the plugin that holds
    it — `slife2-restapi-tools` — sees the same shape as one under `mcp:`, and
    the hub sees a source like any other.  Two sections and one mechanism.
    """
    server = load(write(tmp_path, MCP)).rest_apis["registry"]
    assert server.kind == "rest"
    assert (server.transport, server.command) == ("stdio", "uvx")
    assert server.args == ("mcp-openapi-proxy",)
    env = server.resolved_env()
    assert env["OPENAPI_SPEC_URL"] == "https://example.test/openapi.json"
    assert env["SERVER_URL_OVERRIDE"] == "https://api.example.test"
    assert env["API_KEY"] == "unset"  # the ${VAR:-default} form


def test_a_rest_api_may_name_its_own_proxy(tmp_path) -> None:
    """The escape hatch, for a pinned version or a private fork."""
    config = load(
        write(
            tmp_path,
            "rest-api:\n  mine:\n    command: uvx\n    args: [--from, my-proxy, run]\n",
        )
    )
    assert config.rest_apis["mine"].args == ("--from", "my-proxy", "run")


def test_an_entry_with_no_transport_is_refused(tmp_path) -> None:
    with pytest.raises(ConfigError, match="needs `command`"):
        load(write(tmp_path, "mcp:\n  nothing:\n    args: [x]\n"))


def test_an_entry_with_two_transports_is_refused(tmp_path) -> None:
    """Not resolved by a precedence rule nobody would remember.

    This is not hypothetical: v1's own config grew a `url` on an entry that
    still had a `command`, and its rule was that the URL won.
    """
    text = "mcp:\n  both:\n    command: uvx\n    url: https://example.test/mcp\n"
    with pytest.raises(ConfigError, match="both"):
        load(write(tmp_path, text))


def test_a_rest_api_with_neither_spec_nor_command_is_refused(tmp_path) -> None:
    with pytest.raises(ConfigError, match="needs `spec`"):
        load(write(tmp_path, "rest-api:\n  nothing: {}\n"))


def test_a_name_in_both_sections_is_refused(tmp_path) -> None:
    """Both would become `{name}__{tool}` in the model's list, so which won
    would be invisible at every point a person could look."""
    text = (
        "mcp:\n  twice:\n    command: npx\n"
        "rest-api:\n  twice:\n    spec: https://example.test/openapi.json\n"
    )
    with pytest.raises(ConfigError, match="collide"):
        load(write(tmp_path, text))


def test_a_disabled_entry_is_configured_but_not_connected(tmp_path) -> None:
    """`enabled: false` is read here and *acted on* by the plugin that holds it.

    There is no accessor that filters them any more, and that is the point: what
    a switched-off entry means is a question for the family it was written in —
    it declares the source without rows and the hub marks it `disabled`, which is
    what keeps the row in the catalogue saying "there is a tool for this and
    somebody turned it off".
    """
    config = load(write(tmp_path, MCP))
    assert "off" in config.mcp, "still in the file, and still readable"
    assert config.mcp["off"].enabled is False


def test_the_hub_is_a_plugin_the_config_knows(tmp_path) -> None:
    assert default_config().server("toolhub").port == 8020
    with pytest.raises(ConfigError, match="not a server this system runs"):
        load(write(tmp_path, "servers:\n  nonsense: {port: 9}\n"))


# --- cli: commands already on this machine ------------------------------------
#
# The third source of tools, and the only one with no connection in it: a
# `cli:` entry names a program that is already installed.  Nothing serves these
# yet (DESIGN.md §9) — what these tests hold is the shape the tools will read.

CLI = """
cli:
  yt-dlp:
    command: yt-dlp
    description: Download video.
    install: uv pip install yt-dlp
    source:
      type: pypi
      version: 2026.1
  harness:
    command: python -m harness
    description: More than one word is one command.
  off:
    command: slow-thing
    enabled: false
"""


def test_a_cli_entry_is_a_command_and_what_it_is_for(tmp_path) -> None:
    tool = load(write(tmp_path, CLI)).cli["yt-dlp"]
    assert tool.command == "yt-dlp"
    assert tool.description == "Download video."
    assert tool.install == "uv pip install yt-dlp"


def test_a_cli_command_may_be_more_than_one_word(tmp_path) -> None:
    """v1 allowed `python -m mytool`, and a port that narrowed it to a single
    binary would refuse an entry the file it was copied from accepted."""
    assert load(write(tmp_path, CLI)).cli["harness"].command == "python -m harness"


def test_a_cli_source_is_notes_rather_than_secrets(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`${VAR}` is not resolved here, and a bare version is a string.

    Nothing reads `source`, so resolving a reference in it would invent a
    lookup the operator did not write — and a version written bare is a YAML
    float more often than anybody expects.
    """
    monkeypatch.setenv("SLIFE2_TEST_KEY", "resolved")
    text = "cli:\n  gh:\n    command: gh\n    source: {version: 1, url: '${SLIFE2_TEST_KEY}'}\n"
    source = load(write(tmp_path, text)).cli["gh"].source
    assert source == {"version": "1", "url": "${SLIFE2_TEST_KEY}"}


def test_a_cli_entry_with_no_command_is_refused(tmp_path) -> None:
    """Not a disabled entry — one that can never work, said once at load rather
    than at every call."""
    with pytest.raises(ConfigError, match="cli.nothing: needs `command`"):
        load(write(tmp_path, "cli:\n  nothing:\n    description: does nothing\n"))


def test_a_disabled_cli_entry_is_written_down_but_not_run(tmp_path) -> None:
    """The switch is the plugin's to act on, not the config's to filter.

    A switched-off entry is declared like any other and its rows carry
    `disabled`, so "somebody turned this off" is a thing a search can say rather
    than a silence — which is why there is no filtered view here.
    """
    config = load(write(tmp_path, CLI))
    assert list(config.cli) == ["yt-dlp", "harness", "off"]
    assert config.cli["off"].enabled is False
    assert config.cli["yt-dlp"].enabled is True


# --- skills: what a playbook is given -----------------------------------------
#
# A skill is a document, so this is not configuration for a process — it is the
# credential a skill's own script will need, resolved here because a daemon
# started days ago never saw the key somebody exported this morning.

SKILLS = """
skills:
  baidu-search:
    env:
      BAIDU_API_KEY: ${SLIFE2_TEST_KEY:-unset}
  quiet: {}
"""


def test_a_skills_entry_resolves_through_the_same_chain(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLIFE2_TEST_KEY", "resolved")
    config = load(write(tmp_path, SKILLS))
    assert config.skills["baidu-search"].env["BAIDU_API_KEY"] == "resolved"


def test_a_skills_entry_with_nothing_to_supply_is_fine(tmp_path) -> None:
    """The ambient environment is a real answer, and a playbook that needs
    nothing is the common case."""
    config = load(write(tmp_path, SKILLS))
    assert config.skills["quiet"].env == {}


def test_an_unresolved_skill_secret_stays_a_reference(tmp_path) -> None:
    """Which is what `slife2.plugins.skills.playbooks` reads to tell "configured" from "named".

    The load does not fail — a missing key is not a config error — so the fact
    has to survive to where somebody can act on it.
    """
    config = load(write(tmp_path, SKILLS))
    assert config.skills["baidu-search"].env["BAIDU_API_KEY"] == "unset"


def test_no_skills_section_is_no_skills(tmp_path) -> None:
    assert load(write(tmp_path, A_PROVIDER)).skills == {}
    assert default_config().skills == {}


def test_a_skills_entry_that_is_not_a_mapping_is_refused(tmp_path) -> None:
    """**A value that cannot be read is not a value that is absent.**

    `skills.<name>: just-a-name` was read as an empty, enabled entry, so the
    operator's key was dropped with nothing said and the model later ran a
    playbook that died on the credential this section exists to supply.  Every
    other entry-holding section refuses this shape through `_mapping`
    (`_cli_tools`, `_tool_server`), and this one now does too.
    """
    with pytest.raises(ConfigError, match=r"skills\.baidu-search: expected a mapping"):
        load(write(tmp_path, "skills:\n  baidu-search: just-a-name\n"))


def test_no_cli_section_is_no_cli_entries(tmp_path) -> None:
    """Absent means empty, like every other section: a config written before
    this existed still loads, and it ships with no external command at all."""
    config = load(write(tmp_path, A_PROVIDER))
    assert config.cli == {}
    assert default_config().cli == {}


# --- the switch, in all four sections that have one ---------------------------
#
# `_enabled` is one rule written once for four readers — `_tool_server`,
# `_rest_api`, `_cli_tool` and `_skills` — and the four are checked together
# because four separate tests is how four copies of one rule drift apart.  The
# answers are the three the rule has: a real `false` is off, absent is on, and
# anything else is refused.

#: section name in the file, the shortest valid entry under it, and where those
#: entries come back on `Config`.
ENTRY_SECTIONS = (
    ("mcp", "    command: npx\n", "mcp"),
    ("rest-api", "    spec: https://example.test/openapi.json\n", "rest_apis"),
    ("cli", "    command: gh\n", "cli"),
    ("skills", "    env: {A: b}\n", "skills"),
)


@pytest.mark.parametrize(("section", "entry", "attr"), ENTRY_SECTIONS)
def test_the_switch_is_a_boolean_or_it_is_refused(
    tmp_path, section: str, entry: str, attr: str
) -> None:
    """**`enabled: no` is the string `no`, and `bool("no")` is true.**

    ruamel loads YAML 1.2, where `no`, `off` and a quoted `"false"` are strings
    rather than the booleans PyYAML would have made of them — so
    `bool(raw.get("enabled", True))` turned every one of them *on*, and
    `family.py` then dialled an entry the operator had switched off at every
    start (`settings.enabled` is the whole of what decides whether a link is
    made).  Handing the string to `bool` was the bug and coercing it to `False`
    would be the same bug one spelling over: a value that cannot be read is not
    an absent one, and a switch decides whether a process is dialled, so an
    unreadable one is refused where every other unreadable field is — at load,
    naming the entry and the key.

    One case per section, because `_enabled` is one rule with four readers
    (`_tool_server`, `_rest_api`, `_cli_tool`, `_skills`) and four separate
    tests is how four copies of one rule drift apart.
    """

    def switch(text: str) -> bool:
        return getattr(load(write(tmp_path, text)), attr)["one"].enabled

    assert switch(f"{section}:\n  one:\n{entry}") is True, (
        "absent means on — the key the file never writes"
    )
    assert switch(f"{section}:\n  one:\n{entry}    enabled: false\n") is False

    # Everything a person writes when they mean off in another language or
    # another YAML draft: the word, the alias, and the quoted boolean.
    for written in ("no", "off", "'false'", "1", ""):
        text = f"{section}:\n  one:\n{entry}    enabled: {written}\n"
        with pytest.raises(ConfigError) as raised:
            load(write(tmp_path, text))
        assert f"{section}.one.enabled" in str(raised.value)
        assert "expected `true` or `false`" in str(raised.value)


# --- embeddings: which endpoint answers ---------------------------------------
#
# The table is a list of endpoints, and `active_model` picks one.  A name the
# file does not define is a mistake, not a vote for the first endpoint — a
# substitution there changes where every vector comes from and, with it, the
# index's recorded identity.

EMBEDDINGS_TWO = """
embeddings:
  providers:
    first:
      base_url: http://127.0.0.1:17347/v1
      model: m-one
    second:
      base_url: http://127.0.0.1:17348/v1
      model: m-two
"""


def test_an_unknown_active_model_is_refused_rather_than_substituted(tmp_path) -> None:
    """**A name the config does not define is not the first name it does.**

    A stale `active_model` used to fall back to the first provider, so vectors
    came from an endpoint nobody chose and the index's identity changed
    underneath with nothing in any file to say why.  The refusal names both the
    missing model and the ones that exist.
    """
    path = write(tmp_path, EMBEDDINGS_TWO + "  active_model: gone\n")
    with pytest.raises(ConfigError, match="gone") as caught:
        load(path)
    assert "first" in str(caught.value) and "second" in str(caught.value)


def test_no_active_model_still_takes_the_first(tmp_path) -> None:
    """The designed default, not a substitution: naming none is not naming a
    name that does not exist."""
    config = load(write(tmp_path, EMBEDDINGS_TWO))
    assert config.embeddings.active == "first"
    assert config.embeddings.active_provider().model == "m-one"


def test_a_code_built_settings_with_an_unknown_active_is_refused() -> None:
    """The same rule for a `Config` assembled in code rather than read: an
    `active` nobody defined must not quietly select `next(iter(...))`."""
    from slife2.libs.host.config import EmbeddingProviderSettings, EmbeddingsSettings

    settings = EmbeddingsSettings(
        providers={
            "one": EmbeddingProviderSettings(
                name="one", base_url="http://127.0.0.1:1/v1", model="m"
            )
        },
        active="gone",
    )
    with pytest.raises(ConfigError, match="gone"):
        settings.active_provider()


# --- media: the registered fall-through ---------------------------------------

MEDIA_STALE_DEFAULT = """
media:
  defaults:
    image: test/gone
  providers:
    test:
      api: openai-compat
      base_url: https://example.test/v1
      models:
        - model: img
          kind: image
"""


def test_a_stale_media_default_falls_through_to_the_first_of_the_kind(
    tmp_path,
) -> None:
    """The one registered step past v1's four: a `defaults[kind]` that no longer
    resolves is a *weaker* statement than the caller's, so the first model of the
    kind answers rather than the call stopping — and the answer's own
    `provider/model` is what makes the substitution visible (`DESIGN.md` §10.2).
    """
    provider, model = load(write(tmp_path, MEDIA_STALE_DEFAULT)).media.resolve("image")
    assert (provider.name, model.model) == ("test", "img")


# --- the config as a file that can change ------------------------------------


def test_load_cached_answers_the_same_object_until_the_file_moves(tmp_path) -> None:
    """**Identity is the staleness test, and that is the whole of the mechanism.**

    A process that has to follow an edit asks "did it change" with one `is`
    comparison rather than a watcher or a version counter, and what answers it
    is the file's `mtime` and size — which `configfile` moves on every real
    write, because it swaps atomically and never writes identical bytes.
    """
    path = write(tmp_path, A_PROVIDER)

    first = load_cached(str(path))
    assert load_cached(str(path)) is first, "untouched: the very same object"
    assert first.default == "local/small"

    path.write_text(A_PROVIDER.replace("local/small", "local/big"), encoding="utf-8")
    second = load_cached(str(path))
    assert second is not first, "an edit is a new object"
    assert second.default == "local/big"


def test_load_cached_does_not_answer_one_file_for_another(tmp_path) -> None:
    first = write(tmp_path, A_PROVIDER)
    second = tmp_path / "elsewhere" / "slife2.yaml"
    second.parent.mkdir()
    second.write_text(A_PROVIDER.replace("local/small", "local/big"), encoding="utf-8")

    assert load_cached(str(first)).default == "local/small"
    assert load_cached(str(second)).default == "local/big"


def test_load_cached_with_no_file_is_the_defaults() -> None:
    assert load_cached().default == default_config().default


def test_two_homes_keep_two_cached_configs(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A shared plugin reads the config of the home it is serving.**

    `find_config_path` resolves in the caller's home, so one process serving
    two data directories reads two files — and the cache has to hold both.  One
    slot would answer each home with the other's config on every alternation,
    and worse than wastefully: a reader's whole staleness test is `config is not
    self._config`, so a config that flips per call reads as a file being edited
    between turns — the provider table, its cached SDK clients and the model a
    new conversation starts on, rebuilt every time.
    """
    from slife2.libs.host.paths import in_home

    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    for root, model in ((mine, "local/small"), (theirs, "local/big")):
        root.mkdir()
        (root / "slife2.yaml").write_text(
            A_PROVIDER.replace("local/small", model), encoding="utf-8"
        )
    monkeypatch.setenv("SLIFE2_DATA_DIR", str(mine))

    with in_home(theirs):
        assert load_cached().default == "local/big"
    assert load_cached().default == "local/small"
    with in_home(theirs):
        assert load_cached().default == "local/big"
    # ...and each home's file is still cached on its own contents, so an
    # alternation is not an edit.
    first = load_cached()
    for _ in range(3):
        with in_home(theirs):
            load_cached()
    assert load_cached() is first


def test_a_stat_failure_is_not_read_as_a_fresh_load(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A file that cannot be read is not a file that changed.**

    `path.stat()` failing — a permissions error, a racing delete — was caught
    and turned into an unrelated `load`, which handed a *fresh* `Config` to a
    caller whose whole staleness test is object identity: the edit it was asking
    about would look like it never happened, with nothing reported.
    """
    path = write(tmp_path, A_PROVIDER)
    monkeypatch.setattr(slife2.libs.host.config, "_latest", None)

    real_is_file = Path.is_file
    real_stat = Path.stat

    def fake_is_file(self: Path) -> bool:
        return True if self == path else real_is_file(self)

    def fake_stat(self: Path, *args, **kwargs):
        if self == path:
            raise PermissionError("denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(ConfigError, match="cannot read"):
        load_cached(str(path))


def test_a_default_of_the_wrong_kind_is_refused_rather_than_replaced(tmp_path) -> None:
    """**The fallback was broader than its registration.**

    `MediaSettings.resolve` falls through to the first model of the kind when the
    operator's `defaults:` entry has gone *stale* — a registered fallback, scoped
    to "the name no longer resolves".  A name that resolves to another kind is
    not stale, it is wrong, and the docstring says it is "refused here rather
    than at the provider, where it costs money" — but one `except ConfigError`
    swallowed both, so the operator's stated default was discarded in silence and
    a paid call went to a model named in no `defaults:` line.
    """
    path = tmp_path / "slife2.yaml"
    path.write_text(
        """
media:
  providers:
    p:
      api: openai-images
      base_url: http://127.0.0.1:1/v1
      api_key: k
      models:
        - {model: a-image, kind: image}
        - {model: a-tts, kind: tts}
  defaults:
    image: p/a-tts
""",
        encoding="utf-8",
    )
    config = load(path)
    with pytest.raises(ConfigError, match="is a tts model, not image"):
        config.media.resolve("image")
    # And a name that no longer resolves still falls back, which is the half
    # the registration is for.
    path.write_text(
        """
media:
  providers:
    p:
      api: openai-images
      base_url: http://127.0.0.1:1/v1
      api_key: k
      models:
        - {model: a-image, kind: image}
  defaults:
    image: p/gone
""",
        encoding="utf-8",
    )
    _, entry = load(path).media.resolve("image")
    assert entry.model == "a-image"


def test_a_named_config_that_is_not_there_is_not_the_defaults(tmp_path) -> None:
    """**`load_cached` answered a typo with the built-in config.**

    Its cache slot is the "this installation has no config at all" state and is
    keyed on nothing but `None`, so once a config-less start had filled it,
    `load_cached("<any path>")` returned that object while
    `load("<any path>")` raised `config file not found`.  Today's two callers
    pass no argument, so it was latent; a caller asking about a named file is
    asking about *that* file.
    """
    missing = tmp_path / "typo.yaml"
    with pytest.raises(ConfigError, match="config file not found"):
        load_cached(missing)


# --- the broker on the command line ------------------------------------------

#: A config that is on the peer network: the `peer_agent:` section is what
#: being on one means.
A_MESH = (
    A_PROVIDER
    + """
peer_agent:
  broker:
    host: localhost
    port: 1883
  org: an-org
  unit: a-unit
"""
)


def test_the_broker_flag_overrides_the_address_and_nothing_else(
    tmp_path, monkeypatch
) -> None:
    """`--a2a-broker` wins over the file it is an override of — over its address.

    Which network this is stays the section's: `org` and `unit` name the topic
    namespace, and the flag names a broker to join rather than a mesh to be on.
    """
    monkeypatch.setenv(A2A_BROKER_ENV_VAR, "broker.example:2883")
    config = load(write(tmp_path, A_MESH))

    assert config.peer_agent.address == "broker.example:2883"
    assert config.peer_agent.overridden
    assert (config.peer_agent.org, config.peer_agent.unit) == ("an-org", "a-unit")


A_MESH_AND_LOCAL = (
    A_MESH
    + """
subagent:
  broker:
    host: localhost
    port: 1884
  org: a-local-org
"""
)


def test_the_broker_flag_leaves_the_local_network_alone(tmp_path, monkeypatch) -> None:
    """**The flag names somebody else's broker, and a subagent has no business
    on one.**

    `--a2a-broker` is an override of the *remote* section only
    (`Net`/`_a2a`'s `override`), and the local network's broker is this
    machine's own and the file's to say.  When the flag reached both, the local
    mesh was dialled at the remote address under its per-maker namespace — and
    since `slife2.plugins.a2a.broker.ensure` never starts a broker that is not
    this machine's, the local one stopped being brought up at all.
    """
    monkeypatch.setenv(A2A_BROKER_ENV_VAR, "somebody-elses.example:2883")
    config = load(write(tmp_path, A_MESH_AND_LOCAL))

    assert config.peer_agent.address == "somebody-elses.example:2883"
    assert config.peer_agent.overridden
    assert config.subagent.network.address == "localhost:1884"
    assert not config.subagent.network.overridden


def test_without_the_flag_the_file_is_the_answer(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(A2A_BROKER_ENV_VAR, raising=False)
    config = load(write(tmp_path, A_MESH))

    assert config.peer_agent.address == "localhost:1883"
    assert not config.peer_agent.overridden


def test_the_section_named_a2a_is_refused_rather_than_ignored(tmp_path) -> None:
    """**A config written before the rename must not silently lose the network.**

    `a2a:` is `peer_agent:` now: the section is named for what it configures —
    an agent's peer agents — and `a2a` is the plugin that serves it.  An unknown
    top-level key is normally ignored, which here would load with the network
    off and nothing said, so the old name is refused by name with the new one in
    the sentence.
    """
    text = (
        A_PROVIDER
        + """
a2a:
  provider: mosquitto
"""
    )
    with pytest.raises(ConfigError, match="peer_agent"):
        load(write(tmp_path, text))


#: A remote section that says nothing about where to meet, and a subagent
#: network that does — the two sections' addresses differ in the *namespace* and
#: not in the machine.
A_MESH_WITHOUT_A_BROKER = (
    A_PROVIDER
    + """
peer_agent:
  org: an-org
  unit: a-unit

subagent:
  broker:
    host: 10.0.0.5
    port: 2883
"""
)


def test_a_remote_mesh_with_no_broker_meets_where_the_subagent_network_does(
    tmp_path, monkeypatch
) -> None:
    """**One machine, one broker by default, and `subagent:` is where it is said.**

    The subagent network is this machine's own, so the file has to say where it
    is; a remote mesh that names no broker meets *there* rather than at the
    built-in address, because the two networks are two namespaces on one address
    and a file made to write the same `host:port` twice could write two different
    ones by accident.  Only the address is inherited — the topic namespace stays
    each network's own, which is what keeps two subagents named `researcher`
    apart from each other and from a remote peer.
    """
    monkeypatch.delenv(A2A_BROKER_ENV_VAR, raising=False)
    config = load(write(tmp_path, A_MESH_WITHOUT_A_BROKER))

    assert config.subagent.network.address == "10.0.0.5:2883"
    assert config.peer_agent.address == "10.0.0.5:2883"
    assert (config.peer_agent.org, config.peer_agent.unit) == ("an-org", "a-unit")


def test_a_subagent_network_is_one_mesh_per_agent(tmp_path, monkeypatch) -> None:
    """**A mesh is `{org}/{unit}`, and on this side that pair is the agent's.**

    The unit is the agent's own name and the org is this network's word
    (`SUBAGENT_ORG`), so an agent and its subagents are one mesh and two agents
    are two — which is what the file cannot be trusted to say: a unit that was
    anything but the agent's name would put two agents' subagents in one MQTT
    identity, and an org left at the remote network's would put the two networks
    in one mesh.
    """
    monkeypatch.delenv(A2A_BROKER_ENV_VAR, raising=False)
    config = load(write(tmp_path, A_MESH_AND_LOCAL))

    local = net_settings(config, LOCAL)
    assert LOCAL.namespace(local, "jack") == ("subagent", "jack")
    assert LOCAL.namespace(local, "jill") == ("subagent", "jill")
    # The org may be overridden to group meshes; the unit is the agent's, so what
    # an operator wrote there is refused rather than ignored.
    assert local.org == "a-local-org"
    with pytest.raises(ConfigError, match="not a setting"):
        load(write(tmp_path, A_MESH_AND_LOCAL + "  unit: mine\n"))
    # The remote network still reads both, from its own section.
    remote = net_settings(config, REMOTE)
    assert REMOTE.namespace(remote, "jack") == ("an-org", "a-unit")


def test_the_default_org_says_which_network_it_is(tmp_path, monkeypatch) -> None:
    """Left out, the org is `subagent` and not `default`.

    `default` is what the remote section says when a file does not bother, so a
    subagent network left at it would be the *same* mesh the moment an agent's
    name matched a peer's unit — the two networks are allowed to share one
    broker, and this word is the whole of what keeps their meshes apart.
    """
    monkeypatch.delenv(A2A_BROKER_ENV_VAR, raising=False)
    config = load(
        write(
            tmp_path,
            A_PROVIDER
            + """
subagent:
  broker:
    host: localhost
    port: 1884
""",
        )
    )

    assert net_settings(config, LOCAL).org == "subagent"
    assert LOCAL.namespace(net_settings(config, LOCAL), "jack") == (
        "subagent",
        "jack",
    )


def test_a_broker_the_remote_section_names_is_its_own(tmp_path, monkeypatch) -> None:
    """The subagent network's address is a *default*, not a rule.

    Two networks may meet at two addresses — a team broker for the remote peers
    and this machine's own for its subagents (`subagent:` says pointing it
    elsewhere is supported rather than encouraged) — and the file that names the
    remote one keeps it.
    """
    monkeypatch.delenv(A2A_BROKER_ENV_VAR, raising=False)
    config = load(write(tmp_path, A_MESH_AND_LOCAL))

    assert config.peer_agent.address == "localhost:1883"
    assert config.subagent.network.address == "localhost:1884"


def test_the_broker_flag_cannot_turn_the_mesh_on(tmp_path, monkeypatch) -> None:
    """The section's presence is the switch, and the flag is not a second one.

    A machine with no `peer_agent:` is a machine that is not on that network —
    that is the
    design's whole off switch (`A2ASettings`) — so a flag naming a broker is
    refused by name rather than answered with a mesh the file never described.
    """
    monkeypatch.setenv(A2A_BROKER_ENV_VAR, "broker.example:2883")

    with pytest.raises(ConfigError, match="not on the peer network"):
        load(write(tmp_path, A_PROVIDER))


@pytest.mark.parametrize(
    "written",
    ["nonsense", "host:notaport", ":1883", "host:0", "host:70000"],
)
def test_a_broker_address_that_is_not_one_is_refused(
    tmp_path, monkeypatch, written: str
) -> None:
    """Refused where it is written, naming the flag — not dialled and found out.

    A port the operating system cannot have is a config mistake, and the failure
    it would otherwise produce is a connect that hangs and says nothing.
    """
    monkeypatch.setenv(A2A_BROKER_ENV_VAR, written)

    with pytest.raises(ConfigError, match="a2a-broker"):
        load(write(tmp_path, A_MESH))


def test_a_broker_port_in_the_file_is_refused_like_the_one_on_the_flag(
    tmp_path, monkeypatch
) -> None:
    """**`--a2a-broker host:0` was refused and `broker: {port: 0}` was not.**

    The flag's port goes through `broker_address`, which insists on 1-65535, and
    the section the flag is an override *of* is written in the same shape — but
    its `port:` was read straight through, so `port: 0` loaded and the mesh
    dialled `localhost:0` at every start.  That is a port no process can hold
    (nothing binds this end of a broker connection), so the dial hangs until its
    timeout instead of failing on the line that named it — the same failure the
    flag's own refusal exists to prevent.  Both networks are checked because
    `_a2a` parses both.
    """
    monkeypatch.delenv(A2A_BROKER_ENV_VAR, raising=False)

    for section in ("peer_agent", "subagent"):
        for written in ("0", "70000"):
            text = f"{section}:\n  broker:\n    host: localhost\n    port: {written}\n"
            with pytest.raises(ConfigError) as raised:
                load(write(tmp_path, text))
            assert f"{section}.broker.port" in str(raised.value)
            assert "is not a port" in str(raised.value)


def test_an_ipv6_broker_address_is_split_from_the_right(tmp_path, monkeypatch) -> None:
    """`:1883` is the port even when the host is full of colons.

    Splitting from the left would read `::1:1883` as a host called `::1:1883`,
    which is not a host — and `::1` is one of the three addresses this machine
    answers to (`slife2.plugins.a2a.broker.LOCAL_HOSTS`).
    """
    monkeypatch.setenv(A2A_BROKER_ENV_VAR, "::1:2883")

    assert load(write(tmp_path, A_MESH)).peer_agent.address == "::1:2883"
