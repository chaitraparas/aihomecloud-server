"""
JSON-file-based persistence for users, services config, and device state.
Designed for simplicity on a single-device NAS — no database needed.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
import asyncio
from typing import Any, Dict, List, Optional
import os
import tempfile
import time

from .config import settings

logger = logging.getLogger("aihomecloud.store")

# Async lock to protect concurrent access to JSON files from async handlers
_store_lock = asyncio.Lock()

# Separate lock serialising the first-user check + create sequence to prevent
# a race where two simultaneous requests both observe 0 users and both try to
# create the admin account.  Must be distinct from _store_lock (which is also
# acquired inside add_user → save_users) to avoid re-entrant deadlocks.
_user_creation_lock = asyncio.Lock()

_CACHE_TTL = 5.0  # seconds — longer TTL reduces JSON re-reads during browsing
_cache: dict[str, tuple[Any, float]] = {}

# Sentinel for distinguishing "no default passed" from "default=None"
_UNSET = object()


def _get_cached(key: str) -> Any:
    item = _cache.get(key)
    if item is None:
        return None

    value, expires_at = item
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def _set_cached(key: str, value: Any) -> None:
    if value is None:
        _cache.pop(key, None)
        return
    _cache[key] = (value, time.monotonic() + _CACHE_TTL)


def _read_json(path: Path, default: Any = _UNSET) -> Any:
    if not path.exists():
        return {} if default is _UNSET else default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        logger.error("corrupt_json path=%s — attempting recovery", path)
        fallback = {} if default is _UNSET else default

        # Rename corrupt file for forensic inspection
        corrupt_name = path.with_suffix(".json.corrupt")
        try:
            path.rename(corrupt_name)
        except Exception:
            pass

        # Attempt recovery from the .corrupt backup (may be the previous good copy)
        recovered = False
        try:
            if corrupt_name.exists():
                data = json.loads(corrupt_name.read_text())
                logger.info("corrupt_json_recovered path=%s from backup", path)
                # Restore the recovered data back to the original path
                _atomic_write(path, data)
                recovered = True
                fallback = data
        except (json.JSONDecodeError, ValueError, OSError):
            logger.error("corrupt_json_recovery_failed path=%s — data lost", path)

        # Emit data_corruption event for UI notification
        try:
            from .events import file_event_bus, FileEvent
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(file_event_bus.publish(FileEvent(
                    path=str(path),
                    action="data_corruption" if not recovered else "data_corruption_recovered",
                    user="system",
                )))
        except Exception:
            pass  # event bus may not be ready during early startup

        return fallback


def _atomic_write(path: Path, data: Any) -> None:
    """Write JSON to `path` atomically by writing to a temp file then moving it.

    This prevents partially-written JSON files on crash/power-loss.
    If fsync fails (e.g. disk full), the temp file is cleaned up and the
    original file is left untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Use the same directory to ensure os.replace is atomic on the same filesystem.
    fd, tmp_path = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    fsync_ok = False
    try:
        # Write bytes to fd, flush and fsync to ensure durability.
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            os.fsync(f.fileno())
        fsync_ok = True

        # Atomically replace target — only reached if fsync succeeded.
        os.replace(tmp_path, str(path))
    except Exception as exc:
        # Clean up temp file — original file is untouched.
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        if not fsync_ok:
            logger.error(
                "atomic_write_fsync_failed path=%s error=%s — temp file cleaned up, original file preserved",
                path, exc,
            )
        raise


def _write_json(path: Path, data: Any) -> None:
    _atomic_write(path, data)


# ─── Users ────────────────────────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# First-run setup marker
#
# Unauthenticated admin creation (POST /users with an empty store) is how a board bootstraps its
# first owner. The gate used to be "are there zero users right now?", and that question has a wrong
# answer in one important case: `_read_json` returns [] when users.json is missing or unrecoverably
# corrupt. So a damaged store on an established board — one with a family's photos already on it —
# silently reopened unauthenticated admin creation to anyone on the LAN. Not a concurrency race;
# the in-process `_user_creation_lock` already handles two simultaneous callers correctly. The bug
# is that "empty" and "never set up" are not the same state, and only one of them should open that
# door. (2026-08-08 audit, M-11.)
#
# This marker records "setup has completed on this board" durably and separately from the user
# list, so losing one does not silently change an authorization decision. Clearing it is
# deliberately a factory reset — which wipes data_dir and needs root — because "this board has no
# owner yet" should require physical access to assert.
# ---------------------------------------------------------------------------


def setup_marker_path() -> Path:
    return settings.data_dir / "setup_completed"


def setup_completed() -> bool:
    """Whether this board has ever completed first-run setup."""
    try:
        return setup_marker_path().exists()
    except OSError:
        # Fail CLOSED: if the marker cannot be read, assume setup HAS happened. The safe error is
        # refusing a legitimate first-run (recoverable, visible, needs a human) rather than opening
        # unauthenticated admin creation on a board that may already belong to someone.
        logger.error("setup_marker_unreadable — treating board as already set up")
        return True


async def mark_setup_completed() -> None:
    """Record that setup has completed. Idempotent."""
    path = setup_marker_path()
    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(datetime.now(timezone.utc).isoformat())
        tmp.replace(path)
        logger.info("setup_marker_written path=%s", path)
    except OSError as exc:
        # Loud, because a missing marker is what reopens the hole this closes.
        logger.error("setup_marker_write_failed path=%s err=%s", path, exc)


async def backfill_setup_marker() -> None:
    """
    Write the marker on a board that was set up before this marker existed.

    Called at startup. Without it, every already-installed board keeps the old behaviour until
    someone happens to POST /users — and the whole point is to be protected *before* the store gets
    damaged, not after.
    """
    if setup_completed():
        return
    if await get_users():
        logger.info("setup_marker_backfill — existing users found, marking setup complete")
        await mark_setup_completed()


async def get_users() -> List[dict]:
    """Return the list of users, protected by the store lock."""
    cached = _get_cached("users")
    if cached is not None:
        return cached

    async with _store_lock:
        users = _read_json(settings.users_file, [])
        _set_cached("users", users)
        return users


async def save_users(users: List[dict]) -> None:
    """Persist users to disk using an async lock to prevent concurrent writes."""
    async with _store_lock:
        _write_json(settings.users_file, users)
        _set_cached("users", users)  # update inside lock after write


async def find_user(user_id: str) -> Optional[dict]:
    users = await get_users()
    return next((u for u in users if u["id"] == user_id), None)


def _create_personal_dirs(personal: Path) -> None:
    """Synchronously create the user's personal directory hierarchy.
    Runs in a thread executor so it never blocks the event loop."""
    personal.mkdir(parents=True, exist_ok=True)
    for sub in ("Photos", "Videos", "Documents", "Others", ".inbox"):
        (personal / sub).mkdir(exist_ok=True)


async def add_user(
    name: str,
    pin: Optional[str] = None,
    is_admin: bool = False,
    icon_emoji: str = "",
) -> dict:
    users = await get_users()
    user = {
        "id": f"user_{uuid.uuid4().hex[:8]}",
        "name": name,
        "pin": pin,
        "is_admin": is_admin,
        "icon_emoji": icon_emoji,
        "avatar": "",
        "avatar_version": 0,
    }
    users.append(user)
    await save_users(users)

    # Best-effort: create personal folder hierarchy in a thread executor so we
    # never block the event loop on USB I/O and never fail user creation due to
    # storage errors (folders are created on demand when user first uploads).
    safe_name = Path(name).name  # strips any directory components like ../
    personal = settings.personal_path / safe_name
    try:
        loop = asyncio.get_running_loop()  # always returns the currently-running loop
        await loop.run_in_executor(None, _create_personal_dirs, personal)
    except Exception as _e:
        logger.warning("Could not pre-create personal dirs for %s: %s", name, _e)

    return user


async def remove_user(user_id: str) -> bool:
    users = await get_users()
    filtered = [u for u in users if u["id"] != user_id]
    if len(filtered) == len(users):
        return False
    await save_users(filtered)
    return True


async def update_user_pin(user_id: str, new_pin: str, *, expected_pin: str | None = None) -> bool:
    """
    Set a user's PIN hash.

    `expected_pin` makes this a compare-and-swap: the write only lands if the stored hash is
    still the one the caller last saw. Needed by the background bcrypt-rehash task, which
    computes a new hash from the PIN captured at login and could otherwise overwrite a PIN the
    user changed in the meantime — silently reverting them to their old PIN.
    (2026-07-30 auth finding 7.)

    Omit it for ordinary "just set it" writes.
    """
    users = await get_users()
    for u in users:
        if u["id"] == user_id:
            if expected_pin is not None and u.get("pin") != expected_pin:
                return False
            u["pin"] = new_pin
            await save_users(users)
            return True
    return False


async def update_user_profile(
    user_id: str,
    *,
    name: str | None = None,
    icon_emoji: str | None = None,
) -> bool:
    """Update display name and/or icon_emoji for a user. Returns False if not found."""
    users = await get_users()
    for u in users:
        if u["id"] == user_id:
            if name is not None:
                u["name"] = name.strip()
            if icon_emoji is not None:
                u["icon_emoji"] = icon_emoji.strip()
            await save_users(users)
            return True
    return False


async def set_user_avatar(user_id: str, avatar: str) -> bool:
    """Set (or clear, with "") a user's avatar filename and bump its version. Returns False if not found."""
    users = await get_users()
    for u in users:
        if u["id"] == user_id:
            u["avatar"] = avatar
            u["avatar_version"] = int(u.get("avatar_version", 0)) + 1
            await save_users(users)
            return True
    return False


async def remove_pin(user_id: str) -> bool:
    """Remove PIN from a user (sets to None = no PIN required)."""
    users = await get_users()
    for u in users:
        if u["id"] == user_id:
            u["pin"] = None
            await save_users(users)
            return True
    return False


async def update_user_role(user_id: str, is_admin: bool) -> bool:
    """Set or unset admin flag for a user. Returns False if user not found."""
    users = await get_users()
    for u in users:
        if u["id"] == user_id:
            u["is_admin"] = is_admin
            await save_users(users)
            return True
    return False


# ─── Services ─────────────────────────────────────────────────────────────────

_DEFAULT_SERVICES = [
    {
        "id": "dlna",
        "name": "DLNA / TV Streaming",
        "description": "Stream media to TVs and DLNA-capable devices",
        "isEnabled": True,
    },
    {
        "id": "smb",
        "name": "SMB File Sharing",
        "description": "Windows/Mac/tablet file sharing",
        "isEnabled": True,
    },
    {
        "id": "nfs",
        "name": "NFS File Sharing",
        "description": "Linux/advanced file sharing",
        "isEnabled": False,
    },
    {
        "id": "ssh",
        "name": "SSH",
        "description": "Secure remote terminal",
        "isEnabled": True,
    },
]


async def get_services() -> List[dict]:
    """Return services list, creating defaults if missing."""
    cached = _get_cached("services")
    if cached is not None:
        return cached

    async with _store_lock:
        services = _read_json(settings.services_file, None)
        if services is None:
            _write_json(settings.services_file, _DEFAULT_SERVICES)
            _set_cached("services", _DEFAULT_SERVICES)
            return _DEFAULT_SERVICES

        # Migrate: merge old samba + dlna into unified media service. Guarded on
        # "smb" not in ids too — without that, this would misfire forever on the
        # modern split shape below (which has a standalone, permanent "dlna" but
        # no "media"), fighting the split migration on every cache-expiry re-read
        # and duplicating "smb" once per cycle. Found live 2026-07-14 verifying
        # the SMB/NFS toggle feature: the two migrations ping-ponged a service on
        # and off "media" every 5s (_CACHE_TTL), each cycle adding one more
        # duplicate smb entry — a genuinely unbounded-growth bug, not cosmetic.
        ids = {s["id"] for s in services}
        if "media" not in ids and "smb" not in ids and ("samba" in ids or "dlna" in ids):
            enabled = any(
                s.get("isEnabled", False)
                for s in services
                if s["id"] in ("samba", "dlna")
            )
            services = [
                s for s in services if s["id"] not in ("samba", "dlna")
            ]
            services.insert(0, {
                "id": "media",
                "name": "TV & Computer Sharing",
                "description": "DLNA streaming + SMB file sharing",
                "isEnabled": enabled,
            })
            _write_json(settings.services_file, services)

        # Migrate: split the unified media service back into distinct SMB
        # and DLNA toggles, so each can be controlled independently.
        ids = {s["id"] for s in services}
        if "media" in ids and ("smb" not in ids or "dlna" not in ids):
            media_enabled = next(
                (s.get("isEnabled", False) for s in services if s["id"] == "media"),
                False,
            )
            services = [s for s in services if s["id"] != "media"]
            services.insert(0, {
                "id": "smb",
                "name": "SMB File Sharing",
                "description": "Windows/Mac/tablet file sharing",
                "isEnabled": media_enabled,
            })
            services.insert(0, {
                "id": "dlna",
                "name": "DLNA / TV Streaming",
                "description": "Stream media to TVs and DLNA-capable devices",
                "isEnabled": media_enabled,
            })
            _write_json(settings.services_file, services)

        # Migrate: introduce NFS as a new, separately toggleable service
        # (previously not user-controllable at all).
        ids = {s["id"] for s in services}
        if "nfs" not in ids:
            services = services + [{
                "id": "nfs",
                "name": "NFS File Sharing",
                "description": "Linux/advanced file sharing",
                "isEnabled": False,
            }]
            _write_json(settings.services_file, services)

        _set_cached("services", services)
        return services


async def save_services(services: List[dict]) -> None:
    """Persist services list to disk under lock."""
    async with _store_lock:
        _write_json(settings.services_file, services)
        _set_cached("services", services)


async def toggle_service(service_id: str, enabled: bool) -> bool:
    services = await get_services()
    for svc in services:
        if svc["id"] == service_id:
            svc["isEnabled"] = enabled
            await save_services(services)
            return True
    return False


# ─── Device state ─────────────────────────────────────────────────────────────


async def get_device_state() -> dict:
    """Read device state (name etc.), protected by the store lock."""
    cached = _get_cached("device_state")
    if cached is not None:
        return cached

    async with _store_lock:
        state = _read_json(
            settings.data_dir / "device.json",
            {"name": settings.device_name},
        )
        _set_cached("device_state", state)
        return state


async def update_device_name(name: str) -> None:
    """Update device display name under lock."""
    async with _store_lock:
        dev_file = settings.data_dir / "device.json"
        state = _read_json(dev_file, {"name": settings.device_name})
        state["name"] = name
        _write_json(dev_file, state)
        _set_cached("device_state", state)


# ─── Storage state ────────────────────────────────────────────────────────────

async def get_storage_state() -> dict:
    """Read persisted storage mount info (activeDevice, mountedAt, etc.)."""
    cached = _get_cached("storage_state")
    if cached is not None:
        return cached

    async with _store_lock:
        state = _read_json(settings.storage_file, {})
        _set_cached("storage_state", state)
        return state


async def save_storage_state(state: dict) -> None:
    """Persist storage mount info to disk."""
    async with _store_lock:
        _write_json(settings.storage_file, state)
        _set_cached("storage_state", state)


async def clear_storage_state() -> None:
    """Clear persisted storage state (after unmount)."""
    async with _store_lock:
        _write_json(settings.storage_file, {})
        _set_cached("storage_state", {})


# ─── Tokens (refresh tokens) ─────────────────────────────────────────────────

async def get_tokens() -> List[dict]:
    """Return list of refresh token records."""
    cached = _get_cached("tokens")
    if cached is not None:
        return cached

    async with _store_lock:
        tokens = _read_json(settings.tokens_file, [])
        if not isinstance(tokens, list):
            logger.warning(
                "tokens_file_corrupt type=%s path=%s — all sessions invalidated",
                type(tokens).__name__, settings.tokens_file,
            )
            tokens = []
        _set_cached("tokens", tokens)
        return tokens


async def save_tokens(tokens: List[dict]) -> None:
    """Persist tokens list to disk."""
    async with _store_lock:
        _write_json(settings.tokens_file, tokens)
        _set_cached("tokens", tokens)


async def add_token(record: dict) -> None:
    tokens = await get_tokens()
    tokens.append(record)
    await save_tokens(tokens)


async def get_token(jti: str) -> dict | None:
    tokens = await get_tokens()
    return next((t for t in tokens if t.get("jti") == jti), None)


async def revoke_token(jti: str) -> bool:
    tokens = await get_tokens()
    changed = False
    for t in tokens:
        if t.get("jti") == jti:
            t["revoked"] = True
            changed = True
    if changed:
        await save_tokens(tokens)
    return changed


async def revoke_tokens_for_user(user_id: str) -> int:
    """Revoke every refresh token issued to *user_id*. Called on account deletion so a
    still-cached refresh token can't keep minting access tokens for a user who no longer
    exists. Returns the number of tokens revoked."""
    tokens = await get_tokens()
    changed = 0
    for t in tokens:
        if t.get("userId") == user_id and not t.get("revoked", False):
            t["revoked"] = True
            changed += 1
    if changed:
        await save_tokens(tokens)
    return changed


async def purge_expired_tokens(older_than_ts: int) -> int:
    """Remove tokens whose `expiresAt` is older than `older_than_ts`
    or that have been revoked.  Returns number removed.
    """
    tokens = await get_tokens()
    kept = [
        t for t in tokens
        if t.get("expiresAt", 0) >= older_than_ts and not t.get("revoked", False)
    ]
    removed = len(tokens) - len(kept)
    if removed > 0:
        await save_tokens(kept)
    return removed


# ─── Pairing / OTP persistence ───────────────────────────────────────────────




async def get_otp() -> dict | None:
    """Return the OTP record stored in pairing.json or None if missing/expired.

    The record shape is: {"otp_hash": str, "expires_at": int}
    """
    cached = _get_cached("pairing_otp")
    if cached is not None:
        return cached

    async with _store_lock:
        data = _read_json(settings.data_dir / "pairing.json", None)
        if not data:
            _set_cached("pairing_otp", None)
            return None
        _set_cached("pairing_otp", data)
        return data


async def save_otp(otp_hash: str, expires_at: int) -> None:
    """Persist an OTP record (hash + expiry) to pairing.json under lock."""
    _set_cached("pairing_otp", None)
    async with _store_lock:
        record = {"otp_hash": otp_hash, "expires_at": int(expires_at)}
        _write_json(settings.data_dir / "pairing.json", record)


async def clear_otp() -> None:
    """Clear any stored OTP (remove pairing.json)."""
    _set_cached("pairing_otp", None)
    async with _store_lock:
        # Remove file if it exists; write empty dict for atomicity.
        _write_json(settings.data_dir / "pairing.json", {})


# ---------------------------------------------------------------------------
# Generic key-value store (kv.json) — for simple config blobs
# ---------------------------------------------------------------------------

async def get_value(key: str, default: Any = None) -> Any:
    """Read a value from the generic key-value store (kv.json)."""
    cache_key = f"kv:{key}"
    cached = _cache.get(cache_key)
    if cached is not None:
        value, expires_at = cached
        if time.monotonic() <= expires_at:
            return value  # may legitimately be None
        _cache.pop(cache_key, None)

    async with _store_lock:
        data: Dict[str, Any] = _read_json(settings.data_dir / "kv.json", {})
        value = data.get(key, default)
        _set_cached(cache_key, value)
        return value


async def set_value(key: str, value: Any) -> None:
    """Write a value to the generic key-value store (kv.json)."""
    _set_cached(f"kv:{key}", None)
    async with _store_lock:
        data: Dict[str, Any] = _read_json(settings.data_dir / "kv.json", {})
        data[key] = value
        _write_json(settings.data_dir / "kv.json", data)


async def atomic_update(key: str, fn, default=None) -> Any:
    """Read-modify-write a kv.json key under a single lock acquisition.

    ``fn`` receives the current value and must return the new value.
    Returns the new value after writing.
    """
    _set_cached(f"kv:{key}", None)
    async with _store_lock:
        data: Dict[str, Any] = _read_json(settings.data_dir / "kv.json", {})
        current = data.get(key, default)
        updated = fn(current)
        data[key] = updated
        _write_json(settings.data_dir / "kv.json", data)
        _set_cached(f"kv:{key}", updated)
        return updated


# ---------------------------------------------------------------------------
# Trash metadata (trash.json)
# ---------------------------------------------------------------------------

async def get_trash_items() -> List[dict]:
    """Return all trash item metadata records."""
    cached = _get_cached("trash")
    if cached is not None:
        return cached

    async with _store_lock:
        items = _read_json(settings.trash_file, [])
        _set_cached("trash", items)
        return items


async def save_trash_items(items: List[dict]) -> None:
    """Persist the trash metadata list to disk."""
    async with _store_lock:
        _write_json(settings.trash_file, items)
        _set_cached("trash", items)


async def add_trash_item(item: dict) -> None:
    """Append one trash metadata record atomically. Unlike a get_trash_items() +
    save_trash_items() pair (which each lock individually, leaving a window where two
    concurrent soft-deletes both read the same list and the second save overwrites the
    first's entry), this does the read-append-write under one lock acquisition."""
    async with _store_lock:
        items = _read_json(settings.trash_file, [])
        items.append(item)
        _write_json(settings.trash_file, items)
        _set_cached("trash", items)


async def remove_trash_item(item_id: str) -> None:
    """Remove one trash metadata record by id, atomically (see add_trash_item)."""
    async with _store_lock:
        items = _read_json(settings.trash_file, [])
        items = [i for i in items if i.get("id") != item_id]
        _write_json(settings.trash_file, items)
        _set_cached("trash", items)


# ---------------------------------------------------------------------------
# Activity log (activity_log.json) — persisted, queryable audit trail. Written
# via app/audit.py's audit_log(), a fire-and-forget background task per event
# so callers never need to await persistence.
# ---------------------------------------------------------------------------

_ACTIVITY_LOG_MAX_ENTRIES = 2000


async def get_activity_events() -> List[dict]:
    """Return all persisted activity events, newest first."""
    cached = _get_cached("activity_log")
    if cached is not None:
        return cached

    async with _store_lock:
        events = _read_json(settings.activity_log_file, [])
        _set_cached("activity_log", events)
        return events


async def append_activity_event(event: dict) -> None:
    """Append one activity event (newest first), capping the persisted log at
    _ACTIVITY_LOG_MAX_ENTRIES so it can't grow unbounded over a device's
    lifetime -- oldest entries are dropped first."""
    async with _store_lock:
        events = _read_json(settings.activity_log_file, [])
        events.insert(0, event)
        if len(events) > _ACTIVITY_LOG_MAX_ENTRIES:
            events = events[:_ACTIVITY_LOG_MAX_ENTRIES]
        _write_json(settings.activity_log_file, events)
        _set_cached("activity_log", events)
