"""The tunnels themselves: the state machine, and the three transports.

Every provider shares `_TunnelProviderBase`, so the lifecycle is tested once —
single-flight start, the four states, the health monitor that restarts a lost
tunnel, and the one rule that costs a hostname if it is wrong (a tunnel that
merely *looks* dead is given the heal window before it is respawned).

The two CLI providers are driven against a fake `subprocess`, so the argv, the
URL parsing and the readiness gate are all under test with no `ssh` and no
`cloudflared` anywhere — v1's arrangement, kept because it is the only way to
test a transport that is somebody else's program.

Two of these are not about behaviour but about a *belief* being wrong, and both
were wrong once: a QUIC connector that times out is never logged as
unregistered (so a scraped set stays full through an outage), and a published
URL is not a working one.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import types
from collections.abc import Sequence

import pytest

import slife2.helpers.timeouts as _T
from slife2.plugins.file_tunnel import providers
from slife2.plugins.file_tunnel.providers import (
    CloudflareQuickTunnel,
    LocalhostRunTunnel,
    NgrokTunnel,
    create_provider,
    is_public_address,
)

pytestmark = pytest.mark.unit


# --- doubles ------------------------------------------------------------------


class LinesStdout:
    """A child's stdout: the scripted lines, then EOF."""

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = iter(lines)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        return next(self._lines)

    def close(self) -> None:
        self.closed = True


class SilentStdout:
    """A child that connected but never prints a URL — blocks the reader."""

    def __init__(self) -> None:
        self._release = threading.Event()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        self._release.wait()  # never set — the daemon reader dies with the test
        raise StopIteration

    def close(self) -> None:
        self.closed = True


class BannerThenSilent:
    """A child that prints its banner and then goes quiet.

    Which is what cloudflared *actually does* while the edge connection is
    still being negotiated — and the difference matters: a child that closes
    its pipe has exited (a different failure, reported differently), where this
    one is alive, has published a hostname, and can route nothing to it.
    """

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = list(lines)
        self._release = threading.Event()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        self._release.wait()  # never set — the daemon reader dies with the test
        raise StopIteration

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, stdout, returncode=None) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def install_popen(monkeypatch, stdout, returncode=None, captured=None):
    """Fake the CLI spawn and the binary lookup.

    *stdout* may be a factory — a retrying start spawns a *new* child each
    attempt, so a shared iterator would be exhausted after the first one.
    """
    captured = captured if captured is not None else []
    make_stdout = stdout if callable(stdout) else (lambda: stdout)

    def _popen(argv, **kwargs):
        captured.append((argv, kwargs))
        return FakeProcess(make_stdout(), returncode)

    fake = types.SimpleNamespace(
        Popen=_popen,
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
        DEVNULL=subprocess.DEVNULL,
        CREATE_NO_WINDOW=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(providers, "subprocess", fake)
    # Fake only the "is it installed?" lookup — every configured binary
    # resolves to itself, so the option plumbing stays under test and no test
    # ever finds a REAL ssh on PATH and connects to a real service.
    monkeypatch.setattr(providers, "_which", lambda binary, extra=(): binary)
    return captured


def install_missing_binary(monkeypatch) -> None:
    monkeypatch.setattr(providers, "_which", lambda binary, extra=(): None)


def one_attempt(monkeypatch, read_timeout: float | None = None) -> None:
    """Make a failing start fail **once, and fast**.

    Three attempts with linear backoff is six seconds of sleeping, and these
    are tests about the *reason* a start failed rather than about retrying —
    which has a test of its own.  The read timeout is shortened only where the
    test is about a child that never prints a URL, since that is the wait being
    bounded.
    """
    monkeypatch.setattr(providers, "_MAX_RETRIES", 1)
    if read_timeout is not None:
        monkeypatch.setattr(_T.timeouts.ready, "tunnel_read_url", read_timeout)


def lhr_line(host: str) -> str:
    return f"{host} tunneled with tls termination, https://{host}\n"


#: cloudflared prints the URL inside a banner box, on its own line.
CF_BANNER = (
    "2026-09-10T20:00:00Z INF |  Your quick Tunnel has been created!  |\n"
    "2026-09-10T20:00:00Z INF |  https://random-words.trycloudflare.com  |\n"
)

#: The connector registration cloudflared logs a beat AFTER the banner — the
#: line that says the edge can actually route to this machine.  Between the
#: two, the hostname exists and answers HTTP 530.
CF_REGISTERED = (
    "2026-09-10T20:00:01Z INF Registered tunnel connection connIndex=0 "
    "connection=2f1e location=lax01 protocol=quic ip=203.0.113.7\n"
)
CF_UNREGISTERED = (
    "2026-09-10T20:00:09Z INF Unregistered tunnel connection connIndex=0\n"
)

#: A QUIC connection that times out.  cloudflared retries — and logs NO
#: "Unregistered tunnel connection" for it, which is the whole trap: a
#: connector set scraped from the output still looks complete while the edge
#: answers HTTP 530 to every request for the published hostname.
CF_SERVE_ERROR = (
    '2026-09-10T20:00:30Z ERR Serve tunnel error error="datagram manager '
    'error: timeout: no recent network activity" connIndex=0 event=0\n'
)

#: The loopback metrics API the child announces before it registers.
CF_METRICS = (
    "2026-09-10T20:00:00Z INF Starting metrics server on 127.0.0.1:20241/metrics\n"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The providers write os.environ directly — undo that per test."""
    monkeypatch.delenv("SLIFE2_FILE_TUNNEL_URL", raising=False)
    yield
    import os

    os.environ.pop("SLIFE2_FILE_TUNNEL_URL", None)


# --- the factory --------------------------------------------------------------


def test_the_default_provider_is_ngrok() -> None:
    assert isinstance(create_provider(providers.DEFAULT_PROVIDER), NgrokTunnel)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("ngrok", NgrokTunnel),
        ("localhost.run", LocalhostRunTunnel),
        ("cloudflare", CloudflareQuickTunnel),
    ],
)
def test_every_known_provider_is_constructible(name, expected) -> None:
    assert isinstance(create_provider(name), expected)


def test_an_unknown_name_falls_back_rather_than_raising() -> None:
    """A bad config value must never keep the plugin from loading."""
    assert isinstance(create_provider("made-up"), NgrokTunnel)


def test_every_provider_shares_the_lifecycle() -> None:
    for name in providers.KNOWN_PROVIDERS:
        assert isinstance(create_provider(name), providers._TunnelProviderBase)


def test_the_heal_window_outlasts_the_read_timeout() -> None:
    """Load-bearing: a shorter one would restart a child while it is still
    establishing itself, and every restart mints a new hostname — killing every
    link already handed out."""
    assert _T.timeouts.ready.tunnel_heal >= _T.timeouts.ready.tunnel_read_url


# --- the state machine --------------------------------------------------------


def test_a_fresh_tunnel_is_idle_not_failed() -> None:
    assert NgrokTunnel().status() == {"state": "idle", "url": ""}


def test_a_live_tunnel_reports_its_url() -> None:
    tunnel = NgrokTunnel()
    tunnel._public_url = "https://test.ngrok.io"
    assert tunnel.status() == {"state": "active", "url": "https://test.ngrok.io"}


def test_an_attempt_in_flight_is_starting() -> None:
    """So a reader waits rather than misreading it as a tunnel that is down."""
    tunnel = NgrokTunnel()
    tunnel._starting = True
    assert tunnel.status()["state"] == "starting"


def test_a_start_that_exhausted_its_retries_is_terminal(monkeypatch) -> None:
    """Three attempts, then a terminal state that names the reason — so a
    reader is not left waiting for a tunnel that has given up."""
    monkeypatch.setattr(providers, "_import_ngrok", lambda: _FailingNgrok())
    monkeypatch.setattr(providers.time, "sleep", lambda _seconds: None)

    tunnel = NgrokTunnel({"token": "t"})
    with pytest.raises(RuntimeError, match="3 attempts"):
        tunnel.start(9090)

    state = tunnel.status()
    assert state["state"] == "failed"
    assert state["url"] == ""
    assert "refused" in state["reason"]


class _FailingNgrok:
    def forward(self, *args, **kwargs):
        raise ConnectionError("refused")


def test_a_missing_token_is_terminal_and_says_how_to_fix_it(monkeypatch) -> None:
    """Not worth retrying, so it must fail on the first attempt — and it must
    name the credential rather than returning an auth error from ngrok."""
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    tunnel = NgrokTunnel({})
    with pytest.raises(RuntimeError, match="auth token not found"):
        tunnel.start(9090)


def test_an_unresolved_token_is_reported_as_missing(monkeypatch) -> None:
    """`resolve_secret` leaves a `${VAR}` it cannot resolve verbatim, so the
    literal text would otherwise be sent as the token and come back as an
    authentication failure naming nobody's mistake."""
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    tunnel = NgrokTunnel({"token": "${NGROK_AUTHTOKEN}"})
    with pytest.raises(RuntimeError, match="auth token not found"):
        tunnel.start(9090)


def test_an_unresolved_token_still_honours_its_default(monkeypatch) -> None:
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    assert providers._auth_token({"token": "${NGROK_AUTHTOKEN:-fallback}"}) == (
        "fallback"
    )


def test_the_environment_is_the_fallback_for_a_bare_entry(monkeypatch) -> None:
    """`ngrok: {}` on a machine that exports the variable — how v1 read it."""
    monkeypatch.setenv("NGROK_AUTHTOKEN", "from-env")
    assert providers._auth_token({}) == "from-env"


def test_starting_a_running_tunnel_returns_the_live_url() -> None:
    tunnel = NgrokTunnel()
    tunnel._public_url = "https://already.ngrok.io"
    assert tunnel.start(9090) == "https://already.ngrok.io"


def test_a_start_already_in_flight_is_refused() -> None:
    tunnel = NgrokTunnel()
    tunnel._starting = True
    tunnel._starting_at = __import__("time").monotonic()
    with pytest.raises(RuntimeError, match="already in progress"):
        tunnel.start(9090)


def test_stop_clears_the_url_and_the_failed_flag(monkeypatch) -> None:
    tunnel = NgrokTunnel()
    tunnel._public_url = "https://test.ngrok.io"
    tunnel._failed = True
    tunnel._failure_reason = "boom"
    tunnel.stop()
    assert tunnel.public_url is None
    assert tunnel.status() == {"state": "idle", "url": ""}
    # Idempotent: a second stop is not an error, which shutdown relies on.
    tunnel.stop()


def test_a_stop_during_a_start_tears_the_late_tunnel_down() -> None:
    """A start runs in a worker thread, so cancelling its task does not stop it.

    The lifespan cancels the eager-start task and then calls `stop`, which used
    to see no child because the worker had not spawned one yet.  The worker then
    finished, published a URL and left the child running after shutdown.
    """

    class Slow(providers._TunnelProviderBase):
        label = "slow"

        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.teardowns = 0

        def _do_start(self, port: int) -> str:
            self.entered.set()
            self.release.wait(timeout=5)  # timeouts: the test's own rendezvous
            return "https://slow.example"

        def _teardown(self) -> None:
            self.teardowns += 1

        def is_alive(self) -> bool:
            return False

    tunnel = Slow()
    started: list[str] = []
    thread = threading.Thread(
        target=lambda: started.append(tunnel.start(9090)), daemon=True
    )
    thread.start()
    assert tunnel.entered.wait(5)  # timeouts: the test's own rendezvous

    tunnel.stop()
    tunnel.release.set()
    thread.join(timeout=5)  # timeouts: the test's own rendezvous

    assert started == [""], "a stopped start publishes nothing"
    assert tunnel.public_url is None
    assert tunnel.teardowns >= 2, "the late worker tore its own child down"


def test_the_url_travels_through_the_environment(monkeypatch) -> None:
    """So that anything in this process — and anything it starts — can resolve
    the tunnel without holding the provider.

    **And the URL is usable, not merely visible.**  This is the v1 arrangement
    the constant's comment blesses: a tunnel some *other* process started, whose
    URL this one reads from the environment.  `is_reachable` read the provider's
    own `_public_url`, which is None for exactly that tunnel, so every inherited
    URL was reported unreachable in the same breath as `is_active` and
    `share_url_for` answering with it — and `expose_local_image` refused the file
    with a sentence about the edge answering HTTP 530, a claim about a transport
    this process is not running and never probed.  There is nothing here to
    probe, which is why the answer is not "no"; `tunnel_status` said `idle` with
    an empty url for the same tunnel, and says `active` with it now.
    """
    monkeypatch.setenv("SLIFE2_FILE_TUNNEL_URL", "https://shared.test")
    tunnel = NgrokTunnel()
    assert tunnel.is_active is True
    assert tunnel.public_url == "https://shared.test"
    assert tunnel.share_url_for("abc") == "https://shared.test/share/abc"
    assert tunnel.is_reachable() is True
    assert tunnel.status() == {"state": "active", "url": "https://shared.test"}


def test_sharing_offline_is_none() -> None:
    assert NgrokTunnel().share_url_for("abc") is None


def test_reachable_is_false_before_any_url() -> None:
    assert NgrokTunnel().is_reachable() is False


def test_reachable_follows_the_transport_not_the_url(monkeypatch) -> None:
    """A URL exists; the edge has stopped serving it.  The two are different
    facts and only one of them is worth handing to somebody."""
    tunnel = NgrokTunnel()
    tunnel._public_url = "https://test.ngrok.io"
    monkeypatch.setattr(tunnel, "is_alive", lambda: False)
    assert tunnel.is_active is True
    assert tunnel.is_reachable() is False


def test_a_broken_probe_never_refuses_a_share(monkeypatch) -> None:
    """The caller is deciding whether to refuse an exposure, and a probe that
    cannot answer must not be the thing that refuses."""
    tunnel = NgrokTunnel()
    tunnel._public_url = "https://test.ngrok.io"

    def explode():
        raise OSError("probe died")

    monkeypatch.setattr(tunnel, "is_alive", explode)
    assert tunnel.is_reachable() is True


# --- ngrok's own liveness -----------------------------------------------------


def test_our_url_in_the_listener_list_is_alive(monkeypatch) -> None:
    listener = types.SimpleNamespace(url=lambda: "https://mine.ngrok.io")
    monkeypatch.setattr(
        providers,
        "_import_ngrok",
        lambda: types.SimpleNamespace(get_listeners=lambda: [listener]),
    )
    assert providers._ngrok_tunnel_alive("https://mine.ngrok.io") is True


def test_absent_from_a_non_empty_list_is_the_one_answer(monkeypatch) -> None:
    """There the SDK is demonstrably tracking listeners and ours is not among
    them — the single case that answers "dead"."""
    listener = types.SimpleNamespace(url="https://someone-elses.ngrok.io")
    monkeypatch.setattr(
        providers,
        "_import_ngrok",
        lambda: types.SimpleNamespace(get_listeners=lambda: [listener]),
    )
    assert providers._ngrok_tunnel_alive("https://mine.ngrok.io") is False


def test_an_empty_list_is_unknown_not_dead(monkeypatch) -> None:
    """A false negative tears the tunnel down and mints a NEW hostname, which
    kills every link already handed out — so "cannot tell" answers alive."""
    monkeypatch.setattr(
        providers,
        "_import_ngrok",
        lambda: types.SimpleNamespace(get_listeners=lambda: []),
    )
    assert providers._ngrok_tunnel_alive("https://mine.ngrok.io") is True


def test_an_sdk_without_the_api_stays_alive(monkeypatch) -> None:
    monkeypatch.setattr(providers, "_import_ngrok", lambda: types.SimpleNamespace())
    assert providers._ngrok_tunnel_alive("https://mine.ngrok.io") is True


# --- the two CLI transports ---------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        (lhr_line("abc.lhr.life"), "https://abc.lhr.life"),
        (lhr_line("abc.lhr.life").replace("\n", "/\n"), "https://abc.lhr.life"),
        ("authenticated as anonymous user\n", None),
        ("some unrelated output\n", None),
    ],
)
def test_localhost_run_url_parsing(line, expected) -> None:
    assert LocalhostRunTunnel()._parse_url(line) == expected


@pytest.mark.parametrize(
    "line, expected",
    [
        (
            CF_BANNER.splitlines(keepends=True)[1],
            "https://random-words.trycloudflare.com",
        ),
        ("an unrelated line\n", None),
        ("https://not-a-quick-tunnel.example.com\n", None),
    ],
)
def test_cloudflare_url_parsing(line, expected) -> None:
    assert CloudflareQuickTunnel()._parse_url(line) == expected


def test_localhost_run_forwards_the_port_without_a_prompt(monkeypatch) -> None:
    """Unattended, so no host-key prompt can be answered and no key is read;
    `ExitOnForwardFailure` so a taken remote port exits rather than holding a
    session that tunnels nothing."""
    argv = LocalhostRunTunnel()._argv("ssh", 24038)
    assert argv[0] == "ssh"
    assert argv[-1] == "nokey@localhost.run"
    assert "-R" in argv and "80:localhost:24038" in argv
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "ExitOnForwardFailure=yes" in argv
    assert "-T" in argv  # line-oriented stdout is what gets parsed


def test_the_ssh_binary_option_is_honoured() -> None:
    assert LocalhostRunTunnel({"binary": "/opt/ssh"})._binary == "/opt/ssh"
    # The legacy key, which reads better in a config for an SSH provider.
    assert LocalhostRunTunnel({"ssh": "/opt/ssh"})._binary == "/opt/ssh"


def test_a_malformed_remote_port_is_refused() -> None:
    """DESIGN.md §8.1 declares exactly one warn-instead-of-refuse in this section — an
    unknown provider *name* — and says a malformed *shape* is refused.  A bad
    value in a known option is that same mistake: reading "not a number" as 80
    silently forwards a different remote port than the config wrote."""
    with pytest.raises(ValueError, match=r"remote_port 'not a number'"):
        LocalhostRunTunnel({"remote_port": "not a number"})
    # An absent option is still the documented default, and a written one is
    # honoured — only a malformed value is a mistake worth refusing.
    assert LocalhostRunTunnel({})._remote_port == 80
    assert LocalhostRunTunnel({"remote_port": "8080"})._remote_port == 8080


def test_cloudflare_runs_a_quick_tunnel_for_the_local_port() -> None:
    argv = CloudflareQuickTunnel()._argv("cloudflared", 24038)
    assert argv[:2] == ["cloudflared", "tunnel"]
    assert "--no-autoupdate" in argv  # a self-update must not swap the binary
    assert "http://localhost:24038" in argv


def test_cloudflare_defaults_to_http2(monkeypatch) -> None:
    """QUIC is cloudflared's own default and the one that dies behind a proxy
    or a TUN adapter — registering, then losing the connection repeatedly."""
    argv = CloudflareQuickTunnel()._argv("cloudflared", 24038)
    assert argv[argv.index("--protocol") + 1] == "http2"
    other = CloudflareQuickTunnel({"protocol": "quic"})._argv("cloudflared", 24038)
    assert other[other.index("--protocol") + 1] == "quic"


def test_a_missing_binary_is_terminal_and_names_itself(monkeypatch) -> None:
    install_missing_binary(monkeypatch)
    with pytest.raises(RuntimeError, match="cloudflared not found"):
        CloudflareQuickTunnel().start(24038)
    with pytest.raises(RuntimeError, match="ssh client not found"):
        LocalhostRunTunnel().start(24038)


def test_the_resolved_binary_path_is_reported(monkeypatch) -> None:
    """A CLI binary is found on PATH first, then by guessed install locations.

    Which one ran is otherwise invisible — the provider's name does not say it,
    and a PATH copy and a ``Program Files`` copy are different programs — so the
    resolved path is reported once it has been found.
    """
    lines = CF_BANNER.splitlines(keepends=True) + [CF_REGISTERED]
    install_popen(monkeypatch, LinesStdout(lines))
    monkeypatch.setattr(
        providers, "_which", lambda binary, extra=(): "/opt/cf/cloudflared"
    )
    tunnel = CloudflareQuickTunnel()
    tunnel.start(24038)
    assert tunnel.binary_path == "/opt/cf/cloudflared"
    assert tunnel.status()["binary"] == "/opt/cf/cloudflared"
    tunnel.stop()


# --- spawning, readiness and the reader ---------------------------------------


def test_localhost_run_publishes_the_url_it_prints(monkeypatch) -> None:
    install_popen(monkeypatch, LinesStdout([lhr_line("abc.lhr.life")]))
    tunnel = LocalhostRunTunnel()
    assert tunnel.start(24038) == "https://abc.lhr.life"
    assert tunnel.is_active is True
    tunnel.stop()


def test_a_child_that_dies_early_reports_its_output(monkeypatch) -> None:
    """The CLI's own diagnostics explain the exit, and they are the only
    explanation there is."""
    one_attempt(monkeypatch)
    install_popen(
        monkeypatch,
        LinesStdout(["bind: Address already in use\n"]),
        returncode=1,
    )
    with pytest.raises(RuntimeError, match="exited before the tunnel was up"):
        CloudflareQuickTunnel().start(24038)


def test_silence_times_out_instead_of_hanging(monkeypatch) -> None:
    one_attempt(
        monkeypatch,
        read_timeout=0.3,  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget
    install_popen(monkeypatch, SilentStdout())
    with pytest.raises(RuntimeError, match="timed out"):
        CloudflareQuickTunnel().start(24038)


def test_a_url_without_a_connector_is_never_published(monkeypatch) -> None:
    """cloudflared prints the hostname BEFORE the edge can route to it, and in
    between every fetch answers HTTP 530 — so the URL is not its own proof of
    readiness, and publishing it here is handing out a dead link."""
    one_attempt(
        monkeypatch,
        read_timeout=0.3,  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget
    banner = CF_BANNER.splitlines(keepends=True)
    install_popen(monkeypatch, lambda: BannerThenSilent(banner))
    with pytest.raises(RuntimeError, match="registered no connection to the edge"):
        CloudflareQuickTunnel().start(24038)


def test_the_url_publishes_once_a_connector_registers(monkeypatch) -> None:
    lines = CF_BANNER.splitlines(keepends=True) + [CF_REGISTERED]
    install_popen(monkeypatch, LinesStdout(lines))
    tunnel = CloudflareQuickTunnel()
    assert tunnel.start(24038) == "https://random-words.trycloudflare.com"
    tunnel.stop()


def test_localhost_run_needs_no_registration_vocabulary(monkeypatch) -> None:
    """Its URL is printed by the edge that just handed it over, so the URL is
    itself the proof — no second signal to wait for."""
    install_popen(monkeypatch, LinesStdout([lhr_line("abc.lhr.life")]))
    tunnel = LocalhostRunTunnel()
    tunnel.start(24038)
    assert tunnel._registered == set()
    tunnel.stop()


def test_a_rotated_hostname_is_swapped_in_place() -> None:
    """localhost.run's anonymous tier rotates it every few hours.  A hostname
    that changes under a live tunnel is swapped rather than dropped: a link
    already handed to a person or a model is worth keeping alive.

    The child is installed as the *current* one, which is what `_spawn` does
    before it starts the reader (`self._proc = proc`, then the thread): a reader
    drives the attempt it belongs to, and one that is not the current child is
    silent (`test_a_reader_torn_down_with_its_child_does_not_write_to_it`).
    """
    tunnel = LocalhostRunTunnel()
    tunnel._public_url = "https://old.lhr.life"
    providers.os.environ["SLIFE2_FILE_TUNNEL_URL"] = "https://old.lhr.life"
    tunnel._proc = FakeProcess(LinesStdout([lhr_line("new.lhr.life")]))

    tunnel._read_output(tunnel._proc)

    assert tunnel.public_url == "https://new.lhr.life"
    assert providers.os.environ["SLIFE2_FILE_TUNNEL_URL"] == "https://new.lhr.life"
    assert tunnel.status() == {"state": "active", "url": "https://new.lhr.life"}


def test_a_superseded_readers_death_is_not_the_live_attempts_eof() -> None:
    """**A retry starts a reader that the previous attempt can end.**

    `_spawn` clears `_eof_event` for the attempt it is starting and then starts a
    new reader, while the old one is still draining the old child's pipe.  When
    that old reader finally dies its `finally` set the event — the *new*
    attempt's — so its wait loop raised "closed its output without a tunnel
    URL" over a child that was still coming up, and the start failed for a
    reason that had already been fixed.
    """
    tunnel = LocalhostRunTunnel()
    tunnel._proc = FakeProcess(LinesStdout([]))  # the attempt that is live now

    tunnel._read_output(FakeProcess(LinesStdout([lhr_line("stale.lhr.life")])))

    assert not tunnel._eof_event.is_set(), (
        "the live attempt was told its child had died"
    )
    assert tunnel._pending_url is None, "a superseded reader fed the new attempt"


def test_a_reader_torn_down_with_its_child_does_not_write_to_it() -> None:
    """**`_kill_proc` sets `_proc` to None, and that is not "I am current".**

    The guard on the reader used to be `self._proc is not None and proc is not
    self._proc`, so the window between `stop()` (or a restarted attempt) setting
    `_proc` to None and the child's pipe closing let every line still in the pipe
    be applied: a URL from a dead child was written into `_pending_url` and could
    satisfy the *next* start's URL event with a hostname nobody serves.
    """
    tunnel = LocalhostRunTunnel()
    assert tunnel._proc is None  # torn down, and the reader has not noticed yet

    tunnel._read_output(FakeProcess(LinesStdout([lhr_line("dead.lhr.life")])))

    assert tunnel._pending_url is None
    assert tunnel._public_url is None
    assert not tunnel._url_event.is_set()


def test_a_dead_reader_warns_and_names_the_child(caplog) -> None:
    """Once a tunnel is up this thread is what swaps in a rotated hostname.

    A reader that dies at debug level stops that swap with nothing said — the
    published URL then goes stale and every later fetch fails, invisibly.  The
    warning names the child so the failure can be traced to one spawn.
    """

    class ExplodingStdout:
        closed = False

        def __iter__(self):
            return self

        def __next__(self):
            raise OSError("pipe closed")

        def close(self):
            self.closed = True

    tunnel = LocalhostRunTunnel()
    tunnel._proc = FakeProcess(ExplodingStdout())
    with caplog.at_level(logging.WARNING):
        tunnel._read_output(tunnel._proc)

    assert tunnel._eof_event.is_set()
    assert any(
        "tunnel_reader_died" in record.getMessage()
        and "localhost.run" in record.getMessage()
        for record in caplog.records
    )


def test_is_alive_follows_the_child() -> None:
    tunnel = LocalhostRunTunnel()
    assert tunnel.is_alive() is False  # never started
    tunnel._proc = FakeProcess(LinesStdout([]))
    assert tunnel.is_alive() is True
    tunnel._proc.returncode = 1  # the child died
    assert tunnel.is_alive() is False


def test_stop_kills_the_child_and_is_idempotent(monkeypatch) -> None:
    install_popen(monkeypatch, LinesStdout([lhr_line("abc.lhr.life")]))
    tunnel = LocalhostRunTunnel()
    tunnel.start(24038)
    child = tunnel._proc
    tunnel.stop()
    assert child.terminated is True
    assert tunnel.public_url is None
    tunnel.stop()  # no second kill, no error


# --- cloudflare's liveness, and the trap in its output ------------------------


def test_a_lost_connection_that_is_never_unregistered(monkeypatch) -> None:
    """The one that cost a day: a QUIC timeout is logged as "Serve tunnel
    error" and retried with NO "Unregistered" line, so a connector set scraped
    from stdout stays full straight through an outage that answers every
    published link with HTTP 530."""
    lines = CF_BANNER.splitlines(keepends=True) + [CF_REGISTERED, CF_SERVE_ERROR]
    install_popen(monkeypatch, LinesStdout(lines))
    tunnel = CloudflareQuickTunnel()
    tunnel.start(24038)
    # The scraped set is fooled...
    assert tunnel._registered == {"0"}
    # ...and the child's own metrics API is what answers instead.
    monkeypatch.setattr(tunnel, "_metrics_ready", lambda: False)
    assert tunnel._transport_alive() is False
    tunnel.stop()


def test_a_healthy_transport_reads_as_alive(monkeypatch) -> None:
    lines = CF_BANNER.splitlines(keepends=True) + [CF_REGISTERED]
    install_popen(monkeypatch, LinesStdout(lines))
    tunnel = CloudflareQuickTunnel()
    tunnel.start(24038)
    monkeypatch.setattr(tunnel, "_metrics_ready", lambda: True)
    assert tunnel._transport_alive() is True
    tunnel.stop()


def test_losing_every_connector_reads_as_not_alive(monkeypatch) -> None:
    lines = CF_BANNER.splitlines(keepends=True) + [CF_REGISTERED, CF_UNREGISTERED]
    install_popen(monkeypatch, LinesStdout(lines))
    tunnel = CloudflareQuickTunnel()
    tunnel.start(24038)
    monkeypatch.setattr(tunnel, "_metrics_ready", lambda: None)  # no metrics API
    assert tunnel._transport_alive() is False
    tunnel.stop()


def test_a_failed_probe_is_not_permission_to_trust_the_scrape(monkeypatch) -> None:
    """A metrics server that was announced and could not answer is *unknown*.

    The scraped connector set stays full straight through a QUIC outage — no
    "Unregistered" line follows a "Serve tunnel error" — so letting it stand in
    for the answer the probe could not give is exactly how a flapping tunnel
    reports healthy.  DESIGN.md §8.1 declares the scrape only for a build that announces
    no metrics server, which this is not.
    """

    def refuse(**kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(providers.httpx2, "Client", refuse)
    tunnel = CloudflareQuickTunnel()
    tunnel._registered = {"0"}
    tunnel._metrics_base = "http://127.0.0.1:20241"
    assert tunnel._metrics_ready() is None
    assert tunnel._transport_alive() is False, "the scrape is not a substitute"


def test_without_a_metrics_server_the_scraped_set_is_the_answer() -> None:
    """DESIGN.md §8.1's boundary: the scrape is the fallback for a build that announces
    no metrics server to ask — which is the only case that licenses it."""
    tunnel = CloudflareQuickTunnel()
    tunnel._registered = {"0"}
    assert tunnel._metrics_base is None
    assert tunnel._transport_alive() is True


def test_the_metrics_port_is_picked_up_from_the_child(monkeypatch) -> None:
    """The child names it once, early — before it registers — and it is what
    lets the liveness question be asked of the transport itself rather than
    read off its prose."""
    one_attempt(
        monkeypatch,
        read_timeout=0.3,  # timeouts: the value this test is built around, not a budget
    )  # timeouts: the value this test is built around, not a budget
    install_popen(monkeypatch, LinesStdout([CF_METRICS]))
    tunnel = CloudflareQuickTunnel()
    with pytest.raises(RuntimeError):
        tunnel.start(24038)  # no URL: it only announced the metrics server
    assert tunnel._metrics_base == "http://127.0.0.1:20241"


@pytest.mark.parametrize(
    "count, expected",
    [(0, False), (1, True), (3, True), (None, False)],
)
def test_the_probe_reads_ready_connections(monkeypatch, count, expected) -> None:
    """`readyConnections` is the connector's own view of the edge — the one
    liveness signal that does not depend on the child narrating itself."""

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            return types.SimpleNamespace(json=lambda: {"readyConnections": count})

    monkeypatch.setattr(providers.httpx2, "Client", FakeClient)
    tunnel = CloudflareQuickTunnel()
    tunnel._metrics_base = "http://127.0.0.1:20241"
    assert tunnel._metrics_ready() is expected


def test_no_metrics_server_means_nothing_to_ask() -> None:
    assert CloudflareQuickTunnel()._metrics_ready() is None


# --- the fake-ip accusation ---------------------------------------------------


@pytest.mark.parametrize(
    "address, public",
    [
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("2001:4860:4860::8888", True),
        ("198.18.0.32", False),  # the fake-ip pool a Clash TUN synthesises
        ("127.0.0.1", False),
        ("192.168.1.5", False),
        ("224.0.0.1", False),  # multicast is not a destination
        ("", False),
        ("not-an-address", False),
        (None, False),
    ],
)
def test_which_addresses_are_real_destinations(address, public) -> None:
    assert is_public_address(address) is public


def test_a_fake_ip_edge_is_flagged(monkeypatch) -> None:
    """A tunnel that registers and then dies every 30-60s, with nothing in the
    log to blame — this is the cause, and it is only visible here."""
    monkeypatch.setattr(providers, "_resolver_fakes", True)
    tunnel = CloudflareQuickTunnel()
    tunnel._edge_ip = "198.18.0.32"
    assert tunnel.edge_via_proxy is True


def test_a_real_edge_address_is_not_flagged(monkeypatch) -> None:
    """A globally routable edge is a real destination, so a tunnel flapping
    against one is somebody else's problem.  (Not `203.0.113.x`: the
    documentation ranges are not globally routable, which is exactly the
    distinction `is_public_address` is making.)"""
    monkeypatch.setattr(providers, "_resolver_fakes", True)
    tunnel = CloudflareQuickTunnel()
    tunnel._edge_ip = "104.16.132.229"
    assert tunnel.edge_via_proxy is False
    assert tunnel.edge_ip == "104.16.132.229"


def test_an_unobserved_edge_is_not_flagged(monkeypatch) -> None:
    """Empty means "not observed", never "fine" — but it is also not an
    accusation."""
    monkeypatch.setattr(providers, "_resolver_fakes", True)
    assert CloudflareQuickTunnel().edge_via_proxy is False


def test_a_lying_resolver_is_what_makes_it_an_accusation(monkeypatch) -> None:
    """On a machine whose resolver answers honestly, a non-public edge address
    is a LAN address and not an interception."""
    monkeypatch.setattr(providers, "_resolver_fakes", False)
    tunnel = CloudflareQuickTunnel()
    tunnel._edge_ip = "198.18.0.32"
    assert tunnel.edge_via_proxy is False


def test_the_edge_address_is_really_read_from_the_child(monkeypatch) -> None:
    lines = CF_BANNER.splitlines(keepends=True) + [CF_REGISTERED]
    install_popen(monkeypatch, LinesStdout(lines))
    tunnel = CloudflareQuickTunnel()
    tunnel.start(24038)
    assert tunnel.edge_ip == "203.0.113.7"
    tunnel.stop()
