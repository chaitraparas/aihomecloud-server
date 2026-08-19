#!/bin/sh
# Runs OUTSIDE the aihomecloud sandbox (host namespace, real root) so it can open the
# raw block device — see partition_and_format_device()'s docstring in storage_helpers.py
# for why a direct sgdisk/mkfs.ext4 subprocess call from inside the service cannot do this.
#
# $1 = whole-disk device path, e.g. /dev/nvme0n1. Wipes it, creates one GPT partition
# spanning the whole disk, and mkfs.ext4's it with label "AiHomeCloud" — matches
# smart_activate()'s format=True contract exactly (see storage_routes.py).
set -e

# Mandatory: the device validation below IS the privilege boundary. If the guard is missing we
# refuse to run at all rather than fall through to an unvalidated mkfs/mount.
. /usr/local/bin/ahc-device-guard.sh || { echo "ahc-device-guard.sh missing — refusing" >&2; exit 90; }


DISK=$(ahc_require_destructive_target "$1")
sgdisk -Z "$DISK"
sgdisk -n 1:0:0 -t 1:8300 "$DISK"
udevadm settle --timeout=5
PARTITION="${DISK}1"
case "$DISK" in
    *[0-9]) PARTITION="${DISK}p1" ;;
esac
mkfs.ext4 -F -L AiHomeCloud "$PARTITION"
