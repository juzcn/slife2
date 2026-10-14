"""The Textual application.

Four things here are worth knowing before changing them:

1. **The MCP client is created in `on_mount`, never in `__init__`.**  Its anyio
   task group has to live in the app's event loop, and `__init__` runs before
   there is one.
2. **Submissions queue, and only one turn is in flight at a time.**  The loop on
   the server would accept them concurrently and run them in order, but the
   *transcript* could not: a turn's last delta and its result travel on one
   response while the next turn's first delta travels on another, and nothing
   orders those two arrivals.  Sending one at a time makes the transcript's
   order a property of the code rather than of a race — and it also means the
   client's timeout measures a turn rather than a wait, which is what a timeout
   should measure.  What the server's own inbox is for is the *other* caller:
   any peer that sends to a busy loop is queued there and never loses its turn.
3. **Every event is stamped with the turn it belongs to.**  Textual delivers
   events as queued messages, so a delta from the turn that just finished can be
   handled after the *next* turn's block is already open — and land in it.  The
   ticket is what makes that a dropped fragment instead of a wrong transcript.
4. **`ctrl+c` cancels the running turn, never the queue.**  The message the user
   is still waiting on must not go with the one they gave up on.  With no turn
   running it quits, because a user cannot stop a runaway turn otherwise.  It is
   also the copy key — a selection in the prompt or in the transcript is copied
   and nothing is cancelled — and that first refusal is why `escape` is bound to
   the cancel alone: the interrupt that always fires, and the one that cannot
   close the window.

**And another process can hand this window a message**, which is how a channel
like WeChat reaches the screen: the message goes into the same queue the person's
own typing goes into and is run as the same kind of turn, so there is one path and
one display for both.  It arrives on a call this window *holds open* to
`slife2-tui` — the plugin that owns a session per agent — and the channel rides
along, so the line wears the right bubble and the turn is recorded with where it
came from.

**And the window can be run with no terminal** (`slife2 --headless`), which is
the one thing this module deliberately does nothing about: there is no `headless`
field here and no branch, because a run with nobody in front of it is not a
different kind of window.  It is the same three connections, the same queue, the
same turns, the same prompts; the driver writes nothing and reads nothing, and
that is the whole of the difference.  What tells a *model* that nobody is there
is its system prompt (`AgentClaim.headless`, rendered into the template by
`slife2.plugins.agent`), because that is the lever for how an agent behaves — a branch
in here would be a second rule for a question the prompt already answers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastmcp.exceptions import ToolError
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message as TextualMessage
from textual.worker import Worker

import slife2.helpers.timeouts as _T
from slife2.libs.host.config import DEFAULT_AGENT, Config
from slife2.libs.turn.events import (
    ContextChosen,
    TextDelta,
    ThinkingDelta,
    ToolApprovalRequested,
    ToolCallFinished,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
)
from slife2.plugins.tui.server import NOTE, Handover
from slife2.tui import notes
from slife2.tui.client import (
    AgentClient,
    MCPAgentClient,
    MCPMeshClient,
    MCPWindowClient,
    MeshClient,
    WindowClient,
    refused,
)
from slife2.tui.restore import restore
from slife2.tui.theme import css_variables
from slife2.tui.widgets import (
    WINDOW_CHANNEL,
    ApprovalPrompt,
    ChatView,
    HistoryInput,
    ModelChoice,
    ModelPicker,
    StatusBar,
)

logger = logging.getLogger(__name__)

#: How long to wait before offering to attach again, when the plugin that hands
#: this window its messages is not answering.  A pause rather than a spin: the
#: usual reason is that it has not started yet or is being restarted, and both
#: are over in a second or two.


class TurnEventMessage(TextualMessage):
    """A turn event, carried from the client callback into Textual's handling."""

    def __init__(self, ticket: int, event: TurnEvent) -> None:
        self.ticket = ticket
        self.event = event
        super().__init__()


def context_of(turns: Sequence[Mapping[str, Any]]) -> int:
    """How large the conversation was, from the turns a window has just drawn.

    **The same quantity a live turn reports, and the only place it can come from
    on a window that has run no turn.**  `_context_tokens` is set from
    `TurnFinished`'s `last_usage`, so a window that has only restored kept its
    initial zero — a conversation the server had just rebuilt from fourteen turns
    showed `0 (0.0%)` directly under a note saying fourteen turns had come back.

    `TurnRecord.context_tokens` is documented as exactly this question ("how
    large the conversation had become by the end of it — the number the *next*
    request would resend"), so the bar reads one quantity either way.  A turn
    stored before that column meant anything answers 0, which is what the bar
    says for a size nobody has measured rather than a guess from an older turn.
    """
    if not turns:
        return 0
    value = turns[-1].get("context_tokens")
    return value if isinstance(value, int) and value > 0 else 0


def configured_models(config: Config) -> list[ModelChoice]:
    """Every model the config defines, as the picker's rows.

    Read here, at the moment the window opens, rather than by the window
    itself: `main` is the party that already knows where the config is, and a
    widget that went looking for the file would be a second answer to "which
    models are there" — from a list the server has not seen.  v1 held its
    models the same way.

    **The reference is built the way `Config.resolve` builds it**, so what a
    row names and what `model_switch` accepts are one string rather than two
    that agree today.  The order is the file's, provider by provider and model
    by model: a picker that sorted would open on a row the config does not lead
    with, and the config is the operator's own ordering.
    """
    return [
        ModelChoice(
            ref=f"{provider}/{settings.model}",
            # The config's `name:` when it has one, and empty when it does not,
            # so a row shows a name only where somebody wrote one.
            label=settings.name,
            context_window=settings.context_window,
            thinking=settings.reasoning,
            vision=settings.accepts_images,
        )
        for provider, entry in config.providers.items()
        for settings in entry.models.values()
    ]


class SlifeApp(App[None]):
    """The conversation window."""

    CSS_PATH = "app.tcss"

    BINDINGS = [
        # Deliberately *not* priority.  The priority pass runs before the
        # focused widget, so binding ctrl+c here with `priority=True` takes the
        # key from the two copy actions that want it first: the focused
        # `TextArea`'s ("copy the selection") and the screen's ("copy the mouse
        # selection").  Both skip themselves when there is nothing selected,
        # and that skip is what lands the key here — so ctrl+c copies when
        # there is a selection and interrupts when there is not.
        Binding("ctrl+c", "interrupt", "Cancel or quit", show=False),
        # Escape is the cancel and only the cancel; see `action_cancel`.  The
        # two keys are separate actions rather than one because escape must not
        # be able to close the window — it is the interrupt to reach for while
        # something is selected, and that has to be safe to press.
        Binding("escape", "cancel", "Cancel", show=False),
        # The switch, and the key is v1's — `s` for switch.  Not ctrl+m, which
        # Textual aliases to enter, and not ctrl+g, which VS Code keeps for its
        # own goto-line.  What it opens is the picker; what applies the choice
        # is the *tool* the model calls (`model_switch`), so a person and a
        # model switching a model are one behaviour with one implementation
        # rather than two that can drift apart.
        Binding("ctrl+s", "switch_model", "Switch model", show=False),
        # Home/End go to the transcript rather than to the draft, and
        # `priority` is what makes that true: the priority pass runs *before*
        # the focused widget, and the focused widget is the prompt, whose
        # `TextArea` binds both keys to the ends of the line the cursor is on.
        # TextArea keeps the Emacs spelling of each (Ctrl+A / Ctrl+E), so the
        # bargain slife v1 made — the transcript gets the obvious keys, the
        # draft keeps a way to reach the line's ends — is affordable.
        # PageUp/PageDown need no binding here: they are taken on the prompt
        # itself, because `TextArea` claims them and nothing else can.
        Binding("home", "scroll_home", "Scroll to top", priority=True),
        Binding("end", "scroll_end", "Scroll to bottom", priority=True),
    ]

    def __init__(
        self,
        url: str,
        *,
        agent: str = DEFAULT_AGENT,
        model: str = "",
        model_label: str = "",
        context_window: int = 0,
        thinking: bool = False,
        vision: bool = False,
        models: Sequence[ModelChoice] = (),
        control_url: str = "",
        mesh_url: str = "",
        client_factory: Callable[[], AgentClient] | None = None,
        window_factory: Callable[[], WindowClient] | None = None,
        mesh_factory: Callable[[], MeshClient] | None = None,
    ) -> None:
        # `App.__init__` takes no title, so it is assigned after the fact; the
        # attribute is reactive and the header picks it up.
        super().__init__()
        # The agent label is the window title.  It changes nothing else: there is
        # no per-agent port, process or config, because the servers are shared
        # infrastructure and an identity is not a piece of topology.
        self.title = f"slife2 - {agent}"
        # (The address is *not* kept as an attribute: the client factory below
        # closes over the parameter, so `self._url` was a second spelling of one
        # fact that nothing ever read.)
        self._agent = agent
        self._model = model
        self._model_label = model_label or model
        #: The model's budget, from its config, for the percentage in the status
        #: bar.  Zero when the config does not say, and then no percentage is
        #: shown rather than one against a number somebody invented.
        self._context_window = context_window
        #: The model's own two capabilities, also from its config: it reasons
        #: natively, and it can be shown a picture.  Both are facts about the
        #: model rather than about a turn, which is why they are read once here
        #: instead of being reported by the server per turn — the TUI is what
        #: decides `@image` is worth offering, and the server is what refuses one
        #: a model cannot read.
        self._thinking = thinking
        self._vision = vision
        #: The models the picker offers: the config's, as it was when this
        #: window opened.  Held rather than re-read at the key, because the
        #: window is not a reader of that file — and because a list that grew
        #: under a picker the person is looking at would move the row under the
        #: cursor in the middle of choosing it.
        self._models = list(models)
        #: Whether a picker is on screen.  A second ctrl+s would stack a second
        #: one and leave the first's future unresolved, so the second press is
        #: the one that does nothing — v1's guard, and v1's reason for it.
        self._switching = False
        #: The approval row on screen, if one is.  Held so a cancelled turn can
        #: refuse the call it was holding: the loop is parked on that future, and
        #: a prompt nothing ever answers would keep the turn from ending.
        self._approval: ApprovalPrompt | None = None
        #: Where this window *attaches* so a message arriving on another channel
        #: can be handed to it (`slife2.plugins.tui.server`) — the plugin's address, and
        #: the window is its client.  Empty means no inbox, which is what a test
        #: wants and what a window started without a config address gets.
        self._control_url = control_url
        #: Where this window puts its agent *on the A2A mesh*, and the two halves
        #: of that are the same object as everywhere else here: the window is the
        #: mesh's client, and holding the call is what keeps the agent online
        #: (`slife2.tui.client.MeshClient`).  Empty means this machine is not on
        #: the mesh at all — no `a2a:` section — and then nothing is held and no
        #: line is drawn, which is the whole of what the absence means.
        self._mesh_url = mesh_url
        #: Injectable so the TUI can be driven by a scripted client, with no
        #: server and no network.
        self._client_factory = client_factory or (
            lambda: MCPAgentClient(url, agent=agent, model=model)
        )
        #: The other half, and injectable for the same reason: the connection
        #: this window *receives* on, held open for as long as it is up.
        self._window_factory = window_factory or (
            lambda: MCPWindowClient(control_url, agent=agent)
        )
        #: And the third: the mesh membership this window holds for its agent.
        self._mesh_factory = mesh_factory or (
            lambda: MCPMeshClient(mesh_url, agent=agent)
        )
        self._client: AgentClient | None = None
        self._window: WindowClient | None = None
        self._mesh: MeshClient | None = None
        self._connected = False
        self._connection_text = "connecting"
        #: Whether the previous conversation is drawn, or being drawn.  Once
        #: per window: reading it again would append a second copy of a history
        #: the transcript is already showing.  **Set when the draw is finished**,
        #: not when it starts, so it answers "is the history on the screen" —
        #: which is the question `_ensure_connected` and `_drain` have to ask.
        self._restored = False
        #: Serialises the read that `_restored` reports on.  The flag alone is
        #: not enough: it is set at the *end* of `_restore`, so two callers
        #: arriving while a read is in flight both see it unset, and one of them
        #: going on to mount a turn is the interleaving this lock prevents.  The
        #: second caller waits for the first and then finds the flag set, which
        #: is the ordering `_ensure_connected` promises — see `_restore`.
        self._restoring = asyncio.Lock()
        #: Messages waiting for their turn, oldest first, each with the channel
        #: it came in on.  **Typing is not the only way in**: a message handed
        #: over by another channel lands here too (`deliver`), which is what makes
        #: a WeChat message the same thing as one typed here.  A message is added
        #: to the transcript when its turn starts rather than on arrival, so the
        #: transcript stays a faithful record of the turns that happened, in the
        #: order they happened.
        self._queue: list[tuple[str, str]] = []
        self._worker: Worker[None] | None = None
        self._draining = False
        #: The worker serving the control surface, held so unmounting stops it.
        self._control: Worker[None] | None = None
        #: The worker holding this agent's place on the mesh, held for the same
        #: reason and with the same consequence: cancelling it is the departure.
        self._held_mesh: Worker[None] | None = None
        #: The running turn, as its own task so that cancelling one leaves the
        #: queue alone.  Cancelling the worker would take the queue with it.
        #: It resolves to the turn's answer, which the queue applies before the
        #: next turn opens a block.
        self._turn: asyncio.Task[str] | None = None
        #: Whether the cancellation in flight was ours.  Without it there is no
        #: way to tell "the user stopped this turn" from "the app is closing",
        #: and they want opposite handling.
        self._interrupting = False
        #: Which turn's events the transcript is currently accepting.
        self._ticket = 0
        self._context_tokens = 0
        self._steps = 0

    def compose(self) -> ComposeResult:
        # The transcript signs its messages with the agent name, so a
        # conversation reads `You> ...` / `jack> ...` on both sides.
        yield ChatView(self._agent)
        yield HistoryInput(
            # The agent's name, not the program's: the prompt is addressed to
            # whoever this instance is, and it is the one place the name is
            # read before anything has been said.
            placeholder=f"Message {self._agent}…",
            id="user-input",
            # `focus` rather than `indent`: Tab in a prompt should reach the
            # rest of the app, not insert whitespace nobody can see.
            tab_behavior="focus",
        )
        yield StatusBar()

    def get_css_variables(self) -> dict[str, str]:
        """Publish the palette to the stylesheet.

        This is what keeps the design in one place: `app.tcss` says
        `$slife-amber`, the widgets say `PALETTE["amber"]`, and neither owns a
        hex value.  slife v1 duplicates them, which is how a stylesheet and the
        code that renders into it drift apart.
        """
        return {**super().get_css_variables(), **css_variables()}

    # --- lifecycle -----------------------------------------------------------

    async def on_mount(self) -> None:
        self._client = self._client_factory()
        self.query_one(HistoryInput).focus()
        self._refresh_status()
        if self._control_url:
            self._window = self._window_factory()
            self._control = self.run_worker(
                self._attach_inbox(), group="control", exit_on_error=False
            )
        if self._mesh_url:
            self._mesh = self._mesh_factory()
            self._held_mesh = self.run_worker(
                self._hold_mesh(), group="mesh", exit_on_error=False
            )
        # Connecting can take a moment; a worker keeps the first paint prompt.
        self.run_worker(self._connect(), group="connect", exit_on_error=False)

    async def on_unmount(self) -> None:
        if self._control is not None:
            control, self._control = self._control, None
            # Cancelling the worker drops the attach call, and the plugin sees
            # that as the window going away — there is no `detach` to send.
            control.cancel()
        if self._held_mesh is not None:
            held, self._held_mesh = self._held_mesh, None
            # The same mechanism on the mesh: cancelling this worker drops the
            # `join` call, and its cancellation is what publishes the agent's
            # offline card.  There is no `leave` to send.
            held.cancel()
        if self._window is not None:
            await self._window.close()
        if self._mesh is not None:
            await self._mesh.close()
        if self._client is not None:
            await self._client.close()

    async def _attach_inbox(self) -> None:
        """Hold this window's session open, so messages can be handed to it.

        A worker of its own, because the call does not return while the window is
        up: `attach` *is* the session (`slife2.plugins.tui.server`), and what arrives on
        it goes through `deliver` — the same method the person's own typing goes
        through, which is the whole of the pass-through.

        Retried rather than fatal, for `_connect`'s reason: a plugin that is not
        answering must not make the window unusable.  What is lost while it is
        down is a message somebody else tried to hand over, and that caller is
        told so by its own failed call.
        """
        assert self._window is not None
        complained = False
        while True:
            try:
                await self._window.connect()
                await self._window.attach(self._take)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a missing inbox is operational
                # Said once rather than once per attempt.  This retries until it
                # works, and a terminal left open beside a stopped plugin would
                # otherwise write the same line every two seconds for as long as
                # somebody is looking at it.
                (logger.debug if complained else logger.warning)(
                    "TUI inbox unavailable: %s", exc
                )
                complained = True
                await self._window.close()
                await asyncio.sleep(_T.timeouts.pacing.reconnect)
            else:
                return

    async def _hold_mesh(self) -> None:
        """Put this agent on the A2A mesh, and hold it there while the window is up.

        A worker of its own, for `_attach_inbox`'s reason exactly: the call does
        not return while the window is up, because the call *is* the membership
        (`slife2.tui.client.MeshClient`).  Cancelling it — which unmounting does —
        is what takes the agent off the mesh, so a window that quits, crashes or
        is killed is reported to every peer as `✗ offline` without anything being
        sent.

        **The agent's own `⚡ online` line is the plugin's, not this worker's.**
        There is nothing here to draw it from: the call succeeds by *not
        returning*, so a window that waited for a return to draw a line would
        report a successful join as a silence.  The plugin draws it — the same
        function that draws every peer's arrival — and this worker's business is
        only what went wrong.

        Retried rather than fatal, for `_connect`'s reason: a plugin that is not
        answering must not make the window unusable, and what is lost while it is
        down is a place on the mesh rather than any words.  A *refusal* — the name
        taken, the broker gone — is said once rather than once per attempt: the
        reason does not change between them, and a terminal left open beside it
        would otherwise write the same line every two seconds.
        """
        assert self._mesh is not None
        complained = False
        while True:
            try:
                await self._mesh.connect()
                await self._mesh.hold()
            except asyncio.CancelledError:
                raise
            except ToolError as exc:
                # The mesh answered and said no.  Worth a line of its own,
                # because it is the one failure the person can act on — a name
                # another instance is holding, a broker that is not running —
                # and because the window would otherwise look exactly like one
                # on a mesh that is working.
                if not complained:
                    self._transcript.add_note(notes.not_on_the_mesh(str(exc)))
                    complained = True
                else:
                    logger.debug("A2A mesh still refusing %s: %s", self._agent, exc)
                await self._mesh.close()
                await asyncio.sleep(_T.timeouts.pacing.reconnect)
            except Exception as exc:  # noqa: BLE001 - a missing mesh is operational
                (logger.debug if complained else logger.warning)(
                    "A2A mesh unavailable: %s", exc
                )
                complained = True
                await self._mesh.close()
                await asyncio.sleep(_T.timeouts.pacing.reconnect)
            else:
                return

    def _take(self, handover: Handover) -> None:
        """Take one hand-over, from the plugin's push.

        A method of its own rather than `deliver` passed straight through,
        because the two say different things: this is the wire's shape arriving
        (`slife2.plugins.tui.server`), and `deliver` is the window's one way in.

        **And this is where the two kinds part.**  A message becomes a turn and
        joins the queue, so it is drawn when its turn starts like anything the
        person typed.  A note is drawn *now*, because that is what it is: a line
        about the moment it describes — a peer arriving, most of them — and one
        that waited behind a running turn would be telling the person about a
        peer who has since left.
        """
        if handover.kind == NOTE:
            self._transcript.add_note(handover.text)
            return
        self.deliver(handover.text, handover.channel)

    def deliver(self, text: str, channel: str) -> None:
        """Take a message from another channel, to run as if it were typed.

        **The whole of the pass-through**: it joins the same queue, is run by the
        same drain as one turn among others, and is drawn by the same call — so
        nothing downstream knows or cares that a person typed it somewhere else.
        The channel travels with it, and that is what the bubble and the turn's
        own record are made of.
        """
        self._queue.append((text, channel))
        self._start_draining()
        self._refresh_status()

    async def _connect(self) -> bool:
        """Connect, recording the outcome.  Never raises.

        A server that is not running is an operational problem, not a fatal one:
        the app stays usable, the status bar says why, and the next prompt tries
        again.
        """
        assert self._client is not None
        try:
            await self._client.connect()
        except Exception as exc:  # noqa: BLE001 - a dead server is operational, not fatal
            self._connected = False
            self._connection_text = f"disconnected: {exc}"
            logger.warning("agent server connection failed: %s", exc)
        else:
            self._connected = True
            self._connection_text = "connected"
        self._refresh_status()
        if self._connected:
            # Here rather than in `on_mount`, because a window opened before the
            # servers were is the ordinary case on a cold start: the connect
            # this ends is the lazy retry, and that is the first moment there is
            # anything to ask.  **It is also before anything is mounted** — see
            # `_ensure_connected`, which is what keeps the history from landing
            # under the prompt that triggered the connect.
            await self._restore()
        return self._connected

    async def _ensure_connected(self) -> None:
        """Connect if this window is not, and restore before returning either way.

        **Called before a turn mounts anything, and that is the whole point.**
        `_restore` draws the conversation this name already had; a window that
        had already mounted the prompt would draw that history *underneath* it,
        so the transcript would read as though the old conversation had been
        pasted into the middle of the new one.

        **Connected is not the same as restored, and this is where the difference
        bites.**  `_connect` sets the connected flag *before* it reads the
        history, so a caller that stopped at "connected" — which this used to —
        would sail past a read that is still in flight and mount its turn under
        history that lands afterwards.  The gap is not exotic: a large
        conversation takes a moment to read, and a prompt typed (or a `deliver`
        from another channel) in that moment is an ordinary thing to happen.  So
        the restored half is awaited here too; `_restore` is single-flight, so a
        window whose history is already up returns from it at once.

        The lazy retry, for the case the window opened before the servers were
        up.  Both halves of the condition matter: the window's own flag says
        whether the last attempt worked, and the client's says whether the
        transport is still there — a turn that failed at the transport closes
        the connection inside `run_turn`, so a window consulting only its own
        flag would stay "connected" to a server nothing is talking to.
        """
        if self._connected and self._client is not None and self._client.connected:
            await self._restore()
            return
        await self._connect()

    async def _require_connected(self) -> None:
        """Connect, restore, and raise when a server still cannot be reached.

        `_ensure_connected` plus the check it leaves to its caller, written once:
        `_send`, `_finish_switch` and `_finish_approval` each had the same two
        statements, and the sentence the raise carries is a fact about this
        window rather than about any one of them — so it is stated here once, with
        the reason the caller wants it: a turn, a switch and an answer all need a
        transport, and the failure reads the same to a person watching.
        """
        await self._ensure_connected()
        if not self._connected:
            raise ConnectionError(
                f"not connected to the agent server ({self._connection_text})"
            )

    async def _restore(self) -> None:
        """Show the conversation this name already had.  Once.

        A restart is not a new conversation.  The server has restored the
        *context* the moment a loop is built, so a window that drew nothing
        would be the only part of the system acting as though the conversation
        were over — it would sit blank while the model answered from a history
        the user could not see.

        **The read is single-flight, and a second caller waits rather than
        returning.**  The flag is claimed at the *end*, under a lock, so it means
        "the history is on the screen" — and a caller that arrives while the read
        is in flight blocks until it is, then finds the flag set and returns.  A
        claim made before the await would let that caller go on to mount a turn
        the history then landed under; that is the ordering `_ensure_connected`
        promises, and keeping it is the reason the lock is here and not a plain
        check.  `connect` is guarded the same way and for the same reason — it is
        the shape of anything with an await between its check and its use.
        """
        if self._restored:
            return
        async with self._restoring:
            if self._restored:
                return
            assert self._client is not None
            try:
                turns = await self._client.transcript()
            except Exception as exc:  # noqa: BLE001 - a window is not worth losing
                # Given back (the flag stays unset) because this is the retry
                # path: a read that failed because the servers were still coming
                # up should be tried again when they are.  Nothing is lost by
                # waiting — the history is the server's, and the conversation
                # continues either way.
                logger.warning("the previous conversation could not be read: %s", exc)
                self._connected = False
                self._connection_text = f"disconnected: {exc}"
                self._refresh_status()
                return
            restore(self._transcript, turns)
            # Set only now, so it reports what has actually happened.  `restore`
            # cannot raise on its own data — it drops a row it cannot read — so
            # the only failure left outside the `try` is the transport's, above.
            self._restored = True
        # The bar was the one part of the window that did not know the
        # conversation had come back — see `context_of`.
        self._context_tokens = context_of(turns)
        self._refresh_status()

    # --- the queue of turns --------------------------------------------------

    @property
    def _transcript(self) -> ChatView:
        return self.query_one(ChatView)

    async def on_history_input_submitted(self, message: HistoryInput.Submitted) -> None:
        self.query_one(HistoryInput).remember(message.text)
        self._queue.append((message.text, WINDOW_CHANNEL))
        self._start_draining()
        self._refresh_status()

    def _start_draining(self) -> None:
        """Be the one worker that runs queued turns, if there is not one already.

        The flag rather than the worker's state: `run_worker` returns before the
        worker has started, so a second submission in the same tick would see a
        worker that is set and not yet running, and start a second one.
        """
        if self._draining:
            return
        self._draining = True
        self._worker = self.run_worker(self._drain(), group="turn", exit_on_error=False)

    async def _drain(self) -> None:
        """Run queued turns, one at a time, until the queue is empty."""
        try:
            while self._queue:
                # **Connected — and therefore restored — before anything is
                # mounted.**  On a cold start this is where the connect
                # happens, and `_restore` draws the conversation this name
                # already had; doing it after `add_user` would put the history
                # between the prompt and its answer.  It waits, too, for a read
                # another caller already has in flight — the mount-time connect
                # sets "connected" before it reads, so this is the call that
                # keeps a prompt typed in that gap below the whole history.
                await self._ensure_connected()
                # Taken off the queue *before* it runs, so the count in the
                # status bar is what is still waiting rather than including the
                # turn the user is already watching.
                prompt, channel = self._queue.pop(0)
                self._ticket += 1
                ticket = self._ticket
                # The channel is the caller's, and `add_user` is what turns it
                # into a bubble and unwraps a marker for a line that came in on
                # somebody else's (`slife2.tui.widgets`).
                self._transcript.add_user(prompt, channel=channel)
                # The reader's own message owns the view: sending it goes to
                # the end and re-arms following, however far up they had read.
                # `add_user` follows the tail like every other mount, and that
                # does nothing for a reader who is not at it — so a message sent
                # from up in the history would be written below the fold.
                self._transcript.jump_to_tail()
                # **Nothing is read out of the prompt here.**  The `@path`
                # markers a person types are resolved by the *agent server* now,
                # which is what lets every caller attach an image and not only
                # this terminal (`slife2.libs.turn.attachments`).  The prompt goes as it was
                # written, markers and all — and the picture arrives by way of
                # `attach_image`, whose answer names it.
                self._transcript.begin_assistant()
                self._steps = 0
                # **The task before the refresh, so the bar can see it.**  The
                # bar's "working" comes from `_turn` being set, and a refresh
                # that ran first would find it `None` and draw an idle bar — for
                # the whole turn, since nothing else refreshes until it ends.
                self._turn = asyncio.create_task(self._send(prompt, ticket, channel))
                self._refresh_status()
                try:
                    final = await self._turn
                except asyncio.CancelledError:
                    if not self._interrupting:
                        raise
                    # **The cancelled turn's ticket is retired here, not left for
                    # the next turn to move past.**  Its events can still be in
                    # the pump — a delta the client posted as it unwound — and
                    # the ticket still matched, so they were accepted.
                    # `append_text` is guarded by `_turn_open`, which the
                    # `ends_turn` below clears, but `add_tool_start` is guarded
                    # by neither: a late `ToolCallStarted` mounted a panel under
                    # `[cancelled]`, as though the turn had gone on.  Retiring it
                    # now is what the next turn's own bump does for the tail of
                    # the turn before it — the cancel just does it early.
                    self._ticket += 1
                    self._transcript.add_note(notes.cancelled(), ends_turn=True)
                except Exception as exc:
                    logger.exception("turn failed")
                    self._transcript.add_error(str(exc))
                else:
                    self._transcript.finish_assistant(final)
                finally:
                    self._interrupting = False
                    self._turn = None
                    self._refresh_status()
        finally:
            self._draining = False

    async def _send(self, prompt: str, ticket: int, channel: str) -> str:
        """Run one turn and return its authoritative answer.

        The answer is returned rather than posted so the queue can apply it
        before the next turn opens a block.  Ordering against the deltas is the
        ticket's job, not the message queue's.

        The prompt is sent as the person wrote it, `@path` markers included: what
        those markers resolve to is the agent server's business now, and a caller
        that resolved them itself would be a second implementation of one rule —
        which is also how an image ends up attached twice.
        """
        assert self._client is not None

        # Lazy retry, and the same call `_drain` makes before it mounts
        # anything — asking the client is what keeps one drop from costing a
        # restart, rather than a window that has to be closed and opened again.
        # Restoring is part of it here too: this is the retry path a cold start
        # takes, and the history has to be up before this turn's block opens.
        await self._require_connected()

        return await self._client.run_turn(
            prompt,
            lambda event: self._on_event(ticket, event),
            channel=channel,
        )

    def _on_event(self, ticket: int, event: TurnEvent) -> None:
        """Called by the client for every progress event.  Does not touch widgets."""
        self.post_message(TurnEventMessage(ticket, event))

    # --- message handlers (the only places widgets are mutated) --------------

    def on_turn_event_message(self, message: TurnEventMessage) -> None:
        # Late fragments of a turn that is over.  Dropping them is what keeps
        # one turn's tail out of the next turn's block — see the module
        # docstring.
        if message.ticket != self._ticket:
            return
        event = message.event
        match event:
            case ContextChosen(kept=kept, recalled=recalled):
                # The discriminator's answer, and the only sight of it there is:
                # one model call decides what the turn runs on, it is the most
                # expensive thing a turn does, and until this arrived nothing in
                # the window said whether it had run, what it kept or what it
                # went back to the log for.  A note rather than a panel,
                # because it is the same kind of thing as "[restored N turns]":
                # something the harness did that the conversation itself does
                # not show — and `notes.kept` is where that pair is written.
                self._transcript.add_note(notes.kept(kept, recalled))
            case TextDelta(text=text):
                self._transcript.append_text(text)
            case ThinkingDelta(text=text):
                self._transcript.append_thinking(text)
            case ToolCallStarted(
                call_id=call_id,
                name=name,
                arguments=arguments,
                step=step,
                max_steps=max_steps,
            ):
                self._transcript.add_tool_start(
                    call_id, name, arguments, step=step, max_steps=max_steps
                )
            case ToolApprovalRequested(call_id=call_id, name=name, arguments=arguments):
                # **The turn is parked on this**, so it is answered off the
                # message pump rather than in it — see `_ask_approval`.
                self._ask_approval(call_id, name, arguments)
            case ToolCallFinished(
                call_id=call_id,
                ok=ok,
                result_preview=preview_text,
                result_chars=chars,
                elapsed_ms=elapsed,
            ):
                self._transcript.add_tool_end(call_id, ok, preview_text, chars, elapsed)
            case TurnFinished(usage=usage, last_usage=last_usage, steps=steps):
                # Two numbers, two questions.  `last_usage` is how large the
                # conversation had become by the end — the only thing a context
                # percentage can be a percentage of.  `usage` is the turn's bill
                # across every model call it took, which is what the line under
                # the answer reports.  Taking the bill for the size reads as a
                # context two or three times fuller than it is on any turn that
                # called a tool, and it was what this did.
                self._context_tokens = last_usage.total_tokens
                self._steps = steps
                self._transcript.set_usage(usage.total_tokens)
                self._refresh_status()

    # --- keyboard ------------------------------------------------------------

    def _cancel_turn(self) -> bool:
        """Cancel the turn in flight, reporting whether there was one.

        The flag is set before the cancel so the drain can tell this
        cancellation from the one an exit or a reset causes; see
        `_interrupting`.
        """
        # **The prompt is refused first.**  The loop is parked on that future,
        # and a row left undecided on a turn that is going away is an answer
        # nobody will ever give — v1's rule, which denied the prompt *because*
        # the turn was being cancelled rather than leaving it for the
        # cancellation to sweep up.  Before the early return, so a row whose
        # turn has already ended is still answered rather than left hanging.
        if self._approval is not None:
            self._approval.action_deny()
        if self._turn is None or self._turn.done():
            return False
        self._interrupting = True
        self._turn.cancel()
        return True

    def action_cancel(self) -> None:
        """Stop the running turn — and nothing else, which is the point.

        Slife's rule, kept: escape is never a way out of the app, so pressing it
        by reflex at the wrong moment costs a cancelled turn at worst and never
        the window.  That is what makes it the interrupt to offer while
        something is selected, where ctrl+c is busy deciding whether to copy.
        """
        self._cancel_turn()

    def action_interrupt(self) -> None:
        """Stop the running turn, or quit when there is nothing to stop.

        Bound to ctrl+c, where the copy actions get first refusal: this runs
        only when there was no selection to copy.  Quitting is what is left
        here rather than in `action_cancel` because a user with no turn running
        and nothing selected is trying to leave.
        """
        if not self._cancel_turn():
            self.exit()

    def action_scroll_home(self) -> None:
        """Scroll the transcript to the top, and stop following the tail.

        `scroll_home` rather than `scroll_to(y=0)`: it states the request
        through the same funnel as every other mover, which is where following
        is decided.
        """
        self._transcript.scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        """Scroll to the bottom *and* follow from there.

        `jump_to_tail` rather than `scroll_end`, because End is the reader
        saying they are done with the history: `scroll_end` would leave
        following to be re-armed by the request's own deferred landing, and a
        token streamed inside that window would find it still off.
        """
        self._transcript.jump_to_tail()

    def action_switch_model(self) -> None:
        """Open the model picker: the list, a cursor, and a future for the choice.

        **Sync, and that is not a style choice.**  A binding action runs inside
        the key handler, so awaiting the choice here would block the message
        pump — and the message pump is what would deliver the very `Enter` that
        resolves the picker.  The choice is awaited by a worker instead, which
        is v1's arrangement and v1's reason for it.

        What the picker decides with is `model_switch`, the tool the model
        itself calls, so there is one implementation of "switch the model" and
        one answer as to whether it worked.
        """
        if self._switching:
            return
        if not self._models:
            # Said rather than shown as an empty picker: a list with no rows is
            # a key that looks broken, and the tool that would fix it is the
            # thing to name.
            self._transcript.add_error("no models are configured; `model_set` adds one")
            return
        future: asyncio.Future[ModelChoice | None] = asyncio.Future()
        picker = self._transcript.add_picker(
            ModelPicker(self._models, self._model, future)
        )
        picker.focus()
        self._switching = True
        self.run_worker(self._finish_switch(future), group="model", exit_on_error=False)

    async def _finish_switch(self, future: asyncio.Future[ModelChoice | None]) -> None:
        """Apply the picker's decision, off the key handler.

        A worker rather than a plain task so the window unmounting cancels it:
        the thing it is waiting on is a key, and nothing is going to press one
        in a window that is closing.
        """
        choice = await future
        self._switching = False
        # The prompt takes the focus back, whether or not anything was chosen:
        # the picker had it while it asked, and what the person did before
        # ctrl+s was type.
        self.query_one(HistoryInput).focus()
        if choice is None:
            return  # cancelled — the picker's own line is the whole answer

        assert self._client is not None
        try:
            await self._require_connected()
            answer = await self._client.switch_model(choice.ref)
        except Exception as exc:  # noqa: BLE001 - a key that raises takes the whole app down
            self._transcript.add_error(f"the model was not switched: {exc}")
            return
        # A refusal rather than a note (`client.refused`), and the difference is
        # the bar: a note would leave it describing a switch that never
        # happened.
        if refused(answer):
            self._transcript.add_error(answer)
            return
        self._transcript.add_note(answer)
        self._apply_model(choice)

    def _ask_approval(self, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        """Put one tool call to the person, and answer it off the message pump.

        **Sync, for the picker's reason** (`action_switch_model`): this runs
        inside the message pump, and awaiting the answer here would block the
        very pump that has to deliver the `y` or the `Esc` that gives it.  The
        answer is awaited by a worker instead.

        **One at a time, structurally.**  The loop runs a batch's calls one after
        another and waits on each answer before the next, so two prompts cannot
        arrive together — v1 needed a lock for this because its batch ran in
        parallel; here the shape of the loop is the guarantee.
        """
        future: asyncio.Future[bool] = asyncio.Future()
        prompt = self._transcript.add_approval(ApprovalPrompt(name, arguments, future))
        self._approval = prompt
        prompt.focus()
        self.run_worker(
            self._finish_approval(call_id, future),
            group="approval",
            exit_on_error=False,
        )

    async def _finish_approval(
        self, call_id: str, future: asyncio.Future[bool]
    ) -> None:
        """Send the answer back to the turn that is waiting.

        A worker rather than a plain task so the window unmounting cancels it:
        the thing it is waiting on is a key, and nothing is going to press one in
        a window that is closing.
        """
        approved = await future
        self._approval = None
        # The prompt takes the focus back, whether or not anything was approved:
        # the row had it while it asked, and what the person did before `y` was
        # type.
        self.query_one(HistoryInput).focus()
        assert self._client is not None
        try:
            await self._require_connected()
            answer = await self._client.answer_approval(call_id, approved)
        except Exception as exc:  # noqa: BLE001 - a key that raises takes the whole app down
            self._transcript.add_error(f"the answer was not delivered: {exc}")
            return
        # **A sentence means nobody was waiting for it**: the turn ended while
        # the prompt was up — a cancel, or another window answering the same
        # call.  Said rather than dropped, because a prompt that silently did
        # nothing looks exactly like one that failed.
        if answer:
            self._transcript.add_note(answer)

    def _apply_model(self, choice: ModelChoice) -> None:
        """Move the bar onto the model the switch just put the conversation on.

        All of it moves together, and that is the point: what the bar says
        about a model is a fact about the *model* — its window, whether it
        reasons, whether it reads pictures — so a bar that kept the old
        window's percentage or the old model's capability marks would be
        describing a model that is not the one answering.

        The reference rather than the config's name, which is what the bar
        shows for a window that has not switched anything: one spelling of
        "which model is this" is worth more than the prettier of the two.
        """
        self._model = choice.ref
        self._model_label = choice.ref
        self._context_window = choice.context_window
        self._thinking = choice.thinking
        self._vision = choice.vision
        self._refresh_status()

    # --- status --------------------------------------------------------------

    def _refresh_status(self) -> None:
        """Derive the bar from what is left to do, not from a worker's state.

        Asking the worker whether it is running is wrong at exactly the moment
        the last refresh happens: it is the final statement of the worker's own
        body, so it sees `RUNNING` for a turn that has already finished, and
        nothing afterwards corrects it — the bar says working until the next
        turn, and then until the one after that.  A queue and a task are the two
        things that actually describe the situation.
        """
        self.query_one(StatusBar).update_status(
            connection=self._connection_text,
            connected=self._connected,
            agent=self._agent,
            model=self._model_label,
            busy=self._turn is not None or bool(self._queue),
            queued=len(self._queue),
            context_tokens=self._context_tokens,
            context_window=self._context_window,
            thinking=self._thinking,
            vision=self._vision,
            steps=self._steps,
        )
