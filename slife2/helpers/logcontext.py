"""Who a log line is about: the conversation a call is serving, and its turn.

**A server here is one process for every conversation, and its log is one
file.**  Without a name on the line, `tool_search failed` says nothing about
which conversation paid for it, and a reader has to reconstruct the answer from
timestamps across three processes — the gap the 2026-10 runs made obvious.  So
the conversation is a *context*: set once where a call's arguments name it
(`TraceMiddleware`, in `slife2.libs.plugin.protocol`) and read by every record
the formatter writes (`TraceFilter`), rather than an argument every log call has
to remember to pass.

**The turn is the other half of the same question.**  A conversation's turns are
serialised within one process, but one turn crosses several — the context store,
a model backend — and one conversation can be served by two processes at once
(the agent server and an agent task).  `agent/subagent` therefore cannot say
which turn a line in a shared daemon's log belongs to.  A turn token is minted
when the turn starts (`new_turn`, or `begin_turn` where its scope crosses a
lock), carried on every hop's `_meta` (`slife2.libs.plugin.audience.TURN`), read
back by the receiving server's `TraceMiddleware`, and printed beside the
conversation — so every process's lines for one turn share one value.

**Why a contextvar and not a parameter.**  The code that logs deepest —
`slife2.libs.turn.loop`, `slife2.clients.gateway` — deliberately does not know
which conversation it is serving (`AgentLoop`'s own docstring), and threading a
name through every call would make it know.  `asyncio` copies the context into
every task it starts, so a background call keeps the conversation that scheduled
it; a `with` block ends the trace, and a cancellation unwinds it like any other
context manager.

**The conversation trace is a description, not an id**, and it never names a
home.  A log line is read by a person looking at the transcript, and the home is
where the files are rather than part of who the conversation is
(`slife2.libs.host.runtime.agent_key` states the same rule for a name).  The
turn's own stored id is written on the turn lines that know it
(`slife2.plugins.agent.server`), not in this prefix: the id exists only after
the turn has been recorded.

**The turn token is an id, and that is the difference.**  It exists before the
turn does — the store's rowid does not — so it is on every line the turn writes
in every process, including the lines of a cancelled turn that never gets a row.
`turn_done` carries both, which is where the token and the rowid join.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import uuid
from collections.abc import Generator

#: The conversation a call is serving, as `agent` or `agent/subagent`.  Empty
#: outside a call: a server's start-up, a poll loop, a shutdown.
_trace: contextvars.ContextVar[str] = contextvars.ContextVar(
    "slife2_log_trace", default=""
)

#: The turn a call is serving, as an opaque token.  Empty outside a turn: the
#: same start-up, a poll loop, a call that names no conversation.
_turn: contextvars.ContextVar[str] = contextvars.ContextVar(
    "slife2_log_turn", default=""
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


@contextlib.contextmanager
def turning(token: str) -> Generator[None]:
    """Put a turn token on every record written inside this block.

    **An empty token is a no-op**, for `tracing`'s reason: a call that names no
    turn keeps whatever turn its caller is running, and clearing it would lose
    the turn of the hop that made the call.
    """
    if not token:
        yield
        return
    handle = _turn.set(token)
    try:
        yield
    finally:
        _turn.reset(handle)


def new_turn() -> str:
    """A fresh turn token, minted where a turn starts.

    **Opaque and minted rather than the store's rowid**, because the rowid does
    not exist until the turn is recorded — and a cancelled turn often never gets
    one.  The token exists from the turn's first step, which is what lets a line
    written in another process be joined to the turn that caused it.
    """
    return uuid.uuid4().hex


def begin_turn() -> tuple[str, contextvars.Token[str]]:
    """Mint a token and put it on the records written from here.

    **The pair form of `turning`**, for the agent server: its turn starts inside
    `Loop.lock` and is closed after the `turn_done` line, which is written
    outside that block.  A `with` block would have to wrap the whole of
    `send_message` and indent it; a handle can cross the boundary.  `end_turn`
    is the reset.
    """
    token = new_turn()
    return token, _turn.set(token)


def end_turn(handle: contextvars.Token[str]) -> None:
    """Undo `begin_turn`, restoring the turn that was there before."""
    _turn.reset(handle)


def current() -> str:
    """The conversation being served, or `""` outside one."""
    return _trace.get()


def current_turn() -> str:
    """The turn being served, or `""` outside one."""
    return _turn.get()


class TraceFilter(logging.Filter):
    """Put the ambient conversation and turn on every record, for the formatter.

    A filter rather than a `LoggerAdapter`, because the loggers that matter are
    module-level and shared: an adapter would have to be threaded through every
    call site that already exists.  The formatter decides how it reads; the
    filter only says whether there is one.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        trace = _trace.get()
        turn = _turn.get()
        # Leading space so the formatter's own separator is unchanged when there
        # is no conversation: `slife2.db: ...` stays as it was.
        if trace and turn:
            record.slife_trace = f" [{trace} turn={turn}]"
        elif trace:
            record.slife_trace = f" [{trace}]"
        elif turn:
            # A call may carry a turn without a conversation — a model backend
            # whose caller named no key.  The turn is still worth printing:
            # hiding it would make one turn's lines the only unjoinable ones.
            record.slife_trace = f" [turn={turn}]"
        else:
            record.slife_trace = ""
        return True
