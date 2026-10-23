"""Central registry for every time value in slife2.

All time values live HERE as typed dataclass defaults — this module is the single
source of truth.  There is no config section for them and no second seat: a
developer tunes a value by editing the dataclass default and its comment, and a
structurally invalid edit fails loudly at import (:func:`validate`).

Two kinds of value share the file, because one file answering "what is this
number and who owns it" beats two:

- **budgets** (``work`` / ``ready`` / ``grace`` / ``transport`` / ``storage`` /
  ``deliver``) bound a single await — **the owner-of-await rule**: the layer that
  awaits owns the bound, and a callee never sets a total for its caller.
- **cadences** (``pacing``) are how OFTEN something runs — a poll period, a
  heartbeat, a backoff step, a session lifetime.  A cadence never bounds an await
  and a budget never sets a cadence; the role keeps that visible.

CONVENTION (enforced by ``tests/test_no_magic_timeouts.py``):

- consumers do **call-time lookups** — ``_T.timeouts.<role>.<key>`` — and NEVER
  import-capture values into module constants or def-time default args, so tests
  can monkeypatch ``slife2.helpers.timeouts.timeouts.<role>.<key>`` at runtime;
- a value that has to stay a literal carries ``# timeouts:`` on the line, and
  the gate's docstring says which literals are exempt outright.

**What is deliberately absent, and why.**
There is **no tool-call budget here**.  v1 had one (``work.tool_budget``, 120 s)
and applied it to every tool that did not declare its own bound; v2 does not, on
the argument that a single number applied to every tool is a number wrong for
most of them — a search and a twenty-minute render are both tools.  The bound a
call has is the one an interested party stated: the model's ``_timeout``, or the
tool's own declared parameter, and otherwise none (``slife2.libs.turn.loop``).

**Every value here is v1's unless its comment says otherwise** — the port kept
the numbers and moved them, which is why the roles are v1's too.  ``timeouts.py``
intentionally imports nothing from ``slife2.*`` (``config.py`` imports us; a
cycle would break both).  stdlib only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields


class TimeoutConfigError(ValueError):
    """A registry value violates a type / non-negativity / invariant rule."""


# ── Role dataclasses — the values themselves (single source of truth) ──


@dataclass
class Work:
    """One await a layer does on its own behalf."""

    #: Inactivity watchdog on a streamed model answer: seconds of silence before
    #: the call is declared stalled.  **Not a total** — reset by every chunk, so
    #: a slow-but-live generation is never cut and a provider that answered 200
    #: and went quiet is (`slife2.clients.model`, v1's `work.stall`).
    stall: float = 120.0
    #: One call to the context plugin.  Long enough for `restore` (a whole
    #: context replayed) and `rebuild` (a recall that may embed a query), and
    #: for `remember` to embed and write a long turn.  **Raised from 120 s on
    #: 2026-10-23**: a `winning-avg-corewars` turn's save outlasted 120 s with one
    #: window and no second client, and the whole turn was rolled back as
    #: recorded-nowhere.
    context: float = 300.0
    #: One *ask* of the toolhub by this process — the tool list, and the trim at
    #: a turn boundary.  **Not a tool call**: that is the loop's, and nothing
    #: here bounds it (see the module docstring).  Must outlast the hub's own
    #: settle wait (`validate`).
    hub: float = 20.0
    #: One *catalogue build* at start-up: the first ask that makes a home's
    #: `agents/tools.db` (`slife2.libs.host.launcher.open_catalogue`).  **Longer
    #: than `hub` because it is the cold path**: a hub that has just started
    #: must reach every plugin, ask each family for its declaration, and embed
    #: the rows that are new — on a machine that may still be starting nineteen
    #: servers.  Measured on the slow machine (2026-10-21): 19 servers took
    #: 170 s, and a 20 s ask expired while the hub was still declaring.  A
    #: turn's ask stays `hub`; this budget is spent once, and a start-up that
    #: gives up on it leaves the window with no catalogue at all.  `validate`
    #: keeps it >= `hub`.  **Raised from 240 s on 2026-10-23** for the same cold
    #: start: with two task containers running, the plugins and the external
    #: sources can take longer than four minutes to declare.
    catalogue: float = 360.0
    #: One management call from the terminal to the agent server: `model_switch`,
    #: `reset`, `transcript`.  A question answered from memory or from the store.
    #: **Not the turn** — a turn's length is the answer's own business, and the
    #: person watching has Esc (`slife2.tui.client`).
    management: float = 30.0
    #: The pre-turn recall discriminator: how long the model it asks may take
    #: before the turn runs on the context it already has.  `config.py` reads
    #: this as the default of `context.timeout` — the one seat for the number,
    #: with the config as an override rather than a second copy.
    recall_discriminator: float = 20.0
    #: The per-request clock on the *model client* a plugin makes its own calls
    #: with (the context plugin's discriminator).  Larger than the bound its
    #: caller puts around it, deliberately: the caller's is the operative one,
    #: and this exists so a hung provider cannot park the plugin.
    model_call: float = 600.0


@dataclass
class Ready:
    """Establishing a peer, and one-off probes of one."""

    #: How long a plugin may take to come up.  Generous: the launcher refuses to
    #: start at all if one will not, so a short bound here would be a system that
    #: refuses to boot on a slow machine.  **Raised from 90 s on 2026-10-23**: in
    #: a fresh task container, `schedule` (the last server up, after the hub's
    #: catalogue) took ~90 s with one other trial running and was killed at the
    #: old bound; its port did answer, so this is a cold start, not a hang.
    plugin_start: float = 300.0
    #: A TCP probe of an endpoint (is anything listening).
    probe: float = 2.0
    #: How long a second `slife2` waits on the claim file before deciding the
    #: running process is not using it.  Short: it is a mutex, not a wait.
    agent_claim: float = 0.2
    #: Waiting on a child that has been asked to stop.
    reap: float = 5.0
    #: One of *our own* hops connecting.  Loopback to a daemon this system
    #: started, so this bounds a port that listens and never answers.
    connect_ours: float = 10.0
    #: A `mcp:` entry connecting.  Somebody else's process, possibly an `npx`
    #: fetching itself, so it gets far more patience than our own hops.  **180 s,
    #: not 60**: on the slow machine (2026-10-21) a cold
    #: `@aryanbv/pdf-toolkit-mcp` install took 138 s, and the old 60 s bound
    #: killed it mid-install — leaving a half-written npx cache whose missing
    #: files then failed every later start (`combined-stream/lib/…`,
    #: `color-convert/route.js`).  A connect that is killed while somebody
    #: else's package manager is writing is worse than one given time to finish.
    connect_tools: float = 180.0
    #: How long a listing waits for connects it started, before answering with
    #: what is in hand.  "Not answering yet" and "absent" are the same answer.
    list_settle: float = 5.0
    #: The a2a broker coming up.
    broker: float = 10.0
    #: One probe of an a2a peer, local and remote.
    probe_local: float = 0.25
    probe_remote: float = 5.0
    #: One probe of a tunnel child's own metrics endpoint.
    probe_endpoint: float = 5.0
    #: A file tunnel publishing, and one read of the URL it published.
    tunnel_start: float = 45.0
    tunnel_read_url: float = 30.0
    #: How long a published tunnel may stay unreachable from the edge before the
    #: monitor respawns it.  **Must outlast `tunnel_read_url`** (`validate`): a
    #: child gets at least the patience a fresh start gets, or the monitor would
    #: respawn one that was never given time to heal.
    tunnel_heal: float = 180.0
    #: The WeChat login's QR flow, from code shown to code scanned.
    qr_deadline: float = 600.0


@dataclass
class Grace:
    """Asking something to stop, and then making it."""

    #: After a stop request, how long a process has to go quietly before the
    #: force is used.  Blunt on purpose: these are our own daemons, they have no
    #: console to catch a signal and no state worth flushing.
    kill: float = 5.0
    #: How long a daemon's exit waits for work it detached — a write that is
    #: already in flight.  Bounded, because a store that has stopped answering
    #: must not hold up a shutdown.
    drain: float = 5.0
    #: How long one attempt at a lock waits before giving up.
    lock: float = 30.0


@dataclass
class Transport:
    """Per-request clocks on somebody else's HTTP API."""

    #: One media request, from connect to last byte.  Generous: a synchronous
    #: render is a long POST.
    media_request: float = 180.0
    media_connect: float = 30.0
    #: Downloading the artifact a provider hands back.
    media_download: float = 300.0
    #: How long an asynchronous media job may take before the *wait* gives up.
    #: The job keeps running on the provider's side — the message says so.
    media_deadline: float = 1200.0
    #: One embedding request, and the same one through the API server.
    embed: float = 60.0
    embed_api: float = 30.0
    #: One request to the WeChat backend.
    wechat_request: float = 120.0
    #: Handing a message to a window: it returns when the words are down the
    #: stream, not when the turn they started ends.
    window: float = 10.0
    #: One call to the schedule plugin.
    tasks: float = 30.0


@dataclass
class Storage:
    """Waiting on a local store rather than on a peer."""

    #: SQLite's own busy timeout: how long a writer waits for the writer holding
    #: the database (seconds here; the stores convert to the PRAGMA's ms).
    sqlite_busy: float = 5.0
    #: Waiting for a config file's write lock.
    filelock: float = 30.0


@dataclass
class Stream:
    """Retries, which are counts and not durations — here for the same reason
    v1 kept them here: one file answers "what is this number", whatever it is."""

    #: The embeddings server's.  Zero: an embedding that failed is a caller's
    #: problem to retry, not the server's to repeat.
    retries: int = 0
    #: Handing a message to a window (`slife2.libs.harness.handover`): how many
    #: times it is offered before the offer is a failure.  Three, because what
    #: the retry is for is the window's own gap — an agent's window attaches a
    #: moment after its stack comes up — and the pause between attempts is
    #: `pacing.reconnect`, which is the terminal's and not this count's.
    #:
    #: **A count, so it is here rather than in `pacing`**: the roles are the
    #: registry's own rule, and a number that says "how many" is not a cadence
    #: however close to one it sits.  It was a module constant beside the loop
    #: that uses it, which is the second seat this file exists to be the only
    #: one of.
    handover: int = 3


@dataclass
class Deliver:
    """Handing something to a peer over a channel that can drop it."""

    #: The a2a mesh: connecting to the broker, and the three pieces of its own
    #: wire contract — a peer's card is read in this long, a first reply is
    #: waited for this long, and silence past this long means the link is dead.
    mqtt_connect: float = 15.0
    name_probe: float = 1.0
    reply_first: float = 15.0
    keepalive: float = 25.0


@dataclass
class Pacing:
    """Cadences — how often something runs, never how long an await may block.

    Every value here is a period between two events (a poll, a heartbeat, a
    backoff step) or a lifetime after which a cached thing is stale.
    """

    #: Between polls of the WeChat backend, and how far that poll's backoff
    #: grows when the backend keeps failing.
    wechat_poll: float = 3.0
    wechat_backoff_max: float = 30.0
    #: How long one WeChat session may live before it is logged in again.
    wechat_session_max_age: float = 82800.0  # 23 h
    #: The QR status check while a login waits for a scan.
    qr_poll: float = 2.0
    #: The typing indicator: how often it is refreshed, and how long the
    #: keepalive may run before it gives up on the agent ever answering.
    typing_refresh: float = 8.0
    typing_max_lifetime: float = 300.0
    #: A message that repeats within this is the same message — the WeChat
    #: backend redelivers, and answering twice is worse than answering once.
    wechat_dedup: float = 30.0
    #: The due-task poll.  Cron's unit is a minute, so this fires within half of
    #: one of the due time.
    schedule_poll: float = 30.0
    #: A fire whose due time is within this of "now" is freshly due; older, it
    #: was missed while slife2 was down.
    miss_grace: float = 120.0
    #: How often a tunnel child is checked for life, and the delay before a
    #: spawn that failed is tried again.
    tunnel_health: float = 30.0
    tunnel_retry_delay: float = 2.0
    #: The media plugin's poll of an asynchronous provider job.  The providers'
    #: own examples use fifteen seconds.
    media_poll: float = 15.0
    #: Re-checking a file another process still holds open, while something waits
    #: for it to let go (`slife2.plugins.agent.server._remove_tree`, where a
    #: subagent's directory is deleted).  Short, because the holder is a write
    #: that has been committed and not yet flushed — milliseconds — and the wait
    #: around this is `storage.sqlite_busy`, which is the budget for a file
    #: somebody else has.
    file_release: float = 0.2
    #: The a2a mesh's own poll and its retry delay, and the broker's startup
    #: poll.
    broker_poll: float = 0.1
    mesh_retry_delay: float = 1.0
    #: The terminal's pause before offering to attach again.
    reconnect: float = 2.0
    #: Between two attempts at a local lock.
    lock_poll: float = 0.1
    #: How long a tool server's listing is trusted without being read again.  A
    #: policy number rather than a measurement: a session is hours long and a
    #: server's tool set changes about as often as it is restarted.
    relist_after: float = 300.0
    #: How long a conversation survives with nothing asked of it, before the
    #: agent server lets its live list go.  Housekeeping, not a lifetime a caller
    #: can observe: the next message under the same key starts it again.
    loop_idle: float = 1800.0


@dataclass
class Timeouts:
    work: Work = field(default_factory=Work)
    ready: Ready = field(default_factory=Ready)
    grace: Grace = field(default_factory=Grace)
    transport: Transport = field(default_factory=Transport)
    storage: Storage = field(default_factory=Storage)
    stream: Stream = field(default_factory=Stream)
    deliver: Deliver = field(default_factory=Deliver)
    pacing: Pacing = field(default_factory=Pacing)


# ── Validation ──────────────────────────────────────────────────────────


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(ts: Timeouts) -> list[str]:
    """Return a list of human-readable violations (empty when valid).

    **The invariants are the point of this function**, and each one is a claim
    the code makes about two clocks rather than about one.  A value nobody
    relates to another is checked for being a sane number and left at that: what
    the registry can assert is the relations, because those are the ones that go
    wrong silently — an outer bound shorter than an inner one is an inner bound
    that never fires, and a timer that fires while the thing it bounds is still
    legitimately running.
    """
    errs: list[str] = []
    for role, obj in (
        ("work", ts.work),
        ("ready", ts.ready),
        ("grace", ts.grace),
        ("transport", ts.transport),
        ("storage", ts.storage),
        ("stream", ts.stream),
        ("deliver", ts.deliver),
        ("pacing", ts.pacing),
    ):
        for f in fields(obj):
            v = getattr(obj, f.name)
            if f.name == "retries":
                if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
                    errs.append(
                        f"timeouts.{role}.{f.name} must be a non-negative int, got {v!r}"
                    )
                continue
            if not _is_number(v):
                errs.append(
                    f"timeouts.{role}.{f.name} must be a number, got {type(v).__name__}"
                )
                continue
            if v <= 0 or not math.isfinite(v):
                errs.append(
                    f"timeouts.{role}.{f.name} must be finite and > 0, got {v!r}"
                )
    if ts.work.hub < ts.ready.list_settle:
        # The agent's patience for the hub has to outlast the hub's own brief
        # wait for tool servers that are still connecting, or "waiting for a
        # server that is starting" arrives as a failed turn.
        errs.append(
            "invariant: work.hub >= ready.list_settle (a listing's wait is "
            "inside the asker's patience)"
        )
    if ts.work.catalogue < ts.work.hub:
        # The cold build is the same ask as a turn's, with the starting and
        # embedding on top; a shorter budget would fail the start first.
        errs.append(
            "invariant: work.catalogue >= work.hub (a catalogue build is a "
            "hub ask with the cold work in it)"
        )
    if ts.ready.tunnel_heal < ts.ready.tunnel_read_url:
        # A running child gets at least the patience a fresh start gets, or the
        # monitor would respawn one that was never given time to heal.
        errs.append("invariant: ready.tunnel_heal >= ready.tunnel_read_url")
    if ts.ready.plugin_start < ts.ready.probe:
        errs.append(
            "invariant: ready.plugin_start >= ready.probe (a start is longer "
            "than a look)"
        )
    if ts.ready.connect_tools < ts.ready.connect_ours:
        errs.append(
            "invariant: ready.connect_tools >= ready.connect_ours (somebody "
            "else's process gets at least what ours gets)"
        )
    if ts.transport.media_request > ts.transport.media_deadline:
        # The *wait* for an asynchronous job is longer than one synchronous
        # request by design — this guards the reverse of that claim.
        errs.append("invariant: transport.media_request <= transport.media_deadline")
    if ts.pacing.typing_max_lifetime < ts.pacing.typing_refresh:
        errs.append("invariant: pacing.typing_max_lifetime >= pacing.typing_refresh")
    if ts.pacing.wechat_backoff_max < ts.pacing.wechat_poll:
        errs.append(
            "invariant: pacing.wechat_backoff_max >= pacing.wechat_poll (a "
            "backoff that cannot grow is not one)"
        )
    return errs


def checked(ts: Timeouts) -> Timeouts:
    """Validate *ts* and raise :class:`TimeoutConfigError` on any violation.

    The raising counterpart of :func:`validate` — used at import for the
    singleton, and by tests asserting that a bad edit fails loudly.
    """
    errs = validate(ts)
    if errs:
        raise TimeoutConfigError("; ".join(errs))
    return ts


#: The values every caller reads.  A structurally invalid edit fails at import,
#: which is the only moment a wrong number can be caught before it is used.
timeouts = checked(Timeouts())
