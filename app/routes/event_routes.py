"""
In-app event bus for AiHomeCloud.
Broadcasts events to all connected WebSocket clients (Flutter apps).
"""

import asyncio
import json
import logging
import time
from enum import Enum
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

logger = logging.getLogger("aihomecloud.events")

router = APIRouter(tags=["events"])


# ─── Event types ─────────────────────────────────────────────────────────────

class EventSeverity(str, Enum):
    info = "info"
    success = "success"
    warning = "warning"
    error = "error"


@dataclass
class AppEvent:
    type: str            # e.g. "upload_complete", "storage_warning"
    title: str
    body: str
    severity: str        # EventSeverity value
    timestamp: float     # epoch seconds
    data: Optional[dict] = None
    admin_only: bool = False  # if True, only sent to admin subscribers
    # M-2 fix (security audit 2026-08): set to the lowercased owner name when this event
    # describes something under a specific member's /personal/ scope -- only that member (or an
    # admin) gets it. None means the event isn't scope-restricted (a family/entertainment upload,
    # or any non-upload event), so every subscriber still sees it -- unchanged from before this
    # fix. Without this, "upload_complete" leaked every member's private upload activity
    # (filename + who) to every other connected family member in real time, the same class of
    # cross-member privacy leak as H-4's hash oracle and the duplicate-scanner's own
    # _visible_duplicate_sets filter (backup_routes.py), which this reuses the exact convention of.
    personal_owner: Optional[str] = None

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("admin_only", None)  # don't expose internal field to clients
        d.pop("personal_owner", None)  # server-internal filtering metadata, not for the client
        return json.dumps(d)


# ─── Global event bus ────────────────────────────────────────────────────────

class EventBus:
    """Simple pub-sub for broadcasting events to WebSocket clients."""

    def __init__(self):
        from ..config import settings as _settings
        self._subscribers: list[asyncio.Queue] = []
        self._recent: list[AppEvent] = []
        self._max_recent: int = _settings.event_max_recent
        self._queue_size: int = _settings.event_queue_size

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers = [s for s in self._subscribers if s is not q]

    async def publish(self, event: AppEvent):
        """Broadcast event to all subscribers."""
        self._recent.append(event)
        if len(self._recent) > self._max_recent:
            self._recent = self._recent[-self._max_recent:]

        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        if dead:
            logger.warning(
                "event_bus dropped %d slow subscriber(s) for event '%s' "
                "(queue full — client not consuming fast enough)",
                len(dead),
                event.type,
            )
            for q in dead:
                self._subscribers = [s for s in self._subscribers if s is not q]

        logger.debug("Event published: %s (%d subscribers)", event.type, len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Singleton
event_bus = EventBus()


# ─── Convenience emitters ───────────────────────────────────────────────────

async def emit_upload_complete(file_name: str, user_name: str, personal_owner: Optional[str] = None):
    await event_bus.publish(AppEvent(
        type="upload_complete",
        title="Upload Complete",
        body=f"{file_name} uploaded by {user_name}",
        severity=EventSeverity.success.value,
        timestamp=time.time(),
        data={"fileName": file_name, "userName": user_name},
        personal_owner=personal_owner,
    ))


async def emit_storage_warning(used_percent: float, free_gb: float):
    if used_percent >= 95:
        severity = EventSeverity.error.value
        title = "Storage Critical"
        body = f"Only {free_gb:.1f} GB free ({used_percent:.0f}% full)"
    elif used_percent >= 85:
        severity = EventSeverity.warning.value
        title = "Storage Low"
        body = f"{free_gb:.1f} GB remaining ({used_percent:.0f}% full)"
    else:
        return  # No warning needed

    await event_bus.publish(AppEvent(
        type="storage_warning",
        title=title,
        body=body,
        severity=severity,
        timestamp=time.time(),
        data={"usedPercent": used_percent, "freeGB": free_gb},
        admin_only=True,
    ))


async def emit_service_toggled(service_name: str, enabled: bool):
    await event_bus.publish(AppEvent(
        type="service_toggled",
        title=f"{service_name} {'Started' if enabled else 'Stopped'}",
        body=f"{service_name} has been {'enabled' if enabled else 'disabled'}",
        severity=EventSeverity.info.value,
        timestamp=time.time(),
        data={"serviceName": service_name, "enabled": enabled},
        admin_only=True,
    ))


async def emit_device_mounted(device: str, mount_point: str):
    # Strip /dev/ prefix — never expose raw device paths to clients
    device_display = Path(device).name if device.startswith("/dev/") else device
    await event_bus.publish(AppEvent(
        type="device_mounted",
        title="Storage Mounted",
        body=f"Drive mounted at {mount_point}",
        severity=EventSeverity.success.value,
        timestamp=time.time(),
        data={"device": device_display, "mountPoint": mount_point},
        admin_only=True,
    ))


async def emit_device_ejected(device: str):
    device_display = Path(device).name if device.startswith("/dev/") else device
    await event_bus.publish(AppEvent(
        type="device_ejected",
        title="Device Ejected",
        body=f"Drive safely removed",
        severity=EventSeverity.info.value,
        timestamp=time.time(),
        data={"device": device_display},
        admin_only=True,
    ))


def _should_deliver(event: AppEvent, *, is_admin: bool, my_name: str) -> bool:
    """Whether a subscriber should receive *event* -- pulled out of events_ws's loop so the
    actual delivery decision (not just that AppEvent fields get set) is directly testable."""
    if event.admin_only and not is_admin:
        return False
    # M-2 fix: skip events scoped to another member's /personal/ area.
    if event.personal_owner and not is_admin and event.personal_owner != my_name.lower():
        return False
    return True


# ─── WebSocket endpoint ─────────────────────────────────────────────────────

@router.websocket("/ws/events")
async def events_ws(ws: WebSocket, token: str = Query(default=None)):
    """
    Push in-app notifications to connected Flutter clients.
    Each event is a JSON object: {type, title, body, severity, timestamp, data}.
    Requires JWT token as query parameter: /ws/events?token=<jwt>
    """
    # Authenticate before accepting
    if not token:
        await ws.close(code=4001, reason="Missing token")
        return
    is_admin = False
    my_name = ""
    try:
        from ..auth import decode_token
        from .. import store as _store
        payload = decode_token(token)
        if payload.get("type") == "device":
            is_admin = True
        else:
            user_id = payload.get("sub", "")
            found = await _store.find_user(user_id)
            is_admin = bool(found and found.get("is_admin", False))
            my_name = (found.get("name") if found else "") or ""
    except Exception:
        await ws.close(code=4003, reason="Invalid token")
        return

    await ws.accept()
    queue = event_bus.subscribe()

    try:
        while True:
            event = await queue.get()
            if not _should_deliver(event, is_admin=is_admin, my_name=my_name):
                continue
            await ws.send_text(event.to_json())
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        event_bus.unsubscribe(queue)
