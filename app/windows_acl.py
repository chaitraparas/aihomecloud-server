"""
Shared NTFS ACL helper for windows_identity.py / windows_cert_issuer.py.

The bash reference (ahc-issue-cert.sh / ahc-generate-identity.sh) sets file permissions as part
of publishing, in the same privileged process, at the same time -- not as a separate step, and
not something the installer can do at install time either, since these files don't exist until
the issuer creates them. This mirrors that: called by the issuer right after each atomic publish.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("aihomecloud.windows_acl")


def _icacls(path: Path, *args: str) -> None:
    result = subprocess.run(
        ["icacls", str(path), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"icacls {' '.join(args)} failed for {path}: {result.stdout} {result.stderr}"
        )


def set_exact_acl(path: Path, grants: dict[str, str]) -> None:
    """Break inheritance and set an exact ACL from `grants` (account -> icacls permission mask,
    e.g. "F" for full control, "R" for read). Nothing inherited survives, and nothing from a
    prior grant to an account not listed here survives either (/inheritance:r strips inherited
    entries; each /grant:r call only replaces *that* account's own explicit rights, so chaining
    several for different accounts is safe -- it doesn't require clearing between calls, and one
    account's grant can't be widened by a later call for a different account)."""
    _icacls(path, "/inheritance:r")
    for account, perm in grants.items():
        _icacls(path, "/grant:r", f"{account}:{perm}")
    logger.info("set exact ACL on %s: %s", path, grants)
