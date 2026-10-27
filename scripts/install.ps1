# slife2 installer for Windows (Windows PowerShell 5.1).
#
#   powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.ps1 | iex"
#   .\scripts\install.ps1                 # from a source checkout
#   .\scripts\install.ps1 --dry-run
#   .\scripts\install.ps1 uninstall
#
# Everything installs under the current user: %LOCALAPPDATA%\slife2 for
# binaries, ~\.slife2 for data and configuration.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:Mode = 'tool'
$script:Root = $null
$script:SourceDir = $null
$script:Helper = $null
$script:WorkDir = $null
$script:Py = $null
$script:HomeDir = $null
$script:State = $null
$script:Action = 'install'
$script:DryRun = $false
$script:Failed = $false
$script:ProgramHints = @()

function Say([string]$Message) { Write-Host $Message }
function Step([string]$Number, [string]$Message) { Write-Host ""; Write-Host "[$Number] $Message" }
function Fail([string]$Message) { Write-Host "error: $Message" -ForegroundColor Red; $script:Failed = $true }
function Die([string]$Message) { throw [System.Exception]::new($Message) }

function Invoke-Quiet([scriptblock]$Body, [switch]$Silent) {
    $log = Join-Path $env:TEMP ("slife2-" + [Guid]::NewGuid().ToString('N').Substring(0, 8) + '.log')
    # **Native stderr is output, not a failure.**  With the caller's
    # `$ErrorActionPreference = 'Stop'`, Windows PowerShell 5.1 turns a native
    # command's stderr into an error record once `*>` redirects it, and stops
    # the function: uv's "experimental option" warning failed the Python
    # install.  The exit code is the only verdict this function is entitled to.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Body *> $log
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            if (-not $Silent) {
                Get-Content $log -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
            }
            return $false
        }
        return $true
    }
    catch {
        if (-not $Silent) {
            Get-Content $log -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        }
        return $false
    }
    finally {
        $ErrorActionPreference = $previous
        Remove-Item $log -Force -ErrorAction SilentlyContinue
    }
}

function Get-UvTools {
    # **`2>$null` alone does not silence native stderr.**  With the caller's
    # `$ErrorActionPreference = 'Stop'`, Windows PowerShell 5.1 turns each
    # redirected stderr line into an error record; `uv tool list` exits 0 but
    # warns about a malformed tool, and that warning aborted the install
    # before it started.  Continue turns the warning back into output that the
    # redirection can drop.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { return @(& uv tool list 2>$null) }
    finally { $ErrorActionPreference = $previous }
}

function Parse-Arguments {
    foreach ($arg in $args) {
        $lower = $arg.ToLowerInvariant()
        if ($lower -eq 'install' -or $lower -eq 'uninstall') { $script:Action = $lower }
        elseif ($lower -eq '--dry-run' -or $lower -eq '-dryrun') { $script:DryRun = $true }
        elseif ($lower -eq '--help' -or $lower -eq '-help' -or $lower -eq '-h') {
            Say 'usage: install.ps1 [install|uninstall] [--dry-run]'
            $script:Action = 'help'
            return
        }
        elseif ($lower.StartsWith('-')) { Die "unknown option: $arg" }
        else { Die "unknown action: $arg" }
    }
    if ($script:DryRun -and $script:Action -ne 'install') { Die '--dry-run applies to install' }
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
            $script:Helper = Join-Path $script:Root 'scripts\install_helper.py'
            [void](Set-Location $script:Root)
            return
        }
    }
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
        else { $script:HomeDir = $script:Root }
    }
    elseif ($env:SLIFE2_DATA_DIR) {
        $script:HomeDir = $env:SLIFE2_DATA_DIR
        if (-not [IO.Path]::IsPathRooted($script:HomeDir)) {
            $script:HomeDir = Join-Path (Get-Location).Path $script:HomeDir
        }
    }
    else { $script:HomeDir = Join-Path $env:USERPROFILE '.slife2' }
    if ($env:SLIFE2_INSTALL_STATE) { $script:State = $env:SLIFE2_INSTALL_STATE }
    else { $script:State = Join-Path $script:HomeDir '.install-state.tsv' }
}

function Add-UserPath([string]$Directory) {
    if (-not $Directory) { return }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) { $userPath = '' }
    if ($userPath.Split(';') -notcontains $Directory) {
        [Environment]::SetEnvironmentVariable('Path', "$Directory;$userPath", 'User')
        $env:Path = "$Directory;$env:Path"
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

function Get-RemoteFile([string]$Url, [string]$Path) {
    # curl.exe draws a progress bar and is much faster than PS 5.1's
    # Invoke-WebRequest on a large response; fall back to IWR (which also
    # draws progress) when curl.exe is absent.
    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        & curl.exe -fL --progress-bar -o $Path $Url
        if ($LASTEXITCODE -eq 0) { return $true }
    }
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing -ErrorAction Stop
        return $true
    }
    catch {
        return $false
    }
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

function Ensure-Uv {
    Update-ProcessPath
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Say '  updating uv'
        [void](Invoke-Quiet { & uv self update } -Silent)
        return
    }
    Say '  installing uv'
    try {
        $installer = Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing -ErrorAction Stop
        Invoke-Expression $installer.Content
    }
    catch { Die "uv could not be installed: $($_.Exception.Message)" }
    Add-UserPath (Join-Path $env:USERPROFILE '.local\bin')
    Update-ProcessPath
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die 'uv is not on PATH' }
    Add-Owned 'local' (Join-Path $env:USERPROFILE '.local\bin\uv.exe') 'uv'
}

function Ensure-Python {
    # **Installing is not upgrading.**  `--upgrade` fetched the latest 3.13
    # patch on every run, and uv keeps the patch it replaced because a virtual
    # environment may still point into it: repeated runs leave a ~100 MB
    # interpreter behind each time.  A 3.13 that is already there is the
    # requirement; moving to a newer patch is the operator's call
    # (`uv python upgrade 3.13`).
    $installed = Invoke-Quiet { & uv python find 3.13 --no-python-downloads } -Silent
    if ($installed -and (Get-Command python3 -ErrorAction SilentlyContinue)) {
        Say '  Python 3.13 is already installed'
        Update-ProcessPath
        return
    }
    Say '  installing Python 3.13'
    # **`--default` was behind `--preview` in uv 0.7.**  An older uv still
    # installs 3.13 with the preview flag, so retry rather than failing the run
    # on a flag it does not know.
    if (-not (Invoke-Quiet { & uv python install 3.13 --default } -Silent)) {
        if (-not (Invoke-Quiet { & uv python install 3.13 --preview --default })) {
            Die 'Python 3.13 could not be installed'
        }
    }
    Update-ProcessPath
}

function Download-Source {
    $script:WorkDir = Join-Path $env:TEMP ("slife2-source-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    [void](New-Item -ItemType Directory -Path $script:WorkDir -Force)
    $zip = Join-Path $script:WorkDir 'source.zip'
    Say '  downloading slife2'
    if (-not (Get-RemoteFile 'https://github.com/juzcn/slife2/archive/refs/heads/main.zip' $zip)) {
        if (-not (Get-RemoteFile 'https://gitee.com/juzcn/slife2/repository/archive/main.zip' $zip)) {
            Die 'slife2 could not be downloaded'
        }
    }
    Say '  extracting slife2'
    # **Silent, and without the Archive module.**  `Expand-Archive` loads a
    # module and redraws the console while it works; the .NET call is the same
    # extraction with none of that, and matches the shell installer's quiet
    # `tar`.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $script:WorkDir)
    $source = Get-ChildItem $script:WorkDir -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } |
        Select-Object -First 1
    if (-not $source) { Die 'the downloaded source is incomplete' }
    $script:SourceDir = $source.FullName
    $script:Helper = Join-Path $script:SourceDir 'scripts\install_helper.py'
    if (-not (Test-Path $script:Helper)) { Die 'the downloaded source is incomplete' }
}

function Install-SlifeTool {
    $wasInstalled = [bool]((Get-UvTools) -match '^slife2\s')
    Say '  installing slife2'
    if (-not (Invoke-Quiet { & uv tool install --python 3.13 --force $script:SourceDir })) {
        Die 'slife2 could not be installed'
    }
    if (-not $wasInstalled) { Add-Owned 'uv-tool' 'slife2' 'slife2' }
    [void](Invoke-Quiet { & uv tool update-shell })
    Update-ProcessPath
    $toolDir = (& uv tool dir 2>$null | Select-Object -First 1)
    $script:Py = Join-Path $toolDir 'slife2\Scripts\python.exe'
    if (-not (Test-Path $script:Py)) { Die 'the slife2 environment is incomplete' }
}

function Ensure-Credstore {
    if (Get-Command credstore -ErrorAction SilentlyContinue) { return }
    Say '  installing credstore'
    if (-not (Invoke-Quiet { & uv tool install --python 3.13 credstore })) {
        Die 'credstore could not be installed'
    }
    Update-ProcessPath
    Add-Owned 'uv-tool' 'credstore' 'credstore'
}

function Ensure-Venv {
    $script:Py = Join-Path $script:Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $script:Py)) {
        Say '  creating the virtualenv'
        if (-not (Invoke-Quiet { & uv venv --python 3.13 })) { Die 'the virtualenv could not be created' }
    }
    & $script:Py -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)'
    if ($LASTEXITCODE -ne 0) { Die 'the existing .venv uses another Python; rebuild it with: uv venv --python 3.13' }
    & $script:Py -c "import sqlite3; c = sqlite3.connect(':memory:'); c.enable_load_extension(True)"
    if ($LASTEXITCODE -ne 0) { Die 'the existing .venv cannot load SQLite extensions' }
}

function Sync-Checkout {
    Say '  syncing dependencies'
    if (-not (Invoke-Quiet { & uv sync --upgrade --inexact })) { Die 'uv sync failed' }
}

function Seed-Config {
    if ($script:Mode -eq 'checkout') {
        & $script:Py $script:Helper seed --source (Join-Path $script:SourceDir 'slife2.yaml') --home $script:HomeDir | Out-Null
    }
    else {
        & $script:Py $script:Helper seed --source (Join-Path $script:SourceDir 'slife2.yaml') --home $script:HomeDir
    }
    if ($LASTEXITCODE -ne 0) { Die 'the configuration could not be prepared' }
}

function Get-Requirements {
    $lines = & $script:Py $script:Helper require --home $script:HomeDir
    if ($LASTEXITCODE -ne 0) { Die 'the configuration could not be read' }
    $requirements = @()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line.Split("`t")
        if ($parts.Count -ge 2) {
            # The third field is a `cli:` entry's install line, present only
            # when that entry is the program's sole claimant.  It is printed
            # when the program is missing, not executed.
            $hint = ''
            if ($parts.Count -ge 3) { $hint = ($parts[2..($parts.Count - 1)] -join "`t") }
            $requirements += [pscustomobject]@{
                Executable = $parts[0]
                Provenance = $parts[1]
                InstallHint = $hint
            }
        }
    }
    return $requirements
}

function Test-NodeVersion {
    $version = (& node --version) -replace '^v', ''
    $major = 0
    if (-not [int]::TryParse($version.Split('.')[0], [ref]$major)) {
        Fail 'could not read the Node version'
        return $false
    }
    if ($major -lt 20) {
        Fail "Node $major is too old; install Node 20 or newer"
        return $false
    }
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

function Install-Node {
    $info = Get-NodeLts
    if (-not $info) { return $false }
    $tools = Join-Path $env:LOCALAPPDATA 'slife2\tools'
    $nodeDir = Join-Path $tools 'node'
    $zip = Join-Path $env:TEMP ("node-" + [Guid]::NewGuid().ToString('N').Substring(0, 8) + '.zip')
    $expand = Join-Path $env:TEMP ("node-expand-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    Say "  downloading node $($info.Version)"
    if (-not (Get-RemoteFile "https://nodejs.org/dist/latest-v22.x/$($info.File)" $zip)) { return $false }
    if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne $info.Sha.ToUpperInvariant()) {
        Remove-Item -Force $zip -ErrorAction SilentlyContinue
        return $false
    }
    Say '  extracting node'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $expand)
    $inner = Get-ChildItem $expand -Directory | Select-Object -First 1
    if (-not $inner) { return $false }
    if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir }
    [void](New-Item -ItemType Directory -Path $nodeDir -Force)
    Move-Item (Join-Path $inner.FullName '*') $nodeDir -Force
    Remove-Item -Recurse -Force $expand -ErrorAction SilentlyContinue
    Remove-Item -Force $zip -ErrorAction SilentlyContinue
    Add-UserPath $nodeDir
    Update-ProcessPath
    return [bool](Get-Command npx -ErrorAction SilentlyContinue)
}

function Ensure-Node {
    $nodeDir = Join-Path $env:LOCALAPPDATA 'slife2\tools\node'
    if ((Get-Command node -ErrorAction SilentlyContinue) -and (Get-Command npx -ErrorAction SilentlyContinue)) {
        if (-not (Test-NodeVersion)) { return }
        if (Test-Path $nodeDir) {
            $info = Get-NodeLts
            if ($info -and ((& node --version) -replace '^v', '') -ne $info.Version) {
                if (Install-Node) { Say "  upgraded node $($info.Version)" }
            }
        }
        return
    }
    $info = Get-NodeLts
    if ($info -and (Install-Node)) {
        Add-Owned 'local' $nodeDir 'node'
        Say "  installed node $($info.Version)"
    }
    else { Fail 'Node.js could not be installed' }
}

function Ensure-Cloudflared {
    $target = Join-Path $env:LOCALAPPDATA 'slife2\tools\cloudflared.exe'
    if (Get-Command cloudflared -ErrorAction SilentlyContinue) {
        if (-not (Test-Path $target)) { return }
    }
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'x86') { '386' } else { 'amd64' }
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-$arch.exe"
    Say '  downloading cloudflared'
    if (-not (Get-RemoteFile $url $target)) { Fail 'cloudflared could not be downloaded'; return }
    Add-UserPath (Split-Path -Parent $target)
    Update-ProcessPath
    Add-Owned 'local' $target 'cloudflared'
    Say '  installed cloudflared'
}

function Ensure-Mosquitto {
    if (Get-Command mosquitto -ErrorAction SilentlyContinue) { return }
    if (Test-BrokerListening) { return }
    Fail 'mosquitto is unavailable; install it from https://mosquitto.org/download/ or run a broker on 127.0.0.1:1883'
}

function Ensure-UvTool([string]$Executable, [string]$Python) {
    if (Get-Command $Executable -ErrorAction SilentlyContinue) {
        if ((Get-UvTools) -match "^$Executable\s") {
            Say "  upgrading $Executable"
            [void](Invoke-Quiet { & uv tool upgrade $Executable })
        }
        return
    }
    Say "  installing $Executable"
    if (Invoke-Quiet { & uv tool install --python $Python $Executable }) {
        [void](Invoke-Quiet { & uv tool update-shell })
        Update-ProcessPath
        Add-Owned 'uv-tool' $Executable $Executable
        Say "  installed $Executable"
    }
    else { Fail "$Executable could not be installed" }
}

function Install-Dependencies([object[]]$Requirements) {
    $seen = @{ 'uv' = $true; 'python' = $true }
    foreach ($requirement in $Requirements) {
        $exe = $requirement.Executable
        $tool = switch ($exe) { 'npx' { 'node' } 'node' { 'node' } default { $exe } }
        if ($seen.ContainsKey($tool)) { continue }
        $seen[$tool] = $true
        switch ($tool) {
            'python3' { if (-not (Get-Command python3 -ErrorAction SilentlyContinue)) { Fail 'python3 is missing' } }
            'uvx' { if (-not (Get-Command uvx -ErrorAction SilentlyContinue)) { Fail 'uvx is missing' } }
            'mosquitto' { Ensure-Mosquitto }
            'cloudflared' { Ensure-Cloudflared }
            'node' { Ensure-Node }
            'ssh' { if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) { Fail 'ssh is missing; install an OpenSSH client' } }
            'yt-dlp' { Ensure-UvTool 'yt-dlp' '3.13' }
            'browser-harness' { Ensure-UvTool 'browser-harness' '3.12' }
            default {
                # A configured install line is the operator's answer for a
                # program this script cannot provide; verification prints it
                # rather than failing the run.  Without one, it is a gap.
                if (-not (Get-Command $exe -ErrorAction SilentlyContinue) -and -not $requirement.InstallHint) {
                    Fail "$exe is missing and has no user-level installer ($($requirement.Provenance))"
                }
            }
        }
    }
}

function Invoke-Verification([object[]]$Requirements) {
    foreach ($requirement in $Requirements) {
        if (Get-Command $requirement.Executable -ErrorAction SilentlyContinue) { continue }
        if ($requirement.Executable -eq 'mosquitto' -and (Test-BrokerListening)) { continue }
        # The one miss that is the operator's to fix rather than a failed
        # install, exactly as an unset credential is: the config carries the
        # command, and the run reports success with the command printed.
        if ($requirement.InstallHint) {
            $script:ProgramHints += [pscustomobject]@{
                Executable = $requirement.Executable
                InstallHint = $requirement.InstallHint
                Provenance = $requirement.Provenance
            }
            continue
        }
        Fail "$($requirement.Executable) is missing ($($requirement.Provenance))"
    }
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        & uv --version | Out-Null; if ($LASTEXITCODE -ne 0) { Fail 'uv does not run' }
    }
    else { Fail 'uv is missing' }
    if (Get-Command uvx -ErrorAction SilentlyContinue) {
        & uvx --version | Out-Null; if ($LASTEXITCODE -ne 0) { Fail 'uvx does not run' }
    }
    else { Fail 'uvx is missing' }
    if (Get-Command python3 -ErrorAction SilentlyContinue) {
        & python3 --version | Out-Null; if ($LASTEXITCODE -ne 0) { Fail 'python3 does not run' }
    }
    else { Fail 'python3 is missing' }
    & $script:Py -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)'
    if ($LASTEXITCODE -ne 0) { Fail 'Python 3.13 is not available' }
    & $script:Py -c "import sqlite3, sqlite_vec; c = sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c)"
    if ($LASTEXITCODE -ne 0) { Fail 'SQLite extensions are not available' }
    & $script:Py -c 'import slife2'
    if ($LASTEXITCODE -ne 0) { Fail 'slife2 does not import' }
    # The module, not the console script: `slife2 --help` on PATH can be a
    # stale shim or profile alias that runs the TUI instead of answering the
    # flag.  `-m slife2` is this environment's code and argparse handles it.
    & $script:Py -m slife2 --help | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail 'slife2 does not run' }
}

function Show-ProgramHints {
    if ($script:ProgramHints.Count -eq 0) { return }
    Say ''
    Say '  programs: install the ones you have not set up:'
    foreach ($program in $script:ProgramHints) {
        Say ("    {0}    {1}    ({2})" -f $program.Executable, $program.InstallHint, $program.Provenance)
    }
}

function Show-Credentials {
    if (-not $script:Py -or -not $script:Helper) { return }
    $lines = & $script:Py $script:Helper secrets --home $script:HomeDir 2>$null
    if ($LASTEXITCODE -ne 0) { return }
    $needs = @($lines | Where-Object { $_ })
    if ($needs.Count -eq 0) { return }
    Say ''
    Say "  credentials: store the ones you have not set with 'credstore set':"
    foreach ($line in $needs) {
        $parts = $line.TrimEnd("`r") -split "`t", 2
        $key = $parts[0]
        $origins = if ($parts.Count -gt 1) { $parts[1] } else { '' }
        Say ("    credstore set {0}    ({1})" -f $key, $origins)
    }
}

function Show-Next {
    Write-Host ''
    if ($script:Failed) { Say 'Installation failed.'; return }
    $version = (& $script:Py -c 'import slife2; print(slife2.__version__)' 2>$null)
    if ($version) { Say "Installed slife2 $version." }
    else { Say 'Installed slife2.' }
    if ($script:Mode -eq 'checkout') {
        Say '  run:     uv run slife2'
    }
    else {
        Say '  run:     slife2'
    }
    Say "  data:    $($script:HomeDir)"
    Say "  config:  $(Join-Path $script:HomeDir 'slife2.yaml')"
    Show-ProgramHints
    Show-Credentials
}

function Invoke-Install {
    Say "slife2 installer ($($script:Mode))"
    Step '1/4' 'uv'
    Ensure-Uv
    Step '2/4' 'Python 3.13'
    Ensure-Python
    Step '3/4' 'slife2'
    if ($script:Mode -eq 'checkout') {
        Ensure-Venv
        Seed-Config
        Sync-Checkout
    }
    else {
        Download-Source
        Install-SlifeTool
        Seed-Config
    }
    Ensure-Credstore
    Step '4/4' 'dependencies'
    $requirements = Get-Requirements
    Install-Dependencies $requirements
    Invoke-Verification $requirements
    Show-Next
    if ($script:Failed) { return 1 }
    return 0
}

function Invoke-DryRun {
    Say "slife2 installer ($($script:Mode), dry run)"
    if ($script:Mode -eq 'checkout') {
        Say '  would validate .venv, sync dependencies, and install configured programs'
    }
    else {
        Say '  would install slife2 as a uv tool and install configured programs'
    }
    return 0
}

function Invoke-Uninstall {
    if (-not (Test-Path $script:State)) { Say 'Nothing to uninstall.'; return 0 }
    $entries = @()
    foreach ($line in Get-Content $script:State -Encoding UTF8) {
        if ($line.StartsWith('#') -or [string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line.Split("`t")
        if ($parts.Count -ge 3) {
            $entries += [pscustomobject]@{ Manager = $parts[0]; Package = $parts[1]; Tool = $parts[2] }
        }
    }
    if ($entries.Count -eq 0) { Say 'Nothing to uninstall.'; return 0 }

    Say 'Uninstalling slife2 components...'
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
                & uv tool uninstall $entry.Package | Out-Null
                if ($LASTEXITCODE -ne 0) { $ok = $false }
            }
            default { $ok = $false }
        }
        if (-not $ok) { $keep += $entry; $code = 1 }
    }

    $text = "# host=$env:COMPUTERNAME`n"
    foreach ($entry in $keep) { $text += "$($entry.Manager)`t$($entry.Package)`t$($entry.Tool)`n" }
    [IO.File]::WriteAllText($script:State, $text, $Utf8NoBom)
    if ($code -eq 0) { Say 'Uninstalled.' }
    else { Say 'Some components could not be removed.' }
    return $code
}

$code = 0
try {
    Parse-Arguments @args
    if ($script:Action -ne 'help') {
        Resolve-Mode
        Resolve-DataHome
        if ($script:Action -eq 'install') {
            if ($script:DryRun) { $code = Invoke-DryRun } else { $code = Invoke-Install }
        }
        else { $code = Invoke-Uninstall }
    }
}
catch {
    Fail $_.Exception.Message
    $code = 2
}
# **An inline run (`irm ... | iex`) lives in the caller's shell.**  `exit` here
# closes the window or pane the command was pasted into — Windows Terminal then
# switches to another window, which looks like something was started.  Return
# to the prompt instead; a script file (`-File`, install.cmd) still reports its
# code.
if ($PSScriptRoot) { exit $code }
