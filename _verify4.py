"""Run the a2a tests the `forget` work touches, without pytest."""

import asyncio, contextlib, inspect, logging, os, sys, traceback
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "tests"))

from fastmcp import Client as MCPClient  # noqa: E402
import plugins.a2a.test_a2a as t  # noqa: E402

WANTED = [
    "test_the_four_mesh_tools_are_the_models_and_nothing_else_is",
    "test_the_local_instance_serves_the_local_network",
    "test_forget_gives_up_a_members_place",
    "test_a_send_to_a_subagent_gives_it_its_place_before_publishing",
    "test_the_local_roster_lists_members_whose_cards_have_not_arrived",
    "test_the_remote_network_places_nothing_and_lists_only_cards",
]


class Caplog:
    def __init__(self):
        self.records = []

    def set_level(self, level, logger="slife2"):
        logging.getLogger(logger).setLevel(level)

    @contextlib.contextmanager
    def at_level(self, level, logger=None):
        found = logging.getLogger(logger)
        previous = found.level
        handler = logging.Handler()
        handler.emit = self.records.append
        found.addHandler(handler)
        found.setLevel(level)
        try:
            yield self
        finally:
            found.setLevel(previous)
            found.removeHandler(handler)


async def main() -> int:
    import pytest

    failures = 0
    for name in WANTED:
        test = getattr(t, name)
        root = Path(mkdtemp())
        os.environ["SLIFE2_DATA_DIR"] = str(root / "data")
        os.environ["SLIFE2_RUNTIME_DIR"] = str(root / "runtime")
        from slife2.tui.i18n import set_language

        set_language("en")
        home = Path(mkdtemp())
        window, agent, caplog = t.Window(), t.AgentServer(), Caplog()
        async with contextlib.AsyncExitStack() as stack:
            with pytest.MonkeyPatch.context() as mp:
                window.client = await stack.enter_async_context(MCPClient(window.mcp))
                agent.client = await stack.enter_async_context(MCPClient(agent.mcp))
                try:
                    wanted = set(inspect.signature(test).parameters)
                    available = {
                        "tmp_path": home,
                        "window": window,
                        "agent_server": agent,
                        "caplog": caplog,
                        "monkeypatch": mp,
                    }
                    await test(**{k: v for k, v in available.items() if k in wanted})
                    print(f"PASS {name}")
                except Exception as exc:
                    failures += 1
                    print(f"FAIL {name}: {exc!r}")
                    traceback.print_exc()
    print(f"\n{len(WANTED) - failures}/{len(WANTED)} passed")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if asyncio.run(main()) else 0)
