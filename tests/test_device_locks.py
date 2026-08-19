"""Tests for device_locks — serialising destructive storage operations per physical disk."""

import asyncio

import pytest

from app import device_locks
from app.device_locks import DeviceBusyError, base_disk, device_lock


@pytest.fixture(autouse=True)
def _clean():
    device_locks._reset_for_tests()
    yield
    device_locks._reset_for_tests()


# --- normalisation ---------------------------------------------------------

@pytest.mark.parametrize(
    "partition,disk",
    [
        ("/dev/sda1", "/dev/sda"),
        ("/dev/sdb12", "/dev/sdb"),
        ("/dev/nvme0n1p1", "/dev/nvme0n1"),
        ("/dev/nvme1n2p13", "/dev/nvme1n2"),
        ("/dev/mmcblk0p2", "/dev/mmcblk0"),
    ],
)
def test_partitions_normalise_to_their_parent_disk(partition, disk):
    assert base_disk(partition) == disk


@pytest.mark.parametrize(
    "disk",
    [
        "/dev/sda", "/dev/nvme0n1", "/dev/mmcblk0",
        # Whole disks whose names simply END in a digit. An earlier regex stripped that digit
        # and merged them onto one lock — /dev/loop0 and /dev/loop1 would have collided.
        "/dev/loop0", "/dev/loop7", "/dev/mmcblk1",
    ],
)
def test_a_whole_disk_is_left_alone(disk):
    assert base_disk(disk) == disk


def test_unrecognised_paths_pass_through_unchanged():
    # Under-merging only blocks less than it should; over-merging would defeat the lock or
    # block valid work, so anything unfamiliar is left as-is on purpose.
    for weird in ["", "/dev/mapper/vg-lv", "not-a-device", "/dev/loop0"]:
        assert base_disk(weird) == weird


# --- mutual exclusion ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_second_claim_on_the_same_disk_is_refused():
    await device_locks.acquire("/dev/sda", "format:job1")
    with pytest.raises(DeviceBusyError):
        await device_locks.acquire("/dev/sda", "format:job2")


@pytest.mark.asyncio
async def test_formatting_a_disk_blocks_operations_on_its_partitions():
    # The whole point of normalising: rewriting the partition table under a live filesystem
    # is the same conflict as two concurrent formats.
    await device_locks.acquire("/dev/sda", "format:job1")
    with pytest.raises(DeviceBusyError):
        await device_locks.acquire("/dev/sda1", "mount:req")


@pytest.mark.asyncio
async def test_different_disks_do_not_block_each_other():
    await device_locks.acquire("/dev/sda", "format:job1")
    await device_locks.acquire("/dev/nvme0n1", "format:job2")  # must not raise


@pytest.mark.asyncio
async def test_release_frees_the_disk():
    await device_locks.acquire("/dev/sda", "job1")
    await device_locks.release("/dev/sda")
    await device_locks.acquire("/dev/sda", "job2")  # must not raise


@pytest.mark.asyncio
async def test_releasing_something_not_held_is_harmless():
    await device_locks.release("/dev/sdz")
    await device_locks.release("/dev/sdz")


@pytest.mark.asyncio
async def test_the_error_names_the_current_holder():
    # An admin double-tapping should be told what is already running, not just "busy".
    await device_locks.acquire("/dev/sda", "format:job-abc")
    with pytest.raises(DeviceBusyError) as excinfo:
        await device_locks.acquire("/dev/sda", "format:job-def")
    assert "format:job-abc" in str(excinfo.value)


# --- context manager -------------------------------------------------------

@pytest.mark.asyncio
async def test_context_manager_releases_on_success():
    async with device_lock("/dev/sda", "job1"):
        assert await device_locks.holder_of("/dev/sda") == "job1"
    assert await device_locks.holder_of("/dev/sda") is None


@pytest.mark.asyncio
async def test_context_manager_releases_when_the_job_raises():
    # A crashed format must not leave the disk permanently unformattable — that failure would
    # be worse than the race being prevented.
    with pytest.raises(ValueError):
        async with device_lock("/dev/sda", "job1"):
            raise ValueError("mkfs blew up")
    assert await device_locks.holder_of("/dev/sda") is None


@pytest.mark.asyncio
async def test_context_manager_releases_when_the_job_is_cancelled():
    started = asyncio.Event()

    async def job():
        async with device_lock("/dev/sda", "job1"):
            started.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(job())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await device_locks.holder_of("/dev/sda") is None


@pytest.mark.asyncio
async def test_concurrent_claims_only_one_wins():
    # The real scenario: several requests arriving at once. Exactly one must proceed.
    results = await asyncio.gather(
        *[device_locks.acquire("/dev/sda", f"job{i}") for i in range(8)],
        return_exceptions=True,
    )
    granted = [r for r in results if r is None]
    refused = [r for r in results if isinstance(r, DeviceBusyError)]
    assert len(granted) == 1
    assert len(refused) == 7
