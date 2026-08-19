#!/bin/sh
# Runs OUTSIDE the aihomecloud sandbox (host mount namespace) so the mount
# genuinely affects the real, system-wide mount table.
#
# Bare `mount`, not /usr/bin/mount: found live 2026-07-14 on a Debian 11 board without the
# usr-merge (/usr/bin and /bin are still separate directories there) -- mount actually lives at
# /bin/mount, so the hardcoded /usr/bin/mount path failed with "No such file or directory" (exit
# 127) on exec. Bare `mount` resolves via systemd's own service PATH, which includes both
# directories regardless of whether a given board has usr-merge or not.
#
# Also chowns the mountpoint to aihomecloud:aihomecloud after mounting -- found live 2026-07-14:
# a freshly-mounted drive's root inode ownership comes from whatever it was formatted/last-used
# with (root, here), and the aihomecloud service account has no CAP_CHOWN to fix that itself from
# inside its own sandbox even though ReadWritePaths=/srv/nas makes the path visible there --
# every post-mount step (mkdir personal/family/entertainment, uploads, media indexing) failed
# with a plain PermissionError until this ran as root, in the host namespace, right here. Matches
# the exact chown -R "$APP_USER:$APP_USER" pattern install.sh's own create_directories() already
# uses for the non-NVMe (SD-card) case -- this is that same established pattern, extended to the
# one path that was missing it.
# ahc-root-input: validate
set -e

# Mandatory: the device validation below IS the privilege boundary. If the guard is missing we
# refuse to run at all rather than fall through to an unvalidated mkfs/mount.
. /usr/local/bin/ahc-device-guard.sh || { echo "ahc-device-guard.sh missing — refusing" >&2; exit 90; }

# nosuid,nodev on every data mount, always.
#
# These are data drives — a USB disk or NVMe someone plugged in — and nothing on them should ever
# be trusted to confer privilege. Without nosuid, a filesystem carrying a setuid-root binary hands
# root to anyone who can execute a file on it the moment this script mounts it, and mounting
# removable media is the entire purpose of this helper. nodev likewise stops a crafted image from
# shipping its own device nodes (a writable /dev/sda, say) and bypassing every path check above it.
#
# Neither option costs anything real here: a NAS volume holding photos and documents has no
# legitimate use for setuid bits or device nodes. Generic VFS options, so exFAT/NTFS/ext4 all take
# them. (2026-08-08 audit — the mount endpoint is admin-gated and validates the device against the
# real block-device list, so this is defence in depth, not a live hole.)
# The device is caller-controlled via the ahc-mount@ / ahc-mount-backup@ instance name.
# Validate it as a real, idle block device before handing it to the kernel: `mount` loop-attaches a
# regular FILE, so without this the service could craft a filesystem image in its own writable
# space and have root feed it to the ext4/exFAT parsers.
DEV=$(ahc_require_mount_source "$1")
mount -o nosuid,nodev "$DEV" "$2"
# Soft-fail from here: some filesystems (exFAT, FAT32) don't support real UNIX ownership at all
# -- chown on them always returns EPERM regardless of caller privilege, filesystem type aside.
# Found live 2026-07-15 on the Cubie A5E: with `set -e` a chown failure aborted the whole script
# AFTER the mount had already succeeded, so the API caller saw this whole unit report failure and
# never persisted mount state, while the drive stayed mounted underneath -- every retry after
# that then failed differently ("already mounted"), permanently stuck with no path to recover
# short of a manual unmount. A failed chown is now a warning, not fatal; callers that actually
# need real ownership (ext4) still get it and can check for it themselves.
chown -R aihomecloud:aihomecloud "$2" || echo "ahc-mount-nas.sh: chown failed on $2 (filesystem may not support UNIX ownership) -- continuing" >&2
