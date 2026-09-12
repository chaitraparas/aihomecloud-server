"""
_try_download_prebuilt() downloads a pre-built telegram-bot-api binary from GitHub Releases and
installs it, running it as a systemd service. The old check only asked "is this some ELF file"
(via the `file` command) -- which any substituted ELF binary of the same architecture trivially
satisfies. It never verified the download was the specific binary GitHub's release actually
published, so a compromised/stale CDN edge, a corrupted transfer, or a substituted redirect
target would be installed and run without detection.

`test_old_elf_only_check_could_not_tell_a_real_binary_from_a_substituted_one` reproduces that gap
directly. Everything else proves the new sha256-digest check (fetched from GitHub's own Releases
API, over the same trust the download itself already relies on) rejects a mismatch and still
installs a genuine match -- see _fetch_expected_sha256's docstring in telegram_routes.py for
exactly what this does and does not defend against.
"""

import hashlib
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.routes import telegram_routes as tr

_REAL_ELF_MAGIC = b"\x7fELF" + b"\x00" * 60  # enough for `file` to call it an ELF binary


def _digest_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class TestOldElfOnlyCheckMissedSubstitution:
    def test_old_elf_only_check_could_not_tell_a_real_binary_from_a_substituted_one(self):
        """Reproduces the vulnerable behaviour: the old guard was `"ELF" in file_output` --
        a substituted ELF binary (different bytes entirely) passes it identically to the real one."""
        real_binary = _REAL_ELF_MAGIC + b"legitimate telegram-bot-api build"
        substituted_binary = _REAL_ELF_MAGIC + b"attacker-controlled payload"

        def old_check_accepts(content: bytes) -> bool:
            # Mirrors the old code's only gate: `"ELF" not in file_out` -> reject.
            # A real `file` call reports "ELF" for both these byte strings identically.
            return b"\x7fELF" == content[:4]

        assert old_check_accepts(real_binary) is True
        assert old_check_accepts(substituted_binary) is True, (
            "the old check cannot distinguish the two -- both are 'valid ELF files' to it"
        )
        assert _digest_of(real_binary) != _digest_of(substituted_binary), (
            "a digest check WOULD distinguish them -- which is exactly the gap being closed"
        )


def _release_api_response(asset_name: str, digest_hex: str | None) -> str:
    asset = {"name": asset_name}
    if digest_hex is not None:
        asset["digest"] = f"sha256:{digest_hex}"
    return json.dumps({"tag_name": "tgapi-v1.0.0", "assets": [asset]})


class TestFetchExpectedSha256:
    async def test_parses_digest_for_the_matching_asset(self):
        api_json = _release_api_response("telegram-bot-api-linux-arm64", "a" * 64)

        with patch.object(tr, "run_command", new=AsyncMock(return_value=(0, api_json, ""))):
            digest, err = await tr._fetch_expected_sha256("telegram-bot-api-linux-arm64")

        assert digest == "a" * 64
        assert err == ""

    async def test_fails_closed_when_the_api_is_unreachable(self):
        with patch.object(tr, "run_command", new=AsyncMock(return_value=(22, "", "HTTP 404"))):
            digest, err = await tr._fetch_expected_sha256("telegram-bot-api-linux-arm64")

        assert digest is None
        assert "GitHub Releases API" in err

    async def test_fails_closed_when_the_asset_has_no_published_digest(self):
        api_json = _release_api_response("telegram-bot-api-linux-arm64", None)

        with patch.object(tr, "run_command", new=AsyncMock(return_value=(0, api_json, ""))):
            digest, err = await tr._fetch_expected_sha256("telegram-bot-api-linux-arm64")

        assert digest is None
        assert "no sha256 digest" in err

    async def test_fails_closed_when_the_asset_is_not_in_the_release_at_all(self):
        api_json = _release_api_response("telegram-bot-api-linux-amd64", "b" * 64)

        with patch.object(tr, "run_command", new=AsyncMock(return_value=(0, api_json, ""))):
            digest, err = await tr._fetch_expected_sha256("telegram-bot-api-linux-arm64")

        assert digest is None
        assert "not found" in err

    async def test_fails_closed_on_malformed_json(self):
        with patch.object(tr, "run_command", new=AsyncMock(return_value=(0, "not json", ""))):
            digest, err = await tr._fetch_expected_sha256("telegram-bot-api-linux-arm64")

        assert digest is None
        assert "malformed" in err


class TestTryDownloadPrebuiltIntegrityCheck:
    def _make_fake_run_command(self, tmp_path, api_json, downloaded_content, file_says_elf=True):
        async def fake_run_command(cmd, timeout=30):
            if cmd[0] == "curl" and cmd[-1] == tr._GITHUB_API_LATEST_RELEASE:
                return 0, api_json, ""
            if cmd[0] == "curl":
                # -o <tmp_path> <url> -- write the "downloaded" bytes.
                out_path = cmd[cmd.index("-o") + 1]
                with open(out_path, "wb") as f:
                    f.write(downloaded_content)
                return 0, "", ""
            if cmd[0] == "file":
                return 0, ("ELF 64-bit" if file_says_elf else "ASCII text"), ""
            raise AssertionError(f"unexpected command: {cmd}")

        return fake_run_command

    async def test_rejects_a_binary_whose_digest_does_not_match(self, tmp_path, monkeypatch):
        real_content = _REAL_ELF_MAGIC + b"legitimate build"
        substituted_content = _REAL_ELF_MAGIC + b"substituted payload"
        asset_name = "telegram-bot-api-linux-arm64"
        api_json = _release_api_response(asset_name, _digest_of(real_content))

        monkeypatch.setattr(tr.platform, "machine", lambda: "aarch64")
        fake_run_command = self._make_fake_run_command(tmp_path, api_json, substituted_content)

        with patch.object(tr, "run_command", new=AsyncMock(side_effect=fake_run_command)), \
             patch.object(tr, "_install_binary_from", new=AsyncMock()) as mock_install:
            ok, msg = await tr._try_download_prebuilt()

        assert ok is False
        assert "integrity check" in msg
        mock_install.assert_not_called()

    async def test_installs_a_binary_whose_digest_matches(self, monkeypatch):
        real_content = _REAL_ELF_MAGIC + b"legitimate build"
        asset_name = "telegram-bot-api-linux-arm64"
        api_json = _release_api_response(asset_name, _digest_of(real_content))

        monkeypatch.setattr(tr.platform, "machine", lambda: "aarch64")
        fake_run_command = self._make_fake_run_command(None, api_json, real_content)

        with patch.object(tr, "run_command", new=AsyncMock(side_effect=fake_run_command)), \
             patch.object(tr, "_install_binary_from", new=AsyncMock(return_value=(True, ""))) as mock_install:
            ok, msg = await tr._try_download_prebuilt()

        assert ok is True, msg
        mock_install.assert_called_once()

    async def test_never_downloads_when_the_digest_cannot_be_established(self, monkeypatch):
        """Fail-closed: if the Releases API can't be reached, skip the download entirely
        rather than installing something nothing has verified."""
        monkeypatch.setattr(tr.platform, "machine", lambda: "aarch64")

        download_attempted = False

        async def fake_run_command(cmd, timeout=30):
            nonlocal download_attempted
            if cmd[0] == "curl" and cmd[-1] == tr._GITHUB_API_LATEST_RELEASE:
                return 22, "", "HTTP 404"
            download_attempted = True
            raise AssertionError("should not download without a verified digest")

        with patch.object(tr, "run_command", new=AsyncMock(side_effect=fake_run_command)), \
             patch.object(tr, "_install_binary_from", new=AsyncMock()) as mock_install:
            ok, msg = await tr._try_download_prebuilt()

        assert ok is False
        assert "Could not verify integrity" in msg
        assert download_attempted is False
        mock_install.assert_not_called()
