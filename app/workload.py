"""
User activity always wins. Background work yields.

The board serves a family. When someone is uploading a folder, streaming a film over SMB or
scrolling their photos, that has to feel fast — and the machine must not be busy OCR-ing the very
files it is still receiving. Today it is: every upload fires a `tesseract` subprocess immediately,
so a bulk sync competes against its own indexing. On the Cubie A5E (937 MB, 546 MB available and
already touching swap at idle) that is the difference between 10 MB/s and 4.5 MB/s.

**The rule:** anything a person is waiting for runs immediately and is never throttled. Anything the
system merely wants to do eventually — OCR, embeddings, thumbnails, duplicate scans, reconciliation
— waits for the house to go quiet.

Modelled on Immich's `pause-immich-jobs`, which solves the same problem for self-hosted photo
backup. **Deliberately avoiding its known flaw:** that flag pauses *every* queue indiscriminately,
including notification and bookkeeping work that costs nothing, which users complained about. Here
work is classified by cost, and only the expensive class defers.

Nothing in here blocks the event loop, and a background job that forgets to call `gate()` simply
behaves as it does today — this is opt-in, not a trap.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field

from .config import settings

logger = logging.getLogger("aihomecloud.workload")


class Cost(enum.Enum):
    """
    What a job costs, which is what decides whether it may be deferred.

    Not a priority number: the question is never "how important" but "does a person notice if this
    waits". Bookkeeping is *unimportant* and still runs immediately, because deferring it saves
    nothing — that is precisely the distinction Immich's blanket pause misses.
    """

    #: A person is waiting. Never deferred, never throttled.
    INTERACTIVE = "interactive"
    #: Cheap and non-blocking — audit rows, notifications, flag flips. Runs immediately.
    BOOKKEEPING = "bookkeeping"
    #: Expensive: OCR, embeddings, thumbnails, hashing sweeps, duplicate scans. Defers.
    OPPORTUNISTIC = "opportunistic"


@dataclass
class _State:
    last_user_activity: float = 0.0
    in_flight: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle_event: asyncio.Event = field(default_factory=asyncio.Event)


_state = _State()
_state.idle_event.set()


def _quiet_seconds() -> float:
    """
    How long after the last user request the house counts as quiet.

    Longer on a memory-tight board: there, the cost of guessing wrong is a background job competing
    for RAM that is already in swap, which is far more expensive than simply waiting.
    """
    base = float(getattr(settings, "workload_quiet_seconds", 5.0))
    return base * 2 if _is_memory_tight() else base


#: How long a memory reading stays good for. Deliberately not zero: this is consulted from the
#: gate's poll loop, and every waiting job would otherwise re-read /proc on every tick.
_MEM_CACHE_SECONDS = 15.0
_mem_cache: tuple[float, bool] = (0.0, False)


def _is_memory_tight() -> bool:
    """
    True when this board has little headroom. **Cached**, and that is not an optimisation detail.

    `MemAvailable` is the kernel's own estimate of what can be allocated without swapping, which is
    exactly the question being asked. Unreadable (non-Linux, container) means "assume roomy" — this
    is an optimisation, and failing it closed would throttle boards that never needed it.

    **Why the cache exists:** this is reached from `is_user_active()`, which every deferred job calls
    on every poll tick. Uncached, a bulk upload with 40 waiting OCR jobs meant ~40 synchronous
    `/proc/meminfo` reads per second *on the event loop* — measured on the Cubie, that alone halved
    upload throughput and made this module slower than the problem it was written to fix. Memory
    pressure does not meaningfully change second to second; re-reading it that often bought nothing
    and cost everything.
    """
    global _mem_cache
    now = time.monotonic()
    cached_at, value = _mem_cache
    if now - cached_at < _MEM_CACHE_SECONDS:
        return value

    threshold_mb = int(getattr(settings, "workload_low_memory_mb", 800))
    result = False
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    result = int(line.split()[1]) / 1024 < threshold_mb
                    break
    except (OSError, ValueError, IndexError):
        result = False
    _mem_cache = (now, result)
    return result


def mark_user_active() -> None:
    """
    Record that a person is doing something. Called from the request middleware.

    Deliberately cheap — a float write on the hot path of every user request.
    """
    _state.last_user_activity = time.monotonic()
    if _state.idle_event.is_set():
        _state.idle_event.clear()


def is_user_active() -> bool:
    """Someone is mid-request, or was recently enough that the board should stay out of the way."""
    if _state.in_flight > 0:
        return True
    return (time.monotonic() - _state.last_user_activity) < _quiet_seconds()


class track:
    """
    Async context manager marking a genuinely user-facing operation for its whole duration.

    A long upload or a film streaming over SMB is one request that lasts minutes; the timestamp
    alone would go stale halfway through and let background work start underneath it. `in_flight`
    keeps the house "busy" until the last such operation finishes.
    """

    async def __aenter__(self) -> "track":
        async with _state.lock:
            _state.in_flight += 1
            _state.idle_event.clear()
        mark_user_active()
        return self

    async def __aexit__(self, *exc) -> None:
        async with _state.lock:
            _state.in_flight = max(0, _state.in_flight - 1)
            if _state.in_flight == 0:
                mark_user_active()  # start the quiet countdown from *now*, not from the request start
        return None


async def gate(job: str, *, cost: Cost = Cost.OPPORTUNISTIC, poll: float = 1.0,
               max_wait: float | None = None) -> float:
    """
    Wait here until the family is done. Returns how long it waited, in seconds.

    Call between units of work, not once at the start — a job that checked an hour ago and is still
    grinding is exactly the problem. The semantic indexer's per-batch thermal pause is the right
    shape to copy.

    `max_wait` bounds the deferral so a permanently busy household cannot starve a job forever;
    `None` means wait as long as it takes, which is right for work nobody is waiting on.
    """
    if cost is not Cost.OPPORTUNISTIC:
        return 0.0
    if not getattr(settings, "workload_yield_enabled", True):
        return 0.0

    started = time.monotonic()
    logged = False
    while is_user_active():
        waited = time.monotonic() - started
        if max_wait is not None and waited >= max_wait:
            logger.info("workload_gate_timeout job=%s waited=%.1fs — proceeding anyway", job, waited)
            break
        if not logged and waited > poll:
            logger.info("workload_deferring job=%s — user activity in progress", job)
            logged = True
        await asyncio.sleep(poll)

    waited = time.monotonic() - started
    if waited > 0.5:
        logger.info("workload_resumed job=%s after=%.1fs", job, waited)
    return waited


def gate_sync(job: str, *, cost: Cost = Cost.OPPORTUNISTIC, poll: float = 1.0,
              max_wait: float | None = None) -> float:
    """
    Blocking twin of [gate], for workers that run in a thread rather than on the event loop.

    The semantic indexer's batch loops are plain `def` executed in a background thread — `await`
    is not available there, and `time.sleep` is correct precisely *because* it is not the event
    loop being blocked. Using the async version there is a syntax error; using this one on the
    loop would freeze the server, so the two are deliberately named differently.
    """
    if cost is not Cost.OPPORTUNISTIC:
        return 0.0
    if not getattr(settings, "workload_yield_enabled", True):
        return 0.0

    started = time.monotonic()
    logged = False
    while is_user_active():
        waited = time.monotonic() - started
        if max_wait is not None and waited >= max_wait:
            logger.info("workload_gate_timeout job=%s waited=%.1fs — proceeding anyway", job, waited)
            break
        if not logged and waited > poll:
            logger.info("workload_deferring job=%s — user activity in progress", job)
            logged = True
        time.sleep(poll)

    waited = time.monotonic() - started
    if waited > 0.5:
        logger.info("workload_resumed job=%s after=%.1fs", job, waited)
    return waited


def snapshot() -> dict:
    """For diagnostics and the status endpoint — why is nothing happening right now?"""
    return {
        "userActive": is_user_active(),
        "inFlight": _state.in_flight,
        "secondsSinceActivity": round(time.monotonic() - _state.last_user_activity, 1),
        "quietSeconds": _quiet_seconds(),
        "memoryTight": _is_memory_tight(),
        "yieldEnabled": bool(getattr(settings, "workload_yield_enabled", True)),
    }


def _reset_for_tests() -> None:
    global _mem_cache
    _state.last_user_activity = 0.0
    _state.in_flight = 0
    _state.idle_event.set()
    _mem_cache = (0.0, False)
