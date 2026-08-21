"""
Pydantic models — mirror the Flutter models exactly so JSON serialization
matches what the app expects.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal, Optional, Annotated

from pydantic import BaseModel, Field, field_validator


# ─── AhcDevice ─────────────────────────────────────────────────────────────

class AhcDevice(BaseModel):
    serial: str
    name: str
    ip: str
    backend_version: str = Field(alias="backendVersion")
    board_model: str = Field(default="unknown", alias="boardModel")
    ocr_available: bool = Field(default=False, alias="ocrAvailable")
    os_codename: str = Field(default="unknown", alias="osCodename")
    os_eol_date: Optional[str] = Field(default=None, alias="osEolDate")
    os_eol_warning: bool = Field(default=False, alias="osEolWarning")
    #: The name this board answers to on the local network, e.g. "cubie.local". Worth sending even
    #: though a client can guess it from the hostname: it is what a person should bookmark instead
    #: of an IP, and only the board knows whether Avahi is actually running to back it up.
    mdns_name: Optional[str] = Field(default=None, alias="mdnsName")

    model_config = {"populate_by_name": True}


# ─── StorageStats ────────────────────────────────────────────────────────────

class StorageStats(BaseModel):
    total_gb: float = Field(alias="totalGB")
    used_gb: float = Field(alias="usedGB")

    model_config = {"populate_by_name": True}

    @property
    def free_gb(self) -> float:
        return self.total_gb - self.used_gb

    @property
    def used_percent(self) -> float:
        return min(max(self.used_gb / self.total_gb, 0.0), 1.0)


# ─── SystemStats ─────────────────────────────────────────────────────────────

class SystemStats(BaseModel):
    cpu_percent: float = Field(alias="cpuPercent")
    ram_percent: float = Field(alias="ramPercent")
    temp_celsius: float = Field(alias="tempCelsius")
    uptime_seconds: int = Field(alias="uptimeSeconds")
    network_up_mbps: float = Field(alias="networkUpMbps")
    network_down_mbps: float = Field(alias="networkDownMbps")
    storage: StorageStats

    model_config = {"populate_by_name": True}


# ─── FileItem ────────────────────────────────────────────────────────────────

#: A byte count. Annotated so the OpenAPI schema carries `format: int64` and generated clients
#: emit a 64-bit type. Without it Pydantic emits a bare `integer`, openapi-generator maps that to
#: a 32-bit Int, and any file over 2.1 GB deserialises wrong — on a NAS that stores films, that is
#: not a theoretical edge case. Python ints are unbounded, so nothing changes server-side.
Int64 = Annotated[int, Field(json_schema_extra={"format": "int64"})]
#: A plain `float` becomes OpenAPI `number` with no format, and the Kotlin generator faithfully
#: emits java.math.BigDecimal for that — correct, but wrong for a media position that ExoPlayer
#: hands us as a Double. The format hint gets kotlin.Double instead.
Float64 = Annotated[float, Field(json_schema_extra={"format": "double"})]


class FileItem(BaseModel):
    name: str
    path: str
    is_directory: bool = Field(alias="isDirectory")
    size_bytes: Int64 = Field(alias="sizeBytes")
    modified: datetime
    mime_type: Optional[str] = Field(None, alias="mimeType")
    #: The media-index entry id, when this file has been indexed.
    #:
    #: This is the STABLE identity for anything that has to outlive the file's location — playback
    #: resume above all. The path is how the file is fetched and played; it is not identity, because
    #: a rename or a move changes it and would orphan the resume point.
    #:
    #: **null means "not indexed yet"** (a file dropped in over SMB before the indexer has seen it).
    #: Such a file plays normally but takes no part in server-side resume, and the client must not
    #: invent a path-based fallback — that would recreate exactly the identity this replaces.
    entry_id: Optional[int] = Field(None, alias="entryId")

    model_config = {"populate_by_name": True}


class FileListResponse(BaseModel):
    items: list[FileItem]
    total_count: int = Field(alias="totalCount")
    page: int
    page_size: int = Field(alias="pageSize")

    model_config = {"populate_by_name": True}


class CategoryStatItem(BaseModel):
    path: str
    file_count: int = Field(alias="fileCount")
    total_bytes: Int64 = Field(alias="totalBytes")

    model_config = {"populate_by_name": True}


class CategoryStatsResponse(BaseModel):
    categories: list[CategoryStatItem]

    model_config = {"populate_by_name": True}


# ─── ActivityLogResponse ─────────────────────────────────────────────────────

class ActivityLogResponse(BaseModel):
    """Paginated view of app/audit.py's persisted events. `items` is left as raw
    dicts rather than a strict per-field model -- audit_log()'s callers pass
    genuinely different kwargs per event type (file_deleted has path/file_name/
    size_bytes, storage_formatted has device/label, etc.), so every event always
    has "event" and "timestamp" but the rest is intentionally open-ended."""
    items: list[dict]
    total_count: int = Field(alias="totalCount")
    page: int
    page_size: int = Field(alias="pageSize")


# ─── Funnel telemetry ────────────────────────────────────────────────────────
# See kb/telemetry_architecture_decision.md. event_name is intentionally a free string,
# not an enum -- the app is the source of truth for what stages exist, and pinning that
# list in the backend's own schema would mean every new stage needs a backend deploy too.

_FUNNEL_EVENT_NAMES = frozenset({
    "app_installed",
    "server_connected",
    "first_backup_completed",
    "family_member_added",
    "payment_started",
})


class FunnelEventRequest(BaseModel):
    """POST /api/v1/events body. No PII fields exist on this model at all, deliberately --
    there is nothing here to accidentally over-collect."""
    event_name: str = Field(alias="eventName")
    client_ts: int | None = Field(default=None, alias="clientTs")

    model_config = {"populate_by_name": True}

    @field_validator("event_name")
    @classmethod
    def _validate_known_event(cls, value: str) -> str:
        if value not in _FUNNEL_EVENT_NAMES:
            raise ValueError(
                f"unknown event_name {value!r} -- must be one of {sorted(_FUNNEL_EVENT_NAMES)}"
            )
        return value


class FunnelCountsResponse(BaseModel):
    """GET /api/v1/events/funnel — counts only, never raw events. This board's own funnel,
    nothing aggregated across boards (that's the opt-in path, not built yet — see the
    architecture decision doc's MVP scope)."""
    counts: dict[str, int]
    total_events: int = Field(alias="totalEvents")

    model_config = {"populate_by_name": True}

    model_config = {"populate_by_name": True}


# ─── FamilyUser ──────────────────────────────────────────────────────────────

class FamilyUser(BaseModel):
    id: str
    name: str
    is_admin: bool = Field(alias="isAdmin")
    folder_size_gb: float = Field(alias="folderSizeGB")
    avatar_color: str = Field(alias="avatarColor")  # hex string e.g. "FFE8A84C"
    icon_emoji: str = Field(default="", alias="iconEmoji")
    avatar: str = Field(default="")
    avatar_version: int = Field(default=0, alias="avatarVersion")

    model_config = {"populate_by_name": True}


# ─── ServiceInfo ─────────────────────────────────────────────────────────────

class ServiceInfo(BaseModel):
    id: str
    name: str
    description: str
    is_enabled: bool = Field(alias="isEnabled")

    model_config = {"populate_by_name": True}


# ─── StorageDevice ───────────────────────────────────────────────────────────

class StorageDevice(BaseModel):
    """A physical drive detected on the system (one entry per disk, not per partition)."""
    name: str                                              # "sda", "nvme0n1"
    path: str                                              # "/dev/sda"
    size_bytes: Int64 = Field(alias="sizeBytes")              # raw byte count
    size_display: str = Field(alias="sizeDisplay")          # "64.0 GB"
    fstype: Optional[str] = None                            # fstype of best partition
    label: Optional[str] = None                             # label of best partition
    model: Optional[str] = None                             # "SanDisk Ultra"
    transport: str                                          # "usb", "nvme"
    mounted: bool = False
    mount_point: Optional[str] = Field(None, alias="mountPoint")
    is_nas_active: bool = Field(False, alias="isNasActive")
    is_os_disk: bool = Field(False, alias="isOsDisk")       # True for SD card OS
    display_name: str = Field(alias="displayName")          # "Samsung T7 (500 GB)" — no /dev/ paths
    best_partition: Optional[str] = Field(None, alias="bestPartition")  # "/dev/sda1" or None

    model_config = {"populate_by_name": True}


# ─── Storage Requests ────────────────────────────────────────────────────────

class FormatRequest(BaseModel):
    """Format a block device. confirmDevice must match device for safety."""
    device: str                                         # "/dev/sda1"
    label: str = "AiHomeNAS"                           # ext4 label
    confirm_device: str = Field(alias="confirmDevice")  # must match device

    model_config = {"populate_by_name": True}

    @field_validator("device", "confirm_device")
    @classmethod
    def must_be_dev_path(cls, v: str) -> str:
        if not v.startswith("/dev/"):
            raise ValueError("device path must start with /dev/")
        return v


class MountRequest(BaseModel):
    """Mount a block device at the NAS root."""
    device: str                                         # "/dev/sda1"

    @field_validator("device")
    @classmethod
    def must_be_dev_path(cls, v: str) -> str:
        if not v.startswith("/dev/"):
            raise ValueError("device path must start with /dev/")
        return v


class EjectRequest(BaseModel):
    """Eject a specific device (unmount + power off)."""
    device: str                                         # "/dev/sda1"

    @field_validator("device")
    @classmethod
    def must_be_dev_path(cls, v: str) -> str:
        if not v.startswith("/dev/"):
            raise ValueError("device path must start with /dev/")
        return v


class SmartActivateRequest(BaseModel):
    """One-tap drive activation. Call with a whole disk path (e.g. /dev/sda).

    format is a required, explicit caller choice -- not inferred from the
    drive's current filesystem. True wipes the drive and creates a fresh
    ext4 filesystem; False mounts whatever filesystem is already there as-is
    (any type mount(8) can auto-detect, not just ext4) without touching
    existing data. Previously this was silently inferred from fstype=="ext4",
    which meant any non-ext4 drive with real data on it (NTFS, exFAT, ...)
    was wiped with no way to opt out -- found live 2026-07-30 while checking
    whether a friend's existing NVMe SSD could be added without losing data.
    """
    device: str                                         # "/dev/sda" (disk, not partition)
    format: bool                                        # True = erase + ext4, False = use as-is

    @field_validator("device")
    @classmethod
    def must_be_dev_path(cls, v: str) -> str:
        if not v.startswith("/dev/"):
            raise ValueError("device path must start with /dev/")
        return v


# ─── Request / Response helpers ──────────────────────────────────────────────

class PairRequest(BaseModel):
    serial: str
    key: str


class PairCompleteRequest(BaseModel):
    serial: str
    key: str
    otp: str

    model_config = {"populate_by_name": True}


class OtpRecord(BaseModel):
    otp_hash: str = Field(alias="otpHash")
    expires_at: int = Field(alias="expiresAt")

    model_config = {"populate_by_name": True}


class LoginRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    pin: str = Field(default="", max_length=16)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(alias="refreshToken")

    model_config = {"populate_by_name": True}


class TokenResponse(BaseModel):
    token: str


class CreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    pin: Optional[str] = None
    icon_emoji: str = ""


class RefreshTokenRecord(BaseModel):
    jti: str
    user_id: str = Field(alias="userId")
    issued_at: int = Field(alias="issuedAt")
    expires_at: int = Field(alias="expiresAt")
    revoked: bool = False

    model_config = {"populate_by_name": True}


class ChangePinRequest(BaseModel):
    old_pin: Optional[str] = Field(None, alias="oldPin")
    new_pin: str = Field(max_length=16, alias="newPin")

    model_config = {"populate_by_name": True}


class UpdateProfileRequest(BaseModel):
    """
    A member's display name is served **before anyone authenticates** — the profile picker lists it
    to whoever loads the page. It had no validation at all, so a member could set a name containing
    markup and have it rendered on the pre-auth screen of every device in the house. Escaped at the
    template now too, but a value that can never contain markup is the stronger half of that pair.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    icon_emoji: Optional[str] = Field(default=None, max_length=16)

    @field_validator("name")
    @classmethod
    def _no_markup(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name cannot be blank")
        if any(c in v for c in "<>\"'&\\/"):
            raise ValueError("name cannot contain < > \" ' & \\ or /")
        if any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError("name cannot contain control characters")
        return v


class UpdateNameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class FactoryResetRequest(BaseModel):
    """Wipe the device back to a clean state. mode="keep_media" removes every trace of
    AiHomeCloud (service, systemd units, polkit rules, sudoers, all app data) but leaves the
    user's actual NAS files intact; "wipe_media" additionally deletes all media content. PIN is
    re-verified server-side even though the caller is already authenticated -- this is the single
    most destructive action in the app."""
    mode: Literal["keep_media", "wipe_media"]
    pin: str


class CreateFolderRequest(BaseModel):
    path: str


class RenameRequest(BaseModel):
    old_path: str = Field(alias="oldPath")
    new_name: str = Field(alias="newName")

    model_config = {"populate_by_name": True}


class ToggleServiceRequest(BaseModel):
    enabled: bool


class AddFamilyUserRequest(BaseModel):
    # L-1 fix (security audit 2026-08): the missing half of CreateUserRequest's sibling
    # validation above -- an unbounded name becomes a filesystem directory name downstream
    # (store.add_user -> personal folder creation). Deliberately max_length only, not
    # min_length=1: the route already has its own `if not body.name.strip()` check returning a
    # specific 400, which a Pydantic-level min_length would short-circuit into FastAPI's generic
    # 422 instead -- an observable API contract change a real client could depend on, not
    # something to alter as a side effect of a length-cap fix.
    name: str = Field(max_length=64)


class SetUserRoleRequest(BaseModel):
    is_admin: bool = Field(alias="isAdmin")

    model_config = {"populate_by_name": True}


class FirmwareInfo(BaseModel):
    current_version: str
    latest_version: str
    update_available: bool
    changelog: str
    size_mb: float


# ─── Network ─────────────────────────────────────────────────────────────────

# ─── TrashItem ───────────────────────────────────────────────────────────────

class TrashItem(BaseModel):
    """Metadata for a soft-deleted file stored in the trash."""
    id: str
    original_path: str = Field(alias="originalPath")
    trash_path: str = Field(alias="trashPath")   # absolute path inside trash_dir
    filename: str
    deleted_at: datetime = Field(alias="deletedAt")
    size_bytes: Int64 = Field(alias="sizeBytes")
    deleted_by: str = Field(alias="deletedBy")   # user_id

    model_config = {"populate_by_name": True}


# ─── Network ─────────────────────────────────────────────────────────────────

class NetworkStatus(BaseModel):
    """Aggregated network state for the Cubie device."""
    wifi_enabled: bool = Field(alias="wifiEnabled")
    wifi_connected: bool = Field(alias="wifiConnected")
    wifi_ssid: Optional[str] = Field(None, alias="wifiSsid")
    wifi_ip: Optional[str] = Field(None, alias="wifiIp")
    hotspot_enabled: bool = Field(alias="hotspotEnabled")
    hotspot_ssid: Optional[str] = Field(None, alias="hotspotSsid")
    bluetooth_enabled: bool = Field(alias="bluetoothEnabled")
    lan_connected: bool = Field(alias="lanConnected")
    lan_ip: Optional[str] = Field(None, alias="lanIp")
    lan_speed: Optional[str] = Field(None, alias="lanSpeed")  # "1000Mb/s"
    gateway: Optional[str] = None
    dns: Optional[list[str]] = None

    model_config = {"populate_by_name": True}


class ToggleRequest(BaseModel):
    """Generic enable/disable toggle for wifi, hotspot, bluetooth."""
    enabled: bool


# ─── WiFi ─────────────────────────────────────────────────────────────────────

class WifiNetwork(BaseModel):
    """A single Wi-Fi network visible during scan."""
    ssid: str
    signal: int                                         # 0-100 (%)
    security: str                                       # "WPA2", "WPA3", "Open", etc.
    in_use: bool = Field(False, alias="inUse")          # currently connected
    saved: bool = False                                 # saved in NetworkManager

    model_config = {"populate_by_name": True}


class WifiConnectRequest(BaseModel):
    """Join a Wi-Fi network."""
    ssid: str
    password: str = ""                                  # empty for open networks


class WifiConnectionResult(BaseModel):
    """Result of a Wi-Fi connect attempt."""
    success: bool
    message: str
    ip: Optional[str] = None


class WifiForgetRequest(BaseModel):
    """Remove a saved Wi-Fi connection profile."""
    ssid: str


class WifiSetupRequest(BaseModel):
    """Initial onboarding Wi-Fi setup — sent from app via hotspot."""
    ssid: str
    password: str = ""


class WifiSetupResponse(BaseModel):
    """Acknowledgement that Wi-Fi setup has been accepted."""
    accepted: bool
    message: str


# ─── Hotspot ───────────────────────────────────────────────────────────────────

class HotspotStatus(BaseModel):
    """Whether this board's WiFi radio is currently running as an access point.
    adapter_present distinguishes "no WiFi radio on this board at all" (Rock Pi 4A / the x86
    thin client in this fleet — see wifi_manager's hotspot functions) from "has a radio, just
    not in hotspot mode right now"."""
    adapter_present: bool = Field(alias="adapterPresent")
    enabled: bool
    ssid: Optional[str] = None

    model_config = {"populate_by_name": True}


class HotspotConfigRequest(BaseModel):
    """Start the hotspot with a given SSID/password. Empty password = open network; a
    non-empty one must be WPA2-length-valid (>= 8 chars), enforced in wifi_manager."""
    ssid: str
    password: str = ""


class HotspotActionResult(BaseModel):
    success: bool
    message: str


# ─── Bluetooth ─────────────────────────────────────────────────────────────────

class BluetoothDevice(BaseModel):
    """A device bluez currently knows about — either paired previously, or discovered during
    the most recent scan."""
    address: str
    name: str
    paired: bool = False
    connected: bool = False

    model_config = {"populate_by_name": True}


class BluetoothStatus(BaseModel):
    """adapter_present distinguishes "no Bluetooth controller on this board" (Rock Pi 4A / the
    x86 thin client — see bluetooth_manager) from "has a controller, radio just off"."""
    adapter_present: bool = Field(alias="adapterPresent")
    enabled: bool
    devices: list[BluetoothDevice] = []

    model_config = {"populate_by_name": True}


class BluetoothConnectRequest(BaseModel):
    """Pair with or connect to a device by its Bluetooth address (e.g. "AA:BB:CC:DD:EE:FF")."""
    address: str


# ---------------------------------------------------------------------------
# Auth response models
#
# These describe what the endpoints ALREADY return, byte for byte. That includes
# the casing inconsistency between them: /auth/login answers in camelCase while
# /users/me and /auth/users/names answer in snake_case. That split is real, it is
# already baked into shipped clients, and normalising it here would silently break
# every installed app. Reproduce it faithfully; fix it later behind a version bump
# if it is ever worth fixing.
#
# There is no alias generator anywhere in this codebase, so Pydantic serialises by
# field name and these models change no bytes on the wire.
# ---------------------------------------------------------------------------


class LoginUser(BaseModel):
    id: str
    name: str
    # snake_case here, unlike its camelCase siblings, because the Android client has
    # always read it as "icon_emoji" (Profile.kt LoginUser) — it just never arrived.
    icon_emoji: str = ""
    isAdmin: bool  # noqa: N815 — camelCase is the existing wire format


class LoginResponse(BaseModel):
    accessToken: str  # noqa: N815
    refreshToken: str  # noqa: N815
    user: LoginUser


class RefreshResponse(BaseModel):
    accessToken: str  # noqa: N815


class ProfileSummary(BaseModel):
    """One entry of the pre-login profile picker. Never carries a PIN hash."""
    name: str
    has_pin: bool
    icon_emoji: str
    avatar: str
    avatar_version: int


class ProfileNamesResponse(BaseModel):
    users: list[ProfileSummary]


class UserMeResponse(BaseModel):
    id: str
    name: str
    icon_emoji: str
    has_pin: bool
    is_admin: bool
    avatar: str
    avatar_version: int


# ---------------------------------------------------------------------------
# Storage response models — again, describing what is already returned.
# ---------------------------------------------------------------------------


class JobStartedResponse(BaseModel):
    """A long-running operation was accepted; poll /jobs/{jobId} for progress."""
    jobId: str  # noqa: N815 — existing wire format


class MountResponse(BaseModel):
    status: str
    device: str
    mountPoint: str  # noqa: N815


class DeviceActionResponse(BaseModel):
    """Result of unmount/eject — the device the action was applied to."""
    status: str
    device: str


class SmartActivateResponse(BaseModel):
    """
    Activating a drive either finds it already usable or starts a format job.

    `jobId` is present only in the second case. The route therefore sets
    response_model_exclude_none, because emitting `"jobId": null` on the
    already-active path would be a new key that clients never saw before.
    """
    action: str
    display_name: str
    jobId: str | None = None  # noqa: N815


# ---------------------------------------------------------------------------
# System response models.
#
# Note the deliberate split in null handling: /system/arch already emits
# "target": null when no prebuilt exists, so that null is part of the contract and
# must be preserved. /system/update/status instead OMITS version and reason
# entirely when they do not apply, so that route excludes none. Getting these
# backwards would change the wire format in opposite directions.
# ---------------------------------------------------------------------------


class ArchResponse(BaseModel):
    """Board architecture and whether a prebuilt backend exists for it."""
    machine: str
    target: str | None
    prebuilt_available: bool


class StatusResponse(BaseModel):
    """Bare acknowledgement — shutdown, reboot, factory-reset."""
    status: str


class BackendUpdateStartedResponse(BaseModel):
    status: str
    version: str


class UpdateStatusResponse(BaseModel):
    """
    Progress of a backend self-update. `version` is absent while idle, and
    `reason` only accompanies a failure — both omitted rather than null, hence
    response_model_exclude_none on the route.
    """
    status: str
    version: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# File-operation response models.
#
# transport/sizeBytes/sizeDisplay are declared optional because the handler passes
# them through without an `or ""` fallback, unlike the neighbouring fields — so they
# can already be null on the wire today. They are NOT excluded when none: the null
# is existing behaviour and dropping the key would be the change.
# ---------------------------------------------------------------------------


class MkdirResponse(BaseModel):
    path: str


class ReindexCancelledResponse(BaseModel):
    cancelled: str


class StorageRootEntry(BaseModel):
    name: str
    path: str
    device: str
    transport: str | None
    sizeBytes: Int64 | None  # noqa: N815 — existing wire format
    sizeDisplay: str | None  # noqa: N815
    fstype: str
    label: str
    model: str


class StorageRootsResponse(BaseModel):
    roots: list[StorageRootEntry]


# ---------------------------------------------------------------------------
# Job, pairing, account and device-radio response models.
#
# Job.result and Job.progress are deliberately `Any`: a job's payload varies by
# job type and is not a fixed shape. Typing them as Any documents "arbitrary JSON"
# honestly instead of inventing a schema that would be wrong for most jobs. None of
# these routes exclude nulls — result/error/progress are emitted as null today.
# ---------------------------------------------------------------------------


class JobStatusResponse(BaseModel):
    id: str
    status: str
    startedAt: str  # noqa: N815 — ISO-8601, existing wire format
    result: Any = None
    error: str | None = None
    progress: Any = None


class PairingQrResponse(BaseModel):
    """Payload behind the pairing QR code. `otp` is zero-padded, so it stays a string."""
    qrValue: str  # noqa: N815
    otp: str
    serial: str
    ip: str
    host: str
    expiresAt: int  # noqa: N815 — unix seconds


class CertFingerprintResponse(BaseModel):
    """`fingerprint` is null when no certificate file exists yet — the algorithm is
    still reported, so the key stays present rather than being omitted."""
    fingerprint: str | None
    algorithm: str


class CreatedUserResponse(BaseModel):
    id: str
    name: str
    isAdmin: bool  # noqa: N815
    accessToken: str  # noqa: N815
    refreshToken: str  # noqa: N815


class AvatarResponse(BaseModel):
    avatar: str
    avatarVersion: int  # noqa: N815
    path: str


class SuccessResponse(BaseModel):
    """Bare acknowledgement used by the network and Bluetooth actions."""
    success: bool


class BluetoothPowerResponse(BaseModel):
    success: bool
    enabled: bool


# ---------------------------------------------------------------------------
# Upload, maintenance, health and preference response models.
#
# `blockers` entries are passed straight through from the process scan and their
# shape is not fixed, so they are typed as arbitrary objects rather than given an
# invented schema. That documents "opaque JSON" honestly and, importantly, is
# lossless — a wrong schema here would silently drop fields.
# ---------------------------------------------------------------------------


class UploadPrecheckRequest(BaseModel):
    """
    Hashes the client is about to upload, so the server can say which are unnecessary.

    Hash-only by design: the server needs nothing else to answer, and filenames would disclose
    more than the question requires.
    """
    hashes: list[str] = Field(default_factory=list, max_length=5000)
    syncScope: str | None = None  # noqa: N815 — "personal" (default) or "family"


class UploadPrecheckResponse(BaseModel):
    """`needed` is the subset worth sending; everything else this scope already holds."""
    needed: list[str]
    haveCount: int  # noqa: N815
    checked: int


class UploadResponse(BaseModel):
    """Result of a completed upload. `sortedTo` is null when no auto-sort applied."""
    name: str
    path: str
    sizeBytes: Int64  # noqa: N815
    sortedTo: str | None  # noqa: N815


class ReindexStartedResponse(BaseModel):
    jobId: str  # noqa: N815
    status: str


class CheckUsageResponse(BaseModel):
    """
    Processes holding a drive open. `serviceBlockers` is present only when the scan
    distinguishes NAS services from user processes, so the route excludes none rather
    than emitting a null the clients have never seen.
    """
    blockers: list[dict[str, Any]]
    serviceBlockers: list[dict[str, Any]] | None = None  # noqa: N815
    safe: bool
    message: str


class TelegramSetupStartedResponse(BaseModel):
    job_id: str


class TelegramSetupCancelledResponse(BaseModel):
    cancelled: str


class TrashPrefsResponse(BaseModel):
    autoDelete: bool  # noqa: N815


class HealthResponse(BaseModel):
    status: str
    deviceName: str  # noqa: N815
    serial: str


class RootResponse(BaseModel):
    service: str
    version: str
    deviceName: str  # noqa: N815
    serial: str


class WifiStatusResponse(BaseModel):
    """
    Radio state. `wifiEnabled` is null when nmcli could not be queried at all —
    genuinely unknown, distinct from off — so the null is preserved rather than
    coerced to false.
    """
    wifiEnabled: bool | None  # noqa: N815
    ethernetUp: bool  # noqa: N815
    userOverride: bool  # noqa: N815


class MessageResponse(BaseModel):
    message: str


class DocumentSearchHit(BaseModel):
    """
    One full-text search hit. `snippet` is FTS5's highlighted excerpt on the admin path
    and an empty string on the member path, so it is always present. `added_by`/`added_at`
    are UNINDEXED FTS5 columns with no NOT NULL constraint, so they are nullable rather
    than assumed populated for every row ever indexed.
    """
    path: str
    filename: str
    added_by: str | None = None
    added_at: str | None = None
    snippet: str


class DocumentSearchResponse(BaseModel):
    results: list[DocumentSearchHit]
    query: str
    count: int


class SemanticIndexProgress(BaseModel):
    """
    How far a running index has got. Mirrors `semantic_indexer.Progress.as_dict()` exactly.

    Typed rather than left as `Any` because the client silently discarded the whole object while
    it was untyped — the drift checker caught the field arriving and being thrown away, which is
    precisely the quiet failure that check exists for.

    `itemsPerSecond` and `etaSeconds` are null until 20 items are done: a rate extrapolated from
    two files is not an estimate, it is a wrong promise shown to a person.
    """
    phase: str
    model: str
    total: int
    processed: int
    remaining: int
    deleted: int
    failed: int
    itemsPerSecond: float | None = None  # noqa: N815
    etaSeconds: int | None = None  # noqa: N815


class SemanticIndexStatusResponse(BaseModel):
    """
    Whether this board can do semantic search, and how far along it is.

    `available` False means the runtime or model files are absent — a client should hide the
    feature rather than show an empty or erroring search. `progress` mirrors the running job's
    and is null when nothing is indexing.
    """
    available: bool
    model: str
    running: bool
    jobId: str | None = None  # noqa: N815 — matches the existing job wire format
    indexedCount: int  # noqa: N815
    progress: SemanticIndexProgress | None = None


class SemanticSearchHit(BaseModel):
    """
    One semantic-search hit. `score` is cosine similarity in [-1, 1] against a unit-normalised
    query, so it is comparable *within* one response and meaningless across models — clients
    should rank by it, not threshold on it.
    """
    path: str
    filename: str
    score: float


class SemanticSearchResponse(BaseModel):
    results: list[SemanticSearchHit]
    query: str
    count: int
    model: str


class HostCapabilitiesResponse(BaseModel):
    """
    What this host can actually do, so a client can hide what it cannot.

    Exists because the fleet genuinely disagrees: the ROCK Pi and x86 thin client have no
    WiFi or Bluetooth radios, yet the app offered those controls on every board. A client
    that reads this can drop the whole section rather than presenting a toggle that can only
    ever fail.
    """
    host: str
    machine: str
    capabilities: list[str]
    missing: list[str]

class AutoUpdateStatus(BaseModel):
    """
    Whether this board is actually applying its own security updates.

    Exists because "configured" and "working" turned out to be different things on all three boards
    at once, in three different ways, with nothing logged and nothing visibly wrong: a blacklist
    pattern that was a shell glob where a regex was required aborted every run; an origins pattern
    written against the wrong distribution matched no repository at all. A feature that installs
    updates silently is indistinguishable from one that installs nothing, so the status is part of
    the feature rather than a diagnostic bolted on afterwards.
    """

    enabled: bool = Field(description="unattended-upgrades is installed and switched on")
    lastRunAt: Optional[str] = Field(None, description="ISO-8601 of the last completed run")
    lastSuccessAt: Optional[str] = Field(None, description="ISO-8601 of the last run that finished without error")
    lastError: Optional[str] = Field(None, description="Final error line from the last failed run")
    packagesUpgraded: int = Field(0, description="Packages upgraded in the last run")
    rebootRequired: bool = Field(False, description="A pending update needs a reboot to take effect")
    kernelPolicy: str = Field("unknown", description="'distro' (kernel upgrades applied) or 'vendor' (held back)")

class MaintenanceWindow(BaseModel):
    """
    When the household is happy for the board to look after itself.

    A NAS is busiest exactly when people are awake, and updating packages, restarting the backend or
    rebooting mid-transfer is the one thing an appliance must never do. Rather than guess, the family
    names the hours nobody is using it. `Persistent=true` on the timer means a board that was off at
    the chosen hour catches up when it next boots, instead of silently skipping a month of updates.
    """

    start: str = Field("03:00", description="Local 24-hour start time, HH:MM")
    durationHours: int = Field(2, ge=1, le=12, description="How long the board may take, in hours")
    enabled: bool = Field(True, description="Whether unattended maintenance runs at all")
    timezone: Optional[str] = Field(None, description="The board's timezone — the clock the window is measured against")
    nextRunAt: Optional[str] = Field(None, description="When the timer will next fire, if known")

    @field_validator("start")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", v or ""):
            raise ValueError("start must be HH:MM in 24-hour time")
        return v
