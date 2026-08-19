"""
System, Family, Service, Network, Health, and Job route tests.
Covers endpoints not yet tested by existing test files.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


# ─── Health ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(client: AsyncClient):
    """GET /api/health returns status + device identity, without authentication."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "deviceName" in data
    assert "serial" in data


@pytest.mark.asyncio
async def test_root_endpoint_returns_device_info(client: AsyncClient):
    """GET / returns service info."""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "AiHomeCloud"
    assert "serial" in data


# ─── System ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_system_info_returns_device(authenticated_client: AsyncClient):
    """GET /api/v1/system/info returns device info."""
    response = await authenticated_client.get("/api/v1/system/info")
    assert response.status_code == 200
    data = response.json()
    assert "serial" in data
    assert "name" in data
    assert "ip" in data
    assert "backendVersion" in data


@pytest.mark.asyncio
async def test_system_firmware_check(authenticated_client: AsyncClient):
    """GET /api/v1/system/firmware returns firmware info."""
    response = await authenticated_client.get("/api/v1/system/firmware")
    assert response.status_code == 200
    data = response.json()
    assert "current_version" in data
    assert "latest_version" in data
    assert "update_available" in data


@pytest.mark.asyncio
async def test_system_update_trigger(authenticated_client: AsyncClient, tmp_path):
    """POST /api/v1/system/update stages the upload and triggers the apply unit.
    See tests/test_system_routes.py for admin-gating, version-comparison, and failure-path
    coverage — this is a light smoke check matching this file's style."""
    from unittest.mock import AsyncMock, patch

    with patch("app.routes.system_routes.settings.update_staging_dir", tmp_path), \
         patch("app.routes.system_routes.settings.update_status_file", tmp_path / "update_status"), \
         patch(
             "app.routes.system_routes.run_command",
             new_callable=AsyncMock, return_value=(0, "", ""),
         ):
        response = await authenticated_client.post(
            "/api/v1/system/update",
            params={"version": "999.0.0"},
            data={"pin": "0000"},
            files={"file": ("backend_bundle.tar", b"fake tar bytes", "application/x-tar")},
        )
    assert response.status_code == 202


@pytest.mark.asyncio
async def test_system_name_update(authenticated_client: AsyncClient):
    """PUT /api/v1/system/name updates the device name."""
    response = await authenticated_client.put(
        "/api/v1/system/name",
        json={"name": "TestCubie"},
    )
    assert response.status_code == 204

    # Verify the name changed
    response = await authenticated_client.get("/api/v1/system/info")
    assert response.json()["name"] == "TestCubie"


@pytest.mark.asyncio
async def test_system_name_empty_returns_400(authenticated_client: AsyncClient):
    """PUT /api/v1/system/name with empty name returns 400 or 422 (Pydantic min_length)."""
    response = await authenticated_client.put(
        "/api/v1/system/name",
        json={"name": ""},
    )
    assert response.status_code in (400, 422)


@pytest.mark.asyncio
async def test_system_info_requires_auth(client: AsyncClient):
    """GET /api/v1/system/info without auth returns 401/403."""
    response = await client.get("/api/v1/system/info")
    assert response.status_code in (401, 403)


# ─── Family / Users ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_family_list_returns_users(authenticated_client: AsyncClient):
    """GET /api/v1/users/family returns list of users."""
    response = await authenticated_client.get("/api/v1/users/family")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    # At least the admin user should be present
    assert len(data) >= 1
    user = data[0]
    assert "id" in user
    assert "name" in user
    assert "isAdmin" in user
    assert "folderSizeGB" in user
    assert "avatarColor" in user


@pytest.mark.asyncio
async def test_add_family_member(authenticated_client: AsyncClient):
    """POST /api/v1/users/family adds a new family member."""
    response = await authenticated_client.post(
        "/api/v1/users/family",
        json={"name": "TestChild"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "TestChild"
    assert data["isAdmin"] is False


@pytest.mark.asyncio
async def test_add_family_empty_name_returns_400(authenticated_client: AsyncClient):
    """POST /api/v1/users/family with empty name returns 400."""
    response = await authenticated_client.post(
        "/api/v1/users/family",
        json={"name": ""},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_add_family_overlong_name_rejected(authenticated_client: AsyncClient):
    """L-1 (security audit 2026-08): an unbounded name becomes a filesystem directory name
    downstream (store.add_user), matching CreateUserRequest's sibling 64-char cap."""
    response = await authenticated_client.post(
        "/api/v1/users/family",
        json={"name": "a" * 65},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_add_family_name_at_max_length_accepted(authenticated_client: AsyncClient):
    """The cap is a rejection boundary, not an off-by-one trap."""
    response = await authenticated_client.post(
        "/api/v1/users/family",
        json={"name": "a" * 64},
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_remove_family_member(authenticated_client: AsyncClient):
    """DELETE /api/v1/users/family/{id} removes the user."""
    # Create a user first
    resp = await authenticated_client.post(
        "/api/v1/users/family",
        json={"name": "ToRemove"},
    )
    user_id = resp.json()["id"]

    # Remove
    response = await authenticated_client.delete(f"/api/v1/users/family/{user_id}")
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_remove_nonexistent_family_returns_404(authenticated_client: AsyncClient):
    """DELETE /api/v1/users/family/{id} with bad id returns 404."""
    response = await authenticated_client.delete("/api/v1/users/family/nonexistent_id")
    assert response.status_code == 404


# ─── Services ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_services_list(authenticated_client: AsyncClient):
    """GET /api/v1/services returns list of services."""
    response = await authenticated_client.get("/api/v1/services")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    for svc in data:
        assert "id" in svc
        assert "name" in svc
        assert "isEnabled" in svc


@pytest.mark.asyncio
async def test_service_toggle(authenticated_client: AsyncClient):
    """POST /api/v1/services/{id}/toggle toggles service state."""
    # Get services first
    resp = await authenticated_client.get("/api/v1/services")
    services = resp.json()
    if services:
        svc_id = services[0]["id"]
        response = await authenticated_client.post(
            f"/api/v1/services/{svc_id}/toggle",
            json={"enabled": False},
        )
        # 204 on success, or error if systemd not available
        assert response.status_code == 204


@pytest.mark.asyncio
async def test_services_list_includes_smb_and_nfs(authenticated_client: AsyncClient):
    """GET /api/v1/services surfaces distinct smb and nfs rows (not a bundled 'media')."""
    response = await authenticated_client.get("/api/v1/services")
    assert response.status_code == 200
    ids = {svc["id"] for svc in response.json()}
    assert "smb" in ids
    assert "nfs" in ids
    assert "media" not in ids


@pytest.mark.asyncio
async def test_service_toggle_smb_persists_across_reboot(authenticated_client: AsyncClient):
    """Toggling smb on should both start smbd/nmbd directly AND start the
    ahc-enable-unit@ host-namespace helper for each, so the state survives a
    reboot. Enable/disable can't go through a direct `systemctl enable` for
    these SysV-compat units (found live 2026-07-14: update-rc.d does its own
    root check that a polkit grant for the systemd D-Bus call doesn't satisfy)
    — see PERSISTABLE_UNITS' docstring in service_routes.py."""
    with patch(
        "app.routes.service_routes.run_command",
        new_callable=AsyncMock,
        return_value=(0, "", ""),
    ) as mock_run:
        response = await authenticated_client.post(
            "/api/v1/services/smb/toggle",
            json={"enabled": True},
        )
        assert response.status_code == 204

        calls = [call.args[0] for call in mock_run.call_args_list]
        assert ["systemctl", "start", "smbd"] in calls
        assert ["systemctl", "start", "nmbd"] in calls
        assert ["systemctl", "start", "ahc-enable-unit@smbd.service"] in calls
        assert ["systemctl", "start", "ahc-enable-unit@nmbd.service"] in calls


@pytest.mark.asyncio
async def test_service_toggle_nonexistent_returns_400(authenticated_client: AsyncClient):
    """Toggle a non-whitelisted service returns 400."""
    response = await authenticated_client.post(
        "/api/v1/services/nonexistent_service/toggle",
        json={"enabled": True},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_services_list_requires_auth(client: AsyncClient):
    """GET /api/v1/services without auth returns 401/403."""
    response = await client.get("/api/v1/services")
    assert response.status_code in (401, 403)


# ─── Jobs ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_job_nonexistent_returns_404(authenticated_client: AsyncClient):
    """GET /api/v1/jobs/{id} with nonexistent id returns 404."""
    response = await authenticated_client.get("/api/v1/jobs/nonexistent_job_id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_job_requires_auth(client: AsyncClient):
    """GET /api/v1/jobs/{id} without auth returns 401/403."""
    response = await client.get("/api/v1/jobs/some_id")
    assert response.status_code in (401, 403)


# ─── Backward compatibility redirect ────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_redirect_preserves_path(client: AsyncClient):
    """GET /api/health should still work (unversioned health is direct, not redirected)."""
    response = await client.get("/api/health")
    assert response.status_code == 200


# ─── Cert fingerprint ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cert_fingerprint_endpoint(client: AsyncClient):
    """GET /api/v1/auth/cert-fingerprint returns fingerprint info."""
    response = await client.get("/api/v1/auth/cert-fingerprint")
    assert response.status_code == 200
    data = response.json()
    assert "fingerprint" in data
    assert data["algorithm"] == "sha256"


# ─── QR Pairing ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_qr_pairing_endpoint(client: AsyncClient):
    """GET /api/v1/pair/qr returns pairing QR info."""
    response = await client.get("/api/v1/pair/qr")
    assert response.status_code == 200
    data = response.json()
    assert "qrValue" in data
    assert "serial" in data
    assert "ip" in data
    assert "expiresAt" in data


@pytest.mark.asyncio
async def test_pair_with_wrong_serial_returns_403(client: AsyncClient):
    """POST /api/v1/pair with wrong serial returns 403."""
    response = await client.post(
        "/api/v1/pair",
        json={"serial": "WRONG-SERIAL", "key": "wrong-key"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_pair_with_wrong_key_returns_403(client: AsyncClient):
    """POST /api/v1/pair with correct serial but wrong key returns 403."""
    from app.config import settings

    response = await client.post(
        "/api/v1/pair",
        json={"serial": settings.device_serial, "key": "wrong-key"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_pair_with_correct_credentials(client: AsyncClient):
    """POST /api/v1/pair with correct serial and key returns JWT."""
    from app.config import settings

    response = await client.post(
        "/api/v1/pair",
        json={"serial": settings.device_serial, "key": settings.pairing_key},
    )
    assert response.status_code == 200
    data = response.json()
    assert "token" in data


@pytest.mark.asyncio
async def test_pair_token_without_otp_is_not_admin(client: AsyncClient):
    """
    H1 regression: /pair (serial + key only, no OTP) must NOT yield an admin-capable token.
    Before the fix, require_admin granted admin to ANY type="device" token, so knowing the
    low-entropy serial plus the long-lived shared pairing key alone was enough to get full
    admin (reindex, storage format, reboot, family role changes) with no OTP step at all.
    """
    from app.config import settings

    pair_response = await client.post(
        "/api/v1/pair",
        json={"serial": settings.device_serial, "key": settings.pairing_key},
    )
    assert pair_response.status_code == 200
    token = pair_response.json()["token"]

    response = await client.post(
        "/api/v1/users/family",
        json={"name": "should_be_rejected"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403, "A non-OTP-verified pairing token must not grant admin"
    assert "Admin privileges required" in response.json().get("detail", "")


@pytest.mark.asyncio
async def test_pair_complete_with_valid_otp_is_admin(client: AsyncClient):
    """
    H1 regression counterpart: /pair/complete, which requires visual/physical access to the
    device's own QR-displayed OTP, must still yield the admin-capable device token — this is
    the intended trust boundary, just no longer reachable via /pair alone.
    """
    from app.config import settings

    qr_response = await client.get("/api/v1/pair/qr")
    assert qr_response.status_code == 200
    otp = qr_response.json()["otp"]

    complete_response = await client.post(
        "/api/v1/pair/complete",
        json={"serial": settings.device_serial, "key": settings.pairing_key, "otp": otp},
    )
    assert complete_response.status_code == 200
    token = complete_response.json()["token"]

    response = await client.post(
        "/api/v1/users/family",
        json={"name": "otp_verified_admin_can_add"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code != 403, "An OTP-verified pairing token should still grant admin"
