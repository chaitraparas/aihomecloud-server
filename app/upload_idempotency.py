"""
Upload idempotency — makes a retried upload safe to repeat.

Why this exists: a phone uploading a photo over a home LAN loses the connection
routinely — the user walks out of Wi-Fi range, the board reboots, the socket dies
after the bytes landed but before the 201 came back. The client cannot tell "never
arrived" apart from "arrived, response lost", so it retries, and the NAS grows a
second copy of the same photo. Content hashing does not solve this: two genuinely
distinct shots seconds apart are different bytes, and the same bytes uploaded
deliberately twice is a legitimate action.

The fix is for the *client* to say "this is the same logical upload as before" by
minting one UUIDv7 per logical upload and reusing it across every retry of that
upload. The server records the outcome against that key and replays it instead of
writing the file again.

UUIDv7 specifically, not any UUID: its first 48 bits are a millisecond timestamp,
so a key carries its own age and pruning needs no separate index or clock skew
assumption. A non-v7 UUID is rejected rather than quietly accepted, because
accepting it would mean keys that can never be pruned by age.

Keys are scoped to the user who created them. A key presented by a different user
is treated as absent, so one account can neither observe nor collide with another's
upload keys.

Storage is its own small SQLite file with a single serialized connection rather
than a pool: uploads are rate-limited to 120/minute and each one performs two tiny
writes, so a pool would cost RAM on a 1GB SBC to solve contention that does not
exist. Losing this database entirely is safe — the worst case is that an upload
in flight at that moment gets duplicated, exactly as it would today.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .config import settings

logger = logging.getLogger("aihomecloud.upload_idempotency")

# A claim older than this is assumed to belong to a request that died without
# finishing (process killed mid-upload). Generous: a large upload over a slow LAN
# can legitimately hold a claim for minutes.
STALE_CLAIM_SECONDS = 30 * 60

# Keys are retained well past any plausible retry window, then dropped. A client
# retrying a 24-hour-old upload is starting a new upload by any reasonable reading.
KEY_TTL_SECONDS = 7 * 24 * 3600

STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"

_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def _db_path() -> Path:
    return settings.data_dir / "upload_idempotency.db"


def init_db() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            return
        conn = sqlite3.connect(str(_db_path()), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_keys (
                key        TEXT PRIMARY KEY,
                user_sub   TEXT NOT NULL,
                status     TEXT NOT NULL,
                response   TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        conn.commit()
        _conn = conn


def close_db() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def uuid7_timestamp_ms(key: str) -> Optional[int]:
    """Milliseconds encoded in a UUIDv7, or None if this is not a valid UUIDv7."""
    try:
        parsed = uuid.UUID(key)
    except (ValueError, AttributeError, TypeError):
        return None
    if parsed.version != 7:
        return None
    return parsed.int >> 80  # first 48 bits are the millisecond timestamp


def is_valid_key(key: str) -> bool:
    return uuid7_timestamp_ms(key) is not None


def begin(key: str, user_sub: str) -> tuple[str, Optional[dict[str, Any]]]:
    """
    Claim `key` for this upload.

    Returns one of:
      ("claimed",     None)      caller owns the key and should perform the upload
      ("replayed",    response)  this key already completed; return `response` again
      ("in_progress", None)      another request holds the claim right now
    """
    if _conn is None:
        raise RuntimeError("upload_idempotency.init_db() was not called")

    now = time.time()
    with _lock:
        row = _conn.execute(
            "SELECT status, response, created_at FROM upload_keys WHERE key = ? AND user_sub = ?",
            (key, user_sub),
        ).fetchone()

        if row is not None:
            if row["status"] == STATUS_DONE:
                try:
                    return "replayed", json.loads(row["response"])
                except (TypeError, ValueError):
                    # A corrupt stored response must not wedge the client forever;
                    # drop it and let this attempt proceed as a fresh upload.
                    logger.warning("discarding unreadable stored response for key")
                    _conn.execute("DELETE FROM upload_keys WHERE key = ?", (key,))
                    _conn.commit()
                    # falls through to the INSERT below, as a fresh claim
            elif now - row["created_at"] < STALE_CLAIM_SECONDS:
                return "in_progress", None
            else:
                # The previous holder died without finishing. Take the claim over.
                _conn.execute(
                    "UPDATE upload_keys SET created_at = ? WHERE key = ?", (now, key)
                )
                _conn.commit()
                return "claimed", None

        try:
            _conn.execute(
                "INSERT INTO upload_keys (key, user_sub, status, response, created_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (key, user_sub, STATUS_IN_PROGRESS, now),
            )
            _conn.commit()
        except sqlite3.IntegrityError:
            # Lost the race to insert. Either another request of ours holds it, or
            # the key belongs to a different user — indistinguishable on purpose.
            return "in_progress", None
        return "claimed", None


def finish(key: str, response: dict[str, Any]) -> None:
    """Record the outcome so a later retry replays it instead of re-uploading."""
    if _conn is None:
        return
    with _lock:
        _conn.execute(
            "UPDATE upload_keys SET status = ?, response = ? WHERE key = ?",
            (STATUS_DONE, json.dumps(response), key),
        )
        _conn.commit()


def abandon(key: str) -> None:
    """Release a claim after a failed upload, so the client can retry the same key."""
    if _conn is None:
        return
    with _lock:
        _conn.execute(
            "DELETE FROM upload_keys WHERE key = ? AND status = ?", (key, STATUS_IN_PROGRESS)
        )
        _conn.commit()


def prune(ttl_seconds: int = KEY_TTL_SECONDS) -> int:
    """Drop keys past their TTL. Returns how many were removed."""
    if _conn is None:
        return 0
    cutoff = time.time() - ttl_seconds
    with _lock:
        cur = _conn.execute("DELETE FROM upload_keys WHERE created_at < ?", (cutoff,))
        _conn.commit()
        return cur.rowcount
