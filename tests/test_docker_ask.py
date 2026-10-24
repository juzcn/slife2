"""The one-shot driver the container image ships.

It is not part of the package — it is copied into the image and run by the
entrypoint — so these tests load it by path rather than importing it.  What is
pinned here is the one thing about it that must agree with the stack it dials:
the addresses.
"""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ASK = Path(__file__).resolve().parents[1] / "docker" / "ask.py"

pytestmark = pytest.mark.unit


def load_ask() -> ModuleType:
    """The driver as a module, without running its `main`."""
    spec = spec_from_file_location("slife2_bench_ask", ASK)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_driver_dials_the_addresses_in_the_config(monkeypatch) -> None:
    """**The config, not a compiled-in default.**

    `servers.tui` and `servers.toolhub` are configurable, and `wsl_port_offset`
    shifts every port on a mirrored network — so a driver that trusted the
    shipped defaults would dial an address the stack never bound, and the
    failure would read as "the stack never became ready".  The shipped image
    changes neither, which is why the bug only shows up on a config that does.
    """
    ask = load_ask()
    urls = {
        "tui": "http://127.0.0.1:25005/mcp",
        "toolhub": "http://127.0.0.1:25020/mcp",
    }
    monkeypatch.setattr(
        ask,
        "load_cached",
        lambda: SimpleNamespace(server=lambda name: SimpleNamespace(url=urls[name])),
    )
    monkeypatch.delenv("SLIFE2_BENCH_TUI_URL", raising=False)
    monkeypatch.delenv("SLIFE2_BENCH_TOOLHUB_URL", raising=False)

    assert ask.endpoints() == (urls["tui"], urls["toolhub"])


def test_the_environment_can_still_override_each_address(monkeypatch) -> None:
    """A caller that mapped the ports elsewhere is not second-guessed.

    The host-side adapter is that caller: it dials the container's ports at an
    address of its own choosing, and an override that the config silently won
    would hand the prompt to a window nobody is reading.
    """
    ask = load_ask()
    monkeypatch.setattr(
        ask,
        "load_cached",
        lambda: SimpleNamespace(
            server=lambda name: SimpleNamespace(
                url=f"http://127.0.0.1:1/from-config-{name}"
            )
        ),
    )
    monkeypatch.setenv("SLIFE2_BENCH_TUI_URL", "http://127.0.0.1:2/tui")
    monkeypatch.setenv("SLIFE2_BENCH_TOOLHUB_URL", "http://127.0.0.1:2/hub")

    assert ask.endpoints() == ("http://127.0.0.1:2/tui", "http://127.0.0.1:2/hub")
