"""Continuous watcher to keep document_index in sync with NAS file changes."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .config import settings
from . import workload
from .document_index import (
    index_document,
    is_indexable_document_path,
    remove_document,
)

logger = logging.getLogger("aihomecloud.index_watcher")

# Thermal-adaptive OCR parallelism: up to OCR_MAX_WORKERS concurrent OCR tasks, but fall back to a
# single worker when the CPU is hot (>= OCR_TEMP_LIMIT_C) to protect the SBC.
OCR_MAX_WORKERS = 3
OCR_TEMP_LIMIT_C = 75.0
_thermal_zone_path: "str | None" = None


def _read_cpu_temp_c() -> "float | None":
    """Best-effort CPU temperature in °C from the board's thermal zone; None if unavailable."""
    global _thermal_zone_path
    try:
        if _thermal_zone_path is None:
            from .board import find_thermal_zone
            _thermal_zone_path = find_thermal_zone() or ""
        if _thermal_zone_path:
            with open(_thermal_zone_path) as f:
                return int(f.read().strip()) / 1000.0
    except Exception:
        return None
    return None

# Polling interval in seconds. Keep moderate to reduce IO on ARM hardware.
_DEFAULT_INTERVAL_SECONDS = 20

# path -> (mtime_ns, size_bytes)
FileSignature = tuple[int, int]
StateMap = dict[str, FileSignature]


def _iter_document_roots() -> list[Path]:
    """Return all roots to watch: full personal user dirs, family, and entertainment.

    We index the entire user folder (not just Documents/) so that files dumped
    anywhere — Photos/, Videos/, root of personal folder, etc. — are discovered
    and their content made searchable.
    """
    roots: list[Path] = []

    personal_base = settings.personal_path
    if personal_base.is_dir():
        for user_dir in personal_base.iterdir():
            if user_dir.is_dir() and not user_dir.name.startswith("."):
                roots.append(user_dir)

    if settings.family_path.is_dir():
        roots.append(settings.family_path)

    if settings.entertainment_path.is_dir():
        roots.append(settings.entertainment_path)

    return roots


def _added_by_for_path(path: Path) -> str:
    """Infer added_by from NAS path: personal/<user>/... or shared."""
    try:
        rel = path.resolve().relative_to(settings.nas_root.resolve())
    except ValueError:
        return "system"

    parts = rel.parts
    if len(parts) >= 2 and parts[0] == settings.personal_base:
        return parts[1]
    if len(parts) >= 1 and parts[0] == settings.family_dir:
        return "family"
    return "system"


def _scan_documents_sync() -> StateMap:
    """Scan all watched Documents trees and return current signatures."""
    state: StateMap = {}
    for root in _iter_document_roots():
        try:
            entries = root.rglob("*")
        except OSError:
            continue

        for p in entries:
            try:
                if not is_indexable_document_path(p):
                    continue
                st = p.stat()
                state[str(p.resolve())] = (int(st.st_mtime_ns), int(st.st_size))
            except OSError:
                continue

    return state


def _diff_states(previous: StateMap, current: StateMap) -> tuple[list[str], list[str]]:
    """Return (changed_or_added_abs_paths, removed_abs_paths)."""
    changed_or_added = [path for path, sig in current.items() if previous.get(path) != sig]
    removed = [path for path in previous if path not in current]
    return changed_or_added, removed


async def sync_once(previous_state: StateMap | None, on_progress=None, should_cancel=None, redo_paths=None) -> StateMap:
    """Run one reconciliation pass and return the new state.

    on_progress(current, total) is invoked (throttled) while indexing, for progress UIs.
    """
    loop = asyncio.get_running_loop()
    current_state = await loop.run_in_executor(None, _scan_documents_sync)

    prev = previous_state or {}
    changed_or_added, removed = _diff_states(prev, current_state)

    # Remove stale index rows first to avoid duplicate paths on rename/move.
    for abs_path in removed:
        await remove_document(abs_path)

    # Index new/changed files with thermal-adaptive parallel OCR (fall back to 1 worker when hot).
    # redo_paths forces re-indexing of specific files even if unchanged (e.g. entries with empty OCR).
    to_index = set(changed_or_added)
    if redo_paths:
        to_index |= {p for p in redo_paths if p in current_state}
    pending = list(to_index)
    total = len(pending)
    if on_progress is not None:
        on_progress(0, total)

    async def _index_one(abs_path: str) -> None:
        p = Path(abs_path)
        await index_document(str(p), p.name, _added_by_for_path(p))

    done = 0
    pos = 0
    while pos < total:
        if should_cancel is not None and should_cancel():
            break
        temp = _read_cpu_temp_c()
        workers = 1 if (temp is not None and temp >= OCR_TEMP_LIMIT_C) else OCR_MAX_WORKERS
        batch = pending[pos:pos + workers]
        await asyncio.gather(*[_index_one(ap) for ap in batch])
        pos += len(batch)
        done += len(batch)
        if on_progress is not None:
            on_progress(done, total)

    if changed_or_added or removed:
        logger.info(
            "index_watcher_sync changed_or_added=%d removed=%d",
            len(changed_or_added),
            len(removed),
        )

    return current_state


def _load_persisted_state() -> StateMap | None:
    """Load previously persisted watcher state from disk, or return None."""
    path = settings.index_watcher_state_file
    try:
        raw = json.loads(path.read_text())
        # Stored as {abs_path: [mtime_ns, size_bytes]}
        return {k: tuple(v) for k, v in raw.items()}  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _save_persisted_state(state: StateMap) -> None:
    """Persist watcher state snapshot to disk."""
    path = settings.index_watcher_state_file
    try:
        path.write_text(json.dumps(state))
    except OSError as exc:
        logger.warning("Failed to persist index watcher state: %s", exc)


class DocumentIndexWatcher:
    """Background task that continuously syncs file changes into doc index."""

    def __init__(self, interval_seconds: int = _DEFAULT_INTERVAL_SECONDS) -> None:
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._state: StateMap | None = None

    def start(self) -> None:
        # Restore state from previous run to skip full re-index on startup.
        self._state = _load_persisted_state()
        if self._state is not None:
            logger.info("DocumentIndexWatcher restored %d entries from disk", len(self._state))
        self._task = asyncio.create_task(self._loop(), name="document_index_watcher")
        logger.info("DocumentIndexWatcher started interval=%ds", self._interval_seconds)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        # Persist current state so next startup can skip unchanged files.
        if self._state is not None:
            _save_persisted_state(self._state)
        logger.info("DocumentIndexWatcher stopped")

    async def _loop(self) -> None:
        while True:
            await workload.gate("document_index_sweep", max_wait=24 * 60 * 60)
            try:
                self._state = await sync_once(self._state)
                # Persist state after each successful sync.
                if self._state is not None:
                    _save_persisted_state(self._state)
            except Exception as exc:
                logger.error("DocumentIndexWatcher pass error: %s", exc)
            await asyncio.sleep(self._interval_seconds)


def _make_watcher() -> DocumentIndexWatcher:
    return DocumentIndexWatcher(interval_seconds=settings.document_index_interval)


_watcher = _make_watcher()


def get_index_watcher() -> DocumentIndexWatcher:
    return _watcher
