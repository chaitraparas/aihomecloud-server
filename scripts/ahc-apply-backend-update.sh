#!/usr/bin/env bash
#
# ahc-root-input: validate
# Reads the caller-staged update bundle from update_staging/ and extracts it as root, so the
# archive is service-authored input. Root re-validates it before trusting it: every tar entry
# is rejected if absolute or containing '..' (the tar-slip guard below), and the extracted tree
# must contain app/main.py or the release is discarded. The code it installs then runs
# unprivileged, and NoNewPrivileges=yes on this unit stops pip's arbitrary setup.py from
# climbing back up.
# =============================================================================
# AiHomeCloud — Backend Update Apply
#
# Runs as genuine root, OUTSIDE the aihomecloud service's own process and its
# ProtectSystem=strict sandbox, via the ahc-apply-update@<version>.service
# oneshot unit -- app/routes/system_routes.py's POST /system/update stages an
# uploaded backend_bundle.tar to update_staging/ and triggers this script
# with `systemctl start ahc-apply-update@<version>.service`, then returns 202
# immediately (the process handling that request is the one about to be
# restarted, so it can't wait for or drive its own replacement). This script
# is deliberately the ONLY thing responsible for verifying the new code and
# rolling back on failure: if a bad update is broken badly enough that Python
# can't even import, the FastAPI app has no chance to save itself -- recovery
# has to live in something the new code can't take down with it.
#
# Usage: sudo bash ahc-apply-backend-update.sh <version>
#
# Layout this script owns:
#   /opt/aihomecloud/backend_releases/<version>/   one dir per applied release
#   /opt/aihomecloud/backend                        symlink -> current release
#   /opt/aihomecloud/shared-venv                    one venv, reused by every
#                                                    release via a per-release
#                                                    .venv symlink -- no per-
#                                                    release venv duplication
#                                                    on 1GB-RAM boards.
#   /opt/aihomecloud/update_staging/<version>.tar   input, removed after use
#   /opt/aihomecloud/update_staging/update_status   single-line status the
#                                                    app polls via GET
#                                                    /system/update/status --
#                                                    lives inside update_staging/,
#                                                    not as a sibling path, so it
#                                                    stays covered by the service's
#                                                    existing ReadWritePaths= entry
#                                                    (found live 2026-08-01: a
#                                                    standalone /opt/aihomecloud/
#                                                    update_status path isn't
#                                                    writable under
#                                                    ProtectSystem=strict)
# =============================================================================

set -euo pipefail

APP_HOME="/opt/aihomecloud"
APP_USER="aihomecloud"
RELEASES_DIR="$APP_HOME/backend_releases"
SHARED_VENV="$APP_HOME/shared-venv"
BACKEND_LINK="$APP_HOME/backend"
STAGING_DIR="$APP_HOME/update_staging"
STATUS_FILE="$STAGING_DIR/update_status"
PREVIOUS_TARGET_FILE="$APP_HOME/update_previous_target"
PORT="${AHC_PORT:-8443}"
HEALTH_URL="https://127.0.0.1:${PORT}/api/health"
KEEP_RELEASES=2

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[ahc-update]${NC} $*"; }
warn() { echo -e "${YELLOW}[ahc-update]${NC} $*"; }
die()  { echo -e "${RED}[ahc-update]${NC} $*" >&2; write_status "failed:${VERSION:-unknown}:$1"; exit 1; }

write_status() { echo "$1" > "$STATUS_FILE"; }

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root." >&2
    exit 1
fi

VERSION="${1:-}"
# The instance name of a polkit-reachable template, so the service user chooses this string, and it
# is interpolated into paths that root then `rm -rf`s.
#
# The previous pattern was ^[a-zA-Z0-9._-]+$ — which ALLOWS "..", because ".." is only dots.
# That was enough for a full traversal (2026-08-09 adversarial sweep, confirmed by path arithmetic
# and by checking the precondition is satisfiable):
#
#   VERSION=".."  ->  TAR_PATH  = /opt/aihomecloud/update_staging/...tar   <- the service user can
#                                                                             create this file; the
#                                                                             directory is its own
#                                                                             and in ReadWritePaths
#                     NEW_RELEASE_DIR = /opt/aihomecloud/backend_releases/..
#                                     = /opt/aihomecloud
#                     rm -rf "$NEW_RELEASE_DIR"   ->  deletes the entire install, as root:
#                                                     every release, the shared venv, staging.
#
# So: must begin with an alphanumeric (kills a leading dot outright) and must contain no ".."
# sequence anywhere. Checked as a literal substring as well as by pattern, because this is the
# check standing between an unprivileged process and a root rm -rf.
if [[ -z "$VERSION" || ! "$VERSION" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ || "$VERSION" == *..* ]]; then
    echo "Usage: $0 <version>  (must start alphanumeric; letters, digits, . _ - only; no '..')" >&2
    exit 1
fi

TAR_PATH="$STAGING_DIR/${VERSION}.tar"
NEW_RELEASE_DIR="$RELEASES_DIR/$VERSION"

log "Applying backend update to version $VERSION"
write_status "applying:$VERSION"

if [[ ! -f "$TAR_PATH" ]]; then
    die "staged bundle not found: $TAR_PATH"
fi

if [[ -d "$NEW_RELEASE_DIR" ]]; then
    warn "release dir $NEW_RELEASE_DIR already exists — removing before re-extract"
    rm -rf "$NEW_RELEASE_DIR"
fi

# ── Extract to a versioned dir, never touching the live tree ────────────────
# Safety lives in scripts/extract_update_bundle.py, not a bash name check here: a bash guard
# on `tar -tf` entry NAMES alone never inspects a symlink/hardlink entry's TARGET, which is
# how a clean-looking entry name can still plant a node resolving outside this directory
# (empirically verified against GNU tar 1.35, the version on these boards -- see that
# script's own header for the full analysis). Python's tarfile.data_filter (stdlib, PEP 706)
# validates both the entry path and any link target before anything is written, independent
# of which tar binary a future board ships.
mkdir -p "$NEW_RELEASE_DIR"
if ! python3 "$(dirname "$0")/extract_update_bundle.py" "$TAR_PATH" "$NEW_RELEASE_DIR"; then
    die "unsafe or invalid update bundle — rejected during extraction"
fi
# The uploaded bundle is the same shape bundleBackendForInstaller produces —
# a top-level "backend/" dir plus install.sh. Only the backend/ contents are
# what actually gets run; flatten so NEW_RELEASE_DIR itself is app/, requirements.txt, etc.
if [[ -d "$NEW_RELEASE_DIR/backend" ]]; then
    shopt -s dotglob
    mv "$NEW_RELEASE_DIR/backend/"* "$NEW_RELEASE_DIR/"
    shopt -u dotglob
    rmdir "$NEW_RELEASE_DIR/backend" 2>/dev/null || true
fi

if [[ ! -f "$NEW_RELEASE_DIR/app/main.py" ]]; then
    rm -rf "$NEW_RELEASE_DIR"
    die "extracted bundle missing app/main.py — not a valid backend release"
fi

# Must happen before pip install / py_compile below, not after: extraction runs as root (this
# whole script does), so the aihomecloud user has no write access yet to create __pycache__
# during py_compile or, in principle, anything requirements.txt-driven that touches this tree.
# Found live 2026-07-30 on real hardware -- py_compile failed with a plain PermissionError on
# app/__pycache__ when this chown ran after the compile check instead of before it.
chown -R "$APP_USER:$APP_USER" "$NEW_RELEASE_DIR"

# ── Dropping to the service user: runuser, never sudo ──────────────────────
#
# `sudo -u aihomecloud` used to be how these steps dropped privilege, and it was the wrong tool
# here for a security reason, not a stylistic one. sudo is setuid, so it consults the sudoers file
# and — critically — the resulting child can call sudo AGAIN. `pip install` executes arbitrary code
# from the bundle being installed (setup.py, PEP 517 build backends), as this user, in a process
# tree whose root unit had no NoNewPrivileges. That child could therefore use every grant in
# /etc/sudoers.d/aihomecloud, which included unrestricted `mount` and a `cp` into
# /etc/systemd/system — root, from a code path whose whole purpose is running untrusted new code.
#
# Verified live on the Rock Pi 4A, 2026-08-08. A child dropped to aihomecloud from a unit WITHOUT
# NoNewPrivileges ran `sudo -n lsblk` successfully — meaning it could equally have run `sudo mount`
# or copied a unit into /etc/systemd/system. Under NoNewPrivileges=yes the same call fails. So the
# unit setting is the control that actually closes this; removing the sudoers file closes it a
# second, independent way.
#
# runuser is used rather than `sudo -u` for defence in depth, not because sudo fails here — it does
# not: this script is already root, and root needs no setuid bit to drop privilege, so `sudo -u`
# works under NNP too (checked, rather than assumed). runuser is simply not setuid and consults no
# sudoers policy, so it cannot be re-armed by a future change to that file.
# (2026-08-08 audit, C-5.)
as_app_user() { runuser -u "$APP_USER" -- "$@"; }

# ── Shared venv: create once, reused (via symlink) by every release ─────────
if [[ ! -f "$SHARED_VENV/bin/activate" ]]; then
    log "Creating shared venv (first update on this board)…"
    # Reuse whichever python3 the currently-running install already resolved during its own
    # provision_python() tiers — by this point in a board's life that resolution has already
    # happened once; re-running the full tiered fallback here would duplicate a lot of install.sh
    # for a case (board already has a working Python 3.12) that's already true by construction.
    CURRENT_PY="$(readlink -f "$BACKEND_LINK/.venv/bin/python3" 2>/dev/null || command -v python3)"
    as_app_user "$CURRENT_PY" -m venv "$SHARED_VENV"
fi
ln -sfn "$SHARED_VENV" "$NEW_RELEASE_DIR/.venv"

log "Installing dependencies into shared venv…"
if ! as_app_user "$SHARED_VENV/bin/pip" install --quiet --upgrade pip; then
    rm -rf "$NEW_RELEASE_DIR"
    die "pip self-upgrade failed — aborting before touching the live release"
fi
if ! as_app_user "$SHARED_VENV/bin/pip" install --quiet -r "$NEW_RELEASE_DIR/requirements.txt"; then
    rm -rf "$NEW_RELEASE_DIR"
    die "dependency install failed — aborting before touching the live release"
fi

# ── Sanity-check the new code compiles before it's ever live ────────────────
log "Compile-checking new release…"
compile_error=""
if ! compile_error="$(find "$NEW_RELEASE_DIR/app" -name '*.py' -print0 \
        | xargs -0 runuser -u "$APP_USER" -- "$SHARED_VENV/bin/python" -m py_compile 2>&1)"; then
    warn "compile check failed: $compile_error"
    rm -rf "$NEW_RELEASE_DIR"
    die "new release failed py_compile — aborting before touching the live release"
fi

# ── Record current target for rollback, then flip the symlink ───────────────
PREVIOUS_TARGET="$(readlink -f "$BACKEND_LINK" 2>/dev/null || echo "")"
echo "$PREVIOUS_TARGET" > "$PREVIOUS_TARGET_FILE"

log "Activating new release: $BACKEND_LINK -> $NEW_RELEASE_DIR"
ln -sfn "$NEW_RELEASE_DIR" "$BACKEND_LINK"

log "Restarting aihomecloud service…"
systemctl restart aihomecloud

# ── Health check with rollback on failure ────────────────────────────────────
HEALTHY=false
for _ in $(seq 1 10); do
    sleep 2
    if curl -sk --max-time 3 "$HEALTH_URL" | grep -q '"status":"ok"'; then
        HEALTHY=true
        break
    fi
done

if [[ "$HEALTHY" != "true" ]]; then
    warn "new release failed health check — rolling back to $PREVIOUS_TARGET"
    if [[ -n "$PREVIOUS_TARGET" && -e "$PREVIOUS_TARGET" ]]; then
        ln -sfn "$PREVIOUS_TARGET" "$BACKEND_LINK"
        systemctl restart aihomecloud
        sleep 3
        if curl -sk --max-time 3 "$HEALTH_URL" | grep -q '"status":"ok"'; then
            log "Rollback succeeded — service healthy on previous version."
        else
            warn "Rollback restart ALSO failed health check — service may be down. Manual intervention needed."
        fi
    else
        warn "No valid previous target recorded — cannot auto-rollback. Manual intervention needed."
    fi
    write_status "failed:$VERSION:health_check_timeout"
    # Deliberately NOT pruned -- a failed release dir stays on disk for postmortem.
    rm -f "$TAR_PATH"
    exit 1
fi

log "Update to $VERSION applied and healthy."
write_status "success:$VERSION"
rm -f "$TAR_PATH"

# ── Prune old releases, keep the last few ────────────────────────────────────
if [[ -d "$RELEASES_DIR" ]]; then
    # shellcheck disable=SC2012
    ls -1t "$RELEASES_DIR" | tail -n "+$((KEEP_RELEASES + 1))" | while read -r old; do
        [[ "$RELEASES_DIR/$old" == "$NEW_RELEASE_DIR" ]] && continue
        # Never prune whatever's still the live symlink target (shouldn't happen given the
        # tail offset, but the check costs nothing and this deletes real disk content).
        [[ "$(readlink -f "$BACKEND_LINK")" == "$RELEASES_DIR/$old" ]] && continue
        log "Pruning old release: $old"
        rm -rf "${RELEASES_DIR:?}/$old"
    done
fi
