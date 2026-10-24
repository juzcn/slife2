"""Container shim for credstore's WSL detection.

Docker Desktop's Linux kernel reports "microsoft" in /proc/version, so
credstore's is_wsl() returns True and it selects WslBackend, which tries to
run powershell.exe. Inside a Linux container that executable does not exist,
and the resulting FileNotFoundError escapes instead of leaving an unresolved
${VAR} reference alone.

The container is not WSL. Treat it as native Linux: credstore then falls back
to its Linux keyring backend, and where that is unavailable (the usual case
under Docker's seccomp policy) get_credential() returns None, which is the
documented "leave the reference verbatim" behavior.

**Installed into the venv's site-packages** (see `Dockerfile`), not placed on
`PYTHONPATH`.  A task container is built from the image's filesystem, which
carries no environment, so a shim reached only by an `ENV` line would vanish
with it; a `sitecustomize` beside the packages is loaded by every interpreter
that uses the venv.
"""

from __future__ import annotations


def _not_wsl() -> bool:
    return False


try:
    import credstore._backend as _backend
    import credstore._platform as _platform
except Exception:  # noqa: BLE001 - the shim must never break interpreter startup
    pass
else:
    _platform.is_wsl = _not_wsl
    _backend.is_wsl = _not_wsl
