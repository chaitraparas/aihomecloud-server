"""Audit logger — writes structured records to the app log AND to a bounded,
disk-persisted activity log (queryable via GET /api/v1/activity/events).

Previously log-only: nothing was queryable or surfaced in-app, a real gap
against a "who changed what, when" bar. Persistence is fire-and-forget
(scheduled as a background task, not awaited) so the 5 existing call sites
(all synchronous, inside various route handlers) don't need to become async
just to await it, and an audit record failing to persist never blocks or
fails the request that triggered it — the log line is still written either way.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("aihomecloud.audit")


def audit_log(event: str, **kwargs: Any) -> None:
    """Log an audit event with structured context, and persist it to the
    queryable activity log in the background."""
    logger.info("AUDIT %s %s", event, kwargs)
    record = {"event": event, "timestamp": time.time(), **kwargs}
    try:
        asyncio.create_task(_persist_event(record))
    except RuntimeError:
        # No running event loop (e.g. a script/test calling this outside asyncio) --
        # the log line above already captured the event; persistence is best-effort.
        pass


async def _persist_event(record: dict) -> None:
    from . import store  # deferred: keeps audit.py import-safe for any module that
                          # doesn't otherwise need the full store module at import time

    try:
        await store.append_activity_event(record)
    except Exception:
        logger.exception("Failed to persist activity event: %s", record.get("event"))
