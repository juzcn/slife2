#!/usr/bin/env bash
#
# slife2 installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.sh | bash
#   ./scripts/install.sh                   # from a source checkout
#   ./scripts/install.sh --dry-run
#   ./scripts/install.sh uninstall
#
# Everything installs under the current user: ~/.local for binaries and
# libraries, ~/.slife2 for data and configuration.

set -u

WORK_DIR=""
trap '[ -n "${WORK_DIR:-}" ] && rm -rf "$WORK_DIR"' EXIT

MODE=tool
ROOT=""
SOURCE=""
HELPER=""
PY=""
PY313=""
ACTION=install
DRY_RUN=0
FAILED=0
REQ_FILE=""

say() { printf '%s\n' "$*"; }
step() { printf '[%s] %s\n' "$1" "$2"; }
fail() { printf 'error: %s\n' "$*" >&2; FAILED=1; }
die() { printf 'error: %s\n' "$*" >&2; exit 2; }
stage() {  # message command...
    local message=$1
    shift
    local log pid code
    log=$(mktemp) || return 1
    printf '  %s' "$message"
    "$@" >"$log" 2>&1 &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        [ -t 1 ] && printf '.'
        sleep 1
    done
    wait "$pid"
    code=$?
    printf '\n'
    [ "$code" -eq 0 ] || tail -n 20 "$log" >&2
    rm -f "$log"
    return "$code"
}

for arg in "$@"; do
    case "$arg" in
        install|uninstall) ACTION=$arg ;;
        --dry-run) DRY_RUN=1 ;;
        --help|-h)
            say "usage: install.sh [install|uninstall] [--dry-run]"
            exit 0
            ;;
        -*) die "unknown option: $arg" ;;
        *) die "unknown action: $arg" ;;
    esac
done
[ "$DRY_RUN" -eq 1 ] && [ "$ACTION" != install ] && die "--dry-run applies to install"

case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin) PLATFORM=macos ;;
    Linux) PLATFORM=linux ;;
    *) die "unsupported operating system" ;;
esac
[ "$(id -u)" -eq 0 ] && die "run as your normal user"

is_checkout() { [ -f "$1/pyproject.toml" ] && grep -q 'name = "slife2"' "$1/pyproject.toml"; }
script_dir=""
[ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] \
    && script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [ -n "$script_dir" ] && is_checkout "$(cd -- "$script_dir/.." && pwd)"; then
    MODE=checkout
    ROOT=$(cd -- "$script_dir/.." && pwd)
elif is_checkout "$PWD"; then
    MODE=checkout
    ROOT=$PWD
fi
if [ "$MODE" = checkout ]; then
    cd "$ROOT" || die "could not enter $ROOT"
    SOURCE=$ROOT
    HELPER=$ROOT/scripts/install_helper.py
fi

if [ "$MODE" = checkout ]; then
    HOME_DIR=${SLIFE2_DATA_DIR:-$ROOT}
    case "$HOME_DIR" in /*) ;; *) HOME_DIR="$ROOT/$HOME_DIR" ;; esac
else
    HOME_DIR=${SLIFE2_DATA_DIR:-$HOME/.slife2}
    case "$HOME_DIR" in /*) ;; *) HOME_DIR="$PWD/$HOME_DIR" ;; esac
fi
STATE=${SLIFE2_INSTALL_STATE:-$HOME_DIR/.install-state.tsv}

refresh_path() {
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
    if [ "$PLATFORM" = macos ] && ! command -v brew >/dev/null 2>&1; then
        for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
            [ -x "$b" ] && eval "$("$b" shellenv)" 2>/dev/null
        done
    fi
    if command -v uv >/dev/null 2>&1; then
        bin=$(uv tool dir --bin 2>/dev/null || true)
        [ -n "$bin" ] && case ":$PATH:" in *":$bin:"*) ;; *) PATH="$bin:$PATH" ;; esac
    fi
    export PATH
}

hash_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

broker_listening() {
    (exec 3<>/dev/tcp/127.0.0.1/1883) 2>/dev/null && { exec 3>&-; return 0; }
    return 1
}

require_homebrew() {
    [ "$PLATFORM" = macos ] || return 0
    command -v brew >/dev/null 2>&1 && return 0
    die "macOS needs Homebrew's python@3.13; install Homebrew from https://brew.sh, then re-run"
}

state_header() {
    [ -f "$STATE" ] && return 0
    mkdir -p "$(dirname "$STATE")"
    printf '# host=%s\n' "$(hostname 2>/dev/null || echo unknown)" > "$STATE"
}

record_owned() {  # manager package tool
    state_header
    line=$(printf '%s\t%s\t%s' "$1" "$2" "$3")
    grep -qxF "$line" "$STATE" 2>/dev/null || printf '%s\n' "$line" >> "$STATE"
}

ensure_uv() {
    refresh_path
    if command -v uv >/dev/null 2>&1; then
        uv self update >/dev/null 2>&1 || true
        return 0
    fi
    say "  installing uv"
    stage "installing uv" sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' \
        || die "uv could not be installed"
    record_owned local "$HOME/.local/bin/uv" uv
    refresh_path
    command -v uv >/dev/null 2>&1 || die "uv is not on PATH"
}

ensure_python() {
    if [ "$PLATFORM" = macos ]; then
        require_homebrew
        if ! brew list --formula python@3.13 >/dev/null 2>&1; then
            say "  installing python@3.13"
            stage "installing python@3.13" brew install python@3.13 \
                || die "python@3.13 could not be installed"
            record_owned brew python@3.13 python
        else
            stage "updating python@3.13" brew upgrade python@3.13 || true
        fi
        PY313="$(brew --prefix python@3.13)/bin/python3.13"
        [ -x "$PY313" ] || die "python@3.13 is missing"
    else
        stage "installing Python 3.13" uv python install 3.13 --upgrade --default \
            || die "Python 3.13 could not be installed"
        refresh_path
    fi
}

download_source() {
    WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/slife2-source.XXXXXX") || die "temporary directory failed"
    SOURCE="$WORK_DIR/source"
    mkdir -p "$SOURCE"
    archive="$WORK_DIR/source.tar.gz"
    say "  downloading slife2"
    curl -fL --progress-bar -o "$archive" "https://github.com/juzcn/slife2/archive/refs/heads/main.tar.gz" \
        || curl -fL --progress-bar -o "$archive" "https://gitee.com/juzcn/slife2/repository/archive/main.tar.gz" \
        || die "slife2 could not be downloaded"
    tar -xzf "$archive" -C "$SOURCE" --strip-components=1 || die "slife2 could not be extracted"
    HELPER="$SOURCE/scripts/install_helper.py"
    [ -f "$HELPER" ] || die "the downloaded source is incomplete"
}

install_tool() {
    local was=0 tool_dir
    uv tool list 2>/dev/null | grep -q '^slife2 ' && was=1
    if [ "$PLATFORM" = macos ]; then
        stage "installing slife2" uv tool install --python "$PY313" --force "$SOURCE" \
            || die "slife2 could not be installed"
    else
        stage "installing slife2" uv tool install --python 3.13 --force "$SOURCE" \
            || die "slife2 could not be installed"
    fi
    [ "$was" -eq 0 ] && record_owned uv-tool slife2 slife2
    uv tool update-shell >/dev/null 2>&1 || true
    refresh_path
    tool_dir=$(uv tool dir 2>/dev/null) || die "the slife2 environment is missing"
    PY="$tool_dir/slife2/bin/python"
    [ -x "$PY" ] || die "the slife2 environment is incomplete"
}

ensure_credstore() {
    command -v credstore >/dev/null 2>&1 && return 0
    stage "installing credstore" uv tool install --python 3.13 credstore \
        || die "credstore could not be installed"
    record_owned uv-tool credstore credstore
    refresh_path
}

ensure_venv() {
    PY="$ROOT/.venv/bin/python"
    if [ ! -x "$PY" ]; then
        if [ "$PLATFORM" = macos ]; then
            stage "creating the virtualenv" uv venv --python "$PY313" \
                || die "the virtualenv could not be created"
        else
            stage "creating the virtualenv" uv venv --python 3.13 \
                || die "the virtualenv could not be created"
        fi
    fi
    "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' >/dev/null 2>&1 \
        || die "the existing .venv uses another Python; rebuild it with: uv venv --python 3.13"
    "$PY" -c 'import sqlite3; c = sqlite3.connect(":memory:"); c.enable_load_extension(True)' >/dev/null 2>&1 \
        || die "the existing .venv cannot load SQLite extensions; rebuild it with Homebrew Python"
}

sync_checkout() {
    stage "syncing dependencies" uv sync --upgrade --inexact || die "uv sync failed"
}

seed_config() {
    if [ "$MODE" = checkout ]; then
        "$PY" "$HELPER" seed --source "$SOURCE/slife2.yaml" --home "$HOME_DIR" >/dev/null \
            || die "the configuration could not be prepared"
    else
        "$PY" "$HELPER" seed --source "$SOURCE/slife2.yaml" --home "$HOME_DIR" \
            || die "the configuration could not be prepared"
    fi
}

requirements() {
    REQ_FILE=$(mktemp) || die "temporary file failed"
    "$PY" "$HELPER" require --home "$HOME_DIR" > "$REQ_FILE" \
        || die "the configuration could not be read"
}

install_node() {
    local os arch sums line file sha tmp name version
    case "$(uname -s)/$(uname -m)" in
        Linux/x86_64|Linux/amd64) os=linux; arch=x64 ;;
        Linux/aarch64|Linux/arm64) os=linux; arch=arm64 ;;
        Darwin/x86_64) os=darwin; arch=x64 ;;
        Darwin/arm64) os=darwin; arch=arm64 ;;
        *) return 1 ;;
    esac
    sums=$(curl -fsSL https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt) || return 1
    line=$(printf '%s\n' "$sums" | grep "node-v[0-9.]*-$os-$arch.tar.gz" | head -1)
    [ -n "$line" ] || return 1
    file=$(printf '%s' "$line" | awk '{print $2}')
    sha=$(printf '%s' "$line" | awk '{print $1}')
    version=$(printf '%s' "$file" | sed -n 's/.*node-\(v[0-9.]*\)-.*/\1/p')
    tmp=$(mktemp -d) || return 1
    say "  downloading node $version"
    curl -fL --progress-bar -o "$tmp/node.tgz" "https://nodejs.org/dist/latest-v22.x/$file" \
        || { rm -rf "$tmp"; return 1; }
    [ "$(hash_file "$tmp/node.tgz")" = "$sha" ] || { rm -rf "$tmp"; return 1; }
    rm -rf "$HOME/.local/lib/nodejs"
    mkdir -p "$HOME/.local/lib/nodejs" "$HOME/.local/bin"
    tar -xzf "$tmp/node.tgz" -C "$HOME/.local/lib/nodejs" --strip-components=1 \
        || { rm -rf "$tmp"; return 1; }
    for name in node npm npx; do
        ln -sf "$HOME/.local/lib/nodejs/bin/$name" "$HOME/.local/bin/$name"
    done
    rm -rf "$tmp"
    refresh_path
}

cloudflared_current() {
    local installed latest
    installed=$("$HOME/.local/bin/cloudflared" --version 2>/dev/null | awk '{print $3}')
    [ -n "$installed" ] || return 1
    latest=$(curl -fsSIL -o /dev/null -w '%{url_effective}' \
        "https://github.com/cloudflare/cloudflared/releases/latest" 2>/dev/null \
        | sed -n 's|.*/tag/||p')
    [ -n "$latest" ] && [ "$latest" = "$installed" ]
}

install_cloudflared() {
    local arch tmp
    case "$(uname -m)" in
        x86_64|amd64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) return 1 ;;
    esac
    mkdir -p "$HOME/.local/bin"
    say "  downloading cloudflared"
    # **Download beside the destination and rename it into place.**  Writing
    # over a `cloudflared` that is running fails with ETXTBSY ("Text file
    # busy"), and the file-tunnel plugin keeps one running while an install can
    # legitimately happen.  A rename replaces the name, not the running inode;
    # the old process finishes on the old file and the next start reads the new
    # one.
    tmp=$(mktemp "$HOME/.local/bin/.cloudflared.XXXXXX") || return 1
    curl -fL --progress-bar -o "$tmp" \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch" \
        || { rm -f "$tmp"; return 1; }
    chmod +x "$tmp" || { rm -f "$tmp"; return 1; }
    mv -f "$tmp" "$HOME/.local/bin/cloudflared" || { rm -f "$tmp"; return 1; }
    refresh_path
}

install_mosquitto() {
    command -v apt-get >/dev/null 2>&1 && command -v dpkg >/dev/null 2>&1 || return 1
    local tmp debs deb deps missing pkg
    tmp=$(mktemp -d) || return 1
    debs="$tmp/debs"
    mkdir -p "$debs"
    deps=$(apt-cache depends --recurse --no-recommends --no-suggests \
        --no-conflicts --no-breaks --no-replaces --no-enhances \
        mosquitto mosquitto-clients 2>/dev/null \
        | sed -n 's/^[ |]*Depends: //p' | sed 's/<//g; s/>//g' | awk '{print $1}' | sort -u)
    missing=""
    for pkg in $deps; do
        dpkg -s "$pkg" >/dev/null 2>&1 && continue
        # A virtual dependency (`debconf-2.0`, `libc-dev`) has a name but no
        # candidate, and one such name makes `apt-get download` refuse the
        # whole invocation — which is how mosquitto once ended up extracted
        # without any of the libraries it links against.  `madison` is empty
        # exactly for those names; skip them, and download the rest one by one.
        [ -n "$(apt-cache madison "$pkg" 2>/dev/null)" ] || continue
        missing="$missing $pkg"
    done
    say "  downloading mosquitto packages"
    for pkg in mosquitto mosquitto-clients $missing; do
        ( cd "$debs" && apt-get -q download "$pkg" >/dev/null ) \
            || { rm -rf "$tmp"; return 1; }
    done
    ls "$debs"/*.deb >/dev/null 2>&1 || { rm -rf "$tmp"; return 1; }
    for deb in "$debs"/*.deb; do
        dpkg -x "$deb" "$HOME/.local" || { rm -rf "$tmp"; return 1; }
    done
    rm -rf "$tmp"
    mkdir -p "$HOME/.local/bin"
    # The wrapper must be a regular file.  An install made before it existed
    # left a symlink here to the binary in `usr/sbin`; a redirection through
    # that symlink would replace the binary with this script, which then execs
    # itself forever.
    rm -f "$HOME/.local/bin/mosquitto"
    cat > "$HOME/.local/bin/mosquitto" <<'EOF'
#!/bin/sh
root="$HOME/.local"
LD_LIBRARY_PATH="$root/usr/lib:$root/usr/lib/x86_64-linux-gnu:$root/usr/lib/aarch64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH
exec "$root/usr/sbin/mosquitto" "$@"
EOF
    chmod +x "$HOME/.local/bin/mosquitto"
    refresh_path
    "$HOME/.local/bin/mosquitto" -h 2>&1 | grep -q "mosquitto version"
}

ensure_tool() {  # executable provenance
    local exe=$1 why=$2 major
    case "$exe" in
        mosquitto)
            if command -v mosquitto >/dev/null 2>&1; then
                if [ -e "$HOME/.local/bin/mosquitto" ]; then
                    install_mosquitto >/dev/null 2>&1 && say "  upgraded mosquitto"
                fi
                return 0
            fi
            broker_listening && return 0
            if [ "$PLATFORM" = macos ]; then
                require_homebrew
                if stage "installing mosquitto" brew install mosquitto; then
                    record_owned brew mosquitto mosquitto
                    say "  installed mosquitto"
                else
                    fail "mosquitto could not be installed ($why)"
                fi
            elif install_mosquitto; then
                record_owned local "$HOME/.local/usr" mosquitto
                say "  installed mosquitto"
            else
                fail "mosquitto is unavailable ($why); install it yourself or run a broker on 127.0.0.1:1883"
            fi
            ;;
        cloudflared)
            if [ -e "$HOME/.local/bin/cloudflared" ]; then
                if cloudflared_current; then
                    say "  cloudflared is up to date"
                elif install_cloudflared; then
                    say "  upgraded cloudflared"
                fi
                return 0
            fi
            command -v cloudflared >/dev/null 2>&1 && return 0
            if [ "$PLATFORM" = macos ]; then
                require_homebrew
                if stage "installing cloudflared" brew install cloudflared; then
                    record_owned brew cloudflared cloudflared
                    say "  installed cloudflared"
                else
                    fail "cloudflared could not be installed ($why)"
                fi
            elif install_cloudflared; then
                record_owned local "$HOME/.local/bin/cloudflared" cloudflared
                say "  installed cloudflared"
            else
                fail "cloudflared could not be installed ($why)"
            fi
            ;;
        npx|node)
            if command -v node >/dev/null 2>&1 && command -v npx >/dev/null 2>&1; then
                major=$(node --version 2>/dev/null | sed 's/^v//; s/\..*//')
                case "$major" in
                    ''|*[!0-9]*) fail "could not read the Node version" ;;
                    *) [ "$major" -ge 20 ] || fail "Node $major is too old; install Node 20 or newer" ;;
                esac
                return 0
            fi
            if install_node; then
                record_owned local "$HOME/.local/lib/nodejs" node
                say "  installed node $(node --version 2>/dev/null)"
            else
                fail "Node.js could not be installed ($why)"
            fi
            ;;
        ssh)
            command -v ssh >/dev/null 2>&1 || fail "ssh is missing ($why); install an OpenSSH client"
            ;;
        yt-dlp|browser-harness)
            if command -v "$exe" >/dev/null 2>&1; then
                if uv tool list 2>/dev/null | grep -q "^$exe "; then
                    uv tool upgrade "$exe" >/dev/null 2>&1 && say "  upgraded $exe"
                fi
                return 0
            fi
            if [ "$exe" = yt-dlp ]; then python=3.13; else python=3.12; fi
            if stage "installing $exe" uv tool install --python "$python" "$exe"; then
                uv tool update-shell >/dev/null 2>&1 || true
                record_owned uv-tool "$exe" "$exe"
                refresh_path
                say "  installed $exe"
            else
                fail "$exe could not be installed ($why)"
            fi
            ;;
        *)
            command -v "$exe" >/dev/null 2>&1 || fail "$exe is missing and has no user-level installer ($why)"
            ;;
    esac
}

install_dependencies() {
    local seen=" uv python " exe why
    while IFS=$(printf '\t') read -r exe why; do
        [ -n "$exe" ] || continue
        case " $seen " in *" $exe "*) continue ;; esac
        seen="$seen$exe "
        case "$exe" in
            uv|python|python3|uvx)
                command -v "$exe" >/dev/null 2>&1 || fail "$exe is missing ($why)"
                ;;
            *) ensure_tool "$exe" "$why" ;;
        esac
    done < "$REQ_FILE"
}

verify() {
    local exe why
    while IFS=$(printf '\t') read -r exe why; do
        [ -n "$exe" ] || continue
        command -v "$exe" >/dev/null 2>&1 && continue
        [ "$exe" = mosquitto ] && broker_listening && continue
        fail "$exe is missing ($why)"
    done < "$REQ_FILE"
    uv --version >/dev/null 2>&1 || fail "uv does not run"
    uvx --version >/dev/null 2>&1 || fail "uvx does not run"
    python3 --version >/dev/null 2>&1 || fail "python3 does not run"
    "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' \
        || fail "Python 3.13 is not available"
    "$PY" -c 'import sqlite3, sqlite_vec; c = sqlite3.connect(":memory:"); c.enable_load_extension(True); sqlite_vec.load(c)' \
        || fail "SQLite extensions are not available"
    "$PY" -c 'import slife2' || fail "slife2 does not import"
    # The module, not the console script: `slife2 --help` on PATH can be a
    # stale shim or function that runs the TUI instead of answering the flag.
    "$PY" -m slife2 --help >/dev/null 2>&1 || fail "slife2 does not run"
}

print_credentials() {
    local file key origins
    [ -n "${PY:-}" ] && [ -n "${HELPER:-}" ] || return 0
    file=$(mktemp) || return 0
    # `tr` because a Windows Python writes CRLF; the installer itself runs on a
    # POSIX host, but a checkout on a mounted drive may hand it one.
    "$PY" "$HELPER" secrets --home "$HOME_DIR" 2>/dev/null | tr -d '\r' > "$file"
    if [ -s "$file" ]; then
        say ""
        say "  credentials: store the ones you have not set with 'credstore set':"
        while IFS=$(printf '\t') read -r key origins; do
            [ -n "$key" ] || continue
            say "    credstore set $key    ($origins)"
        done < "$file"
    fi
    rm -f "$file"
}

print_next() {
    local version
    say ""
    [ "$FAILED" -eq 0 ] || { say "Installation failed."; return; }
    version=$("$PY" -c 'import slife2; print(slife2.__version__)' 2>/dev/null || true)
    if [ -n "$version" ]; then
        say "Installed slife2 $version."
    else
        say "Installed slife2."
    fi
    if [ "$MODE" = checkout ]; then
        say "  run:     uv run slife2"
    else
        say "  run:     slife2"
    fi
    say "  data:    $HOME_DIR"
    say "  config:  $HOME_DIR/slife2.yaml"
    print_credentials
}

do_install() {
    say "slife2 installer ($MODE)"
    require_homebrew
    step 1/4 uv
    ensure_uv
    step 2/4 "Python 3.13"
    ensure_python
    step 3/4 slife2
    if [ "$MODE" = checkout ]; then
        ensure_venv
        seed_config
        sync_checkout
    else
        download_source
        install_tool
        seed_config
    fi
    ensure_credstore
    step 4/4 dependencies
    requirements
    install_dependencies
    verify
    print_next
    [ "$FAILED" -eq 0 ]
}

do_dry_run() {
    say "slife2 installer ($MODE, dry run)"
    if [ "$MODE" = checkout ]; then
        say "  would validate .venv, sync dependencies, and install configured programs"
    else
        say "  would install slife2 as a uv tool and install configured programs"
    fi
    exit 0
}

remove_local() {  # tool package
    case "$1" in
        node)
            rm -rf "$HOME/.local/lib/nodejs"
            rm -f "$HOME/.local/bin/node" "$HOME/.local/bin/npm" "$HOME/.local/bin/npx"
            ;;
        mosquitto)
            rm -rf "$HOME/.local/usr"
            rm -f "$HOME/.local/bin/mosquitto" "$HOME/.local/bin/mosquitto_pub" \
                  "$HOME/.local/bin/mosquitto_sub" "$HOME/.local/bin/mosquitto_passwd"
            ;;
        cloudflared) rm -f "$HOME/.local/bin/cloudflared" ;;
        uv) rm -f "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx" "$HOME/.local/bin/uvw" ;;
        *) rm -rf "$2" ;;
    esac
}

do_uninstall() {
    if [ ! -f "$STATE" ] || ! grep -qv -e '^#' -e '^$' "$STATE"; then
        say "Nothing to uninstall."
        exit 0
    fi
    say "Uninstalling slife2 components..."
    new_state=$(mktemp) || die "temporary file failed"
    grep '^#' "$STATE" > "$new_state" 2>/dev/null || true
    code=0
    while IFS=$(printf '\t') read -r manager package tool; do
        case "$manager" in
            local) remove_local "$tool" "$package" ;;
            brew) brew uninstall "$package" >/dev/null 2>&1 || code=1 ;;
            uv-tool) uv tool uninstall "$package" >/dev/null 2>&1 || code=1 ;;
            *) code=1 ;;
        esac
        [ "$code" -eq 0 ] || printf '%s\t%s\t%s\n' "$manager" "$package" "$tool" >> "$new_state"
    done < <(grep -v -e '^#' -e '^$' "$STATE")
    mv "$new_state" "$STATE"
    if [ "$code" -eq 0 ]; then
        say "Uninstalled."
    else
        say "Some components could not be removed; see $STATE."
    fi
    exit "$code"
}

case "$ACTION" in
    install)
        [ "$DRY_RUN" -eq 1 ] && do_dry_run
        do_install
        exit $?
        ;;
    uninstall) do_uninstall ;;
esac
