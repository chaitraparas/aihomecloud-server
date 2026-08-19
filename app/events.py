"""
Internal file-event bus for AiHomeCloud.

publish/subscribe async bus with in-memory circular buffer (last 1000 events).
Foundation for future AI features (auto-tagging, smart search).

Usage:
    from .events import file_event_bus, FileEvent

    # publish
    await file_event_bus.publish(FileEvent(
        path="/srv/nas/shared/Photos/pic.jpg",
        action="upload",
        user="alice",
    ))

    # subscribe (async generator)
    async with file_event_bus.subscribe() as queue:
        async for event in queue:
            ...
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

logger = logging.getLogger("aihomecloud.events.file_bus")

_BUFFER_SIZE = 1000  # max events kept in the circular buffer


@dataclass
class FileEvent:
    """A single file-system action published to the event bus."""

    path: str        # absolute path on the NAS (e.g. /srv/nas/shared/Photos/pic.jpg)
    action: str      # "upload" | "delete" | "rename" | "mkdir"
    user: str        # username that triggered the action
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class FileEventBus:
    """
    Async publish/subscribe bus backed by an in-memory circular buffer.

    Subscribers receive all events published *after* they subscribe.
    The circular buffer holds the latest `_BUFFER_SIZE` events for inspection.
    """

    def __init__(self, maxlen: int = _BUFFER_SIZE) -> None:
        self._buffer: deque[FileEvent] = deque(maxlen=maxlen)
        self._queues: list[asyncio.Queue[FileEvent]] = []

    # ── Public API ────────────────────────────────────────────────────────────

    async def publish(self, event: FileEvent) -> None:
        """Store the event in the buffer and deliver to all subscribers."""
        self._buffer.append(event)
        dead: list[asyncio.Queue[FileEvent]] = []
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)
        logger.debug(
            "file_event published action=%s path=%s user=%s",
            event.action,
            event.path,
            event.user,
        )

    def recent(self, n: int | None = None) -> list[FileEvent]:
        """Return up to *n* most-recent events (all if n is None)."""
        events = list(self._buffer)
        return events[-n:] if n is not None else events

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[FileEvent]]:
        """
        Context manager that yields a per-subscriber asyncio.Queue.

        async with file_event_bus.subscribe() as q:
            event = await q.get()
        """
        q: asyncio.Queue[FileEvent] = asyncio.Queue(maxsize=200)
        self._queues.append(q)
        try:
            yield q
        finally:
            if q in self._queues:
                self._queues.remove(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    @property
    def buffer_len(self) -> int:
        return len(self._buffer)


# Module-level singleton
file_event_bus = FileEventBus()
