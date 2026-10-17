"""The installer scripts, without running an install.

These are static and helper-level checks: the shell parses, the PowerShell
parses, the dependency table covers everything the effective configuration
names, and the config seeder never overwrites a live file.  Nothing here
installs a package, elevates, or touches a real home.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MANIFEST = SCRIPTS / "dependencies.tsv"
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
    for path in (SHELL, POWERSHELL, BATCH, MANIFEST, HELPER):
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


def test_no_global_upgrade_or_cleanup() -> None:
    """The scripts may only touch the packages they name.

    Lines that mention a package manager are checked; comments and help text
    are not commands.  `brew upgrade <formula>` and `dnf ... upgrade <pkg>` are
    allowed because they name their target; the forbidden forms do not.
    """
    forbidden = (
        "winget upgrade --all",
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
    assert "uv sync --locked --inexact" in shell
    assert "'--inexact'" in powershell
    assert "uv sync --upgrade" in powershell  # in the comment/plan wording is fine
    assert "uv venv --clear" not in shell
    assert "uv venv --clear" not in powershell


def _manifest_rows() -> list[list[str]]:
    rows = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rows.append(line.split("\t"))
    return rows


def test_manifest_is_well_formed() -> None:
    rows = _manifest_rows()
    assert rows, "the manifest has no rows"
    allowed_installers = {
        "uv",
        "uv-python",
        "provided",
        "mqtt",
        "download",
        "node",
        "system",
        "uv-tool",
    }
    seen: set[str] = set()
    for row in rows:
        assert len(row) == 4, row
        executable, _tool, installer, _verify = row
        assert executable not in seen, f"duplicate executable: {executable}"
        seen.add(executable)
        assert installer in allowed_installers, row
    for required in (
        "uv",
        "uvx",
        "python",
        "python3",
        "mosquitto",
        "cloudflared",
        "npx",
        "ssh",
        "yt-dlp",
        "browser-harness",
    ):
        assert required in seen, required


def _requirements(home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), "require", "--home", str(home)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_manifest_covers_the_effective_configuration() -> None:
    result = _requirements(ROOT)
    assert result.returncode == 0, result.stderr
    named = {
        line.split("\t", 1)[0] for line in result.stdout.splitlines() if line.strip()
    }
    missing = named - {row[0] for row in _manifest_rows()}
    assert not missing, f"the manifest has no row for: {sorted(missing)}"
    assert {"mosquitto", "cloudflared", "ssh", "npx", "uvx", "yt-dlp"} <= named


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
