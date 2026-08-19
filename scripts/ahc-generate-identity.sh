#!/usr/bin/env bash
#
# ahc-root-input: none
# Generates this board's long-lived Ed25519 identity key, once. The identity key signs rotation
# statements (H-11) and is never used for TLS — clients pin it, not the TLS key, so a legitimate
# certificate reissue no longer breaks pairing. See
# docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md §1 for the full trust-boundary table.
#
# Idempotent: run again on an existing board and it does nothing, so install.sh can call this
# unconditionally on every run without regenerating (and thereby invalidating) an established
# identity.
#
# Root-only, no arguments, no external input — the "ahc-root-input: none" marker matches
# ahc-apply-maintenance-window.sh's precedent for scripts that take no request file at all.

set -euo pipefail

IDENTITY_DIR=/var/lib/aihomecloud/identity
PRIVATE_KEY="$IDENTITY_DIR/identity.key"
PUBLIC_KEY="$IDENTITY_DIR/identity.pub"
EPOCH_FILE="$IDENTITY_DIR/epoch"

[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

if [[ -f "$PRIVATE_KEY" && -f "$PUBLIC_KEY" && -f "$EPOCH_FILE" ]]; then
  echo "identity already exists at $IDENTITY_DIR, leaving it alone"
  exit 0
fi

mkdir -p "$IDENTITY_DIR"
# 711 (rwx--x--x), not 700: GET /system/identity is served by the unprivileged aihomecloud
# service reading statement.json directly out of this directory. Directory *execute* permission
# is what lets a non-owner open a file inside by exact path -- *read* permission on the directory
# (which 711 withholds from others) only controls whether its contents can be listed. identity.key
# stays protected regardless, by its own 0600 file mode: opening it by exact path still requires
# read permission on the file itself, which 711-on-the-directory does not grant. Found live
# 2026-08-13 -- with 700, `sudo -u aihomecloud cat statement.json` was Permission denied even
# though the file itself was 0644, and the endpoint 500'd for exactly that reason.
chmod 711 "$IDENTITY_DIR"

# Ed25519 keypair via openssl (present on every board already — no new dependency). Written to a
# temp path in the same directory first so a crash mid-write can never leave a partial key that
# looks valid, then renamed into place atomically.
TMP_PRIVATE="$PRIVATE_KEY.tmp.$$"
TMP_PUBLIC="$PUBLIC_KEY.tmp.$$"
trap 'rm -f "$TMP_PRIVATE" "$TMP_PUBLIC"' EXIT

openssl genpkey -algorithm ed25519 -out "$TMP_PRIVATE" 2>/dev/null
openssl pkey -in "$TMP_PRIVATE" -pubout -out "$TMP_PUBLIC" 2>/dev/null

chmod 600 "$TMP_PRIVATE"
chmod 644 "$TMP_PUBLIC"
chown root:root "$TMP_PRIVATE" "$TMP_PUBLIC"

mv "$TMP_PRIVATE" "$PRIVATE_KEY"
mv "$TMP_PUBLIC" "$PUBLIC_KEY"

# Epoch starts at 0 and only ever increments (ahc-issue-cert.sh), persisted alongside the key so a
# reinstall regenerates both together — a reinstalled board's epoch must not be reachable by replaying
# a statement signed under the old identity, and it isn't, because the old identity is gone too.
echo 0 > "$EPOCH_FILE.tmp.$$"
chmod 644 "$EPOCH_FILE.tmp.$$"
chown root:root "$EPOCH_FILE.tmp.$$"
mv "$EPOCH_FILE.tmp.$$" "$EPOCH_FILE"

echo "generated board identity at $IDENTITY_DIR"
