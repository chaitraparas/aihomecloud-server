"""
Media query routes — browse ingested files by logical identity (scope, owner,
source_folder, category) rather than physical filesystem path. Backs the
Android Folders/timeline views: the app never needs to know how files are
physically bucketed on disk (flat, YYYY/MM-nested, whatever) — it asks for
e.g. source_folder="Camera" and gets back exactly the entries recorded with
that source_folder, regardless of where those bytes actually live.
"""

import mimetypes as _mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.responses import Response
from pydantic import BaseModel, Field, field_validator
from starlette.requests import Request
from starlette.responses import FileResponse

from .. import media_index
from ..auth import get_current_user
from ..config import settings
from ..ingest import Scope, _resolve_identity
from ..limiter import limiter
from ..models import Float64, Int64

router = APIRouter(prefix="/api/v1/media", tags=["media"])


class MediaEntry(BaseModel):
    id: int
    scope: str
    owner: Optional[str] = None
    category: str
    media_type: Optional[str] = Field(None, alias="mediaType")
    source: str
    source_folder: Optional[str] = Field(None, alias="sourceFolder")
    filename: str
    original_name: str = Field(alias="originalName")
    # ISO-8601, matching /files/list. It was epoch SECONDS while /files/list sent
    # "2026-07-22T21:25:31.179191Z", so every client had to carry a branch to tell the two apart --
    # Hearth's adapter still has the function. One API should not disagree with itself about what a
    # timestamp is. The pagination cursor stays epoch internally: it is opaque to clients and
    # sorting on a number is cheaper than on a string.
    capture_date: Optional[str] = Field(None, alias="captureDate")
    size_bytes: int = Field(alias="sizeBytes")
    duration: Optional[float] = None

    model_config = {"populate_by_name": True}

    @field_validator("capture_date", mode="before")
    @classmethod
    def _epoch_to_iso(cls, v):
        """Rows carry epoch seconds; the wire carries ISO. Strings pass through untouched so a
        row that is already ISO (or a test constructing one) is not mangled."""
        if v is None or isinstance(v, str):
            return v
        return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat().replace("+00:00", "Z")


class MediaListResponse(BaseModel):
    items: list[MediaEntry]
    next_cursor: Optional[str] = Field(None, alias="nextCursor")

    model_config = {"populate_by_name": True}


async def _authorize_media_scope(scope: str, owner: Optional[str], user: dict) -> None:
    """Mirror ingest._authorize_path's per-user restriction, but for a
    (scope, owner) query pair instead of a resolved filesystem path."""
    if scope != Scope.PERSONAL.value:
        return
    name, is_admin = await _resolve_identity(user)
    if owner and Path(owner).name == name:
        return
    if is_admin:
        # An admin may read a member's personal files — that is a real product capability, used for
        # recovery. But they must say WHOSE. With no owner the query returned every member's private
        # library merged into one list, which is not deliberate access, it is accidental
        # aggregation: it silently mixed the whole household's private photos into the admin's own
        # gallery. Naming the owner makes it a decision rather than a side effect.
        if owner:
            return
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Personal scope needs an owner — name whose files you mean",
        )
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "Access to another user's personal files is not allowed",
    )


def _decode_cursor(cursor: Optional[str], sort_by: str) -> tuple[Optional[int | str], Optional[int]]:
    if not cursor:
        return None, None
    try:
        id_str, value_str = cursor.split(":", 1)
        entry_id = int(id_str)
        value: int | str = int(value_str) if sort_by == "modified" else value_str
        return value, entry_id
    except (ValueError, AttributeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor")


@router.get("", response_model=MediaListResponse)
@limiter.limit("60/minute")
async def list_media(
    request: Request,
    scope: str = Query(...),
    owner: Optional[str] = Query(None),
    source_folder: Optional[str] = Query(None, alias="sourceFolder"),
    category: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    sort_by: str = Query("modified", alias="sortBy"),
    sort_dir: str = Query("desc", alias="sortDir"),
    cursor: Optional[str] = Query(None),
    page_size: int = Query(60, ge=1, le=500, alias="pageSize"),
    user: dict = Depends(get_current_user),
):
    """Query ingested files by logical identity — never a physical path.
    Keyset-paginated via an opaque cursor, not OFFSET (degrades badly past
    a few thousand rows on a 1GB-RAM target)."""
    if scope not in (s.value for s in Scope):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid scope")
    if sort_by not in ("modified", "name"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid sortBy")
    if sort_dir not in ("asc", "desc"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid sortDir")
    if type is not None and type not in ("photo", "video"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid type")
    await _authorize_media_scope(scope, owner, user)

    before_value, before_id = _decode_cursor(cursor, sort_by)
    rows = await media_index.query_entries(
        scope=scope,
        owner=owner,
        source_folder=source_folder,
        category=category,
        media_type=type,
        sort_by=sort_by,
        sort_dir=sort_dir,
        before_value=before_value,
        before_id=before_id,
        limit=page_size,
    )

    next_cursor = None
    if len(rows) == page_size and rows:
        last = rows[-1]
        sort_col_key = "capture_date" if sort_by == "modified" else "filename"
        next_cursor = f"{last['id']}:{last[sort_col_key]}"

    return MediaListResponse(
        items=[MediaEntry(**r) for r in rows],
        next_cursor=next_cursor,
    )


@router.get("/{entry_id}/content",
    responses={200: {"content": {"application/octet-stream": {}}}},
    response_class=Response,
)
@limiter.limit("120/minute")
async def get_media_content(request: Request, entry_id: int, user: dict = Depends(get_current_user)):
    """Resolve an opaque entry id to its file bytes. The app never sees or
    needs the physical rel_path — that indirection is the whole point."""
    entry = await media_index.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    await _authorize_media_scope(entry["scope"], entry["owner"], user)

    abs_path = (settings.nas_root / entry["rel_path"].lstrip("/")).resolve()
    if not abs_path.is_relative_to(settings.nas_root.resolve()) or not abs_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    media_type = entry.get("media_type") or _mimetypes.guess_type(entry["filename"])[0]
    return FileResponse(abs_path, media_type=media_type, filename=entry["filename"])


@router.get("/{entry_id}/thumbnail",
    responses={200: {"content": {"image/jpeg": {}}}},
    response_class=Response,
)
@limiter.limit("120/minute")
async def get_media_thumbnail(
    request: Request,
    entry_id: int,
    size: int = Query(256, description="Max thumbnail edge in pixels"),
    user: dict = Depends(get_current_user),
):
    """Small cached JPEG thumbnail, resolved by opaque entry id — grid tiles
    must not load full-resolution /content bytes just to show a thumbnail,
    especially on a 1GB-RAM target. Shares the exact cache/generation logic
    /files/thumbnail uses (file_routes._thumbnail_response)."""
    from .file_routes import _thumbnail_response

    entry = await media_index.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    await _authorize_media_scope(entry["scope"], entry["owner"], user)

    abs_path = (settings.nas_root / entry["rel_path"].lstrip("/")).resolve()
    if not abs_path.is_relative_to(settings.nas_root.resolve()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    return await _thumbnail_response(abs_path, size, log_ref=f"entry:{entry_id}")


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def delete_media(request: Request, entry_id: int, user: dict = Depends(get_current_user)):
    """Soft-delete by opaque entry id — mirrors /files/delete's trash
    behavior (same shared helper) but also marks the media_index row deleted
    immediately, rather than waiting for the next reconcile pass. Once the
    app browses via entry ids instead of physical paths, this is the only
    delete path available to it — it has no path to hand /files/delete."""
    from .file_routes import _soft_delete_resolved

    entry = await media_index.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    await _authorize_media_scope(entry["scope"], entry["owner"], user)

    abs_path = (settings.nas_root / entry["rel_path"].lstrip("/")).resolve()
    if not abs_path.is_relative_to(settings.nas_root.resolve()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    await _soft_delete_resolved(abs_path, entry["rel_path"], user.get("sub", ""))
    await media_index.mark_entry_deleted(entry_id)

class PlaybackPosition(BaseModel):
    """Where someone got to. Seconds, not a percentage — a re-encode changes length."""

    position: Float64 = Field(ge=0, description="Seconds from the start")
    duration: Optional[Float64] = Field(None, ge=0, description="Total length, when the player knows it")
    # Epoch MILLISECONDS from the reporting client, and the only value conflict resolution
    # compares. Deliberately the client's clock, not this board's: these are SBCs without an RTC,
    # so the board's clock cannot be trusted to order two devices' offline playback. Bounded here
    # so a malformed value cannot overflow; the storage layer clamps rather than rejecting, because
    # a device with a broken clock must not be wedged out of syncing forever.
    # REQUIRED, and >= 1. A defaulted 0 was tried and is wrong: every later report would then
    # carry the same timestamp as the stored one, the "equal is not newer" rule would reject it,
    # and the endpoint would silently become write-once — a finished film could never clear. A
    # client that cannot supply a timestamp cannot participate in conflict resolution safely, so
    # it is refused explicitly rather than being given last-write-wins semantics by accident.
    client_updated_at: Int64 = Field(
        ..., ge=1, le=4102444800000, alias="clientUpdatedAt",
        description="Client's epoch-ms timestamp for this report; used for conflict resolution",
    )

    model_config = {"populate_by_name": True}


class PlaybackPositionEntry(BaseModel):
    entry_id: int = Field(alias="entryId")
    position: Float64
    duration: Optional[Float64] = None
    #: This board's clock. Informational only — never use it to order two clients' state.
    updated_at: Int64 = Field(alias="updatedAt")
    #: Synchronisation counter, not a time. Advances whenever the stored state actually changes.
    version: int = 0
    #: The client timestamp behind the stored state; what a client compares its own against.
    #: Int64 because these are epoch MILLISECONDS — they overflow a 32-bit int, and the Kotlin
    #: generator faithfully emits kotlin.Int without the int64 format hint.
    client_updated_at: Int64 = Field(0, alias="clientUpdatedAt")

    model_config = {"populate_by_name": True}


class PlaybackPositionAck(BaseModel):
    """What the board decided. `applied` false means a newer report already won."""

    version: int
    cleared: bool
    applied: bool

    model_config = {"populate_by_name": True}


@router.put("/{entry_id}/position", response_model=PlaybackPositionAck)
@limiter.limit("120/minute")
async def set_playback_position(
    request: Request,
    entry_id: int,
    body: PlaybackPosition,
    user: dict = Depends(get_current_user),
):
    """
    Record how far this person has watched.

    Keyed by the caller's own identity, never by a name in the request: a position is a small but
    real disclosure about what someone has been watching, and accepting a username from the body
    would let any member write — and by implication read back — another member's viewing history.
    """
    name, _ = await _resolve_identity(user)
    entry = await media_index.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such item")
    # Every sibling handler authorises the (scope, owner) pair before touching an entry; this one
    # did not. Without it, a member could probe ids and learn from the 404-vs-204 difference which
    # items exist in another member's personal library — an existence oracle over private files.
    await _authorize_media_scope(entry.get("scope", ""), entry.get("owner"), user)
    # Conflict resolution lives in the storage layer, on the server, so a stale device cannot
    # overwrite a newer one however the client behaves. Retrying an identical report is a no-op
    # that returns the same version.
    result = await media_index.set_position(
        name, entry_id, body.position, body.duration, body.client_updated_at,
    )
    return PlaybackPositionAck(**result)


@router.get("/positions", response_model=list[PlaybackPositionEntry])
async def list_playback_positions(user: dict = Depends(get_current_user)):
    """This caller's part-watched items, most recent first. Never anyone else's."""
    name, _ = await _resolve_identity(user)
    return [PlaybackPositionEntry(**p) for p in await media_index.positions(name)]
