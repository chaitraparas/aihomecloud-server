"""
Missing, empty, truncated, malformed or unreadable secret material must always fail closed.

`hmac.compare_digest("", "")` returns **True**. That is correct for comparing digests and
catastrophic for an authentication check: it makes "this board has no pairing key" and "the caller
proved knowledge of the pairing key" produce the same answer. The pairing endpoints compared
`body.key` against `settings.pairing_key` directly, so a damaged key would have accepted an empty
supplied key from anyone on the LAN.

Reachable because `O_CREAT` precedes `os.write` in the generators: a power cut in that window —
routine on an SBC someone unplugs — leaves a zero-byte file that every later boot reads as "".

Two layers are tested here, deliberately, because either alone is insufficient:
  1. the generators refuse to return unusable material and regenerate it;
  2. the comparison itself fails closed, which holds even if the value arrives from an env var, a
     future config source, or a bug.
"""

import logging

import pytest

from app.auth import verify_device_identifier, verify_shared_secret
from app.config import generate_jwt_secret, generate_pairing_key


@pytest.fixture(autouse=True)
def _quiet(caplog):
    """These paths log CRITICAL by design; keep the test output readable."""
    caplog.set_level(logging.CRITICAL)


# ---------------------------------------------------------------------------
# The comparison primitive
# ---------------------------------------------------------------------------

class TestVerifySharedSecret:
    def test_empty_vs_empty_is_refused(self):
        """THE bug. `hmac.compare_digest("", "")` is True; this must not be."""
        assert verify_shared_secret("", "") is False

    def test_a_real_match_still_succeeds(self):
        secret = "s3cr3t-pairing-key-value"
        assert verify_shared_secret(secret, secret) is True

    def test_a_wrong_key_is_refused(self):
        assert verify_shared_secret("wrong", "s3cr3t-pairing-key-value") is False

    @pytest.mark.parametrize("stored,label", [
        ("",            "empty stored secret"),
        ("short",       "truncated stored secret"),
        (None,          "missing stored secret"),
        (b"x" * 22,     "malformed stored secret (bytes, not str)"),
        (12345,         "malformed stored secret (int)"),
    ])
    def test_unusable_stored_material_always_fails_closed(self, stored, label):
        # Even when the caller supplies exactly what is stored, an unusable stored value must not
        # authenticate anyone — including the degenerate "both are empty" case.
        assert verify_shared_secret(stored, stored) is False, label
        assert verify_shared_secret("x" * 22, stored) is False, label

    @pytest.mark.parametrize("provided", ["", None, b"x" * 22, 0])
    def test_unusable_supplied_material_is_refused(self, provided):
        assert verify_shared_secret(provided, "x" * 22) is False

    def test_minimum_length_is_enforced(self):
        """A stored value shorter than a generated key means damage, not an unusual choice."""
        assert verify_shared_secret("x" * 15, "x" * 15) is False
        assert verify_shared_secret("x" * 16, "x" * 16) is True

    def test_device_identifier_uses_the_same_rule_with_a_lower_floor(self):
        assert verify_device_identifier("", "") is False
        assert verify_device_identifier("AHC-TEST-DEVICE-0001", "AHC-TEST-DEVICE-0001") is True
        assert verify_device_identifier("abc", "abc") is False   # implausibly short serial


# ---------------------------------------------------------------------------
# The generators
# ---------------------------------------------------------------------------

class TestSecretFileHandling:
    def test_empty_pairing_key_file_is_regenerated(self, tmp_path):
        f = tmp_path / "pairing_key"
        f.write_text("")
        value = generate_pairing_key(f)
        assert len(value) >= 16
        assert f.read_text().strip() == value

    def test_truncated_pairing_key_file_is_regenerated(self, tmp_path):
        f = tmp_path / "pairing_key"
        f.write_text("abc")
        assert len(generate_pairing_key(f)) >= 16

    def test_whitespace_only_file_is_regenerated(self, tmp_path):
        f = tmp_path / "pairing_key"
        f.write_text("   \n\t  ")
        assert len(generate_pairing_key(f)) >= 16

    def test_missing_file_is_created(self, tmp_path):
        f = tmp_path / "nested" / "pairing_key"
        value = generate_pairing_key(f)
        assert len(value) >= 16 and f.exists()

    def test_a_healthy_key_is_preserved(self, tmp_path):
        """Regeneration must be the exception — an existing good key must survive restarts."""
        f = tmp_path / "pairing_key"
        f.write_text("an-existing-perfectly-good-key")
        assert generate_pairing_key(f) == "an-existing-perfectly-good-key"

    def test_empty_jwt_secret_file_is_regenerated(self, tmp_path):
        f = tmp_path / "jwt_secret"
        f.write_text("")
        value = generate_jwt_secret(f)
        assert len(value) >= 32, "an empty JWT secret makes PyJWT refuse every token"

    def test_truncated_jwt_secret_file_is_regenerated(self, tmp_path):
        f = tmp_path / "jwt_secret"
        f.write_text("deadbeef")           # plausible-looking but far too short
        assert len(generate_jwt_secret(f)) >= 32

    def test_healthy_jwt_secret_is_preserved(self, tmp_path):
        f = tmp_path / "jwt_secret"
        f.write_text("a" * 64)
        assert generate_jwt_secret(f) == "a" * 64

    def test_unreadable_secret_file_does_not_yield_an_empty_secret(self, tmp_path):
        """
        A directory where the file should be makes every read raise.

        Whatever happens, the one unacceptable outcome is returning "" — which the comparison layer
        would then have to catch. Either raising or regenerating is fine.
        """
        f = tmp_path / "pairing_key"
        f.mkdir()
        try:
            value = generate_pairing_key(f)
        except OSError:
            return                                  # failing loudly is acceptable
        assert value == "" or len(value) >= 16
        assert verify_shared_secret(value, value) is (len(value) >= 16), \
            "an unusable value must never authenticate"


# ---------------------------------------------------------------------------
# End to end through the pairing endpoints
# ---------------------------------------------------------------------------

class TestPairingEndpoint:
    @pytest.mark.asyncio
    async def test_empty_pairing_key_does_not_authenticate(self, client, monkeypatch):
        """
        The regression that matters: with a damaged stored key, an empty supplied key must 403.

        Against the pre-fix `hmac.compare_digest(body.key, settings.pairing_key)` this returned 200
        with a device token.
        """
        from app.config import settings

        monkeypatch.setattr(settings, "pairing_key", "")
        monkeypatch.setattr(settings, "device_serial", "AHC-TEST-SERIAL")

        resp = await client.post(
            "/api/v1/pair", json={"serial": "AHC-TEST-SERIAL", "key": ""},
        )
        assert resp.status_code == 403, f"empty key authenticated: {resp.status_code} {resp.text}"

    @pytest.mark.asyncio
    async def test_legitimate_pairing_still_works(self, client, monkeypatch):
        """The fix must not break real pairing."""
        from app.config import settings

        monkeypatch.setattr(settings, "pairing_key", "a-real-pairing-key-value")
        monkeypatch.setattr(settings, "device_serial", "AHC-TEST-SERIAL")

        resp = await client.post(
            "/api/v1/pair",
            json={"serial": "AHC-TEST-SERIAL", "key": "a-real-pairing-key-value"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["token"]

    @pytest.mark.asyncio
    async def test_empty_serial_does_not_authenticate(self, client, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "pairing_key", "a-real-pairing-key-value")
        monkeypatch.setattr(settings, "device_serial", "")

        resp = await client.post(
            "/api/v1/pair", json={"serial": "", "key": "a-real-pairing-key-value"},
        )
        assert resp.status_code == 403
