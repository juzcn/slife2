"""The tunnels — "expose this local port as a public HTTPS URL".

The plugin owns exactly ONE provider instance, chosen by the `file-tunnel:`
section's `active:` (see :mod:`slife2.libs.host.config`).  Providers are the only
thing that differs between tunnels: ``server.py`` talks to the
:class:`TunnelProvider` surface and never to a concrete implementation, so
switching tunnels is a config change rather than a code change — which is why
`active:` is a name the operator writes and not a branch anybody edits.

Every provider shares one lifecycle (:class:`_TunnelProviderBase`) and supplies
only the three things that actually differ — how to establish the tunnel once,
how to tear it down, and how to tell whether it is still up:

* :class:`NgrokTunnel` drives the ngrok Python SDK, which embeds the agent as a
  native extension — no external binary.
* :class:`LocalhostRunTunnel` and :class:`CloudflareQuickTunnel` spawn a CLI
  (``ssh`` / ``cloudflared``) and read the public URL off its stdout, so they
  share :class:`_CliTunnelProvider` on top of the base.

Contract the base guarantees, and which ``server.py`` relies on:

* ``start(port)`` is **synchronous and blocking**.  It is always reached through
  ``asyncio.to_thread`` — never on the event loop — so it must be callable from
  an arbitrary thread with no running loop: hence ``subprocess`` and not
  ``asyncio.create_subprocess_exec``.
* ``start()`` is single-flight and idempotent, and ``stop()`` never raises —
  it is called at shutdown while a start may still be in flight.
* ``is_reachable()`` splits "a URL exists" from "that URL would be served":
  ``is_active`` reports the former, this the latter.  They come apart exactly
  when the transport has lost the edge — the URL is still published while
  every request to it answers HTTP 530.  Same contract as ``status()``.
* ``status()`` is pure, synchronous, non-blocking and exception-free, and
  always carries a ``state`` key: ``active`` (a URL is live) / ``starting``
  (an attempt is in flight) / ``failed`` (terminal — reported only once an
  attempt has concluded) / ``idle`` (no attempt made).

**The timings live in `slife2.helpers.timeouts`**, and every consumer reads them there
at call time (``_T.timeouts.<role>.<key>``) rather than capturing them — the
rule that module's own docstring states.  It is the build's single source of
truth, and the one invariant that matters here is checked there: a published
tunnel gets at least the patience a fresh start gets before the monitor respawns
it, asserted by ``slife2.helpers.timeouts.validate`` rather than by a
hand-rolled check at the bottom of this module.
"""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx2

import slife2.helpers.timeouts as _T

logger = logging.getLogger(__name__)

#: The provider used when the section is absent, unreadable, or names a
#: provider this build does not have.
DEFAULT_PROVIDER = "ngrok"

#: Providers this build can actually construct (see :func:`create_provider`).
KNOWN_PROVIDERS = frozenset({"ngrok", "localhost.run", "cloudflare"})

#: Environment variable the active tunnel publishes its URL through.  Set on
#: success, cleared on loss/stop.  Read by another *process* — a subagent
#: reusing the parent's tunnel — and it is what keeps the fact visible to a
#: `slife2 status`-style reader without a second call.
_TUNNEL_URL_ENV = "SLIFE2_FILE_TUNNEL_URL"

#: How many establish attempts before the state is terminal `failed`.
_MAX_RETRIES = 3

#: Output lines kept for the failure message when a CLI child dies early.
_TAIL_LINES = 20


# ═══════════════════════════════════════════════════════════════════════
# Fake-ip — the one network fact a flapping tunnel needs named
# ═══════════════════════════════════════════════════════════════════════

#: Measured once per process: whether this host's resolver answers names that
#: cannot exist.  A property of the network rather than of a request.
_resolver_fakes: bool | None = None


def is_public_address(address: object) -> bool:
    """Whether *address* is globally routable — a real destination.

    This module is the only caller of ``is_public_address``, so the function
    lives here rather than in a module of its own: there is no shared
    ``net.py``, because this build has no SSRF guard that would share it.

    Anything unparseable — a hostname, a malformed value, the ``""`` that means
    "never observed" — answers False: a caller asking about something that is
    not an address is not asking a question this can answer yes to.
    ``is_global`` counts multicast as global, and a multicast address is not a
    destination anything can be fetched from.
    """
    try:
        ip = ipaddress.ip_address(address)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return ip.is_global and not ip.is_multicast


def _probe_fake_ip_resolver() -> bool:
    """Ask the resolver for a name that cannot exist.

    A random name under ``.com``: no fake-ip-filter lists it, because filtering
    ``.com`` wholesale would break the proxy.  The answer must be a *non-public*
    address to count.  A resolver that hijacks NXDOMAIN answers with its own ad
    server — a real, globally routable address — and that is an annoying
    resolver, not a fake-ip one; reading it as fake-ip would relax every
    caller's guard on a machine whose answers are honest, so the narrower test
    is also the fail-closed one.
    """
    try:
        infos = socket.getaddrinfo(f"{uuid.uuid4().hex}.com", None)
    except OSError:
        return False
    return any(not is_public_address(info[4][0]) for info in infos)


def _resolver_uses_fake_ip() -> bool:
    """Whether this host's resolver answers names that do not exist."""
    global _resolver_fakes
    if _resolver_fakes is None:
        _resolver_fakes = _probe_fake_ip_resolver()
    return _resolver_fakes


def _is_fake_ip_answer(address: object) -> bool:
    """Whether a lying resolver answered *address* for a name.

    An **accusation, not a permission** — the distinction is the whole reason
    this is allowed to be coarse.  It is read by the tunnel's health report to
    name the proxy behind a flapping tunnel, and a false
    positive adds a hint that is still true of the machine ("a fake-ip proxy is
    in the path") while a false negative leaves the tunnel flapping with no
    cause named.  Nothing is granted through this.

    False for a public address, which is a real destination, and for anything
    unparseable — an edge address never observed is not an interception.
    """
    if not _resolver_uses_fake_ip():
        return False
    try:
        ip = ipaddress.ip_address(address)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return not ip.is_global


# ═══════════════════════════════════════════════════════════════════════
# Binary / SDK discovery
# ═══════════════════════════════════════════════════════════════════════


def _which(binary: str, extra: tuple[Path, ...] = ()) -> str | None:
    """Resolve a CLI binary — PATH first, then any *extra* well-known absolute
    locations, in order.

    ``shutil.which`` and nothing else: Python's resolver already honours
    ``PATHEXT``, so it finds ``cloudflared.exe`` and ``ssh.exe`` on its own, and
    a wrapper around it would have nothing left to do.

    **The guessed install locations are a fallback a reader should be able to
    see.**  OpenSSH and cloudflared are not on PATH in every shell on Windows,
    so a copy under ``System32`` or ``Program Files`` is found and used — which
    is what makes the feature work there, and also means PATH does not
    necessarily win.  The order is therefore reported rather than trusted:
    :attr:`_TunnelProviderBase.binary_path` carries the resolved path into
    :meth:`_TunnelProviderBase.status`.

    **The guess is only for the default binary.**  When the operator named one
    (`binary:`), a name that cannot be found is refused rather than replaced by
    another program from a guessed location; `_find_binary` passes no `extra`
    in that case.  It is registered in SDD Appendix C, and *the failure goes*
    to that reported path or to the refusal that names the configured binary.
    """
    found = shutil.which(binary)
    if found:
        return found
    for candidate in extra:
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_ssh_fallbacks() -> tuple[Path, ...]:
    """OpenSSH is an *optional* Windows capability — it ships with the OS but
    is not on PATH in every shell, so fall back to its install location.
    """
    if sys.platform != "win32":
        return ()
    return (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "OpenSSH"
        / "ssh.exe",
    )


def _cloudflared_fallbacks() -> tuple[Path, ...]:
    """cloudflared is NOT bundled with slife2 and no installer installs it —
    these are the locations its own Windows installers use.
    """
    if sys.platform != "win32":
        return ()
    roots = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    return tuple(
        Path(root) / "cloudflared" / "cloudflared.exe" for root in roots if root
    )


def _import_ngrok() -> Any:
    """Import the official ngrok SDK (embeds the agent — no external binary)."""
    try:
        import ngrok
    except ImportError as exc:
        raise RuntimeError(
            "The ngrok Python SDK is not installed. Run: uv sync"
        ) from exc
    return ngrok


#: A `${VAR}` that `slife2.libs.host.config.resolve_secret` could not resolve is left
#: **verbatim** rather than raised (that module's rule, so a missing key fails
#: where it is used).  A tunnel is the one place that reads badly: the literal
#: text would be sent as the token and come back as an authentication failure
#: naming nobody's mistake.  So the unresolved form is recognised here and
#: reported as the missing credential it is.
_UNRESOLVED = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-([^}]*))?\}$")


def _auth_token(options: dict) -> str | None:
    """The ngrok auth token for this provider, or ``None``.

    The option is the operator's (`token: "${NGROK_AUTHTOKEN}"`, which
    `resolve_secret` reads through the shell env and then credstore).  The
    process's own ``NGROK_AUTHTOKEN`` is the fallback, so that a bare
    ``ngrok: {}`` still works on a machine that exports one; it is the same
    variable the config names, so the two agree when both are present.
    """
    token = str(options.get("token") or "").strip()
    if token:
        matched = _UNRESOLVED.match(token)
        if matched is None:
            return token
        # Unresolved: the `:-default` is the only thing that could still answer.
        token = (matched.group(1) or "").strip()
        if token:
            return token
    return os.environ.get("NGROK_AUTHTOKEN", "").strip() or None


def _ngrok_tunnel_alive(public_url: str) -> bool:
    """True if ngrok still lists *public_url* as a live listener.

    The embedded SDK does NOT report a server-side teardown (free-tier sessions
    get recycled by ngrok), so the monitor asks the SDK what it is still
    forwarding.  ``get_listeners`` is the API this SDK version actually
    exposes — ``get_tunnels`` does not exist in ngrok 1.x, and a probe naming
    it returns True for every input and detects nothing.

    TWO conservative rules, because a wrong "dead" costs more than a wrong
    "alive" here: the monitor answers a false negative by tearing the tunnel
    down and minting a NEW hostname, which kills every link already handed to a
    person or an LLM.  So an unavailable probe (an older SDK, API trouble)
    returns True, and so does an EMPTY list — "no listeners" cannot be told
    apart from "this build does not track them".  A NON-EMPTY list that omits
    our URL does answer: there the SDK is demonstrably tracking listeners for
    this session and ours is not among them.

    **This is the fallback registered in SDD Appendix C**, and its shape is
    that of the two beside it: *what it does* is treat a probe that could not
    answer as alive; *it gives up* telling a broken probe from an outage; *it
    does not cover* a non-empty listener list that proves our URL gone; *the
    failure goes* to the debug line below and to `tunnel_status`.
    """
    try:
        ngrok = _import_ngrok()
        if ngrok is None:
            return True
        get_listeners = getattr(ngrok, "get_listeners", None)
        if get_listeners is None:
            return True
        listeners = list(get_listeners())
        if not listeners:
            return True
        want = public_url.rstrip("/")
        for listener in listeners:
            url = listener.url
            url = url() if callable(url) else url
            if str(url).rstrip("/") == want:
                return True
        return False
    except Exception as exc:  # noqa: BLE001 — a failed probe must not flap the tunnel
        logger.debug(
            "ngrok_liveness_probe_failed url=%s err=%s — treating the tunnel as alive",
            public_url,
            exc,
        )
        return True


@runtime_checkable
class TunnelProvider(Protocol):
    """What ``server.py`` needs from a tunnel.  See the module docstring."""

    def start(self, port: int) -> str: ...

    def stop(self) -> None: ...

    @property
    def is_active(self) -> bool: ...

    @property
    def is_starting(self) -> bool:
        """Whether a start attempt is in flight (the single-flight guard).

        Read by `_ensure` before it decides between waiting for an attempt and
        beginning one, which is the only reason it belongs here rather than
        staying an implementation detail of the base class.
        """
        ...

    def is_reachable(self) -> bool: ...

    def status(self) -> dict[str, str]: ...

    def share_url_for(self, file_id: str) -> str | None: ...

    def start_monitor(self, port: int) -> None: ...

    def stop_monitor(self) -> None: ...


# ═══════════════════════════════════════════════════════════════════════
# Shared lifecycle
# ═══════════════════════════════════════════════════════════════════════


class _TunnelProviderBase:
    """Everything a tunnel provider does apart from the transport itself.

    Subclasses implement exactly three hooks: :meth:`_do_start` (establish the
    tunnel once, return its public URL), :meth:`_teardown` (release the
    transport; must never raise) and :meth:`is_alive` (the liveness probe the
    monitor polls).
    """

    #: Name used in log lines and error messages.
    label: str = "tunnel"

    def __init__(self) -> None:
        self._public_url: str | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._monitor_retries: int = 0
        self._starting: bool = False  # guard against concurrent starts
        self._starting_at: float | None = None  # monotonic time of the attempt
        self._start_gen: int = 0  # bumped per accepted start (stale ownership)
        #: Set by `stop`.  A start runs on a worker thread, so cancelling the
        #: task that awaited it does not cancel the thread: without this flag a
        #: start that was in flight at shutdown could spawn its child, publish
        #: its URL, and leave both behind after `stop()` had returned.
        self._closed: bool = False
        self._start_lock = threading.Lock()  # serializes guard mutation only
        # Set once a start attempt has concluded unsuccessfully.  Cleared on a
        # successful start or an explicit stop — lets a reader report "tunnel
        # down" as a terminal state instead of racing a live attempt.
        self._failed: bool = False
        # Factual message of the last terminal failure (empty when not failed).
        self._failure_reason: str = ""
        #: The edge address the child last reported dialing.  Only the CLI
        #: providers set it (see ``_note_edge_ip``); empty means "unknown", not
        #: "fine", so :attr:`edge_via_proxy` stays False rather than guessing.
        self._edge_ip: str = ""
        #: The CLI binary a tunnel resolved, once it has found one.  Empty for an
        #: SDK transport, and for a CLI provider that has not started.  A CLI
        #: provider looks the binary up on PATH first and then in guessed install
        #: locations (``_which``), so *which* binary ran is otherwise invisible —
        #: reported through :attr:`binary_path` and in :meth:`status`.
        self._binary_path: str = ""

    # ── Subclass hooks ─────────────────────────────────────────────

    def _do_start(self, port: int) -> str:
        """Establish the tunnel once and return its public URL.

        Raises on any failure; retrying is the base's job (see
        :meth:`_run_attempts`).  Called with the single-flight guard held.
        """
        raise NotImplementedError

    def _teardown(self) -> None:
        """Release the transport.  Must never raise."""

    def is_alive(self) -> bool:
        """Whether the tunnel is still up.  Synchronous — the monitor hands
        this to ``asyncio.to_thread``.
        """
        raise NotImplementedError

    # ── Properties ─────────────────────────────────────────────────

    @property
    def public_url(self) -> str | None:
        """The current tunnel's public URL, or a peer's from the env."""
        if self._public_url is not None:
            return self._public_url
        return os.environ.get(_TUNNEL_URL_ENV)

    @property
    def is_active(self) -> bool:
        """True when the tunnel is running."""
        return self.public_url is not None

    @property
    def is_starting(self) -> bool:
        """Whether a start attempt is in flight (the single-flight guard).

        The monitor's first pass normally lands in the middle of the plugin's
        own eager start, and waiting for that attempt to conclude is the whole
        point: it is already running, so there is nothing to retry.
        """
        with self._start_lock:  # held only for guard mutation, never across
            return self._starting

    @property
    def edge_ip(self) -> str:
        """The edge address the child last reported dialing; ``""`` if unknown.

        Only the CLI providers populate it — an SDK-driven transport has no
        line to scrape — so an empty value means "not observed", never "fine".
        """
        return self._edge_ip

    @property
    def edge_via_proxy(self) -> bool:
        """Whether the edge address is one a fake-ip resolver answered.

        True means a proxy in fake-ip mode is resolving the tunnel's edge, so
        the control connection is being carried (and cut) by that proxy rather
        than by Cloudflare.  Nothing about the transport or the protocol helps
        there: the dial itself lands on an address that is not a host.
        Reported as a fact — the caller owns the remedy, which is on the user's
        proxy config, not in slife2.
        """
        return _is_fake_ip_answer(self._edge_ip)

    @property
    def binary_path(self) -> str:
        """The CLI binary this tunnel resolved, or ``""`` when there is none.

        A CLI provider discovers its binary on PATH first and then by guessed
        install locations (see ``_which``), so the provider's name alone does
        not say which program is running.  Empty means "an SDK transport, or a
        CLI provider that has not started" — never "no binary".
        """
        return self._binary_path

    def is_reachable(self) -> bool:
        """Whether the published URL can actually be served right now.

        Narrower than :attr:`is_active`, which only reports that a URL
        *exists*: a transport that lost the edge leaves the URL in place while
        every request to it answers HTTP 530, so handing that URL out is
        handing out a dead link.

        Synchronous and exception-free, like :meth:`status`, and a probe that
        cannot answer is not an outage — the caller is deciding whether to
        refuse an exposure, and a broken probe must not be the thing that
        refuses.

        **A URL from the environment has no transport here to probe**, which is
        the same rule read one step earlier.  :attr:`public_url` falls back to
        ``SLIFE2_FILE_TUNNEL_URL`` — a tunnel this process did not start, which
        is the whole point of the variable and what :meth:`share_url_for` hands
        out — so ``self._public_url`` is ``None`` while ``is_active`` is True.
        Reading the private field made every such URL unreachable in the same
        breath as ``is_active`` saying it was running: ``expose_local_image``
        refused the file with a sentence about the edge answering 530, which is
        a claim about a transport this process is not running and never probed.
        With nothing of ours behind the URL there is nothing to ask, so the
        answer is whether there is a URL at all.

        **A probe that throws is the fallback registered in SDD Appendix C**:
        *what it does* is treat the URL as reachable; *it gives up* telling a
        broken probe from an outage; *it does not cover* a transport its
        provider proves dead; *the failure goes* to the debug line below and to
        `tunnel_status`.
        """
        if self._public_url is None:
            return self.public_url is not None
        try:
            return bool(self.is_alive())
        except Exception as exc:  # noqa: BLE001 — a failed probe must not refuse
            logger.debug(
                "tunnel_reachability_probe_failed provider=%s err=%s — "
                "treating the URL as reachable",
                self.label,
                exc,
            )
            return True

    def share_url_for(self, file_id: str) -> str | None:
        """Build a public share URL for *file_id*."""
        url = self.public_url
        if url is None:
            return None
        return f"{url}/share/{file_id}"

    # ── Status ─────────────────────────────────────────────────────

    def status(self) -> dict[str, str]:
        """Report the tunnel's current state.  See the module docstring.

        A CLI provider adds the resolved ``binary`` path once it has one, so a
        reader can tell *which* program is running and not only its provider
        name.  Absent for an SDK transport, and for a CLI provider still idle.

        **An inherited URL is reported as the active one**, because that is what
        it is: :attr:`is_active` is True and :meth:`share_url_for` hands it out,
        so answering ``idle`` with an empty url made `tunnel_status` deny the one
        URL every other reader in this process was using — the contradiction
        :meth:`is_reachable` was fixed on, told about the same tunnel.
        """
        if self._public_url is not None:
            state = {"state": "active", "url": self._public_url}
        elif (inherited := self.public_url) is not None:
            state = {"state": "active", "url": inherited}
        elif self._starting:
            state = {"state": "starting", "url": ""}
        elif self._failed:
            state = {"state": "failed", "url": "", "reason": self._failure_reason}
        else:
            state = {"state": "idle", "url": ""}
        if self._binary_path:
            state["binary"] = self._binary_path
        return state

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self, port: int) -> str:
        """Start the tunnel to *port*.  Returns the public URL.

        Blocking and single-flight — see the module docstring.
        """
        if self._public_url is not None:
            logger.warning("tunnel_already_running url=%s", self._public_url)
            return self._public_url

        with self._start_lock:
            # **`_closed` is read under the same lock `stop` sets it under.**
            # Read before the lock, a `stop` landing in between was invisible:
            # this attempt then bumped the generation past stop's bump, became
            # the current owner, and published a tunnel after shutdown.
            if self._closed:
                return ""
            if self._starting:
                elapsed = time.monotonic() - (self._starting_at or time.monotonic())
                if elapsed < _T.timeouts.ready.tunnel_start:
                    logger.debug("tunnel_start_already_in_progress")
                    raise RuntimeError("Tunnel start already in progress")
                logger.warning(
                    "tunnel_start_stale_superseded elapsed=%.0fs timeout=%.0fs",
                    elapsed,
                    _T.timeouts.ready.tunnel_start,
                )
            self._start_gen += 1
            gen = self._start_gen
            self._starting = True
            self._starting_at = time.monotonic()
        try:
            # Pessimistic: any exit below marks the attempt terminal.  Cleared
            # only on success, so ``status()`` reports "failed" once the
            # attempt has concluded.
            self._failed = True
            url = self._do_start(port)
            # Only the CURRENT owner may publish the URL.  A superseded
            # attempt (its start crossed _T.timeouts.ready.tunnel_start and a newer one
            # took over) must NOT overwrite the newer tunnel's URL — the
            # tracked URL would point at a duplicate/stale attempt while the
            # monitor tears down "the current" one, leaking a tunnel and
            # flapping the share.  The superseded thread also leaves the newer
            # owner's _failed/_starting state alone.
            with self._start_lock:
                if self._start_gen != gen:
                    logger.warning(
                        "tunnel_start_superseded_after_established "
                        "provider=%s url=%s — not publishing (a newer start "
                        "owns the tunnel)",
                        self.label,
                        url,
                    )
                    if self._closed:
                        # Shutdown took the tunnel while this thread was
                        # establishing it.  `stop` saw no child to kill; this
                        # is the moment there is one.
                        self._teardown()
                    return self._public_url or ""
                self._public_url = url
                os.environ[_TUNNEL_URL_ENV] = url
                self._failed = False
                self._failure_reason = ""
            logger.info(
                "tunnel_started provider=%s port=%s url=%s", self.label, port, url
            )
            return url
        except Exception as e:
            self._failure_reason = str(e)  # factual last-failure message
            raise
        finally:
            # Only the current owner clears the guard — a superseded thread
            # finishing later must not clobber the newer attempt's state.
            with self._start_lock:
                if self._start_gen == gen:
                    self._starting = False
                    self._starting_at = None

    def _run_attempts(self, attempt: Callable[[], str]) -> str:
        """Retry *attempt* with linear backoff; return its public URL.

        Providers call this from :meth:`_do_start` once everything that must
        NOT be retried (a missing token, a missing binary) has been checked.
        """
        last_error: Exception | None = None
        for n in range(1, _MAX_RETRIES + 1):
            if self._closed:
                raise RuntimeError("the tunnel was stopped while it was starting")
            try:
                return attempt()
            except Exception as e:  # noqa: BLE001 — retried, then re-raised
                last_error = e
                self._after_failed_attempt()
                if n < _MAX_RETRIES:
                    delay = _T.timeouts.pacing.tunnel_retry_delay * n
                    logger.warning(
                        "tunnel_retry provider=%s attempt=%d/%d delay=%.1fs err=%s",
                        self.label,
                        n,
                        _MAX_RETRIES,
                        delay,
                        e,
                    )
                    time.sleep(delay)

        raise RuntimeError(
            f"Failed to start the {self.label} tunnel after {_MAX_RETRIES} "
            f"attempts: {last_error}"
        )

    def _after_failed_attempt(self) -> None:
        """Clean up between retries (a CLI provider kills its dead child)."""

    def stop(self) -> None:
        """Disconnect the tunnel.  Never raises."""
        # **Invalidate a start in flight before tearing down.**  The start runs
        # in a worker thread (`asyncio.to_thread`), so cancelling its task does
        # not stop it; the generation bump makes the thread tear down whatever
        # it established instead of publishing it after this returned.
        with self._start_lock:
            self._closed = True
            self._start_gen += 1
            self._starting = False
            self._starting_at = None
        # A stopped tunnel is never "failed" — clear the flag even when there
        # is nothing to disconnect.
        self._failed = False
        self._failure_reason = ""
        self._teardown()
        if self._public_url is None:
            return
        logger.info(
            "tunnel_disconnected provider=%s url=%s", self.label, self._public_url
        )
        self._drop_url()

    def _drop_url(self) -> None:
        """Forget the published URL and un-export it."""
        self._public_url = None
        os.environ.pop(_TUNNEL_URL_ENV, None)

    # ── Health monitor ─────────────────────────────────────────────

    def start_monitor(self, port: int) -> None:
        """Spawn a background monitor that restarts the tunnel on failure."""
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = asyncio.ensure_future(self._run_monitor(port))
        logger.debug("tunnel_monitor_started provider=%s port=%s", self.label, port)

    def stop_monitor(self) -> None:
        """Cancel the health monitor."""
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None

    async def _run_monitor(self, port: int) -> None:
        """Background health loop: keep the tunnel up, restarting if it drops.

        A tunnel failure is temporary — free tiers recycle sessions and the
        transport can die at any time — so this loop never terminates; it runs
        until the monitor is cancelled (``stop_monitor`` / shutdown).
        """
        await asyncio.sleep(_T.timeouts.pacing.tunnel_retry_delay)  # let the start land

        # When the current unreachable stretch began, so a transport that
        # heals itself is not respawned out from under its own recovery.
        down_since: float | None = None

        while True:
            if self._public_url is not None:
                alive = await asyncio.to_thread(self.is_alive)
                if alive:
                    if down_since is not None:
                        logger.info(
                            "tunnel_recovered provider=%s url=%s after=%.0fs",
                            self.label,
                            self._public_url,
                            time.monotonic() - down_since,
                        )
                        down_since = None
                    await asyncio.sleep(_T.timeouts.pacing.tunnel_health)
                    continue

                # Unreachable, but not necessarily lost — and the difference
                # is worth a whole hostname.  A CLI child that loses its edge
                # connection re-registers on its own and KEEPS the hostname it
                # was given, while a respawn mints a new one: every link
                # already handed to a person or an LLM points at the old name
                # and dies for good.  So the child gets the grace window to
                # heal, and only a transport still unreachable at the end of
                # it is the kind a restart can fix.
                if down_since is None:
                    down_since = time.monotonic()
                    logger.warning(
                        "tunnel_unreachable provider=%s url=%s",
                        self.label,
                        self._public_url,
                    )
                elapsed = time.monotonic() - down_since
                if elapsed < _T.timeouts.ready.tunnel_heal:
                    await asyncio.sleep(_T.timeouts.pacing.tunnel_health)
                    continue
                logger.warning(
                    "tunnel_lost provider=%s url=%s elapsed=%.0fs — restarting",
                    self.label,
                    self._public_url,
                    elapsed,
                )
                self._teardown()
                self._drop_url()
                down_since = None

            # No tunnel — but an attempt may already be on its way: the plugin
            # eager-starts the tunnel on a task of its own, so this pass
            # routinely lands mid-attempt.  Waiting for it is the point.
            # Calling start() anyway is refused ("already in progress") and
            # read as a failed restart — two warnings in the log for an
            # attempt that was never broken.
            if self.is_starting:
                await asyncio.sleep(_T.timeouts.pacing.tunnel_retry_delay)
                continue

            # No tunnel — (re)start.
            try:
                await asyncio.to_thread(self.start, port)
            except Exception as e:  # noqa: BLE001 — the loop is the retry
                self._monitor_retries += 1
                logger.warning(
                    "tunnel_restart_failed provider=%s port=%s err=%s retries=%d",
                    self.label,
                    port,
                    e,
                    self._monitor_retries,
                )
                await asyncio.sleep(_T.timeouts.pacing.tunnel_retry_delay)
                continue
            if self._public_url is not None:
                self._monitor_retries = 0
            await asyncio.sleep(_T.timeouts.pacing.tunnel_health)


# ═══════════════════════════════════════════════════════════════════════
# ngrok — Python SDK, embedded agent (no external binary)
# ═══════════════════════════════════════════════════════════════════════


class NgrokTunnel(_TunnelProviderBase):
    """An ngrok HTTP tunnel driven by the official Python SDK.

    Requires an ``NGROK_AUTHTOKEN`` credential, named as the `token:` option of
    its provider entry (credstore, or the env var).  Endpoint pooling lets
    several slife2 instances share one dev domain.

    Note for the free tier: ngrok's edge answers any request whose User-Agent
    contains "Mozilla" with an interstitial splash page (``ERR_NGROK_6024``),
    so a link opened in a *browser* shows ngrok's warning rather than the file.
    Programmatic fetches — the multimodal-LLM path this tool exists for — pass
    through untouched.  ngrok forbids injecting the bypass header server-side,
    so this cannot be worked around here; pick another provider if browser
    access matters.
    """

    label = "ngrok"

    def __init__(self, options: dict | None = None) -> None:
        super().__init__()
        self._options: dict = dict(options or {})
        self._listener: Any = None
        self._ngrok: Any = None

    def _do_start(self, port: int) -> str:
        # A missing token is terminal — never worth retrying.
        token = _auth_token(self._options)
        if not token:
            raise RuntimeError(
                "ngrok auth token not found. Register at https://ngrok.com/signup, "
                "then store the token (`credstore set NGROK_AUTHTOKEN`, or the "
                "`NGROK_AUTHTOKEN` environment variable)."
            )

        self._ngrok = _import_ngrok()

        def _attempt() -> str:
            # Pooled so multiple slife2 instances (WSL + Windows, machines on
            # one account) can share the same ngrok dev domain — ngrok
            # load-balances across all online agents.
            self._listener = self._ngrok.forward(
                f"localhost:{port}",
                authtoken=token,
                pooling_enabled=True,
            )
            return str(self._listener.url()).rstrip("/")

        return self._run_attempts(_attempt)

    def _teardown(self) -> None:
        """Disconnect the listener this provider holds.

        **Keyed on the listener, not the published URL.**  A start can be
        cut off by `stop` before its URL is published (`_start_gen` refuses to
        publish it); the base then calls this to release what the worker
        established, and a URL-keyed guard would answer that call with
        nothing while a live ngrok session stayed connected.
        """
        listener, self._listener = self._listener, None
        if self._ngrok is None:
            return
        url = self._public_url
        try:
            if url is None and listener is not None:
                url = str(listener.url()).rstrip("/")
            if url is None:
                return  # an inherited URL, with no transport of ours behind it
            self._ngrok.disconnect(url)
            logger.info("tunnel_disconnected provider=ngrok url=%s", url)
        except Exception as e:  # noqa: BLE001 — stop() must never raise
            logger.warning("tunnel_disconnect_error err=%s", e)

    def is_alive(self) -> bool:
        if self._public_url is None:
            return False
        return _ngrok_tunnel_alive(self._public_url)


# ═══════════════════════════════════════════════════════════════════════
# Shared machinery for CLI-spawned tunnels
# ═══════════════════════════════════════════════════════════════════════

#: localhost.run prints, once the forward is up::
#:
#:     <id>.lhr.life tunneled with tls termination, https://<id>.lhr.life
#:
#: (an "authenticated as anonymous user" banner may precede it).  Match the
#: URL that follows the marker rather than the banner's bare hostname.
_LHR_MARKER = "tunneled with tls termination,"
_LHR_URL_RE = re.compile(r"https://[0-9A-Za-z.\-]+")

#: cloudflared reports each connector by index, and the index is what pairs a
#: "Registered tunnel connection connIndex=2" with its later "Unregistered…".
_CONN_INDEX_RE = re.compile(r"connIndex=(\d+)")

#: cloudflared announces the loopback port serving its own metrics API::
#:
#:     INF Starting metrics server on 127.0.0.1:20241/metrics
#:
#: ``GET /ready`` there answers from the connector's own view of the edge —
#: ``{"status":200,"readyConnections":N}`` — which is the one liveness signal
#: that does not depend on the child narrating its connection state.  See
#: :meth:`CloudflareQuickTunnel._transport_alive`.
_METRICS_RE = re.compile(r"Starting metrics server on (127\.0\.0\.1:\d+)/metrics")

#: The edge address on cloudflared's connection lines —
#: ``... connIndex=0 event=0 ip=198.18.0.32 location=lax07 protocol=http2``.
_EDGE_IP_RE = re.compile(r"\bip=([0-9a-fA-F:.]+)")


def _conn_index(line: str) -> str:
    """The connector index named in *line*, or ``""`` when it names none."""
    match = _CONN_INDEX_RE.search(line)
    return match.group(1) if match else ""


class _CliTunnelProvider(_TunnelProviderBase):
    """A tunnel built by spawning a CLI and reading its public URL off stdout.

    Subclasses supply the binary, the argument vector, a URL matcher, and the
    message to show when the binary is missing.  The spawn, the reader thread
    (which also republishes a rotated hostname), the failure tail, and the
    escalating kill are shared.
    """

    #: Binary name to look up when the config names none.
    default_binary: str = ""

    #: Whether printing the URL is itself proof the tunnel is reachable.
    #: True for a transport that can only print the URL once the forward is
    #: up (localhost.run's ``ssh -R`` reports the hostname the edge handed
    #: it).  False for one that announces the hostname BEFORE the edge
    #: connection exists: cloudflared's Quick Tunnel banner comes first, and
    #: for the moment in between the hostname answers HTTP 530 — so the URL
    #: must not be published yet.
    url_proves_ready: bool = True

    #: Output lines that report one edge connection appearing / disappearing.
    #: Only read where :attr:`url_proves_ready` is False.  Case matters:
    #: cloudflared prints "Registered tunnel connection connIndex=N" and
    #: "Unregistered tunnel connection connIndex=N", and the lowercase
    #: "registered" inside the second must not read as the first.
    registration_marker: str = ""
    unregistration_marker: str = ""

    def __init__(self, options: dict | None = None) -> None:
        super().__init__()
        opts = options or {}
        named = str(opts.get("binary") or "").strip()
        self._binary: str = named or self.default_binary
        #: Whether the operator named the binary.  A named binary that cannot be
        #: found is refused rather than replaced by a guessed install location —
        #: running a *different* program than the one configured is the
        #: deformation the fallback must not make.
        self._binary_named: bool = bool(named)

        self._proc: subprocess.Popen | None = None
        self._pending_url: str | None = None  # set by the reader for a start
        self._reader: threading.Thread | None = None
        self._recent: collections.deque = collections.deque(maxlen=_TAIL_LINES)
        self._url_event = threading.Event()
        self._eof_event = threading.Event()
        #: Edge connections this child currently holds, by connector index —
        #: the transport's own answer to "is the tunnel actually reachable",
        #: which the process being alive does not give.
        self._registered: set[str] = set()
        #: Base URL of the child's own metrics API once it announces one
        #: (``http://127.0.0.1:20241``), or ``None`` for a child that serves
        #: none.  Preferred over the scraped set above wherever it exists —
        #: see :meth:`_metrics_ready`.
        self._metrics_base: str | None = None

    # ── Subclass hooks ─────────────────────────────────────────────

    def _find_binary(self) -> str | None:
        raise NotImplementedError

    def _argv(self, binary: str, port: int) -> list[str]:
        raise NotImplementedError

    def _parse_url(self, line: str) -> str | None:
        raise NotImplementedError

    def _missing_binary_error(self) -> str:
        raise NotImplementedError

    # ── Lifecycle ──────────────────────────────────────────────────

    def _do_start(self, port: int) -> str:
        # A missing binary is terminal — never worth retrying.
        binary = self._find_binary()
        if binary is None:
            raise RuntimeError(self._missing_binary_error())
        # Remembered so `status()` can report which program is running: the
        # lookup is PATH first, then guessed install locations, so the name
        # alone does not say.
        self._binary_path = binary
        return self._run_attempts(lambda: self._spawn(binary, port))

    def _teardown(self) -> None:
        self._kill_proc()

    def _after_failed_attempt(self) -> None:
        self._kill_proc()

    def is_alive(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        try:
            if proc.poll() is not None:
                return False
        except Exception as exc:  # noqa: BLE001 — a broken probe must not flap
            logger.debug(
                "tunnel_process_probe_failed provider=%s pid=%s err=%s — "
                "treating the tunnel as alive",
                self.label,
                getattr(proc, "pid", "?"),
                exc,
            )
            return True
        return self._transport_alive()

    def _transport_alive(self) -> bool:
        """Whether the transport itself is still up, past "the process runs".

        The base answer is that the process IS the whole signal — true for a
        provider whose child exists only to serve the tunnel.  A provider
        whose child survives a lost tunnel (cloudflared keeps running, and
        keeps serving nothing) overrides this: without it the health monitor
        reports a healthy tunnel forever while every published link answers
        530, which is exactly the state a restart cannot fix.
        """
        return True

    def _ready(self) -> bool:
        """Whether the child has said enough for its URL to be publishable.

        Readiness is deliberately the EDGE's view — a registered connector —
        and not a local fetch of the published URL.  On a fresh Quick Tunnel a
        public resolver answers for the hostname the moment the URL is
        printable, while this machine's own resolver is still negative-caching
        it and the local fetch fails for seconds after.
        Self-probing would therefore gate publication on the local resolver's
        lag rather than on whether anyone else can reach the tunnel, and
        would refuse URLs that work.
        """
        return self.url_proves_ready or bool(self._registered)

    def _note_connections(self, line: str) -> None:
        """Track this child's edge connections off its own output.

        Only meaningful where :attr:`registration_marker` is set; a provider
        whose URL already proves readiness ignores it entirely.
        """
        if not self.registration_marker:
            return
        if self.registration_marker in line:
            self._registered.add(_conn_index(line))
            # The pending start may now be published — this is the line the
            # URL was waiting for.
            if self._pending_url is not None:
                self._url_event.set()
        elif self.unregistration_marker and self.unregistration_marker in line:
            self._registered.discard(_conn_index(line))

    def _note_edge_ip(self, line: str) -> None:
        """Remember the edge address this child is dialing, and whether it is
        a synthetic one.

        cloudflared names it on every connection line (``ip=198.18.0.32``).
        When it is a fake-ip answer, a local proxy is answering for the
        edge — and that is the whole explanation for a tunnel that registers
        and then dies every 30-60 s: the control connection is being cut by
        the proxy, not by the transport or the protocol.  Worth capturing
        because it is otherwise invisible from here: the symptom is a flap
        that looks like Cloudflare's problem.
        """
        match = _EDGE_IP_RE.search(line)
        if match:
            self._edge_ip = match.group(1)

    def _note_metrics(self, line: str) -> None:
        """Pick up the loopback address of the child's own metrics API.

        The child names it once, early (before it registers), and it is what
        lets :meth:`_metrics_ready` ask the transport about itself instead of
        reading its prose.
        """
        match = _METRICS_RE.search(line)
        if match:
            self._metrics_base = f"http://{match.group(1)}"

    def _metrics_ready(self) -> bool | None:
        """Ask the child's own metrics API whether the edge can reach it.

        ``GET /ready`` reports the connector's view — ``readyConnections`` is
        the number of live edge connections — so it answers the question the
        scraped ``_registered`` set only approximates.

        ``None`` means *there was no metrics server to ask* (the child announced
        none), which is the one case that licenses the caller to fall back to
        what it scraped.  A probe that was attempted and could not be answered is
        a different case and reaches the caller the same way — ``None`` — so
        :meth:`_transport_alive` tells the two apart by ``_metrics_base`` being
        set, and neither answer is "alive".
        """
        base = self._metrics_base
        if not base:
            return None
        try:
            with httpx2.Client(
                timeout=httpx2.Timeout(_T.timeouts.ready.probe_endpoint),
            ) as http:
                payload = http.get(f"{base}/ready").json()
            return int(payload.get("readyConnections") or 0) > 0
        except Exception as e:  # noqa: BLE001 — a failed probe is not an answer
            logger.debug("tunnel_ready_probe_failed provider=%s err=%s", self.label, e)
            return None

    def _spawn(self, binary: str, port: int) -> str:
        """Spawn the CLI and block until its URL is publishable (or fails).

        Publishable is :meth:`_ready` — the URL alone for a transport whose
        URL proves the forward, plus the edge's own registration signal for
        one that prints its hostname before it can serve it.
        """
        argv = self._argv(binary, port)
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            # Own process group, so a stuck child can be killed as a group.
            kwargs["start_new_session"] = True

        self._recent.clear()
        self._url_event.clear()
        self._eof_event.clear()
        self._pending_url = None
        # Per-attempt: a previous child's connectors say nothing about this
        # one, and leaving them behind would let a dead attempt pass the
        # readiness gate.  Its metrics port is its own too — a probe aimed at
        # the dead child would answer for a tunnel that no longer exists.
        self._registered.clear()
        self._metrics_base = None

        logger.info("tunnel_spawn provider=%s port=%s", self.label, port)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # the CLI's diagnostics explain an exit
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
        self._proc = proc

        self._reader = threading.Thread(
            target=self._read_output,
            args=(proc,),
            name=f"{self.label}-reader",
            daemon=True,
        )
        self._reader.start()

        deadline = time.monotonic() + _T.timeouts.ready.tunnel_read_url
        while not self._url_event.wait(0.25):
            if proc.poll() is not None:
                raise RuntimeError(
                    f"{self.label} exited before the tunnel was up "
                    f"(code {proc.returncode}):{self._tail()}"
                )
            if self._eof_event.is_set():
                raise RuntimeError(
                    f"{self.label} closed its output without a tunnel URL:"
                    f"{self._tail()}"
                )
            if time.monotonic() > deadline:
                raise RuntimeError(self._timeout_error())

        url = self._pending_url
        self._pending_url = None
        if not url:
            raise RuntimeError(f"{self.label} reported no tunnel URL:{self._tail()}")
        return url

    def _timeout_error(self) -> str:
        """Why the start timed out — naming the half that never arrived.

        A URL with no registered edge connection is the failure that looks
        like success: the hostname exists, so it gets handed out, and every
        fetch of it answers HTTP 530 until the connector comes up — which may
        be never, or minutes later.  Saying so is the difference between a
        dead link and a clear "the tunnel is not reachable from here".
        """
        budget = _T.timeouts.ready.tunnel_read_url
        if self._pending_url is not None and not self._transport_alive():
            return (
                f"the {self.label} tunnel printed its URL "
                f"({self._pending_url}) but registered no connection to the "
                f"edge within {budget:.0f}s — every request to that URL would "
                f"answer HTTP 530 (nothing routes from the edge to this "
                f"machine):{self._tail()}"
            )
        return (
            f"timed out after {budget:.0f}s waiting for the "
            f"{self.label} tunnel URL:{self._tail()}"
        )

    def _read_output(self, proc: subprocess.Popen) -> None:
        """Consume the child's output for the lifetime of the tunnel.

        A URL satisfies the pending start once the child is
        :meth:`_ready`; a later one means the service rotated the published
        hostname (localhost.run's anonymous tier does, every few hours), so
        the URL is swapped in place and re-exported rather than dropped.
        The child's edge-connection lines are tracked here too — they are
        what tells readiness and liveness apart from "the process runs".
        """
        try:
            assert proc.stdout is not None  # Popen set it above
            for line in proc.stdout:
                # **A reader feeds the current attempt or it feeds nothing.**
                # After a retry this thread still holds the OLD child's pipe, and
                # a stale line arriving after the new attempt reset its state
                # would mark a dead tunnel ready — or, once the child is gone and
                # `_proc` is None, resurrect the URL of a tunnel `stop` took
                # down.  The guard used to be `self._proc is not None and proc is
                # not self._proc`, and that first clause read "this process has
                # torn the attempt down" as "this reader is the current one":
                # every stale line written between the termination and the pipe
                # closing was applied to a tunnel that no longer existed.
                if proc is not self._proc:
                    break
                self._recent.append(line.rstrip())
                # The child narrates what it is doing with the edge ("Retrying
                # connection", "Registered tunnel connection").  Nothing else
                # records that: without it a tunnel that is up but unreachable
                # leaves no trace anywhere in the log.
                logger.debug(
                    "tunnel_output provider=%s line=%s",
                    self.label,
                    line.rstrip()[:300],
                )
                self._note_connections(line)
                self._note_metrics(line)
                self._note_edge_ip(line)
                url = self._parse_url(line)
                if url is None:
                    continue
                if self._public_url is None:
                    self._pending_url = url
                    # Where the URL is not its own proof of readiness, the
                    # start waits for the registration line instead — the
                    # reader will set the event from _note_connections.
                    if self._ready():
                        self._url_event.set()
                elif url != self._public_url:
                    logger.info(
                        "tunnel_url_rotated provider=%s old=%s new=%s",
                        self.label,
                        self._public_url,
                        url,
                    )
                    self._public_url = url
                    os.environ[_TUNNEL_URL_ENV] = url
        except Exception as e:  # noqa: BLE001 — see below; the reader must not die silently
            # Once a tunnel is up this thread is what swaps in a rotated
            # hostname, so a reader that dies stops that swap with nothing said
            # — the published URL then goes stale and every later fetch answers
            # 404 against the service, invisibly.  A debug line is not findable;
            # this names the child so the failure can be traced to one spawn.
            logger.warning(
                "tunnel_reader_died provider=%s pid=%s err=%s — a rotated "
                "hostname will no longer be swapped in",
                self.label,
                getattr(proc, "pid", "?"),
                e,
            )
        finally:
            # **`_eof_event` is the *current* attempt's**, and the same guard
            # decides who may set it.  `_spawn` clears it for the attempt it is
            # starting, so a superseded reader that died after that clear used to
            # answer the new attempt's wait loop with "the child closed its
            # output without a tunnel URL" — a start reported as a failure while
            # its own child was still coming up.
            if proc is self._proc:
                self._eof_event.set()

    def _kill_proc(self) -> None:
        """Terminate the child (escalating terminate → kill) and drop the
        handle.
        """
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=_T.timeouts.grace.kill)
                except subprocess.TimeoutExpired:
                    logger.warning("tunnel_kill_escalated provider=%s", self.label)
                    proc.kill()
                    try:
                        proc.wait(timeout=_T.timeouts.grace.kill)
                    except subprocess.TimeoutExpired:
                        pass
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception as e:  # noqa: BLE001 — shutdown must never raise
            logger.debug("tunnel_kill_error provider=%s err=%s", self.label, e)

    def _tail(self) -> str:
        """The last output lines, for a failure message (never empty)."""
        return "\n  " + "\n  ".join(self._recent) if self._recent else " (no output)"


# ═══════════════════════════════════════════════════════════════════════
# localhost.run — SSH reverse tunnel
# ═══════════════════════════════════════════════════════════════════════


class LocalhostRunTunnel(_CliTunnelProvider):
    """An SSH reverse tunnel to localhost.run.

    Free and account-less (``nokey@localhost.run``), and — unlike the ngrok
    free tier — it shows no interstitial to any client, because TLS is
    terminated at localhost.run's edge.  The published hostname is a random
    ``*.lhr.life`` name that rotates after a few hours; the reader thread
    swaps the new one in place.  File bytes are relayed by a third party.
    """

    label = "localhost.run"
    default_binary = "ssh"

    def __init__(self, options: dict | None = None) -> None:
        opts = options or {}
        # ``ssh`` is accepted as a synonym for ``binary`` — it reads better in
        # the config for an SSH-based provider.
        merged = dict(opts)
        if "binary" not in merged and merged.get("ssh"):
            merged["binary"] = merged["ssh"]
        super().__init__(merged)

        self._host: str = str(opts.get("host") or "localhost.run")
        self._user: str = str(opts.get("user") or "nokey")
        # An absent `remote_port` is the default; a *present but malformed* one
        # is refused rather than read as 80.  SDD §12.1 declares exactly one
        # warn-instead-of-refuse in this section (an unknown provider *name*),
        # and says a malformed *shape* is refused like every other section — a
        # bad value in a known option is that same mistake, and silently
        # forwarding a different remote port than the config wrote is the kind
        # of deformation nobody can find (SDD Appendix C).
        raw_port = opts.get("remote_port")
        if raw_port is None or raw_port == "":
            self._remote_port: int = 80
        else:
            try:
                self._remote_port = int(raw_port)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"file-tunnel: localhost.run: remote_port {raw_port!r} is "
                    f"not a number — write it as an integer, e.g. "
                    f"`remote_port: 80`"
                ) from exc

    def _find_binary(self) -> str | None:
        extra = () if self._binary_named else _windows_ssh_fallbacks()
        return _which(self._binary, extra)

    def _argv(self, binary: str, port: int) -> list[str]:
        return [
            binary,
            "-T",  # no pseudo-tty — keep stdout line-oriented and parseable
            # Never prompt: batch mode + accept-new let an unattended daemon
            # connect on a first run without a TTY to answer a host-key prompt.
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            # If the forward cannot be set up (e.g. the remote port is taken),
            # exit instead of holding a session that tunnels nothing.
            "-o",
            "ExitOnForwardFailure=yes",
            "-R",
            f"{self._remote_port}:localhost:{port}",
            f"{self._user}@{self._host}",
        ]

    def _parse_url(self, line: str) -> str | None:
        idx = line.find(_LHR_MARKER)
        if idx == -1:
            return None
        match = _LHR_URL_RE.search(line[idx + len(_LHR_MARKER) :])
        return match.group(0).rstrip("/") if match else None

    def _missing_binary_error(self) -> str:
        return (
            f"ssh client not found ({self._binary!r}). localhost.run needs an "
            "SSH client — Windows 10+ ships OpenSSH (optional feature "
            "'OpenSSH Client'); on Linux/macOS install openssh-client."
        )


# ═══════════════════════════════════════════════════════════════════════
# Cloudflare Quick Tunnel
# ═══════════════════════════════════════════════════════════════════════

#: cloudflared logs a banner box containing the URL on its own line, e.g.::
#:
#:     INF |  https://random-words-here.trycloudflare.com  |
#:
#: so the URL is matched directly rather than after a marker.
_CF_URL_RE = re.compile(r"https://[0-9A-Za-z\-]+\.trycloudflare\.com")


class CloudflareQuickTunnel(_CliTunnelProvider):
    """A Cloudflare Quick Tunnel (``cloudflared tunnel --url``).

    No account, no domain: Cloudflare hands out a random
    ``*.trycloudflare.com`` hostname, and — like localhost.run — it shows no
    interstitial, so browsers and API fetchers both get the file.  The URL is
    stable for as long as the process lives (no rotation).

    Two caveats worth knowing before selecting it:

    * ``cloudflared`` is **not** bundled with slife2 and nothing installs it.
      A missing binary is a terminal ``failed`` state with an actionable
      reason rather than a startup failure — but it *is* the common first-run
      state.
    * Cloudflare positions Quick Tunnels for testing/development, and its
      free-tier terms restrict proxying media through the CDN.  A tunnel that
      serves images sits in that grey area.
    """

    label = "cloudflare"
    default_binary = "cloudflared"

    #: Transport the connector uses to the edge (``--protocol``).  QUIC is
    #: cloudflared's own default and the faster of the two, and it is also the
    #: one that dies behind a proxy / TUN adapter — an environment this tool
    #: meets constantly, since the whole point of a tunnel is to cross a
    #: network the machine does not control.  There the connection registers
    #: and then dies ("timeout: no recent network activity") over and over;
    #: every window in between answers the published hostname with HTTP 530,
    #: and the flapping reads as a healthy tunnel to anything that trusts
    #: stdout.  http2 rides TCP, which those paths carry more reliably than
    #: UDP.  Set ``protocol: "quic"`` (or ``"auto"``) in the section to switch
    #: back.
    #:
    #: **This does NOT rescue a fake-ip proxy** — measured, not theorised: with
    #: a Clash/Mihomo TUN in fake-ip mode the edge hostname resolves to an
    #: address of the proxy's choosing, so the TCP dial itself lands on one and
    #: times out (``dial tcp 198.18.0.32:7844: i/o timeout``).  The transport
    #: choice never comes into play, and the flap continues unchanged on http2.
    #: The honest remedy is on the proxy config (make the edge resolve real and
    #: route direct — see :attr:`_TunnelProviderBase.edge_via_proxy`, which
    #: reports exactly this case).
    DEFAULT_PROTOCOL = "http2"

    # The banner carrying the URL is printed BEFORE the edge connection is
    # negotiated, so the URL alone means nothing here: for the moment in
    # between, the hostname resolves and answers HTTP 530.  Readiness is the
    # connector registration the child logs a beat later.
    url_proves_ready = False
    registration_marker = "Registered tunnel connection"
    unregistration_marker = "Unregistered tunnel connection"

    def __init__(self, options: dict | None = None) -> None:
        super().__init__(options)
        self._protocol: str = str(
            (options or {}).get("protocol") or self.DEFAULT_PROTOCOL
        )

    def _transport_alive(self) -> bool:
        """A cloudflared with no connectors serves only 530s, however alive.

        The process keeps running through a lost edge connection, so process
        liveness alone would report a healthy tunnel while every published
        link is dead.  Zero connectors is the honest answer.

        Sourced from the child's own metrics API, because its output cannot be
        trusted to say so: a QUIC connection that times out is logged as
        "Serve tunnel error" and retried, and NO "Unregistered tunnel
        connection" line follows it — so a connector set scraped from stdout
        stays non-empty straight through an outage that answers every
        published link with HTTP 530, and the monitor watching it never sees
        anything wrong.  **The scraped set is read only when the child announced
        no metrics server at all** (SDD §12.1's boundary): a metrics server that was
        announced and could not be asked leaves `_metrics_ready` answering
        ``None`` while `_metrics_base` is set, and that is *unknown* — not
        alive, because there is no probe answer for it and the scrape is
        exactly the signal that stays full through the outage.
        """
        probed = self._metrics_ready()
        if probed is not None:
            return probed
        if self._metrics_base is not None:
            return False
        return bool(self._registered)

    def _find_binary(self) -> str | None:
        extra = () if self._binary_named else _cloudflared_fallbacks()
        return _which(self._binary, extra)

    def _argv(self, binary: str, port: int) -> list[str]:
        return [
            binary,
            "tunnel",
            # Don't let a background self-update swap the binary under us.
            "--no-autoupdate",
            "--protocol",
            self._protocol,
            "--url",
            f"http://localhost:{port}",
        ]

    def _parse_url(self, line: str) -> str | None:
        match = _CF_URL_RE.search(line)
        return match.group(0).rstrip("/") if match else None

    def _missing_binary_error(self) -> str:
        return (
            f"cloudflared not found ({self._binary!r}). A Cloudflare Quick "
            "Tunnel needs the cloudflared binary — install it from "
            "https://developers.cloudflare.com/cloudflare-one/connections/"
            "connect-networks/downloads/ (or set 'binary' in the "
            "`file-tunnel:` section)."
        )


# ═══════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════


def create_provider(name: str, options: dict | None = None) -> TunnelProvider:
    """Build the tunnel provider named by the `file-tunnel:` section.

    **A name this build does not have is refused**, and the config loader
    refuses it before this is called.  Substituting another provider would
    publish the machine through a vendor the operator did not choose, which is
    a decision this factory has no business making.
    """
    if name == "ngrok":
        return NgrokTunnel(options)
    if name == "localhost.run":
        return LocalhostRunTunnel(options)
    if name == "cloudflare":
        return CloudflareQuickTunnel(options)
    raise ValueError(
        f"unknown tunnel provider {name!r}; known: "
        + ", ".join(sorted(KNOWN_PROVIDERS))
    )
