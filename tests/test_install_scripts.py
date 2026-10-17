"""The installer scripts, without running an install.

These are static and helper-level checks: the shell parses, the PowerShell
parses, the requirement helper covers everything the effective configuration
names, and the config seeder never overwrites a live file.  Nothing here
installs a package, elevates, or touches a real home.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HELPER = SCRIPTS / "install_helper.py"
SHELL = SCRIPTS / "install.sh"
POWERSHELL = SCRIPTS / "install.ps1"
BATCH = SCRIPTS / "install.cmd"

pytestmark = pytest.mark.unit


def _commands(text: str) -> list[str]:
    """The script's lines that can run something, as opposed to prose."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_installer_files_exist() -> None:
    for path in (SHELL, POWERSHELL, BATCH, HELPER):
        assert path.is_file(), path
        assert path.stat().st_size > 0, path


def test_shell_syntax() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    result = subprocess.run(
        [bash, "-n", str(SHELL)], capture_output=True, text=True, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr


def test_powershell_syntax() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not available")
    script = (
        "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{POWERSHELL}', [ref]$null, [ref]$e); "
        "if ($e) { $e | ForEach-Object { $_.Message }; exit 1 }"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_elevation_or_system_package_manager() -> None:
    """The installers remain user-level: no root, no system package manager."""
    forbidden = (
        "sudo",
        "winget",
        "runas",
        "add-windowscapability",
        "remove-windowscapability",
    )
    for path in (SHELL, POWERSHELL):
        text = path.read_text(encoding="utf-8").lower()
        for phrase in forbidden:
            assert phrase not in text, f"{path.name}: {phrase}"


def test_no_global_upgrade_or_cleanup() -> None:
    """The scripts may only touch the packages they name."""
    forbidden = (
        "brew upgrade --all",
        "brew cleanup",
        "brew autoremove",
        "apt-get upgrade",
        "apt-get dist-upgrade",
        "apt-get autoremove",
        "dnf upgrade",
        "dnf autoremove",
        "pacman -Syu",
        "pacman -Sy",
        "pacman -Su",
        "pacman -Rns",
        "pacman -Sc",
        "uv cache clean",
        "uv pip uninstall",
        "uv tool upgrade --all",
        "uv venv --clear",
    )
    for path in (SHELL, POWERSHELL):
        commands = "\n".join(_commands(path.read_text(encoding="utf-8")))
        for phrase in forbidden:
            assert phrase not in commands, f"{path.name}: {phrase}"


def test_python_sync_keeps_user_packages() -> None:
    shell = SHELL.read_text(encoding="utf-8")
    powershell = POWERSHELL.read_text(encoding="utf-8")
    assert "uv sync --upgrade --inexact" in shell
    assert "uv sync --upgrade --inexact" in powershell
    assert "uv venv --clear" not in shell
    assert "uv venv --clear" not in powershell


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def test_mosquitto_install_downloads_the_libraries_it_links(tmp_path: Path) -> None:
    """The dependency closure is downloaded one package at a time.

    A virtual dependency names no candidate — `debconf-2.0` is provided by
    `debconf` — and `apt-get download` answers one such name by downloading
    nothing at all.  The first user-level install therefore extracted mosquitto
    without `libwrap.so.0` and the broker died with exit 127.  The stubs below
    reproduce that apt behaviour, and the stale symlink an older install left in
    `~/.local/bin` (writing the wrapper through it would replace the binary
    with the wrapper, which execs itself).
    """
    if sys.platform != "linux":
        pytest.skip("the apt-based installer is Linux-only")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    _write_executable(
        stub_bin / "apt-cache",
        """\
#!/bin/sh
if [ "$1" = depends ]; then
    cat <<'EOF'
mosquitto
  Depends: libc6
  Depends: <libc-dev>
  Depends: debconf-2.0
  Depends: libwrap0
EOF
elif [ "$1" = madison ]; then
    case "$2" in
        libc6|libwrap0) printf '%s | 1.0 | test\\n' "$2" ;;
    esac
fi
exit 0
""",
    )
    _write_executable(
        stub_bin / "dpkg",
        """\
#!/bin/sh
case "$1" in
    -s) [ "$2" = libc6 ] && exit 0; exit 1 ;;
    -x)
        name=$(basename "$2" .deb)
        dest=$3
        mkdir -p "$dest/usr/lib/x86_64-linux-gnu" "$dest/usr/sbin"
        : > "$dest/usr/lib/x86_64-linux-gnu/$name.so.0"
        if [ "$name" = mosquitto ]; then
            printf '#!/bin/sh\\necho "mosquitto version 9.9.9"\\n' \
                > "$dest/usr/sbin/mosquitto"
            chmod +x "$dest/usr/sbin/mosquitto"
        fi
        exit 0 ;;
esac
exit 1
""",
    )
    _write_executable(
        stub_bin / "apt-get",
        """\
#!/bin/sh
printf '%s\\n' "$*" >> "$APT_LOG"
packages=""
for arg in "$@"; do
    case "$arg" in
        # The real apt refuses the whole invocation when one name has no
        # candidate, and downloads none of the others.
        debconf-2.0|libc-dev) exit 100 ;;
        -q|download) ;;
        *) packages="$packages $arg" ;;
    esac
done
for pkg in $packages; do
    : > "$pkg.deb"
done
exit 0
""",
    )

    match = re.search(
        r"^install_mosquitto\(\) \{.*?^\}$",
        SHELL.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert match, "install_mosquitto is missing from install.sh"
    function_file = tmp_path / "function.sh"
    function_file.write_text(match.group(0) + "\n", encoding="utf-8")

    home = tmp_path / "home"
    harness = tmp_path / "harness.sh"
    harness.write_text(
        """\
set -u
say() { printf '%s\\n' "$*"; }
refresh_path() {
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
    export PATH
}
. "$FUNCTION_FILE"
export HOME="$TEST_HOME"
export PATH="$STUB_BIN:$PATH"
export APT_LOG="$TEST_HOME/apt.log"
mkdir -p "$HOME/.local/bin" "$HOME/.local/usr/sbin"
printf '#!/bin/sh\\necho "mosquitto version 9.9.9"\\n' > "$HOME/.local/usr/sbin/mosquitto"
chmod +x "$HOME/.local/usr/sbin/mosquitto"
ln -s "$HOME/.local/usr/sbin/mosquitto" "$HOME/.local/bin/mosquitto"
install_mosquitto
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [bash, str(harness)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "FUNCTION_FILE": str(function_file),
            "TEST_HOME": str(home),
            "STUB_BIN": str(stub_bin),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr

    log = (home / "apt.log").read_text(encoding="utf-8")
    assert "download mosquitto" in log
    assert "download libwrap0" in log
    assert "debconf-2.0" not in log
    assert "libc-dev" not in log
    assert (home / ".local/usr/lib/x86_64-linux-gnu/libwrap0.so.0").is_file()

    wrapper = home / ".local/bin/mosquitto"
    assert not wrapper.is_symlink()
    assert "LD_LIBRARY_PATH" in wrapper.read_text(encoding="utf-8")
    # The extraction target is still the binary, not the wrapper written over it.
    assert "mosquitto version 9.9.9" in (home / ".local/usr/sbin/mosquitto").read_text(
        encoding="utf-8"
    )


def _requirements(home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), "require", "--home", str(home)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_requirements_cover_the_shipped_configuration() -> None:
    result = _requirements(ROOT)
    assert result.returncode == 0, result.stderr
    named = {
        line.split("\t", 1)[0] for line in result.stdout.splitlines() if line.strip()
    }
    assert {
        "uv",
        "python",
        "python3",
        "mosquitto",
        "cloudflared",
        "ssh",
        "npx",
        "uvx",
        "yt-dlp",
        "browser-harness",
    } <= named


def test_requirements_report_unknown_commands_with_their_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "slife2.yaml").write_text(
        "mcp:\n  mystery:\n    command: no-such-tool\n"
        "cli:\n  yt:\n    command: yt-dlp\n",
        encoding="utf-8",
    )
    result = _requirements(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "no-such-tool\tmcp:mystery" in result.stdout
    assert "yt-dlp\tcli:yt" in result.stdout


def test_requirements_refuse_a_malformed_config(tmp_path: Path) -> None:
    (tmp_path / "slife2.yaml").write_text("foo: [unclosed\n", encoding="utf-8")
    result = _requirements(tmp_path)
    assert result.returncode == 2
    assert "cannot read" in result.stderr


def _seed(source: Path, home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "seed",
            "--source",
            str(source),
            "--home",
            str(home),
            *extra,
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_seed_creates_a_missing_live_file(tmp_path: Path) -> None:
    source = tmp_path / "repo" / "slife2.yaml"
    source.parent.mkdir()
    source.write_text("shipped: true\n", encoding="utf-8")
    home = tmp_path / "home"

    result = _seed(source, home)

    assert result.returncode == 0, result.stderr
    assert (home / "slife2.yaml").read_text(encoding="utf-8") == "shipped: true\n"
    assert not (home / "slife2-default.yaml").exists()


def test_seed_never_overwrites_a_live_file(tmp_path: Path) -> None:
    source = tmp_path / "repo" / "slife2.yaml"
    source.parent.mkdir()
    source.write_text("shipped: true\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    live = home / "slife2.yaml"
    live.write_text("mine: true\n", encoding="utf-8")

    result = _seed(source, home)

    assert result.returncode == 0, result.stderr
    assert live.read_text(encoding="utf-8") == "mine: true\n"
    assert (home / "slife2-default.yaml").read_text(
        encoding="utf-8"
    ) == "shipped: true\n"
    assert "left untouched" in result.stdout


def test_seed_does_nothing_in_a_checkout(tmp_path: Path) -> None:
    home = tmp_path / "repo"
    home.mkdir()
    source = home / "slife2.yaml"
    source.write_text("shipped: true\n", encoding="utf-8")

    result = _seed(source, home)

    assert result.returncode == 0, result.stderr
    assert "checkout detected" in result.stdout
    assert not (home / "slife2-default.yaml").exists()


def test_seed_dry_run_touches_nothing(tmp_path: Path) -> None:
    source = tmp_path / "repo" / "slife2.yaml"
    source.parent.mkdir()
    source.write_text("shipped: true\n", encoding="utf-8")
    home = tmp_path / "home"

    result = _seed(source, home, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "would create" in result.stdout
    assert not home.exists()


def test_readmes_document_the_installer() -> None:
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "install.sh" in text, name
        assert "install.ps1" in text, name
        assert "slife2-default.yaml" in text, name
