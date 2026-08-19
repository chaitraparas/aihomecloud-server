#!/usr/bin/env bash
#
# ahc-root-input: validate
# Reads the requested hostname from device-name.json and re-validates it against RFC 1123 here,
# refusing rather than coercing — a name that does not match is a name we do not set.
# Make the board answer to the name the family gave it: <name>.local
#
# The whole point is that nobody should type an IP address. Avahi already answers for the system
# hostname, so the cheapest correct fix is to make the system hostname follow the device name the
# user set in the app — no extra daemon, no alias publisher, and it works from every OS that speaks
# mDNS (macOS, Windows 10+, Linux, iOS; Android resolves it in-app via NsdManager).
#
# Takes NO arguments, same reasoning as ahc-apply-maintenance-window: the sudoers policy allows
# exact commands with no wildcards, so the value arrives via a file the service can already write
# and is re-validated here. A hostname reaches /etc/hosts and hostnamectl as root; it is untrusted
# input no matter who wrote it.

set -euo pipefail

REQUEST=/var/lib/aihomecloud/device-name.json
[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
[[ -f "$REQUEST" ]] || { echo "no name requested at $REQUEST" >&2; exit 2; }

SLUG=$(python3 - "$REQUEST" <<'PY'
import json, re, sys
try:
    name = json.load(open(sys.argv[1])).get("name", "")
except Exception:
    sys.exit(3)
slug = re.sub(r"[^a-z0-9-]", "-", str(name).strip().lower())
slug = re.sub(r"-+", "-", slug).strip("-")[:31]
print(slug)
PY
) || { echo "unreadable request" >&2; exit 3; }

# RFC 1123: letters, digits, hyphens; must start alphanumeric. Refused, never coerced — a name that
# does not match is a name we do not set, rather than one we quietly turn into something else.
[[ "$SLUG" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || { echo "unusable hostname: '$SLUG'" >&2; exit 4; }

CURRENT=$(hostnamectl --static 2>/dev/null || hostname)
if [[ "$SLUG" != "$CURRENT" ]]; then
  hostnamectl set-hostname "$SLUG"
fi

# Without this, sudo prints "unable to resolve host" on every invocation and can add a multi-second
# delay to each call — a real and confusing regression from what looks like a cosmetic rename.
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${SLUG}/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "$SLUG" >> /etc/hosts
fi

# Pin the name Avahi advertises instead of letting it follow the system hostname implicitly.
#
# mDNS requires a host to rename itself on collision — two boards both called "cubie" and the
# second becomes cubie-2.local. The certificate names cubie.local, so the board would then be
# reachable under a name its own certificate does not cover: a NAME MISMATCH, which is a harder
# failure than the untrusted warning and one some clients will not let anyone past. Pinning does
# not repeal the protocol, but it makes the intended name explicit and stops drift from an
# unrelated hostname change; ensure_tls_cert re-checks coverage on every start and reissues.
if [[ -f /etc/avahi/avahi-daemon.conf ]]; then
  if grep -qE '^[#[:space:]]*host-name=' /etc/avahi/avahi-daemon.conf; then
    sed -i "s/^[#[:space:]]*host-name=.*/host-name=${SLUG}/" /etc/avahi/avahi-daemon.conf
  else
    sed -i "/^\[server\]/a host-name=${SLUG}" /etc/avahi/avahi-daemon.conf
  fi
fi

systemctl restart avahi-daemon 2>/dev/null || true

# The TLS certificate names the OLD hostname until ensure_tls_cert runs again, and that only happens
# at startup. Without this the board answers to its new name with a certificate that does not cover
# it — a NAME MISMATCH, which is harder than the usual untrusted warning and unclickable on some
# clients. Safe here: this runs from a systemd path unit, well after the rename request returned.
if [[ "$SLUG" != "$CURRENT" ]]; then
  systemctl try-restart aihomecloud 2>/dev/null || true
fi

# Report the name Avahi actually settled on. If it differs from what we asked for, something else
# on the network already owns it and the certificate will not match — better said out loud here
# than discovered by a family whose browser suddenly refuses to connect.
sleep 1
# Three outcomes, kept distinct on purpose. Collapsing "cannot tell" into "failed" produced a
# confident false alarm on the first run here: avahi-utils is not installed on these images, so the
# check could not run at all and reported a name conflict that did not exist.
if ! command -v avahi-resolve >/dev/null; then
  echo "hostname=${SLUG} — reachable as ${SLUG}.local (not verified: avahi-utils not installed)"
elif avahi-resolve -n "${SLUG}.local" >/dev/null 2>&1; then
  echo "hostname=${SLUG} — reachable as ${SLUG}.local"
else
  echo "hostname=${SLUG} — WARNING: ${SLUG}.local did not resolve; another device may own it" >&2
  echo "hostname=${SLUG}"
fi
