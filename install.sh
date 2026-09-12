#!/usr/bin/env bash
# =============================================================================
# AiHomeCloud — Universal Installer
# Supports: Ubuntu, Debian, Armbian (aarch64, armv7l, x86_64) — any glibc 2.17+
# Linux. No hard OS-version gate: Python 3.12 is sourced via a 3-tier fallback
# (system apt -> prebuilt download -> build from source), not guessed from the
# distro version number. See provision_python().
#
# Usage:
#   curl -sSL https://install.aihomecloud.app | sudo bash
#   # or
#   sudo bash install.sh [--keep-desktop] [--enable-firewall]
#
#   --keep-desktop     Skip removing Chromium/X.Org/SDDM (default: removed on
#                       headless boards; auto-skipped anyway if a real
#                       graphical session is detected).
#   --enable-firewall  Opt-in: actually enable the LAN-scoped ufw firewall
#                       this script prepares and validates. Defaults OFF —
#                       found live 2026-07-14 that ufw has multiple real,
#                       not-fully-predictable incompatibilities with at least
#                       one board's kernel (see configure_firewall()'s own
#                       comments), including a real lockout incident. Rules
#                       are still written and validated either way; only the
#                       actual `ufw enable` is gated.
#
# This script is IDEMPOTENT — safe to run multiple times.
# It will not overwrite existing config, secrets, or user data.
# =============================================================================

set -euo pipefail

VERSION="1.0.0"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${GREEN}[AiHomeCloud]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

# Phase-marker status file: lets a caller driving this script over a connection that may drop
# (e.g. the app's installer wizard, polling over SSH) read "which step are we on" without holding
# one long-lived channel open for the whole install.
PHASE_STATUS_FILE="/tmp/ahc_install_status"
write_phase() { echo "PHASE=$1" > "$PHASE_STATUS_FILE"; }

# Marks a terminal failure in the status file, not just stderr — a caller polling only the phase
# file (the app's installer wizard) has no other way to distinguish "script died 10 seconds in"
# from "still working" other than the phase never advancing again, which looks identical to a
# slow-but-healthy step. Single-line, newline-stripped, truncated: the status file format is one
# PHASE=... line per read.
die() {
    local msg="$*"
    echo -e "${RED}[ERROR]${NC} $msg" >&2
    write_phase "failed:$(echo "$msg" | tr '\n' ' ' | cut -c1-200)"
    cleanup_on_error
    exit 1
}

# ── Configurable paths ───────────────────────────────────────────────────────
APP_USER="${APP_USER:-aihomecloud}"
APP_HOME="/opt/aihomecloud"
BACKEND_SRC="$APP_HOME/backend"
VENV_DIR="$BACKEND_SRC/.venv"
DATA_DIR="/var/lib/aihomecloud"
NAS_ROOT="/srv/nas"
BACKUP_ROOT="/mnt/ahc_backup"  # local_backup.py's secondary-drive mountpoint
SERVICE_NAME="aihomecloud"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
MIN_PYTHON_VER="3.12"
# Pinned python-build-standalone release used for the prebuilt-download tier —
# verified live against this exact board (Debian 11 bullseye, aarch64, glibc
# 2.31) on 2026-07-14: downloads, extracts, and imports ssl/sqlite3/zlib/ctypes
# cleanly. Bump both together when refreshing.
PYTHON_STANDALONE_RELEASE="20260623"
PYTHON_STANDALONE_VERSION="3.12.13"
# Official CPython source, used only by the last-resort build-from-source tier.
PYTHON_SOURCE_VERSION="3.12.13"
PYTHON_PREFIX="/opt/aihomecloud/python312"
MIN_DISK_MB=500
PORT=8443
AVAHI_SVC="/etc/avahi/services/aihomecloud.service"
SUDOERS_FILE="/etc/sudoers.d/aihomecloud"

# --keep-desktop: skip strip_desktop_stack() entirely, for anyone who
# genuinely runs this board with a monitor/GUI attached rather than headless.
#
# --enable-firewall: configure_firewall() always PREPARES and VALIDATES the
# LAN-scoped ufw rules (safe, inert), but only actually flips ufw on if this
# flag is passed. Found live 2026-07-14 (a real lockout incident recovered
# via physical console access, on the actual Cubie A5E): this board's
# iptables-nft backend has multiple, not-fully-predictable incompatibilities
# with ufw's default templates (a logging rule using an unsupported
# --log-prefix option, a separate IPv6 `rt`-match issue baked into its base
# template, and — most concerning — ufw ending up "active" with zero loaded
# rules after a config-only command like `ufw logging off`, not just an
# explicit `ufw enable`). The --test pre-flight validation below only catches
# the first class of failure; opt-in avoids defaulting every future install
# into a tool with a demonstrated silent-lockout failure mode on at least one
# real board in the fleet until it's better understood.
KEEP_DESKTOP=false
ENABLE_FIREWALL=false
for _arg in "$@"; do
    [[ "$_arg" == "--keep-desktop" ]] && KEEP_DESKTOP=true
    [[ "$_arg" == "--enable-firewall" ]] && ENABLE_FIREWALL=true
done

# ── Auto-detect existing deployment ──────────────────────────────────────────
# If a service file already exists (re-run scenario), extract the actual user,
# backend path, and venv path from it so the installer doesn't fight with the
# running setup.
if [[ -f "$SERVICE_DST" ]]; then
    _svc_user=$(grep '^User=' "$SERVICE_DST" 2>/dev/null | cut -d= -f2 | tr -d ' ')
    _svc_exec=$(grep '^ExecStart=' "$SERVICE_DST" 2>/dev/null | sed 's/^ExecStart=//' | awk '{print $1}')
    if [[ -n "$_svc_user" ]]; then
        APP_USER="$_svc_user"
        APP_HOME=$(eval echo "~$APP_USER" 2>/dev/null || echo "/home/$APP_USER")
    fi
    if [[ -n "$_svc_exec" ]]; then
        # ExecStart is the python binary; walk up to find the venv root
        # e.g. /home/paras/AiHomeCloud/backend/.venv/bin/python -> .venv dir
        _venv_candidate="${_svc_exec%/bin/python*}"
        if [[ -f "$_venv_candidate/bin/activate" ]]; then
            VENV_DIR="$_venv_candidate"
            BACKEND_SRC="$(dirname "$VENV_DIR")"
        fi
    fi
fi

INSTALL_LOG="/tmp/aihomecloud-install-$(date +%Y%m%d%H%M%S).log"

# Track what we've created so we can clean up on failure
_CREATED_DIRS=()
_CREATED_FILES=()
# Set by _build_python_from_source if it creates a temporary swapfile for the
# compile (low-RAM boards only) — cleared once removed, on both success and
# failure paths.
_SWAP_FILE=""

cleanup_on_error() {
    if [[ ${#_CREATED_FILES[@]} -gt 0 || ${#_CREATED_DIRS[@]} -gt 0 ]]; then
        warn "Installation failed. Cleaning up partial install..."
        for f in "${_CREATED_FILES[@]}"; do
            [[ -f "$f" ]] && rm -f "$f" && warn "  Removed: $f"
        done
        for d in "${_CREATED_DIRS[@]}"; do
            [[ -d "$d" ]] && rmdir --ignore-fail-on-non-empty "$d" 2>/dev/null && warn "  Removed: $d"
        done
    fi
    if [[ -n "$_SWAP_FILE" && -f "$_SWAP_FILE" ]]; then
        swapoff "$_SWAP_FILE" 2>/dev/null || true
        rm -f "$_SWAP_FILE"
        warn "  Removed temporary build swap: $_SWAP_FILE"
        _SWAP_FILE=""
    fi
}

# =============================================================================
# PRE-FLIGHT CHECKS
# =============================================================================

preflight() {
    log "=== AiHomeCloud Installer v${VERSION} ==="
    log "Running pre-flight checks..."

    # 1. Root / sudo access
    if [[ $EUID -ne 0 ]]; then
        die "This script must be run as root. Use: sudo bash install.sh"
    fi

    # 2. Architecture detection
    ARCH="$(uname -m)"
    case "$ARCH" in
        aarch64|arm64)  ARCH="aarch64" ;;
        armv7l|armhf)   ARCH="armv7l" ;;
        x86_64|amd64)   ARCH="x86_64" ;;
        *)              die "Unsupported architecture: $ARCH. Supported: aarch64, armv7l, x86_64" ;;
    esac
    log "  Architecture: $ARCH"

    # 3. OS detection
    if [[ ! -f /etc/os-release ]]; then
        die "Cannot detect OS — /etc/os-release not found."
    fi
    # shellcheck source=/dev/null
    source /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_VERSION="${VERSION_ID:-0}"
    OS_NAME="${PRETTY_NAME:-$OS_ID $OS_VERSION}"
    log "  OS: $OS_NAME"

    # No hard version gate here: the real constraint was always "can we get
    # Python 3.12", which provision_python() now resolves itself via its
    # prebuilt/source-build fallback tiers instead of this script guessing it
    # from the OS release number (a guess that was already wrong for Ubuntu
    # 22.04, which ships Python 3.10, not 3.12).
    case "$OS_ID" in
        ubuntu|debian|armbian)
            log "  ${OS_NAME} — proceeding."
            ;;
        *)
            warn "Untested OS: $OS_NAME — proceeding with caution."
            ;;
    esac

    # 4. Disk space check
    local avail_mb
    avail_mb=$(df -m /opt 2>/dev/null | awk 'NR==2{print $4}' || echo "0")
    if [[ "$avail_mb" -lt "$MIN_DISK_MB" ]]; then
        die "Insufficient disk space on /opt: ${avail_mb}MB available, ${MIN_DISK_MB}MB required."
    fi
    log "  Disk space: ${avail_mb}MB available"

    # 5. Internet connectivity
    if ! ping -c1 -W3 8.8.8.8 &>/dev/null && ! ping -c1 -W3 1.1.1.1 &>/dev/null; then
        die "No internet connectivity. Check your network connection."
    fi
    log "  Internet: OK"

    # 6. Port conflict check — stop our own service and kill any orphan process
    if ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
        # Stop the systemd service (covers active/restarting/activating)
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        log "  Port ${PORT}: stopped $SERVICE_NAME..."
        sleep 1
        # Kill any lingering process still holding the port (e.g. orphan from previous run)
        # ss output: "users:(("python",pid=12345,fd=19))" — extract all PIDs and kill them
        local pids
        pids=$(ss -tlnp 2>/dev/null | grep ":${PORT} " \
               | grep -oP 'pid=\K[0-9]+' || true)
        if [[ -n "$pids" ]]; then
            log "  Port ${PORT}: killing orphan process(es): $pids"
            kill -TERM $pids 2>/dev/null || true
            sleep 2
            kill -KILL $pids 2>/dev/null || true
        fi
        # Final check — wait up to 3 more seconds
        for _i in 1 2 3; do
            ss -tlnp 2>/dev/null | grep -q ":${PORT} " || break
            sleep 1
        done
        if ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
            local leftover
            leftover=$(ss -tlnp | grep ":${PORT} " | awk '{print $6}' | head -1)
            die "Port ${PORT} still occupied by: $leftover. Kill it manually and retry."
        fi
    fi
    log "  Port ${PORT}: available"

    log "Pre-flight checks passed!"
    echo ""
}

# =============================================================================
# INSTALLATION STEPS
# =============================================================================

install_packages() {
    log "[1/10] Installing system packages..."
    apt-get update -qq

    local pkgs=(
        python3 python3-venv python3-pip
        openssl curl git lsof
        avahi-daemon
        samba nfs-kernel-server
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin poppler-utils
        minidlna
        smartmontools
        rsync
        cmake g++ libssl-dev zlib1g-dev gperf
        # Mount support for a drive brought in with existing data (storage
        # "use as-is" path) -- the mount kernel drivers may already be
        # built in on a given board's kernel, but these guarantee it works
        # regardless. ext4 needs nothing extra (native since forever).
        ntfs-3g exfatprogs
        # gdisk provides sgdisk, which ahc-partition-format-nas.sh calls to wipe and re-partition
        # a fresh drive. The helper and its unit were deployed without it, so "Activate a fresh
        # drive" died with exit 127 (`sgdisk: not found`) on any board where gdisk happened not to
        # be present -- two of the three here. e2fsprogs supplies mkfs.ext4 for the same path;
        # present on every Debian/Ubuntu image so far, listed so the dependency is explicit rather
        # than assumed. (Found 2026-08-09 while verifying the disk-guard work.)
        gdisk e2fsprogs
        # _THUMB_FFMPEG (file_routes.py) hard-codes /usr/bin/ffmpeg to grab a video's thumbnail
        # frame, both on-demand and via _pregenerate_video_thumbnail right after upload. Never
        # listed here, so every video thumbnail silently failed (best-effort, caught and logged,
        # never surfaced) on a stock install -- the Gallery grid showed a bare play-icon
        # placeholder for every video, indefinitely, not just until first view.
        ffmpeg
    )

    local to_install=()
    for pkg in "${pkgs[@]}"; do
        dpkg -s "$pkg" &>/dev/null || to_install+=("$pkg")
    done

    if [[ ${#to_install[@]} -gt 0 ]]; then
        log "  Installing: ${to_install[*]}"
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${to_install[@]}"
    else
        log "  All packages already installed."
    fi
}

_python_version_ok() {
    # $1 = path to a python3 binary. Returns 0 if its version >= MIN_PYTHON_VER.
    local bin="$1" ver
    [[ -x "$bin" ]] || return 1
    ver="$("$bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)" || return 1
    [[ "$(printf '%s\n' "$MIN_PYTHON_VER" "$ver" | sort -V | head -1)" == "$MIN_PYTHON_VER" ]]
}

_python_smoke_test() {
    # $1 = path to a python3 binary. Confirms the modules the backend actually
    # needs import cleanly — catches a truncated download or a bad build
    # before it surfaces later as a confusing pip/venv failure.
    "$1" -c 'import ssl, sqlite3, zlib, ctypes' &>/dev/null
}

_arch_to_pbs_target() {
    # Maps this script's normalized $ARCH to a python-build-standalone release
    # artifact triple. Empty output = no prebuilt available for this arch.
    case "$ARCH" in
        aarch64)  echo "aarch64-unknown-linux-gnu" ;;
        armv7l)   echo "armv7-unknown-linux-gnueabihf" ;;
        x86_64)   echo "x86_64-unknown-linux-gnu" ;;
        *)        echo "" ;;
    esac
}

_try_download_prebuilt_python() {
    # Tier 2: a prebuilt, portable CPython from astral-sh/python-build-standalone
    # (the same project `uv` uses under the hood) — verified live against this
    # exact board (Debian 11, aarch64, glibc 2.31) before wiring this in.
    # Returns 0 on success (PYTHON_PREFIX populated with a working install),
    # 1 on any failure (caller falls back to building from source).
    local target
    target="$(_arch_to_pbs_target)"
    if [[ -z "$target" ]]; then
        log "  No prebuilt Python target for arch '$ARCH' — will build from source."
        return 1
    fi

    local url tmp_path
    url="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_STANDALONE_RELEASE}/cpython-${PYTHON_STANDALONE_VERSION}+${PYTHON_STANDALONE_RELEASE}-${target}-install_only_stripped.tar.gz"
    tmp_path="/tmp/ahc_python_standalone.tar.gz"  # nosec B108

    log "  Downloading prebuilt Python ${PYTHON_STANDALONE_VERSION} (${target})..."
    if ! curl -fsSL --max-time 180 --retry 1 -o "$tmp_path" "$url"; then
        warn "  Prebuilt Python download failed (no release for this target, or network issue)."
        rm -f "$tmp_path"
        return 1
    fi

    # Sanity check: must be a real gzip archive, not an HTML error page saved
    # under a 200 (shouldn't happen with -f, but cheap to confirm).
    if ! file "$tmp_path" | grep -q "gzip compressed"; then
        warn "  Downloaded file isn't a valid archive — discarding."
        rm -f "$tmp_path"
        return 1
    fi

    rm -rf "$PYTHON_PREFIX"
    mkdir -p "$PYTHON_PREFIX"
    if ! tar xzf "$tmp_path" -C "$PYTHON_PREFIX" --strip-components=1; then
        warn "  Failed to extract prebuilt Python archive."
        rm -f "$tmp_path"
        rm -rf "$PYTHON_PREFIX"
        return 1
    fi
    rm -f "$tmp_path"

    if ! _python_smoke_test "$PYTHON_PREFIX/bin/python3"; then
        warn "  Prebuilt Python failed the import smoke test — discarding."
        rm -rf "$PYTHON_PREFIX"
        return 1
    fi

    log "  Prebuilt Python ${PYTHON_STANDALONE_VERSION} installed at $PYTHON_PREFIX — OK."
    return 0
}

_build_python_from_source() {
    # Tier 3 (last resort): compile CPython on-device. Only reached if both the
    # system package manager and the prebuilt download failed. Every step is
    # logged (install.sh's stdout is tee'd to $INSTALL_LOG, which the app's
    # installer wizard tails live) so the phone shows real progress rather than
    # sitting on a single "Installing..." spinner for tens of minutes.
    log "  Falling back to building Python ${PYTHON_SOURCE_VERSION} from source."

    local mem_mb jobs est
    mem_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
    jobs=$(( mem_mb / 512 ))
    [[ $jobs -lt 1 ]] && jobs=1
    local nproc_n; nproc_n="$(nproc)"
    [[ $jobs -gt $nproc_n ]] && jobs=$nproc_n

    if   [[ $mem_mb -le 1200 ]]; then est="45-90 minutes"
    elif [[ $mem_mb -le 2500 ]]; then est="20-40 minutes"
    else                              est="10-20 minutes"
    fi
    log "  Board has ${mem_mb}MB RAM, ${nproc_n} CPU core(s) — using ${jobs} parallel build job(s)."
    log "  Estimated build time: ${est}. This runs detached — safe to close the app."

    # Low-RAM boards can OOM mid-compile even at a conservative job count.
    # Add a temporary swapfile for the duration of the build if none is
    # already active; cleanup_on_error() (via die()) and the explicit removal
    # below both cover it.
    if [[ $mem_mb -le 1536 ]] && [[ -z "$(swapon --show 2>/dev/null)" ]]; then
        log "  Low RAM detected — adding a temporary 1GB build swapfile."
        _SWAP_FILE="/var/tmp/ahc_build_swap.img"
        if fallocate -l 1G "$_SWAP_FILE" 2>/dev/null || dd if=/dev/zero of="$_SWAP_FILE" bs=1M count=1024 status=none; then
            chmod 600 "$_SWAP_FILE"
            mkswap "$_SWAP_FILE" &>/dev/null && swapon "$_SWAP_FILE" \
                || { warn "  Couldn't activate build swapfile — continuing without it."; rm -f "$_SWAP_FILE"; _SWAP_FILE=""; }
        else
            warn "  Couldn't allocate build swapfile — continuing without it."
            _SWAP_FILE=""
        fi
    fi

    log "  Installing build dependencies..."
    local build_deps=(
        build-essential zlib1g-dev libssl-dev libbz2-dev libreadline-dev
        libsqlite3-dev libffi-dev liblzma-dev libncursesw5-dev tk-dev
        uuid-dev wget
    )
    local to_install=()
    for pkg in "${build_deps[@]}"; do
        dpkg -s "$pkg" &>/dev/null || to_install+=("$pkg")
    done
    if [[ ${#to_install[@]} -gt 0 ]]; then
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${to_install[@]}" \
            || die "Failed to install Python build dependencies: ${to_install[*]}"
    fi

    local build_dir="/tmp/ahc_python_build"
    rm -rf "$build_dir"
    mkdir -p "$build_dir"

    log "  Downloading CPython ${PYTHON_SOURCE_VERSION} source..."
    curl -fsSL --max-time 120 -o "$build_dir/Python.tgz" \
        "https://www.python.org/ftp/python/${PYTHON_SOURCE_VERSION}/Python-${PYTHON_SOURCE_VERSION}.tgz" \
        || die "Failed to download CPython source tarball."

    tar xzf "$build_dir/Python.tgz" -C "$build_dir" --strip-components=1 \
        || die "Failed to extract CPython source tarball."

    log "  Configuring build (this takes a minute)..."
    ( cd "$build_dir" && ./configure --prefix="$PYTHON_PREFIX" >/dev/null ) \
        || die "CPython ./configure failed — see $INSTALL_LOG for details."

    # Deliberately no --enable-optimizations: PGO+LTO roughly triples build
    # time and peak RAM for a marginal runtime speedup that doesn't matter
    # here — this is a one-time provisioning build, not a perf-critical path.
    log "  Compiling — estimated ${est}..."
    ( cd "$build_dir" && make -j"$jobs" >/dev/null ) \
        || die "CPython compilation failed — see $INSTALL_LOG for details."

    log "  Installing to $PYTHON_PREFIX..."
    rm -rf "$PYTHON_PREFIX"
    ( cd "$build_dir" && make altinstall >/dev/null ) \
        || die "CPython 'make altinstall' failed — see $INSTALL_LOG for details."

    # altinstall names the binary python3.12, not python3 — normalize so the
    # rest of this script (and the smoke test below) can rely on one path.
    local ver_short="${PYTHON_SOURCE_VERSION%.*}"
    ln -sf "$PYTHON_PREFIX/bin/python${ver_short}" "$PYTHON_PREFIX/bin/python3"

    rm -rf "$build_dir"
    if [[ -n "$_SWAP_FILE" ]]; then
        swapoff "$_SWAP_FILE" 2>/dev/null || true
        rm -f "$_SWAP_FILE"
        _SWAP_FILE=""
    fi

    if ! _python_smoke_test "$PYTHON_PREFIX/bin/python3"; then
        die "Built Python failed the import smoke test."
    fi

    log "  Built Python ${PYTHON_SOURCE_VERSION} — OK."
}

provision_python() {
    log "[2/10] Provisioning Python ${MIN_PYTHON_VER}+..."
    write_phase "provision_python:checking_system"

    # Tier 1: an already-existing Python at this prefix (idempotent re-run —
    # don't redownload/rebuild every time the script runs).
    if _python_version_ok "$PYTHON_PREFIX/bin/python3" && _python_smoke_test "$PYTHON_PREFIX/bin/python3"; then
        log "  Python already provisioned at $PYTHON_PREFIX — OK."
        PYTHON_BIN="$PYTHON_PREFIX/bin/python3"
        return
    fi

    # Tier 1: system apt-installed python3 (from install_packages).
    local sys_py; sys_py="$(command -v python3 || true)"
    if [[ -n "$sys_py" ]] && _python_version_ok "$sys_py"; then
        log "  System Python ($sys_py) already meets ${MIN_PYTHON_VER}+ — OK."
        PYTHON_BIN="$sys_py"
        return
    fi
    log "  System Python is missing or older than ${MIN_PYTHON_VER} — need an alternative."

    # Tier 2: prebuilt download.
    write_phase "provision_python:downloading_prebuilt"
    if _try_download_prebuilt_python; then
        PYTHON_BIN="$PYTHON_PREFIX/bin/python3"
        return
    fi

    # Tier 3: build from source.
    write_phase "provision_python:building_from_source"
    _build_python_from_source
    PYTHON_BIN="$PYTHON_PREFIX/bin/python3"
}

create_user() {
    log "[3/10] Ensuring system user '$APP_USER' exists..."
    if id "$APP_USER" &>/dev/null; then
        log "  User '$APP_USER' already exists."
    else
        useradd -r -m -s /usr/sbin/nologin -d "$APP_HOME" "$APP_USER"
        log "  Created system user '$APP_USER'."
    fi
}

create_directories() {
    log "[4/10] Creating directories..."
    local dirs=(
        "$NAS_ROOT/personal"
        "$NAS_ROOT/family"
        "$NAS_ROOT/entertainment"
        "$NAS_ROOT/entertainment/Movies"
        "$NAS_ROOT/entertainment/Series"
        "$NAS_ROOT/entertainment/Anime"
        "$NAS_ROOT/entertainment/Music"
        "$NAS_ROOT/entertainment/Others"
        "$DATA_DIR/tls"
        "$APP_HOME"
        "$BACKEND_SRC"
    )

    for d in "${dirs[@]}"; do
        if [[ ! -d "$d" ]]; then
            mkdir -p "$d"
            _CREATED_DIRS+=("$d")
            log "  Created: $d"
        fi
    done

    chown -R "$APP_USER:$APP_USER" "$NAS_ROOT" "$DATA_DIR" "$APP_HOME"
    chmod 750 "$DATA_DIR"
}

deploy_backend() {
    log "[5/10] Deploying backend code..."
    local script_dir repo_root
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    # If run from within the repo, use it directly
    if [[ -d "$script_dir/backend" ]]; then
        repo_root="$script_dir"
    elif [[ -d "$script_dir/../backend" ]]; then
        repo_root="$(cd "$script_dir/.." && pwd)"
    else
        die "Cannot find backend/ directory. Run this script from the repo root."
    fi

    REPO_ROOT="$repo_root"

    # Verify the deploy target points at THIS run's upload, not merely that something is
    # already there. The previous version only checked existence, so re-installing from a
    # different checkout — a new admin account's home, say — logged "already exists" and went
    # on serving whatever code was live before. The install reported success and the freshly
    # uploaded backend was never used, which is indistinguishable from the upload silently
    # failing. Found on the x86 thin client, 2026-07-19; fixed 2026-08-04.
    local desired_target="$REPO_ROOT/backend"
    if [[ -L "$BACKEND_SRC" ]]; then
        local current_target
        current_target="$(readlink -f "$BACKEND_SRC" 2>/dev/null || true)"
        local desired_real
        desired_real="$(readlink -f "$desired_target" 2>/dev/null || echo "$desired_target")"
        if [[ "$current_target" == "$desired_real" ]]; then
            log "  Symlink already correct: $BACKEND_SRC -> $current_target"
        else
            warn "  Deploy symlink pointed at $current_target, but this run uploaded $desired_real"
            rm -f "$BACKEND_SRC"
            ln -s "$desired_target" "$BACKEND_SRC"
            log "  Repointed symlink: $BACKEND_SRC -> $desired_target"
        fi
    elif [[ -d "$BACKEND_SRC" && -f "$BACKEND_SRC/app/main.py" ]]; then
        # A real directory, not a symlink. It is NOT replaced automatically: it may be a
        # working checkout someone edits in place, and deleting it could destroy uncommitted
        # work. But it must not pass silently either, because it may be stale.
        if [[ "$(readlink -f "$BACKEND_SRC")" == "$(readlink -f "$desired_target")" ]]; then
            log "  Backend already deployed at $BACKEND_SRC (same as this run's upload)"
        else
            warn "  $BACKEND_SRC is a real directory, NOT this run's upload ($desired_target)."
            warn "  It is left untouched so nothing is destroyed — but the service will keep"
            warn "  serving that directory's code, not what was just uploaded. Move it aside"
            warn "  and re-run if you intended to deploy the new checkout."
        fi
    else
        rm -rf "$BACKEND_SRC"
        ln -s "$desired_target" "$BACKEND_SRC"
        log "  Created symlink: $BACKEND_SRC -> $desired_target"
    fi

    # The symlink target commonly lives under a human user's home directory (e.g. this project's
    # own rsync/scp-deployed checkout at ~/AiHomeCloud) — home directories default to blocking
    # traversal by other accounts, so the restricted $APP_USER service account can't reach through
    # the symlink even though it resolves. Grant that one account execute-only (traverse, not read)
    # access on every ancestor directory of the real target outside $APP_HOME, without touching the
    # rest of that directory's permissions.
    local real_target
    real_target="$(readlink -f "$BACKEND_SRC")"
    if [[ -n "$real_target" && "$real_target" != "$APP_HOME"* ]]; then
        local dir
        dir="$(dirname "$real_target")"
        while [[ "$dir" != "/" && "$dir" != "$APP_HOME"* ]]; do
            setfacl -m "u:${APP_USER}:x" "$dir" 2>/dev/null \
                || warn "  Couldn't grant $APP_USER traverse access on $dir (setfacl unavailable?) — service may fail to start."
            dir="$(dirname "$dir")"
        done
        log "  Granted $APP_USER traverse access through to $real_target"
    fi
}

setup_venv() {
    log "[6/10] Setting up Python virtual environment..."
    write_phase "setup_venv:creating_venv"
    if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
        # Create the venv directory owned by the app user
        mkdir -p "$(dirname "$VENV_DIR")"
        chown "$APP_USER":"$APP_USER" "$(dirname "$VENV_DIR")" 2>/dev/null || true
        sudo -u "$APP_USER" "$PYTHON_BIN" -m venv "$VENV_DIR"
        log "  Venv created at $VENV_DIR."
    else
        log "  Venv already exists at $VENV_DIR."
    fi

    local requirements="$BACKEND_SRC/requirements.txt"
    if [[ -f "$requirements" ]]; then
        log "  Installing Python dependencies..."
        write_phase "setup_venv:installing_dependencies"
        sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --quiet --upgrade pip
        sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --quiet -r "$requirements"
        log "  Dependencies installed."
    else
        warn "  requirements.txt not found — skipping pip install."
    fi
}

configure_mdns() {
    log "[7/10] Configuring mDNS (Avahi)..."
    if [[ ! -f "$AVAHI_SVC" ]]; then
        cat > "$AVAHI_SVC" << EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">AiHomeCloud on %h</name>
  <service>
    <type>_aihomecloud._tcp</type>
    <port>${PORT}</port>
    <txt-record>version=${VERSION}</txt-record>
  </service>
</service-group>
EOF
        _CREATED_FILES+=("$AVAHI_SVC")
        systemctl restart avahi-daemon 2>/dev/null || true
        log "  mDNS service registered: _aihomecloud._tcp"
    else
        log "  mDNS service already configured."
    fi
}

install_service() {
    log "[8/10] Installing systemd service..."
    local service_src="$BACKEND_SRC/aihomecloud.service"

    if [[ ! -f "$service_src" ]]; then
        die "Service template not found: $service_src"
    fi

    if [[ ! -f "$SERVICE_DST" ]]; then
        cp "$service_src" "$SERVICE_DST"
        _CREATED_FILES+=("$SERVICE_DST")
        chmod 644 "$SERVICE_DST"

        # The template's ProtectHome=yes hides every user home directory from the service's mount
        # namespace entirely (verified live: even with a matching BindPaths= entry, chdir into a
        # bound path under a masked home dir still fails — ProtectHome=yes replaces /home wholesale
        # with an empty tmpfs and BindPaths doesn't reliably punch through it on this systemd
        # version). If the backend checkout the WorkingDirectory symlink resolves to lives under a
        # home directory (this project's own deploy layout, e.g. ~/AiHomeCloud/backend), relax to
        # ProtectHome=read-only instead — confirmed live this combination (read-only + the ACL grant
        # above) lets the service traverse in while still blocking it from writing anywhere else
        # under any user's home.
        local real_backend_target
        real_backend_target="$(readlink -f "$BACKEND_SRC")"
        if [[ -n "$real_backend_target" && "$real_backend_target" != "$APP_HOME"* ]]; then
            sed -i "s/^ProtectHome=yes/ProtectHome=read-only/" "$SERVICE_DST"
            log "  Relaxed ProtectHome to read-only ($real_backend_target lives outside \$APP_HOME)"
        fi

        # Auto-generate unique device serial from hostname + MAC
        local mac auto_serial
        mac=$(ip link show 2>/dev/null | awk '/ether / {print $2}' | head -1 | tr -d ':' | tr '[:lower:]' '[:upper:]')
        auto_serial="AHC-$(hostname -s | tr '[:lower:]' '[:upper:]')-${mac:(-4)}"
        sed -i "s/AHC_DEVICE_SERIAL=AHC-A7A-2025-001/AHC_DEVICE_SERIAL=$auto_serial/" "$SERVICE_DST" 2>/dev/null || true
        log "  Device serial: $auto_serial"

        # Auto-generate pairing key
        local pairing_key
        pairing_key=$(openssl rand -hex 16)
        sed -i "s/AHC_PAIRING_KEY=your-pairing-key/AHC_PAIRING_KEY=$pairing_key/" "$SERVICE_DST" 2>/dev/null || true

        # Verify the substitutions actually landed, rather than trusting `sed ... || true`.
        #
        # Both seds above match a literal placeholder from the shipped template. If that template
        # line is ever reworded, the pattern silently matches nothing, `|| true` swallows it, and
        # the board boots with AHC_PAIRING_KEY=your-pairing-key — a publicly known pairing secret,
        # installed without a single warning. The 2026-08 audit flagged this as a live bug (M-10);
        # checking all three boards showed real 32-character keys, so the substitution does work
        # today. The finding was a false positive, but the silent-failure path that made it
        # plausible is real, and this is what closes it: fail loudly instead of shipping insecure.
        if grep -q 'AHC_PAIRING_KEY=your-pairing-key' "$SERVICE_DST"; then
            die "pairing key placeholder was not replaced — refusing to install an insecure unit."
        fi
        if grep -q 'AHC_DEVICE_SERIAL=AHC-A7A-2025-001' "$SERVICE_DST"; then
            warn "device serial placeholder was not replaced — this board will report a shared serial."
        fi
        log "  Pairing key generated."
    else
        log "  Service file exists — preserving your edits."
    fi

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" 2>/dev/null || true
}

configure_sudoers() {
    # There is no sudoers whitelist any more. This step now REMOVES the one older installs wrote.
    #
    # Every entry in it had been inert since NoNewPrivileges=yes landed on aihomecloud.service
    # (2026-07-14 — see the comment at the ExecStart hardening block below): sudo is setuid, and
    # NNP makes setuid transitions fail outright, so not one grant has worked from the service in
    # over a year. The working mechanism is the polkit rules plus the systemd .path units.
    #
    # An inert file would merely be clutter. This one was a loaded gun, because "inert" depended
    # entirely on NNP being set in every context the service user's code can run in — and the
    # backend self-update runs pip (which executes arbitrary setup.py from the update bundle) as
    # this user from a root oneshot that had no NNP. In that one context the whole file came back
    # to life, and it granted, among others:
    #
    #   mount                                    unrestricted → mount over /etc, or bind-mount
    #                                            anything anywhere: root, directly
    #   tee -a /etc/fstab                        arbitrary fstab entries → root at next mount
    #   cp /tmp/aihomecloud.service              → /etc/systemd/system/ + daemon-reload: write any
    #                                            unit and run it as root. This is C-1's exact shape
    #   cp /tmp/ahc-udev.rules                   → /etc/udev/rules.d/: udev RUN+= executes as root
    #   cp /tmp/telegram-bot-api + chmod 755     → arbitrary root-owned executable. C-3's shape
    #   mkfs.ext4                                unrestricted → destroy any block device
    #
    # So the audit's C-5 ("pip install from a unit without NNP") was not really about pip. pip was
    # just the way in; the escalation was this file, sitting armed behind a single unit setting.
    # Removing it is the fix that does not depend on getting NNP right everywhere, forever.
    # (2026-08-08 audit.)
    log "[9/10] Removing legacy sudoers whitelist..."
    if [[ -f "$SUDOERS_FILE" ]]; then
        rm -f "$SUDOERS_FILE"
        log "  Removed $SUDOERS_FILE — superseded by polkit rules and systemd .path units."
    else
        log "  No legacy sudoers file present."
    fi
}

configure_polkit() {
    log "[9a] Configuring polkit rules..."

    # $REPO_ROOT is set by deploy_backend() earlier in main(). These rules authorize the
    # aihomecloud service account (a scoped, non-sudo grant — see each file's own header comment)
    # to run the specific systemd units the storage/network/power routes depend on
    # (app/routes/storage_helpers.py's ahc-mount@*/ahc-umount units in particular). Found missing
    # live 2026-07-14: a fresh install had none of these deployed at all, so every storage-activate
    # call failed with "Interactive authentication required" — the backend code assumed this
    # authorization layer existed, but nothing had ever wired it into the installer.
    #
    # Deploys BOTH formats found in scripts/polkit/: modern JS rules (.rules -> rules.d/, needs
    # polkit >=0.106) and legacy local-authority rules (.pkla -> localauthority/50-local.d/, for
    # older builds). Found live the same day: this board's polkit is 0.105 — old enough that
    # .rules files are silently ignored outright, not just less capable. Deploying both and
    # letting whichever the local polkit binary understands take effect is simpler and more
    # robust across an unknown board fleet than trying to detect the polkit version here.
    local src_dir="$REPO_ROOT/backend/scripts/polkit"
    local rules_dst="/etc/polkit-1/rules.d"
    local pkla_dst="/etc/polkit-1/localauthority/50-local.d"

    if [[ ! -d "$src_dir" ]]; then
        warn "  No scripts/polkit/ directory found — skipping (storage/network actions may fail)."
        return
    fi

    mkdir -p "$rules_dst" "$pkla_dst"
    local copied=0
    for f in "$src_dir"/*.rules "$src_dir"/*.pkla; do
        [[ -f "$f" ]] || continue
        local dst_dir="$rules_dst"
        [[ "$f" == *.pkla ]] && dst_dir="$pkla_dst"
        local dst="$dst_dir/$(basename "$f")"
        if [[ ! -f "$dst" ]] || ! cmp -s "$f" "$dst"; then
            cp "$f" "$dst"
            chown root:root "$dst"
            chmod 644 "$dst"
            _CREATED_FILES+=("$dst")
            copied=$((copied + 1))
        fi
    done

    if [[ $copied -gt 0 ]]; then
        systemctl restart polkit 2>/dev/null || warn "  Couldn't restart polkit — rules may not take effect until next reboot."
        log "  Installed/updated $copied polkit rule file(s)."
    else
        log "  Polkit rules already up to date."
    fi
}

configure_storage_automount() {
    log "[9b] Configuring storage auto-mount (fstab + udev)..."

    # ── host-namespace mount/umount escape units ─────────────────────────────
    # The main aihomecloud.service runs under ProtectSystem=strict +
    # ReadWritePaths=, which puts it in a private mount namespace where its own
    # mount()/umount() syscalls never reach the real, system-wide mount table.
    # app/routes/storage_helpers.py's "Activate"/"Eject" buttons work around
    # this by asking systemd to run a tiny oneshot unit OUTSIDE that namespace
    # (systemctl start ahc-mount@<escaped-device>.service / ahc-umount.service)
    # — but nothing had ever deployed those unit files or their helper scripts
    # to a fresh board. Found live 2026-07-14: every "Activate" attempt on a
    # genuinely fresh install failed (masked behind a confusing polkit
    # "Interactive authentication required" error, since polkit denies before
    # systemd even reports "unit not found") because `systemctl status
    # ahc-mount@dev-nvme0n1p1.service` showed the unit simply didn't exist.
    local systemd_src="$REPO_ROOT/backend/scripts/systemd"
    if [[ -d "$systemd_src" ]]; then
        cp "$systemd_src/ahc-mount@.service" /etc/systemd/system/
        cp "$systemd_src/ahc-umount.service" /etc/systemd/system/
        # ahc-mount-backup@/ahc-umount-backup: local_backup.py's secondary-drive mount/unmount —
        # same escape-hatch shape, reuses the same two helper scripts below with a different
        # mountpoint argument (see each .service file), not a separate script.
        cp "$systemd_src/ahc-mount-backup@.service" /etc/systemd/system/
        cp "$systemd_src/ahc-umount-backup.service" /etc/systemd/system/
        cp "$REPO_ROOT/backend/scripts/ahc-mount-nas.sh" /usr/local/bin/
        cp "$REPO_ROOT/backend/scripts/ahc-umount-nas.sh" /usr/local/bin/
        # ahc-partition-format@/ahc-format@: same host-namespace escape hatch, for
        # smart-activate's sgdisk+mkfs.ext4 (fresh-drive Activate) and the manual /format
        # endpoint's mkfs.ext4 -- found live 2026-08-02, same "never actually deployed"
        # gap as the mount units above, on a board's genuinely first real Activate attempt.
        cp "$systemd_src/ahc-partition-format@.service" /etc/systemd/system/
        cp "$systemd_src/ahc-format@.service" /etc/systemd/system/
        cp "$REPO_ROOT/backend/scripts/ahc-partition-format-nas.sh" /usr/local/bin/
        cp "$REPO_ROOT/backend/scripts/ahc-format-nas.sh" /usr/local/bin/
        chmod 644 /etc/systemd/system/ahc-mount@.service /etc/systemd/system/ahc-umount.service \
            /etc/systemd/system/ahc-mount-backup@.service /etc/systemd/system/ahc-umount-backup.service \
            /etc/systemd/system/ahc-partition-format@.service /etc/systemd/system/ahc-format@.service
        chmod 755 /usr/local/bin/ahc-mount-nas.sh /usr/local/bin/ahc-umount-nas.sh \
            /usr/local/bin/ahc-partition-format-nas.sh /usr/local/bin/ahc-format-nas.sh
        systemctl daemon-reload
        log "  Deployed ahc-mount@/ahc-umount + ahc-mount-backup@/ahc-umount-backup + ahc-partition-format@/ahc-format@ host-namespace units."
    else
        warn "  No scripts/systemd/ directory found — storage Activate/Eject will fail."
    fi

    local UDEV_RULES_DST="/etc/udev/rules.d/99-ahc-storage.rules"
    local UDEV_HELPER="/usr/local/bin/ahc-mount-helper"

    # ── udev helper script ────────────────────────────────────────────────────
    # Called by udev when a matching drive is plugged in.
    # Mounts the drive at NAS_ROOT if it's not already mounted.
    cat > /tmp/ahc-mount-helper << 'HELPER'
#!/usr/bin/env bash
# AiHomeCloud — udev hot-plug mount helper
# Invoked by udev rule: ACTION=="add", ENV{ID_FS_TYPE}=="ext4"
set -euo pipefail
DEV="$1"
NAS_ROOT="/srv/nas"
LOG="/tmp/ahc-mount-helper.log"

echo "$(date -Iseconds) ahc-mount-helper called: DEV=${DEV}" >> "$LOG"

# Already mounted somewhere?
if grep -q "^${DEV} " /proc/mounts 2>/dev/null; then
    echo "$(date -Iseconds) ${DEV} already mounted — skipping" >> "$LOG"
    exit 0
fi

# Is the NAS root already occupied?
if grep -q " ${NAS_ROOT} " /proc/mounts 2>/dev/null; then
    echo "$(date -Iseconds) ${NAS_ROOT} already mounted — skipping" >> "$LOG"
    exit 0
fi

mkdir -p "$NAS_ROOT"
if mount "$DEV" "$NAS_ROOT"; then
    echo "$(date -Iseconds) Mounted ${DEV} at ${NAS_ROOT}" >> "$LOG"
    # Notify the AiHomeCloud service to re-sync its storage state
    systemctl restart aihomecloud 2>/dev/null || true
else
    echo "$(date -Iseconds) Mount of ${DEV} failed" >> "$LOG"
    exit 1
fi
HELPER
    chmod +x /tmp/ahc-mount-helper
    cp /tmp/ahc-mount-helper "$UDEV_HELPER"

    # ── udev rule ─────────────────────────────────────────────────────────────
    # Triggers on any ext4 partition on USB or NVME transport that matches
    # one of the known AiHomeCloud labels.
    cat > /tmp/ahc-udev.rules << 'RULES'
# AiHomeCloud — auto-mount NAS drives on hot-plug
# Matches: ext4 partition, USB or NVMe, label = AiHomeCloud | AiHomeNAS | ahc_nas
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_TYPE}=="ext4", \
  ENV{ID_FS_LABEL}=="AiHomeCloud|AiHomeNAS|ahc_nas|aihomecloud", \
  RUN+="/usr/local/bin/ahc-mount-helper %E{DEVNAME}"

# Fallback: any ext4 on USB transport (catches unlabelled drives too)
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_TYPE}=="ext4", \
  ENV{ID_BUS}=="usb", \
  RUN+="/usr/local/bin/ahc-mount-helper %E{DEVNAME}"
RULES
    cp /tmp/ahc-udev.rules "$UDEV_RULES_DST"
    udevadm control --reload-rules 2>/dev/null || true
    log "  udev hot-plug rule installed → $UDEV_RULES_DST"

    # ── fstab entry ───────────────────────────────────────────────────────────
    # Detect an already-activated NAS drive by checking what's mounted at
    # NAS_ROOT. If found, add a nofail fstab entry using its UUID.
    local current_dev
    current_dev=$(findmnt -n -o SOURCE --target "$NAS_ROOT" 2>/dev/null || true)
    if [[ -n "$current_dev" && "$current_dev" != "tmpfs" ]]; then
        local uuid
        uuid=$(blkid -s UUID -o value "$current_dev" 2>/dev/null || true)
        if [[ -n "$uuid" ]]; then
            if grep -q "$uuid" /etc/fstab 2>/dev/null; then
                log "  fstab entry for $uuid already present — skipping."
            else
                echo "UUID=${uuid} ${NAS_ROOT} ext4 defaults,nofail,x-systemd.device-timeout=10 0 2" \
                    | tee -a /etc/fstab > /dev/null
                log "  fstab entry added for UUID=${uuid} (${current_dev})."
            fi
        else
            warn "  Could not read UUID for ${current_dev} — fstab entry skipped."
        fi
    else
        warn "  No drive mounted at ${NAS_ROOT} right now — fstab entry skipped."
        warn "  Mount your NAS drive first, then re-run: sudo bash install.sh"
    fi
}

configure_wifi_helper() {
    log "[9c] Configuring Wi-Fi profile-install helper..."

    # Same host-namespace-escape shape as ahc-mount@/ahc-umount above, same reason:
    # app/wifi_manager.py's connect_to_network() writes a staged .nmconnection file to /tmp,
    # then needs it installed as root at /etc/NetworkManager/system-connections/ — but
    # NoNewPrivileges=yes on aihomecloud.service blocks sudo/setuid entirely from inside the
    # service, regardless of sudoers grants. Found live 2026-07-14: an initial sudoers-based
    # design failed every attempt with "sudo: effective uid is not 0" — switched to this
    # unit-escape pattern instead, authorized the same way (systemctl start, via the already-
    # broad manage-units polkit grant) rather than sudo.
    local systemd_src="$REPO_ROOT/backend/scripts/systemd"
    if [[ -d "$systemd_src" && -f "$systemd_src/ahc-wifi-install@.service" ]]; then
        cp "$systemd_src/ahc-wifi-install@.service" /etc/systemd/system/
        cp "$REPO_ROOT/backend/scripts/ahc-wifi-install.sh" /usr/local/bin/
        chmod 644 /etc/systemd/system/ahc-wifi-install@.service
        chmod 755 /usr/local/bin/ahc-wifi-install.sh
        systemctl daemon-reload
        log "  Deployed ahc-wifi-install@ host-namespace helper unit."
    else
        warn "  No scripts/systemd/ahc-wifi-install@.service found — Wi-Fi connect will fail."
    fi

    # Same escape-hatch shape, for item 7's hotspot toggle: `nmcli device wifi hotspot` (and
    # `connection down` on the resulting shared connection) is hardcoded root-only at the
    # NetworkManager D-Bus policy level, same as Settings.ReloadConnections above — confirmed
    # live 2026-07-19 that granting every plausible polkit action changes nothing, but the exact
    # same nmcli call succeeds instantly as root. See wifi_manager.py's hotspot-section comment.
    if [[ -d "$systemd_src" && -f "$systemd_src/ahc-hotspot-enable@.service" && -f "$systemd_src/ahc-hotspot-disable.service" ]]; then
        cp "$systemd_src/ahc-hotspot-enable@.service" /etc/systemd/system/
        cp "$systemd_src/ahc-hotspot-disable.service" /etc/systemd/system/
        cp "$REPO_ROOT/backend/scripts/ahc-hotspot.sh" /usr/local/bin/
        chmod 644 /etc/systemd/system/ahc-hotspot-enable@.service /etc/systemd/system/ahc-hotspot-disable.service
        chmod 755 /usr/local/bin/ahc-hotspot.sh
        systemctl daemon-reload
        log "  Deployed ahc-hotspot-enable@/ahc-hotspot-disable host-namespace helper units."
    else
        warn "  No scripts/systemd/ahc-hotspot-*.service found — WiFi hotspot toggle will fail."
    fi
}

configure_nfs_export() {
    log "[9d] Configuring NFS export..."

    # NFS was previously installed (install_packages) but never actually configured to
    # export anything — /etc/exports shipped empty, so the service ran but served nothing.
    # Export NAS_ROOT read-only, scoped to the local subnet (not '*') so it isn't
    # world-readable to anything that can route to this box. The in-app NFS toggle
    # (service_routes.py) only starts/stops/enables the daemon; the export itself is
    # written once here, at install time, per the v1 scope in
    # docs/plan_sbc_hardening_and_nas_sharing_2026-07-14.md's Phase 1.
    local subnet
    subnet=$(ip -4 route show 2>/dev/null | awk '/proto kernel/ {print $1; exit}')
    if [[ -z "$subnet" ]]; then
        warn "  Could not detect LAN subnet — falling back to 192.168.0.0/16."
        subnet="192.168.0.0/16"
    fi

    local export_line="${NAS_ROOT} ${subnet}(ro,sync,no_subtree_check,root_squash)"
    if grep -qF "$NAS_ROOT" /etc/exports 2>/dev/null; then
        log "  Export for ${NAS_ROOT} already present — skipping."
    else
        echo "$export_line" | tee -a /etc/exports > /dev/null
        exportfs -ra
        log "  Exported ${NAS_ROOT} read-only to ${subnet}."
    fi
}

configure_service_persist_helper() {
    log "[9e] Configuring service enable/disable helper..."

    # Same host-namespace-escape shape as ahc-wifi-install@ above, different
    # underlying reason: smbd/nmbd/nfs-kernel-server are SysV-init-compat units
    # on Debian 11, and `systemctl enable/disable` on them shells out to
    # `update-rc.d`, which does its own root check independent of whatever the
    # manage-unit-files polkit action authorized for the D-Bus call itself.
    # Found live 2026-07-14 verifying the SMB/NFS toggle: the polkit grant let
    # the call through, but update-rc.d itself still returned "Permission
    # denied" for the non-root aihomecloud caller. These oneshot units run the
    # same systemctl enable/disable call as genuine root instead.
    local systemd_src="$REPO_ROOT/backend/scripts/systemd"
    if [[ -d "$systemd_src" && -f "$systemd_src/ahc-enable-unit@.service" ]]; then
        cp "$systemd_src/ahc-enable-unit@.service" /etc/systemd/system/
        cp "$systemd_src/ahc-disable-unit@.service" /etc/systemd/system/
        cp "$REPO_ROOT/backend/scripts/ahc-persist-unit.sh" /usr/local/bin/
        chmod 644 /etc/systemd/system/ahc-enable-unit@.service /etc/systemd/system/ahc-disable-unit@.service
        chmod 755 /usr/local/bin/ahc-persist-unit.sh
        systemctl daemon-reload
        log "  Deployed ahc-enable-unit@/ahc-disable-unit@ host-namespace helper units."
    else
        warn "  No scripts/systemd/ahc-enable-unit@.service found — SMB/NFS toggle persistence will fail."
    fi
}

configure_local_backup_readwrite_path() {
    log "[9h] Granting local_backup.py write access to its secondary-drive mountpoint..."

    # Unlike StateDirectory=/RuntimeDirectory=, systemd's ReadWritePaths= does NOT create a
    # missing path — it fails the whole unit at start with "Failed to set up mount namespacing:
    # <path>: No such file or directory" (control process exit 226/NAMESPACE). Found live
    # 2026-07-15 on the ROCK Pi 4A: this line was missing and the service refused to start the
    # moment ReadWritePaths= referenced $BACKUP_ROOT before anything had ever created it.
    mkdir -p "$BACKUP_ROOT"

    # ReadWritePaths= is baked into the DEPLOYED unit file at install time; install_service()
    # only writes a fresh copy on a truly first install ("Service file exists — preserving your
    # edits" otherwise) — so a board that already had aihomecloud.service installed before this
    # feature shipped needs its EXISTING deployed unit patched, not just the template in this
    # repo. Idempotent: only appends if genuinely missing, so re-running install.sh is safe.
    if [[ ! -f "$SERVICE_DST" ]]; then
        return  # install_service() runs later and will write the up-to-date template
    fi
    if grep -q "ReadWritePaths=.*${BACKUP_ROOT}" "$SERVICE_DST" 2>/dev/null; then
        log "  Already granted."
        return
    fi
    sed -i "s#^\(ReadWritePaths=.*\)#\1 ${BACKUP_ROOT}#" "$SERVICE_DST"
    systemctl daemon-reload
    log "  Added ${BACKUP_ROOT} to ReadWritePaths (takes effect on next service restart)."
}

configure_unattended_upgrades() {
    log "[9i] Configuring automatic security updates..."

    local pkgs=(unattended-upgrades apt-listchanges)
    local to_install=()
    for pkg in "${pkgs[@]}"; do
        dpkg -s "$pkg" &>/dev/null || to_install+=("$pkg")
    done
    if [[ ${#to_install[@]} -gt 0 ]]; then
        log "  Installing: ${to_install[*]}"
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${to_install[@]}"
    fi

    # Security-suite-only auto-patching. The vendor kernel/firmware/U-Boot are
    # hardware-specific for this board — an unattended swap of any of them
    # could break boot on a headless device with nobody there to fix it, so
    # they're explicitly blacklisted regardless of the origin filter above.
    local conf_file="/etc/apt/apt.conf.d/50unattended-upgrades"
    local marker="// AHC_MANAGED_UNATTENDED_UPGRADES"
    if [[ -f "$conf_file" ]] && grep -qF "$marker" "$conf_file" 2>/dev/null; then
        log "  Unattended-upgrades config already managed by this installer — skipping."
    else
        cat > "$conf_file" << EOF
${marker}
// AiHomeCloud — see docs/plan_sbc_hardening_and_nas_sharing_2026-07-14.md Phase 2.
// \${distro_codename} is apt's own runtime substitution (not a shell variable) so
// this stays correct across an eventual bullseye->trixie base-OS migration
// without needing to regenerate this file.
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=\${distro_codename},label=Debian-Security";
    "origin=Debian,codename=\${distro_codename}-security,label=Debian-Security";
};
Unattended-Upgrade::Package-Blacklist {
    "linux-image-*";
    "linux-headers-*";
    "u-boot-*";
    "*-firmware";
    "rockchip-*";
    "radxa-*";
};
Unattended-Upgrade::Automatic-Reboot "false";
EOF
        _CREATED_FILES+=("$conf_file")
        log "  Wrote security-only unattended-upgrades config (vendor kernel/firmware excluded)."
    fi

    cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
    _CREATED_FILES+=("/etc/apt/apt.conf.d/20auto-upgrades")

    systemctl enable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
    log "  apt-daily timers enabled (previously enabled but inert without the package)."
}

configure_firewall() {
    log "[9j] Configuring firewall (LAN-scoped)..."

    if [[ "$ENABLE_FIREWALL" != true ]]; then
        log "  Skipped (opt-in — pass --enable-firewall to enable). See install.sh's"
        log "  --enable-firewall comment for why this defaults off on this board family."
        return
    fi

    local pkgs=(ufw)
    local to_install=()
    for pkg in "${pkgs[@]}"; do
        dpkg -s "$pkg" &>/dev/null || to_install+=("$pkg")
    done
    if [[ ${#to_install[@]} -gt 0 ]]; then
        log "  Installing: ${to_install[*]}"
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${to_install[@]}"
    fi

    # IPv6 support OFF entirely: found live 2026-07-14 that ufw's own default
    # IPv6 template (before6.rules) fails to load on this board's kernel
    # ("ip6tables-restore ... Couldn't load match `rt'") independent of the
    # logging issue below — this board doesn't use IPv6 for anything, so
    # disabling it sidesteps the incompatibility entirely rather than
    # patching around a kernel-specific module gap.
    sed -i 's/^IPV6=yes/IPV6=no/' /etc/default/ufw

    # Logging OFF: found live 2026-07-14 (real lockout incident, recovered via
    # physical console access) that ufw's default logging rules (-j LOG
    # --log-prefix "...") are rejected by this board's iptables-nft backend
    # ("unknown option --log-prefix"; ip6tables separately missing the `rt`
    # match). iptables-restore applies its entire rules file as ONE atomic
    # transaction — that one bad logging line at the END of the file aborted
    # the WHOLE restore, silently discarding every allow rule above it while
    # the separately-applied default-deny-incoming policy still took effect.
    # Net result: total lockout, including the SSH session running this very
    # installer. Logging isn't needed for this feature; disabling it avoids
    # the incompatible rule template entirely rather than patching around a
    # kernel-specific gap.
    ufw logging off > /dev/null

    local subnet
    subnet=$(ip -4 route show 2>/dev/null | awk '/proto kernel/ {print $1; exit}')
    if [[ -z "$subnet" ]]; then
        warn "  Could not detect LAN subnet — falling back to 192.168.0.0/16."
        subnet="192.168.0.0/16"
    fi

    ufw default deny incoming > /dev/null
    ufw default allow outgoing > /dev/null

    # ufw dedupes identical rules on its own, so re-adding these on a re-run is
    # safe (idempotent) without needing our own existence check first.
    ufw allow from "$subnet" to any port 22 proto tcp comment 'AHC: SSH' > /dev/null
    ufw allow from "$subnet" to any port 137,138 proto udp comment 'AHC: SMB (NetBIOS)' > /dev/null
    ufw allow from "$subnet" to any port 139,445 proto tcp comment 'AHC: SMB' > /dev/null
    ufw allow from "$subnet" to any port 111 proto tcp comment 'AHC: NFS (rpcbind)' > /dev/null
    ufw allow from "$subnet" to any port 111 proto udp comment 'AHC: NFS (rpcbind)' > /dev/null
    ufw allow from "$subnet" to any port 2049 proto tcp comment 'AHC: NFS' > /dev/null
    ufw allow from "$subnet" to any port "$PORT" proto tcp comment 'AHC: backend API' > /dev/null

    # Pre-flight validation: `ufw status`/its own bookkeeping does NOT
    # guarantee the kernel actually loaded the rules — found live 2026-07-14
    # that it reported success while the real iptables-restore underneath had
    # silently failed (see the logging comment above). Test the actual rules
    # file compiles cleanly before ever flipping the firewall on; abort
    # rather than risk a lockout if it doesn't.
    local test_err
    if ! test_err=$(iptables-restore --test /etc/ufw/user.rules 2>&1); then
        warn "  Firewall rules failed validation (iptables-restore --test) — NOT enabling ufw."
        warn "  $test_err"
        warn "  Firewall left disabled; investigate manually before re-running the installer."
        return
    fi
    if ! test_err=$(ip6tables-restore --test /etc/ufw/user6.rules 2>&1); then
        warn "  IPv6 firewall rules failed validation — NOT enabling ufw."
        warn "  $test_err"
        warn "  Firewall left disabled; investigate manually before re-running the installer."
        return
    fi

    # Safety net: auto-revert to disabled in 5 minutes unless explicitly
    # confirmed reachable. Defense-in-depth beyond the pre-flight test above,
    # which only catches syntax-level failures (the one actually hit live) —
    # not e.g. a subnet-detection mistake or some other runtime-only issue.
    # start_and_verify()'s own health check cancels this once the service is
    # confirmed responding; if it never gets cancelled, ufw reverts itself.
    systemctl stop ahc-ufw-safety-revert.timer 2>/dev/null || true
    if ! systemd-run --unit=ahc-ufw-safety-revert --on-active=300 \
        --description="AiHomeCloud: auto-revert ufw if not confirmed reachable" \
        /usr/sbin/ufw disable > /dev/null 2>&1; then
        warn "  Could not schedule the ufw safety-net auto-revert timer — proceeding without it."
    fi

    # Enable ONLY after the allow rules are in place AND validated — order
    # matters, this must never lock out the SSH session this very installer
    # is running over.
    ufw --force enable > /dev/null

    log "  Firewall enabled, LAN-scoped (${subnet}) to SSH/SMB/NFS/backend API."
    log "  Safety net armed: auto-reverts to disabled in 5 min unless confirmed reachable."
}

strip_desktop_stack() {
    log "[9k] Removing unnecessary desktop packages..."

    if [[ "$KEEP_DESKTOP" == true ]]; then
        log "  --keep-desktop passed — skipping."
        return
    fi

    # Refuse to yank a GUI out from under an active desktop session — this
    # board might genuinely be used with a monitor attached, not purely
    # headless. Detect via graphical.target + a logged-in seat, not just
    # "is a display manager installed" (which would always be true here).
    if systemctl is-active --quiet graphical.target 2>/dev/null && loginctl list-seats 2>/dev/null | grep -q seat0; then
        warn "  Graphical session detected — skipping desktop-stack removal (pass --keep-desktop to silence this warning)."
        return
    fi

    # Package names cover both the Radxa/Allwinner-vendor names found live on
    # the Cubie A5E and the generic Debian/Ubuntu equivalents another board
    # might ship — each is only purged if actually installed.
    local desktop_pkgs=(
        chromium-browser-sunxi chromium chromium-browser
        sddm lightdm gdm3
        task-allwinner-xorg task-allwinner-chromium radxa-sddm-theme
        xserver-xorg
    )

    local present=()
    for pkg in "${desktop_pkgs[@]}"; do
        dpkg -s "$pkg" &>/dev/null && present+=("$pkg")
    done

    if [[ ${#present[@]} -eq 0 ]]; then
        log "  No desktop packages found — nothing to remove."
        return
    fi

    log "  Purging: ${present[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get purge -y "${present[@]}" || warn "  Some desktop packages failed to purge — continuing."
    DEBIAN_FRONTEND=noninteractive apt-get autoremove --purge -y || true

    systemctl disable sddm lightdm gdm3 2>/dev/null || true

    log "  Desktop stack removed."
}

configure_factory_reset_helper() {
    log "[9d] Configuring factory-reset helper..."

    # Same host-namespace-escape shape as ahc-mount@/ahc-wifi-install@ above: the in-app Factory
    # Reset feature (app/routes/system_routes.py's POST /system/factory-reset) needs root to stop
    # its own service, remove its own systemd unit/polkit/sudoers/udev config, and delete the app
    # user account — none of which NoNewPrivileges=yes on aihomecloud.service permits from inside
    # the running process. scripts/ahc-factory-reset.sh (also usable directly over SSH: sudo
    # bash ahc-factory-reset.sh [--purge] [--wipe-media]) is deployed once here so it's always
    # available even mid-reset, independent of the live repo checkout it's about to delete.
    local systemd_src="$REPO_ROOT/backend/scripts/systemd"
    if [[ -d "$systemd_src" && -f "$systemd_src/ahc-factory-reset@.service" ]]; then
        cp "$systemd_src/ahc-factory-reset@.service" /etc/systemd/system/
        cp "$REPO_ROOT/backend/scripts/ahc-factory-reset.sh" /usr/local/bin/
        chmod 644 /etc/systemd/system/ahc-factory-reset@.service
        chmod 755 /usr/local/bin/ahc-factory-reset.sh
        systemctl daemon-reload
        log "  Deployed ahc-factory-reset@ host-namespace helper unit."
    else
        warn "  No scripts/systemd/ahc-factory-reset@.service found — Factory Reset will fail."
    fi
}

configure_backend_update_helper() {
    log "[9e] Configuring backend-update-apply helper..."

    # Same host-namespace-escape shape as ahc-mount@/ahc-factory-reset@ above: the in-app
    # "Update Backend" feature (app/routes/system_routes.py's POST /system/update) needs to
    # restart aihomecloud.service and flip a symlink under /opt/aihomecloud -- neither of which
    # NoNewPrivileges=yes on the running service permits from inside its own process, and neither
    # of which the process handling the update request could safely do to itself mid-request
    # anyway (it's the thing about to be replaced). scripts/ahc-apply-backend-update.sh is
    # deployed once here, independent of the versioned release dirs it will go on to create under
    # /opt/aihomecloud/backend_releases/, so it survives every future update it applies.
    local systemd_src="$REPO_ROOT/backend/scripts/systemd"
    if [[ -d "$systemd_src" && -f "$systemd_src/ahc-apply-update@.service" ]]; then
        cp "$systemd_src/ahc-apply-update@.service" /etc/systemd/system/
        cp "$REPO_ROOT/backend/scripts/ahc-apply-backend-update.sh" /usr/local/bin/
        chmod 644 /etc/systemd/system/ahc-apply-update@.service
        chmod 755 /usr/local/bin/ahc-apply-backend-update.sh
        mkdir -p "$APP_HOME/update_staging" "$APP_HOME/backend_releases"
        chown "$APP_USER:$APP_USER" "$APP_HOME/update_staging" "$APP_HOME/backend_releases"
        # aihomecloud.service's ReadWritePaths= lists update_status explicitly (needed so
        # trigger_update() can claim the update synchronously, see system_routes.py) -- systemd
        # requires every ReadWritePaths= entry to actually exist on disk before it will set up
        # the service's mount namespace at all, or the service fails outright at ExecStartPre
        # with "Failed to set up mount namespacing: ... No such file or directory". Found live
        # 2026-08-02 on a fresh install: this file had only ever been created manually while
        # debugging existing boards, never provisioned by install.sh itself.
        [[ -f "$APP_HOME/update_status" ]] || echo "idle" > "$APP_HOME/update_status"
        chown "$APP_USER:$APP_USER" "$APP_HOME/update_status"
        systemctl daemon-reload
        log "  Deployed ahc-apply-update@ host-namespace helper unit."
    else
        warn "  No scripts/systemd/ahc-apply-update@.service found — in-app backend updates will fail."
    fi
}

configure_board_identity() {
    log "[9f] Generating board identity key (H-11)..."

    # Ed25519 keypair the board keeps for life (regenerated only by ahc-factory-reset.sh) and
    # signs every future TLS-certificate rotation statement with. Idempotent by construction
    # (scripts/ahc-generate-identity.sh exits early if $DATA_DIR/identity already has a key), so
    # this is safe to run on every install.sh pass, including a re-run against an already
    # provisioned board -- it must NEVER regenerate an established identity out from under
    # existing paired clients. See docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md.
    if [[ -f "$REPO_ROOT/backend/scripts/ahc-generate-identity.sh" ]]; then
        bash "$REPO_ROOT/backend/scripts/ahc-generate-identity.sh"
    else
        warn "  No scripts/ahc-generate-identity.sh found — board will have no identity key, and TLS certificate issuance will fail."
    fi
}

configure_cert_issuance_helper() {
    log "[9g] Configuring TLS certificate issuance helper (H-11)..."

    # Same host-namespace-escape shape as ahc-mount@/ahc-factory-reset@ above, but file-watch
    # triggered rather than one-shot-invoked: aihomecloud.service (NoNewPrivileges=yes) can never
    # be trusted to generate its own TLS key/certificate under H-11 -- a service that could choose
    # what gets signed could get a signature over a key it chose, exactly the compromise the
    # identity key exists to prevent. It can only write app/tls.py's cert-request.json and wait;
    # this root-owned .path unit watches for that file and runs the real issuance out-of-process.
    # Deployed once here, independent of the live repo checkout, since it must keep answering
    # reissue requests for the life of the board, long after this install run ends.
    local systemd_src="$REPO_ROOT/backend/systemd"
    if [[ -d "$systemd_src" && -f "$systemd_src/ahc-issue-cert.path" && -f "$systemd_src/ahc-issue-cert.service" ]]; then
        cp "$REPO_ROOT/backend/scripts/ahc-issue-cert.sh" /usr/local/sbin/ahc-issue-cert
        chmod 755 /usr/local/sbin/ahc-issue-cert
        cp "$systemd_src/ahc-issue-cert.path" "$systemd_src/ahc-issue-cert.service" /etc/systemd/system/
        chmod 644 /etc/systemd/system/ahc-issue-cert.path /etc/systemd/system/ahc-issue-cert.service
        systemctl daemon-reload
        systemctl enable --now ahc-issue-cert.path
        log "  Deployed and armed ahc-issue-cert.path — the service will request its first certificate on startup."
    else
        warn "  No scripts/ahc-issue-cert.sh or systemd/ahc-issue-cert.{path,service} found — TLS certificate issuance will fail."
    fi
}

start_and_verify() {
    log "[10/10] Starting service and verifying..."

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        systemctl restart "$SERVICE_NAME"
        log "  Service restarted."
    else
        systemctl start "$SERVICE_NAME"
        log "  Service started."
    fi

    # Health check with retry
    local retries=5
    local ok=false
    for i in $(seq 1 $retries); do
        sleep 2
        if curl -sk --max-time 5 "https://localhost:${PORT}/api/health" 2>/dev/null | grep -q '"ok"'; then
            ok=true
            break
        fi
        log "  Waiting for service... (attempt $i/$retries)"
    done

    if $ok; then
        log "  Health check PASSED!"
    else
        warn "  Health check did not return OK. Check: sudo journalctl -u $SERVICE_NAME -n 30"
    fi

    # Cancel the ufw safety-net timer (configure_firewall) only if reachable
    # via the box's own real LAN-facing IP, not just localhost/127.0.0.1 —
    # ufw's default rules unconditionally ACCEPT everything on `lo`, so a
    # localhost-only check would pass even if the LAN-facing rules were
    # actually broken (the exact class of failure this whole check exists
    # to catch). If this box's own IP isn't reachable through its own
    # firewall, nothing external will be able to reach it either.
    if systemctl is-active --quiet ahc-ufw-safety-revert.timer 2>/dev/null; then
        local own_ip
        own_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
        if [[ -n "$own_ip" ]] && curl -sk --max-time 5 "https://${own_ip}:${PORT}/api/health" 2>/dev/null | grep -q '"ok"'; then
            systemctl stop ahc-ufw-safety-revert.timer 2>/dev/null || true
            log "  Firewall confirmed reachable via LAN IP (${own_ip}) — safety-net auto-revert cancelled."
        else
            warn "  Could not confirm firewall reachability via LAN IP — leaving the safety-net timer"
            warn "  armed. It will auto-revert ufw to disabled shortly unless you confirm access works."
        fi
    fi
}

print_summary() {
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

    echo ""
    log "=== Installation Complete! ==="
    echo ""
    echo -e "  ${CYAN}Backend URL  :${NC} https://${ip}:${PORT}"
    echo -e "  ${CYAN}Health check :${NC} curl -k https://localhost:${PORT}/api/health"
    echo -e "  ${CYAN}Logs         :${NC} sudo journalctl -u ${SERVICE_NAME} -f"
    echo -e "  ${CYAN}Service file :${NC} ${SERVICE_DST}"
    echo -e "  ${CYAN}Data dir     :${NC} ${DATA_DIR}"
    echo -e "  ${CYAN}NAS root     :${NC} ${NAS_ROOT}"
    echo ""

    # OS end-of-life surfacing (Phase 2 of the SBC hardening plan) — same static
    # codename->date map as backend/app/routes/system_routes.py's
    # _get_os_eol_info(), kept in sync manually since this is a bash script, not
    # Python. Verified via debian.org/tuxcare.com 2026-07-14.
    local codename="${VERSION_CODENAME:-unknown}"
    local eol_date=""
    case "$codename" in
        bullseye) eol_date="2026-08-31" ;;
        bookworm) eol_date="2028-06-10" ;;
        trixie)   eol_date="2028-08-09" ;;
    esac
    if [[ -n "$eol_date" ]]; then
        local days_remaining
        days_remaining=$(( ( $(date -d "$eol_date" +%s) - $(date +%s) ) / 86400 ))
        if [[ $days_remaining -le 90 ]]; then
            if [[ $days_remaining -lt 0 ]]; then
                echo -e "  ${RED}OS EOL       :${NC} ${codename} security support ENDED ${eol_date} — a base OS update is overdue"
            else
                echo -e "  ${YELLOW}OS EOL       :${NC} ${codename} security support ends ${eol_date} (${days_remaining} days) — plan a base OS update"
            fi
            echo ""
        fi
    fi

    echo -e "  ${YELLOW}Next steps:${NC}"
    echo "  1. Open the AiHomeCloud app on your phone"
    echo "  2. Scan the network to discover this device"
    echo "  3. Mount your external storage to ${NAS_ROOT}"
    echo ""
    echo -e "  ${YELLOW}To uninstall:${NC} sudo bash /usr/local/bin/ahc-factory-reset.sh [--purge] [--wipe-media]"
    echo ""
}

# =============================================================================
# MAIN
# =============================================================================

main() {
    write_phase "preflight";                  preflight
    write_phase "install_packages";           install_packages
    write_phase "provision_python";           provision_python
    write_phase "create_user";                create_user
    write_phase "create_directories";         create_directories
    write_phase "deploy_backend";             deploy_backend
    write_phase "setup_venv";                 setup_venv
    write_phase "configure_mdns";             configure_mdns
    write_phase "install_service";            install_service
    write_phase "configure_sudoers";          configure_sudoers
    write_phase "configure_polkit";           configure_polkit
    write_phase "configure_storage_automount"; configure_storage_automount
    write_phase "configure_wifi_helper";       configure_wifi_helper
    write_phase "configure_nfs_export";        configure_nfs_export
    write_phase "configure_service_persist_helper"; configure_service_persist_helper
    write_phase "configure_local_backup_readwrite_path"; configure_local_backup_readwrite_path
    write_phase "configure_unattended_upgrades"; configure_unattended_upgrades
    write_phase "configure_firewall";          configure_firewall
    write_phase "strip_desktop_stack";         strip_desktop_stack
    write_phase "configure_factory_reset_helper"; configure_factory_reset_helper
    write_phase "configure_backend_update_helper"; configure_backend_update_helper
    write_phase "configure_board_identity";   configure_board_identity
    write_phase "configure_cert_issuance_helper"; configure_cert_issuance_helper
    write_phase "start_and_verify";           start_and_verify
    write_phase "done";                       print_summary
}

main "$@" 2>&1 | tee "$INSTALL_LOG"
