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


def test_cloudflared_upgrade_replaces_a_running_binary(tmp_path: Path) -> None:
    """A running `cloudflared` may not be written over, only replaced by name.

    The file-tunnel plugin keeps one running, and an install runs beside it.
    `curl -o` on that path fails with ETXTBSY ("Text file busy"); downloading
    to a temporary file and renaming it into place leaves the running inode
    alone.  The fake curl writes to the `-o` target, so the old shape fails the
    way the real one did.
    """
    if sys.platform != "linux":
        pytest.skip("ETXTBSY is the Linux behaviour this pins")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    _write_executable(
        stub_bin / "curl",
        """\
#!/bin/sh
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out=$2; shift 2 ;;
        *) shift ;;
    esac
done
printf '#!/bin/sh\\necho new-cloudflared\\n' > "$out"
""",
    )

    home = tmp_path / "home"
    binaries = home / ".local" / "bin"
    binaries.mkdir(parents=True)
    running = binaries / "cloudflared"
    shutil.copyfile("/bin/sleep", running)
    running.chmod(0o755)
    process = subprocess.Popen([str(running), "30"])

    try:
        match = re.search(
            r"^install_cloudflared\(\) \{.*?^\}$",
            SHELL.read_text(encoding="utf-8"),
            re.MULTILINE | re.DOTALL,
        )
        assert match, "install_cloudflared is missing from install.sh"
        function_file = tmp_path / "function.sh"
        function_file.write_text(match.group(0) + "\n", encoding="utf-8")

        harness = tmp_path / "harness.sh"
        harness.write_text(
            """\
set -u
say() { :; }
refresh_path() { :; }
. "$FUNCTION_FILE"
install_cloudflared
""",
            encoding="utf-8",
        )
        result = subprocess.run(
            [bash, str(harness)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "HOME": str(home),
                "PATH": f"{stub_bin}:{os.environ['PATH']}",
                "FUNCTION_FILE": str(function_file),
            },
        )
    finally:
        process.kill()
        process.wait()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "new-cloudflared" in running.read_text(encoding="utf-8")
    assert running.stat().st_mode & 0o111, "the replacement is executable"
    assert not list(binaries.glob(".cloudflared.*")), "no temporary file is left"


def test_cloudflared_upgrade_is_skipped_when_the_version_matches(
    tmp_path: Path,
) -> None:
    """`ensure_tool` asks before downloading forty megabytes again.

    The release's "latest" URL redirects to a tag; reading that redirect is one
    small request where the binary is tens of megabytes, and an install that
    changed nothing should not re-download it.
    """
    if sys.platform != "linux":
        pytest.skip("the installer's cloudflared path is POSIX-only")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    _write_executable(
        stub_bin / "curl",
        """\
#!/bin/sh
printf 'https://github.com/cloudflare/cloudflared/releases/tag/%s\\n' "$LATEST"
""",
    )
    home = tmp_path / "home"
    binaries = home / ".local" / "bin"
    binaries.mkdir(parents=True)
    _write_executable(
        binaries / "cloudflared",
        """\
#!/bin/sh
echo "cloudflared version $INSTALLED (built now)"
""",
    )

    match = re.search(
        r"^cloudflared_current\(\) \{.*?^\}$",
        SHELL.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert match, "cloudflared_current is missing from install.sh"
    function_file = tmp_path / "function.sh"
    function_file.write_text(match.group(0) + "\n", encoding="utf-8")

    def current(installed: str) -> int:
        return subprocess.run(
            [bash, "-c", '. "$FUNCTION_FILE"; cloudflared_current'],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "HOME": str(home),
                "PATH": f"{stub_bin}:{os.environ['PATH']}",
                "FUNCTION_FILE": str(function_file),
                "LATEST": "2026.10.1",
                "INSTALLED": installed,
            },
        ).returncode

    assert current("2026.10.1") == 0
    assert current("2026.9.0") == 1


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


def test_the_installers_run_slife2_as_a_module() -> None:
    """**Never the console script.**  `slife2 --help` is a path lookup, and a
    stale shim or a profile function on that name can run the TUI instead of
    answering the flag — which is an installation that starts the stack.  The
    module is this environment's code, and argparse owns the flag.
    """
    for path in (SHELL, POWERSHELL):
        text = path.read_text(encoding="utf-8")
        assert "-m slife2 --help" in text, path.name
        assert re.search(r"^\s*(?:&\s*)?slife2 --help", text, re.MULTILINE) is None, (
            f"{path.name} invokes the console script"
        )


def test_invoke_quiet_judges_a_native_command_by_its_exit_code(
    tmp_path: Path,
) -> None:
    """A native warning on stderr is output, not a failed command.

    Windows PowerShell 5.1 turns redirected native stderr into an error record;
    with the installer's `$ErrorActionPreference = 'Stop'`, uv's experimental
    warning stopped `Invoke-Quiet` and failed the Python install.
    """
    if sys.platform != "win32":
        pytest.skip("the installer's PowerShell host is Windows")
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is not available")

    text = POWERSHELL.read_text(encoding="utf-8")
    match = re.search(r"^function Invoke-Quiet.*?^\}", text, re.MULTILINE | re.DOTALL)
    assert match, "Invoke-Quiet is missing from install.ps1"
    function_file = tmp_path / "function.ps1"
    function_file.write_text(match.group(0) + "\n", encoding="utf-8")
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f". '{function_file}'\n"
        "$ok = Invoke-Quiet { cmd /c 'echo native-warning 1>&2 & exit /b 0' }\n"
        'Write-Output "RESULT=$ok"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
    )

    assert "RESULT=True" in result.stdout, result.stdout + result.stderr


def test_an_inline_installer_run_returns_to_the_caller(tmp_path: Path) -> None:
    """**`irm ... | iex` runs in the caller's shell.**

    The installer used to `exit 0` when it finished, which closes the
    PowerShell window or pane the command was pasted into — Windows Terminal
    then switches to another window, which looks like something was started.
    A dry run is the cheapest path through the same dispatch.
    """
    if sys.platform != "win32":
        pytest.skip("the PowerShell installer runs on Windows")
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is not available")

    harness = tmp_path / "harness.ps1"
    harness.write_text(
        "$text = '$args = @(''--dry-run'')' + [Environment]::NewLine + "
        f"(Get-Content '{POWERSHELL}' -Raw)\n"
        "iex $text\n"
        "Write-Output 'AFTER'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    assert "AFTER" in result.stdout, result.stdout + result.stderr


def test_the_installers_announce_extraction_the_same_way() -> None:
    """One step, one line — and the Windows extraction stays quiet."""
    for path in (SHELL, POWERSHELL):
        assert "extracting slife2" in path.read_text(encoding="utf-8"), path.name
    powershell = POWERSHELL.read_text(encoding="utf-8")
    assert re.search(r"^\s*Expand-Archive\b", powershell, re.MULTILINE) is None, (
        "Expand-Archive redraws the console; the .NET extraction is quiet"
    )


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


def test_secrets_lists_references_and_skips_defaults(tmp_path: Path) -> None:
    """The installer's hint, read off the file rather than resolved.

    A `:-default` is a value the config already supplies, so `${SSH_BINARY:-ssh}`
    is not a credential; a `keyring:` value is named the way `credstore set`
    takes it.  The same credential named twice is one line with both places.
    """
    (tmp_path / "slife2.yaml").write_text(
        "providers:\n"
        "  main:\n"
        "    api_key: ${MAIN_KEY}\n"
        "mcp:\n"
        "  serper:\n"
        "    env:\n"
        "      SERPER_API_KEY: ${SERPER_API_KEY}\n"
        "      OPTIONAL: ${OPTIONAL_KEY:-fallback}\n"
        "skills:\n"
        "  search:\n"
        "    env:\n"
        "      SERPER_API_KEY: ${SERPER_API_KEY}\n"
        "file-tunnel:\n"
        "  providers:\n"
        "    ssh:\n"
        "      binary: ${SSH_BINARY:-ssh}\n"
        "    ngrok:\n"
        "      token: keyring:slife2/ngrok\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(HELPER), "secrets", "--home", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "MAIN_KEY\tproviders.main.api_key" in lines
    assert (
        "SERPER_API_KEY\tmcp.serper.env.SERPER_API_KEY, "
        "skills.search.env.SERPER_API_KEY" in lines
    )
    assert "slife2/ngrok\tfile-tunnel.providers.ngrok.token" in lines
    assert not any("OPTIONAL_KEY" in line or "SSH_BINARY" in line for line in lines)


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
