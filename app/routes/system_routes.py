"""
System routes — device info, firmware, device name, power management.
"""

import asyncio
import hmac
import json
import os
import logging
import platform
import re
import socket
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from starlette.responses import Response

from ..audit import audit_log
from ..auth import get_current_user, require_admin, verify_password
from ..config import settings, get_local_ip
from .. import platform_profile
from ..models import (
    AutoUpdateStatus,
    MaintenanceWindow,
    AhcDevice,
    ArchResponse,
    HostCapabilitiesResponse,
    BackendUpdateStartedResponse,
    FactoryResetRequest,
    FirmwareInfo,
    StatusResponse,
    UpdateNameRequest,
    UpdateStatusResponse,
)
from .. import store
from ..subprocess_runner import run_command
from .service_routes import SERVICE_UNITS as _SERVICE_UNITS

logger = logging.getLogger("aihomecloud.system")

router = APIRouter(prefix="/api/v1/system", tags=["system"])

# Static codename -> EOL date map (verified via debian.org/tuxcare.com 2026-07-14,
# see docs/plan_sbc_hardening_and_nas_sharing_2026-07-14.md's Phase 2). "EOL" here
# means the end of the *free* security-support window (LTS for bullseye/bookworm,
# standard support for trixie's initial 3 years) -- not the hard end of any paid
# ELTS extension, which is well beyond any board's realistic support horizon.
_OS_EOL_DATES: dict[str, str] = {
    "bullseye": "2026-08-31",
    "bookworm": "2028-06-10",
    "trixie": "2028-08-09",
}
_EOL_WARNING_WINDOW_DAYS = 90


def _get_os_eol_info() -> tuple[str, Optional[str], bool]:
    """Return (codename, eol_date_iso, within_warning_window_or_past)."""
    try:
        info = platform.freedesktop_os_release()
        codename = info.get("VERSION_CODENAME", "unknown")
    except OSError:
        return "unknown", None, False

    eol_date_str = _OS_EOL_DATES.get(codename)
    if eol_date_str is None:
        return codename, None, False

    eol_date = datetime.strptime(eol_date_str, "%Y-%m-%d").date()
    days_remaining = (eol_date - date.today()).days
    return codename, eol_date_str, days_remaining <= _EOL_WARNING_WINDOW_DAYS


@router.get("/info", response_model=AhcDevice)
async def device_info(request: Request, user: dict = Depends(get_current_user)):
    """Return device identity & network info."""
    state = await store.get_device_state()
    board = getattr(request.app.state, "board", None)
    board_model = board.model_name if board else "unknown"

    from ..document_index import ocr_pdftotext_available, ocr_tesseract_available

    os_codename, os_eol_date, os_eol_warning = _get_os_eol_info()

    return AhcDevice(
        serial=settings.device_serial,
        name=state.get("name", settings.device_name),
        ip=get_local_ip(),
        backendVersion=settings.backend_version,
        boardModel=board_model,
        ocrAvailable=ocr_pdftotext_available and ocr_tesseract_available,
        osCodename=os_codename,
        osEolDate=os_eol_date,
        osEolWarning=os_eol_warning,
        # Only claim the name if avahi is actually answering for it — advertising a name that does
        # not resolve would send someone to bookmark a dead address.
        mdnsName=_mdns_name(),
    )


# Architectures that have a pre-built binary published as a GitHub Release asset.
_PREBUILT_ARCHES = {"x86_64", "aarch64", "arm64", "armv7l", "armv7"}
_ARCH_TARGET_MAP = {
    "x86_64": "linux-amd64",
    "aarch64": "linux-arm64",
    "arm64": "linux-arm64",
    "armv7l": "linux-armv7",
    "armv7": "linux-armv7",
}


@router.get("/capabilities", response_model=HostCapabilitiesResponse)
async def host_capabilities(user: dict = Depends(get_current_user)):
    """
    What this host supports. Read once by a client so it can hide controls that cannot work
    here rather than offering them and failing.

    Unauthenticated callers get nothing: the capability list describes the hardware fairly
    precisely, and there is no reason to hand that to anyone who has not paired.
    """
    return platform_profile.summary()


@router.get("/identity")
async def board_identity():
    """
    The board's signed SPKI rotation statement (H-11), served verbatim and unauthenticated.

    Unauthenticated on purpose: a client verifies this statement — against the identity key it
    already pinned on first pairing — *before* it can trust the TLS connection it would otherwise
    need to authenticate over. Nothing here is placed by this service; `ahc-issue-cert.sh` writes
    it as root on every certificate issuance, and this handler only reads what root published. See
    docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md.
    """
    path = settings.identity_statement_path
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No identity statement published on this board")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("board_identity_read_failed error=%s", exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Couldn't read the identity statement")


@router.get("/arch", response_model=ArchResponse)
async def device_arch(user: dict = Depends(get_current_user)):
    """Return CPU architecture and whether a pre-built telegram-bot-api binary
    is available for this device from the GitHub Releases page."""
    machine = platform.machine()
    target = _ARCH_TARGET_MAP.get(machine.lower())
    return {
        "machine": machine,
        "target": target,
        "prebuilt_available": target is not None,
    }


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a dotted version string ("1.4.2") into a comparable tuple. Non-numeric segments
    (e.g. "dev", the fallback for an un-bundled local checkout) sort as (), always the lowest
    possible value -- a dev build never claims to have a newer backend than a real release."""
    try:
        return tuple(int(p) for p in v.split("."))
    except (ValueError, AttributeError):
        return ()


@router.get("/firmware", response_model=FirmwareInfo)
async def check_firmware(
    available_version: Optional[str] = None,
    changelog: str = "",
    size_mb: float = 0.0,
    user: dict = Depends(get_current_user),
):
    """Check whether a newer backend version is available.

    There is no central update/release server for this app by design (see
    app/routes/backup_routes.py-adjacent architecture notes on no cross-board dependency) -- the
    Android app is the only thing that knows what backend version is bundled in ITS OWN current
    build (backend_bundle.tar's VERSION file, generated from the same Gradle versionName as
    settings.backend_version below), so it supplies that as a query param and this endpoint just
    does the comparison against what's actually running here. Mirrors the existing APK-update
    check's mental model (GET /app-update/manifest), inverted: there the backend tells the app
    about a new APK; here the app tells the backend about a new backend.
    """
    current = settings.backend_version
    update_available = bool(available_version) and _version_tuple(available_version) > _version_tuple(current)
    return FirmwareInfo(
        current_version=current,
        latest_version=available_version if update_available else current,
        update_available=update_available,
        changelog=changelog if update_available else "",
        size_mb=size_mb if update_available else 0.0,
    )


_UPDATE_VERSION_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_MAX_UPDATE_BUNDLE_BYTES = 200 * 1024 * 1024  # 200MB -- generous headroom over a real bundle's size


@router.post("/update", status_code=status.HTTP_202_ACCEPTED, response_model=BackendUpdateStartedResponse)
async def trigger_update(
    request: Request,
    version: str = Query(...),
    pin: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(require_admin),
):
    """Apply a new backend release, uploaded as backend_bundle.tar (uncompressed — the same
    asset the installer wizard already bundles into the APK, see android/app/build.gradle.kts's
    bundleBackendForInstaller task). Stages the upload, then hands off to
    scripts/ahc-apply-backend-update.sh via a oneshot systemd unit running as genuine root,
    OUTSIDE this process — this handler cannot itself flip the live symlink or restart its own
    service (NoNewPrivileges=yes blocks that from inside the sandbox, and it's also the wrong
    process to trust with its own replacement: if the caller has already returned 202, it can't
    keep running to finish the job). Returns immediately; the client polls GET
    /system/update/status for progress.

    PIN is re-verified here even though the caller already holds a valid admin JWT — same
    verify-then-act shape as /factory-reset, and for the same reason: this installs code that
    runs as the real system service indefinitely, well past the 1-hour JWT lifetime, so a
    briefly-leaked/stolen admin token shouldn't be able to plant a permanent backdoor the way it
    already can't wipe the device outright. Found live 2026-07-30, a pre-release security review
    flagged that this was the app's largest privilege differential yet gated no more strictly
    than /reboot, inconsistent with the precedent /factory-reset already set for smaller-impact
    destructive actions.
    """
    if not _UPDATE_VERSION_RE.match(version):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid version string")
    if _version_tuple(version) <= _version_tuple(settings.backend_version):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Version {version} is not newer than the running backend ({settings.backend_version})",
        )

    user_id = user.get("sub", "")
    found = await store.find_user(user_id)
    stored_pin = found.get("pin") if found else None
    if not stored_pin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "PIN does not match")
    if str(stored_pin).startswith("$2"):
        pin_ok = await verify_password(pin, stored_pin)
    else:
        pin_ok = hmac.compare_digest(str(stored_pin).encode(), pin.encode())
    if not pin_ok:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "PIN does not match")

    # A second concurrent update (two admins, or a client double-submit) would race the first
    # inside the apply script -- both writing into the same shared venv, both flipping the same
    # symlink, both restarting the same service. Checking _read_update_status() alone isn't
    # enough: the apply script doesn't write "applying" until after systemctl start actually
    # dispatches, well after this handler would already have returned 202 -- a second request
    # arriving in that window reads the same stale "idle"/"success" and passes the check too.
    # Claim it here instead, synchronously with the check (no `await` between them, so nothing
    # else in this single-worker event loop can interleave) -- found live 2026-07-30, a
    # pre-release security review flagged the original check-only guard as TOCTOU-incomplete.
    in_progress = _read_update_status()
    if in_progress["status"] == "applying":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"An update to version {in_progress['version']} is already in progress",
        )
    settings.update_status_file.parent.mkdir(parents=True, exist_ok=True)
    settings.update_status_file.write_text(f"applying:{version}")

    settings.update_staging_dir.mkdir(parents=True, exist_ok=True)
    staged_path = settings.update_staging_dir / f"{version}.tar"

    loop = asyncio.get_running_loop()
    size = 0
    too_large = False
    out = await loop.run_in_executor(None, open, staged_path, "wb")
    try:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > _MAX_UPDATE_BUNDLE_BYTES:
                too_large = True
                break
            await loop.run_in_executor(None, out.write, chunk)
    finally:
        await loop.run_in_executor(None, out.close)

    if too_large:
        staged_path.unlink(missing_ok=True)
        settings.update_status_file.write_text("idle")
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Bundle too large")

    # --no-block is required, not optional: a plain `systemctl start` blocks until the unit's
    # job finishes, but this unit's own job is to `systemctl restart aihomecloud` partway through
    # -- which tears down the cgroup this very process (and this awaited child subprocess) is
    # running in. Without --no-block, the apply script can legitimately succeed while this call
    # gets SIGTERM'd out from under itself, reporting rc=-15 and a false 500 to the client even
    # though the update applied cleanly. Found live 2026-08-01 on real hardware: the apply
    # script's own journal showed "Update to 1.3.1 applied and healthy" while this handler had
    # already failed the request 11s earlier. --no-block only waits for systemd to queue the job
    # (still fails synchronously on a bad unit name or permission error) -- actual progress is
    # tracked via update_status_file / GET /system/update/status, exactly as designed.
    rc, _, stderr = await run_command(
        ["systemctl", "start", "--no-block", f"ahc-apply-update@{version}.service"],
    )
    if rc != 0:
        staged_path.unlink(missing_ok=True)
        settings.update_status_file.write_text("idle")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not start update: {stderr}")

    audit_log("backend_update_triggered", actor_id=user.get("sub", ""), version=version)
    return {"status": "applying", "version": version}


def _read_update_status() -> dict:
    """Read settings.update_status_file directly off disk (not through the store/DB) --
    the apply script that writes it runs as root, outside this process, potentially while this
    process itself is mid-restart. Format: "applying:<version>" | "success:<version>" |
    "failed:<version>:<reason>" | missing file (nothing has ever run)."""
    status_file = settings.update_status_file
    if not status_file.exists():
        return {"status": "idle"}
    raw = status_file.read_text().strip()
    parts = raw.split(":", 2)
    if parts[0] not in ("applying", "success", "failed") or len(parts) < 2:
        return {"status": "idle"}
    result = {"status": parts[0], "version": parts[1]}
    if parts[0] == "failed" and len(parts) > 2:
        result["reason"] = parts[2]
    return result


@router.get("/update/status", response_model=UpdateStatusResponse, response_model_exclude_none=True)
async def get_update_status(user: dict = Depends(get_current_user)):
    """Poll the status of an in-progress or last-completed backend update."""
    return _read_update_status()


# ---------------------------------------------------------------------------
# Android app update (distinct from the OS/firmware OTA stub above) -- each
# board serves its own copy from app_update_dir, independently, over the same
# authenticated HTTPS connection every other endpoint uses. Replaces a prior
# session's mechanism that hardcoded one specific dev board's LAN IPs over
# plain HTTP, which broke over Tailscale and coupled every board's update
# check to a board that isn't even the one the user is talking to. Found +
# fixed 2026-07-23 (Paras: "boards should work independently").
# Publish a new build by copying app-update.apk + manifest.json into
# app_update_dir on each board (see backend/CLAUDE.md's deploy section).
# ---------------------------------------------------------------------------

@router.get("/app-update/manifest")
async def app_update_manifest(user: dict = Depends(get_current_user)):
    """Latest available Android app version for this board, or 404 if none has been
    published here yet."""
    manifest_path = settings.app_update_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No app update published on this board")
    try:
        return json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("app_update_manifest_read_failed error=%s", exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Couldn't read the update manifest")


@router.get(
    "/app-update/apk",
    responses={200: {"content": {"application/vnd.android.package-archive": {}}}},    response_class=Response,
)
async def app_update_apk(request: Request, user: dict = Depends(get_current_user)):
    """Streams the published APK (Range-request capable, same as file downloads)."""
    apk_path = settings.app_update_dir / "app-update.apk"
    if not apk_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No app update published on this board")
    from .file_routes import stream_file_download
    return stream_file_download(request, apk_path)


@router.put("/name", status_code=status.HTTP_204_NO_CONTENT)
async def update_name(body: UpdateNameRequest, user: dict = Depends(require_admin)):
    """Rename the device."""
    if not body.name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name cannot be empty")
    await store.update_device_name(body.name)

    # Make the board answer to <name>.local as well, so nobody has to type an IP. Best-effort: the
    # rename itself has already succeeded and must not be reported as failed because a hostname
    # could not be set — the user asked to rename their NAS, not to reconfigure mDNS.
    def _apply_hostname() -> None:
        # Writing the file IS the trigger: ahc-apply-device-name.path watches it and starts the
        # root helper. The previous `sudo -n` could never run — this unit sets NoNewPrivileges=yes
        # and sudo is setuid, so every call failed and the rename silently did nothing.
        _write_request_atomically(_DEVICE_NAME_REQUEST, {"name": body.name})

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _apply_hostname)
    except Exception as exc:
        logger.warning("device_hostname_apply_failed error=%s", exc)


@router.post("/shutdown", status_code=status.HTTP_202_ACCEPTED, response_model=StatusResponse)
async def shutdown_device(user: dict = Depends(require_admin)):
    """Stop all active NAS services and power off the device.

    The poweroff command is deferred by 2 seconds so the HTTP 202 response
    reaches the client before the OS tears down networking.

    Note: On the Radxa Cubie A7A (Allwinner sun60iw2) the PMIC cannot fully
    cut power via software, so the board will reboot after poweroff.  Use the
    /reboot endpoint for a clean restart instead.
    """
    # 1. Stop all enabled services
    services = await store.get_services()
    for svc in services:
        if svc.get("isEnabled"):
            units = _SERVICE_UNITS.get(svc["id"], [])
            for unit in units:
                ok, err = await _systemctl_stop(unit)
                if not ok:
                    logger.warning("Failed to stop %s: %s", unit, err)

    # 2. Schedule poweroff after a short delay so the response is delivered.
    # `systemctl poweroff` (not sudo /usr/sbin/shutdown) so the request goes
    # through systemd-logind over D-Bus, authorized by the scoped
    # org.freedesktop.login1.power-off polkit rule for this service account
    # rather than needing sudo (which NoNewPrivileges=yes on this unit
    # blocks outright, regardless of sudoers).
    logger.info("Shutdown requested by user %s", user.get("sub", "unknown"))
    asyncio.create_task(_deferred_power_command(["systemctl", "poweroff"]))
    return {"status": "shutting_down"}


@router.post("/reboot", status_code=status.HTTP_202_ACCEPTED, response_model=StatusResponse)
async def reboot_device(user: dict = Depends(require_admin)):
    """Reboot the device.  Response is sent before the OS restarts."""
    logger.info("Reboot requested by user %s", user.get("sub", "unknown"))
    asyncio.create_task(_deferred_power_command(["systemctl", "reboot"]))
    return {"status": "rebooting"}


async def _deferred_power_command(cmd: list[str]) -> None:
    """Wait 2 seconds then execute a power command (poweroff / reboot)."""
    await asyncio.sleep(2)
    rc, _, stderr = await run_command(cmd, timeout=15)
    if rc != 0:
        logger.error("Power command %s failed: %s", cmd, stderr)


@router.post("/factory-reset", status_code=status.HTTP_202_ACCEPTED, response_model=StatusResponse)
async def factory_reset(body: FactoryResetRequest, user: dict = Depends(require_admin)):
    """Wipe the device back to a clean state -- the single most destructive action in the app.

    The PIN is re-verified here even though the caller already holds a valid admin JWT (same
    verify-then-act shape as PUT /users/pin) -- defense in depth for an action with no undo.
    Response is sent before the actual reset runs, same as /reboot and /shutdown, since the
    running service is about to remove itself.
    """
    user_id = user.get("sub", "")
    found = await store.find_user(user_id)
    stored_pin = found.get("pin") if found else None
    if not stored_pin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "PIN does not match")
    if str(stored_pin).startswith("$2"):
        pin_ok = await verify_password(body.pin, stored_pin)
    else:
        pin_ok = hmac.compare_digest(str(stored_pin).encode(), body.pin.encode())
    if not pin_ok:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "PIN does not match")

    logger.warning("FACTORY RESET requested by %s, mode=%s", user_id, body.mode)
    instance = "wipe-media" if body.mode == "wipe_media" else "keep-media"
    asyncio.create_task(_deferred_factory_reset(instance))
    return {"status": "resetting"}


async def _deferred_factory_reset(instance: str) -> None:
    """Wait 2 seconds then hand off to the root-context ahc-factory-reset@<instance> unit --
    the running service can't remove its own systemd unit/polkit/sudoers/data or delete the app
    user from inside itself, same NoNewPrivileges=yes reasoning as the WiFi connect flow."""
    await asyncio.sleep(2)
    rc, _, stderr = await run_command(
        ["systemctl", "start", f"ahc-factory-reset@{instance}.service"], timeout=15
    )
    if rc != 0:
        logger.error("Failed to start factory-reset unit (%s): %s", instance, stderr)


async def _systemctl_stop(unit: str) -> tuple[bool, str]:
    """Run `systemctl stop <unit>` via centralized runner.

    No sudo: authorized via the scoped polkit rule for this specific,
    allowlisted set of NAS-adjacent units (see 50-aihomecloud-storage.rules /
    52-aihomecloud-power-and-services.rules).
    """
    rc, _, stderr = await run_command(["systemctl", "stop", unit], timeout=15)
    return rc == 0, stderr

_UPGRADED_MARKER = "Packages that will be upgraded:"
#: Written by the apt-daily-upgrade drop-in, inside the service's own data dir. Preferred because
#: the real log lives in a root:adm 0750 directory the service deliberately has no access to.
#: Root-owned directory, group-readable by the service. Deliberately NOT under
#: /var/lib/aihomecloud: a root writer must never follow a path the service user can
#: replace with a symlink.
_UU_LOG_MIRROR = Path("/var/log/ahc-status/auto-update.log")
_UU_LOG = Path("/var/log/unattended-upgrades/unattended-upgrades.log")
_UU_ENABLE = Path("/etc/apt/apt.conf.d/20auto-upgrades")
_AHC_UU_CONF = Path("/etc/apt/apt.conf.d/51ahc-auto-upgrades")


def _write_request_atomically(path: Path, payload: dict) -> None:
    """
    Write a request file that a systemd .path unit is watching.

    Must be atomic. `write_text` truncates and then writes, so the watcher fires on the empty
    intermediate file and the root helper reads nothing — observed live: "invalid start time: ''"
    followed by a second, successful run. Writing to a temporary file in the same directory and
    renaming means the watched path only ever exists complete.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)

def _mdns_name() -> Optional[str]:
    """"<hostname>.local", or None when avahi is not running to back it up."""
    try:
        host = socket.gethostname().split(".")[0]
        if not host:
            return None
        active = subprocess.run(
            ["systemctl", "is-active", "avahi-daemon"], capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return f"{host}.local" if active == "active" else None
    except Exception:
        return None


def _read_auto_update_status() -> dict:
    """
    Read the state of unattended-upgrades off disk. Never raises.

    Deliberately reads the log rather than trusting configuration. Every board in this fleet was
    configured and enabled while applying nothing — one because a blacklist glob was parsed as a
    regex and threw, another because its origins pattern named the wrong distribution. Only the log
    distinguishes "ran and did nothing because there was nothing to do" from "crashed before it
    looked".
    """
    status = {
        "enabled": False, "lastRunAt": None, "lastSuccessAt": None, "lastError": None,
        "packagesUpgraded": 0, "rebootRequired": Path("/var/run/reboot-required").exists(),
        "kernelPolicy": "unknown",
    }
    try:
        if _UU_ENABLE.exists():
            status["enabled"] = 'Unattended-Upgrade "1"' in _UU_ENABLE.read_text()
        if _AHC_UU_CONF.exists():
            # The generator writes "... -> policy: vendor" in its header. Parse the value rather
            # than matching a literal — the first version of this checked for "-> vendor" and
            # silently reported "unknown" against a correctly-configured board.
            m = re.search(r"->\s*policy:\s*(\w+)", _AHC_UU_CONF.read_text()[:400])
            if m and m.group(1) in ("vendor", "distro"):
                status["kernelPolicy"] = m.group(1)
    except OSError:
        pass

    lines = None
    for candidate in (_UU_LOG_MIRROR, _UU_LOG):
        try:
            lines = candidate.read_text(errors="replace").splitlines()[-400:]
            break
        except OSError:
            continue
    if lines is None:
        return status

    for line in lines:
        stamp = line[:19] if len(line) > 19 and line[4] == "-" else None
        if stamp:
            status["lastRunAt"] = stamp
        if "All upgrades installed" in line or "No packages found that can be upgraded" in line:
            status["lastSuccessAt"] = stamp or status["lastRunAt"]
            status["lastError"] = None
        elif _UPGRADED_MARKER in line:
            # Split on the marker, not the first colon — the first colon is inside the timestamp
            # ("03:00:03"), which counted the rest of the line as package names.
            status["packagesUpgraded"] = len(line.split(_UPGRADED_MARKER, 1)[1].split())
        elif "error" in line.lower() or "Traceback" in line:
            status["lastError"] = line.strip()[:300]
    return status


@router.get("/auto-updates", response_model=AutoUpdateStatus)
async def auto_update_status(user: dict = Depends(get_current_user)):
    """Is this board actually keeping itself patched? Read from the log, not the config."""
    loop = asyncio.get_running_loop()
    return AutoUpdateStatus(**await loop.run_in_executor(None, _read_auto_update_status))

_DEVICE_NAME_REQUEST = Path("/var/lib/aihomecloud/device-name.json")
_DEVICE_NAME_HELPER = "/usr/local/sbin/ahc-apply-device-name"
_WINDOW_REQUEST = Path("/var/lib/aihomecloud/maintenance-window.json")
_WINDOW_HELPER = "/usr/local/sbin/ahc-apply-maintenance-window"


def _read_window() -> dict:
    """The stored choice, or the default. Never raises — settings screens must always render."""
    data = {"start": "03:00", "durationHours": 2, "enabled": True, "timezone": None}
    try:
        data.update(json.loads(_WINDOW_REQUEST.read_text()))
    except (OSError, ValueError):
        pass
    try:
        data["timezone"] = subprocess.run(
            ["timedatectl", "show", "-p", "Timezone", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or None
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["systemctl", "show", "apt-daily-upgrade.timer", "-p", "NextElapseUSecRealtime", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        data["nextRunAt"] = out or None
    except Exception:
        data["nextRunAt"] = None
    return data


@router.get("/maintenance-window", response_model=MaintenanceWindow)
async def get_maintenance_window(user: dict = Depends(get_current_user)):
    """When this board is allowed to update and restart itself."""
    loop = asyncio.get_running_loop()
    return MaintenanceWindow(**await loop.run_in_executor(None, _read_window))


@router.put("/maintenance-window", response_model=MaintenanceWindow)
async def set_maintenance_window(
    body: MaintenanceWindow,
    user: dict = Depends(require_admin),
):
    """
    Choose the window, admin only.

    The value is written to the service's own data directory and applied by a helper invoked through
    sudo. The helper takes no arguments on purpose — the sudoers policy here allows exact commands
    with no wildcards, and passing a time on the command line would need a wildcard rule. The helper
    re-validates the file rather than trusting that this endpoint already did.
    """
    payload = {"start": body.start, "durationHours": body.durationHours,
               "enabled": body.enabled, "timezone": body.timezone}

    def _apply() -> Optional[str]:
        # ahc-apply-maintenance-window.path watches this file and runs the root helper, so writing
        # it is the whole operation. It used to shell out to `sudo -n`, which cannot work under
        # NoNewPrivileges=yes — the endpoint therefore returned 503 every single time and the window
        # was never applied through the app at all.
        _write_request_atomically(_WINDOW_REQUEST, payload)
        return None

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _apply)
    except Exception as exc:
        logger.error("maintenance_window_write_failed error=%s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Could not save the schedule on this board: {exc}",
        )
    # The helper runs a moment later; give it a beat so the response reflects reality rather than
    # the value we just asked for.
    await asyncio.sleep(1.5)
    return MaintenanceWindow(**await loop.run_in_executor(None, _read_window))
