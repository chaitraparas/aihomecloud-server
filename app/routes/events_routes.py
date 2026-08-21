"""
Activation-funnel telemetry routes. See kb/telemetry_architecture_decision.md for the full
reasoning — the short version: events post here (this board, over the connection the app
already uses for everything else) and never leave the house on their own. There is no
upload-to-founder path in this module at all; that's the explicitly opt-in, off-by-default
piece the architecture decision deferred out of the v1 scope, not something silently
half-built here.

POST is any authenticated user (a family member's own phone posting its own progress).
GET (aggregate counts) is admin-only, same reasoning as activity_routes.py's admin gate —
this is founder-facing usage data about the whole household, not any one member's business.
"""

from fastapi import APIRouter, Depends

from ..auth import get_current_user, require_admin
from ..models import FunnelCountsResponse, FunnelEventRequest
from .. import store

router = APIRouter(prefix="/api/v1/events", tags=["events"])


@router.post("", status_code=201)
async def record_funnel_event(
    body: FunnelEventRequest,
    user: dict = Depends(get_current_user),
):
    """Record one funnel-stage event. Deliberately minimal: event name + timestamp + which
    user's device sent it, nothing else — see FunnelEventRequest's own docstring on why
    there's no PII field to even accidentally populate."""
    from datetime import datetime, timezone

    event = {
        "event": body.event_name,
        "timestamp": body.client_ts or int(datetime.now(timezone.utc).timestamp()),
        "username": user.get("sub"),
    }
    await store.append_funnel_event(event)
    return {"status": "recorded"}


@router.get("/funnel", response_model=FunnelCountsResponse)
async def get_funnel_counts(user: dict = Depends(require_admin)):
    """Aggregate counts per event name, this board only. Not per-user, not raw events —
    just how many times each funnel stage has fired, which is the whole ask from the 90-day
    plan's "find the single biggest drop-off" goal."""
    events = await store.get_funnel_events()
    counts: dict[str, int] = {}
    for e in events:
        name = e.get("event", "unknown")
        counts[name] = counts.get(name, 0) + 1
    return FunnelCountsResponse(counts=counts, total_events=len(events))
