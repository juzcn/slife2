"""The broker: probed at the configured address, started if it is not there,
and **fatal if it still is not**.

This is the one place this port departs from v1 on purpose, and the departure is
the operator's call rather than a discovery.  v1's rule was that slife "does NOT
spawn Mosquitto — the user must start it", and the presence of a listener *was*
the feature's on/off switch: no broker, no mesh, everything else carried on.  Here
a machine whose config says `a2a:` has said it wants the mesh, so a missing broker
is a missing dependency rather than a preference — the plugin brings one up, and a
broker that will not serve is a start that fails loudly instead of an `a2a_*` tool
surface quietly attached to nothing.

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
interfaces that nobody asked for and that the config does not describe.
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
from slife2.runtime import spawn, tail_log, tcp_listening

logger = logging.getLogger(__name__)

#: The broker binary, and the only provider this build has
#: (`A2ASettings.provider` refuses anything else at config load).
MOSQUITTO = "mosquitto"

#: How long a broker we started has to start answering.  Generous for a binary
#: that has to read a config and bind a port, short enough to be a deadline.

#: The hosts `broker.host` may name and still be *this* machine.  A broker is
#: started only for one of these; anything else is somebody else's host.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "::", ""})

#: How long the probe waits, and **two numbers because the two answers cost
#: different things.**  A local address refuses in microseconds when nothing is
#: bound, so the budget is only a guard against a hung listener and
#: `slife2.runtime`'s own default is right.  A remote one has a network in the
#: way, and there the probe is deciding between "start a broker" and "fail the
#: start" — neither of which a slow link is entitled to choose.  Getting it wrong
#: in the tight direction is the worse mistake: a broker that is merely far away
#: would be reported as absent, and absent-and-remote is fatal.

#: The config we hand mosquitto, and **it is not optional on mosquitto 2.x**.
#: From 2.0 the broker listens on loopback only and refuses anonymous clients
#: unless a config says otherwise, so a bare `mosquitto -p PORT` starts, binds,
#: and then rejects every connection this plugin makes — a failure that would
#: arrive as a connect timeout naming nothing.  An explicit `listener` also pins
#: the bind address, which `-p` cannot: without it the broker would listen on
#: every interface while the config says `127.0.0.1`.
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


def _probe(host: str, port: int) -> bool:
    """Whether something accepts a connection at this address.

    The timeout is chosen by which kind of address it is — see the two constants
    — so a caller never has to remember that one question costs different amounts
    at different distances.
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
    ready_seconds: float = _T.timeouts.ready.broker,
) -> str:
    """Make sure something is serving `settings.address`, and say what happened.

    Returns a sentence for the start-up log — "already listening" or "started" —
    and raises :class:`BrokerUnavailable` with a reason and the child's log tail
    when it cannot.  The seams are for a test, which must not need a broker
    installed to exercise the answers: there already, started, no binary,
    started-and-never-answered, and **remote and absent**.

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
    """
    probe = listening or _probe
    if probe(settings.broker_host, settings.broker_port):
        return f"broker already listening on {settings.address}"

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
