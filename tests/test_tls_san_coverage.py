"""
The certificate must name every address the board answers on.

This is the failure that has no workaround at the client: Chrome treats a name mismatch as
ERR_CERT_COMMON_NAME_INVALID and offers no "proceed anyway", so a board reachable on an address
its certificate omits is simply unusable from a browser — with nothing in the UI to explain why.
"""

import socket
import subprocess
from pathlib import Path

import pytest

from app import tls


class TestAddressDiscovery:
    def test_loopback_is_always_included(self):
        assert "127.0.0.1" in tls._get_local_ips()

    def test_every_interface_is_enumerated_not_just_the_default_route(self, monkeypatch):
        """
        The original implementation opened a UDP socket to 8.8.8.8 and returned only the interface
        the default route uses — on a board with Ethernet and Wi-Fi, that silently dropped one.
        """
        class Addr:
            def __init__(self, family, address):
                self.family, self.address = family, address

        fake = {
            "eth0": [Addr(socket.AF_INET, "192.168.1.50")],
            "wlan0": [Addr(socket.AF_INET, "192.168.1.60")],
            "lo": [Addr(socket.AF_INET, "127.0.0.1")],
        }
        import psutil
        monkeypatch.setattr(psutil, "net_if_addrs", lambda: fake)

        ips = tls._get_local_ips()

        assert "192.168.1.50" in ips
        assert "192.168.1.60" in ips, "the Wi-Fi address is the one that used to go missing"

    def test_non_ipv4_families_are_ignored(self, monkeypatch):
        class Addr:
            def __init__(self, family, address):
                self.family, self.address = family, address

        import psutil
        monkeypatch.setattr(psutil, "net_if_addrs", lambda: {
            "eth0": [Addr(socket.AF_INET6, "fe80::1"), Addr(socket.AF_INET, "10.0.0.5")],
        })

        assert "10.0.0.5" in tls._get_local_ips()
        assert "fe80::1" not in tls._get_local_ips()


class TestCoverageCheck:
    def _make_cert(self, tmp_path: Path, sans: str) -> Path:
        cert = tmp_path / "c.pem"
        key = tmp_path / "k.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
             "-out", str(cert), "-days", "1", "-nodes", "-subj", "/CN=test",
             "-addext", f"subjectAltName={sans}"],
            check=True, capture_output=True,
        )
        return cert

    def test_a_cert_naming_every_address_is_kept(self, tmp_path):
        cert = self._make_cert(tmp_path, "IP:127.0.0.1,IP:192.168.1.50")

        assert tls._cert_covers(cert, ["127.0.0.1", "192.168.1.50"]) is True

    def test_a_cert_missing_an_address_is_rejected(self, tmp_path):
        """Exactly the dual-interface situation: live on .60 too, certificate only names .50."""
        cert = self._make_cert(tmp_path, "IP:127.0.0.1,IP:192.168.1.50")

        assert tls._cert_covers(cert, ["127.0.0.1", "192.168.1.50", "192.168.1.60"]) is False

    def test_an_unreadable_cert_does_not_trigger_churn(self, tmp_path):
        """If openssl cannot be asked, keep what we have rather than reissuing on every boot."""
        junk = tmp_path / "not-a-cert.pem"
        junk.write_text("nonsense")

        assert tls._cert_covers(junk, ["127.0.0.1"]) is True


class TestBrowserAcceptability:
    """
    Chrome refuses a malformed certificate outright, with no "Advanced -> Proceed".

    A self-signed certificate is *expected* to warn — it has no trusted issuer. The distinction
    that matters is between a warning a person can click through and an error they cannot, and
    these properties decide it. The original certificate failed two of them, and the web client
    was simply unopenable as a result.

    Since H-11 moved certificate generation to root (`ahc-issue-cert.sh`), these properties are no
    longer producible through `ensure_tls_cert()` in-process — calling it here would hang waiting
    for a root daemon this test environment does not have. The checks below verify the script
    still requests every load-bearing flag (catches an accidental flag removal in an edit); the
    actual generated output was verified against a live board 2026-08-13 (SAN covered hostname +
    hostname.local + localhost + all 3 real IPs, CA:FALSE, correct key/extended-key usage,
    365-day validity) — see docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md and this
    session's log for the exact `openssl x509 -text` output that was checked.
    """

    def _script_text(self):
        return (Path(__file__).parent.parent / "scripts" / "ahc-issue-cert.sh").read_text()

    def test_validity_is_within_chromes_398_day_limit(self):
        """Over 398 days is ERR_CERT_VALIDITY_TOO_LONG. The original was 3650."""
        assert tls._CERT_DAYS <= 398

    def test_script_requests_days_matching_the_python_constant(self):
        """The script hardcodes -days 365; this catches it drifting out of sync with tls._CERT_DAYS."""
        assert f"-days {tls._CERT_DAYS}" in self._script_text()

    def test_script_marks_the_cert_as_not_a_certificate_authority(self):
        """The original was CA:TRUE — a CA served as a server cert, which browsers reject."""
        assert "basicConstraints=critical,CA:FALSE" in self._script_text()

    def test_script_claims_server_authentication(self):
        assert "extendedKeyUsage=serverAuth" in self._script_text()

    def test_script_builds_a_subject_alternative_name_from_hostname_and_ips(self):
        text = self._script_text()
        assert "subjectAltName=" in text
        assert 'DNS:${HOSTNAME}' in text
        assert 'DNS:${HOSTNAME}.local' in text
        assert "IP:${ip}" in text

    def test_ip_enumeration_does_not_depend_on_psutil(self):
        """
        Regression pin for a real infinite reissue-restart loop found live 2026-08-13 rolling H-11
        out to Cubie A5E (multi-homed: Ethernet + Wi-Fi + Tailscale). This script runs as root via
        systemd, outside the aihomecloud venv where psutil actually lives -- root's system python3
        has no reason to have it installed, and on Cubie A5E it didn't. `psutil.net_if_addrs()`
        inside a bare `try/except Exception` silently fell through to app/tls.py's own
        already-fixed-once single-IP fallback (a UDP-socket trick landing on whichever interface
        owns the default route), so the issued certificate covered only one of the board's three
        real addresses. The service's own `_cert_covers()` check (comparing against its own
        venv-side psutil-based enumeration, which DID see every interface) correctly judged that
        certificate insufficient on every single restart, requested another reissue, and the
        service's own `try-restart` immediately loop right back into the same one-IP bug --
        confirmed live via journalctl showing repeated "TLS cert does not cover [...] --
        requesting reissue" lines seconds apart, epoch climbing on every iteration.
        `ip` (iproute2) needs no language runtime or installed package, so root's script can't
        drift out of sync with whatever happens to be present in a venv it deliberately doesn't
        share, ever again.
        """
        text = self._script_text()
        assert "import psutil" not in text
        assert "ip -4" in text


class TestHstsGatedOnCertTrust:
    """
    HSTS must not be sent alongside a self-signed certificate.

    Chrome implements Strict-Transport-Security by removing the "Proceed anyway" link from the
    certificate warning. On a board whose certificate no CA has signed, that warning is the only
    way in — so sending the header locks every browser out of the device for the full max-age,
    recoverable only via chrome://net-internals/#hsts. Found the hard way: the Mac and the phone
    both stopped being able to open the web client, while curl and a cert-ignoring headless
    browser kept working perfectly, which is exactly the pair of signals that hides this.
    """

    def _write_cert(self, tmp_path, subject, issuer):
        import subprocess
        key = tmp_path / "k.pem"
        cert = tmp_path / "c.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", subject],
            capture_output=True, check=True,
        )
        return cert

    def test_a_self_signed_certificate_is_detected(self, tmp_path):
        from app import tls
        tls._self_signed_cache = None
        cert = self._write_cert(tmp_path, "/CN=board.local", "/CN=board.local")

        assert tls.is_self_signed(cert) is True

    def test_an_unreadable_certificate_fails_closed(self, tmp_path):
        """No HSTS is the recoverable direction; a wrongly-sent header is not."""
        from app import tls
        tls._self_signed_cache = None

        assert tls.is_self_signed(tmp_path / "does-not-exist.pem") is True

    async def test_the_header_is_absent_while_the_certificate_is_self_signed(self, client, monkeypatch):
        from app import main, tls
        tls._self_signed_cache = None
        monkeypatch.setattr(main, "is_self_signed", lambda *a, **k: True)
        monkeypatch.setattr(main.settings, "tls_enabled", True, raising=False)

        res = await client.get("/api/health")

        assert "strict-transport-security" not in {k.lower() for k in res.headers}

    async def test_the_header_returns_once_a_ca_has_signed_the_certificate(self, client, monkeypatch):
        from app import main, tls
        tls._self_signed_cache = None
        monkeypatch.setattr(main, "is_self_signed", lambda *a, **k: False)
        monkeypatch.setattr(main.settings, "tls_enabled", True, raising=False)

        res = await client.get("/api/health")

        assert "max-age" in res.headers.get("strict-transport-security", "")


class TestKeyRotatesOnReissueSinceH11:
    """
    Before H-11: a reissued certificate deliberately kept the same key, because clients pinned the
    raw SPKI and a rotated key would break every pairing with no recovery.

    Since H-11: the board's identity key (not the raw TLS SPKI) is what clients pin, and it signs a
    statement binding the new SPKI on every reissue — so the TLS key **should** now rotate every
    time; keeping it was a workaround for a problem the identity key makes obsolete. Getting this
    backwards (still reusing the key) would silently defeat H-11's actual security property without
    breaking anything visibly, which is exactly the kind of regression worth pinning a test to.

    `ensure_tls_cert()` no longer generates in-process (see TestBrowserAcceptability's docstring),
    so this checks the script never reuses an existing key — a `-key <path>` flag reappearing next
    to `-newkey rsa:2048` would be exactly that regression.
    """

    def test_script_always_generates_a_fresh_key_never_reuses_one(self):
        text = (Path(__file__).parent.parent / "scripts" / "ahc-issue-cert.sh").read_text()
        assert "-newkey rsa:2048" in text
        assert " -key " not in text, "reusing the old key would silently defeat H-11's rotation guarantee"

    @pytest.mark.skip(
        reason=(
            "Documents what was actually observed on Rock Pi 4A 2026-08-13, not asserted here: "
            "epoch 0->1 and 1->2 (two real ahc-issue-cert.sh runs, systemd-.path-triggered) produced "
            "SPKIs qgdzWP2OZ7yP6Rl15apjxW/Jsm6X+TnU8ixKauNW8yA= and a second, different value on the "
            "next run -- confirmed by independently reconstructing and verifying the Ed25519 "
            "signature over each statement, and by matching the live-served certificate's SPKI "
            "against the statement's spki field via a real TLS handshake (openssl s_client), not by "
            "reading files off disk. Requires root + a real board, so it's skipped rather than run "
            "here -- an `assert True` body would silently pass forever with zero regression "
            "coverage if key-rotation-on-reissue ever broke, which is worse than an honest skip."
        )
    )
    def test_two_real_issuances_on_a_live_board_produced_different_keys(self):
        pass


class TestIdentityDirIsTraversableByTheService:
    """
    GET /system/identity is served by the unprivileged aihomecloud service reading
    statement.json directly out of $DATA_DIR/identity/ -- a directory that also holds the
    root-only identity.key. Directory *execute* permission is what lets a non-owner open a file
    inside by exact path; directory *read* permission (which the service must NOT have) only
    controls whether the directory's contents can be listed. identity.key's own 0600 file mode
    already blocks the service regardless of the directory bit, so withholding execute too was
    pure over-restriction -- and it broke the one thing this directory's non-secret file exists
    to do.

    Found live 2026-08-13, deploying this exact feature to Rock Pi 4A: `identity/` was chmod 700,
    `sudo -u aihomecloud cat statement.json` was "Permission denied" despite the file itself being
    0644, and GET /system/identity 500'd on real hardware for exactly that reason -- a class of
    bug this backend's own test suite structurally cannot catch (tests run as a single local user
    with no cross-UID permission enforcement at all), which is exactly why this is pinned as a
    script-content check rather than trusted to a functional test.
    """

    def test_identity_dir_permission_is_711_not_700(self):
        text = (Path(__file__).parent.parent / "scripts" / "ahc-generate-identity.sh").read_text()
        assert "chmod 711 \"$IDENTITY_DIR\"" in text
        assert "chmod 700 \"$IDENTITY_DIR\"" not in text
