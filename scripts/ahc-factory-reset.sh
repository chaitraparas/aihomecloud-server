#!/usr/bin/env bash
#
# ahc-root-input: none
# Deletes these trees; it never reads their contents as input. Destructive by design, but there
# is no service-authored value here that steers what root does.
# =============================================================================
# AiHomeCloud — Factory Reset / Uninstall Script
#
# Formerly backend/uninstall.sh. Renamed and extended (2026-07-14) to close
# real gaps found by reading install.sh end-to-end: the old version never
# touched polkit rules, the udev rule, the WiFi/mount escape-hatch systemd
# units, the fstab line, telegram-bot-api, or the *real* backend checkout
# (/opt/aihomecloud/backend is a symlink -- removing $APP_HOME alone leaves
# the actual code, e.g. ~/AiHomeCloud, behind).
#
# This single script serves two callers:
#   1. Manual SSH use: sudo bash ahc-factory-reset.sh [--purge] [--wipe-media]
#   2. The in-app Factory Reset feature, via ahc-factory-reset@<mode>.service
#      (Type=oneshot, ExecStart=.../ahc-factory-reset.sh --purge %i) -- the
#      API-triggered path always purges; the two end-user modes map to
#      `--purge` (keep media) and `--purge --wipe-media` (wipe everything).
#
# Usage:
#   sudo bash ahc-factory-reset.sh                        # Remove code + all
#                                                          # install-time OS
#                                                          # config, keep data
#   sudo bash ahc-factory-reset.sh --purge                # + app data (every
#                                                          # SQL DB/JSON/secret
#                                                          # under data_dir)
#                                                          # + system user
#   sudo bash ahc-factory-reset.sh --purge --wipe-media   # + all NAS media
#                                                          # content too
#
# .avatars/ and .ahc_trash/ under NAS_ROOT are AiHomeCloud-generated, not raw
# user files -- removed whenever --purge is set (both factory-reset modes),
# independent of --wipe-media. Physical storage is never unmounted/reformatted
# by any mode here -- --wipe-media only empties personal/family/entertainment
# so a fresh install.sh + wizard run can immediately re-provision the same
# mount.
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[AiHomeCloud]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

APP_USER="aihomecloud"
APP_HOME="/opt/aihomecloud"
DATA_DIR="/var/lib/aihomecloud"
NAS_ROOT="/srv/nas"
SERVICE_NAME="aihomecloud"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
AVAHI_SVC="/etc/avahi/services/aihomecloud.service"
SUDOERS_FILE="/etc/sudoers.d/aihomecloud"
SYSTEMD_DIR="/etc/systemd/system"
POLKIT_RULES_DIR="/etc/polkit-1/rules.d"
POLKIT_PKLA_DIR="/etc/polkit-1/localauthority/50-local.d"
UDEV_RULES="/etc/udev/rules.d/99-ahc-storage.rules"
UDEV_HELPER="/usr/local/bin/ahc-mount-helper"

PURGE=false
WIPE_MEDIA=false
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=true ;;
        --wipe-media) WIPE_MEDIA=true ;;
        # --mode=<%i> is how ahc-factory-reset@<mode>.service invokes this
        # script (systemd template instance name), rather than a flag a
        # human would type over SSH -- --wipe-media above is the manual
        # equivalent for --mode=wipe-media.
        --mode=wipe-media) WIPE_MEDIA=true ;;
        --mode=keep-media) : ;;
    esac
done

# ── Must run as root ──────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[ERROR]${NC} Run with sudo: sudo bash ahc-factory-reset.sh" >&2
    exit 1
fi

log "=== AiHomeCloud Factory Reset ==="
$PURGE && warn "PURGE mode — all app data (SQL databases, accounts, secrets) will be removed!"
$WIPE_MEDIA && warn "WIPE-MEDIA mode — all NAS media content will be permanently deleted!"
echo ""

# 1. Stop and disable the main service. (Nothing under NAS_ROOT is unmounted
#    here -- physical storage stays mounted through the whole reset + reboot,
#    matching install.sh's own fstab entry, so a fresh install can reuse it.)
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    log "Stopped service."
fi
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
    log "Disabled service."
fi

# 2. Remove every polkit file this project deploys (both formats -- see
#    install.sh's configure_polkit(), which copies whatever is present in
#    scripts/polkit/ without a hardcoded count).
polkit_removed=0
for f in "$POLKIT_RULES_DIR"/*aihomecloud*.rules "$POLKIT_PKLA_DIR"/*aihomecloud*.pkla; do
    if [[ -f "$f" ]]; then
        rm -f "$f"
        polkit_removed=$((polkit_removed + 1))
    fi
done
if [[ $polkit_removed -gt 0 ]]; then
    systemctl restart polkit 2>/dev/null || true
    log "Removed $polkit_removed polkit rule file(s)."
fi

# 3. Remove the storage-automount udev rule + its helper.
if [[ -f "$UDEV_RULES" ]]; then
    rm -f "$UDEV_RULES"
    udevadm control --reload-rules 2>/dev/null || true
    log "Removed udev rule."
fi
[[ -f "$UDEV_HELPER" ]] && rm -f "$UDEV_HELPER"

# 4. Remove the escape-hatch systemd units + their /usr/local/bin helpers
#    (ahc-mount@, ahc-umount, ahc-wifi-install@, ahc-factory-reset@ itself,
#    telegram-bot-api). Deleting this unit's own template file and this
#    script's own path while still executing is safe on Linux: an open file
#    descriptor keeps the inode alive after unlink(), so the running process
#    is unaffected -- it just can't be started again by name afterward, which
#    is exactly the point.
for unit in ahc-mount@.service ahc-umount.service ahc-wifi-install@.service \
            ahc-factory-reset@.service telegram-bot-api.service; do
    [[ -f "$SYSTEMD_DIR/$unit" ]] && rm -f "$SYSTEMD_DIR/$unit"
done
for helper in ahc-mount-nas.sh ahc-umount-nas.sh ahc-wifi-install.sh ahc-factory-reset.sh telegram-bot-api; do
    [[ -f "/usr/local/bin/$helper" ]] && rm -f "/usr/local/bin/$helper"
done
log "Removed mount/WiFi/Telegram helper units and scripts."

# 5. Remove the main service unit file.
if [[ -f "$SERVICE_DST" ]]; then
    rm -f "$SERVICE_DST"
    log "Removed systemd service file."
fi
systemctl daemon-reload

# 6. Remove Avahi mDNS service.
if [[ -f "$AVAHI_SVC" ]]; then
    rm -f "$AVAHI_SVC"
    systemctl restart avahi-daemon 2>/dev/null || true
    log "Removed mDNS service."
fi

# 7. Remove sudoers rules.
if [[ -f "$SUDOERS_FILE" ]]; then
    rm -f "$SUDOERS_FILE"
    log "Removed sudoers whitelist."
fi

# 8. Remove the fstab NAS line install.sh appended -- match on the /srv/nas
#    mountpoint field specifically (not just any UUID= line, and not e.g.
#    /srv/nasty), so an unrelated fstab entry is never touched. Extended
#    regex + POSIX character classes rather than \S/\+ -- verified those GNU
#    BRE extensions behave inconsistently across sed implementations, this
#    form is portable and was tested directly against a synthetic fstab.
if grep -qE "^[[:space:]]*UUID=[^[:space:]]+[[:space:]]+${NAS_ROOT}[[:space:]]+" /etc/fstab 2>/dev/null; then
    sed -E -i.bak-factory-reset "\|^UUID=[^[:space:]]+[[:space:]]+${NAS_ROOT}[[:space:]]|d" /etc/fstab
    log "Removed NAS entry from /etc/fstab (backup: /etc/fstab.bak-factory-reset)."
fi

# 9. Remove the application directory (the symlink at $APP_HOME/backend
#    points at the REAL checkout, commonly outside $APP_HOME entirely --
#    e.g. ~/AiHomeCloud/backend -- resolve it BEFORE removing $APP_HOME or
#    the symlink is gone and can't be resolved anymore).
if [[ -L "$APP_HOME/backend" ]]; then
    real_checkout="$(readlink -f "$APP_HOME/backend" 2>/dev/null || true)"
fi
if [[ -d "$APP_HOME" || -L "$APP_HOME/backend" ]]; then
    rm -rf "$APP_HOME"
    log "Removed application directory: $APP_HOME"
fi
if [[ -n "${real_checkout:-}" && -d "$real_checkout" ]]; then
    # Repo root is the parent of backend/ (e.g. ~/AiHomeCloud, not
    # ~/AiHomeCloud/backend) -- remove the whole checkout, not just backend/.
    repo_root="$(dirname "$real_checkout")"
    # `dirname` of a top-level path is "/", and this is an unconditional root `rm -rf`. The symlink
    # this derives from lives in root-owned /opt and is not service-writable, so this is defence in
    # depth rather than a live hole — but the failure mode is so total that a three-line guard is
    # obviously worth it. (2026-08-09 sweep.)
    case "$repo_root" in
        ""|/|/usr|/usr/*|/etc|/var|/opt|/home|/root|/boot|/bin|/sbin|/lib*|/srv|/mnt)
            log "Refusing to remove implausible repo root: $repo_root"
            ;;
        *)
            rm -rf "${repo_root:?}"
            log "Removed backend repo checkout: $repo_root"
            ;;
    esac
fi

# 9b. Destroy the board's H-11 identity key -- unconditionally, independent of --purge.
#     docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md states the identity key
#     "never rotates except factory reset": every other factory-reset artifact only goes
#     away in --purge mode, but this one is the board's actual cryptographic identity, and
#     a factory reset that left it intact while the app data around it was wiped (or kept
#     serving it to a re-provisioned board) would defeat the reason it exists. A fresh
#     identity gets generated on the next install (ahc-generate-identity.sh, run from
#     install.sh) -- there is nothing here to preserve.
if [[ -d "$DATA_DIR/identity" ]]; then
    rm -rf "$DATA_DIR/identity"
    log "Removed board identity key ($DATA_DIR/identity) -- a fresh identity will be generated on next install."
fi

# 9c. Destroy the TLS certificate alongside it -- also unconditionally. A non-purge reset (the
#     in-app "keep media" mode) otherwise left $DATA_DIR/tls untouched: the surviving certificate
#     still covers the board's current IPs and isn't expiring, so ensure_tls_cert() on the next
#     start returns it as-is and never asks root to reissue -- meaning ahc-issue-cert.sh never
#     runs, statement.json is never regenerated, GET /system/identity 404s indefinitely, and the
#     reinstall-triggers-a-recovery-screen flow the design doc describes (§5) never fires, because
#     the served SPKI never actually changed. Removing it here forces a real reissue (and a fresh,
#     signed statement) the next time the service starts, matching identity's treatment above.
#     (2026-08-13 security review.)
if [[ -d "$DATA_DIR/tls" ]]; then
    rm -rf "$DATA_DIR/tls"
    log "Removed TLS certificate ($DATA_DIR/tls) -- a fresh one will be issued and signed on next start."
fi

# 10. Purge-only: app data (all SQL DBs + JSON state + jwt_secret + TLS certs
#     live under $DATA_DIR -- see config.py -- so this one directory covers
#     all of it), .avatars/.ahc_trash (AiHomeCloud-generated, not raw user
#     media -- removed in both factory-reset modes since both purge), and the
#     system user account.
if $PURGE; then
    if [[ -d "$DATA_DIR" ]]; then
        rm -rf "$DATA_DIR"
        log "Removed data directory: $DATA_DIR"
    fi
    for d in "$NAS_ROOT/.avatars" "$NAS_ROOT/.ahc_trash"; do
        [[ -d "$d" ]] && rm -rf "$d" && log "Removed $d"
    done
    if id "$APP_USER" &>/dev/null; then
        userdel -r "$APP_USER" 2>/dev/null || userdel "$APP_USER" 2>/dev/null || true
        log "Removed system user: $APP_USER"
    fi
else
    log "App data preserved at: $DATA_DIR"
    log "System user preserved: $APP_USER"
fi

# 11. Wipe-media-only: empty the actual NAS content. Never unmount/reformat
#     the drive itself -- just its contents -- so a fresh install.sh +
#     wizard run can immediately re-provision the same physical storage.
if $WIPE_MEDIA; then
    for d in "$NAS_ROOT/personal" "$NAS_ROOT/family" "$NAS_ROOT/entertainment"; do
        [[ -d "$d" ]] && rm -rf "${d:?}"/* "${d:?}"/.[!.]* 2>/dev/null
    done
    log "Wiped NAS media content (personal/family/entertainment)."
elif [[ -d "$NAS_ROOT" ]]; then
    log "NAS media preserved at: $NAS_ROOT"
fi

echo ""
log "=== Factory reset complete ==="
echo ""
echo "Removed: systemd service, mount/WiFi/Telegram helper units + scripts,"
echo "         polkit rules, udev rule, sudoers whitelist, avahi mDNS service,"
echo "         fstab NAS entry, application directory + real repo checkout"
$PURGE && echo "         + app data ($DATA_DIR), .avatars/.ahc_trash, system user ($APP_USER)"
$WIPE_MEDIA && echo "         + all NAS media content"
echo ""

# Reboot for a genuinely clean end state. Deliberately unconditional (not gated on $PURGE) --
# even the lighter default mode has removed the running service, so a reboot leaves the board in
# an unambiguous, fully-stopped state rather than a half-torn-down one still limping along.
# Found live 2026-07-14: this step was specified in the original design but never actually
# written into the script -- the board just sat there post-reset instead of rebooting. Harmless
# in that it happened to fail safe (nothing destructive), but the intended clean end state (and
# the app's "this device will disconnect and restart" messaging) depends on this actually running.
log "Rebooting..."
systemctl reboot
