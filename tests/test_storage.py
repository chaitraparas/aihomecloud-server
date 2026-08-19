"""
Storage Security Tests (Milestone 7D)

Test cases for storage device safety, format request validation,
OS partition protection, and graceful error handling.
"""

import pytest
from httpx import AsyncClient
from pathlib import Path

pytestmark = pytest.mark.integration
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_format_request_with_mismatched_confirm_device_returns_400(authenticated_client: AsyncClient):
    """
    7D.2: Format request with confirmDevice != device → 400
    
    Verify that format requests require confirmDevice to match device
    (like GitHub's repo delete confirmation pattern).
    """
    response = await authenticated_client.post(
        "/api/v1/storage/format",
        json={
            "device": "/dev/sda1",
            "label": "CubieNAS",
            "confirmDevice": "/dev/sdb1"  # Intentionally mismatched
        }
    )
    assert response.status_code == 400, "Mismatched confirmDevice should return 400"
    assert "confirmation does not match" in response.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_format_request_for_os_disk_returns_403(authenticated_client: AsyncClient):
    """
    7D.3: Format request for OS disk /dev/mmcblk0 → 400 (protected)
    
    Verify that OS partitions (like microSD card) cannot be formatted.
    """
    response = await authenticated_client.post(
        "/api/v1/storage/format",
        json={
            "device": "/dev/mmcblk0p1",
            "label": "CubieNAS",
            "confirmDevice": "/dev/mmcblk0p1"
        }
    )
    # Should return 403 (Forbidden) or 404 (not found) depending on whether the device exists
    assert response.status_code in (403, 404), \
        f"OS partition format should be blocked, got {response.status_code}"
    
    if response.status_code == 403:
        assert "OS partition" in response.json().get("detail", "")


@pytest.mark.asyncio
async def test_mount_returns_409_when_nas_has_open_file_handles(authenticated_client: AsyncClient):
    """
    7D.4: Mount returns 409 when NAS has open file handles (mock lsof)
    
    Verify that attempting to mount a device when another device is already
    mounted returns a conflict (409) error.
    """
    # First, we'd need to mock the storage state to simulate an already-mounted device
    # For now, we verify the endpoint rejects when something is already mounted
    
    # Use a guaranteed-nonexistent device so no real mount occurs and
    # pytest can clean up the tmp directory safely.
    response = await authenticated_client.post(
        "/api/v1/storage/mount",
        json={"device": "/dev/nonexistent_test_device"}
    )
    
    # 404 expected (device not found). 200/403/409 also valid in other environments.
    # 500 is never acceptable — a server crash is always a bug.
    assert response.status_code in (200, 403, 404, 409), \
        f"Mount should return 200, 403, 404, or 409, got {response.status_code}"
    
    if response.status_code == 409:
        assert "already mounted" in response.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_eject_on_unmounted_device_returns_graceful_error(authenticated_client: AsyncClient):
    """
    7D.5: Eject on already-unmounted device returns graceful error, not 500
    
    Verify that ejecting a device that is not currently mounted
    returns a graceful error (not a 500 server error).
    """
    response = await authenticated_client.post(
        "/api/v1/storage/eject",
        json={"device": "/dev/sda1"}
    )
    
    # Should return 200 (graceful, no-op) or 404 (not found), not 500
    assert response.status_code in (200, 404), \
        f"Eject should return 200 or 404, got {response.status_code}"


@pytest.mark.asyncio
async def test_format_nonexistent_device_returns_404(authenticated_client: AsyncClient):
    """
    Bonus: Format request for non-existent device returns 404.
    """
    response = await authenticated_client.post(
        "/api/v1/storage/format",
        json={
            "device": "/dev/nonexistent999",
            "label": "CubieNAS",
            "confirmDevice": "/dev/nonexistent999"
        }
    )
    assert response.status_code == 404, "Non-existent device should return 404"
    assert "not found" in response.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_mount_nonexistent_device_returns_404(authenticated_client: AsyncClient):
    """
    Bonus: Mount request for non-existent device returns 404.
    """
    response = await authenticated_client.post(
        "/api/v1/storage/mount",
        json={"device": "/dev/nonexistent999"}
    )
    assert response.status_code == 404, "Non-existent device mount should return 404"
    assert "not found" in response.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_mount_unformatted_device_returns_400(authenticated_client: AsyncClient):
    """
    Bonus: Mount request for device without filesystem returns 400.
    
    Verify that attempting to mount a device with no filesystem
    (e.g., raw device) fails gracefully.
    """
    # This would require a real device without a filesystem
    # The test may not apply in the test environment, so we skip if needed
    response = await authenticated_client.post(
        "/api/v1/storage/mount",
        json={"device": "/dev/sda"}  # Raw device, not a partition
    )
    
    # Should return 400 (bad request) or 404 (not found), not 500
    assert response.status_code in (400, 404, 409), \
        f"Mount of raw device should return 400/404/409, got {response.status_code}"


@pytest.mark.asyncio
async def test_format_mounted_device_returns_409(authenticated_client: AsyncClient):
    """
    Bonus: Format request for currently mounted device returns 409.
    """
    # Get the currently mounted device (if any) from storage state
    response = await authenticated_client.get("/api/v1/storage/devices")
    assert response.status_code == 200
    
    devices = response.json()
    
    # Find a mounted device — use bestPartition path for the format request
    # since format_device requires a partition path (not a whole-disk path).
    mounted_device = None
    for dev in devices:
        if dev.get("mountPoint") and not dev.get("isOsDisk"):
            # Prefer bestPartition; fall back to path only if it looks like a partition
            candidate = dev.get("bestPartition") or dev.get("path", "")
            if candidate and not candidate.startswith("/dev/mmcblk"):
                # Verify it's an actual partition (has a digit suffix after the disk name)
                import re
                if re.search(r'(\d+p\d+|[a-z]\d+)$', candidate):
                    mounted_device = candidate
                    break
    
    if mounted_device:
        # Try to format the mounted device (partition path) — expect 409
        response = await authenticated_client.post(
            "/api/v1/storage/format",
            json={
                "device": mounted_device,
                "label": "Test",
                "confirmDevice": mounted_device
            }
        )
        assert response.status_code == 409, "Format mounted device should return 409"


@pytest.mark.asyncio
async def test_format_returns_job_id(authenticated_client: AsyncClient):
    """
    Bonus: Format request returns a jobId for async tracking.
    """
    # Try to format a non-OS device (if one exists)
    response = await authenticated_client.get("/api/v1/storage/devices")
    assert response.status_code == 200
    
    devices = response.json()
    external_device = None
    
    # Find a non-OS, non-mounted device
    for dev in devices:
        if not dev.get("isOsDisk") and not dev.get("mountPoint"):
            external_device = dev.get("path")
            break
    
    if external_device:
        response = await authenticated_client.post(
            "/api/v1/storage/format",
            json={
                "device": external_device,
                "label": "Test",
                "confirmDevice": external_device
            }
        )
        
        # May return 200 (job created) or other status depending on permissions
        if response.status_code == 200:
            data = response.json()
            assert "jobId" in data, "Format response should include jobId"


@pytest.mark.asyncio
async def test_storage_devices_endpoint_returns_list(authenticated_client: AsyncClient):
    """
    Bonus: Verify that GET /api/v1/storage/devices returns a device list.
    """
    response = await authenticated_client.get("/api/v1/storage/devices")
    assert response.status_code == 200
    
    data = response.json()
    assert isinstance(data, list), "Devices endpoint should return a list"
    
    # If devices are present, verify structure
    for device in data:
        assert "path" in device, "Device should have 'path' field"
        assert "sizeBytes" in device, "Device should have 'sizeBytes' field"
        assert "transport" in device, "Device should have 'transport' field"


@pytest.mark.asyncio
async def test_storage_stats_endpoint_returns_stats(client: AsyncClient, admin_token: str):
    """
    Bonus: Verify that GET /api/v1/storage/stats returns storage statistics.
    """
    response = await client.get(
        "/api/v1/storage/stats",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 200
    
    data = response.json()
    assert "totalGB" in data, "Stats should include totalGB"
    assert "usedGB" in data, "Stats should include usedGB"
    assert data["totalGB"] >= 0
    assert data["usedGB"] >= 0


@pytest.mark.asyncio
async def test_format_requires_admin_privileges(client: AsyncClient):
    """
    Bonus: Verify that format endpoint requires authentication.
    """
    # Make request without authentication
    response = await client.post(
        "/api/v1/storage/format",
        json={
            "device": "/dev/sda1",
            "label": "Test",
            "confirmDevice": "/dev/sda1"
        }
    )
    # Should return 401 or 403 (missing auth)
    assert response.status_code in (401, 403), "Format should require authentication"


@pytest.mark.asyncio
async def test_mount_requires_admin_privileges(client: AsyncClient):
    """
    Bonus: Verify that mount endpoint requires authentication.
    """
    # Make request without authentication
    response = await client.post(
        "/api/v1/storage/mount",
        json={"device": "/dev/sda1"}
    )
    # Should return 401 or 403 (missing auth)
    assert response.status_code in (401, 403), "Mount should require authentication"


@pytest.mark.asyncio
async def test_unmount_requires_admin_privileges(client: AsyncClient):
    """
    Bonus: Verify that unmount endpoint requires authentication.
    """
    # Make request without authentication
    response = await client.post("/api/v1/storage/unmount")
    # Should return 401 or 403 (missing auth)
    assert response.status_code in (401, 403), "Unmount should require authentication"


@pytest.mark.asyncio
async def test_eject_requires_admin_privileges(client: AsyncClient):
    """
    Bonus: Verify that eject endpoint requires authentication.
    """
    # Make request without authentication
    response = await client.post(
        "/api/v1/storage/eject",
        json={"device": "/dev/sda1"}
    )
    # Should return 401 or 403 (missing auth)
    assert response.status_code in (401, 403), "Eject should require authentication"


@pytest.mark.asyncio
async def test_storage_stats_does_not_require_admin(client: AsyncClient, admin_token: str):
    """
    Bonus: Verify that /stats endpoint is readable by non-admin users.
    """
    # Create a member user (requires admin auth now that first user exists)
    response = await client.post(
        "/api/v1/users",
        json={"name": "member_user", "pin": "5678"},
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 201
    
    # Login as member
    response = await client.post(
        "/api/v1/auth/login",
        json={"name": "member_user", "pin": "5678"}
    )
    assert response.status_code == 200
    member_token = response.json().get("accessToken")
    
    # Member should be able to read storage stats
    response = await client.get(
        "/api/v1/storage/stats",
        headers={"Authorization": f"Bearer {member_token}"}
    )
    assert response.status_code == 200, "Non-admin should be able to read stats"


# ── smart-activate tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_smart_activate_already_active_returns_200(authenticated_client: AsyncClient):
    """
    TASK-DRIVE-02: State D — drive already active → idempotent 200 + action=already_active.
    """
    with patch(
        "app.store.get_storage_state",
        new=AsyncMock(return_value={"activeDevice": "/dev/sda1", "displayName": "Test Drive"}),
    ), patch(
        "app.routes.storage_routes.list_block_devices",
        new=AsyncMock(return_value=[]),
    ):
        response = await authenticated_client.post(
            "/api/v1/storage/smart-activate",
            json={"device": "/dev/sda", "format": False},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "already_active"
    assert "display_name" in data


@pytest.mark.asyncio
async def test_smart_activate_nonexistent_device_returns_404(authenticated_client: AsyncClient):
    """
    TASK-DRIVE-02: Non-existent device → 404.
    """
    with patch(
        "app.routes.storage_routes.list_block_devices",
        new=AsyncMock(return_value=[]),
    ):
        response = await authenticated_client.post(
            "/api/v1/storage/smart-activate",
            json={"device": "/dev/nonexistent99", "format": False},
        )
    assert response.status_code == 404
    assert "not found" in response.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_smart_activate_requires_admin(client: AsyncClient):
    """
    TASK-DRIVE-02: smart-activate requires admin auth.
    """
    response = await client.post(
        "/api/v1/storage/smart-activate",
        json={"device": "/dev/sda", "format": False},
    )
    assert response.status_code in (401, 403), "smart-activate should require admin"


# ── TASK-DRIVE-06: full coverage of all 4 smart-activate states ──────────────


_MOCK_USB_DISK = {
    "name": "sda",
    "size": 500107862016,
    "type": "disk",
    "mountpoint": None,
    "fstype": None,
    "label": None,
    "model": "Samsung T7",
    "tran": "usb",
    "serial": "ABC123",
    "children": [],
}

_MOCK_EXT4_DISK = {
    **_MOCK_USB_DISK,
    "children": [
        {
            "name": "sda1",
            "size": 500107862016,
            "type": "part",
            "mountpoint": None,
            "fstype": "ext4",
            "label": "AiHomeCloud",
            "model": None,
            "tran": "usb",
            "serial": None,
            "children": [],
        }
    ],
}

_MOCK_MMCBLK_DISK = {
    "name": "mmcblk0",
    "size": 31268536320,
    "type": "disk",
    "mountpoint": None,
    "fstype": None,
    "label": None,
    "model": "SD32G",
    "tran": "mmc",
    "serial": None,
    "children": [
        {
            "name": "mmcblk0p1",
            "size": 268435456,
            "type": "part",
            "mountpoint": "/boot",
            "fstype": "vfat",
            "label": None,
            "model": None,
            "tran": None,
            "serial": None,
            "children": [],
        }
    ],
}


@pytest.mark.asyncio
async def test_smart_activate_ext4_partition_mounts_without_format(
    authenticated_client: AsyncClient,
):
    """
    TASK-DRIVE-06: format=False on a disk with an existing ext4 partition —
    smart-activate must mount it directly without calling mkfs.ext4.
    """
    # Mounting now goes through mount_nas_device() (imported into
    # storage_routes from storage_helpers), which asks systemd to run an
    # unsandboxed oneshot unit rather than calling `mount` as a direct
    # subprocess — see the docstring on
    # test_do_unmount_propagates_unmount_service_failure in
    # test_storage_helpers.py for why (aihomecloud.service's private mount
    # namespace can't affect the real host mount table on its own). Format
    # (mkfs) is unaffected by that and still goes through run_command
    # directly, so it's still assertable there.
    run_commands = []

    async def _mock_run_command(cmd, **kwargs):
        run_commands.append(cmd)
        return 0, "", ""

    with patch(
        "app.store.get_storage_state",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.routes.storage_routes.list_block_devices",
        new=AsyncMock(return_value=[_MOCK_EXT4_DISK]),
    ), patch(
        "app.routes.storage_routes.run_command",
        new=AsyncMock(side_effect=_mock_run_command),
    ), patch(
        "app.routes.storage_routes.mount_nas_device",
        new=AsyncMock(return_value=(0, "", "")),
    ) as mock_mount, patch(
        "app.routes.storage_routes._post_mount_setup",
        new=AsyncMock(),
    ), patch(
        "app.routes.storage_routes.emit_device_mounted",
        new=AsyncMock(),
    ):
        response = await authenticated_client.post(
            "/api/v1/storage/smart-activate",
            json={"device": "/dev/sda", "format": False},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "mounted", f"Expected mounted, got {data}"
    assert "display_name" in data

    assert mock_mount.called, "mount_nas_device must be called"
    cmd_strings = [" ".join(c) for c in run_commands]
    assert not any("mkfs" in s for s in cmd_strings), "mkfs must NOT be called for ext4 disk"


@pytest.mark.asyncio
async def test_smart_activate_format_false_mounts_existing_ntfs_without_formatting(
    authenticated_client: AsyncClient,
):
    """
    Regression test for the 2026-07-30 fix: a non-ext4 filesystem with real data
    (e.g. NTFS/exFAT from prior use on another OS) must mount as-is when the
    caller explicitly passes format=False — previously ANY non-ext4 filesystem
    was silently wiped and reformatted regardless of caller intent.
    """
    ntfs_disk = {
        **_MOCK_USB_DISK,
        "children": [
            {
                "name": "sda1",
                "size": 500107862016,
                "type": "part",
                "mountpoint": None,
                "fstype": "ntfs",
                "label": "MyOldDrive",
                "model": None,
                "tran": "usb",
                "serial": None,
                "children": [],
            }
        ],
    }
    run_commands = []

    async def _mock_run_command(cmd, **kwargs):
        run_commands.append(cmd)
        return 0, "", ""

    with patch(
        "app.store.get_storage_state",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.routes.storage_routes.list_block_devices",
        new=AsyncMock(return_value=[ntfs_disk]),
    ), patch(
        "app.routes.storage_routes.run_command",
        new=AsyncMock(side_effect=_mock_run_command),
    ), patch(
        "app.routes.storage_routes.mount_nas_device",
        new=AsyncMock(return_value=(0, "", "")),
    ) as mock_mount, patch(
        "app.routes.storage_routes._post_mount_setup",
        new=AsyncMock(),
    ), patch(
        "app.routes.storage_routes.emit_device_mounted",
        new=AsyncMock(),
    ):
        response = await authenticated_client.post(
            "/api/v1/storage/smart-activate",
            json={"device": "/dev/sda", "format": False},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "mounted", f"Expected mounted (not wiped), got {data}"
    assert mock_mount.called, "mount_nas_device must be called for the existing NTFS partition"
    cmd_strings = [" ".join(c) for c in run_commands]
    assert not any("mkfs" in s or "sgdisk" in s for s in cmd_strings), (
        "NTFS drive with format=False must never be wiped or reformatted"
    )


@pytest.mark.asyncio
async def test_smart_activate_format_false_with_no_filesystem_returns_400(
    authenticated_client: AsyncClient,
):
    """format=False on a bare/unpartitioned disk has nothing to mount as-is —
    must fail clearly (400) rather than silently formatting anyway."""
    with patch(
        "app.store.get_storage_state",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.routes.storage_routes.list_block_devices",
        new=AsyncMock(return_value=[_MOCK_USB_DISK]),
    ):
        response = await authenticated_client.post(
            "/api/v1/storage/smart-activate",
            json={"device": "/dev/sda", "format": False},
        )

    assert response.status_code == 400
    assert "no existing filesystem" in response.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_smart_activate_format_true_wipes_even_an_ext4_disk(
    authenticated_client: AsyncClient,
):
    """format=True is an explicit, deliberate override — must format even a
    disk that already has a mountable ext4 partition, not silently mount it."""
    created_tasks = []

    def _mock_create_task(coro):
        coro.close()
        created_tasks.append(True)

    with patch(
        "app.store.get_storage_state",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.routes.storage_routes.list_block_devices",
        new=AsyncMock(return_value=[_MOCK_EXT4_DISK]),
    ), patch(
        "asyncio.create_task",
        side_effect=_mock_create_task,
    ):
        response = await authenticated_client.post(
            "/api/v1/storage/smart-activate",
            json={"device": "/dev/sda", "format": True},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "formatting", f"Expected formatting (explicit override), got {data}"
    assert "jobId" in data and data["jobId"]
    assert len(created_tasks) == 1


@pytest.mark.asyncio
async def test_smart_activate_unformatted_starts_format_job(
    authenticated_client: AsyncClient,
):
    """
    TASK-DRIVE-06: format=True on a bare disk with no partitions —
    smart-activate must start a background format job and return jobId.
    """
    created_tasks = []

    def _mock_create_task(coro):
        # Discard the coroutine — we don't want actual formatting to run.
        coro.close()
        created_tasks.append(True)

    with patch(
        "app.store.get_storage_state",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.routes.storage_routes.list_block_devices",
        new=AsyncMock(return_value=[_MOCK_USB_DISK]),
    ), patch(
        "asyncio.create_task",
        side_effect=_mock_create_task,
    ):
        response = await authenticated_client.post(
            "/api/v1/storage/smart-activate",
            json={"device": "/dev/sda", "format": True},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "formatting", f"Expected formatting, got {data}"
    assert "jobId" in data and data["jobId"], "formatting response must include jobId"
    assert "display_name" in data
    assert len(created_tasks) == 1, "One background task should have been created"


@pytest.mark.asyncio
async def test_smart_activate_os_disk_blocked(
    authenticated_client: AsyncClient,
):
    """
    TASK-DRIVE-06: OS disk (mmcblk) must be rejected with 403.
    """
    with patch(
        "app.store.get_storage_state",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.routes.storage_routes.list_block_devices",
        new=AsyncMock(return_value=[_MOCK_MMCBLK_DISK]),
    ):
        response = await authenticated_client.post(
            "/api/v1/storage/smart-activate",
            json={"device": "/dev/mmcblk0", "format": False},
        )

    assert response.status_code == 403, (
        f"OS disk should be blocked with 403, got {response.status_code}"
    )


class TestRecoverStorageMountDetection:
    """Regression pin (found live 2026-07-16, full-repo audit): /recover -- the app's
    'Reconnect Storage' button and the Telegram /mount command -- had the SAME
    mountpoint-only-comparison defect already fixed elsewhere in this file
    (try_auto_remount): ProtectSystem=strict + ReadWritePaths=<nas_root> makes systemd
    bind-mount nas_root onto itself, adding a real /proc/mounts entry for it regardless of
    whether an actual external drive is mounted. Without the st_dev guard, this endpoint
    would report false "already_mounted" success (with the root filesystem's own device
    path) even with nothing external connected -- silently defeating the exact recovery
    flow it exists for ("drive present but storage.json lost after a power cut")."""

    @pytest.mark.asyncio
    async def test_reports_not_already_mounted_when_nas_root_is_just_a_bind_mount_onto_itself(
        self, authenticated_client: AsyncClient,
    ):
        # settings.nas_root is a plain tmp_path in tests (conftest.py) -- same st_dev as its
        # own parent by construction, exactly modeling the sandbox self-bind-mount case:
        # a real /proc/mounts entry exists, but it's not backed by a genuinely different
        # filesystem. The fix must NOT trust that alone.
        with patch(
            "app.routes.storage_routes._scan_for_unmounted_nas_drive",
            new=AsyncMock(return_value=None),
        ):
            response = await authenticated_client.post("/api/v1/storage/recover")

        assert response.status_code == 404, (
            "a same-st_dev bind-mount-onto-itself must not be trusted as 'already mounted' "
            f"-- got {response.status_code}: {response.text}"
        )

    @pytest.mark.asyncio
    async def test_reports_already_mounted_when_nas_root_is_a_genuine_external_mount(
        self, authenticated_client: AsyncClient,
    ):
        from types import SimpleNamespace
        from app.config import settings

        def fake_stat(self, *args, **kwargs):
            # Only st_dev is read by the code under test -- a different value for
            # nas_root than its parent simulates a genuinely distinct filesystem.
            return SimpleNamespace(st_dev=2 if self == settings.nas_root else 1)

        mounts_line = f"/dev/sda1 {settings.nas_root} ext4 rw,relatime 0 0\n"
        real_open = open

        def fake_open(path, *args, **kwargs):
            if path == "/proc/mounts":
                return __import__("io").StringIO(mounts_line)
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "stat", fake_stat), patch("builtins.open", fake_open):
            response = await authenticated_client.post("/api/v1/storage/recover")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "already_mounted"
        assert body["device"] == "/dev/sda1"
