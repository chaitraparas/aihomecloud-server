#!/usr/bin/env bash
#
# ahc-root-input: validate
# Generates the TLS key + certificate, signs a rotation statement over it with the board's
# identity key, and publishes all three atomically. This is the load-bearing piece of H-11 — see
# docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md §2. Steps 3-4 of that design (Android
# trusting a statement) are theatre without this script upholding two properties:
#
#   1. The service supplies *values* (a reissue reason), never *structure* and never key
#      material. IP SANs are derived here from the live interfaces, never trusted from the
#      request file. Hostname is re-validated against RFC 1123 here, never coerced.
#   2. Statement and certificate can never disagree, because this one process produces both from
#      the same key and publishes the statement last (write-to-temp, rename: key, cert, statement
#      — in that order). A torn publish is caught by clients (condition 4 in the design) but must
#      not be able to happen from this side either.
#
# Triggered by ahc-issue-cert.path watching /var/lib/aihomecloud/cert-request.json, same
# file-watch pattern as ahc-apply-device-name.path.

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

DATA_DIR=/var/lib/aihomecloud
REQUEST="$DATA_DIR/cert-request.json"
IDENTITY_DIR="$DATA_DIR/identity"
PRIVATE_IDENTITY_KEY="$IDENTITY_DIR/identity.key"
PUBLIC_IDENTITY_KEY="$IDENTITY_DIR/identity.pub"
EPOCH_FILE="$IDENTITY_DIR/epoch"
TLS_DIR="$DATA_DIR/tls"
CERT_PATH="$TLS_DIR/cert.pem"
KEY_PATH="$TLS_DIR/key.pem"
STATEMENT_PATH="$IDENTITY_DIR/statement.json"

[[ -f "$PRIVATE_IDENTITY_KEY" ]] || { echo "no identity key at $PRIVATE_IDENTITY_KEY — run ahc-generate-identity.sh first" >&2; exit 2; }
[[ -f "$EPOCH_FILE" ]] || { echo "no epoch file at $EPOCH_FILE" >&2; exit 2; }

# The request file only tells us *why* we were asked to reissue. It is read for logging, not
# trusted for any value that ends up in the certificate — everything the cert actually names is
# derived below from the live system, independent of what the request claims.
REASON="unspecified"
if [[ -f "$REQUEST" ]]; then
  REASON=$(python3 -c "import json,sys
try:
    print(json.load(open(sys.argv[1])).get('reason', 'unspecified'))
except Exception:
    print('unspecified')" "$REQUEST" 2>/dev/null || echo "unspecified")
fi
# Strip control characters (incl. newlines) before logging -- the service chose this string, and
# an embedded newline could forge an adjacent-looking journal line. Not exploitable for command
# injection (this only ever reaches a double-quoted echo, never re-evaluated), but cheap to close.
REASON=$(printf '%s' "$REASON" | tr -d '\000-\037')
echo "issuing certificate — reason: $REASON"

mkdir -p "$TLS_DIR"
chmod 755 "$TLS_DIR"
# The service (aihomecloud:aihomecloud) owns $DATA_DIR and everything install.sh pre-creates
# under it, including this directory -- ownership was never handed back to root, which meant the
# service could delete/replace cert.pem/key.pem directly via directory-level write access even
# though the files themselves were correctly root-owned. Doesn't defeat H-11's actual invariant
# (identity/ stays genuinely root:root, so a service-planted rogue cert has no matching
# identity-signed statement and both a paired client and a fresh TOFU client reject it per design
# §4), but it hands the service self-sabotage/DoS capability over TLS material this design exists
# to keep it away from. (2026-08-13 security review.)
chown root:root "$TLS_DIR"

# --- Re-derive hostname + SANs from the live system (never from the request) ---
HOSTNAME=$(hostnamectl --static 2>/dev/null || hostname)
[[ "$HOSTNAME" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { echo "unusable hostname: '$HOSTNAME'" >&2; exit 3; }

# `ip` (iproute2), not psutil: this script runs as root via systemd, outside the aihomecloud
# venv where psutil is actually installed -- root's system python3 has no reason to have it, and
# in practice doesn't. That silently sent this through app/tls.py's *already-fixed* single-IP
# fallback (a bare UDP-socket trick landing on whichever interface owns the default route), so a
# multi-homed board (Ethernet + Wi-Fi + Tailscale, e.g. Cubie A5E) got a certificate covering only
# one of them and instantly needed another reissue on the very next boot -- an infinite
# reissue-restart loop, found live 2026-08-13 rolling this out to Cubie A5E. `ip` needs no
# language runtime or package at all, so this can't regress the same way again by drifting out of
# sync with whatever happens to be in the venv.
IPS=$(ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | sort -u | tr '\n' ' ')
[[ -z "${IPS// }" ]] && IPS="127.0.0.1"

SAN="DNS:${HOSTNAME},DNS:${HOSTNAME}.local,DNS:localhost"
for ip in $IPS; do
  SAN="${SAN},IP:${ip}"
done

# --- Generate a fresh key + certificate. Deliberately NOT reusing the old key: under this design
# the identity key provides pinning continuity, so the TLS key can (and per the design, should)
# rotate on every reissue — see the design doc's "Key continuity note" (§2). Written to temp paths
# and only renamed into place once the whole batch (key, cert, statement) is ready to publish. ---
TMP_KEY="$KEY_PATH.tmp.$$"
TMP_CERT="$CERT_PATH.tmp.$$"
trap 'rm -f "$TMP_KEY" "$TMP_CERT" "$STATEMENT_PATH.tmp.$$" "$STATEMENT_PATH.signinput.$$"' EXIT

openssl req -x509 -newkey rsa:2048 -keyout "$TMP_KEY" -out "$TMP_CERT" \
  -days 365 -nodes \
  -subj "/CN=${HOSTNAME}/O=AiHomeCloud" \
  -addext "subjectAltName=${SAN}" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth" 2>&1

openssl x509 -in "$TMP_CERT" -noout >/dev/null 2>&1 || { echo "generated certificate is unreadable" >&2; exit 4; }

# --- Compute SHA-256(SubjectPublicKeyInfo) of the certificate we just produced, not of anything
# the request supplied — this is what binds the statement to this specific certificate. ---
SPKI=$(openssl x509 -in "$TMP_CERT" -pubkey -noout \
  | openssl pkey -pubin -outform DER \
  | openssl dgst -sha256 -binary \
  | base64 -w0 2>/dev/null || openssl x509 -in "$TMP_CERT" -pubkey -noout \
  | openssl pkey -pubin -outform DER \
  | openssl dgst -sha256 -binary \
  | base64)

SERIAL=$(systemctl show aihomecloud.service -p Environment --value 2>/dev/null \
  | tr ' ' '\n' | grep '^AHC_DEVICE_SERIAL=' | cut -d= -f2-)
[[ -n "$SERIAL" ]] || { echo "could not read AHC_DEVICE_SERIAL from the service unit" >&2; exit 5; }

OLD_EPOCH=$(cat "$EPOCH_FILE")
NEW_EPOCH=$((OLD_EPOCH + 1))
NOT_BEFORE=$(date +%s)

# --- Build the canonical statement and sign it. Canonical encoding is pinned exactly as the
# design specifies (sort_keys, no whitespace) — a formatting drift here silently invalidates every
# signature already accepted in the field, which is why test_spki_rotation.py asserts these exact
# bytes for a known input. ---
STATEMENT_JSON=$(python3 -c "
import json, sys
statement = {
    'spki': sys.argv[1],
    'serial': sys.argv[2],
    'notBefore': int(sys.argv[3]),
    'epoch': int(sys.argv[4]),
}
print(json.dumps(statement, sort_keys=True, separators=(',', ':')))
" "$SPKI" "$SERIAL" "$NOT_BEFORE" "$NEW_EPOCH")

# pkeyutl's -rawin mode cannot determine input size from a pipe ("unable to determine file size
# for oneshot operation") on this openssl build — write the exact bytes being signed to a real
# file first. Cleaned up in the exit trap along with the other temp files.
TMP_STATEMENT_BYTES="$STATEMENT_PATH.signinput.$$"
printf '%s' "$STATEMENT_JSON" > "$TMP_STATEMENT_BYTES"
SIGNATURE=$(openssl pkeyutl -sign -inkey "$PRIVATE_IDENTITY_KEY" -rawin -in "$TMP_STATEMENT_BYTES" \
  | base64 -w0 2>/dev/null || openssl pkeyutl -sign -inkey "$PRIVATE_IDENTITY_KEY" -rawin -in "$TMP_STATEMENT_BYTES" \
  | base64)
rm -f "$TMP_STATEMENT_BYTES"

IDENTITY_PUB_B64=$(openssl pkey -in "$PUBLIC_IDENTITY_KEY" -pubin -pubout -outform DER 2>/dev/null \
  | base64 -w0 2>/dev/null || openssl pkey -in "$PUBLIC_IDENTITY_KEY" -pubin -pubout -outform DER \
  | base64)

TMP_STATEMENT="$STATEMENT_PATH.tmp.$$"
python3 -c "
import json, sys
statement = json.loads(sys.argv[1])
envelope = {
    'identityPublicKey': sys.argv[2],
    'statement': statement,
    'signature': sys.argv[3],
}
with open(sys.argv[4], 'w') as f:
    json.dump(envelope, f, indent=2)
" "$STATEMENT_JSON" "$IDENTITY_PUB_B64" "$SIGNATURE" "$TMP_STATEMENT"

# --- Publish atomically, key then cert then statement — exactly the order in the design, so a
# client can never observe a statement naming a certificate that is not yet live, or a live
# certificate with no statement covering it. ---
chmod 640 "$TMP_KEY"
chown root:aihomecloud "$TMP_KEY"
chmod 644 "$TMP_CERT" "$TMP_STATEMENT"
chown root:root "$TMP_CERT" "$TMP_STATEMENT"

mv "$TMP_KEY" "$KEY_PATH"
mv "$TMP_CERT" "$CERT_PATH"
mv "$TMP_STATEMENT" "$STATEMENT_PATH"

echo "$NEW_EPOCH" > "$EPOCH_FILE.tmp.$$"
chmod 644 "$EPOCH_FILE.tmp.$$"
chown root:root "$EPOCH_FILE.tmp.$$"
mv "$EPOCH_FILE.tmp.$$" "$EPOCH_FILE"

rm -f "$REQUEST"

systemctl try-restart aihomecloud 2>/dev/null || true

echo "issued certificate for ${HOSTNAME} — epoch ${OLD_EPOCH} -> ${NEW_EPOCH}, spki=${SPKI:0:12}..."
