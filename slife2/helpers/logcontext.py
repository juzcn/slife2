"""Who a log line is about: the conversation a call is serving.

**A server here is one process for every conversation, and its log is one
file.**  Without a name on the line, `tool_search failed` says nothing about
which conversation paid for it, and a reader has to reconstruct the answer from
timestamps across three processes — the gap the 2026-10 runs made obvious.  So
the conversation is a *context*: set once where a call's arguments name it
(`TraceMiddleware`, in `slife2.libs.plugin.protocol`) and read by every record
the formatter writes (`TraceFilter`), rather than an argument every log call has
to remember to pass.

**Why a contextvar and not a parameter.**  The code that logs deepest —
`slife2.libs.turn.loop`, `slife2.clients.gateway` — deliberately does not know
which conversation it is serving (`AgentLoop`'s own docstring), and threading a
name through every call would make it know.  `asyncio` copies the context into
every task it starts, so a background call keeps the conversation that scheduled
it; a `with` block ends the trace, and a cancellation unwinds it like any other
context manager.

**The trace is a description, not an id**, and it never names a home.  A log line
is read by a person looking at the transcript, and the home is where the files
are rather than part of who the conversation is (`slife2.libs.host.runtime.agent_key`
states the same rule for a name).  The turn's own id is written on the turn
lines that know it (`slife2.plugins.agent.server`), not in this prefix: the id
exists only after the turn has been recorded.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Generator

#: The conversation a call is serving, as `agent` or `agent/subagent`.  Empty
#: outside a call: a server's start-up, a poll loop, a shutdown.
_trace: contextvars.ContextVar[str] = contextvars.ContextVar(
    "slife2_log_trace", default=""
)


def describe(agent: str, subagent: str = "") -> str:
    """The conversation as a log line names it: `jack` or `jack/echo`.

    The same spelling `slife2.plugins.agent.server.describe` uses in its own
    lines, kept here so that a trace and a message cannot disagree about one
    conversation.
    """
    return f"{agent}/{subagent}" if subagent else agent


@contextlib.contextmanager
def tracing(agent: str, subagent: str = "") -> Generator[None]:
    """Name the conversation for every log record written inside this block.

    **A call that names nobody leaves the trace alone.**  Not every tool serves
    a conversation — a clock, a search, a catalogue read — and clearing the trace
    for one of them would lose the conversation of the turn that called it.  So
    the empty case is a no-op rather than a reset.
    """
    if not agent:
        yield
        return
    token = _trace.set(describe(agent, subagent))
    try:
        yield
    finally:
        _trace.reset(token)


def current() -> str:
    """The conversation being served, or `""` outside one."""
    return _trace.get()


class TraceFilter(logging.Filter):
    """Put the ambient conversation on every record, for the formatter.

    A filter rather than a `LoggerAdapter`, because the loggers that matter are
    module-level and shared: an adapter would have to be threaded through every
    call site that already exists.  The formatter decides how it reads; the
    filter only says whether there is one.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        trace = _trace.get()
        # Leading space so the formatter's own separator is unchanged when there
        # is no conversation: `slife2.db: ...` stays as it was.
        record.slife_trace = f" [{trace}]" if trace else ""
        return True
