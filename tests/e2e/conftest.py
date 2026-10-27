"""The e2e lane's own watchdog, and why it is not the fast lane's.

`faulthandler_timeout` (180 s, `pyproject.toml`) is sized for the fast lane,
where the slowest test is ~7 s.  This lane is a different shape: every test's
fixture starts nineteen daemons, and the launcher's own readiness budget for
*one* of them is `timeouts.ready.plugin_start` — 300 s.  Under the fast lane's
bound a server that genuinely never comes up is killed by the watchdog before
`_wait_ready` can answer with its name and log tail, and the worker is reported
as "crashed while running …", which names no server at all.  So the lane
replaces the timer with the launcher's budget plus a margin, and a real start
failure arrives as the diagnosis it already is.

**A hook rather than `-o` on the command line**, because the lane is entered
from several places (`pyproject.toml`'s comment, both READMEs, and CI's step)
and a number repeated in each is a number one of them will lose.
`pytest_runtest_setup` is where the timer can be replaced: pytest's faulthandler
plugin arms the global one around the whole protocol, before the fixture setup
this lane is about, and the first test hook to run is this one.
"""

from __future__ import annotations

import faulthandler

import pytest
from _pytest.faulthandler import (
    fault_handler_stderr_fd_key,
    get_exit_on_timeout_config_value,
)

import slife2.helpers.timeouts as _T

#: Two minutes for the teardown that follows a start that failed.  A magnitude
#: of the kind this suite's timeout gate leaves to tests — "long enough" rather
#: than a daemon's budget, which is the registry's and is read at call time.
E2E_MARGIN = 120.0


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Re-arm the watchdog above the launcher's own readiness budget."""
    # The file descriptor pytest's plugin armed the global timer with: the one
    # dup'd at configure, because a worker's streams may be wrapped by the time
    # a test runs and not be files `faulthandler` can write to.  Its absence
    # means the plugin is off (`-p no:faulthandler`), and then there is no
    # timer to replace and no finally to cancel one armed here.
    stderr = item.config.stash.get(fault_handler_stderr_fd_key, None)
    if stderr is None:
        return
    faulthandler.cancel_dump_traceback_later()
    faulthandler.dump_traceback_later(
        _T.timeouts.ready.plugin_start + E2E_MARGIN,
        file=stderr,
        exit=get_exit_on_timeout_config_value(item.config),
    )
