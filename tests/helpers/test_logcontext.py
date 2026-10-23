"""The conversation a log line belongs to.

**A server is one process for every conversation, and its log is one file.**
These tests pin the two halves that make a trace work: the context a call sets
from its own `agent`/`subagent` arguments, and the filter that puts it on the
record the formatter writes.  The middleware is the seam worth testing end to
end, because a signature that stops passing `agent` is exactly the silent
regression this replaces.
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import Client

from slife2.helpers import logcontext
from slife2.libs.plugin.protocol import house_server

pytestmark = pytest.mark.unit


def test_a_trace_names_the_conversation_and_resets() -> None:
    assert logcontext.current() == ""
    with logcontext.tracing("jack"):
        assert logcontext.current() == "jack"
        with logcontext.tracing("jack", "echo"):
            assert logcontext.current() == "jack/echo"
        assert logcontext.current() == "jack", "the inner block is over, the outer is not"
    assert logcontext.current() == ""


def test_a_call_that_names_nobody_leaves_the_trace_alone() -> None:
    """A tool about the machine keeps the conversation of the turn that asked."""
    with logcontext.tracing("jack"):
        with logcontext.tracing(""):
            assert logcontext.current() == "jack"


def test_the_filter_marks_only_the_records_inside_a_call() -> None:
    def record() -> logging.LogRecord:
        return logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)

    outside = record()
    logcontext.TraceFilter().filter(outside)
    assert outside.slife_trace == "", "a start-up line keeps its old shape"

    with logcontext.tracing("jack", "echo"):
        inside = record()
        logcontext.TraceFilter().filter(inside)
    assert inside.slife_trace == " [jack/echo]"


@pytest.mark.asyncio
async def test_a_tool_call_reads_the_conversation_from_its_arguments() -> None:
    """The middleware is the one place that knows a call's identity.

    `TraceMiddleware` sits on every server `house_server` builds, so a tool does
    not have to pass anything to a logger: it calls one, and the record has the
    conversation.  A call with no `agent` is anonymous, which is the right
    answer for a clock or a catalogue read.
    """
    server = house_server("trace-test")

    @server.tool
    async def who(agent: str = "", subagent: str = "") -> str:
        """Say which conversation the logger would name."""
        return logcontext.current()

    @server.tool
    async def anonymous() -> str:
        """A call about the machine, not a conversation."""
        return logcontext.current()

    async with Client(server) as client:
        named = await client.call_tool("who", {"agent": "jack", "subagent": "echo"})
        plain = await client.call_tool("anonymous", {})

    assert named.data == "jack/echo"
    assert plain.data == ""
