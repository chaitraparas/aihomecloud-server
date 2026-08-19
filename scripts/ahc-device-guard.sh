#!/bin/sh
# Authoritative block-device validation for every privileged helper that can destroy or mount one.
#
# ahc-root-input: validate
#
# THE INVARIANT THIS ENFORCES
#
#   A compromised unprivileged aihomecloud service must not be able to cause a privileged helper to
#   format, partition, overwrite, mount, or otherwise destructively operate on an arbitrary block
#   device.
#
# WHY IT LIVES HERE AND NOT IN THE API
#
# The API endpoints that normally reach these helpers already refuse OS partitions and mounted
# devices. That is not the boundary. polkit grants `systemctl start ahc-format@*` and
# `ahc-partition-format@*` to the SERVICE USER, and the unit templates pass the instance name
# straight through as the device path — so anything with code execution as that user starts the
# unit directly and never touches a single line of application code. Same shape as the sudoers file
# deleted in C-11: an OS-level capability constrained only at the application layer.
#
# So the check has to be here, in the privileged process, and it has to be authoritative — it may
# not assume any caller-side validation happened at all.
#
# ALLOWLIST, NOT DENYLIST
#
# The previous version enumerated things to refuse (the boot disk, mounted devices). This one
# defines the only set that is ever legitimate and rejects everything else:
#
#   a kernel-enumerated block device, named by its canonical /dev node, that holds no mounted
#   filesystem and no active swap anywhere in its subtree, and is not the disk backing the running
#   operating system.
#
# WHAT THE OLD VERSION GOT WRONG (kept, because the reasoning is the point)
#
#   * It never canonicalised. It matched `/dev/*` as a string, so `/dev/../dev/sda`, `/dev/./sda`
#     and `/dev/disk/by-uuid/<x>` all reached the destructive command as-is. On util-linux 2.39
#     these happened to be refused anyway, because findmnt resolves aliases internally before
#     comparing — but that is an implementation detail of one tool on one version, and correctness
#     was resting on it. Canonicalising first makes the property ours instead of borrowed.
#   * Its system-disk test compared device path STRINGS (`[ "$src" = "$dev" ]`), which cannot match
#     an alias for the same device, and used a prefix comparison (`case "$src" in "$dev"*`) — the
#     same `/srv/nas` vs `/srv/nasty` bug fixed in the Python path handling this same week.
#   * It did not cover ahc-mount-nas.sh at all, which takes a caller-controlled device and mounts
#     it. `mount` auto-attaches a loop device for a regular FILE, so the service could craft a
#     filesystem image in its own writable space and have root mount it — handing a hostile image
#     to the kernel's ext4/exFAT parsers, as root. Requiring a real block device closes that.

# --- refusal -----------------------------------------------------------------------------------
# Exit codes are distinct so tests can assert WHICH rule fired, not merely that something did.
_ahc_refuse() { echo "ahc-device-guard: $2" >&2; exit "$1"; }

# --- canonicalisation --------------------------------------------------------------------------
# Resolve to the real /dev node, or refuse. Everything downstream operates on the result, never on
# the caller's string.
ahc_canonical_block_device() {
    _arg="$1"
    [ -n "$_arg" ] || _ahc_refuse 2 "no device given"

    # realpath -e resolves symlinks and .. and fails if the target does not exist. /dev/disk/by-*
    # aliases are legitimate and resolve here to the node they name; the resolved node is what gets
    # validated, so an alias buys the caller nothing.
    _canon=$(realpath -e "$_arg" 2>/dev/null) || _ahc_refuse 2 "device does not exist: $_arg"

    # Exactly /dev/<name>: one level, no subdirectories. Rejects /dev/shm/<file> and anything else
    # nested under /dev that is not a top-level device node.
    case "$_canon" in
        /dev/*/*) _ahc_refuse 3 "not a top-level device node: $_canon (from $_arg)" ;;
        /dev/?*)  ;;
        *)        _ahc_refuse 3 "not a /dev path: $_canon (from $_arg)" ;;
    esac

    # A real block device, not a regular file (which `mount` would silently loop-attach) and not a
    # character device.
    [ -b "$_canon" ] || _ahc_refuse 4 "not a block device: $_canon"

    # And one the kernel actually enumerates — a stray node created with mknod is not a device.
    lsblk -ndo NAME "$_canon" >/dev/null 2>&1 || _ahc_refuse 4 "kernel does not enumerate: $_canon"

    printf '%s' "$_canon"
}

# --- helpers -----------------------------------------------------------------------------------
# The whole disk a node belongs to: itself if it is already a disk, else its parent.
_ahc_whole_disk() {
    _pk=$(lsblk -ndo PKNAME "$1" 2>/dev/null)
    if [ -n "$_pk" ]; then printf '/dev/%s' "$_pk"; else printf '%s' "$1"; fi
}

# Every mountpoint anywhere in the device's subtree. Tree-based, so it covers partitions without
# globbing device names, and needs no string comparison against mount sources.
_ahc_subtree_mountpoints() {
    lsblk -nro MOUNTPOINT "$1" 2>/dev/null | sed '/^$/d'
}

_ahc_subtree_has_swap() {
    for _n in $(lsblk -nro NAME "$1" 2>/dev/null); do
        if grep -q "^/dev/$_n " /proc/swaps 2>/dev/null; then return 0; fi
    done
    return 1
}

# The disk backing the running OS, resolved from the actual mount table rather than guessed.
_ahc_system_disks() {
    for _mp in / /boot /boot/efi /boot/firmware /usr /var; do
        _src=$(findmnt -no SOURCE --target "$_mp" 2>/dev/null) || continue
        [ -n "$_src" ] || continue
        case "$_src" in /dev/*) ;; *) continue ;; esac      # skip zram/overlay/tmpfs sources
        _src=$(realpath -e "$_src" 2>/dev/null) || continue
        printf '%s\n%s\n' "$_src" "$(_ahc_whole_disk "$_src")"
    done
}

_ahc_assert_not_system_disk() {
    _canon="$1"
    _canon_disk=$(_ahc_whole_disk "$_canon")
    _ahc_system_disks | sort -u | while IFS= read -r _sys; do
        [ -n "$_sys" ] || continue
        if [ "$_canon" = "$_sys" ] || [ "$_canon_disk" = "$_sys" ]; then
            echo "ahc-device-guard: refusing the system device ($_canon resolves onto $_sys)" >&2
            exit 5
        fi
    done
    # `while` runs in a subshell, so its exit does not end this one — check explicitly.
    [ $? -eq 0 ] || exit 5
}

# --- public entry points -----------------------------------------------------------------------

# For mkfs / sgdisk / anything that destroys data. Prints the canonical device on success.
ahc_require_destructive_target() {
    _canon=$(ahc_canonical_block_device "$1") || exit $?
    _ahc_assert_not_system_disk "$_canon"

    _mounts=$(_ahc_subtree_mountpoints "$_canon")
    [ -z "$_mounts" ] || _ahc_refuse 6 "in use — mounted at: $(echo "$_mounts" | tr '\n' ' ')"

    _ahc_subtree_has_swap "$_canon" && _ahc_refuse 6 "in use — active swap on $_canon"

    printf '%s' "$_canon"
}

# For mount. Same trust rules; the device must not already be mounted anywhere.
ahc_require_mount_source() {
    _canon=$(ahc_canonical_block_device "$1") || exit $?
    _ahc_assert_not_system_disk "$_canon"

    _mounts=$(_ahc_subtree_mountpoints "$_canon")
    [ -z "$_mounts" ] || _ahc_refuse 6 "already mounted at: $(echo "$_mounts" | tr '\n' ' ')"

    printf '%s' "$_canon"
}
