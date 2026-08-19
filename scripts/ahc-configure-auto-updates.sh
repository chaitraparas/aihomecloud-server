#!/usr/bin/env bash
#
# ahc-root-input: none
# Only ever `rm -f`s the legacy mirror path — never reads it. The real log lives in root-owned
# /var/log/ahc-status precisely because the old location was service-writable (C-2).
# Configure unattended security updates for an AiHomeCloud board.
#
# Idempotent and safe to re-run. Writes ONE file, /etc/apt/apt.conf.d/51ahc-auto-upgrades, and
# never edits the distribution's own 50unattended-upgrades — apt reads the directory in order, so
# a later file with `#clear` replaces a list cleanly and leaves the vendor file to be updated by
# the vendor.
#
# Why this script exists rather than a config committed to the repo: the correct configuration is
# not knowable in advance, and getting it wrong fails SILENTLY. Two real examples from this fleet:
#
#   * Package-Blacklist entries are Python REGEXES, not shell globs. A hand-written "*-firmware"
#     threw `re.error: nothing to repeat` and aborted the entire nightly run before applying
#     anything, on all three boards, indefinitely. Nothing logged to the journal, nothing visible.
#   * On Linux Mint, ${distro_codename} expands to the MINT codename ("zena") while the security
#     archive is the Ubuntu base ("noble-security"). A pattern written against ${distro_codename}
#     therefore matches nothing at all, and again applies nothing, silently.
#
# Both looked correctly configured. So this derives origins and kernel policy from what the machine
# actually reports, and prints what it decided.

set -euo pipefail

TARGET=/etc/apt/apt.conf.d/51ahc-auto-upgrades
[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Origins: which repositories may be upgraded from, discovered not assumed.
# ---------------------------------------------------------------------------
# Use whichever field actually carries the pocket, because the two distributions disagree.
#
# Debian:  a=oldstable-security  n=bookworm-security   <- a= is a MOVING alias. bookworm is
#          "oldstable" today and "oldoldstable" at the next release, at which point a pattern
#          written against a= matches nothing and the board silently stops updating.
# Ubuntu:  a=noble-security      n=noble               <- here n= is the base codename shared by
#          every pocket, so matching on n= would also sweep in -backports, which are new upstream
#          versions rather than fixes.
#
# So: prefer n= when it names the pocket, otherwise a=. Picking one field globally is wrong on one
# of the two distributions, and both failure modes are silent.
mapfile -t TRIPLES < <(apt-cache policy 2>/dev/null \
  | grep -oE 'o=[^, ]+,a=[^, ]+,n=[^, ]+' | sort -u)

ORIGINS=()
for t in "${TRIPLES[@]}"; do
  o=${t%%,*}; o=${o#o=}
  a=${t#*,a=}; a=${a%%,*}
  n=${t##*,n=}
  # Security always. Ordinary updates too: on an appliance nobody is watching, a fix that ships in
  # -updates rather than -security is still a fix, and same-release updates do not change the
  # distribution. Backports are deliberately excluded — they are newer upstream versions, not fixes.
  case "$a" in
    *-security|*-updates)
      case "$n" in
        *-security|*-updates) ORIGINS+=("\"o=${o},n=${n}\";") ;;   # Debian: pocket is in n=
        *)                    ORIGINS+=("\"o=${o},a=${a}\";") ;;   # Ubuntu/Mint: pocket is in a=
      esac
      ;;
  esac
done
# Two pockets can map to one pattern; emit each only once.
mapfile -t ORIGINS < <(printf '%s\n' "${ORIGINS[@]}" | awk '!seen[$0]++')
[[ ${#ORIGINS[@]} -gt 0 ]] || { echo "no -security/-updates archives found; refusing to write a config that would do nothing" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Kernel policy: decided by provenance, not by board name.
#
# A distribution kernel upgrading through its own archive is routine and excluding it just
# accumulates CVEs. A VENDOR kernel is a different thing: apt's kernel is not the one that boots
# the SoC, and replacing it on a headless appliance in a cupboard has no recovery path — nobody can
# reach a boot menu. A rolling/trunk channel is treated as vendor for the same reason: the upgrade
# is not "newer stable", it is "today's build".
# ---------------------------------------------------------------------------
RUNNING=$(uname -r)
KPKG=$(dpkg -S "/boot/vmlinuz-${RUNNING}" 2>/dev/null | cut -d: -f1 || true)
KERNEL_POLICY="distro"   # assume upgradable; every branch below only downgrades that

if [[ -z "$KPKG" ]]; then
  KERNEL_POLICY="vendor"      # not owned by any package: apt cannot reason about it at all
else
  POLICY=$(apt-cache policy "$KPKG" 2>/dev/null || true)
  CAND=$(sed -n 's/^ *Candidate: *//p' <<<"$POLICY" | head -1)
  # The line after "***" names the repository actually providing the installed version.
  SRC=$(grep -A1 -- '\*\*\*' <<<"$POLICY" | tail -1)
  if grep -qiE 'trunk|rolling|edge' <<<"$CAND"; then
    KERNEL_POLICY="vendor"
  elif grep -q '/var/lib/dpkg/status' <<<"$SRC"; then
    KERNEL_POLICY="vendor"    # installed locally, available from no configured repository
  # Allowlist exact distribution archive hosts. The first version matched the substring `archive.`,
  # which also matches archive.raspberrypi.org — a VENDOR repo — and would have let an unattended
  # kernel upgrade land on Pi hardware. Anything not on this list is vendor, which is the safe
  # default: holding a kernel back costs CVEs, replacing the wrong one costs a board that will not
  # boot, in a cupboard, with no console.
  elif ! grep -qiE '(^|[/.])(deb|security)\.debian\.org|(^|[/.])(archive|ports|security)\.ubuntu\.com' <<<"$SRC"; then
    KERNEL_POLICY="vendor"    # served by a third-party/vendor repository
  fi
fi

BLACKLIST=()
if [[ "$KERNEL_POLICY" == "vendor" ]]; then
  # Anchored regexes. NOT globs — see the header.
  BLACKLIST+=('"^linux-image";' '"^linux-headers";' '"^linux-dtb";' '"^u-boot";'
              '"-firmware$";' '"^rockchip";' '"^radxa";' '"^armbian";')
fi

# ---------------------------------------------------------------------------
{
  echo "// Generated by ahc-configure-auto-updates.sh on $(date -Is). Re-run to refresh."
  echo "// Running kernel: ${RUNNING} (package: ${KPKG:-none}) -> policy: ${KERNEL_POLICY}"
  echo
  echo 'APT::Periodic::Update-Package-Lists "1";'
  echo 'APT::Periodic::Unattended-Upgrade "1";'
  echo
  echo '#clear Unattended-Upgrade::Origins-Pattern;'
  echo 'Unattended-Upgrade::Origins-Pattern {'
  printf '    %s\n' "${ORIGINS[@]}"
  echo '};'
  echo
  echo '#clear Unattended-Upgrade::Package-Blacklist;'
  echo 'Unattended-Upgrade::Package-Blacklist {'
  [[ ${#BLACKLIST[@]} -gt 0 ]] && printf '    %s\n' "${BLACKLIST[@]}"
  echo '};'
  echo
  # Restart services whose libraries were replaced, so a patched openssl actually takes effect in
  # the running backend instead of waiting for someone to reboot a machine nobody reboots.
  echo 'Unattended-Upgrade::Automatic-Reboot "false";'
  echo 'Unattended-Upgrade::MinimalSteps "true";'
  echo 'Unattended-Upgrade::Remove-Unused-Kernel-Packages "false";'
} > "$TARGET"

# ---------------------------------------------------------------------------
# M-9 fix (security audit 2026-08): needrestart, when installed, restarts any daemon whose
# loaded libraries an upgrade just replaced -- with zero awareness of app/workload.py's "user
# activity always wins" rule. A person mid-upload has no say in whether unattended-upgrades
# decides *right now* is when aihomecloud gets bounced. That is the actual gap the finding
# names ("outside workload.py's reach"), distinct from the reboot/kernel risk already closed
# above.
#
# Fix mirrors this script's own established philosophy for the same problem class (the kernel/
# bootloader Package-Blacklist above): exclude aihomecloud from automatic restart entirely,
# rather than building a live coordination channel between needrestart and the running app's
# in-memory busy state. That channel would be real, working infrastructure this script cannot
# verify from a one-shot run anyway (it would need its own test, its own failure mode when the
# app is down, etc.) for a benefit -- a patched shared library taking effect sooner -- that isn't
# worth trading against "never get killed mid-transfer". A held-back restart is applied the next
# time the service restarts for any other reason (deploy, reboot, manual).
#
# Deliberately scoped to just this one service: needrestart still restarts everything else on
# the box normally, so this only changes behaviour for the one process whose disruption cost is
# actually described in the finding.
if command -v needrestart >/dev/null 2>&1; then
  NR_DROPIN_DIR=/etc/needrestart/conf.d
  mkdir -p "$NR_DROPIN_DIR"
  cat > "$NR_DROPIN_DIR/51-ahc-no-auto-restart.conf" <<'EOF'
# Generated by ahc-configure-auto-updates.sh (M-9). Additive to the stock $nrconf{override_rc}
# hash -- see the conf.d loop at the bottom of /etc/needrestart/needrestart.conf.
$nrconf{override_rc}{qr(^aihomecloud)} = 0;
EOF
  echo "needrestart : aihomecloud excluded from auto-restart (wrote $NR_DROPIN_DIR/51-ahc-no-auto-restart.conf)"
else
  echo "needrestart : not installed on this board, nothing to configure"
fi

# The failure this whole script exists to prevent: a pattern that does not compile silently aborts
# every run. Prove it here, at write time, rather than discovering it months later.
python3 - "$TARGET" <<'PY'
import re, sys
bad = []
for line in open(sys.argv[1]):
    s = line.strip()
    if s.startswith('"') and s.endswith('";') and 'o=' not in s:
        pat = s[1:-2]
        try:
            re.compile(pat)
        except re.error as e:
            bad.append((pat, str(e)))
if bad:
    for p, e in bad:
        print(f"  INVALID REGEX {p!r}: {e}", file=sys.stderr)
    sys.exit(1)
PY

# Let the backend read the run log, without granting it the run of the system logs.
#
# /var/log/unattended-upgrades is root:adm 0750, so the service user cannot traverse it. The
# options were: add it to `adm` (read across the whole system log estate — far more than "did last
# night succeed" needs), chmod the directory world-readable (worse), or install `acl` for a scoped
# ACL (a new package, and `acl` is not present on these images).
#
# Instead a systemd drop-in mirrors the log tail into the service's own data directory. Crucially
# it uses ExecStopPost, which runs whether the upgrade SUCCEEDED OR FAILED — an
# Unattended-Upgrade::Post-Invoke hook would be skipped by exactly the early crash this is meant to
# surface, leaving a stale timestamp that reads like success.
SVC_USER=aihomecloud
# The mirror lives in a ROOT-OWNED directory, and that is the whole point.
#
# The first version wrote it to /var/lib/aihomecloud/auto-update.log — a directory the service user
# owns — from a root ExecStopPost that did `> $MIRROR` followed by chown. A compromised backend
# could replace that path with a symlink to /etc/shadow and root would truncate the file and hand
# ownership of it to the service user. Full root, no preconditions. Writing into a directory the
# unprivileged user cannot modify removes the swap entirely; 0750 root:$SVC_USER still lets the
# backend read it.
MIRROR_DIR=/var/log/ahc-status
MIRROR="$MIRROR_DIR/auto-update.log"
LEGACY_MIRROR=/var/lib/aihomecloud/auto-update.log

if id "$SVC_USER" &>/dev/null; then
  install -d -o root -g "$SVC_USER" -m 0750 "$MIRROR_DIR"
  # Remove the old attackable path, whatever it currently is (file or symlink).
  rm -f "$LEGACY_MIRROR"

  DROPIN=/etc/systemd/system/apt-daily-upgrade.service.d
  mkdir -p "$DROPIN"
  cat > "$DROPIN/50-ahc-status.conf" <<EOF
# Generated by ahc-configure-auto-updates.sh — mirrors the run log where AiHomeCloud can read it.
# Target directory is root-owned on purpose: the service user must not be able to substitute a
# symlink here, because this runs as root.
[Service]
ExecStopPost=/bin/sh -c 'install -o root -g ${SVC_USER} -m 0640 /dev/null ${MIRROR} 2>/dev/null; tail -c 20000 /var/log/unattended-upgrades/unattended-upgrades.log > ${MIRROR} 2>/dev/null; true'
EOF
  systemctl daemon-reload 2>/dev/null || true
  install -o root -g "$SVC_USER" -m 0640 /dev/null "$MIRROR" 2>/dev/null || true
  tail -c 20000 /var/log/unattended-upgrades/unattended-upgrades.log > "$MIRROR" 2>/dev/null || true
  echo "  log access: mirrored to ${MIRROR} (root-owned dir, ${SVC_USER}-readable)"
fi

echo "wrote $TARGET"
echo "  kernel   : ${RUNNING} -> ${KERNEL_POLICY}$([[ $KERNEL_POLICY == vendor ]] && echo ' (kernel/bootloader held back)' || echo ' (kernel upgrades allowed)')"
echo "  origins  : ${#ORIGINS[@]}"
printf '    %s\n' "${ORIGINS[@]}"
