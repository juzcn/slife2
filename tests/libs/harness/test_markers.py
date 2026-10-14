"""`[Name:{…}]`: the marker a plugin puts on a message it delivers, and how a
screen takes one back off.

Two exist — WeChat's and an agent task's — and the point of these is that they
are *one mechanism*: `strip` takes the prefixes the caller knows about and
removes whichever one it finds, so a third channel is a line in
`slife2.tui.widgets.MARKERS` and nothing here at all.

The names live with their writers rather than in the leaf, which is what the
comment in `widgets` argues for: the module that writes a marker is the one that
knows what it looks like, and a copy drifts into printing the raw marker at
somebody.  So each constant is asserted **against its own writer**, not against
a literal here.
"""

from __future__ import annotations

import pytest

from slife2.libs.harness import markers
from slife2.plugins.agent_task.server import CHANNEL, MARKER_PREFIX
from slife2.plugins.wechat import server as wechat
from slife2.tui.widgets import MARKERS, channel_prefix

pytestmark = pytest.mark.unit


def test_both_markers_come_off_and_nothing_else_does() -> None:
    """One call, every prefix — which is what the window does, and what makes
    adding a channel a line in a list rather than a branch in a function."""
    wechat_text = f'{wechat.MARKER_PREFIX}{{"peer_wechat_id": "p"}}] 你好'
    task_text = f'{MARKER_PREFIX}{{"task_id": "x"}}] three tries'
    assert markers.strip(wechat_text, *MARKERS) == "你好"
    assert markers.strip(task_text, *MARKERS) == "three tries"
    # A prefix this window does not know is left alone rather than guessed at.
    assert markers.strip("[Something:{}] hi", *MARKERS) == "[Something:{}] hi"


def test_a_prefix_without_a_closing_bracket_is_left_whole() -> None:
    """A torn row, or a hand-edited file: a display that eats the rest of a
    message is worse than one that shows a marker it could not read."""
    torn = f'{MARKER_PREFIX}{{"task_id": "x"'
    assert markers.strip(torn, *MARKERS) == torn


def test_a_marker_a_message_only_mentions_is_left_alone() -> None:
    """A marker is a prefix by construction, and only that — so text *about* one
    is somebody's words rather than something to unwrap."""
    inside = f"we discussed {MARKER_PREFIX}{{}} and moved on"
    assert markers.strip(inside, *MARKERS) == inside


def test_the_window_takes_off_exactly_what_the_plugins_write() -> None:
    """The pair held together across the seam: what a plugin writes is what the
    screen removes, character for character — the same property
    `tests/test_wechat.py` asserts for its own marker."""
    assert wechat.MARKER_PREFIX in MARKERS
    assert MARKER_PREFIX in MARKERS


def test_an_agent_tasks_answer_wears_the_task_it_came_from() -> None:
    """The bubble is built from the channel and nothing else, so the task's name
    has to *be* in the channel — and the bubble is deliberately not the key
    spelled again: `agent-task:research` is an address, and what a person is
    being told is who answered."""
    assert channel_prefix(f"{CHANNEL}:research") == "AsyncTask(research)> "
    # A key with no name is shown as it is, like any other channel this window
    # does not have a spelling for.
    assert channel_prefix(CHANNEL) == f"{CHANNEL}> "


def test_a_background_calls_result_wears_the_tool_that_produced_it() -> None:
    """The other half of the pair above, and built the same way: several calls of
    one conversation can be in the background at once (`slife2.libs.harness.async_tasks`), so
    the tool's name is what the channel has to carry."""
    from slife2.libs.harness.async_tasks import CHANNEL as ASYNC_TOOL_CHANNEL
    from slife2.libs.harness.async_tasks import channel_for

    assert channel_for("pdf_to_md") == f"{ASYNC_TOOL_CHANNEL}:pdf_to_md"
    assert channel_prefix(channel_for("pdf_to_md")) == "AsyncTool(pdf_to_md)> "


def test_a_scheduled_trigger_says_which_task_came_due() -> None:
    """The clock's messages wear **no marker** — there is nothing to match a
    result to, because a trigger is not a result (DESIGN.md §1.7) — so the
    channel carries the whole of what the window can say about where it came
    from, and every message this plugin hands over names a task."""
    from slife2.plugins.schedule.server import CHANNEL as SCHEDULE_CHANNEL

    assert channel_prefix(f"{SCHEDULE_CHANNEL}:daily") == "Scheduled(daily)> "
    # A schedule message with no task in it cannot happen: a fire is the only
    # thing this plugin hands over, and a trigger always names the task it is
    # about (`slife2.plugins.schedule.server`), which is what keeps this branch total.
    assert channel_prefix(SCHEDULE_CHANNEL) == f"{SCHEDULE_CHANNEL}> "


def test_the_two_channels_a_window_already_knew_are_unchanged() -> None:
    """The window's own turns are the person's, and WeChat keeps the spelling
    v1 gave its bubble — one less thing to relearn."""
    assert channel_prefix("") == "You> "
    assert channel_prefix("tui") == "You> "
    assert channel_prefix("wechat") == "Wechat> "
