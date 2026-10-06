"""Config loading and secret resolution.

No marker: these are `unit` by the default run, and the point of the file is
that they touch the filesystem only through `tmp_path`.
"""

from __future__ import annotations

import pytest

from slife2.config import (
    CONFIG_ENV_VAR,
    ConfigError,
    default_config,
    find_config_path,
    load,
    resolve_secret,
)


def test_defaults_point_each_process_at_its_own_port() -> None:
    """The four processes must not collide on a port out of the box."""
    cfg = default_config()
    ports = {
        cfg.agent.server.port,
        cfg.llm_openai.server.port,
        cfg.llm_anthropic.server.port,
    }
    assert len(ports) == 3


def test_tui_url_follows_the_agent_server() -> None:
    """The TUI's URL is derived, never independently written.

    A second hard-coded address is the one that silently disagrees after a port
    change, so this asserts the derivation rather than the value.
    """
    cfg = default_config()
    assert cfg.tui_url == cfg.agent.server.url


def test_default_provider_resolves() -> None:
    cfg = default_config()
    provider = cfg.agent.provider()
    assert provider.model
    assert provider.url.startswith("http")


def test_unknown_provider_names_the_known_ones() -> None:
    cfg = default_config()
    with pytest.raises(ConfigError, match="deepseek"):
        cfg.agent.provider("nope")


def test_missing_unnamed_config_is_not_an_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No file anywhere means the defaults *are* the config."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    assert find_config_path() is None
    assert load() == default_config()


def test_missing_named_config_is_an_error(tmp_path) -> None:
    """Asking for a file that is not there must not silently use defaults."""
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "absent.yaml")


def test_env_var_names_a_config_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text("agent:\n  max_steps: 3\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(path))
    assert load().agent.max_steps == 3


def test_explicit_path_beats_the_env_var(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / "env.yaml"
    env_path.write_text("agent:\n  max_steps: 1\n", encoding="utf-8")
    arg_path = tmp_path / "arg.yaml"
    arg_path.write_text("agent:\n  max_steps: 2\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(env_path))
    assert load(arg_path).agent.max_steps == 2


def test_working_directory_config_is_found(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "slife2.yaml").write_text("agent:\n  max_steps: 7\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    assert load().agent.max_steps == 7


def test_malformed_yaml_is_fatal(tmp_path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("agent: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(path)


def test_non_mapping_top_level_is_fatal(tmp_path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load(path)


def test_partial_file_keeps_the_other_defaults(tmp_path) -> None:
    """A file that sets one thing must not blank everything else."""
    path = tmp_path / "partial.yaml"
    path.write_text("agent:\n  max_steps: 5\n", encoding="utf-8")
    cfg = load(path)
    assert cfg.agent.max_steps == 5
    assert cfg.llm_openai.server.port == default_config().llm_openai.server.port
    assert cfg.agent.providers  # the provider table survived


def test_provider_table_replaces_the_default(tmp_path) -> None:
    path = tmp_path / "providers.yaml"
    path.write_text(
        "agent:\n"
        "  default: local\n"
        "  providers:\n"
        "    local: {url: 'http://127.0.0.1:9999/mcp', model: llama3}\n",
        encoding="utf-8",
    )
    cfg = load(path)
    assert list(cfg.agent.providers) == ["local"]
    assert cfg.agent.provider().model == "llama3"


# --- secret resolution -------------------------------------------------------


def test_plaintext_passes_through() -> None:
    assert resolve_secret("sk-literal") == "sk-literal"


def test_env_var_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLIFE2_TEST_KEY", "from-env")
    assert resolve_secret("${SLIFE2_TEST_KEY}") == "from-env"


def test_default_is_used_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLIFE2_TEST_KEY", raising=False)
    assert resolve_secret("${SLIFE2_TEST_KEY:-fallback}") == "fallback"


def test_unset_without_default_stays_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leniency is the contract: the failure lands where the value is *used*.

    Raising here would take down a process that may never read this key, and the
    error at the API call names the variable anyway.
    """
    monkeypatch.delenv("SLIFE2_TEST_KEY", raising=False)
    assert resolve_secret("${SLIFE2_TEST_KEY}") == "${SLIFE2_TEST_KEY}"


def test_reference_embedded_in_a_larger_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLIFE2_TEST_HOST", "example.test")
    assert resolve_secret("https://${SLIFE2_TEST_HOST}/v1") == "https://example.test/v1"


def test_non_string_scalars_become_strings() -> None:
    """YAML hands back ints for unquoted scalars; a numeric key is still a key."""
    assert resolve_secret(12345) == "12345"


def test_braces_that_are_not_references_are_untouched() -> None:
    """A prompt containing `${...}` must not be treated as a lookup."""
    assert resolve_secret("use ${1+1} here") == "use ${1+1} here"


def test_api_key_is_resolved_lazily(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw reference is what the config holds; the key is read on demand.

    This is what keeps the TUI process — which never needs a provider key — from
    opening the OS keyring at startup.
    """
    path = tmp_path / "lazy.yaml"
    path.write_text("llm_openai:\n  api_key: ${SLIFE2_TEST_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("SLIFE2_TEST_KEY", "resolved-later")
    cfg = load(path)
    assert cfg.llm_openai.api_key_ref == "${SLIFE2_TEST_KEY}"
    assert cfg.llm_openai.api_key == "resolved-later"
