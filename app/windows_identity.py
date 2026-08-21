"""
Windows port of scripts/ahc-generate-identity.sh — generates this board's long-lived Ed25519
identity key, once. See docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md §1.

The identity key signs rotation statements (windows_cert_issuer.py) and is never used for TLS —
clients pin it, not the TLS key, so a legitimate certificate reissue does not break pairing.

Idempotent by design, matching the bash original: safe to call unconditionally on every install
run without regenerating (and thereby invalidating) an established identity.

UNTESTED ON REAL WINDOWS until this session's verification pass — written against the
`cryptography` package (already a pinned project dependency, avoids assuming system openssl.exe
exists on Windows, which it does not by default) mirroring the bash script's exact key type,
file layout, and idempotency check.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .windows_acl import set_exact_acl

logger = logging.getLogger("aihomecloud.windows_identity")


def generate_identity_if_missing(identity_dir: Path) -> None:
    """Create identity.key / identity.pub / epoch under `identity_dir` if not already present.

    Must only be called from the privileged issuer process (enforced by the caller, not this
    function). Sets ACLs itself, right after publishing -- identity.key stays readable by
    SYSTEM/Administrators only; the low-privilege main service account (name read from the
    AHC_SERVICE_ACCOUNT env var the issuer service is configured with) is granted read on
    identity.pub only, since GET /api/v1/system/identity needs to serve statement.json but the
    private key itself must never be readable by the network-facing process.
    """
    private_key_path = identity_dir / "identity.key"
    public_key_path = identity_dir / "identity.pub"
    epoch_path = identity_dir / "epoch"

    if private_key_path.exists() and public_key_path.exists() and epoch_path.exists():
        logger.info("identity already exists at %s, leaving it alone", identity_dir)
        # Re-grant the CURRENT service account read access on identity.pub even though the key
        # itself isn't touched. Found live 2026-08-20: a Windows local account's SID is minted
        # fresh every time the account is created, even with an identical name -- reinstalling
        # AiHomeCloud with DataDir preserved (the intended, documented behavior; see
        # install_windows.ps1's own uninstall boundary) recreates the `aihomecloud` account with
        # a NEW SID, but this file's ACL still references the OLD, now-nonexistent SID from the
        # previous account. Without this, the main service's TLS startup throws PermissionError
        # trying to read the (unrelated but identically-affected) tls/key.pem the same way -- see
        # windows_cert_issuer.py's matching fix. identity.key itself is never touched here: it has
        # no service-account grant to refresh, by design.
        service_account = os.environ.get("AHC_SERVICE_ACCOUNT")
        if service_account:
            set_exact_acl(public_key_path, {"SYSTEM": "F", "Administrators": "F", service_account: "R"})
        return

    identity_dir.mkdir(parents=True, exist_ok=True)
    # Lock the directory itself before anything is ever written inside it -- mirrors
    # ahc-generate-identity.sh's chmod 711-before-openssl-genpkey ordering. Security review
    # 2026-08-20 found (and live icacls confirmed) that skipping this let the low-privilege
    # service account inherit a Modify grant on this directory from install_windows.ps1's
    # broader $DataDir grant, since identity_dir already existed by the time that grant ran --
    # so identity.key briefly inherited write access for the account it must never be readable
    # or writable by. No grants dict entry for the service account here, ever: this directory
    # holds the board's one long-lived signing key, and unlike identity.pub/epoch below it has
    # no legitimate reader outside SYSTEM/Administrators.
    set_exact_acl(identity_dir, {"SYSTEM": "(OI)(CI)F", "Administrators": "(OI)(CI)F"})

    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Same discipline as the bash original and this project's other secret-writers (config.py's
    # generate_jwt_secret, store.py): write to a temp path in the same directory, then rename
    # into place, so a crash mid-write can never leave a partial key that looks valid.
    tmp_private = private_key_path.with_suffix(".key.tmp")
    tmp_public = public_key_path.with_suffix(".pub.tmp")
    tmp_private.write_bytes(private_pem)
    tmp_public.write_bytes(public_pem)
    tmp_private.replace(private_key_path)
    tmp_public.replace(public_key_path)

    # Epoch starts at 0 and only ever increments (windows_cert_issuer.py), persisted alongside
    # the key so a reinstall regenerates both together -- a reinstalled board's epoch must not be
    # reachable by replaying a statement signed under the old identity, and it isn't, because the
    # old identity is gone too.
    tmp_epoch = epoch_path.with_suffix(".tmp")
    tmp_epoch.write_text("0")
    tmp_epoch.replace(epoch_path)

    service_account = os.environ.get("AHC_SERVICE_ACCOUNT")
    admin_grants = {"SYSTEM": "F", "Administrators": "F"}
    set_exact_acl(private_key_path, admin_grants)  # no read grant for anyone else -- ever
    if service_account:
        set_exact_acl(public_key_path, {**admin_grants, service_account: "R"})
        set_exact_acl(epoch_path, admin_grants)  # issuer-only; the service never reads this directly
    else:
        logger.warning(
            "AHC_SERVICE_ACCOUNT not set — leaving identity.pub/epoch on default ACLs. "
            "The main service will not be able to read identity.pub until this is fixed."
        )

    logger.info("generated board identity at %s", identity_dir)
