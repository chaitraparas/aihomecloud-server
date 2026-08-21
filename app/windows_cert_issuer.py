"""
Windows port of scripts/ahc-issue-cert.sh — the privileged half of H-11 SPKI rotation.
See docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md §2.

Meant to run ONLY inside the separate, privileged "AiHomeCloudCertIssuer" Windows service
(LocalSystem), never inside the main AiHomeCloud service, which runs under a dedicated
low-privilege account specifically so it can never sign its own certificate. Upholds the same
two properties as the bash original:

  1. The (low-privilege) service supplies *values* (a reissue reason), never *structure* and
     never key material. IP SANs and hostname are re-derived here from the live system, never
     trusted from the request file.
  2. Statement and certificate can never disagree, because this one process produces both from
     the same key and publishes key, then cert, then statement -- in that order, so a client can
     never observe a statement naming a certificate that is not yet live.

UNTESTED ON REAL WINDOWS until this session's verification pass. Written against `cryptography`
(already a pinned dependency) rather than shelling out to openssl.exe, which Windows does not
ship by default.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .tls import _get_local_ips  # noqa: PLC0415 -- reuse, never re-derive independently and drift
from .windows_acl import set_exact_acl
from .windows_identity import generate_identity_if_missing

logger = logging.getLogger("aihomecloud.windows_cert_issuer")

_HOSTNAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class IssuanceError(Exception):
    """Raised for any condition that must abort issuance without publishing anything."""


def _get_or_create_device_serial(identity_dir: Path) -> str:
    """AHC_DEVICE_SERIAL equivalent. Linux derives this once at install time from hostname+MAC
    and bakes it into the systemd unit's Environment=, where ahc-issue-cert.sh reads it back via
    `systemctl show`. Windows has no equivalent way for a separate service to introspect another
    service's environment, so this generates and persists an equally stable identifier the first
    time it's needed -- idempotent, matching identity.key's own idempotency, and independent of
    it (a serial is descriptive metadata inside the signed statement, not a security boundary)."""
    serial_path = identity_dir / "device_serial.txt"
    if serial_path.exists():
        return serial_path.read_text().strip()

    identity_dir.mkdir(parents=True, exist_ok=True)
    hostname = socket.gethostname().split(".")[0].upper()
    suffix = secrets.token_hex(2).upper()
    serial = f"AHC-{hostname}-{suffix}"

    tmp = serial_path.with_suffix(".tmp")
    tmp.write_text(serial)
    tmp.replace(serial_path)
    logger.info("generated device serial: %s", serial)
    return serial


def _canonical_statement_bytes(statement: dict) -> bytes:
    """Pinned exactly as the design specifies (sort_keys, no whitespace) -- a formatting drift
    here silently invalidates every signature already accepted in the field. Doing this in
    Python end-to-end (unlike the bash original, which shells out to python3 -c for this exact
    step) removes the cross-language canonicalization risk entirely: issuer and this helper use
    the identical `json` module call."""
    return json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")


def issue_certificate(data_dir: Path, reason: str) -> None:
    """Ask-and-wait side is app/tls.py; this is the answer side. Call only from the privileged
    issuer service. Raises IssuanceError on any condition that must abort without publishing."""
    identity_dir = data_dir / "identity"
    private_identity_path = identity_dir / "identity.key"
    public_identity_path = identity_dir / "identity.pub"
    epoch_path = identity_dir / "epoch"
    tls_dir = data_dir / "tls"
    cert_path = tls_dir / "cert.pem"
    key_path = tls_dir / "key.pem"
    statement_path = identity_dir / "statement.json"

    generate_identity_if_missing(identity_dir)
    if not (private_identity_path.exists() and epoch_path.exists()):
        raise IssuanceError(f"no identity key at {private_identity_path} after generation attempt")

    logger.info("issuing certificate — reason: %s", reason)
    tls_dir.mkdir(parents=True, exist_ok=True)
    # Same directory-lock-before-write discipline as windows_identity.py, and for the same
    # reason (security review 2026-08-20): tls_dir already exists by the time
    # install_windows.ps1's New-ServiceAccount grants Modify on $DataDir, so without this the
    # service account would briefly inherit *write* access to its own key.pem the instant it's
    # created -- letting a compromised main service replace its own TLS key underneath the
    # identity-signed statement. RX here (not the M the installer's broader grant used), since
    # the main service does need to read cert.pem/key.pem to terminate TLS, just never write them.
    service_account_early = os.environ.get("AHC_SERVICE_ACCOUNT")
    tls_lock_grants = {"SYSTEM": "(OI)(CI)F", "Administrators": "(OI)(CI)F"}
    if service_account_early:
        tls_lock_grants[service_account_early] = "(OI)(CI)RX"
    set_exact_acl(tls_dir, tls_lock_grants)

    # --- Re-derive hostname + SANs from the live system (never from the request) ---
    hostname = socket.gethostname().split(".")[0].lower()
    if not _HOSTNAME_RE.match(hostname):
        raise IssuanceError(f"unusable hostname: {hostname!r}")

    ips = _get_local_ips()
    san_entries: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.DNSName(f"{hostname}.local"),
        x509.DNSName("localhost"),
    ]
    for ip in ips:
        try:
            san_entries.append(x509.IPAddress(__import__("ipaddress").ip_address(ip)))
        except ValueError:
            logger.warning("skipping unparseable IP in SAN list: %s", ip)

    # --- Fresh key + certificate every time. Deliberately not reusing the old TLS key: the
    # identity key provides pinning continuity, so the TLS key should rotate on every reissue —
    # see the design doc's "Key continuity note" (§2). ---
    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AiHomeCloud"),
    ])
    not_before = datetime.datetime.now(datetime.timezone.utc)
    not_after = not_before + datetime.timedelta(days=365)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True, content_commitment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(tls_key, hashes.SHA256())
    )

    # --- SHA-256(SubjectPublicKeyInfo) of the certificate just produced, not of anything the
    # request supplied — this is what binds the statement to this specific certificate. ---
    spki_der = cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    spki_b64 = base64.b64encode(hashlib.sha256(spki_der).digest()).decode("ascii")

    serial = _get_or_create_device_serial(identity_dir)
    old_epoch = int(epoch_path.read_text().strip())
    new_epoch = old_epoch + 1
    not_before_ts = int(time.time())

    statement = {
        "spki": spki_b64,
        "serial": serial,
        "notBefore": not_before_ts,
        "epoch": new_epoch,
    }
    identity_key = serialization.load_pem_private_key(private_identity_path.read_bytes(), password=None)
    signature = identity_key.sign(_canonical_statement_bytes(statement))
    # Verified against the actual Android client (BoardIdentity.kt): identityPublicKey must be
    # base64 of the raw DER SubjectPublicKeyInfo -- NOT base64 of the PEM text identity.pub is
    # stored as (see windows_identity.py). Caught here before this ever shipped: an earlier draft
    # of this function base64'd the PEM bytes directly, which would have made every Windows
    # board's identity key unverifiable by every Android client, silently.
    identity_pub_obj = serialization.load_pem_public_key(public_identity_path.read_bytes())
    identity_pub_der = base64.b64encode(
        identity_pub_obj.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("ascii")
    envelope = {
        "identityPublicKey": identity_pub_der,
        "statement": statement,
        "signature": base64.b64encode(signature).decode("ascii"),
    }

    # --- Publish atomically, key then cert then statement — exactly the order in the design. ---
    tmp_key = key_path.with_suffix(".pem.tmp")
    tmp_cert = cert_path.with_suffix(".pem.tmp")
    tmp_statement = statement_path.with_suffix(".json.tmp")
    tmp_epoch = epoch_path.with_suffix(".tmp")

    tmp_key.write_bytes(
        tls_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    tmp_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    tmp_statement.write_text(json.dumps(envelope, indent=2))
    tmp_epoch.write_text(str(new_epoch))

    tmp_key.replace(key_path)
    tmp_cert.replace(cert_path)
    tmp_statement.replace(statement_path)
    tmp_epoch.replace(epoch_path)

    # key.pem specifically is the file the 2026-08-13 Linux security review found: the service
    # account must be able to READ it (it terminates TLS) but never WRITE it, or it can
    # delete/replace its own cert out from under the identity-signed statement. cert.pem and
    # statement.json are public artifacts once published; epoch is issuer-only.
    admin_grants = {"SYSTEM": "F", "Administrators": "F"}
    service_account = os.environ.get("AHC_SERVICE_ACCOUNT")
    if service_account:
        set_exact_acl(key_path, {**admin_grants, service_account: "R"})
        set_exact_acl(cert_path, {**admin_grants, service_account: "R"})
        set_exact_acl(statement_path, {**admin_grants, service_account: "R"})
        set_exact_acl(epoch_path, admin_grants)
    else:
        logger.warning(
            "AHC_SERVICE_ACCOUNT not set — leaving tls/cert files on default ACLs. The main "
            "service will not be able to read its own TLS key until this is fixed."
        )

    request_path = data_dir / "cert-request.json"
    request_path.unlink(missing_ok=True)

    logger.info(
        "issued certificate for %s — epoch %s -> %s, spki=%s...",
        hostname, old_epoch, new_epoch, spki_b64[:12],
    )

    try:
        import win32serviceutil  # noqa: PLC0415 -- Windows-only, deliberately deferred

        win32serviceutil.RestartService("AiHomeCloud")
    except Exception as e:  # noqa: BLE001 -- best-effort restart, never abort a successful issuance over this
        logger.warning("could not restart AiHomeCloud service after issuance: %s", e)


_POLL_INTERVAL_S = 2.0


def _refresh_existing_tls_acls(data_dir: Path) -> None:
    """Re-grant the CURRENT AHC_SERVICE_ACCOUNT read access on already-published tls/*, without
    touching their contents. issue_certificate() only runs when a rotation is actually requested,
    so a fresh reinstall that preserves DataDir (the documented, intended uninstall boundary --
    see install_windows.ps1) can leave tls/key.pem's ACL referencing a service account SID that no
    longer exists: Windows mints a new SID for a recreated account even with an identical name.
    Found live 2026-08-20 -- the main service's uvicorn startup threw PermissionError loading its
    own TLS key after exactly this sequence. Runs once at issuer startup, before the poll loop, so
    every issuer restart (which happens on every reinstall) self-heals this regardless of whether
    a rotation ever fires."""
    service_account = os.environ.get("AHC_SERVICE_ACCOUNT")
    if not service_account:
        return
    admin_grants = {"SYSTEM": "F", "Administrators": "F"}
    tls_dir = data_dir / "tls"
    identity_dir = data_dir / "identity"
    for path in (tls_dir / "key.pem", tls_dir / "cert.pem", identity_dir / "statement.json"):
        if path.exists():
            set_exact_acl(path, {**admin_grants, service_account: "R"})


def run_forever(data_dir: Path) -> None:
    """Entrypoint for the AiHomeCloudCertIssuer service. Stands in for
    ahc-issue-cert.path -- polling, not filesystem-event-watched, matching the poll-based style
    app/tls.py's request side already uses (consistent methodology, and avoids a Windows
    filesystem-watch API dependency for a 2-second-latency requirement nothing needs tighter).
    """
    request_path = data_dir / "cert-request.json"
    logging.basicConfig(level=logging.INFO)
    _refresh_existing_tls_acls(data_dir)
    logger.info("AiHomeCloudCertIssuer started, watching %s", request_path)
    while True:
        try:
            if request_path.exists():
                reason = "unspecified"
                try:
                    reason = json.loads(request_path.read_text()).get("reason", "unspecified")
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning("unreadable request file, issuing anyway: %s", e)
                issue_certificate(data_dir, reason)
        except IssuanceError as e:
            logger.error("issuance failed, leaving request file for retry: %s", e)
        except Exception:  # noqa: BLE001 -- this loop must never die from one bad cycle
            logger.exception("unexpected error in issuance loop")
        time.sleep(_POLL_INTERVAL_S)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m app.windows_cert_issuer <data_dir>", file=sys.stderr)
        raise SystemExit(2)
    run_forever(Path(sys.argv[1]))
