"""
Subprocess runner and job store tests.
"""

import sys
import pytest


@pytest.mark.asyncio
async def test_run_command_basic():
    """Run a simple command and get output."""
    from app.subprocess_runner import run_command

    rc, out, err = await run_command([sys.executable, "-c", "print('hello')"])
    assert rc == 0
    assert "hello" in out


@pytest.mark.asyncio
async def test_run_command_nonexistent():
    """Running a nonexistent command returns -1."""
    from app.subprocess_runner import run_command

    rc, out, err = await run_command(["nonexistent_binary_xyz"])
    assert rc == -1
    assert err == "not_found"


@pytest.mark.asyncio
async def test_run_command_rejects_empty():
    """Empty command list raises ValueError."""
    from app.subprocess_runner import run_command

    with pytest.raises(ValueError, match="non-empty list"):
        await run_command([])


@pytest.mark.asyncio
@pytest.mark.security
async def test_run_command_shell_metacharacters_are_never_shell_interpreted():
    """M6: run_command must never invoke a shell, so tokens containing shell metacharacters
    (e.g. a Wi-Fi password with a `$`, or any arg an admin/user legitimately supplies) are
    passed to the target program as a single literal argv entry, never parsed as shell syntax —
    verified by round-tripping each token through a subprocess that echoes argv[1] back exactly."""
    from app.subprocess_runner import run_command

    for token in ["echo;rm", "cat|grep", "$(whoami)", "test`id`", "a&b"]:
        rc, out, err = await run_command(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", token]
        )
        assert rc == 0
        assert out == token


@pytest.mark.asyncio
async def test_run_command_timeout():
    """Command that exceeds timeout returns -1 with 'timeout'."""
    from app.subprocess_runner import run_command

    rc, out, err = await run_command(
        [sys.executable, "-c", "import time\ntime.sleep(10)"], timeout=1
    )
    assert rc == -1
    assert err == "timeout"


@pytest.mark.asyncio
async def test_run_command_stderr():
    """Command that writes to stderr captures it."""
    from app.subprocess_runner import run_command

    rc, out, err = await run_command(
        [sys.executable, "-c", "import sys\nsys.stderr.write('oops')\nsys.exit(1)"]
    )
    assert rc != 0
    assert err  # stderr should contain error message


# ─── Job store ───────────────────────────────────────────────────────────────

def test_job_store_create_and_get():
    """Create a job and retrieve it."""
    from app.job_store import create_job, get_job, JobStatus

    job = create_job()
    assert job.status == JobStatus.running

    fetched = get_job(job.id)
    assert fetched is not None
    assert fetched.id == job.id


def test_job_store_update():
    """Update job status."""
    from app.job_store import create_job, update_job, get_job, JobStatus

    job = create_job()
    update_job(job.id, status=JobStatus.running)
    assert get_job(job.id).status == JobStatus.running

    update_job(job.id, status=JobStatus.completed, result={"ok": True})
    fetched = get_job(job.id)
    assert fetched.status == JobStatus.completed
    assert fetched.result == {"ok": True}


def test_job_store_update_nonexistent():
    """Updating a nonexistent job returns None."""
    from app.job_store import update_job, JobStatus

    result = update_job("nonexistent_job_id", status=JobStatus.failed)
    assert result is None


def test_job_store_get_nonexistent():
    """Getting a nonexistent job returns None."""
    from app.job_store import get_job

    assert get_job("no_such_job") is None


def test_job_store_cleanup():
    """Old completed jobs should be cleaned up automatically."""
    from datetime import datetime, timezone, timedelta
    from app.job_store import _jobs, create_job, update_job, JobStatus, _purge_old_jobs, Job

    # Add an old completed job
    old_job = Job(
        id="old_test_job",
        status=JobStatus.completed,
        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    _jobs["old_test_job"] = old_job

    # Purge should remove it
    _purge_old_jobs()
    assert "old_test_job" not in _jobs
