"""The widgets the TUI is made of.

The design is a low-chrome dark transcript, borderless messages marked by a
bold amber prefix, tool calls as collapsible bordered panels, and a single dim
status row.  What follows is what is load-bearing about it, because most of
these choices look arbitrary until you change one.

**Nothing the model produces is parsed as markup.**  Every string that came from
a model, a tool or a user goes through `Text(...)`, which does not interpret
`[bold]`-style tags.  Rich's markup would raise on a stray `[` — and model output
is full of them — so the distinction between "our label" and "their text" is
kept by construction rather than by escaping.

**Text is styled in pieces, not by string templates.**  A prefix is a separate
stylable span rather than part of a format string, which is why slife's messages
can carry a coloured `You>` and slife2's can too, without either of them
inventing an escaping scheme.

**Following the tail is decided where a scroll is ASKED FOR**, not from the
offset — see :meth:`ChatView._scroll_to`.  Textual re-clamps `scroll_y` by
itself whenever a size change leaves it past the end, and reading that
correction as intent re-arms following under a reader who is up in the history:
the next streamed token then pulls the page out from under them.

**And it is re-armed when the content's height changes** — see
:meth:`ChatView._size_updated`.  The end of the content is settled a frame after
the text that moved it, so the follow a burst of deltas asks for is aimed at a
height that is already out of date by the time it lands.

**The agent's `name>` is painted only where there is text under it.**  A block
is opened by whatever has something to put in it — the first delta, a resumed
stream after a tool panel, the final answer — so a signature painted
unconditionally leaves a bare `slife2>` row in the transcript for every
iteration that has only reasoning to show.  The message is signed, not the
widget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.geometry import Size
from textual.message import Message as TextualMessage
from textual.widget import Widget
from textual.widgets import Static, TextArea

from slife2.libs.harness.async_tasks import CHANNEL as ASYNC_TOOL_CHANNEL
from slife2.libs.harness.async_tasks import MARKER_PREFIX as ASYNC_CALL_MARKER
from slife2.libs.harness.markers import strip as strip_marker

# Imported, never re-spelled.  The module that writes a marker is the one that
# knows what it looks like, and a prefix typed a second time here is a copy that
# drifts — into printing the raw marker at somebody, which is the failure these
# imports exist to prevent.  Taking one off is `slife2.libs.harness.markers.strip`, which is
# the *shape* rather than any one name, so a third channel is a line here and
# nothing at all in that module.
from slife2.libs.host.config import LOCAL, LOCAL_AGENT, REMOTE
from slife2.plugins.a2a.server import CHANNEL as PEER_CHANNEL
from slife2.plugins.a2a.server import MARKER_PREFIX as PEER_MARKER
from slife2.plugins.agent_task.server import CHANNEL as ASYNC_TASK_CHANNEL
from slife2.plugins.agent_task.server import MARKER_PREFIX as ASYNC_TASK_MARKER
from slife2.plugins.schedule.server import CHANNEL as SCHEDULE_CHANNEL
from slife2.plugins.wechat.server import CONFIG_KEY as WECHAT_CHANNEL
from slife2.plugins.wechat.server import MARKER_PREFIX as WECHAT_MARKER
from slife2.tui.i18n import t
from slife2.tui.theme import GLYPHS, PALETTE

#: Every marker this window knows how to take back off, handed to `strip_marker`
#: together.  A marker that is not in here is one this window does not know
#: about, and an unknown marker is left alone rather than guessed at — which is
#: what makes adding a channel a line in this list.
MARKERS: tuple[str, ...] = (
    WECHAT_MARKER,
    ASYNC_TASK_MARKER,
    ASYNC_CALL_MARKER,
    PEER_MARKER,
    # **The local network's, which is a second envelope and not a second name
    # for the remote one.**  This list held the four the plugins export and
    # missed the one the *config* owns (`LOCAL.marker`, `[Subagent:`) — so a
    # subagent's handed-over answer was drawn with its raw envelope in front of
    # it, which is the exact failure the imports above exist to prevent:
    # `channel_prefix` already knows this channel and gives it `Subagent(jill)>`.
    LOCAL.marker,
    # And the same network's other speaker: a message the agent sends to one of
    # its subagents wears `[Agent:` (`slife2.libs.host.config.LOCAL_AGENT`).
    # Two markers because the *sender's* kind decides the word; one list
    # because this window strips every envelope it knows.
    LOCAL_AGENT.marker,
    # **The group envelopes are their own pair**, and they are *not* the
    # sender's word: every group message on the remote network wears
    # `[PeerAgentGroup:` and every one on the local network `[SubagentGroup:`,
    # whether the sender is the maker or a sibling.  `LOCAL_AGENT` inherits the
    # local group marker through `dataclasses.replace`, so two entries cover
    # both speakers.
    REMOTE.group_marker,
    LOCAL.group_marker,
)

#: So `_add` can hand back the widget type it was given.
WidgetT = TypeVar("WidgetT", bound=Static)

#: How much of a tool's primary argument the header row shows.  The point of
#: the row is to be readable at a glance, so it is a glance's worth.
PRIMARY_ARG_CHARS = 72

#: How much of an approval request's arguments the row shows, and how many of
#: them.  A glance's worth, as the tool panel's own `PRIMARY_ARG_CHARS` is: the
#: question is "may this run", and a call whose arguments run to a page cannot be
#: answered from a page anyway.
APPROVAL_ARG_CHARS = 120
APPROVAL_ARG_LINES = 6

#: How much reasoning an expanded thinking block shows.  Reasoning runs long —
#: it is the model talking to itself — and past a few hundred characters nobody
#: is reading it, they are skimming for the shape of it.
THINKING_PREVIEW_CHARS = 500

#: The channel this window's own turns are sent on (`slife2.tui.client`), and
#: therefore the one whose messages are the person's own words.
WINDOW_CHANNEL = "tui"


def is_own_channel(channel: str) -> bool:
    """Whether a turn came in on this window's own channel.

    **The one predicate for "these are the person's words", asked by both things
    the screen does with a channel**, because a second answer to that question
    disagrees with this one somewhere nobody can see which is right: the bubble
    (:func:`channel_prefix`) and the marker that comes back off the text
    (:meth:`ChatView.add_user`) are the same claim about the same turn, and they
    were written as `channel in ("", WINDOW_CHANNEL)` and `channel != WINDOW_CHANNEL`
    — so an *absent* channel was the person's own to the prefix and somebody
    else's to the strip.  A row can be absent-channel for real: it is what every
    turn stored before the column existed reads as (`DEFAULT ''`), and what
    `Client.run_turn` sends when its caller names none, so the disagreement ate
    the front of a person's own message the moment it happened to begin with a
    marker.

    Absent is this window's own, and that is deliberate rather than a default:
    the only rows without a channel are rows this window wrote or ran, so there
    is nobody else the bubble could belong to.
    """
    return channel in ("", WINDOW_CHANNEL)


def _group_bubble(bubble: str, channel: str) -> str:
    """`PeerAgentGroup(alice@builders)> ` from a group channel.

    The channel is ``<group_channel>:<group>:<sender>``.  A group name is
    validated to exclude ``:``, so the first colon after the channel is the
    group's end; the sender is everything after it, colon and all, because a
    member's name is not this parser's to rewrite.
    """
    rest = channel.split(":", 1)[1]
    group, _, sender = rest.partition(":")
    if not sender:
        return f"{bubble}({group})> "
    return f"{bubble}({sender}@{group})> "


def channel_prefix(channel: str) -> str:
    """What a turn's channel is called on screen.

    **One function, and every reader of a channel asks it** — the live prompt and
    the rebuild both go through :meth:`ChatView.add_user`: a prefix decided in
    two places is two answers to "who said this", and the disagreement surfaces
    on the screen, where nobody can tell which one is wrong.  Which channels
    are the person's own is :func:`is_own_channel`, asked here rather than
    spelled again.

    `tui` is this window, so its turns are the person's own and read `You> `.
    An absent channel is read the same way, because the only rows without one are
    rows this window wrote before there was a column for it.

    Anything else is a name that already exists in the conversation — the key a
    plugin runs under, or a worker's — and it is shown as itself.  `wechat` keeps
    its own spelling of its bubble rather than the key's (`Wechat> `): a
    conversation that reads the same everywhere is one less thing to relearn,
    and the footnote beside the message still carries the key it really came in
    on.
    """
    if is_own_channel(channel):
        return "You> "
    if channel == WECHAT_CHANNEL:
        return "Wechat> "
    if channel.startswith(f"{ASYNC_TASK_CHANNEL}:"):
        # An agent task's answer, wearing the name of the task that produced it.
        # The name is in the channel because a hand-over carries the channel and
        # the words and nothing else (`slife2.plugins.agent_task.server`), and the bubble
        # is deliberately *not* the key spelled again: `agent-task:research` is
        # an address, and what a person is being told is who answered.
        return f"AsyncTask({channel.split(':', 1)[1]})> "
    if channel.startswith(f"{ASYNC_TOOL_CHANNEL}:"):
        # A background tool call of this conversation's own — the other half of
        # the pair above, and named the same way: the tool is what a person
        # needs, since several calls can be in the background at once
        # (`slife2.libs.harness.async_tasks.channel_for`).
        return f"AsyncTool({channel.split(':', 1)[1]})> "
    if channel.startswith(f"{SCHEDULE_CHANNEL}:"):
        # The clock, and which task came due.  **There is no marker to take off
        # here** — unlike an agent task's answer, a trigger wears none, because
        # the marker vocabulary is for matching a result to what produced it and
        # a trigger is not a result (SDD §9.4).  So what the channel
        # carries is the *whole* of what the window can say about where this came
        # from, which is why every message this plugin hands over names a task —
        # including the reconciliation, which is sent per task for this reason.
        return f"Scheduled({channel.split(':', 1)[1]})> "
    if channel.startswith(f"{REMOTE.group_channel}:"):
        # A group on the peer network.  The bubble names the sender *and* the
        # group, because a group turn's channel is the only place either can be
        # recovered: the marker is stripped before drawing, and the words alone
        # do not say who in the group spoke.
        return _group_bubble(REMOTE.group_bubble, channel)
    if channel.startswith(f"{LOCAL.group_channel}:"):
        # A group on the local network.  The sender is `@`-named inside the
        # word the *network* owns: a maker and a sibling both speak here, and
        # the group vocabulary deliberately does not tell them apart
        # (`slife2.libs.host.config.LOCAL.group_bubble`).
        return _group_bubble(LOCAL.group_bubble, channel)
    if channel.startswith(f"{PEER_CHANNEL}:"):
        # A peer agent.  The name is in the channel because a hand-over carries
        # the channel and the words and nothing else — the marker beside the
        # message is taken back off (it is what a *reply* needs, which nothing a
        # person reads can act on), and the footnote still carries the key it
        # really came in on.
        #
        return f"PeerAgent({channel.split(':', 1)[1]})> "
    if channel.startswith(f"{LOCAL.channel}:"):
        # A subagent speaking — to its maker, or to a sibling.  **The word is
        # the sender's, not the window's**: the same local network has two
        # speakers, and `agent:<name>` below is the other one.  A subagent's
        # conversation can be entered by its maker and by a sibling, and the
        # model has to be able to tell whose instruction it is answering
        # (`slife2.libs.host.config.LOCAL`).
        return f"{LOCAL.bubble}({channel.split(':', 1)[1]})> "
    if channel.startswith(f"{LOCAL_AGENT.channel}:"):
        # The agent that made this subagent, speaking into the subagent's own
        # conversation — drawn on the watch screen, and stored in the turn row
        # so a rebuilt one says the same.  `Agent(name)` rather than
        # `Subagent(name)`: a person watching sees who spoke, and the model
        # that read the message sees the same word in its envelope
        # (`slife2.libs.host.config.LOCAL_AGENT`).
        return f"{LOCAL_AGENT.bubble}({channel.split(':', 1)[1]})> "
    # Every other channel is a name that already exists in the conversation, and
    # it is shown as it is rather than guessed at, since a guess here would be
    # this window inventing a speaker (SDD §9.3).
    return f"{channel}> "


def _timestamp(when: datetime | str | None = None) -> str:
    """`HH:MM`, or a longer form once the day is no longer today; `""` for none.

    Accepts the ISO-8601 string a turn records its own time as, so a restored
    message can carry the moment it was sent rather than the moment the window
    was opened — which is the whole difference between a transcript that reads
    as a conversation and one that reads as a wall of text stamped now.

    **A stored moment that will not parse is no moment, not this one.**  The
    `now` a caller gets for an *absent* moment is the live path's — a message
    that is arriving has no other moment to carry — and a damaged record was
    falling into it, so yesterday's turns wore the moment the window opened
    (SDD §5.5: a window that stamped everything now "would render every
    restored conversation as one undivided block stamped now").  A string that
    is not a moment answers `""`, which `add_user` draws as no stamp at all.
    """
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return ""
    now = datetime.now()
    moment = when or now
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    if moment.year == now.year:
        return moment.strftime("%m-%d %H:%M")
    return moment.strftime("%Y-%m-%d %H:%M")


def _duration(ms: int) -> str:
    """How long a tool took, in the unit a person reads it in.

    `ms` under a second and seconds above it, because the number that matters
    changes with the magnitude: 800ms is a fact worth having, 12.4s is, and
    12,400ms is the same fact spelled in a way nobody converts in their head.
    """
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def plain(text: str, style: str = "") -> Text:
    """Model, tool or user text as a renderable it cannot style itself with."""
    return Text(text, style=style)


def content_text(content: object) -> str:
    """A rendered widget's content as the plain string it shows.

    `Static.content` is whatever `update` was handed: a `Text`, a `Content`
    (which is a `Text` too), or a bare string.  Four readers want the same string
    out of it — the three widgets' own `plain_text` and the transcript's, for
    every class it does not know by name — and the fourth copy is the one that
    gets forgotten: a new widget class whose `plain_text` was written by hand is
    the one that quietly stops appearing in what tests read.

    `str(content)` rather than a cast for the last case, because a plain string
    is already the answer and repr-ing a `Text` would put its markup on screen.
    """
    if isinstance(content, Text):
        return content.plain
    return str(content)


async def redirect_printable(widget: Widget, event: events.Key) -> bool:
    """Forward a printable key to the prompt; `True` when it was taken.

    The transcript's widgets are focusable — that is what gives a message Enter
    and Space to unfold its reasoning, and a tool panel the same to open — but a
    click lands focus on one of them, and after that typing has to reach the
    prompt anyway.  Letters and punctuation are what a person types at; the keys
    those widgets were made focusable for are theirs.
    """
    # Space is the one printable key that is *also* a binding, and the widget's
    # own `_on_key` runs before the binding chain is consulted — so taking it
    # here would type a space into the prompt instead of unfolding the reasoning
    # the message was made focusable for.
    if not event.is_printable or event.key == "space":
        return False
    prompt = widget.screen.query_one_optional(HistoryInput)
    if prompt is None or prompt.has_focus:
        return False
    prompt.focus()
    await prompt._on_key(event)
    event.stop()
    return True


def _primary_argument(arguments: dict) -> str:
    """The first non-empty string argument, for the header row.

    A tool call's arguments are the interesting part of it — "read:
    config.yaml" says something, "read" does not — so the first one is shown
    even though which key it is varies by tool.
    """
    for value in arguments.values():
        if isinstance(value, str) and value.strip():
            return value
    return ""


class ChatView(VerticalScroll):
    """The scrolling transcript.

    Streaming means mutating the trailing block on every delta, which is why
    this holds widgets rather than being a `RichLog`: `RichLog` appends whole
    lines, so writing one line per delta would render a separate block per
    token.
    """

    can_focus = True

    def __init__(self, agent: str = "") -> None:
        super().__init__()
        #: Used to sign the assistant's messages, so a conversation reads
        #: `You> ...` / `jack> ...` rather than only marking one side of it.
        self._agent = agent
        self._streaming: AssistantMessage | None = None
        self._streaming_text = ""
        #: What this turn's bill turned out to be, held until there is a block to
        #: print it under.  `set_usage` is called by `TurnFinished`, which arrives
        #: *before* the turn's answer does, so a turn that has streamed no text
        #: yet has nowhere to put the number — see :meth:`set_usage`.
        self._pending_usage = 0
        self._turn_open = False
        #: Tool panels by call id, for a result that is still coming.  **Scoped
        #: to the turn in progress** — :meth:`add_user` clears it as a turn opens
        #: — because a call id is only unique within its turn: keeping every id a
        #: window had ever drawn grew without bound (a day of agent work is
        #: thousands of widget handles) and let a later turn's result for a
        #: reused id complete the previous turn's panel.
        self._tools: dict[str, ToolCallWidget] = {}
        #: Whether the view is pinned to the bottom.  Cleared when the user
        #: scrolls up, so streaming does not yank the page out from under
        #: someone reading back.
        self._at_tail = True
        #: Whether mounting a widget follows the tail.  On for a live turn;
        #: restore turns it off, mounts the whole history and scrolls exactly
        #: once at the end — a scroll per widget is what made a rebuild jitter.
        self._autoscroll = True

    # --- following the tail --------------------------------------------------

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Repaint at the new offset — and deliberately nothing else.

        This overrides `Widget.watch_scroll_y`, which is what repaints the
        widget at the new offset: dropping the delegation left the reader
        scrolling an image that never moved.

        Following is **not** decided here, and that is the whole point of the
        method being this short.  The offset is not the reader's alone: Textual
        re-clamps `scroll_y` whenever a size change leaves it past the end, and
        that assignment arrives through this watcher exactly like a keypress
        does.  Reading it as intent re-armed following under a reader who was
        reading history — the prompt box growing a line was enough, and the next
        streamed token then pulled them to the bottom.  The decision is made
        where the request is made; see :meth:`_scroll_to`.
        """
        super().watch_scroll_y(old_value, new_value)

    def _scroll_to(self, x=None, y=None, **kwargs) -> bool:
        """Following is decided here, from the REQUEST — the one place.

        Every scroll Textual can make funnels through this method: the arrow
        keys, PageUp/PageDown, Home/End, the wheel, the scrollbar, and our own
        `scroll_end`.  So one comparison covers every mover, including one added
        later — which is what a per-method override could never promise, since
        each new way to move the view would have to remember to state the
        intent, and the one that forgot re-armed following under the reader.

        `scroll_target_y` is the clamped request — the value the offset will
        land on — so aiming at or past the end means "follow from here" and
        anything else means the reader is reading history.  A move nothing
        requested never reaches this method, which is the point.

        *x* is the horizontal half of the framework signature: this widget
        scrolls one axis, and a request without a *y* says nothing about
        following.
        """
        result = super()._scroll_to(x, y, **kwargs)
        if y is not None:
            self._at_tail = self.scroll_target_y >= self.max_scroll_y
        return result

    def _size_updated(
        self,
        size: Size,
        virtual_size: Size,
        container_size: Size,
        layout: bool = True,
    ) -> bool:
        """The content grew or shrank: follow it, if the reader is at the tail.

        Following is armed by whatever *changes* the content, but the tail is a
        property of the content's *height*, and the height is settled a frame
        later — in the layout pass this method is part of.  A burst of deltas
        that arrives inside one frame therefore asks to follow the height the
        content had a frame ago; every one of those requests lands short, and
        once the layout has moved past them nothing asks again.  The end of the
        answer then sits below the fold until something else scrolls the view.

        Following on the size is what closes that gap, and it is also the
        cheapest place to do it: this is called on every layout pass, so the
        guard is the change itself rather than the call.
        """
        before = self.virtual_size
        resized = super()._size_updated(size, virtual_size, container_size, layout)
        if self.virtual_size != before:
            self.follow_tail()
        return resized

    def follow_tail(self) -> None:
        """Follow new content, unless following is off or the reader has gone
        back to reading.

        Every streamed token and every mounted widget lands here, so the guards
        have to be here too: without the second, each token scrolls back to the
        end and paging up during a turn is impossible; without the first, a
        rebuild scrolls once per widget.
        """
        if self._autoscroll and self._at_tail:
            self.scroll_end(animate=False)

    def jump_to_tail(self) -> None:
        """Return to the tail and follow from there — the reader's own send.

        The re-arm is stated here as well as by the request, and that is not
        redundant: `scroll_end` makes its request a refresh later, and a token
        streamed inside that window would find following still off.  The intent
        is known now, so it is recorded now.
        """
        self._at_tail = True
        self.scroll_end(animate=False)

    async def _on_key(self, event: events.Key) -> None:
        """Redirect printable keys to the prompt — see `redirect_printable`."""
        if not await redirect_printable(self, event):
            await super()._on_key(event)

    def _add(self, widget: WidgetT, classes: str = "") -> WidgetT:
        if classes:
            widget.add_class(classes)
        self.mount(widget)
        self.follow_tail()
        return widget

    def _close_block(self) -> None:
        self._streaming = None
        self._streaming_text = ""

    # --- messages ------------------------------------------------------------

    def add_user(
        self,
        text: str,
        moment: datetime | str | None = None,
        footnote: str = "",
        channel: str = WINDOW_CHANNEL,
    ) -> None:
        """`[HH:MM] <channel>> text`, the timestamp dim and the prefix bold amber.

        `moment` is when the message was *sent*, and defaults to now.  Restore
        is the only caller that passes one, and it is what keeps a rebuilt
        transcript honest: a conversation from yesterday that says every line
        arrived at the second the window opened is worse than no timestamps.
        A stored moment that will not parse is drawn as no stamp at all rather
        than as this one (`_timestamp`), so the bracket is only there when there
        is a moment to put in it.

        `footnote` is the turn's id, channel and span, and restore is its only
        caller too — a live message has no turn yet to name.  It is drawn as its
        own span rather than found in the text and restyled, which is what keeps
        this widget free of the escaping rules a marker needs: the text a person
        typed is one piece and the metadata is another, and neither has to be
        parsed back out of a string.

        `channel` is where the turn came in on, and it is asked for here rather
        than passed in as a prefix so that the two things the screen does with it
        cannot come apart: the bubble it wears (`channel_prefix`) and the marker
        it must not show (`strip_marker`).  Both are for a message that arrived
        over *another* channel — what the person typed is `You> ` and is theirs,
        which is also why their own text is never unwrapped.  Which channels are
        the person's own is `is_own_channel`, asked here and by `channel_prefix`
        so the two cannot answer differently.
        """
        self._turn_open = False
        self._close_block()
        # **A new turn, so the previous turn's panels are no longer addressable.**
        # This is the one place a turn begins on this view — live and rebuilt go
        # through here — and it is where the tool map is bounded: a call id is
        # scoped to the turn that used it, so an id reused later cannot reach the
        # earlier panel and the map cannot outlive the turn it belongs to.
        self._tools.clear()
        self._user_line(text, moment, footnote, channel)

    def add_cut_in(
        self,
        text: str,
        moment: datetime | str | None = None,
        channel: str = WINDOW_CHANNEL,
    ) -> None:
        """A message that arrived mid-turn, drawn inside the turn that took it.

        **It is a user line and it does not close the turn.**  `add_user` is the
        only other writer of this shape, and it ends the turn (`_turn_open =
        False`) and forgets the open panels (`_tools.clear()`), because it opens
        a new one.  A cut-in belongs to the turn already running: the answer
        still arriving has to land in the block below this line, and the tool
        panels that turn opened are still addressable.  So this closes the
        current *block* — the text run that ended when the message arrived — and
        nothing else.

        **Why this exists at all.**  A cut-in is written into the conversation as
        a harness tool pair, so a display with no vocabulary for it could only
        draw a tool panel (which it is not) or nothing (which is what the
        transcript did while this was deferred, SDD §14.2).  The words are
        already in the record; this is the line that says so while they arrive.
        """
        self._close_block()
        self._user_line(text, moment, "", channel)

    def _user_line(
        self,
        text: str,
        moment: datetime | str | None,
        footnote: str,
        channel: str,
    ) -> None:
        """The one rendering of a message somebody sent.

        `add_user` and `add_cut_in` differ only in what they do to the turn
        around this line; the line itself is one function so the bubble, the
        marker strip and the timestamp cannot come to two answers about the same
        channel.
        """
        if not is_own_channel(channel):
            text = strip_marker(text, *MARKERS)
        line = Text()
        stamp = _timestamp(moment)
        if stamp:
            line.append(f"[{stamp}] ", f"dim {PALETTE['dim']}")
        line.append(channel_prefix(channel), f"bold {PALETTE['amber-bold']}")
        line.append(text, PALETTE["text"])
        if footnote:
            # Inline and after the words, in the style reasoning gets: dim and
            # italic, so it reads as a note about the message rather than as
            # part of it — the distinction the styling carries, not a bracket.
            line.append(f" {footnote}", f"italic {PALETTE['dim']}")
        self._add(Static(line), "user-message")

    def add_note(self, text: str, *, ends_turn: bool = False) -> None:
        """A line from the harness rather than from anyone in the conversation.

        **The brackets are applied here, and every harness line wears them.**
        `[restored 3 turns]`, `[interrupted]`, `[⚡ jill online]` — the shape is
        what tells a reader this line is not somebody speaking, so it belongs to
        the one widget that draws them rather than to each producer.  A caller
        passing text that already carried them would be a caller spelling the
        format a second time, and a caller that forgot them would leave a line
        nothing marks as the harness's.  The words inside are
        `slife2.tui.notes`' (or a plugin's, for a line that arrived over the
        wire).

        **The turn stays open by default, and that is load-bearing.**  A note
        lands *inside* a turn that is still going: the discriminator's `[kept …]`
        arrives before the first delta, and everything the model then streams has
        to flow into the block under it — closing the turn here would make
        `append_text` drop the rest of the answer as though it belonged to
        nobody.  So ending a turn is something a caller asks for.

        `ends_turn` is for the one note that *is* the end of a turn:
        `[interrupted]`.  A cancelled turn has no rest to stream, and leaving it
        open is what let a delta already in the pump for it be drawn under this
        line as though the turn had gone on — the trigger the module docstring
        names for the finish boundary, arriving from the other side.
        """
        if ends_turn:
            self._turn_open = False
        self._close_block()
        self._add(Static(plain(f"[{text}]", PALETTE["dim"])), "system-message")

    def add_picker(self, picker: ModelPicker) -> ModelPicker:
        """Mount the model picker inline, at the tail.

        **Nothing is closed for it**, unlike every message above: a picker
        opened while an answer is streaming belongs *between* two pieces of that
        answer, and `_close_block` would end the block the rest of the answer
        still has to arrive in — under a second signature, because a block is
        opened by the first thing that has something to put in it.
        """
        return self._add(picker, "model-picker")

    def add_conversation_picker(self, picker: ConversationPicker) -> ConversationPicker:
        """Mount the subagent picker inline, at the tail.

        **Nothing is closed for it**, for `add_picker`'s reason exactly: an
        answer that is still streaming stays above the question, and the line
        the decision leaves appears under it.
        """
        return self._add(picker, "conversation-picker")

    def add_approval(self, prompt: ApprovalPrompt) -> ApprovalPrompt:
        """Mount an approval row inline, at the tail.

        **Nothing is closed for it**, for the picker's reason exactly: a call can
        be put to the person in the middle of an answer that is still arriving,
        and ending the block would drop the rest of that answer.
        """
        return self._add(prompt, "approval-prompt")

    def add_error(self, text: str) -> None:
        """A harness line whose news is bad — `add_note`'s shape, in red.

        **The same bracket as a note, on purpose.**  Both are the harness
        speaking; the difference between them is how bad the news is, and colour
        says that without a second format for a reader to learn.  The glyph sits
        inside the bracket, where the bracket is the format rather than part of
        the message.
        """
        self._turn_open = False
        self._close_block()
        line = Text.assemble(
            (f"[{GLYPHS['failed']} ", f"bold {PALETTE['red']}"),
            (text, PALETTE["red"]),
            ("]", PALETTE["red"]),
        )
        self._add(Static(line), "system-message")

    # --- the assistant's turn ------------------------------------------------

    def begin_assistant(self) -> None:
        """Open a turn: from here until `finish_assistant`, deltas land.

        **No block yet, because nothing has been said.**  One used to be opened
        here — "before anything is known about it" — and *anything* that wrote a
        line first closed it again: `add_note` closes the block it is given, so a
        note arriving before the first delta left an empty assistant block in the
        transcript, a blank gap between the prompt and the answer.  The
        discriminator's note is such a line, and so is `[interrupted]` on a turn
        that streamed nothing before it was interrupted.

        Text, reasoning and the final answer each open the block they need —
        which `append_text` was already doing for the case after a tool panel —
        so a turn with nothing to say leaves nothing behind.
        """
        self._close_block()
        self._turn_open = True
        # A turn opens with no bill of its own.  `set_usage` fills this in, and
        # a count left over from a turn that never closed (a cancel, an error)
        # would otherwise print under this one's answer.
        self._pending_usage = 0

    def _open_block(self) -> AssistantMessage:
        return self._add(AssistantMessage(self._agent), "assistant-message")

    def append_text(self, delta: str) -> None:
        """Add a delta to the current block, if a turn is still open.

        Deltas that arrive after the turn's result are **dropped**, and that is
        load-bearing rather than defensive.  Progress events reach the UI as
        queued messages while the result comes back on the awaited call, so a
        delta can be handled after `finish_assistant` has already run.  Letting
        it reopen a block would append a stale fragment under the final answer.
        """
        if not self._turn_open:
            return
        if self._streaming is None:
            # Text resumed after a tool panel: a new block, same turn.
            self._streaming_text = ""
            self._streaming = self._open_block()
        self._streaming_text += delta
        self._streaming.set_text(self._streaming_text)
        self.follow_tail()

    def append_thinking(self, delta: str) -> None:
        """Add reasoning to the current block, if a turn is still open.

        Dropped after the turn's result for the same reason deltas are: a late
        fragment appended under a finished answer is worse than a missing one.
        """
        if not self._turn_open:
            return
        if self._streaming is None:
            self._streaming = self._open_block()
        self._streaming.append_thinking(delta)
        self.follow_tail()

    def finish_assistant(self, text: str) -> None:
        """Close the turn with the authoritative answer.

        Not an optimisation — a correction.  The deltas are a display channel
        and may be incomplete or late; the return value of the tool call is the
        answer, and this is where the two are reconciled.

        **A turn with nothing to say leaves nothing behind**, which
        `begin_assistant` promises and which it takes this method to keep: the
        block is opened here for a turn that streamed nothing, and an *empty*
        answer used to open an empty one — a lone dim `…`, which is the widget's
        "still working" affordance, left under a turn that had finished.  The
        bill goes with it: a turn that drew no answer has no line to print a
        token count under, so the count is dropped rather than hung on the
        previous turn's block (see :meth:`set_usage`).
        """
        self._turn_open = False
        if self._streaming is None and not text:
            self._pending_usage = 0
            self._close_block()
            return
        if self._streaming is None:
            self._streaming = self._open_block()
        self._streaming_text = text
        self._streaming.set_text(text)
        # The bill, for the block that was not open yet when `set_usage` ran.
        # Re-applying an already-placed count is idempotent, which is cheaper to
        # reason about than a flag that says which of the two paths got here.
        if self._pending_usage:
            self._streaming.set_usage(self._pending_usage)
        self._pending_usage = 0
        self._close_block()
        # `follow_tail`, not `jump_to_tail`: ending a turn is not a reason to
        # yank the viewport off somebody who scrolled up to read — which is the
        # whole case the `_at_tail` machinery exists for.  It follows when the
        # view is at the tail, and leaves it alone when it is not.
        self.follow_tail()

    def set_usage(self, tokens: int) -> None:
        """Put a token count under the answer it belongs to.

        **Aimed at this turn's block, and held when it does not exist yet.**
        `TurnFinished` arrives on the progress stream, *before* the turn's answer
        comes back on the awaited call (`slife2.tui.app`), so a turn that has
        streamed no text yet has no block to print the number under.  Holding it
        is what stops the fallback this used to have — `_last_assistant`, the
        *previous* turn's block — from printing this turn's bill under the last
        answer on the screen: a turn that ended with nothing to say (a
        `max_iterations` stop) reported its own tokens beneath somebody else's words.
        :meth:`finish_assistant` places what was held.
        """
        self._pending_usage = tokens
        if self._streaming is not None:
            self._streaming.set_usage(tokens)

    # --- tools ---------------------------------------------------------------

    def add_tool_start(
        self,
        call_id: str,
        name: str,
        arguments: dict | None = None,
        iteration: int = 0,
        max_iterations: int = 0,
    ) -> None:
        # Closes the current text block but not the turn: text may resume after.
        #
        # The block it closes was an iteration on the way somewhere rather than
        # the answer, so its reasoning folds away — the final answer keeps its
        # own open.  This is the only place that distinction is known.
        if self._streaming is not None:
            self._streaming.collapse_thinking()
        self._close_block()
        widget = ToolCallWidget(
            call_id=call_id,
            name=name,
            arguments=arguments or {},
            iteration=iteration,
            max_iterations=max_iterations,
        )
        self._tools[call_id] = widget
        self.mount(widget)
        self.follow_tail()

    def add_tool_end(
        self,
        call_id: str,
        ok: bool,
        result_preview: str,
        result_chars: int,
        elapsed_ms: int,
    ) -> None:
        # A miss is a result with no panel of *this* turn to complete: an id this
        # turn never started, or the same id twice.  The map is cleared as each
        # turn opens (`add_user`), so a call id reused by a later turn can never
        # find the earlier turn's panel here.
        widget = self._tools.get(call_id)
        if widget is None:  # pragma: no cover - a result without a start
            return
        # `ok` is the positive phrasing and `set_complete` wants the negative
        # one.  Naming both ends after the same thing is how these get swapped;
        # a test that asserts a success says "done" is what keeps them honest.
        widget.set_complete(
            result_preview,
            is_error=not ok,
            result_chars=result_chars,
            elapsed_ms=elapsed_ms,
        )
        self.follow_tail()

    def settle_tools(self) -> None:
        """Close out whatever a finished turn left running.

        **Every path that ends a turn comes through here**, because a panel
        showing "running" is a claim about right now and there is no longer
        anything running it: a call's only ending is a `ToolCallFinished` event,
        and a turn that was cancelled or that failed stops receiving them.  Left
        alone, the panel keeps that claim for the rest of the session — the
        ordinary "cancel a slow render" path, and the one a reader is most likely
        to take.

        The map is not cleared: `add_user` owns that, at the next turn's open, and
        an id has to stay addressable for as long as its turn is on screen.  What
        is cleared is the *claim*, which is the part that goes stale.
        """
        for widget in self._tools.values():
            widget.settle_interrupted()

    # --- for tests -----------------------------------------------------------

    def plain_text(self) -> str:
        """Everything shown, as plain text.

        Reads the widgets rather than a private mirror of what was appended, so
        a test asserting on this is asserting on what the user sees.

        **The selector is every class the transcript draws, and an approval row
        is one of them** — it went without one, so the most consequential thing
        on the screen was the one thing this could not see.  A class added here
        and not there is a row that is visibly present and textually absent;
        `content_text` is what the fallback branch reads it through.
        """
        lines = []
        for widget in self.query(
            ".user-message, .assistant-message, .system-message, .tool-call, "
            ".model-picker, .conversation-picker, .approval-prompt"
        ):
            if isinstance(widget, ToolCallWidget):
                lines.append(widget.plain_text())
            elif isinstance(widget, (ModelPicker, ConversationPicker)):
                lines.append(widget.plain_text())
            elif isinstance(widget, Static):
                lines.append(content_text(widget.content))
        return "\n".join(lines)


class AssistantMessage(Static):
    """What the model said, streamed in and then corrected.

    The text is rebuilt wholesale on each delta rather than appended to, which
    is what lets the authoritative answer replace it exactly — and what makes
    the optional usage line land underneath rather than being appended to the
    streamed text and then duplicated.
    """

    can_focus = True

    BINDINGS = [
        Binding("enter,space", "toggle_thinking", "Toggle thinking", show=False),
    ]

    def __init__(self, agent: str = "") -> None:
        super().__init__()
        self._agent = agent
        self._text = ""
        self._thinking = ""
        #: Shown by default, folded away only for an iteration that is not the
        #: answer.
        #:
        #: Reasoning on the *final* answer is the interesting case — it is how
        #: you tell whether the model understood the question — so it is shown.
        #: An iteration that ended in a tool call is machinery on the way
        #: somewhere, and reads better as one line; `collapse_thinking` is what
        #: a tool call does to the block it closes.
        self._thinking_open = True
        self._tokens = 0
        # Painted once at construction, so the block is there before the first
        # delta lands: an empty one renders the dim ellipsis `_refresh` draws for
        # "working", which is the only affordance a model that has not said
        # anything yet can offer.  The *signature* is not painted here — a block
        # is opened before anything is known about it, so signing at construction
        # would leave a bare `jack>` row per iteration (see `_refresh`).
        self._refresh()

    @property
    def thinking(self) -> str:
        return self._thinking

    @property
    def text(self) -> str:
        """The answer, without the reasoning that may sit above it."""
        return self._text

    @property
    def thinking_expanded(self) -> bool:
        return self._thinking_open

    def append_thinking(self, delta: str) -> None:
        self._thinking += delta
        self._refresh()

    def collapse_thinking(self) -> None:
        """Fold the reasoning away, for an iteration that is not the answer."""
        if self._thinking:
            self._thinking_open = False
            self._refresh()

    def action_toggle_thinking(self) -> None:
        self._thinking_open = not self._thinking_open
        self._refresh()

    def on_click(self) -> None:
        """A click expands, and never collapses — a click is also how text gets
        selected, and collapsing would destroy the selection."""
        if not self._thinking_open and self._thinking:
            self._thinking_open = True
            self._refresh()

    def _thinking_block(self) -> Text:
        """The reasoning, as one line or as a block.

        The label is the interface's and the count is a fact about this block,
        so the two are separate spans of one line rather than one formatted
        string: a translator moving the parenthetical has to be able to move it,
        and the glyphs on either end are not a translator's to move at all.
        """
        label = f"{GLYPHS['thinking']} {t('thinking')}"
        if not self._thinking_open:
            line = Text(
                f"{label} {t('thinking_chars', n=len(self._thinking))} "
                f"{GLYPHS['collapsed']}",
                style=f"italic {PALETTE['dim']}",
            )
            return line

        shown = self._thinking
        if len(shown) > THINKING_PREVIEW_CHARS:
            shown = shown[:THINKING_PREVIEW_CHARS] + GLYPHS["ellipsis"]
        line = Text(f"{label}{GLYPHS['ellipsis']}\n", style=f"italic {PALETTE['dim']}")
        line.append(shown, style=PALETTE["dim"])
        return line

    def set_text(self, text: str) -> None:
        self._text = text
        self._refresh()

    def set_usage(self, tokens: int) -> None:
        self._tokens = tokens
        self._refresh()

    def _refresh(self) -> None:
        line = Text()
        if self._thinking:
            line.append_text(self._thinking_block())
        if self._text:
            if self._thinking:
                line.append("\n")
            # The same treatment as the user's `You> `: a signature in bold
            # amber, so the two sides of a conversation are marked the same way
            # and the agent's name is visible where it is being used.
            #
            # Signed only where there is text to sign.  A block is opened before
            # anything is known about it — at `begin_assistant`, and again after
            # each tool panel when reasoning or text resumes — so a signature
            # painted at construction leaves a bare `slife2>` row in the
            # transcript for every iteration that has only reasoning to show:
            # one per tool call, on a reasoning model.  The message is signed,
            # not the widget.
            if self._agent:
                line.append(f"{self._agent}> ", f"bold {PALETTE['amber-bold']}")
            line.append(self._text, PALETTE["text"])
        elif not self._thinking:
            # Nothing at all yet: a dim ellipsis is the only affordance that
            # says "working", and dim so it never reads as content.
            line.append(GLYPHS["ellipsis"], PALETTE["dimmest"])
        if self._tokens:
            line.append(
                f"\n{GLYPHS['up']} {t('tokens', n=self._tokens)}", PALETTE["dim"]
            )
        self.update(line)


class ToolCallWidget(VerticalScroll):
    """One tool call, as a collapsible bordered panel.

    Collapsed it is a single header row — which is the state it will spend
    almost all its life in, since a collapsed one-liner is the whole value of
    showing a tool call at all.  Expanded it shows the arguments and the result.
    """

    can_focus = True

    BINDINGS = [
        Binding("enter,space", "toggle_detail", "Toggle detail", show=False),
    ]

    def __init__(
        self,
        *,
        call_id: str,
        name: str,
        arguments: dict,
        iteration: int = 0,
        max_iterations: int = 0,
    ) -> None:
        super().__init__()
        self.call_id = call_id
        self.tool_name = name
        self.arguments = arguments
        #: Which iteration of the turn asked for it (1-based) and the cap that
        #: turn is running under, `0` for no cap — both from the event, and both
        #: 0 for a panel rebuilt from the turn log, which keeps no such column.
        self._iteration = iteration
        self._max_iterations = max_iterations
        self._collapsed = True
        self._result = ""
        self._result_chars = 0
        self._is_error = False
        self._done = False
        #: Set when the turn this call belongs to ended before the call did.
        #: Terminal, and the only one of these four that says something about the
        #: *window* rather than about the call (`_status`).
        self._interrupted = False
        #: Wall time the call took, from the event.  Zero while it is running,
        #: and zero for a call whose duration nobody reported.
        self._elapsed_ms = 0
        self.add_class("tool-call")
        self._body = Static(classes="tool-content")
        self._refresh_display()

    def compose(self):
        yield self._body

    def _refresh_display(self) -> None:
        self._body.update(self._content())

    def _status(self) -> tuple[str, str, str]:
        """`(glyph, colour, word)` for the header row.

        The word is a key rather than the state's own name: `running` is what
        this widget's vocabulary calls the state and `运行中` is what the screen
        says about it, and only the first of those belongs in the code.

        **Four states, and the fourth is not an error.**  `interrupted` is a call
        the window was still waiting on when its turn ended — cancelled, or the
        turn failed — so the answer never arrived and never will.  It is drawn
        apart from both of the others because it is neither: the call did not
        fail, and saying it had would be this widget inventing a verdict about a
        tool that may have run to completion on the server.  A panel left in
        `running` for the rest of the session is the alternative, and that is the
        one thing a status may not be — a claim about right now that stopped
        being true.
        """
        if self._interrupted:
            return GLYPHS["interrupted"], PALETTE["dim"], t("tool_interrupted")
        if not self._done:
            return GLYPHS["running"], PALETTE["amber"], t("tool_running")
        if self._is_error:
            return GLYPHS["error"], PALETTE["red"], t("tool_error")
        return GLYPHS["done"], PALETTE["green"], t("tool_done")

    def _label(self) -> str:
        """`read_file` reads better as `Read file`."""
        return self.tool_name.replace("_", " ").capitalize()

    def _header(self) -> Text:
        glyph, colour, word = self._status()
        indicator = GLYPHS["collapsed"] if self._collapsed else GLYPHS["expanded"]
        line = Text.assemble(
            (f"{indicator} ", PALETTE["text"]),
            (f"{glyph} ", colour),
            (self._label(), f"bold {PALETTE['amber']}"),
        )
        primary = _primary_argument(self.arguments)
        if primary:
            short = primary[:PRIMARY_ARG_CHARS]
            if len(primary) > PRIMARY_ARG_CHARS:
                short += GLYPHS["ellipsis"]
            line.append(": ", PALETTE["text"])
            line.append(short, PALETTE["muted"])
        line.append("  ")
        line.append(word, colour)
        if self._iteration > 0:
            # Which iteration of the turn asked for this, in two spellings:
            # a bare `(3)` while `max_iterations` is 0 — no cap, so
            # there is no denominator to write — and `(3/30)` under one.  Either
            # way a long turn is visibly advancing, which is the whole of what
            # the counter is for; a panel with no iteration to report (a rebuilt
            # one) draws nothing rather than `(0)`.
            counter = (
                f"({self._iteration}/{self._max_iterations})"
                if self._max_iterations > 0
                else f"({self._iteration})"
            )
            line.append(f" {counter}", PALETTE["dimmest"])
        if self._elapsed_ms:
            # How long it took, in the collapsed header rather than in the
            # detail: a tool that hung for four seconds is worth seeing without
            # expanding anything, and the duration is the one thing about a
            # finished call that the result text does not say.
            line.append(
                f" {GLYPHS['ellipsis']} {_duration(self._elapsed_ms)}",
                PALETTE["dimmest"],
            )
        return line

    def _content(self) -> Text:
        line = self._header()
        if self._collapsed:
            return line

        line.append("\n")
        line.append(t("tool_arguments"), f"bold {PALETTE['muted']}")
        if self.arguments:
            for key, value in self.arguments.items():
                shown = str(value)
                if len(shown) > 500:
                    shown = shown[:500] + GLYPHS["ellipsis"]
                line.append(f"\n  {key} = ", PALETTE["muted"])
                line.append(shown, PALETTE["text-secondary"])
        else:
            line.append(f"\n  {t('tool_no_arguments')}", PALETTE["muted"])

        if self._interrupted:
            line.append(f"\n\n{t('tool_interrupted_note')}", f"italic {PALETTE['dim']}")
        elif self._done:
            if self._is_error:
                line.append(f"\n\n{t('tool_error_heading')}", f"bold {PALETTE['red']}")
                line.append("\n" + self._result, PALETTE["red"])
            else:
                line.append(
                    f"\n\n{t('tool_result_heading')}", f"bold {PALETTE['muted']}"
                )
                line.append("\n" + self._result, PALETTE["text-secondary"])
                # What the tool actually returned, when that is more than the
                # preview shows.  A line-count guard used to stand here,
                # trimming to `MAX_RESULT_LINES` and saying how many were left
                # — and it could never fire: the result on the wire is capped at
                # `events.PREVIEW_CHARS` (two hundred), which cannot make five
                # hundred lines however it is spelled.  What is worth saying is
                # the size, which is the number carried beside the preview for
                # exactly this and was read by nothing else.
                if self._result_chars > len(self._result):
                    line.append(
                        f"\n{GLYPHS['ellipsis']} "
                        f"{t('tool_result_chars', n=self._result_chars)}",
                        PALETTE["dimmest"],
                    )
        return line

    def set_complete(
        self, result: str, is_error: bool, result_chars: int, elapsed_ms: int
    ) -> None:
        # **The marker comes off before it is kept, not before it is drawn.**
        # A tool result may wear one — a background call's result does
        # (`slife2.libs.harness.async_tasks`), and it is the same shape a handed-over message
        # wears — and the panel is a display: the marker is machinery's words for
        # other machinery, and a person reading a result should get the sentence
        # that follows it.
        self._result = strip_marker(result, *MARKERS)
        self._is_error = is_error
        self._result_chars = result_chars
        self._elapsed_ms = elapsed_ms
        self._done = True
        self._refresh_display()

    def settle_interrupted(self) -> None:
        """Say the turn ended before this call did — and say it once.

        Called by the view when a turn is over, on every panel the turn opened
        and never closed.  A call that already has a result keeps it: the events
        and the ending race (`slife2.tui.app`), and a result that arrived is the
        answer, whichever of the two got there first.
        """
        if self._done:
            return
        self._interrupted = True
        self._refresh_display()

    @property
    def collapsed(self) -> bool:
        """Whether the panel is showing only its header row."""
        return self._collapsed

    def action_toggle_detail(self) -> None:
        self._collapsed = not self._collapsed
        self._refresh_display()

    def on_click(self) -> None:
        """A click expands, and never collapses.

        Asymmetric on purpose: a click is also how text gets selected, and
        collapsing on click would destroy the selection the user was making.
        Collapsing is Enter or Space, which cannot be a mis-aimed drag.
        """
        if self._collapsed:
            self._collapsed = False
            self._refresh_display()

    def plain_text(self) -> str:
        return content_text(self._body.content)


class HistoryInput(TextArea):
    """The prompt: Enter sends, Shift+Enter breaks the line.

    `TextArea` rather than `Input` because a pasted stack trace or a paragraph
    is a normal thing to send, and `Input` silently truncates at the first
    newline.  It grows from one row to twelve as the draft does, wrapped lines
    included, so a long prompt reads in place rather than through a one-row
    window.
    """

    #: How many previous prompts Up/Down can reach.
    MAX_HISTORY = 256

    BINDINGS = [
        Binding("shift+enter", "newline", "Newline", show=False, priority=True),
        Binding("up", "history_previous", "Previous prompt", show=False),
        Binding("down", "history_next", "Next prompt", show=False),
        # TextArea claims PageUp/PageDown to page through its *own* text, and
        # for a chat prompt that is the wrong target: the draft is a few rows
        # at most while the transcript above is what the reader is paging.
        # The binding is what takes the key back — a binding here beats the one
        # inherited from TextArea — and the action forwards it to the transcript.
        Binding("pageup", "transcript_page_up", show=False),
        Binding("pagedown", "transcript_page_down", show=False),
    ]

    class Submitted(TextualMessage):
        """Enter was pressed with something in the box."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._history: list[str] = []
        self._index = -1
        self._draft = ""

    async def _on_key(self, event) -> None:
        """Enter submits, wherever the cursor happens to be.

        Handled here rather than as a binding because a binding would need to
        win against TextArea's own newline insertion at every cursor position;
        `prevent_default` stops that insertion, and stopping the event stops it
        reaching anything else.
        """
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text.strip()
            if text:
                # The message carries the text, so the box can be emptied here
                # rather than by the handler — which also means a slow handler
                # cannot leave the sent prompt sitting in the input, looking
                # like it is still about to be sent.
                self.post_message(self.Submitted(text))
                self.clear()
            return
        await super()._on_key(event)

    # --- paging the transcript, which the prompt sits under ------------------

    def _transcript(self) -> ChatView | None:
        """The chat view, or None when there is no transcript to page.

        `HistoryInput` is a `TextArea` and can be mounted on its own, where the
        keys above have nothing to forward to.
        """
        return self.screen.query_one_optional(ChatView)

    def action_transcript_page_up(self) -> None:
        """Page the transcript up — the key belongs to it, not to the draft."""
        transcript = self._transcript()
        if transcript is not None:
            transcript.scroll_page_up(animate=False)

    def action_transcript_page_down(self) -> None:
        """The mirror of :meth:`action_transcript_page_up`."""
        transcript = self._transcript()
        if transcript is not None:
            transcript.scroll_page_down(animate=False)

    # The wheel over this box is the same case as PageUp/PageDown above, and it
    # is how the transcript is normally read: Textual delivers a tick to
    # whatever is under the pointer, and the pointer sits here after typing.
    # The draft has nothing to scroll — `overflow` is `hidden` on the prompt, so
    # it follows the cursor rather than a wheel — and the base handler neither
    # moved it nor stopped the tick, leaving the tick to bubble to a Screen with
    # no scroll to give it.  `_scroll_*_for_pointer` is still asked first, so a
    # stylesheet that ever made the draft scrollable would not have this
    # override steal every tick from it.

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        """Forward a wheel tick the draft cannot use to the transcript."""
        if event.ctrl or event.shift:
            super()._on_mouse_scroll_up(event)  # horizontal — not ours
            return
        if not self._scroll_up_for_pointer(animate=False):
            self._wheel_transcript(up=True)
        event.stop()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        """The mirror of :meth:`_on_mouse_scroll_up`."""
        if event.ctrl or event.shift:
            super()._on_mouse_scroll_down(event)
            return
        if not self._scroll_down_for_pointer(animate=False):
            self._wheel_transcript(up=False)
        event.stop()

    def _wheel_transcript(self, *, up: bool) -> None:
        """Step the transcript by one wheel notch.

        Measured from the offset, not from `scroll_target_y`: a token's follow
        is aimed at the end and may still be on its way there, so a notch taken
        from the target would jump the reader to the end minus one line instead
        of stepping from where they are looking.  `immediate=True` is the same
        point — the notch lands now, and stating the intent now is what stops
        the next token from following it back down.
        """
        transcript = self._transcript()
        if transcript is None:
            return  # nothing to scroll
        step = self.app.scroll_sensitivity_y
        transcript.scroll_to(
            y=transcript.scroll_y - step if up else transcript.scroll_y + step,
            animate=False,
            immediate=True,
        )

    def action_newline(self) -> None:
        self.insert("\n")

    def remember(self, text: str) -> None:
        """Add a submitted prompt to the history, skipping an immediate repeat."""
        text = text.strip()
        if not text or (self._history and self._history[-1] == text):
            return
        self._history.append(text)
        del self._history[: -self.MAX_HISTORY]
        self._index = -1

    def action_history_previous(self) -> None:
        """Walk back through history, but only from the first line.

        Inside a multi-line draft, Up is a cursor movement — reaching for
        history there would make a long prompt unusable.
        """
        if self.cursor_location[0] > 0 or not self._history:
            self.action_cursor_up()
            return
        if self._index == -1:
            self._draft = self.text
        if self._index < len(self._history) - 1:
            self._index += 1
            self._show(self._history[-(self._index + 1)])

    def action_history_next(self) -> None:
        if not self._history or self._index == -1:
            self.action_cursor_down()
            return
        self._index -= 1
        self._show(
            self._draft if self._index == -1 else self._history[-(self._index + 1)]
        )

    def _show(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)


@dataclass(frozen=True)
class ModelChoice:
    """One model the picker offers: what to ask for, and what it is.

    A value built from the config by whoever opens the picker, rather than a
    `ModelSettings` handed over or a config read from in here.  A widget that
    can reach the config file is a widget that can disagree with the server
    about what the models are; this carries only what the row draws and what
    the switch names — `ref` — and the two are the same string by construction.
    """

    #: `provider/model` — the reference `model_switch` takes, and the row's text.
    ref: str
    #: The config's `name:` where it wrote one, and empty where it did not: a
    #: row shows a name only for a model somebody took the trouble to name.
    label: str = ""
    context_window: int = 0
    thinking: bool = False
    vision: bool = False


class ModelPicker(Static):
    """The models this config defines, inline in the transcript.

    The interaction is fixed: `↑`/`↓` move a cursor, `Enter` picks, `Esc`
    cancels, the row re-renders itself to a status line once decided, and the
    caller mounts it, focuses it and awaits the future it was handed.  The
    cursor opens on the model the conversation is already on, so a bare `Enter`
    is a no-op rather than a surprise.  **A conversation on a model this list
    does not offer has no such row**, so the cursor starts under nothing, the row
    says which model is missing, and `Enter` picks nothing — rather than
    defaulting to the first model and switching the conversation onto it.

    **The bindings are `priority`, and that is what makes the keys the picker's.**
    The priority pass runs before the focused widget, and the widget that would
    otherwise take them is the prompt — a `TextArea` whose `↑`/`↓` reach back
    through the draft's history, which is the wrong list to move while a model
    is being chosen.

    **Losing focus without a decision cancels.**  A picker is dismissed by a
    click on the transcript as easily as by `Esc`, and one left undecided is
    worse than a wasted keystroke: the future would never resolve, so the app's
    `Ctrl+S` would stay latched and every later press would do nothing.  A
    decided picker ignores the blur, which is what makes the refocus after a
    pick harmless.
    """

    can_focus = True

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True, show=False),
        Binding("down", "cursor_down", "Down", priority=True, show=False),
        Binding("enter", "pick", "Pick", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(
        self,
        choices: Sequence[ModelChoice],
        active: str,
        future: asyncio.Future[ModelChoice | None],
    ) -> None:
        super().__init__("")
        self._choices = list(choices)
        self._active = active
        self._future = future
        self._decided = False
        # `-1` when the conversation is on a model this list does not offer —
        # ordinary after a config edit.  A cursor defaulting to row 0 would make
        # a bare `Enter`, documented as a no-op, switch to whichever model
        # happened to be first, so nothing is under the cursor and the row says
        # why (`_body`); `action_pick` then chooses nothing.
        self._cursor = next(
            (i for i, one in enumerate(self._choices) if one.ref == active), -1
        )
        self.update(self._body())

    # --- the keys ------------------------------------------------------------

    def action_cursor_up(self) -> None:
        """Move the cursor up, clamped at the top."""
        if not self._decided and self._cursor > 0:
            self._cursor -= 1
            self.update(self._body())

    def action_cursor_down(self) -> None:
        """Move the cursor down, clamped at the bottom."""
        if not self._decided and self._cursor < len(self._choices) - 1:
            self._cursor += 1
            self.update(self._body())

    def action_pick(self) -> None:
        """Choose the model under the cursor, or nothing when no row is under it.

        `_cursor` is `-1` for a conversation running a model this list does not
        offer, and a bare `Enter` there must pick nothing rather than the first
        row — see `__init__`.
        """
        if not self._decided and 0 <= self._cursor < len(self._choices):
            self._decide(self._choices[self._cursor])

    def action_cancel(self) -> None:
        """Choose nothing."""
        self._decide(None)

    def on_blur(self) -> None:
        """Focus went elsewhere: nothing was chosen.  See the class docstring."""
        self._decide(None)

    def _decide(self, choice: ModelChoice | None) -> None:
        """Resolve the future once, and re-render as the decision.

        Once, because the blur that follows every decided picker would
        otherwise set the same future a second time, and a resolved future
        raises rather than shrugs.

        **And a future that is *gone* is not a decision to report.**  Quitting
        while the picker is open unmounts the widget and cancels the worker
        parked on this future, which cancels the future itself; the blur that
        shutdown then delivers arrives with nothing to resolve it into, and
        `set_result` on a cancelled future raises `InvalidStateError` — out of
        the message pump, as a traceback at exit instead of a clean quit.
        `ApprovalPrompt._decide` guards this and this did not; the two are the
        same widget shape, so they are guarded the same way.
        """
        if self._decided:
            return
        self._decided = True
        self._chosen = choice
        if not self._future.done():
            self._future.set_result(choice)
        self.update(self._body())

    # --- the row -------------------------------------------------------------

    def _body(self) -> Text:
        """The row, as it stands: the list, or the line the decision left.

        **Not `_render`**, which is the name this wants and cannot have: Textual
        has one, it is the hook `get_content_height` calls to measure a widget,
        and overriding it makes this class return a `Text` where the framework
        wants a visual — a layout crash at the first resize, nowhere near the
        line that caused it.
        """
        if self._decided:
            return self._decided_line()
        line = Text()
        line.append(t("picker_title"), f"bold {PALETTE['text']}")
        if self._cursor < 0:
            # A conversation running a model this config no longer lists: said,
            # so the missing cursor is not read as a broken picker, and so that
            # `Enter` doing nothing is explained rather than felt.  A record with
            # no name at all is named by a word of its own, so the sentence
            # reads as one either way.
            line.append("\n")
            line.append(f"{GLYPHS['failed']} ", f"bold {PALETTE['red']}")
            line.append(
                t(
                    "picker_missing",
                    name=self._active or t("picker_model_in_use"),
                ),
                PALETTE["red"],
            )
        for index, choice in enumerate(self._choices):
            current = choice.ref == self._active
            line.append("\n")
            line.append(
                f"{GLYPHS['cursor']} " if index == self._cursor else "  ",
                PALETTE["amber"],
            )
            line.append(f"{index + 1}. ", PALETTE["dim"])
            line.append(
                f"{GLYPHS['current']} " if current else "  ",
                PALETTE["amber"] if current else PALETTE["dim"],
            )
            # The reference, because that is what the switch names; the label
            # follows it, because that is what the config calls the thing.
            line.append(choice.ref, f"bold {PALETTE['text']}")
            if choice.label and choice.label != choice.ref:
                line.append(f"  {choice.label}", PALETTE["muted"])
            meta = _model_meta(choice)
            if meta:
                line.append(f"  {meta}", PALETTE["dim"])
        # The hint row: the keys as they are, and the action each one performs in
        # the interface's language.  `↑`/`↓`, `Enter` and `Esc` are what is
        # printed on the keyboard, so they are not words and do not move.
        line.append("\n")
        line.append(f"{GLYPHS['up']}/{GLYPHS['down']} ", PALETTE["dim"])
        line.append(t("picker_select"), f"bold {PALETTE['green']}")
        line.append("  Enter ", PALETTE["dim"])
        line.append(t("picker_pick"), f"bold {PALETTE['green']}")
        line.append("  Esc ", PALETTE["dim"])
        line.append(t("picker_cancel"), f"bold {PALETTE['red']}")
        return line

    def _decided_line(self) -> Text:
        line = Text()
        if self._chosen is None:
            line.append(f"{GLYPHS['failed']} ", f"bold {PALETTE['red']}")
            line.append(t("picker_cancelled"), f"bold {PALETTE['red']}")
            return line
        line.append(f"{GLYPHS['check']} ", f"bold {PALETTE['green']}")
        line.append(t("picker_switched"), f"bold {PALETTE['green']}")
        line.append(self._chosen.ref, f"bold {PALETTE['text']}")
        return line

    def plain_text(self) -> str:
        """What this row shows, for the transcript's own `plain_text`."""
        return content_text(self.content)


class ConversationPicker(Static):
    """The subagents this agent has made, inline in the transcript.

    The model picker's shape, one decision smaller: a cursor, `Enter` picks,
    `Esc` cancels, a blur cancels (a question abandoned is worse than a wasted
    keystroke), and the row re-renders as the line the decision left.  The
    bindings are `priority` for the picker's reason: the focused widget is the
    prompt, whose `TextArea` would otherwise take `↑`/`↓` for its draft
    history — the wrong list to move while a conversation is being chosen.

    **No active row and no missing-row case**, unlike the model picker: there is
    no setting this changes, so there is nothing the list could already be on.
    The cursor opens on the first name, because a person who pressed the key
    came to watch one of them.
    """

    can_focus = True

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True, show=False),
        Binding("down", "cursor_down", "Down", priority=True, show=False),
        Binding("enter", "pick", "Pick", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", priority=True, show=False),
    ]

    def __init__(
        self, choices: Sequence[str], future: asyncio.Future[str | None]
    ) -> None:
        super().__init__("")
        self._choices = list(choices)
        self._future = future
        self._decided = False
        self._chosen: str | None = None
        self._cursor = 0 if self._choices else -1
        self.update(self._body())

    # --- the keys ------------------------------------------------------------

    def action_cursor_up(self) -> None:
        if not self._decided and self._cursor > 0:
            self._cursor -= 1
            self.update(self._body())

    def action_cursor_down(self) -> None:
        if not self._decided and self._cursor < len(self._choices) - 1:
            self._cursor += 1
            self.update(self._body())

    def action_pick(self) -> None:
        """Choose the name under the cursor, or nothing when none is under it."""
        if not self._decided and 0 <= self._cursor < len(self._choices):
            self._decide(self._choices[self._cursor])

    def action_cancel(self) -> None:
        self._decide(None)

    def on_blur(self) -> None:
        """Focus went elsewhere: nothing was chosen.  See `ModelPicker`."""
        self._decide(None)

    def _decide(self, choice: str | None) -> None:
        """Resolve the future once, and re-render as the decision.

        Once, because the blur that follows every decided picker would otherwise
        set the same future a second time and raise out of the message pump —
        the guard `ModelPicker._decide` and `ApprovalPrompt._decide` both carry.
        """
        if self._decided:
            return
        self._decided = True
        self._chosen = choice
        if not self._future.done():
            self._future.set_result(choice)
        self.update(self._body())

    # --- the row -------------------------------------------------------------

    def _body(self) -> Text:
        if self._decided:
            return self._decided_line()
        line = Text()
        line.append(t("conversation_picker_title"), f"bold {PALETTE['text']}")
        for index, name in enumerate(self._choices):
            line.append("\n")
            line.append(
                f"{GLYPHS['cursor']} " if index == self._cursor else "  ",
                PALETTE["amber"],
            )
            line.append(f"{index + 1}. ", PALETTE["dim"])
            line.append(name, f"bold {PALETTE['text']}")
        line.append("\n")
        line.append(f"{GLYPHS['up']}/{GLYPHS['down']} ", PALETTE["dim"])
        line.append(t("picker_select"), f"bold {PALETTE['green']}")
        line.append("  Enter ", PALETTE["dim"])
        line.append(t("picker_pick"), f"bold {PALETTE['green']}")
        line.append("  Esc ", PALETTE["dim"])
        line.append(t("picker_cancel"), f"bold {PALETTE['red']}")
        return line

    def _decided_line(self) -> Text:
        line = Text()
        if self._chosen is None:
            line.append(f"{GLYPHS['failed']} ", f"bold {PALETTE['red']}")
            line.append(t("picker_cancelled"), f"bold {PALETTE['red']}")
            return line
        line.append(f"{GLYPHS['check']} ", f"bold {PALETTE['green']}")
        line.append(
            t("conversation_watching", name=self._chosen),
            f"bold {PALETTE['green']}",
        )
        return line

    def plain_text(self) -> str:
        """What this row shows, for the transcript's own `plain_text`."""
        return content_text(self.content)


class ApprovalPrompt(Static):
    """One tool call, waiting for the person to allow it.

    The model asked to be checked before a call ran (`_approve`), the loop is
    parked on the answer, and this row is the question: which tool, what it would
    be called with, and the keys that answer.  A row in the transcript rather
    than a modal, so the question and the answer sit together in the history.

    **The bindings are `priority`, and the `TextArea` below is why** — the
    picker's reason, one keystroke further: without them `y` and `n` would be
    typed into the prompt and `Esc` would cancel the whole *turn* instead of
    refusing one call, so the key a person presses here would sometimes be the
    one that throws their turn away.

    **A decided row stays, as its status line.**  The panel for the tool itself
    is mounted *after* an approval and never for a refusal, so this line is the
    only place a refused call is ever seen — which is why the denial is drawn
    here rather than as a tool result.

    **Losing focus does not answer, and that is where this parts from the
    picker.**  A picker that loses focus is a question abandoned and resolves
    itself (see `ModelPicker`), but this row is blurred by the two things that
    take focus on purpose: every printable key, which `redirect_printable` lands
    in the prompt, and `Ctrl+S`, which opens the picker over it.  Reading that
    blur as "no" means a person who merely began their next thought — an `a`,
    a paste, the `s` of `Ctrl+S` — silently refuses the call the model was
    parked on, and then watches the picker `Ctrl+S` had opened cancel itself
    off the refocus that answer set off.  So there is deliberately **no
    `on_blur` override here**: the row stays up and `pending`, and the answers
    are the keys (`y`, `n`, `Esc`) or a cancel of the turn, which denies the
    call it holds (`slife2.tui.app._cancel_turn`) rather than leaving a future
    nobody will set.
    """

    can_focus = True

    BINDINGS = [
        Binding("y", "approve", "Approve", priority=True, show=False),
        Binding("n", "deny", "Deny", priority=True, show=False),
        Binding("escape", "deny", "Deny", priority=True, show=False),
    ]

    def __init__(
        self, name: str, arguments: dict, future: asyncio.Future[bool]
    ) -> None:
        super().__init__("")
        self._name = name
        self._arguments = dict(arguments)
        self._future = future
        self._decided: bool | None = None
        self.update(self._body())

    # --- the keys ------------------------------------------------------------

    def action_approve(self) -> None:
        """Let the call run."""
        self._decide(True)

    def action_deny(self) -> None:
        """Refuse it — `n` and `Esc` are the same answer."""
        self._decide(False)

    @property
    def pending(self) -> bool:
        """Whether this row is still waiting for an answer.

        A decided row keeps the class, so a caller looking for "the approval
        that is blocking the loop" selects on this rather than on the widget.
        """
        return self._decided is None

    def _decide(self, approved: bool) -> None:
        """Resolve the future once, and re-render as the decision.

        Once, because the blur that follows every decision would otherwise set
        the same future a second time — and a resolved future raises rather than
        shrugs (`ModelPicker._decide`'s reason, verbatim).
        """
        if self._decided is not None:
            return
        self._decided = approved
        if not self._future.done():
            self._future.set_result(approved)
        self.update(self._body())

    # --- the row -------------------------------------------------------------

    def _body(self) -> Text:
        if self._decided is not None:
            return self._decided_line()
        line = Text()
        line.append(f"{GLYPHS['running']} ", f"bold {PALETTE['amber']}")
        line.append(self._name, f"bold {PALETTE['text']}")
        line.append(t("approval_requests"), PALETTE["amber"])
        # A glance's worth of the arguments, and the count of what is left: the
        # question is "may this run", and a call whose arguments run to a page
        # cannot be answered from a page anyway.
        items = list(self._arguments.items())
        for key, value in items[:APPROVAL_ARG_LINES]:
            shown = str(value)
            if len(shown) > APPROVAL_ARG_CHARS:
                shown = shown[:APPROVAL_ARG_CHARS] + GLYPHS["ellipsis"]
            line.append(f"\n  {key} = ", PALETTE["dim"])
            line.append(shown, PALETTE["muted"])
        rest = len(items) - APPROVAL_ARG_LINES
        if rest > 0:
            line.append(
                f"\n  {GLYPHS['ellipsis']} {t('approval_more', n=rest)}",
                PALETTE["dim"],
            )
        # `y` and `n`/`Esc` are the keys; the words after them are the actions,
        # and they are the interface's (`ModelPicker._body`'s rule).
        line.append("\n")
        line.append("y ", PALETTE["dim"])
        line.append(t("approval_approve"), f"bold {PALETTE['green']}")
        line.append("   n/Esc ", PALETTE["dim"])
        line.append(t("approval_deny"), f"bold {PALETTE['red']}")
        return line

    def _decided_line(self) -> Text:
        line = Text()
        if self._decided:
            line.append(f"{GLYPHS['check']} ", f"bold {PALETTE['green']}")
            line.append(t("approval_approved"), f"bold {PALETTE['green']}")
        else:
            line.append(f"{GLYPHS['failed']} ", f"bold {PALETTE['red']}")
            line.append(t("approval_denied"), f"bold {PALETTE['red']}")
        line.append("  ", PALETTE["dim"])
        line.append(self._name, f"bold {PALETTE['text']}")
        return line

    def plain_text(self) -> str:
        """What this row shows, for the transcript's own `plain_text`."""
        return content_text(self.content)


def _model_meta(choice: ModelChoice) -> str:
    """One model's facts, as the row's trailing note: what it costs and does.

    Two of the three words are the status bar's (`StatusBar.update_status`
    reads the same keys), which is the point: a model that reasons is the same
    fact in both places, and one word for it cannot drift into two.
    """
    parts = []
    if choice.context_window:
        parts.append(t("model_context", n=choice.context_window))
    if choice.thinking:
        parts.append(t("model_thinking"))
    if choice.vision:
        parts.append(t("model_vision"))
    return "  ".join(parts)


class StatusBar(Static):
    """One dim row: who you are, what is happening, and what the keys do."""

    def update_status(
        self,
        *,
        connection: str,
        agent: str = "",
        model: str = "",
        busy: bool = False,
        queued: int = 0,
        context_tokens: int = 0,
        context_window: int = 0,
        thinking: bool = False,
        vision: bool = False,
        tools: int = 0,
        connected: bool = True,
        connecting: bool = False,
    ) -> None:
        parts: list[str] = []
        if agent:
            # `[` is escaped so a name from a config cannot inject markup.
            parts.append(f"[{PALETTE['muted']}]{agent.replace('[', '[[')}[/]")
        if not connected:
            # **A pending connection is not an error.**  Both states have no
            # connection, so both words are the bar's, but only the one that
            # was tried and failed wears red — a normal start used to flash
            # `connecting` in the error colour, which read as a startup
            # failure nobody could find in a log.
            colour = PALETTE["dim"] if connecting else PALETTE["red"]
            parts.append(f"[{colour}]{connection}[/]")
        elif busy:
            parts.append(
                f"[{PALETTE['amber']}]{t('status_working')}{GLYPHS['ellipsis']}[/]"
            )
        # Messages the user has sent that have not had their turn yet.  Shown
        # because they are deliberately *not* in the transcript until they run:
        # a queued message appearing above the answer still being written would
        # misreport the order the turns actually happened in.
        if queued:
            parts.append(f"[{PALETTE['dim']}]{t('status_queued', n=queued)}[/]")
        if model:
            parts.append(f"[{PALETTE['dim']}]{model.replace('[', '[[')}[/]")

        # **The catalogue's size, not the model's loaded list.**  The number
        # beside the model is what toolhub holds — every category, so skills and
        # CLI entries are in it, and so are the rows of a server that is down —
        # and it is the hub's own answer to the last ask, not a count this
        # window keeps (`slife2.libs.turn.events.ToolsCounted`).  Zero is
        # "nobody has said yet" rather than an empty catalogue: the hub always
        # holds its own tools, so the bar says nothing until it knows.
        if tools:
            parts.append(f"[{PALETTE['dim']}]{t('status_tools', n=tools)}[/]")

        # What the model *is*, as opposed to what it is doing — which is why
        # these are dim words beside its name rather than coloured states:
        # `amber` means a turn is running, and a capability that was amber
        # whenever the model happened to reason would read as an activity.
        #
        # Both are worth the space.  Reasoning explains the pause before an
        # answer and the block above it, and vision is the one thing a person
        # has to know *before* typing — a picture the person names is either
        # going to work or be refused, and the config already knows which.
        if thinking:
            parts.append(
                f"[{PALETTE['dim']}]{GLYPHS['thinking']} {t('model_thinking')}[/]"
            )
        if vision:
            parts.append(f"[{PALETTE['dim']}]{GLYPHS['vision']} {t('model_vision')}[/]")

        # The *last* turn's tokens, not a running total: what a context
        # percentage is a percentage of is the conversation as it now stands,
        # and a sum over every turn is a number that only ever grows.
        #
        # Shown from zero when the model has a window, rather than appearing
        # with the first answer: an indicator that is absent until it has
        # something to say is one nobody knows is there, and the window is a
        # fact about the model rather than about the turn.
        if context_window:
            used = context_tokens / context_window * 100
            colour = PALETTE["amber"] if used >= 80 else PALETTE["dim"]
            parts.append(
                f"[{colour}]{GLYPHS['up']} {context_tokens:,} ({used:.1f}%)[/]"
            )
        elif context_tokens:
            # No window is configured for this model, and inventing one would
            # render a percentage that means nothing.
            parts.append(
                f"[{PALETTE['dim']}]{GLYPHS['up']} {t('tokens', n=context_tokens)}[/]"
            )
        # Both spellings of the cancel, because they are not interchangeable:
        # the copy actions get first refusal on ctrl+c, and escape is the one
        # that stops the turn and cannot do anything else — nothing else claims
        # it, and it never quits.
        #
        # **What is named here is what reaches the app in every terminal, and
        # that is the whole test.**  ctrl+c is the one control key nothing
        # intercepts, and it is what a person reaches for to leave; ctrl+s is
        # for the switch, "s" for switch, and it is not ctrl+g because VS Code
        # keeps that one; ctrl+o is the picker, "o" for open, and no
        # terminal claims it.  Ctrl+Q would quit too — Textual binds it — and
        # it is deliberately not named: a terminal with flow control eats it as
        # XON, so the bar would be promising a key that arrives nowhere.  A bar
        # read by somebody whose key does nothing is worse than a bar that says
        # less.
        parts.append(
            f"[{PALETTE['dimmest']}]{GLYPHS['separator']} {t('status_keybinds')}[/]"
        )
        self.update("  ".join(parts))
