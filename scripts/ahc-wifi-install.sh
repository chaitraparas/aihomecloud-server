#!/bin/sh
#
# ahc-root-input: validate
# Installs a service-authored NetworkManager keyfile as root, so root re-validates its structure
# first: an allowlist of the exact sections and keys we emit, refusing anything else. A newline
# in the SSID would otherwise inject arbitrary keys into root-owned config. (C-9)
# Runs OUTSIDE the aihomecloud sandbox (host namespace, as root, via the
# ahc-wifi-install@ oneshot unit) -- NoNewPrivileges=yes on the main service
# blocks sudo/setuid entirely regardless of sudoers grants (found live
# 2026-07-14: "sudo: effective uid is not 0" on every attempt), the same
# reason ahc-mount-nas.sh/ahc-umount-nas.sh exist for storage instead of a
# sudo call from inside the service.
#
# Source lives under /var/lib/aihomecloud, not /tmp: aihomecloud.service runs with
# PrivateTmp=yes, so a file staged in the service's own /tmp is invisible outside its
# private mount namespace -- this host-namespace unit would see "No such file or
# directory" for a real, just-written file (found live 2026-07-14). /var/lib/aihomecloud
# is one of the service's ReadWritePaths= entries, a genuine bind-mount both sides see.
#
# Also runs `nmcli connection reload` here rather than from wifi_manager.py: found live
# 2026-07-14 that NetworkManager's Settings.ReloadConnections is hardcoded root-only at
# the D-Bus policy level (confirmed via upstream bug 1921082), not a polkit-gated action
# -- no .pkla/.rules grant for org.freedesktop.NetworkManager.{reload,settings.modify.
# system} can ever authorize it for a non-root caller. This script already runs as root,
# so it does the reload itself immediately after installing the profile.
set -e
SAFE="$1"
SRC="/var/lib/aihomecloud/wifi_setup_staging/ahc-wifi-${SAFE}.nmconnection"
DEST="/etc/NetworkManager/system-connections/${SAFE}.nmconnection"
# Root is about to install this file as 0600 root:root system configuration, and the file was
# written by the unprivileged service user -- so check its shape before trusting it. The values
# (SSID, passphrase) are the user's and cannot be re-derived here, but the STRUCTURE can be
# checked, and structure is what an injected newline attacks: wifi_manager builds this keyfile by
# interpolating the SSID and passphrase verbatim, so before those values were validated a newline
# in either one could add arbitrary sections and keys to a root-owned NetworkManager profile.
#
# Allowlist, not denylist: every non-blank line must be a section header we emit or a key we emit.
# Anything else means the file is not the file wifi_manager.connect_to_network writes, and the
# right answer to that is to refuse rather than to guess which part is legitimate.
# (2026-08-08 audit, root-consumes-service-writable-path sweep.)
[ -f "$SRC" ] || { echo "no profile staged at $SRC" >&2; exit 2; }
_ALLOWED='^\[(connection|wifi|wifi-security|ipv4|ipv6)\]$|^(id|uuid|type|autoconnect|mode|ssid|key-mgmt|psk|method|addr-gen-mode)=|^[[:space:]]*$'
if grep -nEv "$_ALLOWED" "$SRC" >&2; then
    rm -f "$SRC"
    echo "refusing to install profile: unexpected content above" >&2
    exit 3
fi

install -m 600 -o root -g root "$SRC" "$DEST"
rm -f "$SRC"
nmcli connection reload
