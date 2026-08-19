#!/bin/sh
#
# ahc-root-input: validate
# Reads iface/ssid/password staged by the service and re-validates the file's SHAPE here — exactly
# three lines, plausible interface name, non-empty SSID. The line count is the load-bearing
# check: a newline in the SSID would otherwise shift the password field and silently bring the
# hotspot up OPEN. (C-9)
# Runs OUTSIDE the aihomecloud sandbox (host namespace, as root, via the ahc-hotspot-enable@
# oneshot unit) -- same reason ahc-wifi-install.sh exists: `nmcli device wifi hotspot` returns
# "Not authorized to share connections via wifi" for the unprivileged aihomecloud user even with
# every plausible polkit grant in place (org.freedesktop.NetworkManager.wifi.share.protected/
# open both added and confirmed NOT to fix it, live, 2026-07-19) -- confirmed the same command
# succeeds instantly as root, so this is the same class of hardcoded-root-only NetworkManager
# D-Bus restriction as Settings.ReloadConnections (see ahc-wifi-install.sh's own comment), not a
# polkit-rule gap. NoNewPrivileges=yes on the main service also rules out a plain sudo call from
# inside it either way.
#
# Source lives under /var/lib/aihomecloud (a real ReadWritePaths= bind-mount both this host-
# namespace unit and the sandboxed service see), not /tmp -- aihomecloud.service runs with
# PrivateTmp=yes, so anything staged in its own /tmp is invisible here.
set -e
SAFE="$1"
SRC="/var/lib/aihomecloud/hotspot_staging/ahc-hotspot-${SAFE}.conf"

# Re-validate here, even though wifi_manager._reject_unsafe_value already refuses control
# characters upstream. This file is written by the unprivileged service user and parsed here as
# root, so its SHAPE is the security boundary and the boundary has to check itself -- a second
# writer, or a regression in the first, must not be able to change what root reads.
#
# Exactly three lines, in order: iface, ssid, password (the password line may be empty for an open
# network). A newline inside the SSID would push every later field down one, so line 3 would carry
# part of the SSID instead of the password -- the hotspot would come up OPEN while the app reported
# a password had been set. Counting lines is what detects that, so it is not optional.
[ -f "$SRC" ] || { echo "no hotspot request staged at $SRC" >&2; exit 2; }
LINES=$(wc -l < "$SRC")
if [ "$LINES" -ne 3 ]; then
    rm -f "$SRC"
    echo "malformed hotspot request: expected 3 lines, got $LINES" >&2
    exit 3
fi

IFACE=$(sed -n '1p' "$SRC")
SSID=$(sed -n '2p' "$SRC")
PASSWORD=$(sed -n '3p' "$SRC")
rm -f "$SRC"

# An interface name is chosen by the kernel, never by a user -- anything else means the file was
# not written by the code that is supposed to write it.
case "$IFACE" in
    ''|*[!A-Za-z0-9._-]*) echo "refusing implausible interface name: '$IFACE'" >&2; exit 4 ;;
esac
[ -n "$SSID" ] || { echo "refusing empty SSID" >&2; exit 5; }

nmcli connection delete ahc-hotspot >/dev/null 2>&1 || true
if [ -n "$PASSWORD" ]; then
    nmcli device wifi hotspot ifname "$IFACE" con-name ahc-hotspot ssid "$SSID" password "$PASSWORD"
else
    nmcli device wifi hotspot ifname "$IFACE" con-name ahc-hotspot ssid "$SSID"
fi
