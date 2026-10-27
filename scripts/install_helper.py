"""The facts the installer needs from inside the project.

`seed` puts the shipped configuration where slife2 will read it, without ever
overwriting a file the user already has, and copies the bundled `skills/` and
`jobs/` into the home **overwriting files of the same name** — the content
update, and deliberately not the versioned-backup dance the old installer did:
the bundle is the authority, and a person who edits a seeded file edits their
copy.  `require` reads the *effective* configuration with the
project's own loader and prints every external program it names, with the place
that names it — and, for a `cli:` entry, the line that installs it.  The
installer maps those names to packages and installs them; nothing here installs
anything.  `secrets` reads the same configuration for the credentials it cannot
resolve, so the installer can say which `credstore set` commands would make it
work.

    python install_helper.py seed --source <repo>/slife2.yaml --home <dir> [--dry-run]
    python install_helper.py require --home <dir>
    python install_helper.py secrets --home <dir>

`seed` is stdlib-only and can run on any Python 3.  `require` and `secrets`
import slife2, so they run with the project virtualenv's interpreter after
`uv sync`.
"""

from __future__ import annotations

import argparse
import os
import re
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


def _seed_config(source: Path, home: Path, dry_run: bool) -> int:
    """Place the shipped configuration, or a reference copy of it.

    **The live file is never overwritten.**  A checkout's own `slife2.yaml` is
    already the live configuration, so seeding it beside itself is a no-op; any
    other home gets the shipped file as `slife2.yaml` when it has none, and as
    `slife2-default.yaml` when it has one.  The second name is a reference for
    a person to compare and merge, not something slife2 reads.
    """
    if not source.is_file():
        _warn(
            f"no shipped configuration at {source}; the home gets no "
            f"{CONFIG_NAME}, and slife2 will not start without one."
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


#: The bundled content directories the installer seeds beside the config.
#: Both are things a person or a model *uses*, not project source: `skills/`
#: ships the playbooks and `jobs/` ships the code-defined tools.
SEED_TREES = ("skills", "jobs")


def seed_trees(source: Path, home: Path, dry_run: bool) -> int:
    """Copy the bundled `skills/` and `jobs/` into *home*, overwriting.

    **Overwrite is the whole update story.**  A reinstall replaces a bundled
    file with the one it ships; a skill or job the user added under a name of
    its own is not in the source and stays.  The v1 installer preserved a
    changed bundled file under a versioned name instead; this is deliberately
    simpler — the bundle is the authority, and editing a seeded file is editing
    a copy.

    **A copy that fails is a warning, not a fatal install.**  The configuration
    is in place and the install continues; a file held open by an editor or
    another slife2 is the common failure on Windows, and the fix is to close it
    and run the installer again.  Aborting the whole install over retryable
    content is exactly how a self-healing problem becomes a dead end.

    A checkout whose home is the checkout itself is a no-op: the source *is*
    the destination, so there is no update to make.  A home pointed elsewhere
    from a checkout is seeded like any other.
    """
    try:
        root = source.resolve()
        target = home.resolve()
        if root == target:
            _say("skills/jobs: checkout detected; nothing to seed.")
            return 0
        for name in SEED_TREES:
            src = root / name
            if not src.is_dir():
                _say(f"{name}: no bundled directory at {src}; nothing to seed.")
                continue
            dst = target / name
            if dry_run:
                _say(f"{name}: would copy {src} -> {dst}, overwriting same names.")
                continue
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                src,
                dst,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            _say(f"{name}: seeded {dst}; same names overwritten.")
        return 0
    except OSError as exc:
        print(
            f"skills/jobs: {exc}\n"
            "  the configuration is in place and the install continues; only "
            "the bundled skills/jobs could not be copied. Close whatever holds "
            "the file (an editor, another slife2) and run the installer again "
            "to finish seeding.",
            file=sys.stderr,
        )
        return 0


def seed(source: Path, home: Path, dry_run: bool) -> int:
    """Place the shipped configuration **and** the bundled skills and jobs.

    One step, the same source root: the configuration keeps its own rule (the
    live file is never overwritten — a changed shipped one is written beside it
    as a reference), while `skills/` and `jobs/` are copied over the home with
    files of the same name replaced.  A checkout whose home is the checkout
    itself is a no-op for both; a home pointed elsewhere from a checkout gets
    the same seeding as an installed one.
    """
    code = _seed_config(source, home, dry_run)
    if code:
        return code
    return seed_trees(source.parent, home, dry_run)


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


def _add(
    needed: dict[str, set[str]],
    hints: dict[str, str],
    executable: str,
    origin: str,
    hint: str = "",
) -> None:
    """Record one program, and the config's own answer to "how do I get it".

    The first hint wins: an executable named by two `cli:` entries is one
    program, and one install command is enough to act on.
    """
    executable = executable.strip()
    if not executable:
        return
    needed.setdefault(executable, set()).add(origin)
    if hint:
        # **One line per program.**  A tab or a newline in the operator's
        # sentence would split the line the installers read, so the hint keeps
        # its words and loses its layout.
        hints.setdefault(executable, " ".join(hint.split()))


#: The reference syntax `slife2.libs.host.config` resolves.  Kept here rather
#: than imported: this reads the file before the project decides whether it is
#: valid, and the loader's pattern is private.
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _record(found: dict[str, list[str]], name: str, origin: str) -> None:
    origins = found.setdefault(name, [])
    if origin not in origins:
        origins.append(origin)


def _references(value: object, path: str, found: dict[str, list[str]]) -> None:
    """Walk one configuration value, recording every credential it names."""
    if isinstance(value, dict):
        for key, item in value.items():
            _references(item, f"{path}.{key}" if path else str(key), found)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _references(item, f"{path}[{index}]", found)
    elif isinstance(value, str):
        if value.startswith("keyring:"):
            _record(found, value.removeprefix("keyring:"), path)
            return
        for match in _REFERENCE.finditer(value):
            # A `:-default` is a value, not a credential — the config supplies
            # what to use when nothing is stored (`${SSH_BINARY:-ssh}`).
            if match.group(2) is None:
                _record(found, match.group(1), path)


def secrets(home: Path) -> int:
    """Print the credentials the configuration references, and where.

    **A hint, not a check.**  Whether a key is already in the environment or
    in credstore is deliberately not asked: resolving every reference would
    consult the keyring once per key, and the operator can read that off
    `credstore list` — a key already configured is simply a line to ignore.

    One line per credential, in the order the file first names it:
    ``<credential><TAB><origin>, <origin>``.
    """
    path = home / CONFIG_NAME
    if not path.is_file():
        _warn(f"no {CONFIG_NAME} at {path}; nothing to suggest.")
        return 0

    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    try:
        raw = YAML(typ="safe").load(path) or {}
    except (YAMLError, OSError) as exc:
        _warn(f"cannot read {path}: {exc}")
        return 2

    found: dict[str, list[str]] = {}
    _references(raw, "", found)
    for name, origins in found.items():
        print(f"{name}\t{', '.join(origins)}")
    return 0


def require(home: Path) -> int:
    """Print `<executable><TAB><origin>[<TAB><install>]` for every program.

    **The effective configuration, not a copy of the shipped one.**  The home
    is set before the import so the project's own `load()` resolves the same
    `slife2.yaml` the server would; a checkout therefore yields the checkout's
    configuration, and an installed home yields its own.

    **The third column is the `cli:` entry's `install:` line**, and only when
    every origin naming the program is a `cli:` entry.  Those entries are the
    one family the installer treats as a hint: it prints the line for a program
    it cannot install, the way it prints `credstore set` for a credential.
    A program an MCP server, a REST proxy, a tunnel or a skill names is
    required — a hint beside it would read as permission to leave it missing —
    so the third column is left off as soon as one such origin is present.
    """
    os.environ["SLIFE2_DATA_DIR"] = str(home)

    from slife2.libs.host.config import ConfigError, load

    try:
        config = load()
    except ConfigError as exc:
        _warn(str(exc))
        return 2

    needed: dict[str, set[str]] = {}
    hints: dict[str, str] = {}
    _add(needed, hints, "uv", "bootstrap")
    _add(needed, hints, "python", "bootstrap")

    if config.peer_agent.configured and config.peer_agent.provider == "mosquitto":
        _add(needed, hints, "mosquitto", "peer_agent.provider")
    if (
        config.subagent.network.configured
        and config.subagent.network.provider == "mosquitto"
    ):
        _add(needed, hints, "mosquitto", "subagent.provider")

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
            _add(needed, hints, binary, f"file-tunnel.providers.{name}")

    for name, entry in config.mcp.items():
        _add(needed, hints, _first_word(entry.command), f"mcp:{name}")
    for name, entry in config.rest_apis.items():
        _add(needed, hints, _first_word(entry.command), f"rest-api:{name}")
    for name, entry in config.cli.items():
        _add(
            needed,
            hints,
            _first_word(entry.command),
            f"cli:{name}",
            hint=entry.install,
        )

    skills = home / "skills"
    if skills.is_dir():
        from slife2.plugins.skills.playbooks import scan

        for skill in scan(skills):
            for executable in skill.requires.bins:
                _add(
                    needed,
                    hints,
                    executable,
                    f"skill:{skill.name}.requires.bins",
                )

    for executable in sorted(needed):
        origins = needed[executable]
        line = f"{executable}\t{','.join(sorted(origins))}"
        hint = hints.get(executable, "")
        if hint and all(origin.startswith("cli:") for origin in origins):
            line += f"\t{hint}"
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    # **UTF-8 on both streams, whatever the console code page is.**  The
    # installers read one line per requirement, and a `cli:` hint is the
    # operator's own sentence — Chinese here, but any language in principle.
    # Windows would otherwise encode it in cp936 (or whatever the machine uses)
    # while both scripts decode it as UTF-8 (`install.ps1` sets the console
    # encoding for exactly this), splitting characters at the line boundary.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:  # pragma: no cover - a Python older than 3.7
            pass

    parser = argparse.ArgumentParser(prog="install_helper.py")
    commands = parser.add_subparsers(dest="command", required=True)

    seed_parser = commands.add_parser("seed", help="place the shipped configuration")
    seed_parser.add_argument("--source", required=True)
    seed_parser.add_argument("--home", required=True)
    seed_parser.add_argument("--dry-run", action="store_true")

    require_parser = commands.add_parser("require", help="list configured programs")
    require_parser.add_argument("--home", required=True)

    secrets_parser = commands.add_parser(
        "secrets", help="list the credentials the configuration references"
    )
    secrets_parser.add_argument("--home", required=True)

    args = parser.parse_args(argv)
    if args.command == "seed":
        return seed(Path(args.source), Path(args.home), args.dry_run)
    if args.command == "require":
        return require(Path(args.home))
    return secrets(Path(args.home))


if __name__ == "__main__":
    raise SystemExit(main())
