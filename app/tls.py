"""
TLS certificate management for AiHomeCloud.

Certificate generation moved to root (H-11, docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md).
This module now only *requests* a reissue and waits for `ahc-issue-cert.sh` (triggered by root via
the ahc-issue-cert.path unit) to produce it — the service itself must never be able to generate a
certificate, because a service that could produce a certificate root would sign is a service that
could obtain a signature over a key it chose, which is exactly the compromise the board's identity
key exists to prevent. See that doc's §0 for the governing invariant.
"""

import asyncio
import json
import logging
import socket
import subprocess
import time
from pathlib import Path

from .config import settings

logger = logging.getLogger("aihomecloud.tls")


#: Chrome rejects certificates valid for more than 398 days outright. Staying well under that
#: means reissuing routinely, which is why _needs_reissue also watches expiry.
_CERT_DAYS = 365
#: Reissue this far ahead of expiry, so a board that is off for a few weeks still comes back with
#: a valid certificate rather than a dead one.
_RENEW_BEFORE_DAYS = 30
#: How long to wait for root to answer a reissue request before falling back. Generous relative to
#: measured issuance time (~1-3s on a Rock Pi 4A including RSA-2048 keygen) so a loaded board does
#: not spuriously time out; short enough that a genuinely stuck ahc-issue-cert.path fails fast into
#: the "keep the existing cert" / "no cert at all" fallbacks below rather than hanging startup.
_REISSUE_TIMEOUT_S = 30
_REISSUE_POLL_INTERVAL_S = 0.5


def _get_local_ips() -> list[str]:
    """
    Every local IPv4 address, so the certificate covers every address the board answers on.

    This used to find exactly *one*: it opened a UDP socket towards 8.8.8.8 and read back the
    local end, which yields whichever interface the default route uses. On a board with both
    Ethernet and Wi-Fi that is the Ethernet address, and the Wi-Fi one never entered the
    certificate — so a browser reaching the board over Wi-Fi got ERR_CERT_COMMON_NAME_INVALID,
    which Chrome refuses to let anyone click through. Found 2026-08-07 on a board with both
    interfaces active: cert covered its Ethernet address only, the board was also live on Wi-Fi.

    The socket trick is kept as a fallback for hosts where psutil cannot enumerate interfaces.
    Kept here (not just in ahc-issue-cert.sh) because `_cert_covers` below still needs it to
    decide whether a reissue is needed at all — root re-derives its own copy independently rather
    than trusting this one, per the design's "service supplies values, never structure" rule.
    """
    ips = {"127.0.0.1"}
    try:
        import psutil  # noqa: PLC0415 — only needed here, and optional by design

        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address:
                    ips.add(addr.address)
    except Exception:  # psutil missing, or a platform it cannot read
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ips.add(s.getsockname()[0])
        except Exception:
            pass
    return sorted(ips)


def _cert_covers(cert_path: Path, ips: list[str], names: list[str] | None = None) -> bool:
    """
    Whether an existing certificate already names every address we answer on.

    Without this the certificate is written once and never revisited, so a board that moves
    network, gains an interface or is handed a new DHCP lease keeps serving a certificate for an
    address it no longer has — and every browser client stops working, permanently, with no
    obvious cause. Cheap to check on each start; regenerating is the rare path.
    """
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-ext", "subjectAltName"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return True  # cannot tell — do not churn the cert on a broken or missing openssl
    # A non-zero exit means openssl could not parse it. `subprocess.run` does not raise for that
    # without check=True, and the empty stdout that follows reads as "covers nothing" — which
    # would reissue the certificate on every single boot. Treat unreadable as "leave it alone".
    if proc.returncode != 0 or not proc.stdout.strip():
        return True
    if not all(f"IP Address:{ip}" in proc.stdout for ip in ips):
        return False
    # The mDNS name changes whenever the family renames the board, and a stale one gives a NAME
    # MISMATCH rather than a plain untrusted warning — so it has to be part of "still covers us".
    return all(f"DNS:{n}" in proc.stdout for n in (names or []))


def _cert_expiring(cert_path: Path) -> bool:
    """True when the certificate is within [_RENEW_BEFORE_DAYS] of expiry (or already past it)."""
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-checkend",
             str(_RENEW_BEFORE_DAYS * 86400)],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False  # cannot tell — do not churn
    return proc.returncode != 0


_self_signed_cache: tuple[str, bool] | None = None


def is_self_signed(cert_path: Path | None = None) -> bool:
    """
    True when the served certificate is its own issuer.

    This gates HSTS, and that is not a stylistic choice. `Strict-Transport-Security` tells a browser
    to refuse any insecure or untrusted connection to this host — and Chrome implements that by
    **removing the "Proceed anyway" link from the certificate warning entirely**. On a board serving
    a certificate no CA has signed, the warning is the only way in, so sending HSTS locks every
    browser out of the device permanently, for the full max-age, with no in-browser recovery beyond
    chrome://net-internals/#hsts.

    Nor does it buy anything here. HSTS defends against a downgrade attack on a host whose identity
    the browser can already verify. With a self-signed certificate the user is doing
    trust-on-first-use by hand anyway, and an attacker on the LAN could present their own
    self-signed certificate to exactly the same click-through prompt. The protection HSTS offers is
    not achievable until a real certificate is in place — at which point this returns False and the
    header comes back on its own.

    Cached on the path: this is consulted from a response middleware, i.e. every request.
    """
    global _self_signed_cache
    path = cert_path or settings.tls_cert_path
    if _self_signed_cache is not None and _self_signed_cache[0] == str(path):
        return _self_signed_cache[1]

    result = True  # fail closed: unreadable cert means no HSTS, which is the recoverable direction
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", str(path), "-noout", "-subject", "-issuer"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            lines = {}
            for line in proc.stdout.splitlines():
                key, _, value = line.partition("=")
                lines[key.strip().lower()] = value.strip()
            subject, issuer = lines.get("subject"), lines.get("issuer")
            if subject and issuer:
                result = subject == issuer
    except Exception:
        pass

    _self_signed_cache = (str(path), result)
    return result


async def _request_reissue_and_wait(reason: str, cert_path: Path, key_path: Path) -> tuple[Path, Path]:
    """
    Ask root for a fresh certificate and wait for it to appear.

    The service writes *why* a reissue is wanted, never *what* the certificate should contain —
    `ahc-issue-cert.sh` re-derives hostname and IP SANs itself from the live system. This is the
    request half of H-11 §2; ahc-issue-cert.path on the root side is the other half.
    """
    request_path = settings.data_dir / "cert-request.json"
    before_mtime = cert_path.stat().st_mtime if cert_path.exists() else None

    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps({"reason": reason}))
    logger.info("requested certificate reissue: %s", reason)

    deadline = time.monotonic() + _REISSUE_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(_REISSUE_POLL_INTERVAL_S)
        if cert_path.exists() and key_path.exists():
            after_mtime = cert_path.stat().st_mtime
            if before_mtime is None or after_mtime > before_mtime:
                logger.info("root issued a new certificate (reason=%r)", reason)
                return cert_path, key_path

    if cert_path.exists() and key_path.exists():
        # Stale beats absent, same philosophy as before this moved to root: a board still serving
        # yesterday's (still cryptographically valid, just not-yet-ideal) certificate is reachable;
        # a board with none at all falls back to plain HTTP in main.py's startup path.
        logger.error(
            "root did not issue a new certificate within %ss — keeping the existing one (reason=%r)",
            _REISSUE_TIMEOUT_S, reason,
        )
        return cert_path, key_path

    raise RuntimeError(
        f"no certificate at {cert_path} and root did not issue one within {_REISSUE_TIMEOUT_S}s "
        f"(reason={reason!r}) — is ahc-issue-cert.path enabled? (systemctl status ahc-issue-cert.path)"
    )


async def ensure_tls_cert() -> tuple[Path, Path]:
    """
    Return (cert_path, key_path). If the certificate on disk is missing, stale, or expiring,
    request root to reissue it and wait for the result — never generate one in-process.
    """
    cert_path = settings.tls_cert_path
    key_path = settings.tls_key_path

    if cert_path.exists() and key_path.exists():
        current_ips = _get_local_ips()
        host = socket.gethostname()
        stale_names = not _cert_covers(cert_path, current_ips, [host, f"{host}.local"])
        expiring = _cert_expiring(cert_path)
        if not stale_names and not expiring:
            logger.info("TLS cert already exists at %s", cert_path)
            return cert_path, key_path
        reason = "expiring" if expiring else f"does not cover {current_ips} / {host}.local"
        logger.warning("TLS cert %s — requesting reissue", reason)
        return await _request_reissue_and_wait(reason, cert_path, key_path)

    logger.info("no TLS certificate present — requesting one")
    return await _request_reissue_and_wait("no certificate present", cert_path, key_path)
