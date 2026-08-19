"""
Activity/audit log routes — read-only, paginated view of persisted audit
events (app/audit.py's audit_log() calls, e.g. file_deleted, storage_formatted,
family_member_removed). Admin-only: this surfaces who-did-what across every
family member, not just the requesting user's own actions.

Previously audit_log() only wrote to the app's log file -- nothing was
queryable or surfaced in-app, a real gap against a "who changed what, when"
bar (see docs/plan_sbc_hardening_and_nas_sharing_2026-07-14.md's production-
grade survey, 2026-07-15).
"""

from fastapi import APIRouter, Depends, Query

from ..auth import require_admin
from ..models import ActivityLogResponse
from .. import store

router = APIRouter(prefix="/api/v1/activity", tags=["activity"])


@router.get("/events", response_model=ActivityLogResponse)
async def list_activity_events(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(require_admin),
):
    """Paginated activity log, newest first (store.append_activity_event()
    inserts at the front, so no re-sorting is needed here)."""
    events = await store.get_activity_events()
    total = len(events)
    start = (page - 1) * page_size
    end = start + page_size
    return ActivityLogResponse(
        items=events[start:end],
        totalCount=total,
        page=page,
        pageSize=page_size,
    )
