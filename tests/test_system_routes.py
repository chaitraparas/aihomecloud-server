"""
Tests for system_routes.py — device info, firmware check, device name.
"""

import json

import pytest
from unittest.mock import AsyncMock, patch


class TestSystemInfo:
    @pytest.mark.asyncio
    async def test_device_info(self, authenticated_client):
        resp = await authenticated_client.get("/api/v1/system/info")
        assert resp.status_code == 200
        data = resp.json()
        assert "serial" in data or "name" in data

    @pytest.mark.asyncio
    async def test_device_info_includes_os_eol_fields(self, authenticated_client):
        """GET /system/info surfaces osCodename/osEolDate/osEolWarning (Phase 2 of the
        SBC hardening plan). freedesktop_os_release() raises OSError on macOS/CI (no
        /etc/os-release), so codename falls back to "unknown" and the date/warning
        fields are absent — assert the graceful-degradation shape, not real values."""
        resp = await authenticated_client.get("/api/v1/system/info")
        assert resp.status_code == 200
        data = resp.json()
        assert "osCodename" in data
        assert "osEolDate" in data
        assert "osEolWarning" in data
        assert data["osCodename"] == "unknown"
        assert data["osEolDate"] is None
        assert data["osEolWarning"] is False

    @pytest.mark.asyncio
    async def test_firmware_info(self, authenticated_client):
        resp = await authenticated_client.get("/api/v1/system/firmware")
        assert resp.status_code == 200
        data = resp.json()
        assert "update_available" in data or "updateAvailable" in data

    @pytest.mark.asyncio
    async def test_firmware_reports_update_available_when_newer_version_supplied(
        self, authenticated_client,
    ):
        resp = await authenticated_client.get(
            "/api/v1/system/firmware",
            params={"available_version": "999.0.0", "changelog": "notes", "size_mb": 12.5},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is True
        assert data["latest_version"] == "999.0.0"
        assert data["changelog"] == "notes"

    @pytest.mark.asyncio
    async def test_firmware_reports_no_update_when_supplied_version_not_newer(
        self, authenticated_client,
    ):
        with patch("app.routes.system_routes.settings.backend_version", "5.0.0"):
            resp = await authenticated_client.get(
                "/api/v1/system/firmware", params={"available_version": "0.0.1"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is False

    @pytest.mark.asyncio
    async def test_trigger_update_requires_admin(self, member_token, client):
        resp = await client.post(
            "/api/v1/system/update",
            params={"version": "999.0.0"},
            data={"pin": "1111"},
            files={"file": ("backend_bundle.tar", b"fake tar bytes", "application/x-tar")},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_trigger_update_rejects_wrong_pin(self, authenticated_client):
        resp = await authenticated_client.post(
            "/api/v1/system/update",
            params={"version": "999.0.0"},
            data={"pin": "9999"},
            files={"file": ("backend_bundle.tar", b"fake tar bytes", "application/x-tar")},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "PIN does not match"

    @pytest.mark.asyncio
    async def test_trigger_update_rejects_non_newer_version(self, authenticated_client):
        with patch("app.routes.system_routes.settings.backend_version", "5.0.0"):
            resp = await authenticated_client.post(
                "/api/v1/system/update",
                params={"version": "0.0.1"},
                data={"pin": "0000"},
                files={"file": ("backend_bundle.tar", b"fake tar bytes", "application/x-tar")},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_trigger_update_stages_file_and_starts_unit(self, authenticated_client, tmp_path):
        with patch("app.routes.system_routes.settings.update_staging_dir", tmp_path), \
             patch("app.routes.system_routes.settings.update_status_file", tmp_path / "update_status"), \
             patch(
                 "app.routes.system_routes.run_command",
                 new_callable=AsyncMock, return_value=(0, "", ""),
             ) as mock_cmd:
            resp = await authenticated_client.post(
                "/api/v1/system/update",
                params={"version": "999.0.0"},
                data={"pin": "0000"},
                files={"file": ("backend_bundle.tar", b"fake tar bytes", "application/x-tar")},
            )
        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert data["version"] == "999.0.0"
        assert (tmp_path / "999.0.0.tar").read_bytes() == b"fake tar bytes"
        mock_cmd.assert_awaited_once()
        assert mock_cmd.await_args[0][0] == ["systemctl", "start", "--no-block", "ahc-apply-update@999.0.0.service"]

    @pytest.mark.asyncio
    async def test_trigger_update_claims_status_before_upload_starts(self, authenticated_client, tmp_path):
        """The status claim must happen synchronously before the (slow) upload, not after --
        otherwise a second request could race in during the upload window. Verified indirectly:
        inject a run_command that inspects the status file mid-request."""
        status_file = tmp_path / "update_status"
        seen_status_during_upload = {}

        async def _fake_run_command(cmd, *a, **kw):
            seen_status_during_upload["value"] = status_file.read_text()
            return (0, "", "")

        with patch("app.routes.system_routes.settings.update_staging_dir", tmp_path), \
             patch("app.routes.system_routes.settings.update_status_file", status_file), \
             patch("app.routes.system_routes.run_command", side_effect=_fake_run_command):
            resp = await authenticated_client.post(
                "/api/v1/system/update",
                params={"version": "999.0.0"},
                data={"pin": "0000"},
                files={"file": ("backend_bundle.tar", b"fake tar bytes", "application/x-tar")},
            )
        assert resp.status_code == 202
        assert seen_status_during_upload["value"] == "applying:999.0.0"

    @pytest.mark.asyncio
    async def test_trigger_update_rejects_concurrent_update(self, authenticated_client, tmp_path):
        status_file = tmp_path / "update_status"
        status_file.write_text("applying:5.0.0")
        with patch("app.routes.system_routes.settings.update_status_file", status_file):
            resp = await authenticated_client.post(
                "/api/v1/system/update",
                params={"version": "999.0.0"},
                data={"pin": "0000"},
                files={"file": ("backend_bundle.tar", b"fake tar bytes", "application/x-tar")},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_trigger_update_returns_500_when_unit_start_fails(self, authenticated_client, tmp_path):
        status_file = tmp_path / "update_status"
        with patch("app.routes.system_routes.settings.update_staging_dir", tmp_path), \
             patch("app.routes.system_routes.settings.update_status_file", status_file), \
             patch(
                 "app.routes.system_routes.run_command",
                 new_callable=AsyncMock, return_value=(1, "", "unit not found"),
             ):
            resp = await authenticated_client.post(
                "/api/v1/system/update",
                params={"version": "999.0.0"},
                data={"pin": "0000"},
                files={"file": ("backend_bundle.tar", b"fake tar bytes", "application/x-tar")},
            )
        assert resp.status_code == 500
        # staged file cleaned up on failure to start the apply unit
        assert not (tmp_path / "999.0.0.tar").exists()
        # status claim released -- a failed trigger must not permanently block future attempts
        assert status_file.read_text() == "idle"

    @pytest.mark.asyncio
    async def test_update_status_idle_when_no_status_file(self, authenticated_client, tmp_path):
        with patch("app.routes.system_routes.settings") as mock_settings:
            mock_settings.update_status_file = tmp_path / "does_not_exist"
            resp = await authenticated_client.get("/api/v1/system/update/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "idle"

    @pytest.mark.asyncio
    async def test_update_status_reports_success(self, authenticated_client, tmp_path):
        status_file = tmp_path / "update_status"
        status_file.write_text("success:999.0.0")
        with patch("app.routes.system_routes.settings") as mock_settings:
            mock_settings.update_status_file = status_file
            resp = await authenticated_client.get("/api/v1/system/update/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"status": "success", "version": "999.0.0"}

    @pytest.mark.asyncio
    async def test_update_status_reports_failure_with_reason(self, authenticated_client, tmp_path):
        status_file = tmp_path / "update_status"
        status_file.write_text("failed:999.0.0:health_check_timeout")
        with patch("app.routes.system_routes.settings") as mock_settings:
            mock_settings.update_status_file = status_file
            resp = await authenticated_client.get("/api/v1/system/update/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "status": "failed", "version": "999.0.0", "reason": "health_check_timeout",
        }


class TestDeviceName:
    @pytest.mark.asyncio
    async def test_update_name(self, authenticated_client):
        with patch("app.routes.system_routes.store") as mock_store:
            mock_store.update_device_name = AsyncMock()
            resp = await authenticated_client.put(
                "/api/v1/system/name",
                json={"name": "My Cubie"},
            )
            assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_update_name_empty(self, authenticated_client):
        resp = await authenticated_client.put(
            "/api/v1/system/name",
            json={"name": "  "},
        )
        assert resp.status_code == 400


class TestPowerEndpoints:
    @pytest.mark.asyncio
    async def test_shutdown(self, authenticated_client):
        with patch("app.routes.system_routes.store") as mock_store, \
             patch("app.routes.system_routes.run_command", new_callable=AsyncMock, return_value=(0, "", "")), \
             patch("app.routes.system_routes._deferred_power_command", new_callable=AsyncMock):
            mock_store.get_services = AsyncMock(return_value=[])
            resp = await authenticated_client.post("/api/v1/system/shutdown")
            assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_reboot(self, authenticated_client):
        with patch("app.routes.system_routes._deferred_power_command", new_callable=AsyncMock):
            resp = await authenticated_client.post("/api/v1/system/reboot")
            assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_shutdown_stops_services(self, authenticated_client):
        with patch("app.routes.system_routes.store") as mock_store, \
             patch("app.routes.system_routes._systemctl_stop", new_callable=AsyncMock, return_value=(True, "")) as mock_stop, \
             patch("app.routes.system_routes._deferred_power_command", new_callable=AsyncMock):
            mock_store.get_services = AsyncMock(return_value=[
                {"id": "samba", "isEnabled": True},
            ])
            resp = await authenticated_client.post("/api/v1/system/shutdown")
            assert resp.status_code == 202
            assert mock_stop.called


class TestOsEolInfo:
    """_get_os_eol_info() — Phase 2 of the SBC hardening plan."""

    def test_known_codename_past_eol_warns(self):
        from app.routes.system_routes import _get_os_eol_info
        with patch(
            "app.routes.system_routes.platform.freedesktop_os_release",
            return_value={"VERSION_CODENAME": "bullseye"},
        ):
            codename, eol_date, warning = _get_os_eol_info()
        assert codename == "bullseye"
        assert eol_date == "2026-08-31"
        assert warning is True  # today (2026-07-14 per this session) is within 90 days

    def test_unknown_codename_no_warning(self):
        from app.routes.system_routes import _get_os_eol_info
        with patch(
            "app.routes.system_routes.platform.freedesktop_os_release",
            return_value={"VERSION_CODENAME": "some-future-release-not-in-the-map"},
        ):
            codename, eol_date, warning = _get_os_eol_info()
        assert codename == "some-future-release-not-in-the-map"
        assert eol_date is None
        assert warning is False

    def test_os_release_unreadable_falls_back_gracefully(self):
        from app.routes.system_routes import _get_os_eol_info
        with patch(
            "app.routes.system_routes.platform.freedesktop_os_release",
            side_effect=OSError("no /etc/os-release"),
        ):
            codename, eol_date, warning = _get_os_eol_info()
        assert codename == "unknown"
        assert eol_date is None
        assert warning is False


class TestFactoryReset:
    """authenticated_client's admin user has real PIN "0000" (conftest.admin_token) -- these use
    the real store/bcrypt verification, not mocks, since the whole point is verifying the PIN
    check actually works against a real stored hash, not a mocked one."""

    @pytest.mark.asyncio
    async def test_wrong_pin_returns_403_and_never_triggers_reset(self, authenticated_client):
        with patch("app.routes.system_routes._deferred_factory_reset", new_callable=AsyncMock) as mock_deferred:
            resp = await authenticated_client.post(
                "/api/v1/system/factory-reset",
                json={"mode": "keep_media", "pin": "9999"},
            )
            assert resp.status_code == 403
            mock_deferred.assert_not_called()

    @pytest.mark.asyncio
    async def test_correct_pin_keep_media_triggers_reset(self, authenticated_client):
        with patch("app.routes.system_routes._deferred_factory_reset", new_callable=AsyncMock) as mock_deferred:
            resp = await authenticated_client.post(
                "/api/v1/system/factory-reset",
                json={"mode": "keep_media", "pin": "0000"},
            )
            assert resp.status_code == 202
            mock_deferred.assert_awaited_once_with("keep-media")

    @pytest.mark.asyncio
    async def test_correct_pin_wipe_media_triggers_reset(self, authenticated_client):
        with patch("app.routes.system_routes._deferred_factory_reset", new_callable=AsyncMock) as mock_deferred:
            resp = await authenticated_client.post(
                "/api/v1/system/factory-reset",
                json={"mode": "wipe_media", "pin": "0000"},
            )
            assert resp.status_code == 202
            mock_deferred.assert_awaited_once_with("wipe-media")

    @pytest.mark.asyncio
    async def test_invalid_mode_rejected(self, authenticated_client):
        resp = await authenticated_client.post(
            "/api/v1/system/factory-reset",
            json={"mode": "delete_everything", "pin": "0000"},
        )
        assert resp.status_code == 422


class TestDeferredFactoryReset:
    @pytest.mark.asyncio
    async def test_starts_correct_systemd_instance(self):
        from app.routes.system_routes import _deferred_factory_reset
        with patch("app.routes.system_routes.asyncio.sleep", new_callable=AsyncMock), \
             patch("app.routes.system_routes.run_command", new_callable=AsyncMock, return_value=(0, "", "")) as mock_cmd:
            await _deferred_factory_reset("wipe-media")
            mock_cmd.assert_awaited_once_with(
                ["systemctl", "start", "ahc-factory-reset@wipe-media.service"], timeout=15,
            )


class TestAppUpdate:
    """Each board serves its own Android update independently from app_update_dir --
    replaces a prior mechanism hardcoded to one specific dev board's LAN-only IPs."""

    @pytest.mark.asyncio
    async def test_manifest_404_when_nothing_published(self, authenticated_client):
        resp = await authenticated_client.get("/api/v1/system/app-update/manifest")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_apk_404_when_nothing_published(self, authenticated_client):
        resp = await authenticated_client.get("/api/v1/system/app-update/apk")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_manifest_served_once_published(self, authenticated_client):
        from app.config import settings
        settings.app_update_dir.mkdir(parents=True, exist_ok=True)
        (settings.app_update_dir / "manifest.json").write_text(
            '{"versionCode": 42, "versionName": "1.2.3", "apkFilename": "app-update.apk"}'
        )
        resp = await authenticated_client.get("/api/v1/system/app-update/manifest")
        assert resp.status_code == 200
        assert resp.json() == {"versionCode": 42, "versionName": "1.2.3", "apkFilename": "app-update.apk"}

    @pytest.mark.asyncio
    async def test_apk_streamed_once_published(self, authenticated_client):
        from app.config import settings
        settings.app_update_dir.mkdir(parents=True, exist_ok=True)
        (settings.app_update_dir / "app-update.apk").write_bytes(b"fake apk bytes")
        resp = await authenticated_client.get("/api/v1/system/app-update/apk")
        assert resp.status_code == 200
        assert resp.content == b"fake apk bytes"

    @pytest.mark.asyncio
    async def test_app_update_requires_auth(self, client):
        resp = await client.get("/api/v1/system/app-update/manifest")
        assert resp.status_code == 401


class TestBoardIdentity:
    """GET /system/identity serves the H-11 signed SPKI rotation statement that
    ahc-issue-cert.sh (root) writes -- this service only ever reads it."""

    @pytest.mark.asyncio
    async def test_404_when_no_statement_published(self, client):
        resp = await client.get("/api/v1/system/identity")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_served_verbatim_once_published(self, client):
        from app.config import settings
        settings.identity_statement_path.parent.mkdir(parents=True, exist_ok=True)
        statement = {
            "identityPublicKey": "base64pubkey",
            "statement": {"spki": "base64spki", "serial": "0300DA660501847", "notBefore": 1, "epoch": 1},
            "signature": "base64sig",
        }
        settings.identity_statement_path.write_text(json.dumps(statement))

        resp = await client.get("/api/v1/system/identity")

        assert resp.status_code == 200
        assert resp.json() == statement

    @pytest.mark.asyncio
    async def test_unreadable_statement_returns_500_not_a_silent_pass(self, client):
        from app.config import settings
        settings.identity_statement_path.parent.mkdir(parents=True, exist_ok=True)
        settings.identity_statement_path.write_text("not json")

        resp = await client.get("/api/v1/system/identity")

        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_no_auth_required(self, client):
        """Unauthenticated by design: a client must verify this statement before it can trust
        the TLS connection it would otherwise authenticate over."""
        resp = await client.get("/api/v1/system/identity")
        assert resp.status_code != 401
