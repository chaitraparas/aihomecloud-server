"""
Job tracking for long-running operations.
Jobs are persisted to disk so their final status survives a backend restart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any
from uuid import uuid4

logger = logging.getLogger("aihomecloud.job_store")


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


@dataclass
class Job:
    id: str
    status: JobStatus
    started_at: datetime
    user_id: str = ""
    result: Any = None
    error: str | None = None
    progress: Any = None  # {"current": int, "total": int} for long-running jobs


_jobs: dict[str, Job] = {}
_loaded = False  # lazy-load flag


def _max_jobs() -> int:
    from .config import settings
    return settings.job_max_count


def _job_ttl() -> timedelta:
    from .config import settings
    return timedelta(hours=settings.job_ttl_hours)


# ── Persistence helpers ────────────────────────────────────────────────────────

def _jobs_file():
    from .config import settings
    return settings.jobs_file


def _load_jobs() -> None:
    """Load persisted jobs from disk. Running/pending jobs become failed (interrupted)."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    try:
        raw = json.loads(_jobs_file().read_text())
    except (OSError, json.JSONDecodeError):
        return

    now = datetime.now(timezone.utc)
    for entry in raw:
        try:
            started_at = datetime.fromisoformat(entry["started_at"])
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            status = JobStatus(entry["status"])
            # Jobs that were in-progress at shutdown did not complete — mark failed.
            if status in (JobStatus.pending, JobStatus.running):
                status = JobStatus.failed
                entry["error"] = "Backend restarted while job was in progress"
            job = Job(
                id=entry["id"],
                status=status,
                started_at=started_at,
                user_id=entry.get("user_id", ""),
                result=entry.get("result"),
                error=entry.get("error"),
            )
            # Skip jobs that have already expired.
            if (now - job.started_at) <= _job_ttl():
                _jobs[job.id] = job
        except (KeyError, ValueError):
            continue

    if _jobs:
        logger.info("job_store loaded %d persisted job(s) from disk", len(_jobs))


def _save_jobs() -> None:
    """Write terminal jobs to disk."""
    terminal = [
        j for j in _jobs.values()
        if j.status in (JobStatus.completed, JobStatus.failed)
    ]
    data = []
    for j in terminal:
        d = asdict(j)
        d["started_at"] = j.started_at.isoformat()
        d["status"] = j.status.value
        data.append(d)
    try:
        _jobs_file().write_text(json.dumps(data))
    except OSError as exc:
        logger.warning("Failed to persist job store: %s", exc)


# ── Public API ─────────────────────────────────────────────────────────────────

def _purge_old_jobs() -> None:
    """Remove completed/failed jobs older than job_ttl, keep at most job_max_count."""
    now = datetime.now(timezone.utc)
    ttl = _job_ttl()
    expired = [
        jid for jid, job in _jobs.items()
        if job.status in (JobStatus.completed, JobStatus.failed)
        and (now - job.started_at) > ttl
    ]
    for jid in expired:
        del _jobs[jid]
    max_jobs = _max_jobs()
    if len(_jobs) > max_jobs:
        finished = sorted(
            [(jid, j) for jid, j in _jobs.items()
             if j.status in (JobStatus.completed, JobStatus.failed)],
            key=lambda x: x[1].started_at,
        )
        for jid, _ in finished[:len(_jobs) - max_jobs]:
            del _jobs[jid]


def create_job(user_id: str = "") -> Job:
    _load_jobs()
    _purge_old_jobs()
    job = Job(
        id=uuid4().hex,
        status=JobStatus.running,
        started_at=datetime.now(timezone.utc),
        user_id=user_id,
    )
    _jobs[job.id] = job
    return job


def update_job(
    job_id: str,
    *,
    status: JobStatus | None = None,
    result: Any = None,
    error: str | None = None,
    progress: Any = None,
) -> Job | None:
    _load_jobs()
    job = _jobs.get(job_id)
    if not job:
        return None

    if status is not None:
        job.status = status
    if result is not None:
        job.result = result
    if error is not None:
        job.error = error
    if progress is not None:
        job.progress = progress

    # Persist whenever a job reaches a terminal state.
    if job.status in (JobStatus.completed, JobStatus.failed):
        _save_jobs()

    return job


def get_job(job_id: str) -> Job | None:
    _load_jobs()
    return _jobs.get(job_id)
