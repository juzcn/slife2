"""Install every package the shipped config launches, at image build time.

**A package installed on first use is a package installed inside the trial.**
The bench's task containers unpack this image and then start the shipped
configuration from cold; the tool gate waits for each enabled `mcp:`/
`rest-api:` source to report `ready` within the trial's connect budget, and a
cold `npm`/`uv` install is what that budget cannot cover —
`@aryanbv/pdf-toolkit-mcp` alone takes longer than it.  So each source is
*installed* here, into the same cache entry its launcher would use: `npx
--package=<spec> -- node -e ""` fills the `_npx/<hash>` tree `npx -y <spec>`
builds, `uvx --from <pkg> python -c ""` builds the environment `uvx <pkg>`
reuses.  The package's install scripts run too, which is what pulls Chrome for
Testing into `~/.cache/puppeteer` for the chrome-devtools source.

**Nothing is started.**  A server that ignores EOF cannot hang the build, and
an exit code means what it says: the install succeeded or it did not.

**The list comes from the config, not from this file.**  A new `mcp:` or
`rest-api:` entry is warmed by existing; `enabled: false` is skipped because
the hub skips it; a source with no `command` (a remote `url:`) has nothing to
install; a command that is neither `npx` nor `uvx` is a program
`scripts/install.sh` already installed.

A failed entry fails the build.  The bench's readiness gate has no fallback for
a source that never loads, and an image that silently lacks one is exactly the
invisible failure this step exists to prevent.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from slife2.libs.host.config import load_cached

#: One package's install.  Generous: a browser download alone is minutes.
TIMEOUT = float(os.environ.get("SLIFE2_PREWARM_TIMEOUT", "900"))
ATTEMPTS = 3
#: Installs are network-bound; running a few at once is what keeps the build
#: from paying every cold `npm`/`uv` install one after another.
JOBS = int(os.environ.get("SLIFE2_PREWARM_JOBS", "4"))

#: `uvx` flags that take the next token as their value rather than as the tool
#: to run.  A flag this table does not know is passed through as a boolean.
UV_VALUE_FLAGS = {
    "--with",
    "--from",
    "--python",
    "-p",
    "--index",
    "--default-index",
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "--env-file",
    "--cache-dir",
    "--constraints",
    "-c",
    "--overrides",
    "--config-file",
    "--directory",
    "--project",
    "--refresh-package",
    "--keyring-provider",
    "--prerelease",
    "--index-strategy",
    "--resolution",
    "--exclude-newer",
    "--link-mode",
    "--color",
}


def entries():
    """Every enabled source the config names that starts a local process."""
    config = load_cached()
    for name, settings in {**config.mcp, **config.rest_apis}.items():
        if settings.enabled and settings.command:
            yield name, settings


def install_command(settings) -> list[str] | None:
    """The command that installs this source's package, or None if there is none."""
    program = settings.command
    launcher = os.path.basename(program).lower()
    args = list(settings.args or [])
    if launcher in {"npx", "npx.cmd"}:
        spec = _npm_spec(args)
        if spec is None:
            return None
        # `npx` is `npm exec`; this asks for the same package and then runs the
        # one program every npm environment has, so the package is installed
        # and nothing else is run.
        return [program, "--yes", f"--package={spec}", "--", "node", "-e", ""]
    if launcher in {"uvx", "uvx.exe"}:
        found = _uvx_from(args)
        if found is None:
            return None
        flags, package = found
        return [program, *flags, "--from", package, "python", "-c", ""]
    return None


def _npm_spec(args: list[str]) -> str | None:
    """The package `npx` is being asked to launch: its first bare argument."""
    for arg in args:
        if arg.startswith("--package="):
            return arg.split("=", 1)[1]
        if arg.startswith("-"):
            continue
        return arg
    return None


def _uvx_from(args: list[str]) -> tuple[list[str], str] | None:
    """The flags to keep and the package whose environment they build."""
    flags: list[str] = []
    package: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            break
        if arg.startswith("--from="):
            package = arg.split("=", 1)[1]
        elif arg == "--from":
            if index < len(args):
                package = args[index]
                index += 1
        elif arg.startswith("-"):
            flags.append(arg)
            if arg in UV_VALUE_FLAGS and index < len(args):
                flags.append(args[index])
                index += 1
        else:
            if package is None:
                package = arg
            break
    return (flags, package) if package else None


def warm(name: str, settings) -> bool:
    command = install_command(settings)
    if command is None:
        print(f"prewarm: {name}: no package launcher; nothing to install", flush=True)
        return True
    env = {**os.environ, **settings.env}
    for attempt in range(1, ATTEMPTS + 1):
        print(f"prewarm: {name}: {' '.join(command)}", flush=True)
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=settings.cwd or None,
                timeout=TIMEOUT,
                text=True,
            )
        except subprocess.TimeoutExpired:
            print(
                f"prewarm: {name}: attempt {attempt} timed out after {TIMEOUT:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            continue
        if result.returncode == 0:
            print(f"prewarm: {name}: installed", flush=True)
            return True
        print(
            f"prewarm: {name}: attempt {attempt} exited {result.returncode}: "
            f"{(result.stdout or '')[-600:]}",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(2)
    return False


def main() -> int:
    work = list(entries())
    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        results = list(pool.map(lambda entry: warm(*entry), work))
    failed = [name for (name, _), ok in zip(work, results, strict=True) if not ok]
    if failed:
        print(f"prewarm: not installable: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
