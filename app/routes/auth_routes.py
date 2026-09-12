"""
Auth routes — pairing, user creation, logout, PIN management, QR generation.
"""

from __future__ import annotations

import hmac
import logging
import time
from pathlib import Path
from typing import Dict, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from starlette.responses import Response

from ..limiter import limiter

logger = logging.getLogger("aihomecloud.auth")

# In-memory account lockout: target account name → (fail_count, lockout_until_timestamp,
# last_attempt_at). Keyed on the account being logged into, not the caller's IP — a shared IP
# (multiple family members behind one router/relay) must not let one member's fat-fingered PIN
# lock out everyone else, and an attacker rotating source addresses must not be able to
# brute-force one specific account's PIN just by never repeating an IP. Keying on account name
# closes both gaps: the counter follows who's being targeted, not where the request came from.
#
# /auth/login itself needs no auth, and _record_failure() runs even for a nonexistent account
# name (body.name is caller-chosen free text) -- an old prune that only removed entries whose
# lockout_until had already fired never touched an account that stayed under _MAX_FAILURES, so
# submitting one failed attempt per distinct (even fake) account name grew this dict forever.
# Every entry is now aged out by last_attempt_at regardless of whether it ever locked, plus a
# hard cap as a second, independent bound.
_failed_logins: Dict[str, Tuple[int, float, float]] = {}
_MAX_FAILURES = 10
_LOCKOUT_SECONDS = 900  # 15 minutes
_MAX_ENTRIES = 5000  # oldest (by last_attempt_at) evicted first if ever reached


def _prune_failed_logins() -> None:
    """Age out entries inactive for _LOCKOUT_SECONDS, then enforce _MAX_ENTRIES. Called
    opportunistically to keep the dict bounded."""
    now = time.time()
    stale = [
        account for account, (_, _, last_attempt_at) in _failed_logins.items()
        if now - last_attempt_at > _LOCKOUT_SECONDS
    ]
    for account in stale:
        _failed_logins.pop(account, None)

    if len(_failed_logins) > _MAX_ENTRIES:
        by_age = sorted(_failed_logins.items(), key=lambda item: item[1][2])
        for account, _ in by_age[: len(_failed_logins) - _MAX_ENTRIES]:
            _failed_logins.pop(account, None)


def _record_failure(account: str) -> None:
    """Increment failed login counter for a target account; set lockout when threshold reached."""
    record = _failed_logins.get(account)
    count = (record[0] if record else 0) + 1
    now = time.time()
    lockout_until = (now + _LOCKOUT_SECONDS) if count >= _MAX_FAILURES else 0.0
    _failed_logins[account] = (count, lockout_until, now)
    # Opportunistic prune: remove other stale entries while we have the dict open
    _prune_failed_logins()

from ..auth import (
    create_token,
    create_refresh_token,
    decode_refresh_token,
    get_current_user,
    get_current_user_optional,
    require_admin,
    hash_password,
    verify_password,
    verify_shared_secret,
    verify_device_identifier,
    pwd_context,
)
from ..config import settings, get_local_ip
from ..models import (
    AvatarResponse,
    CertFingerprintResponse,
    ChangePinRequest,
    CreatedUserResponse,
    PairingQrResponse,
    LoginResponse,
    ProfileNamesResponse,
    RefreshResponse,
    UserMeResponse,
    CreateUserRequest,
    LoginRequest,
    RefreshRequest,
    PairRequest,
    PairCompleteRequest,
    TokenResponse,
    UpdateProfileRequest,
)
from .. import store
from ..audit import audit_log

router = APIRouter(prefix="/api/v1", tags=["auth"])


def _has_real_content(d: Path) -> bool:
    """True if *d* contains anything beyond app-created scaffolding (currently just
    family/.inbox/, created unconditionally on every startup by main.py's lifespan — without
    excluding it here, that single always-present dir made every dir "non-empty" from the very
    first boot, silently disabling _wipe_stale_nas_dirs entirely, on every installation, forever."""
    for entry in d.rglob("*"):
        try:
            relative_parts = entry.relative_to(d).parts
        except ValueError:
            continue
        if relative_parts and relative_parts[0] == ".inbox":
            continue
        return True
    return False


def _wipe_stale_nas_dirs() -> None:
    """Remove app-managed top-level dirs from NAS root on first-time setup.
    Only wipes dirs that are genuinely empty — refuses to touch any dir that
    contains existing files so a re-pair or lost-users.json cannot destroy data.
    Runs in an executor — never blocks the event loop.
    """
    import shutil
    dirs_to_check = []
    for dirname in ("personal", "family", "entertainment"):
        d = settings.nas_root / dirname
        if d.exists():
            if _has_real_content(d):
                logger.warning(
                    "First-user setup: refusing to wipe non-empty NAS dir %s — "
                    "existing data detected; this is not a fresh installation.", d
                )
                return  # abort entire wipe — at least one dir has data
            dirs_to_check.append(d)
    for d in dirs_to_check:
        try:
            shutil.rmtree(d)
            logger.info("First-time setup: wiped empty NAS dir %s", d)
        except Exception as e:
            logger.warning("Could not wipe stale NAS dir %s: %s", d, e)


async def _bg_wipe_stale_nas_dirs() -> None:
    """Await _wipe_stale_nas_dirs in a thread executor."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _wipe_stale_nas_dirs)
    except Exception as e:
        logger.warning("Background NAS dir wipe failed: %s", e)


async def _rehash_pin(user_id: str, plain_pin: str, expected_hash: str) -> None:
    """
    Background task: re-hash a PIN with the current bcrypt rounds.

    Conditional on the stored hash still being `expected_hash` — the one verified at login.
    Hashing is deliberately slow, so there is a real window in which the user can change their
    PIN before this finishes; without the check, this task would write a hash of the OLD pin
    over the new one and silently revert them. The old PIN would start working again and the
    new one would not, which is both a correctness and a security failure.
    (2026-07-30 auth finding 7.)
    """
    try:
        new_hash = await hash_password(plain_pin)
        updated = await store.update_user_pin(user_id, new_hash, expected_pin=expected_hash)
        if updated:
            logger.info("Auto-upgraded bcrypt rounds for user %s", user_id)
        else:
            # Expected whenever the PIN changed mid-flight. Not an error — the newer value wins.
            logger.info("Skipped bcrypt upgrade for user %s — PIN changed since login", user_id)
    except Exception as e:
        logger.warning("Failed to auto-upgrade PIN hash: %s", e)


@router.get("/pair/qr", response_model=PairingQrResponse)
async def get_pairing_qr(request: Request):
    """
    Return the QR payload string that the Flutter app needs to scan.
    The Cubie displays this as a QR code on its screen or web UI.
    Format: aihomecloud://pair?serial=...&key=...&host=...

    Loopback-only: this response includes the pairing key and a fresh OTP in plaintext, which
    together are enough for a full unconditional-admin pairing (see pair_device's docstring on
    why OTP-based proof-of-physical-access exists at all). If any LAN client could hit this over
    the network, the OTP step would prove nothing — it would just hand the OTP to whoever asked,
    the exact bypass /pair/complete's OTP gate was built to prevent. Only the device's own local
    display/web UI process (running on the same host) is meant to call this.
    """
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This endpoint is only available on the device itself")

    ip = get_local_ip()
    serial = settings.device_serial
    key = settings.pairing_key
    host = f"ahc-{serial}.local"

    # Always generate a fresh short-lived OTP so the caller can display it.
    import hashlib
    import secrets
    from datetime import datetime, timedelta, timezone

    otp = f"{secrets.randbelow(10**6):06d}"
    otp_hash = hashlib.sha256(otp.encode()).hexdigest()
    expires_at = int((datetime.now(timezone.utc) + timedelta(seconds=300)).timestamp())
    await store.save_otp(otp_hash, expires_at)

    from urllib.parse import urlencode
    params = {
        "serial": serial,
        "key": key,
        "host": host,
        "expiresAt": str(expires_at),
    }
    qr_value = "aihomecloud://pair?" + urlencode(params)

    return {
        "qrValue": qr_value,
        "otp": otp,
        "serial": serial,
        "ip": ip,
        "host": host,
        "expiresAt": expires_at,
    }


@router.post("/pair", response_model=TokenResponse)
@limiter.limit("10/minute")
async def pair_device(request: Request, body: PairRequest):
    """
    Pair with the Cubie by providing its serial + pairing key. Returns a JWT, but NOT an
    admin-capable one — the serial is low-entropy (last 6 hex of the MAC) and the pairing key
    is a long-lived shared secret, so anyone on the LAN who obtains both would otherwise get
    unconditional admin (require_admin previously granted it to any type="device" token). Full
    admin capability requires /pair/complete's OTP step, which needs visual/physical access to
    the device's own QR display — that's the actual proof of possession this flow is meant to
    gate on.
    """
    # verify_* rather than hmac.compare_digest directly: compare_digest("", "") is True, so a
    # damaged or empty stored pairing key would have accepted an empty supplied key from anyone on
    # the LAN. These fail closed on missing/empty/truncated stored material. (2026-08-09 sweep.)
    if not verify_device_identifier(body.serial, settings.device_serial):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Unknown serial")
    if not verify_shared_secret(body.key, settings.pairing_key, label="pairing key"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid pairing key")

    token = create_token(subject=body.serial, extra={"type": "device_unverified"})
    return TokenResponse(token=token)


@router.post("/pair/complete", response_model=TokenResponse)
@limiter.limit("5/minute")
async def pair_complete(request: Request, body: PairCompleteRequest):
    """
    Complete pairing by validating serial, pairing key, and OTP.
    On success, clears stored OTP and returns a device JWT.
    """
    if not verify_device_identifier(body.serial, settings.device_serial):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Unknown serial")
    if not verify_shared_secret(body.key, settings.pairing_key, label="pairing key"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid pairing key")

    # Validate OTP
    import hashlib
    from datetime import datetime, timezone

    otp_rec = await store.get_otp()
    now = int(datetime.now(timezone.utc).timestamp())
    if not otp_rec or not otp_rec.get("otp_hash") or not otp_rec.get("expires_at"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No active OTP for pairing")
    if int(otp_rec.get("expires_at", 0)) < now:
        # Clear expired OTP
        await store.clear_otp()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "OTP expired")

    provided_hash = hashlib.sha256(body.otp.encode()).hexdigest()
    if not hmac.compare_digest(provided_hash, otp_rec.get("otp_hash")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid OTP")

    # OTP valid — clear it and issue device token
    await store.clear_otp()
    token = create_token(subject=body.serial, extra={"type": "device"})
    return TokenResponse(token=token)


@router.get("/auth/cert-fingerprint", response_model=CertFingerprintResponse)
async def cert_fingerprint():
    """Return SHA-256 fingerprint of server TLS cert (DER hex)."""
    import hashlib
    from base64 import b64decode
    try:
        cert_bytes = settings.tls_cert_path.read_bytes()
        lines = cert_bytes.decode().splitlines()
        der_lines = []
        inside = False
        for line in lines:
            if "BEGIN CERTIFICATE" in line:
                inside = True
                continue
            if "END CERTIFICATE" in line:
                break
            if inside:
                der_lines.append(line)
        der = b64decode("".join(der_lines))
        fp = hashlib.sha256(der).hexdigest()
        return {"fingerprint": fp, "algorithm": "sha256"}
    except FileNotFoundError:
        return {"fingerprint": None, "algorithm": "sha256"}


@router.get("/auth/users/names", response_model=ProfileNamesResponse)
async def list_user_names():
    """
    Return user names and PIN status for the login picker.
    has_pin is True when the account has a PIN set, False when no PIN required.
    No auth required — this is public so the picker can show before login.
    The actual PIN hash is never returned.
    """
    users = await store.get_users()
    return {
        "users": [
            {
                "name": u["name"],
                "has_pin": bool(u.get("pin")),
                "icon_emoji": u.get("icon_emoji", ""),
                "avatar": u.get("avatar", ""),
                "avatar_version": int(u.get("avatar_version", 0)),
            }
            for u in users
        ]
    }


@router.post("/users", status_code=status.HTTP_201_CREATED, response_model=CreatedUserResponse)
@limiter.limit("5/minute")
async def create_user(
    request: Request,
    body: CreateUserRequest,
    caller: dict | None = Depends(get_current_user_optional),
):
    """Create a new user. First call (setup) is unauthenticated; all subsequent calls require admin."""
    if not body.name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name cannot be empty")
    # No PIN at all is a deliberate, allowed choice (a family member may not want one) --
    # but a NON-empty PIN under 4 characters is a false-security trap: it looks protected
    # but offers almost none. change_pin() already enforces this same floor; found live
    # 2026-07-16 (full-repo audit, SEC-2) that create_user() never did, so a 1-digit PIN
    # was accepted at account creation, silently open to anyone on the LAN who guesses it.
    if body.pin and len(body.pin) < 4:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PIN must be at least 4 digits")

    # Reject duplicate names, case-insensitively, matching what PUT /users/me already enforces.
    # store.add_user does not check: it appends unconditionally, and the personal folder is
    # `personal_path / name`, so two profiles called "Paras" share /srv/nas/personal/Paras/.
    # Deleting either account then deletes the other's photos. (2026-07-30 auth finding 9,
    # which asked for store.add_user to be read before deciding — it was, and it does not
    # enforce this.)
    requested = body.name.strip()

    # Hash the PIN before entering the creation lock so we don't hold it during bcrypt.
    hashed_pin = await hash_password(body.pin) if body.pin else None

    # Acquire the dedicated creation lock so the read → check → write sequence is
    # atomic.  We cannot reuse store._store_lock here because add_user() also
    # acquires it (via save_users), which would deadlock.
    async with store._user_creation_lock:
        existing = await store.get_users()

        # Duplicate-name check lives INSIDE the lock. Outside it, two concurrent creates of the
        # same name both saw an empty result and both proceeded — and since the personal folder is
        # `personal_path / name`, the two profiles then shared one directory, so deleting either
        # deleted the other's photos.
        for other in existing:
            if other.get("name", "").lower() == requested.lower():
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "That name is already taken by another profile",
                )

        # "Empty store" is NOT the same question as "never set up", and only the second one may
        # open unauthenticated admin creation. _read_json returns [] for a users.json that is
        # missing or unrecoverably corrupt, so the old `len(existing) == 0` test handed admin to
        # whoever posted first whenever an established board's store was damaged — on a board that
        # already holds a family's photos. The durable marker is what distinguishes the two states.
        # (2026-08-08 audit, M-11.)
        already_set_up = store.setup_completed()
        is_first_user = len(existing) == 0 and not already_set_up

        if len(existing) == 0 and already_set_up:
            # The board has an owner but no readable user list. Refuse rather than bootstrap: the
            # correct recovery is restoring the store or a factory reset, both of which need
            # access to the device itself.
            logger.error(
                "user_store_empty_but_setup_complete — refusing unauthenticated admin creation"
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "This device is already set up but its profile list could not be read. "
                "Restore from backup or factory-reset the device.",
            )

        if not is_first_user:
            # Require admin auth once the first user has been created
            if caller is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
            await require_admin(caller)

        is_admin = is_first_user

        # First-time setup: remove stale app folders from a previous
        # installation before creating the new user's directory hierarchy.
        # Awaited (not background) to avoid a race where the wipe deletes
        # folders that add_user just created.
        if is_first_user:
            await _bg_wipe_stale_nas_dirs()

        user = await store.add_user(
            body.name,
            hashed_pin,
            is_admin=is_admin,
            icon_emoji=body.icon_emoji.strip(),
        )

        # Written inside the lock, immediately after the admin exists, so the window cannot reopen
        # even if the process dies here — on restart the marker is already on disk.
        if is_first_user:
            await store.mark_setup_completed()
            audit_log("first_admin_created", actor_id=user["id"], user_name=user["name"])

    # Auto-login: return tokens immediately so the client needs only one request.
    access_token = create_token(
        subject=user["id"],
        extra={"type": "user", "is_admin": is_admin},
    )
    refresh_token_str, _jti, _exp = await create_refresh_token(user["id"])

    return {
        "id": user["id"],
        "name": user["name"],
        "isAdmin": user.get("is_admin", False),
        "accessToken": access_token,
        "refreshToken": refresh_token_str,
    }


@router.post("/auth/login", response_model=LoginResponse)
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest):
    """Login with username and PIN and return an access token."""
    lockout_key = body.name
    now = time.time()

    # Prune stale entries before checking — keeps dict bounded
    _prune_failed_logins()

    # Check account lockout
    record = _failed_logins.get(lockout_key)
    if record:
        count, lockout_until, _ = record
        if lockout_until > now:
            remaining = int(lockout_until - now)
            minutes = max(remaining // 60, 1)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Too many failed attempts. Try again in {minutes} minute(s).",
            )
        if lockout_until > 0:
            # Lockout expired — reset counter
            _failed_logins.pop(lockout_key, None)

    users = await store.get_users()
    found = next((u for u in users if u.get("name") == body.name), None)
    if not found:
        _record_failure(lockout_key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    stored_pin = found.get("pin")
    if not stored_pin:
        # User has no PIN set — allow login with any input (including empty string)
        pass
    elif str(stored_pin).startswith("$2"):
        ok = await verify_password(body.pin, stored_pin)
        if not ok:
            _record_failure(lockout_key)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
        # Auto-upgrade bcrypt rounds if configuration changed (e.g. 12 → 10).
        if pwd_context.needs_update(stored_pin):
            import asyncio as _aio
            _aio.create_task(_rehash_pin(found["id"], body.pin, stored_pin))
    else:
        logger.warning("Non-bcrypt PIN found for user %s — rejecting", body.name)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    # Success — clear lockout counter
    _failed_logins.pop(lockout_key, None)

    access_token = create_token(
        subject=found["id"],
        extra={
            "type": "user",
            "is_admin": bool(found.get("is_admin", False)),
        },
    )
    refresh_token, jti, expires_at = await create_refresh_token(found["id"])
    return {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "user": {
            "id": found["id"],
            "name": found["name"],
            # The client has read this since forever with a "👤" fallback, so every login
            # showed the generic emoji instead of the user's own until it was actually sent.
            "icon_emoji": found.get("icon_emoji", ""),
            "isAdmin": bool(found.get("is_admin", False)),
        },
    }


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest | None = None, user: dict = Depends(get_current_user)):
    """Logout — revoke provided refresh token (if any)."""
    if body and getattr(body, "refresh_token", None):
        try:
            payload = decode_refresh_token(body.refresh_token)
            # Only revoke a token that belongs to the caller. Without this, anyone holding
            # another user's refresh token could revoke it — logging that person out at will.
            # It presumes an already-compromised token, so severity is low, but "you may only
            # revoke your own session" is the obvious rule and it was simply absent.
            # (2026-07-30 auth finding 8.)
            if payload.get("sub") != user.get("sub"):
                # Deliberately silent: answering differently for "not yours" than for
                # "invalid" would confirm to a caller that a token is real and whose it is.
                return None
            jti = payload.get("jti")
            if jti:
                await store.revoke_token(jti)
        except HTTPException:
            # treat invalid token as already logged out
            pass
    return None



@router.post("/auth/refresh", response_model=RefreshResponse)
@limiter.limit("30/minute")
async def refresh(request: Request, body: RefreshRequest):
    """Exchange a refresh token for a new access token."""
    payload = decode_refresh_token(body.refresh_token)
    jti = payload.get("jti")
    if not jti:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    rec = await store.get_token(jti)
    if not rec or rec.get("revoked", False):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token revoked")
    # Issue new access token
    subject = payload.get("sub")
    # Lookup user to set is_admin flag — also acts as the deleted-account check: a refresh
    # token surviving past its user's deletion (e.g. deletion predates the revoke-on-delete
    # fix) must not keep minting access tokens for an account that no longer exists.
    found = await store.find_user(subject)
    if not found:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    extra = {"type": "user", "is_admin": bool(found.get("is_admin", False))}
    access_token = create_token(subject=subject, extra=extra)
    return {"accessToken": access_token}


@router.put("/users/pin", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def change_pin(request: Request, body: ChangePinRequest, user: dict = Depends(get_current_user)):
    """Change the current user's PIN."""
    if len(body.new_pin) < 4:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PIN must be at least 4 digits")

    user_id = user.get("sub", "")
    found = await store.find_user(user_id)
    if not found:
        # Deleted user with a still-valid token, or a device_unverified token (sub = device
        # serial, no matching user record) — either way there's no real account to update.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if found.get("pin"):
        stored_pin = found.get("pin")
        if str(stored_pin).startswith("$2"):
            if not body.old_pin or not await verify_password(body.old_pin, stored_pin):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Old PIN does not match")
        else:
            if not hmac.compare_digest(str(stored_pin).encode(), body.old_pin.encode() if body.old_pin else b""):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Old PIN does not match")

    await store.update_user_pin(user_id, await hash_password(body.new_pin))

    # M-1 fix (security audit 2026-08): without this, a refresh token minted under the old PIN
    # keeps working for up to 30 days after the change — e.g. a device the account owner meant
    # to lock out by rotating the PIN stays logged in regardless. Same primitive already used by
    # self-delete below; the caller's own session gets revoked too, matching a PIN change's
    # intent (force every device to re-authenticate with the new PIN, this one included).
    await store.revoke_tokens_for_user(user_id)


@router.get("/users/me", response_model=UserMeResponse)
async def get_my_profile(user: dict = Depends(get_current_user)):
    """Return the current user's own profile data."""
    user_id = user.get("sub", "")
    found = await store.find_user(user_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return {
        "id": found["id"],
        "name": found["name"],
        "icon_emoji": found.get("icon_emoji", ""),
        "has_pin": bool(found.get("pin")),
        "is_admin": found.get("is_admin", False),
        "avatar": found.get("avatar", ""),
        "avatar_version": int(found.get("avatar_version", 0)),
    }


@router.put("/users/me", status_code=status.HTTP_204_NO_CONTENT)
async def update_my_profile(
    body: UpdateProfileRequest,
    user: dict = Depends(get_current_user),
):
    """Update current user's display name and/or emoji icon."""
    user_id = user.get("sub", "")
    old_name: str | None = None

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Name cannot be empty"
            )
        # Prevent duplicate names (case-insensitive)
        existing = await store.get_users()
        for u in existing:
            if u["id"] != user_id and u["name"].lower() == name.lower():
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "That name is already taken by another profile",
                )
        current = next((u for u in existing if u["id"] == user_id), None)
        if current is not None and current["name"] != name:
            old_name = current["name"]

    updated = await store.update_user_profile(
        user_id,
        name=body.name,
        icon_emoji=body.icon_emoji,
    )
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if old_name is not None and body.name is not None:
        # M2: /media queries authorize/filter purely on media_index's stored owner column, not
        # the physical folder name — without this, the member's pre-rename personal photos would
        # silently stop matching scope=personal&owner=<newname> and vanish from "Mine".
        from .. import media_index
        await media_index.rename_owner(old_name, body.name.strip())


async def _avatar_jpeg_from_bytes(raw: bytes) -> bytes:
    """Downscale arbitrary image bytes to a <=256px JPEG. Raises if not an image."""
    import asyncio
    import io
    from PIL import Image, ImageOps

    def _make() -> bytes:
        with Image.open(io.BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((256, 256), Image.Resampling.LANCZOS)  # ponytail: max-edge, not square-cropped
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue()

    return await asyncio.get_running_loop().run_in_executor(None, _make)


@router.post("/users/avatar", response_model=AvatarResponse)
@limiter.limit("10/minute")
async def set_my_avatar(
    request: Request,
    file: UploadFile | None = File(None),
    source_path: str | None = Form(None),
    user: dict = Depends(get_current_user),
):
    """Set the caller's avatar from an uploaded image OR an existing NAS file path."""
    from .file_routes import (
        _authorize_path,
        _generate_image_thumbnail,
        _require_external_storage,
        _safe_resolve,
    )

    user_id = user.get("sub", "")
    if not await store.find_user(user_id):
        # Deleted user with a still-valid token, or a device_unverified token (sub = device
        # serial, no matching user record) — either way there's no real account to attach this to.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if (file is None) == (source_path is None):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide exactly one of file or source_path")

    _require_external_storage()

    if source_path is not None:
        resolved = _safe_resolve(source_path)
        await _authorize_path(resolved, user)  # user may only pick files they can access
        if not resolved.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source file not found")
        try:
            data = await _generate_image_thumbnail(resolved, 256)
        except Exception:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Source file is not an image")
    else:
        # M-4 fix (security audit 2026-08): read one byte past the cap rather than the whole
        # body -- an oversized upload is rejected without ever buffering it in full. A plain
        # `await file.read()` had no size limit of its own; only the much larger transport-level
        # cap (max_upload_bytes, 25 GB) applied, which is legal-request-sized, not
        # avatar-sized, and would OOM a 1 GB-RAM board on a request nowhere near that limit.
        raw = await file.read(settings.max_avatar_bytes + 1)
        if len(raw) > settings.max_avatar_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Avatar image is too large")
        try:
            data = await _avatar_jpeg_from_bytes(raw)
        except Exception:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is not an image")

    settings.avatars_dir.mkdir(parents=True, exist_ok=True)
    (settings.avatars_dir / f"{user_id}.jpg").write_bytes(data)
    await store.set_user_avatar(user_id, f"{user_id}.jpg")
    found = await store.find_user(user_id)
    return {
        "avatar": f"{user_id}.jpg",
        "avatarVersion": int(found.get("avatar_version", 0)) if found else 0,
        "path": f"/.avatars/{user_id}.jpg",
    }


@router.delete("/users/avatar", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_avatar(user: dict = Depends(get_current_user)):
    """Remove the caller's avatar (reverts the client to initials)."""
    user_id = user.get("sub", "")
    try:
        (settings.avatars_dir / f"{user_id}.jpg").unlink(missing_ok=True)
    except OSError:
        pass
    await store.set_user_avatar(user_id, "")


@router.get("/users/avatar/{filename}",
    responses={200: {"content": {"image/jpeg": {}}}},
    response_class=Response,
)
@limiter.limit("120/minute")
async def get_user_avatar(request: Request, filename: str):
    """Public: serve a user's avatar JPEG. Unauthenticated because the login profile
    picker (which shows these) runs before any token exists — user names are already
    public via /auth/users/names. Only serves basenames inside avatars_dir (no traversal)."""
    from fastapi.responses import FileResponse

    safe = Path(filename).name  # strip any path components
    if not safe.endswith(".jpg"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    path = settings.avatars_dir / safe
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No avatar")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.delete("/users/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_profile(user: dict = Depends(get_current_user)):
    """
    Delete the current user's own profile and personal folder.
    Blocked if this user is the only remaining user, or the only admin.
    """
    import shutil as _shutil

    user_id = user.get("sub", "")
    found = await store.find_user(user_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    all_users = await store.get_users()

    # Block if last user
    if len(all_users) <= 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot delete the only profile on this device",
        )

    # Block if last admin
    if found.get("is_admin"):
        admins = [u for u in all_users if u.get("is_admin")]
        if len(admins) <= 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Cannot delete the only admin profile",
            )

    # Remove from users list
    removed = await store.remove_user(user_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    # Revoke any refresh tokens this user's devices still hold — without this, a cached
    # refresh token keeps minting valid access tokens for the deleted account for up to 30 days.
    await store.revoke_tokens_for_user(user_id)

    audit_log("user_deleted_self", actor_id=user_id, user_name=found["name"])

    # Delete personal folder (best-effort, non-blocking)
    safe_name = Path(found["name"]).name
    personal_dir = settings.personal_path / safe_name
    if personal_dir.exists() and personal_dir.is_dir():
        try:
            _shutil.rmtree(personal_dir)
            logger.info("Deleted personal folder for user %s", found["name"])
        except Exception as exc:
            logger.warning("Could not delete folder for %s: %s", found["name"], exc)
        else:
            # Keep media_index in sync — without this, deleting a profile left
            # media_index unaware every file under it was gone (they'd keep
            # showing up until the next reconcile pass). This also clears
            # dedup: ingest's duplicate lookup reads live media_index entries
            # directly, so identical content re-uploaded by a future user with
            # the same name is treated as new. Mirrors
            # file_routes._soft_delete_resolved's per-file equivalent for this bulk case.
            from .. import media_index

            rel_path_prefix = f"/{settings.personal_base}/{safe_name}"
            await media_index.mark_entries_deleted_by_prefix(rel_path_prefix)


@router.delete("/users/pin", status_code=status.HTTP_204_NO_CONTENT)
async def remove_my_pin(user: dict = Depends(get_current_user)):
    """Remove the current user's PIN so no PIN is required to log in."""
    user_id = user.get("sub", "")
    removed = await store.remove_pin(user_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

