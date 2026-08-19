"""
Service management routes — list and toggle NAS services.
Uses real systemctl calls to start/stop systemd units on the Cubie.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import get_current_user, require_admin
from ..models import ServiceInfo, ToggleServiceRequest
from .. import store
from .event_routes import emit_service_toggled
from ..subprocess_runner import run_command

logger = logging.getLogger("aihomecloud.services")

router = APIRouter(prefix="/api/v1/services", tags=["services"])

# Map our service IDs → systemd unit names (shared with system_routes for shutdown).
# NFS uses "nfs-server" (the canonical unit), not the "nfs-kernel-server" alias the
# Debian package is named after — found live 2026-07-14: `systemctl enable
# nfs-kernel-server` correctly resolves the alias, but `systemctl disable
# nfs-kernel-server` does not propagate to the real unit (is-enabled on
# nfs-server.service stayed "enabled" after a "disabled" alias call reported
# success) — using the canonical name avoids that asymmetry entirely.
SERVICE_UNITS: dict[str, list[str]] = {
    "samba": ["smbd", "nmbd"],  # legacy id, kept for backward compat with old service files
    "smb": ["smbd", "nmbd"],
    "nfs": ["nfs-server"],
    "ssh": ["ssh"],
    "dlna": ["minidlna", "minidlnad"],
    "media": ["minidlna", "minidlnad", "smbd", "nmbd"],  # legacy id, see store.py migration
}

ALLOWED_SERVICES: frozenset[str] = frozenset(SERVICE_UNITS.keys())

# Units authorized for enable/disable (so toggle state survives a reboot),
# kept in sync with scripts/ahc-persist-unit.sh's own allowlist. Units not
# listed here (e.g. ssh) only get start/stop.
#
# These are all SysV-init-compat units on Debian 11 (Samba/NFS still ship
# /etc/init.d scripts), not native systemd units. `systemctl enable/disable`
# on a SysV-compat unit shells out to `update-rc.d` via systemd-sysv-install,
# and that subprocess does its own root check independent of whatever the
# manage-unit-files polkit grant authorized for the D-Bus call itself — found
# live 2026-07-14: the polkit grant let the call through, but update-rc.d
# still returned "Permission denied" for the actual aihomecloud (non-root)
# caller. So enable/disable for these units routes through the
# ahc-enable-unit@/ahc-disable-unit@ host-namespace oneshot units (which run
# as genuine root) instead of a direct systemctl call — same escape-hatch
# shape as the WiFi/mount features, different underlying reason (a legacy
# subprocess's own root check, not a mount-namespace boundary).
PERSISTABLE_UNITS: frozenset[str] = frozenset({"smbd", "nmbd", "nfs-server"})


async def _systemctl(action: str, unit: str) -> tuple[bool, str]:
    """Run `systemctl <action> <unit>` via centralized runner.

    No sudo: `unit` is always one of ALLOWED_SERVICES' fixed underlying
    systemd names, authorized via a scoped polkit rule rather than sudo
    (which NoNewPrivileges=yes on this service blocks outright).
    """
    rc, _, stderr = await run_command(["systemctl", action, unit], timeout=15)
    return rc == 0, stderr


async def _persist_unit_state(enable: bool, unit: str) -> tuple[bool, str]:
    """Enable/disable a PERSISTABLE_UNITS unit via the root-context oneshot
    escape hatch (see PERSISTABLE_UNITS' docstring for why a direct
    `systemctl enable/disable` doesn't work for these SysV-compat units)."""
    kind = "enable" if enable else "disable"
    return await _systemctl("start", f"ahc-{kind}-unit@{unit}.service")


@router.get("", response_model=list[ServiceInfo])
async def list_services(user: dict = Depends(get_current_user)):
    """List all configurable NAS services."""
    return [ServiceInfo(**svc) for svc in await store.get_services()]


@router.post("/{service_id}/toggle", status_code=status.HTTP_204_NO_CONTENT)
async def toggle(
    service_id: str,
    body: ToggleServiceRequest,
    user: dict = Depends(require_admin),
):
    """Enable or disable a NAS service (persists + runs systemctl)."""
    if service_id not in ALLOWED_SERVICES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown service: {service_id}")

    if not await store.toggle_service(service_id, body.enabled):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")

    # Run real systemctl start/stop, plus enable/disable for units where
    # that's supported, so the toggle state survives a reboot.
    units = SERVICE_UNITS.get(service_id, [])
    start_stop = "start" if body.enabled else "stop"

    errors: list[str] = []
    for unit in units:
        ok, err = await _systemctl(start_stop, unit)
        if not ok:
            logger.warning("systemctl %s %s failed: %s", start_stop, unit, err)
            errors.append(f"{unit} {start_stop}: {err}")

        if unit in PERSISTABLE_UNITS:
            ok, err = await _persist_unit_state(body.enabled, unit)
            if not ok:
                logger.warning(
                    "persist-unit-state %s %s failed: %s",
                    "enable" if body.enabled else "disable", unit, err,
                )
                errors.append(f"{unit} persist: {err}")

    if errors:
        # Service state was persisted, but systemctl had issues
        logger.error(
            "Service %s toggled to %s but systemctl errors: %s",
            service_id, body.enabled, "; ".join(errors),
        )
        # Don't fail the request — the store state is updated.
        # The service might not be installed yet.

    # Notify connected clients
    await emit_service_toggled(service_id, body.enabled)
