#!/usr/bin/env bash
#
# slife2 installer for macOS and Linux.
#
# One line, no checkout required (installs slife2 as a uv tool; data and
# configuration live in ~/.slife2):
#
#   curl -fsSL https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.sh | bash
#
# From a checkout (development mode: the checkout's own slife2.yaml, .venv and
# ownership record are used, isolated from ~/.slife2):
#
#   ./scripts/install.sh                 # install missing, upgrade present
#   ./scripts/install.sh --dry-run       # report only; changes nothing
#   ./scripts/install.sh --no-upgrade    # install missing only
#   ./scripts/install.sh uninstall       # remove only what this installer added
#
# The installer is idempotent.  Every external program the effective
# configuration names is a hard dependency: a missing one that this table has no
# package for fails the run.  Only named packages are ever installed, upgraded
# or removed; there is no global upgrade, cleanup or autoremove.
#
# Exit codes: 0 success; 1 a required step failed; 2 usage/environment/config;
# 3 cancelled or no terminal without --yes.

set -u

# A script read from a pipe is saved before anything else runs: `confirm` must
# read the terminal rather than the rest of the script, and a child that reads
# stdin must not consume it.
if [ -z "${BASH_SOURCE[0]:-}" ] || [ "${BASH_SOURCE[0]}" = "/dev/stdin" ] \
    || [ "${BASH_SOURCE[0]#/dev/fd/}" != "${BASH_SOURCE[0]}" ]; then
    saved=$(mktemp "${TMPDIR:-/tmp}/slife2-install.XXXXXX") || exit 1
    cat > "$saved" || exit 1
    tr -d '\r' < "$saved" > "$saved.tmp" && mv "$saved.tmp" "$saved"
    # Put the terminal back on stdin, so `confirm` asks the person rather than
    # reading the now-exhausted pipe.  No terminal (CI) leaves stdin as it was.
    if (exec </dev/tty) 2>/dev/null; then
        SLIFE2_INSTALL_SCRIPT="$saved" exec bash "$saved" "$@" </dev/tty
    fi
    SLIFE2_INSTALL_SCRIPT="$saved" exec bash "$saved" "$@"
fi

WORK_DIR=""
cleanup() {
    [ -n "${SLIFE2_INSTALL_SCRIPT:-}" ] && rm -f "$SLIFE2_INSTALL_SCRIPT" "$SLIFE2_INSTALL_SCRIPT.tmp"
    [ -n "${WORK_DIR:-}" ] && rm -rf "$WORK_DIR"
}
trap cleanup EXIT

MODE=tool
ROOT=""
SOURCE_DIR=""
MANIFEST=""
HELPER=""

is_checkout() {
    [ -f "$1/pyproject.toml" ] && grep -q 'name = "slife2"' "$1/pyproject.toml"
}

SCRIPT_PATH="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
    SCRIPT_DIR=$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
fi

if [ -n "$SCRIPT_DIR" ] && is_checkout "$(cd -- "$SCRIPT_DIR/.." && pwd)"; then
    MODE=checkout
    ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
elif is_checkout "$PWD"; then
    # Piped into a checkout: that checkout is the development environment.
    MODE=checkout
    ROOT=$PWD
fi
if [ "$MODE" = checkout ]; then
    cd "$ROOT" || exit 2
    SOURCE_DIR=$ROOT
    MANIFEST="$ROOT/scripts/dependencies.tsv"
    HELPER="$ROOT/scripts/install_helper.py"
fi

ACTION=install
ACTION_SEEN=""
DRY_RUN=0
NO_UPGRADE=0
YES=0
FAILED=0
PY=""
PY313=""
REQ_FILE=""

say() { printf '%s\n' "$*"; }
step() { printf '\n[%s] %s\n' "$1" "$2"; }
ok() { printf '[OK] %s\n' "$*"; }
skip() { printf '[SKIP] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
fail() { printf '[FAIL] %s\n' "$*" >&2; FAILED=1; }
die() { code=$1; shift; printf '[FAIL] %s\n' "$*" >&2; exit "$code"; }
run() { printf '+ %s\n' "$*"; "$@"; }

usage() {
    cat <<'EOF'
slife2 installer (macOS / Linux)

Usage:
  curl -fsSL https://raw.githubusercontent.com/juzcn/slife2/main/scripts/install.sh | bash
  ./scripts/install.sh [install|uninstall] [options]    # from a checkout

Actions:
  install       default; install missing dependencies and upgrade present ones
  uninstall     remove only packages this installer recorded as installed by it

Options:
  --dry-run     report what would happen; never changes anything
  --no-upgrade  install missing packages only
  --yes, -y     do not ask for confirmation
  --help, -h    this text

Exit codes: 0 success, 1 a required step failed, 2 usage/environment/config,
3 cancelled or no terminal without --yes.
EOF
}

for arg in "$@"; do
    case "$arg" in
        install|uninstall)
            [ -z "$ACTION_SEEN" ] || die 2 "only one action may be named"
            ACTION=$arg
            ACTION_SEEN=$arg
            ;;
        --dry-run) DRY_RUN=1 ;;
        --no-upgrade) NO_UPGRADE=1 ;;
        --yes|-y) YES=1 ;;
        --help|-h) usage; exit 0 ;;
        -*) die 2 "unknown option: $arg" ;;
        *) die 2 "unknown action: $arg (use install or uninstall)" ;;
    esac
done

[ "$DRY_RUN" -eq 1 ] && [ "$ACTION" != install ] && die 2 "--dry-run is only valid with install"
[ "$NO_UPGRADE" -eq 1 ] && [ "$ACTION" != install ] && die 2 "--no-upgrade is only valid with install"

# ---------------------------------------------------------------------------
# platform, home, state

case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin) PLATFORM=macos ;;
    Linux) PLATFORM=linux ;;
    *) die 2 "unsupported operating system: $(uname -s 2>/dev/null || echo unknown)" ;;
esac

[ "$(id -u)" -eq 0 ] && die 2 "run as your normal user; this installer is user-level and never uses sudo"

if [ "$MODE" = checkout ]; then
    if [ -n "${SLIFE2_DATA_DIR:-}" ]; then
        HOME_DIR=$SLIFE2_DATA_DIR
        case "$HOME_DIR" in
            /*) ;;
            *) HOME_DIR="$ROOT/$HOME_DIR" ;;
        esac
    else
        # A checkout is a development environment: its own files are the home,
        # so the development configuration stays out of ~/.slife2.
        HOME_DIR=$ROOT
    fi
else
    # A standalone install is an application: one data directory, under the
    # user's home, with no checkout and no repository to keep.
    HOME_DIR=${SLIFE2_DATA_DIR:-$HOME/.slife2}
    case "$HOME_DIR" in
        /*) ;;
        *) HOME_DIR="$PWD/$HOME_DIR" ;;
    esac
fi
STATE=${SLIFE2_INSTALL_STATE:-$HOME_DIR/.install-state.tsv}

refresh_path() {
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
    if [ "$PLATFORM" = macos ] && ! command -v brew >/dev/null 2>&1; then
        for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
            [ -x "$candidate" ] && eval "$("$candidate" shellenv)" 2>/dev/null
        done
    fi
    if command -v uv >/dev/null 2>&1; then
        bin=$(uv tool dir --bin 2>/dev/null || true)
        [ -n "$bin" ] && case ":$PATH:" in *":$bin:"*) ;; *) PATH="$bin:$PATH" ;; esac
    fi
    export PATH
}

confirm() {
    local answer
    [ "$YES" -eq 1 ] && return 0
    [ -t 0 ] || die 3 "no terminal to confirm; re-run with --yes"
    printf 'Proceed? [y/N] '
    read -r answer || exit 3
    case "$answer" in y|Y|yes|YES) return 0 ;; *) exit 3 ;; esac
}

# ---------------------------------------------------------------------------
# rootless installers — no sudo, no system package manager
#
# Everything lands under the user's own directories: ~/.local for binaries and
# libraries, ~/.slife2 for application data and configuration.  A dependency
# that cannot be installed this way is reported with the manual command rather
# than escalating.

hash_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# A broker already serving the address slife2 uses is all it needs; the
# mosquitto binary is only started when nothing is listening.
broker_listening() {
    (exec 3<>/dev/tcp/127.0.0.1/1883) 2>/dev/null && { exec 3>&-; return 0; }
    return 1
}

require_homebrew() {
    [ "$PLATFORM" = macos ] || return 0
    command -v brew >/dev/null 2>&1 && return 0
    fail "macOS needs Homebrew's python@3.13 (uv's Python cannot load SQLite extensions)."
    say "  Install Homebrew yourself (https://brew.sh), then re-run this installer."
    say "  This installer never uses sudo."
    return 1
}

# The LTS release for this OS/architecture: "<version> <filename> <sha256>".
node_latest() {
    local os arch sums line
    os=$(uname -s | tr '[:upper:]' '[:lower:]')
    case "$os" in linux|darwin) ;; *) return 1 ;; esac
    case "$(uname -m)" in
        x86_64|amd64) arch=x64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) return 1 ;;
    esac
    sums=$(curl -fsSL https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt) || return 1
    line=$(printf '%s\n' "$sums" | grep "node-v[0-9.]*-$os-$arch\.tar\.gz" | head -1)
    [ -n "$line" ] || return 1
    printf '%s %s %s\n' \
        "$(printf '%s' "$line" | sed -n 's/.*node-\(v[0-9.]*\)-.*/\1/p')" \
        "$(printf '%s' "$line" | awk '{print $2}')" \
        "$(printf '%s' "$line" | awk '{print $1}')"
}

install_node_rootless() {  # version filename sha256
    local version=$1 filename=$2 sha=$3 tmp got name
    tmp=$(mktemp -d) || return 1
    say "+ downloading node $version"
    curl -fsSL -o "$tmp/node.tar.gz" "https://nodejs.org/dist/latest-v22.x/$filename" \
        || { rm -rf "$tmp"; return 1; }
    got=$(hash_file "$tmp/node.tar.gz")
    if [ -z "$got" ] || [ "$got" != "$sha" ]; then
        warn "SHA256 mismatch for $filename; not installing it"
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$HOME/.local/lib/nodejs"
    mkdir -p "$HOME/.local/lib/nodejs" "$HOME/.local/bin" || { rm -rf "$tmp"; return 1; }
    tar -xzf "$tmp/node.tar.gz" -C "$HOME/.local/lib/nodejs" --strip-components=1 \
        || { rm -rf "$tmp"; return 1; }
    for name in node npm npx; do
        rm -f "$HOME/.local/bin/$name"
        ln -s "$HOME/.local/lib/nodejs/bin/$name" "$HOME/.local/bin/$name"
    done
    rm -rf "$tmp"
    ok "node $version installed (rootless)"
}

# Debian/Ubuntu: no root needed — `apt-get download` fetches the packages and
# `dpkg -x` unpacks them under ~/.local.  A wrapper supplies LD_LIBRARY_PATH so
# the extracted binary finds the libraries that came with it.
install_mosquitto_rootless() {
    local tmp deb_dir deb deps missing
    command -v apt-get >/dev/null 2>&1 && command -v dpkg >/dev/null 2>&1 || return 1
    tmp=$(mktemp -d) || return 1
    deb_dir="$tmp/debs"
    mkdir -p "$deb_dir"
    # The broker needs the libraries in its dependency closure, not only its
    # own package.  Download what is not already installed system-wide; dpkg -x
    # then unpacks the whole closure under ~/.local.
    deps=$(apt-cache depends --recurse --no-recommends --no-suggests \
        --no-conflicts --no-breaks --no-replaces --no-enhances \
        mosquitto mosquitto-clients 2>/dev/null \
        | sed -n 's/^[ |]*Depends: //p' \
        | sed 's/<//g; s/>//g' | awk '{print $1}' | sort -u)
    missing=""
    for pkg in $deps; do
        dpkg -s "$pkg" >/dev/null 2>&1 || missing="$missing $pkg"
    done
    ( cd "$deb_dir" && apt-get download mosquitto mosquitto-clients $missing ) >/dev/null 2>&1 || true
    ls "$deb_dir"/*.deb >/dev/null 2>&1 || { rm -rf "$tmp"; return 1; }
    for deb in "$deb_dir"/*.deb; do
        dpkg -x "$deb" "$HOME/.local" 2>/dev/null || { rm -rf "$tmp"; return 1; }
    done
    rm -rf "$tmp"
    mkdir -p "$HOME/.local/bin"
    cat > "$HOME/.local/bin/mosquitto" <<'EOF'
#!/bin/sh
root="$HOME/.local"
LD_LIBRARY_PATH="$root/usr/lib:$root/usr/lib/x86_64-linux-gnu:$root/usr/lib/aarch64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH
exec "$root/usr/sbin/mosquitto" "$@"
EOF
    chmod +x "$HOME/.local/bin/mosquitto"
    for name in mosquitto_pub mosquitto_sub mosquitto_passwd; do
        [ -e "$HOME/.local/usr/bin/$name" ] \
            && ln -sf "$HOME/.local/usr/bin/$name" "$HOME/.local/bin/$name"
    done
    "$HOME/.local/bin/mosquitto" -h 2>&1 | grep -q "mosquitto version"
}

# ---------------------------------------------------------------------------
# ownership record

state_header() {
    [ -f "$STATE" ] && return 0
    mkdir -p "$(dirname "$STATE")" || return 1
    printf '# host=%s\n' "$(hostname 2>/dev/null || echo unknown)" > "$STATE"
}

record_owned() {  # manager package tool
    local line
    state_header || { warn "could not write $STATE"; return 1; }
    line=$(printf '%s\t%s\t%s' "$1" "$2" "$3")
    grep -qxF "$line" "$STATE" 2>/dev/null && return 0
    printf '%s\n' "$line" >> "$STATE" || warn "could not write $STATE"
}

# ---------------------------------------------------------------------------
# tools

ensure_uv() {
    step "1/8" "uv"
    refresh_path
    if command -v uv >/dev/null 2>&1; then
        if [ "$NO_UPGRADE" -eq 0 ]; then
            run uv self update || warn "uv self update failed; keeping $(uv --version 2>/dev/null)"
        else
            skip "uv upgrade disabled"
        fi
        ok "uv $(uv --version 2>/dev/null)"
        return 0
    fi
    say "installing uv into ~/.local (the official installer, no sudo)"
    run sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' \
        || { fail "uv could not be installed"; return 1; }
    record_owned local "$HOME/.local/bin/uv" uv
    refresh_path
    command -v uv >/dev/null 2>&1 || { fail "uv is still not on PATH"; return 1; }
    ok "uv $(uv --version 2>/dev/null)"
}

ensure_python() {
    step "2/8" "Python 3.13"
    if [ "$PLATFORM" = macos ]; then
        require_homebrew || return 1
        if brew list --formula python@3.13 >/dev/null 2>&1; then
            if [ "$NO_UPGRADE" -eq 0 ]; then
                run brew upgrade python@3.13 || warn "python@3.13 upgrade failed"
            else
                skip "Python upgrade disabled"
            fi
        else
            run brew install python@3.13 || { fail "python@3.13 could not be installed"; return 1; }
            record_owned brew python@3.13 python
        fi
        PY313="$(brew --prefix python@3.13)/bin/python3.13"
        [ -x "$PY313" ] || { fail "Homebrew python@3.13 not found at $PY313"; return 1; }
    else
        if [ "$NO_UPGRADE" -eq 1 ]; then
            run uv python install 3.13 --default || { fail "Python 3.13 could not be installed"; return 1; }
        else
            run uv python install 3.13 --upgrade --default || { fail "Python 3.13 could not be installed"; return 1; }
        fi
        refresh_path
    fi
    ok "Python 3.13 ready"
}

tool_python() {
    local tool_dir
    tool_dir=$(uv tool dir 2>/dev/null) || return 1
    printf '%s/slife2/bin/python' "$tool_dir"
}

download_source() {
    WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/slife2-source.XXXXXX") || { fail "mktemp failed"; return 1; }
    SOURCE_DIR="$WORK_DIR/source"
    mkdir -p "$SOURCE_DIR"
    archive="$WORK_DIR/slife2.tar.gz"
    say "downloading the latest slife2"
    if ! curl -fsSL -o "$archive" "https://github.com/juzcn/slife2/archive/refs/heads/main.tar.gz"; then
        warn "GitHub unreachable; trying the gitee mirror"
        curl -fsSL -o "$archive" "https://gitee.com/juzcn/slife2/repository/archive/main.tar.gz" \
            || { fail "could not download slife2 from GitHub or gitee"; return 1; }
    fi
    tar -xzf "$archive" -C "$SOURCE_DIR" --strip-components=1 \
        || { fail "could not extract the downloaded source"; return 1; }
    MANIFEST="$SOURCE_DIR/scripts/dependencies.tsv"
    HELPER="$SOURCE_DIR/scripts/install_helper.py"
    if [ ! -f "$MANIFEST" ] || [ ! -f "$HELPER" ]; then
        fail "the downloaded source has no scripts/ directory"
        return 1
    fi
    ok "source: $SOURCE_DIR"
}

install_tool() {
    step "3/8" "installing slife2"
    local was_installed=0
    uv tool list 2>/dev/null | grep -q '^slife2 ' && was_installed=1
    if [ "$PLATFORM" = macos ]; then
        run uv tool install --python "$PY313" --force "$SOURCE_DIR" \
            || { fail "slife2 could not be installed"; return 1; }
    else
        run uv tool install --python 3.13 --force "$SOURCE_DIR" \
            || { fail "slife2 could not be installed"; return 1; }
    fi
    [ "$was_installed" -eq 0 ] && record_owned uv-tool slife2 slife2
    # Make the tool bin directory visible to new shells, not only this one.
    uv tool update-shell >/dev/null 2>&1 || true
    refresh_path
    PY=$(tool_python) || { fail "could not locate the slife2 tool environment"; return 1; }
    [ -x "$PY" ] || { fail "the slife2 tool environment has no interpreter at $PY"; return 1; }
    command -v slife2 >/dev/null 2>&1 || { fail "slife2 is installed but is not on PATH"; return 1; }
    ok "slife2 installed (uv tool)"
}

ensure_venv() {
    step "3/8" "project virtualenv"
    PY="$ROOT/.venv/bin/python"
    if [ ! -x "$PY" ]; then
        if [ "$PLATFORM" = macos ]; then
            run uv venv --python "$PY313" || { fail "the virtualenv could not be created"; return 1; }
        else
            run uv venv --python 3.13 || { fail "the virtualenv could not be created"; return 1; }
        fi
    fi
    [ -x "$PY" ] || { fail "the virtualenv has no interpreter at $PY"; return 1; }

    if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' >/dev/null 2>&1; then
        cat >&2 <<EOF
[FAIL] $ROOT/.venv exists but is not Python 3.13.
Refusing to recreate it, because that would delete packages you installed.
Rebuild it yourself:
  uv pip freeze > venv-packages.txt
  rm -rf .venv
  uv venv --python 3.13          # macOS: uv venv --python "\$(brew --prefix python@3.13)/bin/python3.13"
  uv sync --upgrade --inexact
  # reinstall anything you added from venv-packages.txt
EOF
        return 2
    fi
    if ! "$PY" -c 'import sqlite3; c = sqlite3.connect(":memory:"); c.enable_load_extension(True)' >/dev/null 2>&1; then
        cat >&2 <<EOF
[FAIL] $ROOT/.venv cannot load SQLite extensions.
Refusing to recreate it, because that would delete packages you installed.
Rebuild it yourself with a Python that supports them (see README, "Requirements").
EOF
        return 2
    fi
    ok "venv: $PY"
}

seed_config() {
    step "4/8" "configuration"
    "$PY" "$HELPER" seed --source "$SOURCE_DIR/slife2.yaml" --home "$HOME_DIR" || return 1
}

sync_project() {
    step "5/8" "Python dependencies"
    before=$(hash_file "$ROOT/uv.lock")
    if [ "$NO_UPGRADE" -eq 1 ]; then
        run uv sync --locked --inexact || { fail "uv sync failed"; return 1; }
    else
        run uv sync --upgrade --inexact || { fail "uv sync failed"; return 1; }
    fi
    after=$(hash_file "$ROOT/uv.lock")
    [ -n "$before" ] && [ "$before" != "$after" ] && warn "uv.lock changed; review and commit it yourself"
    ok "Python dependencies synced"
}

derive_requirements() {
    step "6/8" "reading the effective configuration"
    REQ_FILE=$(mktemp "${TMPDIR:-/tmp}/slife2-requirements.XXXXXX") || die 2 "mktemp failed"
    "$PY" "$HELPER" require --home "$HOME_DIR" > "$REQ_FILE"
    code=$?
    if [ "$code" -ne 0 ]; then
        rm -f "$REQ_FILE"
        REQ_FILE=""
        return "$code"
    fi
    ok "configuration names $(wc -l < "$REQ_FILE" | tr -d ' ') program(s)"
}

check_node_major() {
    local major
    major=$(node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1)
    case "$major" in
        ''|*[!0-9]*) fail "could not read the Node version"; return 1 ;;
    esac
    [ "$major" -ge 20 ] || { fail "Node $major is too old; upgrade to Node 20 or newer yourself"; return 1; }
}

ensure_provided() {  # exe provenance
    local exe=$1 provenance=$2
    if command -v "$exe" >/dev/null 2>&1; then
        ok "$exe present"
    else
        fail "$exe is provided by another installed tool but is not on PATH (named by $provenance)"
    fi
}

ensure_uv_tool() {  # exe provenance [python]
    local exe=$1 provenance=$2 python=${3:-3.13}
    if command -v "$exe" >/dev/null 2>&1; then
        if uv tool list 2>/dev/null | grep -q "^$exe "; then
            if [ "$NO_UPGRADE" -eq 1 ]; then
                skip "$exe upgrade disabled"
            else
                run uv tool upgrade "$exe" || warn "$exe upgrade failed"
            fi
        else
            ok "$exe present (unmanaged)"
        fi
        return 0
    fi
    run uv tool install --python "$python" "$exe" || {
        fail "$exe could not be installed (named by $provenance)"
        return 1
    }
    uv tool update-shell >/dev/null 2>&1 || true
    refresh_path
    command -v "$exe" >/dev/null 2>&1 || { fail "$exe installed but is not on PATH"; return 1; }
    record_owned uv-tool "$exe" "$exe"
    ok "$exe installed (uv tool)"
}

ensure_node() {  # provenance
    local provenance=$1 latest version filename sha
    if command -v node >/dev/null 2>&1; then
        check_node_major || return 1
        if [ -d "$HOME/.local/lib/nodejs" ]; then
            latest=$(node_latest) || { ok "node $(node --version) present; update check unavailable"; return 0; }
            version=$(printf '%s' "$latest" | awk '{print $1}')
            if [ "$(node --version 2>/dev/null)" = "$version" ]; then
                ok "node $version is current (rootless)"
                return 0
            fi
            [ "$NO_UPGRADE" -eq 1 ] && { skip "node upgrade disabled"; return 0; }
            filename=$(printf '%s' "$latest" | awk '{print $2}')
            sha=$(printf '%s' "$latest" | awk '{print $3}')
            install_node_rootless "$version" "$filename" "$sha" || { fail "node upgrade failed (named by $provenance)"; return 1; }
            refresh_path
            return 0
        fi
        ok "node present (unmanaged; not upgraded)"
        return 0
    fi
    latest=$(node_latest) || { fail "could not resolve the Node LTS download (named by $provenance)"; return 1; }
    version=$(printf '%s' "$latest" | awk '{print $1}')
    filename=$(printf '%s' "$latest" | awk '{print $2}')
    sha=$(printf '%s' "$latest" | awk '{print $3}')
    install_node_rootless "$version" "$filename" "$sha" \
        || { fail "node could not be installed (named by $provenance)"; return 1; }
    record_owned local "$HOME/.local/lib/nodejs" node
    refresh_path
    command -v npx >/dev/null 2>&1 || { fail "node installed but npx is not on PATH"; return 1; }
}

ensure_cloudflared() {  # provenance
    local provenance=$1 latest current arch url
    if [ "$PLATFORM" = macos ]; then
        require_homebrew || return 1
        if brew list --formula cloudflared >/dev/null 2>&1; then
            if [ "$NO_UPGRADE" -eq 1 ]; then
                skip "cloudflared upgrade disabled"
            else
                run brew upgrade cloudflared || warn "cloudflared upgrade failed"
            fi
        else
            run brew install cloudflared || { fail "cloudflared could not be installed"; return 1; }
            record_owned brew cloudflared cloudflared
        fi
        return 0
    fi
    if command -v cloudflared >/dev/null 2>&1 && [ ! -e "$HOME/.local/bin/cloudflared" ]; then
        ok "cloudflared present (unmanaged; not upgraded)"
        return 0
    fi
    current=$(cloudflared --version 2>/dev/null || true)
    latest=$(curl -fsSI https://github.com/cloudflare/cloudflared/releases/latest 2>/dev/null \
        | sed -n 's#^[Ll]ocation:.*/tag/##p' | tr -d '\r')
    if [ -n "$latest" ] && [ -n "$current" ] && printf '%s' "$current" | grep -q "$latest"; then
        ok "cloudflared $latest is current"
        return 0
    fi
    [ "$NO_UPGRADE" -eq 1 ] && [ -n "$current" ] && { skip "cloudflared upgrade disabled"; return 0; }
    case "$(uname -m)" in
        x86_64|amd64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) fail "no cloudflared build for $(uname -m)"; return 1 ;;
    esac
    url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
    mkdir -p "$HOME/.local/bin"
    say "+ downloading cloudflared"
    curl -fsSL -o "$HOME/.local/bin/cloudflared" "$url" \
        || { fail "cloudflared could not be downloaded (named by $provenance)"; return 1; }
    chmod +x "$HOME/.local/bin/cloudflared"
    refresh_path
    command -v cloudflared >/dev/null 2>&1 || { fail "cloudflared is not on PATH"; return 1; }
    [ -n "$current" ] || record_owned local "$HOME/.local/bin/cloudflared" cloudflared
    ok "cloudflared installed (rootless)"
}

ensure_mosquitto() {  # provenance
    local provenance=$1
    if command -v mosquitto >/dev/null 2>&1; then
        if [ -e "$HOME/.local/bin/mosquitto" ]; then
            if [ "$NO_UPGRADE" -eq 1 ]; then
                skip "mosquitto upgrade disabled"
            elif install_mosquitto_rootless; then
                ok "mosquitto upgraded (rootless)"
            else
                warn "mosquitto upgrade failed; keeping the current binary"
            fi
        elif [ "$PLATFORM" = macos ] && brew list --formula mosquitto >/dev/null 2>&1; then
            if [ "$NO_UPGRADE" -eq 1 ]; then
                skip "mosquitto upgrade disabled"
            else
                run brew upgrade mosquitto || warn "mosquitto upgrade failed"
            fi
        else
            ok "mosquitto present (unmanaged; not upgraded)"
        fi
        return 0
    fi
    if broker_listening; then
        ok "an MQTT broker is already listening on 127.0.0.1:1883; mosquitto is not needed"
        return 0
    fi
    if [ "$PLATFORM" = macos ]; then
        require_homebrew || return 1
        run brew install mosquitto || { fail "mosquitto could not be installed"; return 1; }
        record_owned brew mosquitto mosquitto
        return 0
    fi
    if install_mosquitto_rootless; then
        record_owned local "$HOME/.local/usr" mosquitto
        ok "mosquitto installed (rootless, Debian/Ubuntu packages)"
        return 0
    fi
    fail "mosquitto is missing and this installer never uses sudo (named by $provenance)."
    say "  Install it yourself, or start an MQTT broker on 127.0.0.1:1883, then re-run."
    return 1
}

install_requirements() {
    step "7/8" "external programs"
    local seen exe provenance
    # uv and Python were ensured before the requirements were read.
    seen=" uv python "
    while IFS=$(printf '\t') read -r exe provenance; do
        [ -n "$exe" ] || continue
        case " $seen " in *" $exe "*) continue ;; esac
        seen="$seen$exe "
        case "$exe" in
            uv|python|python3|uvx)
                if command -v "$exe" >/dev/null 2>&1; then
                    ok "$exe present"
                else
                    fail "$exe is not on PATH (named by $provenance)"
                fi
                ;;
            mosquitto) ensure_mosquitto "$provenance" ;;
            cloudflared) ensure_cloudflared "$provenance" ;;
            npx|node) ensure_node "$provenance" ;;
            ssh)
                if command -v ssh >/dev/null 2>&1; then
                    ok "ssh present"
                else
                    fail "ssh is missing (named by $provenance); install an OpenSSH client yourself"
                fi
                ;;
            yt-dlp) ensure_uv_tool yt-dlp "$provenance" 3.13 ;;
            browser-harness) ensure_uv_tool browser-harness "$provenance" 3.12 ;;
            *)
                if command -v "$exe" >/dev/null 2>&1; then
                    ok "$exe present (unmanaged; named by $provenance)"
                else
                    fail "$exe is missing and has no rootless installer (named by $provenance)"
                fi
                ;;
        esac
    done < "$REQ_FILE"
}

version_probe() {
    local out
    if command -v "$1" >/dev/null 2>&1; then
        out=$("$@" 2>&1 | head -n 1)
        [ -n "$out" ] && printf '       %-16s %s\n' "$1" "$out"
    fi
}

verify() {
    step "8/8" "verification"
    while IFS=$(printf '\t') read -r exe provenance; do
        [ -n "$exe" ] || continue
        if command -v "$exe" >/dev/null 2>&1; then
            ok "$exe -> $(command -v "$exe")"
        elif [ "$exe" = mosquitto ] && broker_listening; then
            ok "mosquitto not on PATH; an MQTT broker is already listening on 127.0.0.1:1883"
        else
            fail "$exe is still missing (named by $provenance)"
        fi
    done < "$REQ_FILE"

    uv --version >/dev/null 2>&1 || fail "uv does not run"
    uvx --version >/dev/null 2>&1 || fail "uvx does not run"
    python3 --version >/dev/null 2>&1 || fail "python3 does not run (a Store alias is not an interpreter)"
    "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' \
        || fail "the virtualenv is not Python 3.13"
    "$PY" -c 'import sqlite3, sqlite_vec; c = sqlite3.connect(":memory:"); c.enable_load_extension(True); sqlite_vec.load(c)' \
        || fail "sqlite-vec cannot be loaded by this Python"
    "$PY" -c 'import slife2; print("slife2 " + slife2.__version__)' \
        || fail "slife2 does not import"
    if [ "$MODE" = checkout ]; then
        (cd "$ROOT" && uv run --no-sync slife2 --help >/dev/null 2>&1) \
            || fail "uv run slife2 does not run"
    else
        slife2 --help >/dev/null 2>&1 || fail "slife2 does not run"
    fi

    version_probe mosquitto -h
    version_probe cloudflared --version
    version_probe node --version
    version_probe npx --version
    version_probe ssh -V
    version_probe yt-dlp --version
    version_probe browser-harness --version
}

print_summary() {
    say ""
    if [ "$FAILED" -eq 0 ]; then
        say "summary: all required programs are present."
    else
        say "summary: some required programs are missing or failed; see [FAIL] above."
    fi
    say "  mode:  $MODE"
    say "  home:  $HOME_DIR"
    say "  state: $STATE"
    if [ "$MODE" = checkout ]; then
        say "  next:  uv run credstore set <PROVIDER>_API_KEY"
        say "         uv run slife2"
    else
        say "  next:  export <PROVIDER>_API_KEY=...   (or store it with credstore)"
        say "         slife2"
    fi
}

do_install() {
    say "slife2 installer"
    say "  platform: $PLATFORM"
    say "  method:   $MODE"
    say "  home:     $HOME_DIR"
    say "  state:    $STATE"
    confirm

    require_homebrew || return 1
    ensure_uv || return 1
    ensure_python || return 1
    if [ "$MODE" = checkout ]; then
        ensure_venv || return $?
        seed_config || return 1
        sync_project || return 1
    else
        download_source || return 1
        install_tool || return 1
        seed_config || return 1
    fi
    derive_requirements || return $?
    install_requirements
    verify
    print_summary
    [ "$FAILED" -eq 0 ]
}

do_dry_run() {
    say "slife2 installer --dry-run"
    say "  platform: $PLATFORM"
    say "  method:   $MODE"
    say "  home:     $HOME_DIR"
    say "  state:    $STATE"
    say ""
    if [ "$MODE" = checkout ]; then
        say "[PLAN] external programs this installer manages:"
        while IFS=$(printf '\t') read -r exe tool kind; do
            if command -v "$exe" >/dev/null 2>&1; then
                printf '  %-18s present   %s\n' "$exe" "$(command -v "$exe")"
            else
                printf '  %-18s missing   (%s; installs via %s)\n' "$exe" "$kind" "$tool"
            fi
        done < <(awk -F '\t' '!/^#/ {print $1 "\t" $2 "\t" $3}' "$MANIFEST")
    else
        say "[PLAN] would download the latest main from"
        say "       https://github.com/juzcn/slife2/archive/refs/heads/main.tar.gz"
        say "       and install it with uv tool install --python 3.13 --force."
        say "       The dependency table is read from the downloaded source."
    fi

    live="$HOME_DIR/slife2.yaml"
    say ""
    if [ "$MODE" = checkout ]; then
        say "[PLAN] config: checkout detected; $live is the live configuration; nothing to seed."
    elif [ -f "$live" ]; then
        say "[PLAN] config: $live exists; would write the shipped reference to $HOME_DIR/slife2-default.yaml."
    else
        say "[PLAN] config: would create $live from the shipped configuration."
    fi
    if [ "$MODE" = checkout ]; then
        say "[PLAN] install would then: ensure uv and Python 3.13, validate (never clear) .venv,"
        say "       seed the config, run uv sync --upgrade --inexact, install every program"
        say "       named by the effective configuration, and verify."
    else
        say "[PLAN] install would then: ensure uv and Python 3.13, install slife2 as a uv tool,"
        say "       seed ~/.slife2, install every program named by the effective configuration,"
        say "       and verify."
    fi
    exit 0
}

remove_one() {  # manager package tool
    local manager=$1 package=$2 tool=$3 pkgs
    pkgs=$(printf '%s' "$package" | tr ',' ' ')
    case "$manager" in
        brew) run brew uninstall $pkgs ;;
        local)
            case "$tool" in
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
                *) rm -rf "$package" ;;
            esac
            ok "removed $tool from ~/.local"
            ;;
        uv-tool) run uv tool uninstall "$package" ;;
        uv-python)
            skip "uv-managed Python is left in place; remove it with: uv python uninstall 3.13"
            ;;
        *)
            warn "unknown ownership manager '$manager' for $tool"
            return 1
            ;;
    esac
}

do_uninstall() {
    if [ ! -f "$STATE" ]; then
        say "no installer-owned packages recorded at $STATE; nothing to remove."
        exit 0
    fi
    if ! grep -qv -e '^#' -e '^$' "$STATE"; then
        say "no installer-owned packages recorded at $STATE; nothing to remove."
        exit 0
    fi

    say "WARNING: system packages may be shared with other software on this machine."
    say "Only packages recorded as installed by this installer will be removed."
    say "Data, configuration, skills, agents, runtime, .venv and uv-managed Python"
    say "are left untouched."
    say ""
    grep -v -e '^#' -e '^$' "$STATE" | while IFS=$(printf '\t') read -r manager package tool; do
        say "  [REMOVE] $tool ($manager $package)"
    done
    confirm

    new_state=$(mktemp "$(dirname "$STATE")/.install-state.XXXXXX") || die 2 "mktemp failed"
    grep '^#' "$STATE" > "$new_state" 2>/dev/null || true
    code=0
    while IFS=$(printf '\t') read -r manager package tool; do
        if remove_one "$manager" "$package" "$tool"; then
            ok "$tool removed"
        else
            fail "$tool could not be removed"
            printf '%s\t%s\t%s\n' "$manager" "$package" "$tool" >> "$new_state"
            code=1
        fi
    done < <(grep -v -e '^#' -e '^$' "$STATE")
    mv "$new_state" "$STATE"
    say ""
    say "uninstall finished. The ownership record is $STATE."
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
