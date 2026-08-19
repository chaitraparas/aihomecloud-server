"""
JWT authentication utilities.
"""

import asyncio
import functools
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt import InvalidTokenError
from passlib.context import CryptContext
import uuid

from . import store

from .config import settings

logger = logging.getLogger("aihomecloud.auth")

#: Shortest usable pairing key. `generate_pairing_key` emits `secrets.token_urlsafe(16)` (22 chars);
#: materially shorter means the stored material is damaged, not merely unusual.
MIN_SHARED_SECRET_LEN = 16


def verify_shared_secret(provided: object, expected: object, *, minimum: int = MIN_SHARED_SECRET_LEN,
                         label: str = "shared secret") -> bool:
    """
    Constant-time comparison that FAILS CLOSED when the stored secret is unusable.

    `hmac.compare_digest("", "")` returns **True**. Correct for a digest comparison, catastrophic
    for an authentication check: it makes "we have no secret" and "the caller proved knowledge of
    the secret" produce the same answer. The pairing endpoints compared `body.key` against
    `settings.pairing_key` directly, so an empty stored key would have accepted an empty supplied
    key from anyone on the LAN.

    Reachable because `O_CREAT` precedes `os.write` in `generate_pairing_key`: a power cut in that
    window — routine on an SBC someone unplugs — leaves a zero-byte file that later boots read as
    "". `config.py` now regenerates such a file, but that is one layer. This is the one that
    matters, because it holds even when the value arrives from somewhere else entirely: an env var,
    a future config source, or a bug.

    The rule: **missing, empty, truncated, malformed or unreadable secret material always fails
    closed.** Never "both sides are empty, therefore equal".
    (2026-08-09 adversarial sweep.)
    """
    if not isinstance(expected, str) or len(expected) < minimum:
        # CRITICAL, not warning: the board is running with unusable authentication material and
        # every attempt against it will now be refused. That must be visible in the journal.
        logger.critical(
            "%s is missing or too short (%s) — refusing all comparisons against it. "
            "The stored secret is damaged; regenerate it.",
            label, ("absent" if not isinstance(expected, str) else f"{len(expected)} chars"),
        )
        return False
    if not isinstance(provided, str) or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def verify_device_identifier(provided: object, expected: object) -> bool:
    """
    The same fail-closed rule for the device serial.

    Not a secret — it is derived from the MAC and shown on screen — but it is one half of the
    pairing check, and an empty stored serial matching an empty supplied one would hand a caller
    that half for free. A real serial is always at least a few characters.
    """
    return verify_shared_secret(provided, expected, minimum=4, label="device serial")

_bearer_scheme = HTTPBearer()
# rounds configurable via AHC_BCRYPT_ROUNDS (default 10 ≈ 0.1s on ARM).
# PINs are rate-limited (10 attempts / 15 min lockout), so lower rounds are safe.
def _make_pwd_context() -> CryptContext:
    from .config import settings
    return CryptContext(
        schemes=["bcrypt"], deprecated="auto",
        bcrypt__rounds=settings.bcrypt_rounds,
    )

pwd_context = _make_pwd_context()


async def hash_password(plain: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        functools.partial(pwd_context.hash, plain),
    )


async def verify_password(plain: str, hashed: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        functools.partial(pwd_context.verify, plain, hashed),
    )


def create_token(subject: str, extra: Optional[dict] = None) -> str:
    """Create a signed JWT for the given subject (user id / device serial)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def create_refresh_token(subject: str, expires_days: int = 30) -> tuple[str, str, int]:
    """Create a refresh JWT with a `jti` and persist a token record.

    Returns (token, jti, expires_at_ts).
    """
    now = datetime.now(timezone.utc)
    jti = uuid.uuid4().hex
    exp = now + timedelta(days=expires_days)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": exp,
        "type": "refresh",
        "jti": jti,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    # Persist token record (epoch seconds)
    record = {
        "jti": jti,
        "userId": subject,
        "issuedAt": int(now.timestamp()),
        "expiresAt": int(exp.timestamp()),
        "revoked": False,
    }
    await store.add_token(record)
    return token, jti, int(exp.timestamp())


def decode_refresh_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        if payload.get("type") != "refresh":
            raise InvalidTokenError("Not a refresh token")
        return payload
    except InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises HTTPException on failure.

    Refresh tokens carry `type: "refresh"` and a 30-day expiry — they must only ever be
    accepted by decode_refresh_token (the /auth/refresh exchange), never here. Without this
    check a leaked refresh token works as a Bearer credential on every protected endpoint for
    its full 30-day life, and logout (which only revokes the refresh token's jti) can't end
    that session since access tokens are stateless.
    """
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    if payload.get("type") == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh tokens cannot be used as access tokens",
        )
    return payload


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> dict:
    """FastAPI dependency — extracts & validates the Bearer token."""
    return decode_token(credentials.credentials)


_optional_bearer = HTTPBearer(auto_error=False)


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
) -> Optional[dict]:
    """FastAPI dependency — returns decoded token or None if no auth header."""
    if credentials is None:
        return None
    return decode_token(credentials.credentials)


async def migrate_plaintext_pins() -> int:
    """Hash any plaintext PINs still in the user store. Returns count migrated."""
    users = await store.get_users()
    migrated = 0
    changed = False
    for user in users:
        pin = user.get("pin", "")
        if pin and not str(pin).startswith("$2"):
            user["pin"] = await hash_password(str(pin))
            migrated += 1
            changed = True
    if changed:
        await store.save_users(users)
    return migrated


async def is_currently_admin(user: dict) -> bool:
    """Non-raising admin check, freshly re-verified against the store — same source of truth
    as require_admin(). Use this (never a raw `user.get("is_admin")` read on the JWT payload)
    anywhere an endpoint needs an admin/non-admin branch rather than an outright 403. The JWT's
    own `is_admin` claim is only as fresh as the token's issue time (up to jwt_expire_hours old)
    — a user demoted after login keeps a stale "admin" claim in their still-valid access token,
    so trusting it directly re-grants admin-level access for the rest of that token's life.
    """
    from . import store

    if user.get("type") == "device":
        return True
    found = await store.find_user(user.get("sub", ""))
    return bool(found and found.get("is_admin", False))


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency — ensures the user has admin privileges.
    Works by looking up the user in the store by subject (serial/user_id).
    Only OTP-verified device tokens (type == "device", issued by /pair/complete) are treated as
    admin. type == "device_unverified" (issued by /pair, serial+key only, no OTP) deliberately
    does NOT match here — falls through to the user lookup below and gets rejected, since it has
    no corresponding store entry. Don't widen this check to accept "device_unverified" too; that
    would recreate the exact serial+shared-key -> unconditional-admin bypass this was fixed for.
    """
    from . import store

    if user.get("type") == "device":
        return user  # Device tokens are admin-level

    user_id = user.get("sub", "")
    found = await store.find_user(user_id)
    # Reject if user not found (deleted account with valid JWT) OR not admin
    if not found or not found.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


