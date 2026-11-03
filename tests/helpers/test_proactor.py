"""The Windows Proactor's close, when the peer has already reset the connection.

The failure is not one a caller sees: `_call_connection_lost` is a callback, so
asyncio logs the escaping exception as an unhandled error — and because the
exception leaves the `finally` before `close()`, the socket and the server's
transport are left behind (`python/cpython#160429`).  Asking the callback
directly is therefore the only seam there is.
"""

from __future__ import annotations

import sys
from asyncio import proactor_events

import pytest

from slife2.helpers import proactor

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        sys.platform != "win32", reason="the Proactor loop only runs on Windows"
    ),
]


class _Protocol:
    def __init__(self) -> None:
        self.lost: list[BaseException | None] = []

    def connection_lost(self, exc: BaseException | None) -> None:
        self.lost.append(exc)


class _Server:
    def __init__(self) -> None:
        self.detached: list[object] = []

    def _detach(self, transport: object) -> None:
        self.detached.append(transport)


class _Socket:
    """The part of a socket the close uses, with one call made to fail."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.closed = False
        self.shutdowns = 0

    def fileno(self) -> int:
        return 1

    def shutdown(self, how: int) -> None:
        self.shutdowns += 1
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


class _Pipe:
    """A socket-shaped object without `shutdown`, as the same transport sees one."""

    def __init__(self) -> None:
        self.closed = False

    def fileno(self) -> int:
        return 1

    def close(self) -> None:
        self.closed = True


class _Transport:
    """What `_call_connection_lost` touches, and nothing a real transport needs."""

    def __init__(self, sock: object) -> None:
        self._sock: object | None = sock
        self._protocol = _Protocol()
        self._server: _Server | None = _Server()
        self._called_connection_lost = False


def _callback():
    proactor.tolerate_reset_on_close()
    return proactor_events._ProactorBasePipeTransport._call_connection_lost


def test_a_reset_during_the_close_does_not_stop_the_close() -> None:
    """**The whole fix.**  Windows answers `shutdown` on a reset connection with
    `WinError 10054`; that one failure is what the peer already did, and the
    close, the socket release and the server detach that follow it are what the
    transport asked for.  All three have to run."""
    sock = _Socket(ConnectionResetError(10054, "reset"))
    transport = _Transport(sock)
    server = transport._server
    assert server is not None

    _callback()(transport, None)

    assert sock.closed
    assert transport._sock is None
    assert transport._protocol.lost == [None]
    assert server.detached == [transport]


def test_a_pipe_keeps_the_shutdown_guard() -> None:
    """A pipe has no `shutdown`, and the original skips the call for it.  The
    wrapper must not count as having one, or every subprocess pipe would close
    with an `AttributeError` where the guard used to be."""
    pipe = _Pipe()
    transport = _Transport(pipe)

    _callback()(transport, None)

    assert pipe.closed
    assert transport._sock is None


def test_another_shutdown_failure_still_raises() -> None:
    """Only the reset is expected.  A shutdown that fails for any other reason
    is still a fault, and swallowing it would hide one."""
    transport = _Transport(_Socket(ValueError("not a reset")))

    with pytest.raises(ValueError, match="not a reset"):
        _callback()(transport, None)


def test_the_patch_is_installed_once() -> None:
    """`main` and `serve_plugin` both ask, and a process that does both — a test,
    or a future in-process server — must not stack wrappers."""
    proactor.tolerate_reset_on_close()
    first = proactor_events._ProactorBasePipeTransport._call_connection_lost

    proactor.tolerate_reset_on_close()

    assert proactor_events._ProactorBasePipeTransport._call_connection_lost is first


def test_a_build_without_the_proactor_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another platform, or a future Python that reworks the callback, is not a
    failure: there is nothing to fix and the answer says so."""
    monkeypatch.setattr(proactor_events, "_ProactorBasePipeTransport", None)

    assert proactor.tolerate_reset_on_close() is False
