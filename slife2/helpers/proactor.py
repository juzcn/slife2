"""The Windows Proactor loop's connection close, when the peer has reset it.

**What Windows does.**  A Proactor transport ends a connection by calling
`socket.shutdown(SHUT_RDWR)` and then `close()`.  A peer that leaves in the
middle of a streamed response — the ordinary way an MCP client quits, with an
SSE body still arriving — has already reset the connection, and Windows answers
that `shutdown` with `ConnectionResetError [WinError 10054]`.  The exception
escapes `_ProactorBasePipeTransport._call_connection_lost`, so asyncio logs an
unhandled callback error *and the cleanup after the shutdown never runs*: the
socket is not closed, and a transport on a server is never detached from it
(`python/cpython#160429`).  A normal exit therefore writes a traceback that
names no fault anybody can act on, and a daemon that outlives its clients keeps
a socket and a transport for every such disconnect.

**Fixed by wrapping, not by replacing.**  The patched method puts a socket in
front of the real one whose `shutdown` answers that one exception with silence,
then calls the original.  CPython's cleanup body is not copied: whatever a
later Python adds to it still runs, and the wrapper exists only for the length
of that call.

**Only that exception, and only on that socket.**  A `shutdown` that fails for
any other reason still raises, every other method is forwarded untouched, and
the patch is on the Proactor transport alone — the one transport whose close
sends a FIN a reset connection cannot take.

**Nothing that can still be acted on is dropped.**  The connection was already
gone — that is what the reset says — so the only report silenced is about a
close that was already happening.  `protocol.connection_lost` runs before the
shutdown and is untouched, and a `close` that fails afterwards still raises and
still reaches the log.
"""

from __future__ import annotations

import socket as _socket
import sys
from typing import Any


class _ResetTolerantSocket:
    """A socket whose `shutdown` lets an already-reset connection go quietly.

    `shutdown` is answered through `__getattr__` rather than defined here so
    that `hasattr(sock, "shutdown")` still tells the truth: `_call_connection_lost`
    guards the call with it, and a pipe — which the same transport class also
    carries — is supposed to fail that check.
    """

    __slots__ = ("_socket",)

    def __init__(self, sock: _socket.socket) -> None:
        self._socket = sock

    def __getattr__(self, name: str) -> Any:
        if name != "shutdown":
            return getattr(self._socket, name)
        shutdown = self._socket.shutdown  # AttributeError for a pipe

        def _quiet_shutdown(how: int) -> None:
            try:
                shutdown(how)
            except ConnectionResetError:
                # The peer reset the connection; that is why this is closing it.
                pass

        return _quiet_shutdown


#: Marks the patched method, so a second install over the same process does not
#: put a second wrapper in front of the first.
_INSTALLED = "_slife2_reset_tolerant"


def tolerate_reset_on_close() -> bool:
    """Make the Proactor transport finish closing a connection the peer reset.

    Called once per process, by every entry point that runs a loop
    (`slife2.main` for a client, `serve_plugin` for a server).  Answers whether
    this build has the callback at all: another platform, or a future Python
    that reworks the cleanup, has nothing to fix, and that is not an error.
    """
    if sys.platform != "win32":  # pragma: no cover — the Proactor loop is Windows'
        return False

    from asyncio import proactor_events

    transport = getattr(proactor_events, "_ProactorBasePipeTransport", None)
    if transport is None:
        return False
    method = getattr(transport, "_call_connection_lost", None)
    if method is None:
        return False
    if getattr(method, _INSTALLED, False):
        return True

    def _call_connection_lost(self: Any, exc: BaseException | None) -> Any:
        sock = self._sock
        if sock is not None and not isinstance(sock, _ResetTolerantSocket):
            self._sock = _ResetTolerantSocket(sock)
        return method(self, exc)

    setattr(_call_connection_lost, _INSTALLED, True)
    transport._call_connection_lost = _call_connection_lost
    return True
