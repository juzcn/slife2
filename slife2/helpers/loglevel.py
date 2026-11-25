"""`SLIFE2_LOG_LEVEL`, read one way.

Two readers existed for the same environment variable: `slife2 status` warns
about a value this build cannot read, and every server sets its root level from
it.  Two decoders are two rules for one lever — the warning could name a level
the daemon rejects, or accept one the daemon fell back on — so the answer is
here, once, and both callers ask it.

An unreadable value is not a reason to refuse to start: the level is INFO and
the value comes back beside it so the caller can say it.  The fallback is never
silent.
"""

from __future__ import annotations

import logging
import os

#: The one environment variable this module reads.
ENV_VAR = "SLIFE2_LOG_LEVEL"


def configured_level(value: str | None = None) -> tuple[int, str]:
    """The root level named by `SLIFE2_LOG_LEVEL`, and the value if it is not one.

    `(logging.INFO, "")` when the variable is unset or empty; `(level, "")` for
    a name this build knows; `(logging.INFO, asked)` for anything else, with the
    value stripped as it was read so the warning quotes what was written.

    A recognised name is anything `logging.getLevelName` answers with an int —
    the standard names, and any name a plugin has registered with
    `logging.addLevelName`, which is the same set the logging module itself
    honours.
    """
    asked = (os.environ.get(ENV_VAR, "") if value is None else value).strip()
    if not asked:
        return logging.INFO, ""
    level = logging.getLevelName(asked.upper())
    if isinstance(level, int):
        return level, ""
    return logging.INFO, asked
