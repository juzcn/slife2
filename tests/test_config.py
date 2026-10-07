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
        MEMORY_SERVER_NAME,
        TOOLHUB_SERVER_NAME,
    )

    assert importlib.import_module("slife2.server.server").SERVER_NAME == (
        AGENT_SERVER_NAME
    )
    assert importlib.import_module("slife2.memory_server").SERVER_NAME == (
        MEMORY_SERVER_NAME
    )
    assert importlib.import_module("slife2.toolhub").SERVER_NAME == (
        TOOLHUB_SERVER_NAME
    )
    for api, module in API_BACKENDS.items():
        assert importlib.import_module(module).SERVER_NAME == API_SERVER_NAMES[api]


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
    models:
      - model: m
        compat: {store: false}
""",
    )
    assert load(path).provider("p").model("m").store is False


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
    """Nothing downstream knows that REST exists.

    The entry becomes an ordinary stdio upstream here, which is what lets the
    hub have one mechanism instead of two.
    """
    server = load(write(tmp_path, TOOLS)).tools["registry"]
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
    assert config.tools["mine"].args == ("--from", "my-proxy", "run")


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
    config = load(write(tmp_path, TOOLS))
    assert "off" in config.tools, "still in the file, and still readable"
    assert "off" not in [server.name for server in config.tool_servers()]


def test_the_hub_is_a_component_the_config_knows(tmp_path) -> None:
    assert default_config().server("toolhub").port == 8020
    with pytest.raises(ConfigError, match="not a server this system runs"):
        load(write(tmp_path, "servers:\n  nonsense: {port: 9}\n"))
