# slife2 installer for Windows (Windows PowerShell 5.1).
#
#   .\scripts\install.ps1                 # install missing, upgrade present
#   .\scripts\install.ps1 --dry-run       # report only; changes nothing
#   .\scripts\install.ps1 --no-upgrade    # install missing only
#   .\scripts\install.ps1 uninstall       # remove only what this installer added
#
# Every external program the effective configuration names is a hard
# dependency.  Everything is installed into the user's own directories
# (%LOCALAPPDATA%\slife2 and ~\.local); there is no administrator prompt, no
# winget, no system package manager, and no system-wide change.  Python 3.13 is
# uv-managed, so the system PATH's `python` is never replaced.
#
# The ownership record lives in the current home: the checkout root when run
# from a checkout, otherwise $env:SLIFE2_DATA_DIR or ~/.slife2.  A development
# checkout and a production installation therefore never share it.
#
# Exit codes: 0 success; 1 a required step failed; 2 usage/environment/config;
# 3 cancelled or no terminal without --yes.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$script:Mode = 'tool'
$script:Root = $null
$script:SourceDir = $null
$script:Manifest = $null
$script:Helper = $null
$script:WorkDir = $null

$script:Action = 'install'
$script:ActionSeen = ''
$script:DryRun = $false
$script:NoUpgrade = $false
$script:Yes = $false
$script:Failed = $false
$script:Py = ''
$script:HomeDir = ''
$script:State = ''

function Say([string]$Message) { Write-Host $Message }
function Step([string]$Number, [string]$Message) { Write-Host ""; Write-Host "[$Number] $Message" }
function Ok([string]$Message) { Write-Host "[OK] $Message" }
function Skip([string]$Message) { Write-Host "[SKIP] $Message" }
function Warn([string]$Message) { Write-Host "[WARN] $Message" -ForegroundColor Yellow }
function Fail([string]$Message) { Write-Host "[FAIL] $Message" -ForegroundColor Red; $script:Failed = $true }
function Die([int]$Code, [string]$Message) { Write-Host "[FAIL] $Message" -ForegroundColor Red; exit $Code }

function Show-Usage {
    @'
slife2 installer (Windows)

Usage:
  powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.ps1 | iex"
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 [install|uninstall] [options]   # from a checkout

Actions:
  install       default; install missing dependencies and upgrade present ones
  uninstall     remove only packages this installer recorded as installed by it

Options:
  --dry-run, -DryRun        report what would happen; never changes anything
  --no-upgrade, -NoUpgrade  install missing packages only
  --yes, -Yes               do not ask for confirmation
  --help, -Help             this text

Exit codes: 0 success, 1 a required step failed, 2 usage/environment/config,
3 cancelled or no terminal without --yes.
'@ | Write-Host
}

function Parse-Arguments {
    foreach ($arg in $args) {
        $lower = $arg.ToLowerInvariant()
        if ($lower -eq 'install' -or $lower -eq 'uninstall') {
            if ($script:ActionSeen) { Die 2 'only one action may be named' }
            $script:Action = $lower
            $script:ActionSeen = $lower
        }
        elseif ($lower -eq '--dry-run' -or $lower -eq '-dryrun') { $script:DryRun = $true }
        elseif ($lower -eq '--no-upgrade' -or $lower -eq '-noupgrade') { $script:NoUpgrade = $true }
        elseif ($lower -eq '--yes' -or $lower -eq '-yes' -or $lower -eq '-y') { $script:Yes = $true }
        elseif ($lower -eq '--help' -or $lower -eq '-help' -or $lower -eq '-h') { Show-Usage; exit 0 }
        elseif ($lower.StartsWith('-')) { Show-Usage; Die 2 "unknown option: $arg" }
        else { Show-Usage; Die 2 "unknown action: $arg (use install or uninstall)" }
    }
    if ($script:DryRun -and $script:Action -ne 'install') { Die 2 '--dry-run is only valid with install' }
    if ($script:NoUpgrade -and $script:Action -ne 'install') { Die 2 '--no-upgrade is only valid with install' }
}

function Test-BrokerListening {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $pending = $client.BeginConnect('127.0.0.1', 1883, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(300)) { $client.Close(); return $false }
        $client.EndConnect($pending)
        $client.Close()
        return $true
    }
    catch { return $false }
}

function Add-UserPath([string]$Directory) {
    if (-not $Directory) { return }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) { $userPath = '' }
    if ($userPath.Split(';') -notcontains $Directory) {
        [Environment]::SetEnvironmentVariable('Path', "$Directory;$userPath", 'User')
        $env:Path = "$Directory;$env:Path"
        Ok "added $Directory to the user PATH"
    }
}

function Update-ProcessPath {
    $parts = @()
    foreach ($scope in @('Machine', 'User')) {
        $value = [Environment]::GetEnvironmentVariable('Path', $scope)
        if ($value) { $parts += $value.Split(';') }
    }
    foreach ($part in $env:Path.Split(';')) {
        if ($part -and ($parts -notcontains $part)) { $parts += $part }
    }
    $local = Join-Path $env:USERPROFILE '.local\bin'
    if (Test-Path $local) { $parts = @($local) + $parts }
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $bin = & uv tool dir --bin 2>$null
        if ($bin) { $parts = @($bin) + $parts }
    }
    $env:Path = (($parts | Where-Object { $_ } | Select-Object -Unique) -join ';')
}

function Test-Checkout([string]$Path) {
    if (-not $Path) { return $false }
    $pyproject = Join-Path $Path 'pyproject.toml'
    if (-not (Test-Path $pyproject)) { return $false }
    return [bool](Select-String -Path $pyproject -Pattern 'name = "slife2"' -Quiet)
}

function Resolve-Mode {
    $candidates = @()
    if ($PSScriptRoot) { $candidates += (Split-Path -Parent $PSScriptRoot) }
    if ($PWD) { $candidates += $PWD.Path }
    foreach ($candidate in $candidates) {
        if (Test-Checkout $candidate) {
            $script:Mode = 'checkout'
            $script:Root = (Resolve-Path $candidate).Path
            $script:SourceDir = $script:Root
            $script:Manifest = Join-Path $script:Root 'scripts\dependencies.tsv'
            $script:Helper = Join-Path $script:Root 'scripts\install_helper.py'
            [void](Set-Location $script:Root)
            return
        }
    }
    # A standalone install is an application: no checkout, no repository.
    $script:Mode = 'tool'
}

function Resolve-DataHome {
    if ($script:Mode -eq 'checkout') {
        if ($env:SLIFE2_DATA_DIR) {
            $script:HomeDir = $env:SLIFE2_DATA_DIR
            if (-not [IO.Path]::IsPathRooted($script:HomeDir)) {
                $script:HomeDir = Join-Path $script:Root $script:HomeDir
            }
        }
        else {
            # A checkout is a development environment: its own files are the home.
            $script:HomeDir = $script:Root
        }
    }
    elseif ($env:SLIFE2_DATA_DIR) {
        $script:HomeDir = $env:SLIFE2_DATA_DIR
        if (-not [IO.Path]::IsPathRooted($script:HomeDir)) {
            $script:HomeDir = Join-Path (Get-Location).Path $script:HomeDir
        }
    }
    else {
        $script:HomeDir = Join-Path $env:USERPROFILE '.slife2'
    }
    if ($env:SLIFE2_INSTALL_STATE) {
        $script:State = $env:SLIFE2_INSTALL_STATE
    }
    else {
        $script:State = Join-Path $script:HomeDir '.install-state.tsv'
    }
}

function Confirm-Action {
    if ($script:Yes) { return }
    if ([Console]::IsInputRedirected) { Die 3 'no terminal to confirm; re-run with --yes' }
    $answer = Read-Host 'Proceed? [y/N]'
    if ($answer -notmatch '^(y|yes)$') { exit 3 }
}

function Get-ManifestRows {
    $rows = @()
    if (-not $script:Manifest -or -not (Test-Path $script:Manifest)) { return $rows }
    foreach ($line in Get-Content $script:Manifest -Encoding UTF8) {
        if ($line.StartsWith('#') -or [string]::IsNullOrWhiteSpace($line)) { continue }
        $f = $line.Split("`t")
        if ($f.Count -ge 4) {
            $rows += [pscustomobject]@{
                Executable = $f[0]; Tool = $f[1]; Installer = $f[2]; Verify = $f[3]
            }
        }
    }
    return $rows
}

function Add-Owned([string]$Manager, [string]$Package, [string]$Tool) {
    $directory = Split-Path -Parent $script:State
    if (-not (Test-Path $directory)) { [void](New-Item -ItemType Directory -Path $directory -Force) }
    if (-not (Test-Path $script:State)) {
        [IO.File]::WriteAllText($script:State, "# host=$env:COMPUTERNAME`n", $Utf8NoBom)
    }
    $line = "$Manager`t$Package`t$Tool"
    foreach ($existing in Get-Content $script:State -Encoding UTF8) {
        if ($existing -eq $line) { return }
    }
    [IO.File]::AppendAllText($script:State, "$line`n", $Utf8NoBom)
}

function Get-ToolsDir {
    $tools = Join-Path $env:LOCALAPPDATA 'slife2\tools'
    if (-not (Test-Path $tools)) { [void](New-Item -ItemType Directory -Path $tools -Force) }
    return $tools
}

function Ensure-Uv {
    Step '1/8' 'uv'
    Update-ProcessPath
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        if ($script:NoUpgrade) { Skip 'uv upgrade disabled' }
        else {
            Say '+ uv self update'
            & uv self update | Out-Host
            if ($LASTEXITCODE -ne 0) { Warn 'uv self update failed; keeping the current version' }
        }
        Update-ProcessPath
        Ok ("uv " + ((& uv --version) -join ' '))
        return $true
    }
    Say 'installing uv into the user profile (the official installer, no administrator)'
    try {
        $installer = Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing -ErrorAction Stop
        Invoke-Expression $installer.Content
    }
    catch {
        Fail "uv could not be installed: $($_.Exception.Message)"
        return $false
    }
    Add-UserPath (Join-Path $env:USERPROFILE '.local\bin')
    Update-ProcessPath
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Fail 'uv is installed but is not on PATH'
        return $false
    }
    Add-Owned 'local' (Join-Path $env:USERPROFILE '.local\bin\uv.exe') 'uv'
    Ok ("uv " + ((& uv --version) -join ' '))
    return $true
}

function Ensure-Python {
    Step '2/8' 'Python 3.13'
    if ($script:NoUpgrade) { $arguments = @('python', 'install', '3.13', '--default') }
    else { $arguments = @('python', 'install', '3.13', '--upgrade', '--default') }
    Say ('+ uv ' + ($arguments -join ' '))
    & uv @arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail 'Python 3.13 could not be installed'; return $false }
    Update-ProcessPath
    Ok 'Python 3.13 ready (uv-managed)'
    return $true
}

function Get-RemoteFile([string]$Url, [string]$Path) {
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing -ErrorAction Stop
        return $true
    }
    catch { }
    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        & curl.exe -fsSL -o $Path $Url 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    return $false
}

function Download-Source {
    if (-not $script:WorkDir) {
        $script:WorkDir = Join-Path $env:TEMP ("slife2-install-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
        [void](New-Item -ItemType Directory -Force $script:WorkDir)
    }
    $zip = Join-Path $script:WorkDir 'slife2.zip'
    Say 'downloading the latest slife2'
    $ok = Get-RemoteFile 'https://github.com/juzcn/slife2/archive/refs/heads/main.zip' $zip
    if (-not $ok) {
        Warn 'GitHub unreachable; trying the gitee mirror'
        $ok = Get-RemoteFile 'https://gitee.com/juzcn/slife2/repository/archive/main.zip' $zip
    }
    if (-not $ok) { Fail 'could not download slife2 from GitHub or gitee'; return $false }
    Expand-Archive -Path $zip -DestinationPath $script:WorkDir -Force
    $source = Get-ChildItem $script:WorkDir -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } |
        Select-Object -First 1
    if (-not $source) { Fail 'the downloaded archive has no source directory'; return $false }
    $script:SourceDir = $source.FullName
    $script:Manifest = Join-Path $script:SourceDir 'scripts\dependencies.tsv'
    $script:Helper = Join-Path $script:SourceDir 'scripts\install_helper.py'
    if (-not (Test-Path $script:Manifest) -or -not (Test-Path $script:Helper)) {
        Fail 'the downloaded source has no scripts\ directory'
        return $false
    }
    Ok "source: $($script:SourceDir)"
    return $true
}

function Install-SlifeTool {
    Step '3/8' 'installing slife2'
    $wasInstalled = [bool]((& uv tool list 2>$null) -match '^slife2\s')
    Say "+ uv tool install --python 3.13 --force `"$($script:SourceDir)`""
    & uv tool install --python 3.13 --force $script:SourceDir | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail 'slife2 could not be installed'; return $false }
    if (-not $wasInstalled) { Add-Owned 'uv-tool' 'slife2' 'slife2' }
    & uv tool update-shell | Out-Null
    $binDir = (& uv tool dir --bin 2>$null | Select-Object -First 1)
    Add-UserPath $binDir
    Update-ProcessPath
    $toolDir = (& uv tool dir 2>$null | Select-Object -First 1)
    $script:Py = Join-Path $toolDir 'slife2\Scripts\python.exe'
    if (-not (Test-Path $script:Py)) {
        Fail "the slife2 tool environment has no interpreter at $script:Py"
        return $false
    }
    if (-not (Get-Command slife2 -ErrorAction SilentlyContinue)) {
        Fail 'slife2 is installed but is not on PATH'
        return $false
    }
    Ok 'slife2 installed (uv tool)'
    return $true
}

function Ensure-Venv {
    Step '3/8' 'project virtualenv'
    $script:Py = Join-Path $script:Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $script:Py)) {
        Say '+ uv venv --python 3.13'
        & uv venv --python 3.13 | Out-Host
        if ($LASTEXITCODE -ne 0) { Fail 'the virtualenv could not be created'; return $false }
    }
    if (-not (Test-Path $script:Py)) { Fail "the virtualenv has no interpreter at $script:Py"; return $false }

    & $script:Py -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)'
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[FAIL] .venv exists but is not Python 3.13.' -ForegroundColor Red
        Write-Host 'Refusing to recreate it, because that would delete packages you installed.'
        Write-Host 'Rebuild it yourself:'
        Write-Host '  uv pip freeze > venv-packages.txt'
        Write-Host '  Remove-Item -Recurse -Force .venv'
        Write-Host '  uv venv --python 3.13'
        Write-Host '  uv sync --upgrade --inexact'
        Write-Host '  # reinstall anything you added from venv-packages.txt'
        return $false
    }
    & $script:Py -c 'import sqlite3; c = sqlite3.connect(":memory:"); c.enable_load_extension(True)'
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[FAIL] .venv cannot load SQLite extensions.' -ForegroundColor Red
        Write-Host 'Refusing to recreate it, because that would delete packages you installed.'
        Write-Host 'Rebuild it with a Python that supports them (see README, "Requirements").'
        return $false
    }
    Ok "venv: $script:Py"
    return $true
}

function Invoke-SeedConfig {
    Step '4/8' 'configuration'
    & $script:Py $script:Helper seed --source (Join-Path $script:SourceDir 'slife2.yaml') --home $script:HomeDir
    if ($LASTEXITCODE -ne 0) { Fail 'the configuration could not be seeded'; return $false }
    return $true
}

function Invoke-UvSync {
    Step '5/8' 'Python dependencies'
    $lock = Join-Path $script:Root 'uv.lock'
    $before = if (Test-Path $lock) { (Get-FileHash $lock -Algorithm SHA256).Hash } else { '' }
    if ($script:NoUpgrade) { $arguments = @('sync', '--locked', '--inexact') }
    else { $arguments = @('sync', '--upgrade', '--inexact') }
    Say ("+ uv " + ($arguments -join ' '))
    & uv @arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail 'uv sync failed'; return $false }
    $after = if (Test-Path $lock) { (Get-FileHash $lock -Algorithm SHA256).Hash } else { '' }
    if ($before -and $before -ne $after) { Warn 'uv.lock changed; review and commit it yourself' }
    Ok 'Python dependencies synced'
    return $true
}

function Get-Requirements {
    Step '6/8' 'reading the effective configuration'
    $lines = & $script:Py $script:Helper require --home $script:HomeDir
    if ($LASTEXITCODE -ne 0) { return $null }
    $requirements = @()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line.Split("`t")
        if ($parts.Count -ge 2) {
            $requirements += [pscustomobject]@{ Executable = $parts[0]; Provenance = $parts[1] }
        }
    }
    Ok ("configuration names " + $requirements.Count + ' program(s)')
    return $requirements
}

function Test-NodeMajor {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) { return $true }
    $version = (& node --version) -replace '^v', ''
    $major = 0
    if (-not [int]::TryParse($version.Split('.')[0], [ref]$major)) {
        Fail 'could not read the Node version'
        return $false
    }
    if ($major -lt 20) {
        Fail "Node $major is too old; upgrade to Node 20 or newer yourself"
        return $false
    }
    return $true
}

function Ensure-UvTool([string]$Executable, [string]$Provenance, [string]$Python = '3.13') {
    if (Get-Command $Executable -ErrorAction SilentlyContinue) {
        $listed = (& uv tool list 2>$null) -match "^$Executable\s"
        if ($listed) {
            if ($script:NoUpgrade) { Skip "$Executable upgrade disabled" }
            else {
                Say "+ uv tool upgrade $Executable"
                & uv tool upgrade $Executable | Out-Host
                if ($LASTEXITCODE -ne 0) { Warn "$Executable upgrade failed" }
            }
        }
        else { Ok "$Executable present (unmanaged)" }
        return $true
    }
    Say "+ uv tool install --python $Python $Executable"
    & uv tool install --python $Python $Executable | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail "$Executable could not be installed (named by $Provenance)"; return $false }
    & uv tool update-shell | Out-Null
    Update-ProcessPath
    if (-not (Get-Command $Executable -ErrorAction SilentlyContinue)) {
        Fail "$Executable installed but is not on PATH"
        return $false
    }
    Add-Owned 'uv-tool' $Executable $Executable
    Ok "$Executable installed (uv tool)"
    return $true
}

function Get-NodeLts {
    $arch = switch ($env:PROCESSOR_ARCHITECTURE) {
        'ARM64' { 'arm64' }
        'x86' { 'x86' }
        default { 'x64' }
    }
    try {
        $sums = (Invoke-WebRequest -Uri 'https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt' -UseBasicParsing -ErrorAction Stop).Content
    }
    catch {
        if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { return $null }
        $sums = (& curl.exe -fsSL 'https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt') -join "`n"
    }
    $line = ($sums -split "`n") | Where-Object { $_ -match "node-v[0-9.]+-win-$arch\.zip" } | Select-Object -First 1
    if (-not $line) { return $null }
    $parts = $line.Trim() -split '\s+'
    return [pscustomobject]@{
        Version = ($parts[1] -replace '^node-', '' -replace "-win-$arch\.zip$", '')
        File = $parts[1]
        Sha = $parts[0]
    }
}

function Install-NodeRootless([object]$Info) {
    $tools = Get-ToolsDir
    $nodeDir = Join-Path $tools 'node'
    $zip = Join-Path $env:TEMP ("node-" + [Guid]::NewGuid().ToString('N').Substring(0, 8) + '.zip')
    $expand = Join-Path $env:TEMP ("node-expand-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    Say "+ downloading node $($Info.Version)"
    if (-not (Get-RemoteFile "https://nodejs.org/dist/latest-v22.x/$($Info.File)" $zip)) {
        Fail 'node could not be downloaded'
        return $false
    }
    $sha = (Get-FileHash $zip -Algorithm SHA256).Hash
    if ($sha -ne $Info.Sha.ToUpperInvariant()) {
        Fail "SHA256 mismatch for $($Info.File)"
        Remove-Item -Force $zip -ErrorAction SilentlyContinue
        return $false
    }
    Expand-Archive -Path $zip -DestinationPath $expand -Force
    $inner = Get-ChildItem $expand -Directory | Select-Object -First 1
    if (-not $inner) { Fail 'the node archive has no directory'; return $false }
    if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir }
    [void](New-Item -ItemType Directory -Path $nodeDir -Force)
    Move-Item (Join-Path $inner.FullName '*') $nodeDir -Force
    Remove-Item -Recurse -Force $expand -ErrorAction SilentlyContinue
    Remove-Item -Force $zip -ErrorAction SilentlyContinue
    Add-UserPath $nodeDir
    Update-ProcessPath
    if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
        Fail 'node installed but npx is not on PATH'
        return $false
    }
    Ok "node $($Info.Version) installed into $nodeDir"
    return $true
}

function Ensure-Node([string]$Provenance) {
    if (Get-Command node -ErrorAction SilentlyContinue) {
        if (-not (Test-NodeMajor)) { return $false }
        $nodeDir = Join-Path (Get-ToolsDir) 'node'
        if (Test-Path $nodeDir) {
            $info = Get-NodeLts
            if (-not $info) { Ok "node $(node --version) present; update check unavailable"; return $true }
            $current = (& node --version) -replace '^v', ''
            if ($current -eq $info.Version) { Ok "node v$current is current (rootless)"; return $true }
            if ($script:NoUpgrade) { Skip 'node upgrade disabled'; return $true }
            return (Install-NodeRootless $info)
        }
        Ok 'node present (unmanaged; not upgraded)'
        return $true
    }
    $info = Get-NodeLts
    if (-not $info) { Fail "could not resolve the Node LTS download (named by $Provenance)"; return $false }
    $ok = Install-NodeRootless $info
    if ($ok) { Add-Owned 'local' (Join-Path (Get-ToolsDir) 'node') 'node' }
    return $ok
}

function Ensure-Cloudflared([string]$Provenance) {
    $tools = Get-ToolsDir
    $target = Join-Path $tools 'cloudflared.exe'
    if (Get-Command cloudflared -ErrorAction SilentlyContinue) {
        if (-not (Test-Path $target)) { Ok 'cloudflared present (unmanaged; not upgraded)'; return $true }
    }
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'x86') { '386' } else { 'amd64' }
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-$arch.exe"
    if (-not $script:NoUpgrade -or -not (Test-Path $target)) {
        Say '+ downloading cloudflared'
        if (-not (Get-RemoteFile $url $target)) {
            Fail "cloudflared could not be downloaded (named by $Provenance)"
            return $false
        }
    }
    Add-UserPath $tools
    Update-ProcessPath
    if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
        Fail 'cloudflared is installed but is not on PATH'
        return $false
    }
    if (Test-Path $target) { Add-Owned 'local' $target 'cloudflared' }
    Ok 'cloudflared installed (rootless)'
    return $true
}

function Ensure-Mosquitto([string]$Provenance) {
    if (Get-Command mosquitto -ErrorAction SilentlyContinue) {
        Ok 'mosquitto present'
        return $true
    }
    if (Test-BrokerListening) {
        Ok 'an MQTT broker is already listening on 127.0.0.1:1883; mosquitto is not needed'
        return $true
    }
    Fail "mosquitto is missing and there is no portable Windows build for a normal user (named by $Provenance)."
    Say '  Install it from https://mosquitto.org/download/ (that installer may ask for an administrator),'
    Say '  or start an MQTT broker on 127.0.0.1:1883, then re-run.'
    return $false
}

function Install-Requirements([object[]]$Requirements) {
    Step '7/8' 'external programs'
    # uv and Python were ensured before the requirements were read.
    $tools = @{ 'uv' = $true; 'python' = $true }
    foreach ($requirement in $Requirements) {
        $exe = $requirement.Executable
        $tool = switch ($exe) {
            'npx' { 'node' }
            'node' { 'node' }
            default { $exe }
        }
        if ($tools.ContainsKey($tool)) { continue }
        $tools[$tool] = $true
        switch ($tool) {
            'python3' {
                if (Get-Command python3 -ErrorAction SilentlyContinue) { Ok 'python3 present' }
                else { Fail "python3 is not on PATH (named by $($requirement.Provenance))" }
            }
            'uvx' {
                if (Get-Command uvx -ErrorAction SilentlyContinue) { Ok 'uvx present' }
                else { Fail "uvx is not on PATH (named by $($requirement.Provenance))" }
            }
            'mosquitto' { [void](Ensure-Mosquitto $requirement.Provenance) }
            'cloudflared' { [void](Ensure-Cloudflared $requirement.Provenance) }
            'node' { [void](Ensure-Node $requirement.Provenance) }
            'ssh' {
                if (Get-Command ssh -ErrorAction SilentlyContinue) { Ok 'ssh present' }
                else { Fail "ssh is missing (named by $($requirement.Provenance)); install an OpenSSH client yourself" }
            }
            'yt-dlp' { [void](Ensure-UvTool 'yt-dlp' $requirement.Provenance '3.13') }
            'browser-harness' { [void](Ensure-UvTool 'browser-harness' $requirement.Provenance '3.12') }
            default {
                if (Get-Command $exe -ErrorAction SilentlyContinue) {
                    Ok "$exe present (unmanaged; named by $($requirement.Provenance))"
                }
                else {
                    Fail "$exe is missing and has no rootless installer (named by $($requirement.Provenance))"
                }
            }
        }
    }
}

function Show-Version([string]$Executable, [string[]]$Arguments) {
    if (-not (Get-Command $Executable -ErrorAction SilentlyContinue)) { return }
    $output = & $Executable @Arguments 2>&1 | Select-Object -First 1
    if ($output) { Write-Host ("       {0,-16} {1}" -f $Executable, $output) }
}

function Invoke-Verification([object[]]$Requirements) {
    Step '8/8' 'verification'
    foreach ($requirement in $Requirements) {
        if (Get-Command $requirement.Executable -ErrorAction SilentlyContinue) {
            $path = (Get-Command $requirement.Executable).Source
            Ok "$($requirement.Executable) -> $path"
        }
        elseif ($requirement.Executable -eq 'mosquitto' -and (Test-BrokerListening)) {
            Ok 'mosquitto not on PATH; an MQTT broker is already listening on 127.0.0.1:1883'
        }
        else {
            Fail "$($requirement.Executable) is still missing (named by $($requirement.Provenance))"
        }
    }

    if (Get-Command uv -ErrorAction SilentlyContinue) {
        & uv --version | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail 'uv does not run' }
    }
    else { Fail 'uv is not on PATH' }
    if (Get-Command uvx -ErrorAction SilentlyContinue) {
        & uvx --version | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail 'uvx does not run' }
    }
    else { Fail 'uvx is not on PATH' }
    if (Get-Command python3 -ErrorAction SilentlyContinue) {
        & python3 --version | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail 'python3 does not run (a Windows Store alias is not an interpreter)' }
    }
    else { Fail 'python3 is not on PATH' }
    & $script:Py -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)'
    if ($LASTEXITCODE -ne 0) { Fail 'the virtualenv is not Python 3.13' }
    & $script:Py -c 'import sqlite3, sqlite_vec; c = sqlite3.connect(":memory:"); c.enable_load_extension(True); sqlite_vec.load(c)'
    if ($LASTEXITCODE -ne 0) { Fail 'sqlite-vec cannot be loaded by this Python' }
    & $script:Py -c 'import slife2; print("slife2 " + slife2.__version__)'
    if ($LASTEXITCODE -ne 0) { Fail 'slife2 does not import' }
    if ($script:Mode -eq 'checkout') {
        & uv run --no-sync slife2 --help | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail 'uv run slife2 does not run' }
    }
    elseif (Get-Command slife2 -ErrorAction SilentlyContinue) {
        & slife2 --help | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail 'slife2 does not run' }
    }
    else { Fail 'slife2 is not on PATH' }

    Show-Version 'mosquitto' @('-h')
    Show-Version 'cloudflared' @('--version')
    Show-Version 'node' @('--version')
    Show-Version 'npx' @('--version')
    Show-Version 'ssh' @('-V')
    Show-Version 'yt-dlp' @('--version')
    Show-Version 'browser-harness' @('--version')
}

function Show-Summary {
    Write-Host ''
    if (-not $script:Failed) { Say 'summary: all required programs are present.' }
    else { Say 'summary: some required programs are missing or failed; see [FAIL] above.' }
    Say "  mode:  $script:Mode"
    Say "  home:  $script:HomeDir"
    Say "  state: $script:State"
    if ($script:Mode -eq 'checkout') {
        Say '  next:  uv run credstore set <PROVIDER>_API_KEY'
        Say '         uv run slife2'
    }
    else {
        Say '  next:  set <PROVIDER>_API_KEY in the environment (or store it with credstore)'
        Say '         slife2'
    }
}

function Invoke-Install {
    Say 'slife2 installer'
    Say '  platform: windows'
    Say "  method:   $script:Mode"
    Say "  home:     $script:HomeDir"
    Say "  state:    $script:State"
    Confirm-Action

    if (-not (Ensure-Uv)) { return $false }
    if (-not (Ensure-Python)) { return $false }
    if ($script:Mode -eq 'checkout') {
        if (-not (Ensure-Venv)) { return $false }
        if (-not (Invoke-SeedConfig)) { return $false }
        if (-not (Invoke-UvSync)) { return $false }
    }
    else {
        if (-not (Download-Source)) { return $false }
        if (-not (Install-SlifeTool)) { return $false }
        if (-not (Invoke-SeedConfig)) { return $false }
    }
    $requirements = Get-Requirements
    if ($null -eq $requirements) { return $false }
    Install-Requirements $requirements
    Invoke-Verification $requirements
    Show-Summary
    return (-not $script:Failed)
}

function Invoke-DryRun {
    Update-ProcessPath
    Say 'slife2 installer --dry-run'
    Say '  platform: windows'
    Say "  method:   $script:Mode"
    Say "  home:     $script:HomeDir"
    Say "  state:    $script:State"
    Say ''
    if ($script:Mode -eq 'checkout') {
        Say '[PLAN] external programs this installer manages:'
        foreach ($row in Get-ManifestRows) {
            $found = Get-Command $row.Executable -ErrorAction SilentlyContinue
            if ($found) { Write-Host ("  {0,-18} present   {1}" -f $row.Executable, $found.Source) }
            else { Write-Host ("  {0,-18} missing   ({1}; installs via {2})" -f $row.Executable, $row.Installer, $row.Tool) }
        }
    }
    else {
        Say '[PLAN] would download the latest main from'
        Say '       https://github.com/juzcn/slife2/archive/refs/heads/main.zip'
        Say '       and install it with uv tool install --python 3.13 --force.'
        Say '       The dependency table is read from the downloaded source.'
    }
    $live = Join-Path $script:HomeDir 'slife2.yaml'
    Say ''
    if ($script:Mode -eq 'checkout') {
        Say "[PLAN] config: checkout detected; $live is the live configuration; nothing to seed."
    }
    elseif (Test-Path $live) {
        Say "[PLAN] config: $live exists; would write the shipped reference to $(Join-Path $script:HomeDir 'slife2-default.yaml')."
    }
    else {
        Say "[PLAN] config: would create $live from the shipped configuration."
    }
    if ($script:Mode -eq 'checkout') {
        Say '[PLAN] install would then: ensure uv and Python 3.13, validate (never clear) .venv,'
        Say '       seed the config, run uv sync --upgrade --inexact, install every program'
        Say '       named by the effective configuration, and verify.'
    }
    else {
        Say '[PLAN] install would then: ensure uv and Python 3.13, install slife2 as a uv tool,'
        Say '       seed ~/.slife2, install every program named by the effective configuration,'
        Say '       and verify.'
    }
    exit 0
}

function Invoke-Uninstall {
    if (-not (Test-Path $script:State)) {
        Say "no installer-owned packages recorded at $script:State; nothing to remove."
        return 0
    }
    $entries = @()
    foreach ($line in Get-Content $script:State -Encoding UTF8) {
        if ($line.StartsWith('#') -or [string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line.Split("`t")
        if ($parts.Count -ge 3) {
            $entries += [pscustomobject]@{ Manager = $parts[0]; Package = $parts[1]; Tool = $parts[2] }
        }
    }
    if ($entries.Count -eq 0) {
        Say "no installer-owned packages recorded at $script:State; nothing to remove."
        return 0
    }

    Say 'WARNING: system packages may be shared with other software on this machine.'
    Say 'Only packages recorded as installed by this installer will be removed.'
    Say 'Data, configuration, skills, agents, runtime, .venv and uv-managed Python'
    Say 'are left untouched.'
    Say ''
    foreach ($entry in $entries) { Say "  [REMOVE] $($entry.Tool) ($($entry.Manager) $($entry.Package))" }
    Confirm-Action

    $keep = @()
    $code = 0
    foreach ($entry in $entries) {
        $ok = $true
        switch ($entry.Manager) {
            'local' {
                switch ($entry.Tool) {
                    'node' { Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA 'slife2\tools\node') -ErrorAction SilentlyContinue }
                    'cloudflared' { Remove-Item -Force (Join-Path $env:LOCALAPPDATA 'slife2\tools\cloudflared.exe') -ErrorAction SilentlyContinue }
                    'uv' {
                        Remove-Item -Force (Join-Path $env:USERPROFILE '.local\bin\uv.exe') -ErrorAction SilentlyContinue
                        Remove-Item -Force (Join-Path $env:USERPROFILE '.local\bin\uvx.exe') -ErrorAction SilentlyContinue
                    }
                    default { if ($entry.Package) { Remove-Item -Recurse -Force $entry.Package -ErrorAction SilentlyContinue } }
                }
            }
            'uv-tool' {
                Say "+ uv tool uninstall $($entry.Package)"
                & uv tool uninstall $entry.Package | Out-Host
                if ($LASTEXITCODE -ne 0) { $ok = $false }
            }
            default {
                Warn "unknown ownership manager '$($entry.Manager)' for $($entry.Tool); remove it yourself"
                $ok = $false
            }
        }
        if ($ok) { Ok "$($entry.Tool) removed" }
        else { Fail "$($entry.Tool) could not be removed"; $keep += $entry; $code = 1 }
    }

    $text = "# host=$env:COMPUTERNAME`n"
    foreach ($entry in $keep) { $text += "$($entry.Manager)`t$($entry.Package)`t$($entry.Tool)`n" }
    [IO.File]::WriteAllText($script:State, $text, $Utf8NoBom)
    Say ''
    Say "uninstall finished. The ownership record is $script:State."
    return $code
}

function Main {
    Resolve-Mode
    Resolve-DataHome

    if ($script:Action -eq 'install' -and $script:DryRun) { Invoke-DryRun }
    if ($script:Action -eq 'uninstall' -and -not (Test-Path $script:State)) {
        Say "no installer-owned packages recorded at $script:State; nothing to remove."
        exit 0
    }

    if ($script:Action -eq 'install') {
        if (Invoke-Install) { exit 0 } else { exit 1 }
    }
    exit (Invoke-Uninstall)
}

try {
    Parse-Arguments @args
    Main
}
catch {
    Fail $_.Exception.Message
    exit 2
}
