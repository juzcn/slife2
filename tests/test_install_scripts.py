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

#: The sections every file must have (SDD §4.1).  `require` loads the home's
#: file, so a config these tests hand it has to be one the loader accepts.
REQUIRED = """\
providers:
  local:
    api: openai-completions
    base_url: https://example.test/v1
    models: [{model: big}]
embeddings:
  providers:
    local:
      base_url: http://127.0.0.1:17347/v1
      model: big-emb
"""


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
    # **The script goes in on stdin, as bytes.**  On Windows `bash` may be
    # WSL's alias (`System32\bash.exe`), whose arguments are translated to Linux
    # paths — a Windows path this test passed as an operand arrived as
    # `D:DevWorkspaceslife2scriptsinstall.sh`.  Bytes rather than text because
    # subprocess's text mode translates `\n` to `\r\n` on the way in, and a
    # shell script with CRLF endings is a syntax error in bash.
    result = subprocess.run(
        [bash, "-n"],
        input=SHELL.read_text(encoding="utf-8").encode("utf-8"),
        capture_output=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


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


@pytest.mark.skipif(sys.platform == "win32", reason="the shell installer is POSIX-only")
def test_uninstall_keeps_a_failed_entry_and_drains_the_rest(tmp_path: Path) -> None:
    """One failure must not write every later success back as still installed.

    The accumulator was shared across the loop, so after `uv tool uninstall`
    failed, the local node removal that followed succeeded but was re-appended
    to the state file — and the file could never drain.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    state = tmp_path / "state.tsv"
    state.write_text("uv-tool\tslife2\tslife2\nlocal\tnode\tnode\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "uv", "#!/bin/sh\nexit 1\n")
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        [bash, str(SHELL), "uninstall"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            **os.environ,
            "HOME": str(home),
            "SLIFE2_DATA_DIR": str(tmp_path / "data"),
            "SLIFE2_INSTALL_STATE": str(state),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert state.read_text(encoding="utf-8").splitlines() == ["uv-tool\tslife2\tslife2"]


@pytest.mark.skipif(sys.platform == "win32", reason="the shell installer is POSIX-only")
def test_uninstall_keeps_a_local_component_that_could_not_be_removed(
    tmp_path: Path,
) -> None:
    """A `local` removal's failure counts, like a package manager's.

    `remove_local` ran several `rm`s and answered with the last one's status,
    so a failed removal in the middle was invisible: the entry drained from the
    state file while the component was still there, and no later run could find
    it again.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    state = tmp_path / "state.tsv"
    state.write_text("local\tnode\tnode\n", encoding="utf-8")
    home = tmp_path / "home"
    # `rm -f` refuses a directory without `-r`, which is the failure the
    # per-entry accumulator has to see.
    (home / ".local" / "bin" / "node").mkdir(parents=True)

    result = subprocess.run(
        [bash, str(SHELL), "uninstall"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            **os.environ,
            "HOME": str(home),
            "SLIFE2_DATA_DIR": str(tmp_path / "data"),
            "SLIFE2_INSTALL_STATE": str(state),
        },
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert state.read_text(encoding="utf-8").splitlines() == ["local\tnode\tnode"]


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
    # The helper writes UTF-8 on every platform (a `cli:` hint may be any
    # language); `text=True` alone would decode it with the Windows code page.
    return subprocess.run(
        [sys.executable, str(HELPER), "require", "--home", str(home)],
        capture_output=True,
        encoding="utf-8",
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
        "rg",
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


def test_uv_tool_list_tolerates_a_warning_on_stderr(tmp_path: Path) -> None:
    """**A malformed tool is a warning, not a failed command.**

    `uv tool list` exits 0 but writes the warning to stderr.  Under the
    installer's `$ErrorActionPreference = 'Stop'`, Windows PowerShell 5.1
    turns a redirected stderr line into an error record, and that warning
    aborted the install before `uv tool install` ran.
    """
    if sys.platform != "win32":
        pytest.skip("the installer's PowerShell host is Windows")
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is not available")

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    (stub_bin / "uv.cmd").write_text(
        "@echo off\r\n"
        "echo warning: Ignoring malformed tool `qd-evolve` 1>&2\r\n"
        "echo slife2 v0.1.0\r\n"
        "echo - slife2.exe\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )

    text = POWERSHELL.read_text(encoding="utf-8")
    match = re.search(r"^function Get-UvTools.*?^\}", text, re.MULTILINE | re.DOTALL)
    assert match, "Get-UvTools is missing from install.ps1"
    function_file = tmp_path / "function.ps1"
    function_file.write_text(match.group(0) + "\n", encoding="utf-8")
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f". '{function_file}'\n"
        f"$env:PATH = '{stub_bin};' + $env:PATH\n"
        "$tools = Get-UvTools\n"
        'Write-Output ("TOOLS=" + ($tools -join "|"))\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "TOOLS=slife2 v0.1.0|- slife2.exe" in result.stdout, (
        result.stdout + result.stderr
    )


def _ensure_python_function(path: Path) -> str:
    name = "Ensure-Python" if path.suffix == ".ps1" else "ensure_python"
    pattern = (
        rf"^function {re.escape(name)}.*?^\}}"
        if path.suffix == ".ps1"
        else rf"^{name}\(\) \{{.*?^\}}"
    )
    match = re.search(
        pattern, path.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL
    )
    assert match, f"{name} is missing from {path.name}"
    return match.group(0) + "\n"


def test_the_shell_installer_falls_back_for_a_uv_without_upgrade(
    tmp_path: Path,
) -> None:
    """**`--upgrade` is newer than uv 0.7, and `--default` was preview-only.**

    A uv that predates either flag rejects the whole command, and `uv self
    update` is allowed to fail quietly, so the installer must still put Python
    3.13 in place rather than dying on a flag it does not know.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    if sys.platform == "win32" and "system32" in Path(bash).as_posix().lower():
        # WSL's alias translates Windows path operands into Linux paths, so the
        # harness and its env files arrive without their separators.  The test
        # needs a bash that can read Windows paths — Git Bash, as CI has.
        pytest.skip("WSL's bash cannot read the Windows paths this harness passes")

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    _write_executable(
        stub_bin / "uv",
        """\
#!/bin/sh
printf '%s\\n' "$*" >> "$UV_LOG"
for arg in "$@"; do
    [ "$arg" = "--preview" ] && exit 0
done
exit 1
""",
    )

    function_file = tmp_path / "function.sh"
    function_file.write_text(_ensure_python_function(SHELL), encoding="utf-8")
    harness = tmp_path / "harness.sh"
    harness.write_text(
        """\
set -u
PLATFORM=linux
say() { :; }
refresh_path() { :; }
stage() { local message=$1; shift; "$@"; }
die() { printf 'error: %s\\n' "$*" >&2; exit 2; }
. "$FUNCTION_FILE"
ensure_python
""",
        encoding="utf-8",
    )
    log = tmp_path / "uv.log"

    result = subprocess.run(
        [bash, str(harness)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
            "FUNCTION_FILE": str(function_file),
            "UV_LOG": str(log),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "python install 3.13 --upgrade --default",
        "python install 3.13 --preview --default",
    ]


def test_the_powershell_installer_falls_back_for_a_uv_without_upgrade(
    tmp_path: Path,
) -> None:
    """The Windows half of the same fallback."""
    if sys.platform != "win32":
        pytest.skip("the installer's PowerShell host is Windows")
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is not available")

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    (stub_bin / "uv.cmd").write_text(
        "@echo off\r\n"
        'echo %*>>"%UV_LOG%"\r\n'
        'echo %* | findstr /c:"--preview" >nul && exit /b 0\r\n'
        "exit /b 1\r\n",
        encoding="utf-8",
    )

    text = POWERSHELL.read_text(encoding="utf-8")
    quiet = re.search(r"^function Invoke-Quiet.*?^\}", text, re.MULTILINE | re.DOTALL)
    assert quiet, "Invoke-Quiet is missing from install.ps1"
    function_file = tmp_path / "function.ps1"
    function_file.write_text(
        quiet.group(0) + "\n" + _ensure_python_function(POWERSHELL), encoding="utf-8"
    )
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "function Say([string]$Message) { }\n"
        "function Update-ProcessPath { }\n"
        "function Die([string]$Message) { throw $Message }\n"
        f". '{function_file}'\n"
        f"$env:PATH = '{stub_bin};' + $env:PATH\n"
        "Ensure-Python\n"
        'Write-Output "RESULT=$?"\n',
        encoding="utf-8",
    )
    log = tmp_path / "uv.log"

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        env={**os.environ, "UV_LOG": str(log)},
    )

    assert "RESULT=True" in result.stdout, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "python install 3.13 --upgrade --default",
        "python install 3.13 --preview --default",
    ]


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
        REQUIRED + "mcp:\n  mystery:\n    command: no-such-tool\n"
        "cli:\n  yt:\n    command: yt-dlp\n",
        encoding="utf-8",
    )
    result = _requirements(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "no-such-tool\tmcp:mystery" in result.stdout
    assert "yt-dlp\tcli:yt" in result.stdout


def test_requirements_carry_a_cli_install_hint(tmp_path: Path) -> None:
    """A `cli:` entry's install line is a prompt, not an installer.

    The helper prints it as a third field so both installers can show it for a
    program they cannot provide themselves, the way they show `credstore set`
    for a credential.  The field is left off when another family names the same
    program — a server's dependency is required, not optional — and when the
    entry carries no install line, because there is nothing to print.
    """
    (tmp_path / "slife2.yaml").write_text(
        REQUIRED + "mcp:\n"
        "  mystery:\n"
        "    command: no-such-tool\n"
        "cli:\n"
        "  hunt:\n"
        "    command: no-such-tool --fast\n"
        "    install: brew install no-such-tool\n"
        "  rg:\n"
        "    command: rg\n"
        "    install: choco install ripgrep\n"
        "  plain:\n"
        "    command: no-such-tool-plain\n",
        encoding="utf-8",
    )
    result = _requirements(tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "no-such-tool\tcli:hunt,mcp:mystery" in lines
    assert "rg\tcli:rg\tchoco install ripgrep" in lines
    assert "no-such-tool-plain\tcli:plain" in lines


def test_the_installers_print_install_hints_without_failing() -> None:
    """Both scripts end with the same section for a program's own install line."""
    phrase = "programs: install the ones you have not set up:"
    for path in (SHELL, POWERSHELL):
        assert phrase in path.read_text(encoding="utf-8"), path.name


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
        "    localhost.run:\n"
        "      binary: ${SSH_BINARY:-ssh}\n"
        "    ngrok:\n"
        "      token: keyring:slife2/ngrok\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(HELPER), "secrets", "--home", str(tmp_path)],
        capture_output=True,
        encoding="utf-8",
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


def test_requirements_refuse_a_home_with_no_config(tmp_path: Path) -> None:
    """A home without its file is not a home, and the installer says so."""
    result = _requirements(tmp_path)

    assert result.returncode == 2
    assert "no slife2.yaml" in result.stderr


def test_requirements_refuse_a_malformed_config(tmp_path: Path) -> None:
    (tmp_path / "slife2.yaml").write_text("foo: [unclosed\n", encoding="utf-8")
    result = _requirements(tmp_path)
    assert result.returncode == 2
    assert "cannot read" in result.stderr


def _seed(source: Path, home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run `seed` with the bundle a real one carries: a config and its guide.

    Every shipped bundle has `AGENTS.md` beside `slife2.yaml`; a fixture that
    omitted it would be seeding a bundle no installer produces — and would
    assert the warning path instead of the one users take.
    """
    guide = source.parent / "AGENTS.md"
    if not guide.exists():
        guide.write_text("shipped guide\n", encoding="utf-8")
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
        encoding="utf-8",
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


def test_seed_places_the_guide_and_leaves_a_persons_copy_alone(
    tmp_path: Path,
) -> None:
    """`AGENTS.md` is seeded like the config, not like `skills/`.

    It is a document a person edits — the model's guide to its tools, and the
    thing an operator changes per home — so a reinstall never overwrites it: the
    shipped copy lands beside it as a reference.  A missing one is created,
    exactly as a missing `slife2.yaml` is.
    """
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "slife2.yaml"
    source.write_text("shipped: true\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("shipped guide\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / "slife2.yaml").write_text("shipped: true\n", encoding="utf-8")
    (home / "AGENTS.md").write_text("my guide\n", encoding="utf-8")

    result = _seed(source, home)

    assert result.returncode == 0, result.stderr
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == "my guide\n"
    assert (home / "AGENTS-default.md").read_text(encoding="utf-8") == (
        "shipped guide\n"
    )


def test_seed_creates_a_missing_guide(tmp_path: Path) -> None:
    """A home the installer made has both documents it requires."""
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "slife2.yaml"
    source.write_text("shipped: true\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("shipped guide\n", encoding="utf-8")
    home = tmp_path / "home"

    result = _seed(source, home)

    assert result.returncode == 0, result.stderr
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == "shipped guide\n"


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


def test_seed_copies_the_bundled_skills_and_jobs_over_the_home(
    tmp_path: Path,
) -> None:
    """**The same seed step, and the trees are overwritten.**  A bundled file
    replaces the home's copy of the same path; a file the home added under
    another name stays.  That is the whole update story — the bundle is the
    authority, not a versioned backup."""
    root = tmp_path / "repo"
    (root / "skills" / "one").mkdir(parents=True)
    (root / "skills" / "one" / "SKILL.md").write_text("bundled skill\n")
    (root / "jobs").mkdir()
    (root / "jobs" / "one.py").write_text("bundled job\n")
    source = root / "slife2.yaml"
    source.write_text("shipped: true\n", encoding="utf-8")

    home = tmp_path / "home"
    (home / "skills" / "one").mkdir(parents=True)
    (home / "skills" / "one" / "SKILL.md").write_text("old\n", encoding="utf-8")
    (home / "skills" / "one" / "mine.txt").write_text("keep\n", encoding="utf-8")
    (home / "jobs").mkdir()
    (home / "jobs" / "one.py").write_text("old\n", encoding="utf-8")
    (home / "jobs" / "mine.py").write_text("keep\n", encoding="utf-8")

    result = _seed(source, home)

    assert result.returncode == 0, result.stderr
    assert (home / "skills" / "one" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "bundled skill\n"
    assert (home / "skills" / "one" / "mine.txt").read_text(encoding="utf-8") == (
        "keep\n"
    )
    assert (home / "jobs" / "one.py").read_text(encoding="utf-8") == "bundled job\n"
    assert (home / "jobs" / "mine.py").read_text(encoding="utf-8") == "keep\n"
    assert "seeded" in result.stdout


def test_seed_dry_run_does_not_touch_the_trees(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "skills" / "one").mkdir(parents=True)
    (root / "skills" / "one" / "SKILL.md").write_text("bundled\n", encoding="utf-8")
    source = root / "slife2.yaml"
    source.write_text("shipped: true\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / "skills" / "one").mkdir(parents=True)
    live = home / "skills" / "one" / "SKILL.md"
    live.write_text("old\n", encoding="utf-8")

    result = _seed(source, home, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert live.read_text(encoding="utf-8") == "old\n"
    assert "would copy" in result.stdout


def test_seed_trees_failure_is_a_warning_not_a_fatal_install(
    tmp_path: Path,
) -> None:
    """**A retryable content failure does not abort the install.**  The config
    and programs are in place; a locked skill or job is fixed by closing
    whatever holds it and running the installer again — so the seed says that
    and returns 0, rather than `die`-ing and leaving the user to search."""
    root = tmp_path / "repo"
    (root / "skills" / "one").mkdir(parents=True)
    (root / "skills" / "one" / "SKILL.md").write_text("bundled\n", encoding="utf-8")
    source = root / "slife2.yaml"
    source.write_text("shipped: true\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    # A file where the skills directory should go makes the copy fail.
    (home / "skills").write_text("not a directory\n", encoding="utf-8")

    result = _seed(source, home)

    assert result.returncode == 0, result.stderr
    assert "run the installer again" in result.stderr
    assert (home / "slife2.yaml").is_file(), "the config still landed"
    assert (home / "skills").read_text(encoding="utf-8") == "not a directory\n"
