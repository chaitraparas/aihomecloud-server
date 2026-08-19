#!/bin/sh
# Runs OUTSIDE the aihomecloud sandbox (host namespace, as root, via the
# ahc-enable-unit@/ahc-disable-unit@ oneshot units) -- same reason
# ahc-wifi-install.sh/ahc-mount-nas.sh exist: NoNewPrivileges=yes on the main
# service blocks sudo/setuid entirely regardless of sudoers grants.
#
# Specifically needed for SMB/NFS toggle persistence (found live 2026-07-14):
# smbd/nmbd/nfs-server are SysV-init-compat units on Debian 11 --
# `systemctl enable/disable` on them shells out to `update-rc.d` via
# systemd-sysv-install, and that subprocess does its own root check
# independent of whatever polkit authorized for the systemd D-Bus call
# itself ("update-rc.d: error: Permission denied" even though the polkit
# grant for org.freedesktop.systemd1.manage-unit-files succeeded) -- the
# same class of gap as NetworkManager's hardcoded-root ReloadConnections.
# Native (non-SysV-compat) units don't need this escape hatch; only route
# through it for units confirmed to need it (see PERSISTABLE_UNITS in
# service_routes.py, which must be kept in sync with this allowlist).
#
# nfs-server (not the nfs-kernel-server alias the Debian package is named
# after): found live the same day that `disable` on the alias name silently
# doesn't propagate to the real unit, even run as root -- use the canonical
# name to avoid that entirely, not just the permission issue above.
set -e
ACTION="$1"
UNIT="$2"

case "$UNIT" in
    smbd|nmbd|nfs-server) ;;
    *)
        echo "ahc-persist-unit.sh: refusing unrecognized unit: $UNIT" >&2
        exit 1
        ;;
esac

case "$ACTION" in
    enable|disable) ;;
    *)
        echo "ahc-persist-unit.sh: refusing unrecognized action: $ACTION" >&2
        exit 1
        ;;
esac

systemctl "$ACTION" "$UNIT"
