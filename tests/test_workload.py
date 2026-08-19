"""
User activity preempts background work.

The failure this guards against is the inverse of the obvious one: not "background work never
runs", but "background work runs at the worst possible moment" — OCR-ing files while the board is
still receiving them. Both directions are tested, because a gate that never opens is just as broken
as one that never closes.
"""

import asyncio
import time

import pytest

from app import workload
from app.workload import Cost


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    workload._reset_for_tests()
    monkeypatch.setattr(workload.settings, "workload_quiet_seconds", 0.2, raising=False)
    monkeypatch.setattr(workload.settings, "workload_yield_enabled", True, raising=False)
    # Pin the memory verdict so these tests do not depend on the machine running them.
    monkeypatch.setattr(workload, "_is_memory_tight", lambda: False)
    yield
    workload._reset_for_tests()


class TestActivityDetection:
    def test_a_fresh_board_is_idle(self):
        assert workload.is_user_active() is False

    def test_marking_activity_makes_it_busy(self):
        workload.mark_user_active()

        assert workload.is_user_active() is True

    async def test_activity_expires_after_the_quiet_period(self):
        workload.mark_user_active()

        await asyncio.sleep(0.3)  # quiet_seconds is 0.2 here

        assert workload.is_user_active() is False

    async def test_a_tracked_operation_keeps_the_board_busy_while_it_runs(self):
        """A long upload is one request lasting minutes — the timestamp alone would go stale."""
        async with workload.track():
            await asyncio.sleep(0.3)  # longer than the quiet period
            assert workload.is_user_active() is True

    async def test_the_quiet_countdown_starts_when_the_operation_ends(self):
        async with workload.track():
            pass

        assert workload.is_user_active() is True, "countdown starts at release, not at entry"
        await asyncio.sleep(0.3)
        assert workload.is_user_active() is False

    async def test_nested_operations_stay_busy_until_the_last_one_finishes(self):
        outer = workload.track()
        inner = workload.track()
        await outer.__aenter__()
        await inner.__aenter__()

        await inner.__aexit__(None, None, None)
        assert workload.is_user_active() is True, "one is still in flight"

        await outer.__aexit__(None, None, None)
        await asyncio.sleep(0.3)
        assert workload.is_user_active() is False


class TestGate:
    async def test_an_idle_board_does_not_delay_background_work(self):
        waited = await workload.gate("job", poll=0.05)

        assert waited < 0.1

    async def test_expensive_work_waits_for_the_user(self):
        workload.mark_user_active()

        started = time.monotonic()
        await workload.gate("ocr", poll=0.05)
        elapsed = time.monotonic() - started

        assert elapsed >= 0.15, "should have waited out the quiet period"

    async def test_cheap_work_is_never_deferred(self):
        """
        Immich's blanket pause stalls notification and bookkeeping queues too, which users
        complained about. Deferring work that costs nothing buys nothing.
        """
        workload.mark_user_active()

        waited = await workload.gate("audit", cost=Cost.BOOKKEEPING, poll=0.05)

        assert waited == 0.0

    async def test_interactive_work_is_never_deferred(self):
        workload.mark_user_active()

        assert await workload.gate("thumb-for-open-file", cost=Cost.INTERACTIVE, poll=0.05) == 0.0

    async def test_max_wait_stops_a_busy_household_starving_a_job(self):
        """A house that is never quiet must still get its documents indexed eventually."""
        async def keep_busy():
            for _ in range(20):
                workload.mark_user_active()
                await asyncio.sleep(0.05)

        noise = asyncio.create_task(keep_busy())
        started = time.monotonic()
        await workload.gate("ocr", poll=0.05, max_wait=0.2)
        elapsed = time.monotonic() - started
        noise.cancel()

        assert elapsed < 0.6, "must give up waiting and proceed"

    async def test_the_feature_can_be_switched_off(self, monkeypatch):
        monkeypatch.setattr(workload.settings, "workload_yield_enabled", False, raising=False)
        workload.mark_user_active()

        assert await workload.gate("ocr", poll=0.05) == 0.0


class TestMemoryAwareness:
    def test_a_tight_board_stays_out_of_the_way_for_longer(self, monkeypatch):
        monkeypatch.setattr(workload, "_is_memory_tight", lambda: True)

        assert workload._quiet_seconds() == pytest.approx(0.4)

    def test_an_unreadable_meminfo_assumes_roomy(self, monkeypatch):
        """This is an optimisation; failing it closed would throttle boards that never needed it."""
        def boom(*a, **k):
            raise OSError("no /proc here")

        monkeypatch.setattr("builtins.open", boom)

        assert workload._is_memory_tight() is False


class TestSnapshot:
    def test_snapshot_explains_why_nothing_is_happening(self):
        workload.mark_user_active()

        snap = workload.snapshot()

        assert snap["userActive"] is True
        assert "secondsSinceActivity" in snap
        assert "memoryTight" in snap
