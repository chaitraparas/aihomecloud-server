#!/bin/sh
# Runs OUTSIDE the aihomecloud sandbox (host namespace, real root) — same reason as
# ahc-partition-format-nas.sh. $1 = an already-existing partition device path. Label comes
# from $AHC_FORMAT_LABEL (passed via `systemctl start --setenv=`, see format_partition() in
# storage_helpers.py), defaulting to "AiHomeCloud" if unset.
set -e

# Mandatory: the device validation below IS the privilege boundary. If the guard is missing we
# refuse to run at all rather than fall through to an unvalidated mkfs/mount.
. /usr/local/bin/ahc-device-guard.sh || { echo "ahc-device-guard.sh missing — refusing" >&2; exit 90; }


DEV=$(ahc_require_destructive_target "$1")
mkfs.ext4 -F -L "${AHC_FORMAT_LABEL:-AiHomeCloud}" "$DEV"
