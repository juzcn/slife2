"""Run the agent-side removal tests directly, without pytest."""

import asyncio, contextlib, inspect, os, sys, traceback
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "tests"))

from fastmcp import Client  # noqa: E402

WANTED = [
    "test_removing_a_subagent_tells_the_network_before_it_deletes",
    "test_a_machine_with_no_subagent_network_removes_without_telling_anyone",
    "test_removing_a_subagent_takes_its_directory_with_it",
    "test_removing_something_that_is_not_a_subagent_is_refused",
    "test_a_subagent_is_a_directory_inside_its_makers",
    "test_a_subagents_turns_are_recorded_in_its_own_store",
]


async def main() -> int:
    import pytest
    import plugins.agent.test_server as t
    from slife2.libs.host.config import default_config
    from slife2.plugins.context.server import build_server as build_context
    from slife2.plugins.toolhub.server import build_server as build_hub
    from tests.fakes import StubEmbedder, plugin_transports

    failures = 0
    for name in WANTED:
        test = getattr(t, name)
        root = Path(mkdtemp())
        os.environ["SLIFE2_DATA_DIR"] = str(root / "data")
        os.environ["SLIFE2_RUNTIME_DIR"] = str(root / "runtime")
        from slife2.tui.i18n import set_language

        set_language("en")
        async with contextlib.AsyncExitStack() as stack:
            with pytest.MonkeyPatch.context() as mp:
                context = await stack.enter_async_context(
                    Client(
                        build_context(
                            default_config(),
                            embedder=StubEmbedder(),
                            ask=t.keep_the_context,
                        )
                    )
                )
                hub = await stack.enter_async_context(
                    Client(
                        build_hub(
                            default_config(),
                            transports=plugin_transports(default_config()),
                            embedder=StubEmbedder(),
                        )
                    )
                )
                try:
                    wanted = set(inspect.signature(test).parameters)
                    available = {"context": context, "hub": hub, "monkeypatch": mp}
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
