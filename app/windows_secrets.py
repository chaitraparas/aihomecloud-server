"""
Windows-native at-rest protection for the JWT secret and pairing key, analogous to
config.py's generate_jwt_secret()/generate_pairing_key() but using DPAPI instead of a
0600-permission file — Windows ACLs aren't the same protection model, and the architecture
review for this port specifically called out "investigate DPAPI, Credential Manager,
keyring, or ACLs" rather than assuming Linux's file-permission approach ports as-is.

UNTESTED ON REAL WINDOWS — written 2026-08-19 on macOS, no Windows machine available to
verify. The DPAPI calls (win32crypt.CryptProtectData/CryptUnprotectData) are correct against
documented pywin32 API and are a thin, stable wrapper around a Windows API that has not
changed shape in decades, but "written correctly against the docs" is not the same claim as
"verified running." Treat this module as a draft to compile-check and exercise for real
before it ships, not as done.

Migration answer (the architecture review's explicit "what happens on a new PC" question):
DPAPI keys are derived from machine-specific material and CANNOT be copied to a new machine
— a raw copy of the encrypted file is unreadable there. export_identity_bundle() /
import_identity_bundle() exist specifically for this: they re-encrypt under a user-supplied
passphrase (via the `cryptography` package, already a project dependency, using Fernet —
itself AES-128-CBC + HMAC, so this isn't a new crypto primitive to trust) into a portable
file, then the new machine re-protects under its own DPAPI on import. The passphrase leg is
genuinely testable without Windows (pure `cryptography` library, exercised in
tests/test_windows_secrets.py); only the DPAPI leg needs a real Windows machine to verify.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

logger = logging.getLogger("aihomecloud.windows_secrets")

# DPAPI_LOCAL_MACHINE: protect for any process on this machine (any user), not just the
# calling user account -- the backend runs as its own Windows service account, and a
# per-user DPAPI blob would become unreadable the moment that account's profile changes,
# which is a more fragile failure mode than "this specific machine" for a background service.
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


def _dpapi_protect(data: bytes, description: str) -> bytes:
    """Encrypt `data` with DPAPI, scoped to this machine. Windows-only; raises ImportError
    with a clear message on any other platform rather than a confusing pywin32 import error."""
    try:
        import win32crypt  # noqa: PLC0415 — Windows-only import, deliberately deferred
    except ImportError as e:
        raise ImportError(
            "windows_secrets requires pywin32 (win32crypt) — only usable on Windows"
        ) from e

    blob = win32crypt.CryptProtectData(
        data, description, None, None, None, _CRYPTPROTECT_LOCAL_MACHINE
    )
    return blob


def _dpapi_unprotect(blob: bytes) -> bytes:
    try:
        import win32crypt  # noqa: PLC0415
    except ImportError as e:
        raise ImportError(
            "windows_secrets requires pywin32 (win32crypt) — only usable on Windows"
        ) from e

    _description, data = win32crypt.CryptUnprotectData(
        blob, None, None, None, _CRYPTPROTECT_LOCAL_MACHINE
    )
    return data


def protect_secret_to_file(secret: str, path: Path, description: str) -> None:
    """Write `secret` to `path` DPAPI-encrypted. Creates parent dirs; overwrites atomically
    via a temp-file-then-replace, same discipline as config.py's generate_jwt_secret()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = _dpapi_protect(secret.encode("utf-8"), description)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)  # atomic on the same volume, same reasoning as os.replace() in store.py
    logger.info("windows_secrets: wrote DPAPI-protected secret to %s", path)


def read_protected_secret(path: Path) -> str | None:
    """Returns the decrypted secret, or None if the file doesn't exist. Raises if the file
    exists but can't be decrypted (e.g. moved from a different machine without going through
    export/import) -- that failure must be loud, not silently treated as "no secret yet",
    or a corrupted/foreign blob would look identical to first-run and quietly regenerate."""
    if not path.exists():
        return None
    blob = path.read_bytes()
    return _dpapi_unprotect(blob).decode("utf-8")


def export_identity_bundle(secrets: dict[str, str], passphrase: str) -> bytes:
    """
    Package `secrets` (e.g. {"jwt_secret": ..., "pairing_key": ...}) into a portable,
    passphrase-encrypted bundle for moving to a new machine. NOT DPAPI -- DPAPI is
    deliberately machine-bound, which is exactly what a migration needs to escape.

    Uses Fernet (AES-128-CBC + HMAC via the `cryptography` package, already a dependency)
    with a key derived from the passphrase via PBKDF2, matching the iteration-count
    convention `keyring`-style tools use for this today (100k+ iterations, no reason to be
    cheaper here -- this runs once, interactively, at migration time, not in a hot path).
    """
    import json
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    import os as _os

    salt = _os.urandom(16)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

    payload = json.dumps(secrets).encode("utf-8")
    ciphertext = Fernet(key).encrypt(payload)

    # salt || ciphertext, both needed on import to re-derive the same key
    return salt + b"." + ciphertext


def import_identity_bundle(bundle: bytes, passphrase: str) -> dict[str, str]:
    """Inverse of export_identity_bundle(). Raises cryptography.fernet.InvalidToken on a
    wrong passphrase or corrupted bundle -- that must surface to the caller as a real error,
    not be swallowed, since silently returning {} would look identical to "no secrets" and
    the caller would regenerate fresh ones, invalidating every existing session/pairing."""
    import json
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt, ciphertext = bundle.split(b".", 1)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

    payload = Fernet(key).decrypt(ciphertext)
    return json.loads(payload.decode("utf-8"))
