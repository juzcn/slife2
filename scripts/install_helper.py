"""The two facts the installer needs from inside the project.

`seed` puts the shipped configuration where slife2 will read it, without ever
overwriting a file the user already has.  `require` reads the *effective*
configuration with the project's own loader and prints every external program
it names, with the place that names it.  The installer maps those names to
packages and installs them; nothing here installs anything.

    python install_helper.py seed --source <repo>/slife2.yaml --home <dir> [--dry-run]
    python install_helper.py require --home <dir>

`seed` is stdlib-only and can run on any Python 3.  `require` imports slife2,
so it runs with the project virtualenv's interpreter after `uv sync`.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from pathlib import Path

CONFIG_NAME = "slife2.yaml"
#: The reference copy written when the live configuration already exists.  A
#: name slife2 never reads: `find_config_path` looks for `slife2.yaml` only.
REFERENCE_NAME = "slife2-default.yaml"


def _say(message: str) -> None:
    print(message)


def _warn(message: str) -> None:
    print(f"config: {message}", file=sys.stderr)


def seed(source: Path, home: Path, dry_run: bool) -> int:
    """Place the shipped configuration, or a reference copy of it.

    **The live file is never overwritten.**  A checkout's own `slife2.yaml` is
    already the live configuration, so seeding it beside itself is a no-op; any
    other home gets the shipped file as `slife2.yaml` when it has none, and as
    `slife2-default.yaml` when it has one.  The second name is a reference for
    a person to compare and merge, not something slife2 reads.
    """
    if not source.is_file():
        _warn(
            f"no shipped configuration at {source}; slife2 will use its "
            "built-in defaults."
        )
        return 0

    live = home / CONFIG_NAME
    reference = home / REFERENCE_NAME
    try:
        source = source.resolve()
        home = home.resolve()
        live = home / CONFIG_NAME
        reference = home / REFERENCE_NAME
        if source.parent == home:
            _say(
                f"config: checkout detected; {live} is the live configuration; nothing to seed."
            )
            return 0
        if not live.exists():
            if dry_run:
                _say(f"config: would create {live} from the shipped configuration.")
                return 0
            home.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, live)
            _say(f"config: created {live} from the shipped configuration.")
            return 0

        shipped = source.read_bytes()
        if live.read_bytes() == shipped:
            _say(f"config: {live} already matches the shipped configuration.")
            return 0
        if reference.is_file() and reference.read_bytes() == shipped:
            _say(f"config: {reference} already matches the shipped configuration.")
            return 0
        if dry_run:
            _say(f"config: would write the shipped configuration to {reference}.")
            return 0
        home.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, reference)
        _say(
            f"config: {live} already exists and was left untouched. The shipped "
            f"configuration is at {reference}; compare and merge by hand."
        )
        return 0
    except OSError as exc:
        _warn(f"{exc}")
        return 1


def _first_word(command: str) -> str:
    """The program a configured command line names.

    The same split `slife2.plugins.cli.server._missing_program` makes, because
    both answer the same question: what has to be on PATH for this entry to
    run.  A line that cannot be split names nothing here; the config loader
    refuses those before an entry exists.
    """
    try:
        words = shlex.split(command, posix=False)
    except ValueError:
        return ""
    return words[0].strip("\"'") if words else ""


def _add(needed: dict[str, set[str]], executable: str, origin: str) -> None:
    executable = executable.strip()
    if executable:
        needed.setdefault(executable, set()).add(origin)


def require(home: Path) -> int:
    """Print `<executable><TAB><origin>` for every program the config names.

    **The effective configuration, not a copy of the shipped one.**  The home
    is set before the import so the project's own `load()` resolves the same
    `slife2.yaml` the server would; a checkout therefore yields the checkout's
    configuration, and an installed home yields its own.
    """
    os.environ["SLIFE2_DATA_DIR"] = str(home)

    from slife2.libs.host.config import ConfigError, find_config_path, load

    if find_config_path() is None:
        _warn(f"no {CONFIG_NAME} in {home}; using the built-in defaults.")
    try:
        config = load()
    except ConfigError as exc:
        _warn(str(exc))
        return 2

    needed: dict[str, set[str]] = {}
    _add(needed, "uv", "bootstrap")
    _add(needed, "python", "bootstrap")

    if config.peer_agent.configured and config.peer_agent.provider == "mosquitto":
        _add(needed, "mosquitto", "peer_agent.provider")
    if (
        config.subagent.network.configured
        and config.subagent.network.provider == "mosquitto"
    ):
        _add(needed, "mosquitto", "subagent.provider")

    # The provider classes own the default binary names; reading them here is
    # what keeps this helper from spelling `ssh`/`cloudflared` a second time.
    from slife2.plugins.file_tunnel.providers import (
        CloudflareQuickTunnel,
        LocalhostRunTunnel,
    )

    tunnel_binaries = {
        "localhost.run": LocalhostRunTunnel.default_binary,
        "cloudflare": CloudflareQuickTunnel.default_binary,
    }
    for name in config.file_tunnel.providers:
        options = config.file_tunnel.options_for(name)
        binary = str(options.get("binary") or tunnel_binaries.get(name, ""))
        if binary:
            _add(needed, binary, f"file-tunnel.providers.{name}")

    for name, entry in config.mcp.items():
        _add(needed, _first_word(entry.command), f"mcp:{name}")
    for name, entry in config.rest_apis.items():
        _add(needed, _first_word(entry.command), f"rest-api:{name}")
    for name, entry in config.cli.items():
        _add(needed, _first_word(entry.command), f"cli:{name}")

    skills = home / "skills"
    if skills.is_dir():
        from slife2.plugins.skills.playbooks import scan

        for skill in scan(skills):
            for executable in skill.requires.bins:
                _add(needed, executable, f"skill:{skill.name}.requires.bins")

    for executable in sorted(needed):
        print(f"{executable}\t{','.join(sorted(needed[executable]))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install_helper.py")
    commands = parser.add_subparsers(dest="command", required=True)

    seed_parser = commands.add_parser("seed", help="place the shipped configuration")
    seed_parser.add_argument("--source", required=True)
    seed_parser.add_argument("--home", required=True)
    seed_parser.add_argument("--dry-run", action="store_true")

    require_parser = commands.add_parser("require", help="list configured programs")
    require_parser.add_argument("--home", required=True)

    args = parser.parse_args(argv)
    if args.command == "seed":
        return seed(Path(args.source), Path(args.home), args.dry_run)
    return require(Path(args.home))


if __name__ == "__main__":
    raise SystemExit(main())
