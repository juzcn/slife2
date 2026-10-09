"""Config loading: providers, models, and the resolution chain for secrets.

The shape is slife v1's, so the tests are about the parts that shape has to get
right — several models per provider, a `provider/model` reference that is
unambiguous, and parameters that are absent rather than defaulted.
"""

from __future__ import annotations

import pytest

from slife2.config import (
    API_BACKENDS,
    ConfigError,
    default_config,
    find_config_path,
    load,
    resolve_secret,
)
from slife2.paths import DATA_ENV_VAR

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
        assert module.startswith("slife2.llm."), api


def test_every_declared_server_name_is_the_one_its_module_uses() -> None:
    """The names here are what a client checks to prove it reached the right
    server, and they are spelled out rather than imported — the TUI must not
    pull in a server, and the agent loop must never import `openai_server`.

    Spelling them out is only safe if something notices when one drifts, and
    that is this: each claimed name against the `SERVER_NAME` the server itself
    passes to `FastMCP(...)`, which is what actually ends up on the wire.
    """
    import importlib

    from slife2.config import (
        AGENT_SERVER_NAME,
        API_SERVER_NAMES,
        BUILTINS_SERVER_NAME,
        CLI_SERVER_NAME,
        DB_SERVER_NAME,
        EMBEDDINGS_SERVER_NAME,
        MCP_TOOLS_SERVER_NAME,
        RESTAPI_TOOLS_SERVER_NAME,
        SKILLS_SERVER_NAME,
        TOOLHUB_SERVER_NAME,
    )

    assert importlib.import_module("slife2.server.server").SERVER_NAME == (
        AGENT_SERVER_NAME
    )
    assert importlib.import_module("slife2.db_server").SERVER_NAME == DB_SERVER_NAME
    assert importlib.import_module("slife2.builtins").SERVER_NAME == (
        BUILTINS_SERVER_NAME
    )
    assert importlib.import_module("slife2.skills_server").SERVER_NAME == (
        SKILLS_SERVER_NAME
    )
    assert importlib.import_module("slife2.cli_server").SERVER_NAME == CLI_SERVER_NAME
    assert importlib.import_module("slife2.mcp_tools").SERVER_NAME == (
        MCP_TOOLS_SERVER_NAME
    )
    assert importlib.import_module("slife2.restapi_tools").SERVER_NAME == (
        RESTAPI_TOOLS_SERVER_NAME
    )
    assert importlib.import_module("slife2.toolhub").SERVER_NAME == (
        TOOLHUB_SERVER_NAME
    )
    assert importlib.import_module("slife2.llm.embeddings_server").SERVER_NAME == (
        EMBEDDINGS_SERVER_NAME
    )
    for api, module in API_BACKENDS.items():
        assert importlib.import_module(module).SERVER_NAME == API_SERVER_NAMES[api]


def test_a_name_the_model_may_not_unload_is_spelled_on_both_sides() -> None:
    """`skill_use` is served by a plugin and refused by the hub.

    The name crosses a process boundary — `slife2-skills` serves the tool, and
    `slife2.toolhub` keeps it on the list the model cannot unload, because the
    playbooks are what every session is meant to reach for — so it is written
    twice.  This is what notices when one side is renamed and the other is not,
    and the failure it prevents is silent in both directions: a hub spelling a
    name nothing serves protects nothing, and a server renamed out from under
    the hub hands the model a tool it can throw away.
    """
    from slife2 import skills
    from slife2.skills_server import CONFIG_KEY, SOURCE
    from slife2.toolhub import ALWAYS_LOADED, SKILL_USE

    assert SKILL_USE == skills.USE_TOOL
    assert SKILL_USE in ALWAYS_LOADED

    # And the two ids a declared row must keep apart: the source is what the
    # catalogue files the rows under and the key is what the launcher starts.
    assert SOURCE != CONFIG_KEY


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

    Three settings were affected and each fails differently: a threshold of
    zero is the configuration the guard exists to refuse, and was silently
    replaced by the default instead; `max_steps: 0` is a turn with no model
    call in it, and became sixteen; and `port: 0` is a server bound somewhere
    the launcher will never look for it.
    """
    path = write(tmp_path, "tool_load:\n  threshold: 0\n")
    with pytest.raises(ConfigError, match="would evict every tool"):
        load(path)

    path = write(tmp_path, "tool_load:\n  threshold: -5\n")
    with pytest.raises(ConfigError, match="would evict every tool"):
        load(path)

    path = write(tmp_path, "agent:\n  max_steps: 0\n")
    assert load(path).agent.max_steps == 0, "a written zero is a written zero"

    path = write(tmp_path, "servers:\n  db:\n    port: 0\n")
    with pytest.raises(ConfigError, match="is not a port"):
        load(path)


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


def test_a_data_dir_without_a_config_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(DATA_ENV_VAR, str(tmp_path))
    assert find_config_path() is None
    assert load() == default_config()


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
    config = load(write(tmp_path, "agent:\n  max_steps: 5\n"))
    assert config.agent.max_steps == 5
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
# `tools:` is other people's MCP servers and `rest-api:` is other people's REST
# APIs, and the second is expanded into the first at load time — so what these
# are about is the expansion, and the refusals that keep an ambiguous entry from
# becoming a silent precedence rule.

TOOLS = """
tools:
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
    server = load(write(tmp_path, TOOLS)).tools["filesystem"]
    assert (server.transport, server.command) == ("stdio", "npx")
    assert server.args == ("-y", "server-filesystem", ".")
    assert server.description == "Files on disk."


def test_an_entry_with_a_url_is_http(tmp_path) -> None:
    """Which field is set *is* the transport; there is no separate switch."""
    server = load(write(tmp_path, TOOLS)).tools["arxiv"]
    assert server.transport == "http"
    assert server.url == "https://example.test/mcp"


def test_a_tool_servers_secrets_go_through_the_same_chain(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLIFE2_TEST_KEY", "resolved")
    config = load(write(tmp_path, TOOLS))
    assert config.tools["serper"].env["SERPER_API_KEY"] == "resolved"
    assert config.tools["arxiv"].headers["Authorization"] == "Bearer resolved", (
        "a header is a credential too"
    )


def test_a_rest_api_is_expanded_into_the_proxy_that_serves_it(tmp_path) -> None:
    """The config layer is where a declarative entry becomes a runnable command.

    The entry becomes an ordinary stdio upstream here, so the plugin that holds
    it — `slife2-restapi-tools` — sees the same shape as one under `tools:`, and
    the hub sees a source like any other.  Two sections and one mechanism.
    """
    server = load(write(tmp_path, TOOLS)).rest_apis["registry"]
    assert server.kind == "rest"
    assert (server.transport, server.command) == ("stdio", "uvx")
    assert server.args == ("mcp-openapi-proxy",)
    assert server.env["OPENAPI_SPEC_URL"] == "https://example.test/openapi.json"
    assert server.env["SERVER_URL_OVERRIDE"] == "https://api.example.test"
    assert server.env["API_KEY"] == "unset"  # the ${VAR:-default} form


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
        load(write(tmp_path, "tools:\n  nothing:\n    args: [x]\n"))


def test_an_entry_with_two_transports_is_refused(tmp_path) -> None:
    """Not resolved by a precedence rule nobody would remember.

    This is not hypothetical: v1's own config grew a `url` on an entry that
    still had a `command`, and its rule was that the URL won.
    """
    text = "tools:\n  both:\n    command: uvx\n    url: https://example.test/mcp\n"
    with pytest.raises(ConfigError, match="both"):
        load(write(tmp_path, text))


def test_a_rest_api_with_neither_spec_nor_command_is_refused(tmp_path) -> None:
    with pytest.raises(ConfigError, match="needs `spec`"):
        load(write(tmp_path, "rest-api:\n  nothing: {}\n"))


def test_a_name_in_both_sections_is_refused(tmp_path) -> None:
    """Both would become `{name}__{tool}` in the model's list, so which won
    would be invisible at every point a person could look."""
    text = (
        "tools:\n  twice:\n    command: npx\n"
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
    config = load(write(tmp_path, TOOLS))
    assert "off" in config.tools, "still in the file, and still readable"
    assert config.tools["off"].enabled is False


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
    """Which is what `slife2.skills` reads to tell "configured" from "named".

    The load does not fail — a missing key is not a config error — so the fact
    has to survive to where somebody can act on it.
    """
    config = load(write(tmp_path, SKILLS))
    assert config.skills["baidu-search"].env["BAIDU_API_KEY"] == "unset"


def test_no_skills_section_is_no_skills(tmp_path) -> None:
    assert load(write(tmp_path, A_PROVIDER)).skills == {}
    assert default_config().skills == {}


def test_no_cli_section_is_no_cli_entries(tmp_path) -> None:
    """Absent means empty, like every other section: a config written before
    this existed still loads, and it ships with no external command at all."""
    config = load(write(tmp_path, A_PROVIDER))
    assert config.cli == {}
    assert default_config().cli == {}
