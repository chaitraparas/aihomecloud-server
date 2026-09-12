"""
Auto Backup routes — phone-to-NAS background backup.

Endpoints:
  POST   /api/v1/backup/check-duplicate
  POST   /api/v1/backup/record-hash
  GET    /api/v1/backup/status
  POST   /api/v1/backup/jobs
  DELETE /api/v1/backup/jobs/{job_id}
  POST   /api/v1/backup/jobs/{job_id}/report
  POST   /api/v1/backup/notify

Hash records live in kv.json under key "backup_file_hashes".
Job configs live in kv.json under key "backup_jobs".
"""

import html
import logging
import urllib.parse
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, field_validator

from ..auth import get_current_user, require_admin
from ..ingest import _resolve_identity
from ..config import settings
from .. import store

logger = logging.getLogger("aihomecloud.backup")

router = APIRouter(prefix="/api/v1/backup", tags=["backup"])

_MAX_HASHES = 50_000
_MAX_JOBS = 20
_VALID_DESTINATIONS = frozenset({"personal", "family", "entertainment"})


# ── Request models ─────────────────────────────────────────────────────────────

class DuplicateCheckRequest(BaseModel):
    sha256: str
    filename: str

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, v: str) -> str:
        v = v.lower()
        if len(v) != 64 or not all(c in "0123456789abcdef" for c in v):
            raise ValueError("sha256 must be a 64-character hex string")
        return v


class RecordHashRequest(BaseModel):
    sha256: str
    filename: str
    destination: str

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, v: str) -> str:
        v = v.lower()
        if len(v) != 64 or not all(c in "0123456789abcdef" for c in v):
            raise ValueError("sha256 must be a 64-character hex string")
        return v

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, v: str) -> str:
        if v not in _VALID_DESTINATIONS:
            raise ValueError("destination must be personal or family")
        return v


class CreateJobRequest(BaseModel):
    phoneFolder: str
    destination: str

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, v: str) -> str:
        if v not in _VALID_DESTINATIONS:
            raise ValueError("destination must be personal or family")
        return v

    @field_validator("phoneFolder")
    @classmethod
    def validate_phone_folder(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("phoneFolder must not be empty")
        return v


class SyncReportRequest(BaseModel):
    uploaded: int
    skipped: int
    lastSyncAt: str

    @field_validator("uploaded", "skipped")
    @classmethod
    def validate_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("counts must be non-negative")
        return v


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/check-duplicate")
async def check_duplicate(
    req: DuplicateCheckRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Check whether a SHA-256 hash already exists in the *caller's own* backup hash store.

    H-4 fix (security audit 2026-08): the store used to be one flat sha256->record map shared
    by every family member, so this endpoint doubled as a hash oracle — anyone who could compute
    or already knew the hash of a sensitive file could probe whether *another* member had ever
    backed it up, with no relation to their own backup activity. Scoped per-user, same ownership
    model already used for backup_jobs/_owns_job above: a hit now only ever means I've already
    uploaded this, never that someone else in the family has."""
    all_hashes = await store.get_value("backup_file_hashes", default={})
    user_hashes = all_hashes.get(user.get("sub", ""), {})
    return {"exists": req.sha256 in user_hashes}


@router.post("/record-hash", status_code=status.HTTP_200_OK)
async def record_hash(
    req: RecordHashRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Persist a SHA-256 → file mapping, scoped to the caller (see check_duplicate's H-4 note)."""
    user_id = user.get("sub", "")

    def _add(all_hashes: dict) -> dict:
        user_hashes = all_hashes.setdefault(user_id, {})
        user_hashes[req.sha256] = {
            "filename": req.filename,
            "destination": req.destination,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        if len(user_hashes) > _MAX_HASHES:
            # Evict oldest entries to stay within the per-user limit
            oldest = sorted(user_hashes, key=lambda k: user_hashes[k].get("saved_at", ""))
            for k in oldest[: len(user_hashes) - _MAX_HASHES]:
                del user_hashes[k]
        return all_hashes

    await store.atomic_update("backup_file_hashes", _add, default={})
    return {"ok": True}


def _owns_job(job: dict, user_id: str, is_admin: bool) -> bool:
    """A job created before this ownership fix has no "ownerId" field at all — treat those
    legacy rows as admin-only rather than open-to-everyone (the previous de facto behavior),
    the safer default for a family NAS where "unowned" shouldn't mean "anyone's"."""
    owner_id = job.get("ownerId")
    if not owner_id:
        return is_admin
    return is_admin or owner_id == user_id


@router.get("/status")
async def get_backup_status(
    user: dict = Depends(get_current_user),
) -> dict:
    """Return the current backup configuration and per-job stats — scoped to the caller's own
    jobs (or all jobs, for an admin). Each phoneFolder->destination mapping is a per-device
    backup config, not a shared/family-wide setting; before this fix every job dict had no
    owner field at all, so any authenticated family member saw and could tamper with every
    other member's job list. Found live 2026-07-30 via Stage 3's dynamic authorization sweep."""
    from ..auth import is_currently_admin

    all_jobs = await store.get_value("backup_jobs", default=[])
    is_admin = await is_currently_admin(user)
    user_id = user.get("sub", "")
    jobs = [j for j in all_jobs if _owns_job(j, user_id, is_admin)]
    return {
        "enabled": bool(jobs),
        "jobs": jobs,
    }


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def create_backup_job(
    req: CreateJobRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Create a new backup job configuration and persist it.

    Under store.atomic_update's single lock acquisition -- a get_value()+set_value() pair
    (the previous shape) would let two devices setting up a backup job at the same moment
    each read the same jobs list and each append their own job to it; whichever set_value ran
    last would silently discard the other device's new job.
    """
    created: dict = {}

    def _create(jobs: list) -> list:
        if len(jobs) >= _MAX_JOBS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Maximum number of backup jobs reached",
            )
        job: dict = {
            "id": uuid.uuid4().hex[:12],
            "ownerId": user.get("sub", ""),
            "phoneFolder": req.phoneFolder,
            "destination": req.destination,
            "lastSyncAt": None,
            "totalUploaded": 0,
            "totalSkipped": 0,
        }
        created.update(job)
        return jobs + [job]

    await store.atomic_update("backup_jobs", _create, default=[])
    return created


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backup_job(
    job_id: str,
    user: dict = Depends(get_current_user),
) -> None:
    """Remove a backup job configuration by ID. Owner (or admin) only — see _owns_job.

    See create_backup_job's docstring: a get_value()+set_value() pair here would let a
    concurrent create/report on a different job silently be discarded by this delete's write.
    """
    from ..auth import is_currently_admin

    is_admin = await is_currently_admin(user)
    user_id = user.get("sub", "")

    def _delete(jobs: list) -> list:
        target = next((j for j in jobs if j.get("id") == job_id), None)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Backup job not found")
        if not _owns_job(target, user_id, is_admin):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot delete another user's backup job")
        return [j for j in jobs if j.get("id") != job_id]

    await store.atomic_update("backup_jobs", _delete, default=[])


@router.post("/jobs/{job_id}/report")
async def report_sync_run(
    job_id: str,
    req: SyncReportRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Update a job's stats after a completed sync run. Owner (or admin) only — see _owns_job.

    Two phones syncing at the same moment (even different jobs) is the exact race this used
    to hit: each read the same jobs list, each incremented their own target's counters against
    that shared snapshot, and whichever set_value ran last overwrote the other's counters --
    not merged. See create_backup_job's docstring for the general pattern.
    """
    from ..auth import is_currently_admin

    is_admin = await is_currently_admin(user)
    user_id = user.get("sub", "")
    reported: dict = {}

    def _report(jobs: list) -> list:
        target = next((j for j in jobs if j.get("id") == job_id), None)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Backup job not found")
        if not _owns_job(target, user_id, is_admin):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot report sync for another user's backup job")
        target["totalUploaded"] = target.get("totalUploaded", 0) + req.uploaded
        target["totalSkipped"] = target.get("totalSkipped", 0) + req.skipped
        target["lastSyncAt"] = req.lastSyncAt
        reported.update(target)
        return jobs

    await store.atomic_update("backup_jobs", _report, default=[])
    return reported


# ── Telegram backup notification ─────────────────────────────────────────────

class BackupNotifyRequest(BaseModel):
    success: bool
    uploaded: int = 0
    skipped: int = 0
    folders: int = 0
    error_message: str = ""


@router.post("/notify")
async def send_backup_notification(
    req: BackupNotifyRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Send a backup summary or failure notification via the Telegram bot."""
    try:
        from .. import telegram_bot as tb
    except ImportError:
        return {"sent": False, "reason": "telegram_not_available"}

    # Don't send a message when a successful run processed nothing.
    # The runner only calls /notify when it has something meaningful to report.
    if req.success and req.uploaded == 0 and req.skipped == 0:
        return {"sent": False, "reason": "nothing_to_notify"}

    if tb._application is None:
        return {"sent": False, "reason": "telegram_not_configured"}

    linked_ids = await tb._get_linked_ids()
    if not linked_ids:
        return {"sent": False, "reason": "no_linked_users"}

    if req.success:
        lines = ["✅ <b>Backup complete</b>\n"]
        lines.append(
            f"📁 {req.folders} folder{'s' if req.folders != 1 else ''} checked"
        )
        if req.uploaded > 0:
            lines.append(
                f"⬆️ {req.uploaded} file{'s' if req.uploaded != 1 else ''} uploaded"
            )
        if req.skipped > 0:
            lines.append(f"⏭ {req.skipped} already synced")
        if req.uploaded == 0 and req.skipped == 0:
            lines.append("✨ Everything is up to date")
        msg = "\n".join(lines)
    else:
        # M-3 fix (security audit 2026-08): error_message is caller-supplied free text,
        # broadcast unescaped to every linked family member's Telegram chat via parse_mode
        # "HTML" — a crafted <a href=...> here rendered as a real, clickable link.
        error_text = html.escape(req.error_message) if req.error_message else "Unknown error"
        msg = f"❌ <b>Backup failed</b>\n\n{error_text}"

    sent = 0
    for chat_id in linked_ids:
        with suppress(Exception):
            await tb._application.bot.send_message(
                chat_id=chat_id, text=msg, parse_mode="HTML"
            )
            sent += 1

    return {"sent": True, "recipients": sent}


# ── Duplicate scanner endpoints ───────────────────────────────────────────────

def _resolve_dup_path(encoded_path: str) -> Path:
    """URL-decode *encoded_path* and resolve it safely within nas_root."""
    raw = urllib.parse.unquote(encoded_path)
    nas_root = settings.nas_root.resolve()
    if Path(raw).is_absolute():
        candidate = Path(raw).resolve()
    else:
        candidate = (settings.nas_root / raw.lstrip("/")).resolve()
    # is_relative_to, not a string prefix. `str(candidate).startswith(str(nas_root))` was the
    # original check and it is not a containment test: "/srv/nas" is a prefix of "/srv/nasty", so
    # a raw path of "../nasty/secrets" resolves to /srv/nasty/secrets and passes. .resolve() above
    # has already collapsed the "..", so the only thing standing between that and a read outside
    # the NAS root was the comparison — and it was the wrong one. The rest of this codebase already
    # uses is_relative_to in ~59 places; these two functions were the outliers.
    # (2026-08-08 audit, adversarial pass.)
    if not candidate.is_relative_to(nas_root):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path outside NAS root")
    return candidate


def _visible_duplicate_sets(results: list, name: str, is_admin: bool) -> list:
    """
    Drop any duplicate set that names a path outside the caller's reach.

    The scan runs fleet-wide and its results carry full paths and owners, so returning them intact
    told any authenticated member exactly what files other members hold and where — the private
    scopes this product promises, disclosed through a housekeeping endpoint. A set is only shown if
    every path in it is the caller's own or shared; partially-redacting a set would still leak that
    a matching file exists elsewhere.
    """
    if is_admin:
        return results
    mine = f"/personal/{(name or '').lower()}/"

    def reachable(p: str) -> bool:
        low = str(p or "").lower()
        return low.startswith(mine) or not low.startswith("/personal/")

    visible = []
    for entry in results or []:
        paths = entry.get("paths") or entry.get("files") or []
        if paths and all(reachable(p) for p in paths):
            visible.append(entry)
    return visible


@router.get("/duplicates")
async def get_duplicate_results(
    user: dict = Depends(get_current_user),
) -> dict:
    """The last duplicate scan, filtered to what this caller may see."""
    results = await store.get_value("duplicate_scan_results", default=[])
    ran_at = await store.get_value("duplicate_scan_ran_at", default=None)
    name, is_admin = await _resolve_identity(user)
    visible = _visible_duplicate_sets(results, name, is_admin)
    return {"results": visible, "ranAt": ran_at, "count": len(visible)}


@router.post("/duplicates/scan", status_code=status.HTTP_202_ACCEPTED)
async def trigger_duplicate_scan(
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_admin),
) -> dict:
    """Trigger an immediate duplicate scan (admin only).  Runs in background."""
    from ..duplicate_scanner import get_duplicate_scanner
    scanner = get_duplicate_scanner()
    if scanner.is_scanning:
        return {"status": "already_scanning"}
    background_tasks.add_task(scanner._scan_nas_for_duplicates)
    return {"status": "scanning"}


@router.delete("/duplicates/{encoded_path:path}", status_code=status.HTTP_200_OK)
async def delete_duplicate_file(
    encoded_path: str,
    user: dict = Depends(require_admin),
) -> dict:
    """Delete one file from a duplicate set (soft-delete — moves to trash) and update stored
    results. Previously called resolved.unlink() directly: permanent, no trash/undo, and only
    best-effort updated the document index — media_index kept listing the file until the next
    reconcile pass (showing a broken tile whose content now 404s), and its hash stayed in
    ingest_hashes forever, silently no-op'ing any future re-upload of identical content. Routing
    through the same _soft_delete_resolved every other delete path uses keeps this one from
    drifting out of sync with the rest of the delete/dedup/index invariants."""
    resolved = _resolve_dup_path(encoded_path)

    if not resolved.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if not resolved.is_file():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path is not a file")

    path_str = str(resolved)
    original_path = "/" + str(resolved.relative_to(settings.nas_root.resolve())).replace("\\", "/")

    from .file_routes import _soft_delete_resolved
    await _soft_delete_resolved(resolved, original_path, user.get("sub", ""))
    logger.info("duplicate_deleted path=%s by=%s", path_str, user.get("sub", "?"))

    # Update stored results: remove this path; drop sets reduced to 1 copy
    def _prune(results: list) -> list:
        updated = []
        for entry in results:
            copies = [c for c in entry.get("copies", []) if c["path"] != path_str]
            if len(copies) >= 2:
                updated.append({**entry, "copies": copies})
        return updated

    await store.atomic_update("duplicate_scan_results", _prune, default=[])

    return {"deleted": path_str}
