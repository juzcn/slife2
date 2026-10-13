"""The broker: probed at the configured address, started if it is not there,
**fatal if it still is not**, and stopped when the stack that started it goes.

This is the one place this port departs from v1 on purpose, and the departure is
the operator's call rather than a discovery.  v1's rule was that slife "does NOT
spawn Mosquitto — the user must start it", and the presence of a listener *was*
the feature's on/off switch: no broker, no mesh, everything else carried on.  Here
a machine whose config says `a2a:` has said it wants the mesh, so a missing broker
is a missing dependency rather than a preference — the plugin brings one up, and a
broker that will not serve is a start that fails loudly instead of an `a2a_*` tool
surface quietly attached to nothing.

**And it is stopped by the take-down, because otherwise nothing ever would.**  The
first version of this left it running — "a broker is a machine's, not a run's" —
and what that produced in practice was a live mosquitto whose parent process had
died: an orphan that `slife2 down` did not know about, at a port the next start
would then find "already listening" and adopt without owning.  So a broker is
started by `ensure` **and recorded** (`BROKER_NAME`, at the broker's own
`mqtt://` address), and `stop` below reads that record — which is what makes it
stoppable *and* what keeps it out of the take-down when it is somebody else's: no
record, no signalling.  The two halves live in this module because the policy
about whose broker it is (local, concrete address, started by us) is the same
question in both directions.

**A hard failure is the launcher's, and it is the right one.**  `slife2` starts
every plugin together and refuses to start at all if one of them will not come
up (`slife2.mcp_server.open_server`), reporting the child's own log tail — so
raising here arrives as a named server, a reason, and mosquitto's last twenty
lines.  The alternative, starting anyway with a mesh that is not on the mesh, is
the failure this project keeps choosing against: the model would be handed four
tools whose every call refused, and nothing would say why.

**Only a local broker is started.**  `broker.host` naming another machine is a
broker this process cannot bring up — there is no listener to create on somebody
else's host — so an absent remote broker is the hard failure with nothing
attempted, and the message says which of the two situations it is.  Starting a
*local* mosquitto for a remote address would put a broker on this machine's
interfaces that nobody asked for and that the config does not describe.  **And a
wildcard address is not "local" either**: `0.0.0.0` (or `::`, or ``""``) names
every interface, so a broker started for one would be a world-reachable,
unauthenticated one — refused by name rather than started (see :func:`ensure`).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import slife2.timeouts as _T
from slife2.config import A2ASettings
from slife2.paths import runtime_dir
from slife2.runtime import (
    ServerRecord,
    clear_record,
    pid_alive,
    read_record,
    same_process,
    spawn,
    tail_log,
    tcp_listening,
    terminate,
    write_record,
)

logger = logging.getLogger(__name__)

#: The broker binary, and the only provider this build has
#: (`A2ASettings.provider` refuses anything else at config load).
MOSQUITTO = "mosquitto"

#: What the broker is called in the records and in what `stop` reports — the
#: name of a *daemon*, not of the plugin that starts it, because `slife2 down`
#: says this word to somebody deciding whether the machine is clean.
BROKER_NAME = "a2a-broker"

#: The hosts `broker.host` may name and still be *this* machine at one address —
#: the only ones a broker is started for.  **Deliberately without the wildcards**
#: (`0.0.0.0`, `::`, ``""``): mosquitto takes `listener <port> <host>` verbatim,
#: so a wildcard would be started with `allow_anonymous true` on every interface —
#: a world-reachable, unauthenticated broker conjured by an edit that reads as
#: "make it reachable".  A wildcard is refused by name instead (`WILDCARD_HOSTS`,
#: and the refusal in :func:`ensure`).
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Addresses that mean "every interface".  Kept apart from `LOCAL_HOSTS` because
#: "may I start a broker here" and "does this name every interface" are two
#: questions, and answering the second with the remote-host message would send
#: the operator looking for a broker on the wrong machine.
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})

#: The config we hand mosquitto, and **it is not optional on mosquitto 2.x**.
#: From 2.0 the broker listens on loopback only and refuses anonymous clients
#: unless a config says otherwise, so a bare `mosquitto -p PORT` starts, binds,
#: and then rejects every connection this plugin makes — a failure that would
#: arrive as a connect timeout naming nothing.  An explicit `listener` also pins
#: the bind address, which `-p` cannot: without it the broker would listen on
#: every interface while the config says `127.0.0.1`.  The address written here
#: is therefore `broker.host` verbatim, which is safe *because* only a concrete
#: loopback address ever reaches this point (`LOCAL_HOSTS`): a wildcard is
#: refused before a config is written, so this line can never publish the mesh.
_CONFIG = """\
# Written by slife2-a2a.  Safe to delete: it is regenerated on the next start
# that has to bring a broker up, and nothing else reads it.
listener {port} {host}
allow_anonymous true
"""


class BrokerUnavailable(RuntimeError):
    """The broker this config names is not there, and could not be made to be."""


def is_local(host: str) -> bool:
    """Whether *host* is this machine — the question "may I start a broker"."""
    return host.strip().lower() in LOCAL_HOSTS


def is_wildcard(host: str) -> bool:
    """Whether *host* names every interface rather than one address.

    Not "local" and not "remote": starting a broker for it is what would make
    the mesh world-readable, so it is its own answer.
    """
    return host.strip().lower() in WILDCARD_HOSTS


def _probe(host: str, port: int) -> bool | None:
    """Whether something accepts a connection at this address.

    Mirrors `runtime.tcp_listening`'s three answers, so an undetermined probe is
    not read here as a port that is closed; the caller takes `None` where it
    took `False`.  The timeout is chosen by which kind of address it is — a
    local one refuses in microseconds and only needs a guard against a hung
    listener, a remote one has a network in the way — so a caller never has to
    remember that one question costs different amounts at different distances.
    The two numbers live in `slife2.timeouts` (`ready.probe_local` /
    `ready.probe_remote`), which is the one place any of this module's clocks is
    written down.
    """
    timeout = (
        _T.timeouts.ready.probe_local
        if is_local(host)
        else _T.timeouts.ready.probe_remote
    )
    return tcp_listening(host, port, timeout=timeout)


def log_url(settings: A2ASettings) -> str:
    """The key this broker's log file is filed under.

    A `runtime.endpoint_key` digest of a URL, which is the shape `spawn` and
    `tail_log` both take — the scheme is `mqtt` so a broker's log cannot collide
    with the log of an MCP server on the same port.
    """
    return f"mqtt://{settings.broker_host}:{settings.broker_port}"


def _write_config(settings: A2ASettings) -> Path:
    """The mosquitto config for this address, written where the daemons live."""
    path = runtime_dir() / "mosquitto.conf"
    path.write_text(
        _CONFIG.format(port=settings.broker_port, host=settings.broker_host),
        encoding="utf-8",
    )
    return path


def ensure(
    settings: A2ASettings,
    *,
    listening: Callable[[str, int], bool] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    start: Callable[..., subprocess.Popen] = spawn,
    ready_seconds: float | None = None,
) -> str:
    """Make sure something is serving `settings.address`, and say what happened.

    Returns a sentence for the start-up log — "already listening" or "started" —
    and raises :class:`BrokerUnavailable` with a reason and the child's log tail
    when it cannot.  The seams are for a test, which must not need a broker
    installed to exercise the answers: there already, started, no binary,
    started-and-never-answered, **remote and absent**, and **wildcard and
    absent**.

    **A probe, not a connection.**  What is asked is whether anything accepts a
    TCP connection at that address, which is deliberately weaker than "is this a
    broker" — the same weaker question `slife2.launcher` asks of a port before it
    builds an MCP client, and for the same reason: a connection to something that
    is not a broker is refused, authenticated or timed out by the mesh's own
    connect, which knows what to say about it, where a probe that tried to speak
    MQTT would be a second implementation of the handshake.

    **`broker.host` may name another machine, and that is a different situation
    rather than a harder one.**  A remote broker is reached exactly the way a
    local one is — the address is all the mesh ever needs — so what changes here
    is only what may be done when nothing answers: nothing.  A local broker can be
    started, and a remote one cannot be started from here at all, so an absent
    remote broker is the hard failure with no attempt made and a message saying
    which of the two it was.

    **A wildcard is refused, because the alternative is an open broker.**  The
    config we write is `allow_anonymous true` — this build has no way to
    authenticate peers — so a broker started for `0.0.0.0` would publish every
    `$a2a/v1/...` topic to the network and let any host on the LAN impersonate a
    peer.  That is not a "harder" remote case and not a local one either: it is
    the one edit that reads as "make it reachable" and silently makes the mesh
    reachable by everybody, so it fails with the reason rather than being
    started.  The operator who wants a reachable broker runs one, with
    authentication, and points `a2a.broker.host` at it.
    """
    probe = listening or _probe
    if probe(settings.broker_host, settings.broker_port):
        return f"broker already listening on {settings.address}"

    if is_wildcard(settings.broker_host):
        raise BrokerUnavailable(
            f"nothing is listening on {settings.address}, and "
            f"{settings.broker_host!r} binds every interface — a broker started "
            f"here would answer the whole network with `allow_anonymous true` "
            f"and let any host impersonate a peer. Start a broker yourself on a "
            f"concrete address, or point `a2a.broker.host` at `localhost`."
        )

    if not is_local(settings.broker_host):
        raise BrokerUnavailable(
            f"nothing is listening on {settings.address}, and "
            f"{settings.broker_host!r} is not this machine — a broker on somebody "
            f"else's host is theirs to start, and a local one cannot stand in for "
            f"it. Start it there, or point `a2a.broker.host` at this one."
        )

    binary = which(MOSQUITTO)
    if binary is None:
        raise BrokerUnavailable(
            f"nothing is listening on {settings.address} and {MOSQUITTO!r} is not "
            f"on PATH, so one could not be started. Install it (or start a broker "
            f"yourself on that address)."
        )

    config = _write_config(settings)
    url = log_url(settings)
    logger.info(
        "a2a_broker_starting binary=%s address=%s config=%s",
        binary,
        settings.address,
        config,
    )
    process = start([binary, "-c", str(config)], url=url)
    # **The record is what makes the broker stoppable, and it is written here
    # because this is the only moment anything knows the pid.**  It is the same
    # file every other daemon writes (`runtime.ServerRecord`), keyed by the
    # broker's own `mqtt://` address, so `stop` can prove the process is the one
    # *this machine's slife2* started rather than something at that pid later —
    # and so a broker slife2 did **not** start (a system service, one somebody
    # ran by hand) has no record and is never signalled.
    write_record(
        ServerRecord.now(
            name=BROKER_NAME,
            url=url,
            pid=process.pid,
            config=str(config),
            version="",
        )
    )

    # The registry is read *here*, not in the signature: a def-time default
    # would capture the value at import and ignore a patch, which is the rule
    # `slife2/timeouts.py` states and `tests/test_no_magic_timeouts.py`
    # enforces.  `None` is the "nobody said" spelling, so an explicit value
    # (a test's zero, say) still wins.
    if ready_seconds is None:
        ready_seconds = _T.timeouts.ready.broker
    deadline = time.monotonic() + ready_seconds
    while True:
        # The child is checked on every poll, not only at the deadline: a broker
        # that exits at once — the port taken by something else, a bad config —
        # says so in its own log in about fifty milliseconds, where a loop that
        # only watched the clock would sit out the whole budget and then blame a
        # timeout.
        if process.poll() is not None:
            raise BrokerUnavailable(
                f"{MOSQUITTO} exited with code {process.returncode} before it "
                f"served {settings.address}\n{tail_log(url)}".rstrip()
            )
        if probe(settings.broker_host, settings.broker_port):
            logger.info("a2a_broker_started address=%s", settings.address)
            return f"started {MOSQUITTO} on {settings.address}"
        if time.monotonic() >= deadline:
            raise BrokerUnavailable(
                f"{MOSQUITTO} did not serve {settings.address} within "
                f"{ready_seconds:.0f}s\n{tail_log(url)}".rstrip()
            )
        time.sleep(_T.timeouts.pacing.broker_poll)


def stop(
    settings: A2ASettings,
    *,
    kill: Callable[[int], bool] = terminate,
    listening: Callable[[str, int], bool] | None = None,
) -> str:
    """Stop the broker **this machine's slife2 started**, and say what happened.

    **The record is the whole of the permission.**  `ensure` writes one when it
    starts a broker and nothing else ever does, so a broker with no record is
    somebody else's — a system service, one an operator ran, one belonging to a
    data directory this config does not name — and it is left alone.  A record
    whose pid is gone, or whose pid has been reused by something that is not the
    process we started, is the same answer for `slife2.runtime.terminate`'s
    reason: killing an innocent process is a much worse failure than leaving a
    daemon running.

    **This is the other half of `ensure`, and the shape is the launcher's.**
    Every other daemon here is stopped by the record its start wrote
    (`slife2.launcher.stop`), and until this existed the broker was stopped only
    as a *side effect* of killing the plugin that spawned it — the tree kill
    reaching a child.  That works for a broker whose parent is still the plugin
    and leaks for every other case: a plugin restarted after a crash leaves the
    broker orphaned, and an orphan is a process nobody will ever stop.

    Returns the sentence `slife2` prints, like `ensure`'s — the two are the
    halves of one story and read the same way in a terminal — or **`""` when
    there is nothing to say**: no record and nothing listening means this
    machine has no broker at all, and a take-down that announced it would be
    reporting the absence of a daemon nobody asked for.
    """
    url = log_url(settings)
    record = read_record(url)
    if record is None:
        probe = listening or _probe
        if not probe(settings.broker_host, settings.broker_port):
            return ""
        return "not started by slife2; left alone"

    if not same_process(record):
        clear_record(url)
        # Two situations, and they are different sentences: a pid that is gone
        # (a machine rebooted, a broker that exited on its own) and a pid that
        # now belongs to somebody else — which is what the start token is for.
        why = "the process is gone" if not pid_alive(record.pid) else "pid reused"
        return f"the broker at {settings.address} is no longer pid {record.pid} ({why})"

    gone = kill(record.pid)
    clear_record(url)
    if not gone:
        return f"pid {record.pid} did not exit"
    logger.info("a2a_broker_stopped address=%s pid=%d", settings.address, record.pid)
    return f"stopped {MOSQUITTO} on {settings.address}"
