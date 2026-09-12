#!/usr/bin/env python3
"""
Safely extract a caller-staged backend update bundle, as root.

ahc-apply-backend-update.sh runs entirely as root against a tar an admin-authenticated but
still untrusted caller uploaded (see that script's own header). Its previous guard only
rejected entry NAMES containing ".." or a leading "/" (via `tar -tf`), which never inspects
a symlink or hardlink entry's *target*. Empirically verified against GNU tar 1.35 (the
version on the boards, Ubuntu 24.04): tar itself sanitizes absolute/".."-prefixed hardlink
targets and refuses to write a member through an existing symlink used as a directory — but
it does NOT sanitize a symlink entry's target at all. A tar entry named e.g. "app/x.py" with
a clean name and linkname "../../../etc/whatever" extracts cleanly, planting a symlink node
that resolves outside the release directory, undetected by the old bash guard.

`tarfile.data_filter` (stdlib, PEP 706, Python 3.12+) closes this without hand-rolled path
logic: for every member it resolves the destination path AND, for symlinks/hardlinks, the
link target, and rejects anything that would land outside `dest` -- across the ".."-traversal,
absolute-path, symlink, and hardlink cases the old check missed, independent of which `tar`
binary later extracts it.

Usage: python3 extract_update_bundle.py <tar_path> <dest_dir>
Exit:  0 extracted cleanly. 1 unsafe or invalid bundle -- nothing is left half-extracted.
"""

from __future__ import annotations

import shutil
import sys
import tarfile


def safe_extract(tar_path: str, dest_dir: str) -> None:
    """Raises tarfile.TarError (or a subclass) if the bundle is unsafe or malformed."""
    with tarfile.open(tar_path) as tf:
        tf.extractall(dest_dir, filter=tarfile.data_filter)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <tar_path> <dest_dir>", file=sys.stderr)
        return 2

    tar_path, dest_dir = argv[1], argv[2]
    try:
        safe_extract(tar_path, dest_dir)
    except tarfile.TarError as exc:
        # Leave nothing half-extracted for the caller to accidentally trust or clean up wrong.
        shutil.rmtree(dest_dir, ignore_errors=True)
        print(f"rejected update bundle: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
