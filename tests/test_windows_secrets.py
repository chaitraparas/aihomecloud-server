"""
The passphrase-based export/import path is genuinely testable without Windows (pure
`cryptography` library). The DPAPI path (protect_secret_to_file/read_protected_secret)
is NOT exercised here — it needs a real Windows machine (win32crypt) and is intentionally
left untested until one is available; see windows_secrets.py's module docstring.
"""

import pytest

from app.windows_secrets import export_identity_bundle, import_identity_bundle


def test_export_then_import_round_trips_exactly():
    secrets = {"jwt_secret": "a" * 64, "pairing_key": "b" * 22}

    bundle = export_identity_bundle(secrets, passphrase="correct horse battery staple")
    recovered = import_identity_bundle(bundle, passphrase="correct horse battery staple")

    assert recovered == secrets


def test_wrong_passphrase_raises_rather_than_returning_garbage():
    secrets = {"jwt_secret": "x" * 64}
    bundle = export_identity_bundle(secrets, passphrase="right-passphrase")

    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        import_identity_bundle(bundle, passphrase="wrong-passphrase")


def test_export_is_not_deterministic_and_never_leaks_plaintext():
    """Random salt + random Fernet nonce each call -- two exports of the same secret must
    look nothing alike on disk, and neither may contain the raw secret bytes."""
    secrets = {"jwt_secret": "s3cr3t-value-that-must-not-appear-in-ciphertext"}

    bundle_a = export_identity_bundle(secrets, passphrase="pw")
    bundle_b = export_identity_bundle(secrets, passphrase="pw")

    assert bundle_a != bundle_b
    assert b"s3cr3t-value-that-must-not-appear-in-ciphertext" not in bundle_a


def test_dpapi_functions_fail_clearly_on_a_non_windows_host():
    """On macOS/Linux (no pywin32), these must raise a clear, specific ImportError --
    not a bare crash several layers into a missing-module traceback."""
    from app.windows_secrets import _dpapi_protect, _dpapi_unprotect

    with pytest.raises(ImportError, match="pywin32"):
        _dpapi_protect(b"data", "description")

    with pytest.raises(ImportError, match="pywin32"):
        _dpapi_unprotect(b"blob")
