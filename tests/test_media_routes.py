"""
Media query route tests — GET /api/v1/media (browse by logical identity, not
physical path) and GET /api/v1/media/{id}/content (opaque-id content fetch).
"""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.ingest import Destination, IngestMode, Scope, ingest


async def _chunks(*parts: bytes):
    for p in parts:
        yield p


def _epoch(y: int, m: int, d: int = 1) -> float:
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp()


@pytest.mark.asyncio
async def test_list_media_filters_by_scope_and_source_folder(authenticated_client: AsyncClient):
    from app.config import settings

    dest_family = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    await ingest(_chunks(b"jan family"), filename="jan.jpg", dest=dest_family, original_date=_epoch(2025, 1))
    await ingest(_chunks(b"feb family"), filename="feb.jpg", dest=dest_family, original_date=_epoch(2025, 2))

    # Real Sync uploads route through the same SORTED/bucket-allocated tree as a
    # direct upload (the physical Backups/<folder-id>/ tree was retired) — source
    # and source_folder are now passed explicitly by the /upload route rather than
    # inferred from a raw_dir path shape, so the test does the same.
    dest_sync = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    await ingest(
        _chunks(b"camera photo"), filename="cam.jpg", dest=dest_sync, original_date=_epoch(2025, 3),
        source="sync", source_folder="Camera",
    )

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 3

    resp = await authenticated_client.get(
        "/api/v1/media", params={"scope": "family", "sourceFolder": "Camera"},
    )
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["sourceFolder"] == "Camera"
    assert items[0]["source"] == "sync"


@pytest.mark.asyncio
async def test_list_media_orders_newest_capture_first(authenticated_client: AsyncClient):
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    await ingest(_chunks(b"old"), filename="old.jpg", dest=dest, original_date=_epoch(2024, 1))
    await ingest(_chunks(b"new"), filename="new.jpg", dest=dest, original_date=_epoch(2025, 6))

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    items = resp.json()["items"]
    assert [i["filename"] for i in items] == ["new.jpg", "old.jpg"]


@pytest.mark.asyncio
async def test_list_media_pagination_cursor(authenticated_client: AsyncClient):
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    for month in range(1, 6):
        await ingest(
            _chunks(f"file{month}".encode()), filename=f"f{month}.jpg",
            dest=dest, original_date=_epoch(2025, month),
        )

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family", "pageSize": 2})
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["nextCursor"] is not None

    resp2 = await authenticated_client.get(
        "/api/v1/media",
        params={"scope": "family", "pageSize": 2, "cursor": body["nextCursor"]},
    )
    body2 = resp2.json()
    assert len(body2["items"]) == 2
    ids1 = {i["id"] for i in body["items"]}
    ids2 = {i["id"] for i in body2["items"]}
    assert ids1.isdisjoint(ids2)


@pytest.mark.asyncio
async def test_list_media_rejects_invalid_scope(authenticated_client: AsyncClient):
    resp = await authenticated_client.get("/api/v1/media", params={"scope": "bogus"})
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.security
async def test_list_media_personal_scope_blocks_other_users(client: AsyncClient, member_token: str):
    dest = Destination(scope=Scope.PERSONAL, owner="admin", mode=IngestMode.SORTED)
    await ingest(_chunks(b"admins photo"), filename="p.jpg", dest=dest, original_date=_epoch(2025, 1))

    client.headers.update({"Authorization": f"Bearer {member_token}"})
    resp = await client.get("/api/v1/media", params={"scope": "personal", "owner": "admin"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_media_content_returns_bytes(authenticated_client: AsyncClient):
    await ingest(_chunks(b"the actual bytes"), filename="c.jpg",
                 dest=Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED),
                 original_date=_epoch(2025, 1))

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    entry_id = resp.json()["items"][0]["id"]

    content_resp = await authenticated_client.get(f"/api/v1/media/{entry_id}/content")
    assert content_resp.status_code == 200
    assert content_resp.content == b"the actual bytes"


@pytest.mark.asyncio
async def test_get_media_content_404_for_unknown_id(authenticated_client: AsyncClient):
    resp = await authenticated_client.get("/api/v1/media/999999/content")
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.security
async def test_get_media_content_blocks_other_users_personal_file(client: AsyncClient, member_token: str):
    dest = Destination(scope=Scope.PERSONAL, owner="admin", mode=IngestMode.SORTED)
    await ingest(_chunks(b"private"), filename="priv.jpg", dest=dest, original_date=_epoch(2025, 1))

    admin_login = await client.post("/api/v1/auth/login", json={"name": "admin", "pin": "0000"})
    admin_tok = admin_login.json()["accessToken"]
    resp = await client.get(
        "/api/v1/media", params={"scope": "personal", "owner": "admin"},
        headers={"Authorization": f"Bearer {admin_tok}"},
    )
    entry_id = resp.json()["items"][0]["id"]

    client.headers.update({"Authorization": f"Bearer {member_token}"})
    content_resp = await client.get(f"/api/v1/media/{entry_id}/content")
    assert content_resp.status_code == 403


@pytest.mark.asyncio
async def test_mark_missing_deleted_hides_soft_deleted_files(authenticated_client: AsyncClient):
    """The reconcile pass is the backstop for files removed/moved OUTSIDE any API route
    (e.g. directly over the raw SMB share, or a stale row left by some other gap) —
    covered here by simulating a raw unlink() media_index never saw. The /files/delete
    HTTP route itself now updates media_index immediately without needing this pass;
    see test_delete_file_marks_media_index_deleted_immediately in test_file_routes.py."""
    from app import media_index
    from app.config import settings

    result = await ingest(
        _chunks(b"will be deleted"), filename="gone.jpg",
        dest=Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED),
        original_date=_epoch(2025, 1),
    )

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    assert len(resp.json()["items"]) == 1

    result.path.unlink()  # simulate a delete/move that never touched media_index
    marked = await media_index.mark_missing_deleted(settings.nas_root.resolve())
    assert marked == 1

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_record_entry_is_idempotent_for_same_blob(authenticated_client: AsyncClient):
    """Re-recording the same rel_path (e.g. the backfill scanner re-running
    over already-indexed files) must update the existing entries row, not
    create a duplicate — one physical file has exactly one current entry."""
    from app import media_index

    await ingest(
        _chunks(b"same file"), filename="repeat.jpg",
        dest=Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED),
        original_date=_epoch(2025, 5),
    )

    rows = await media_index.query_entries(scope="family", limit=10)
    assert len(rows) == 1
    row = rows[0]
    entry = await media_index.get_entry(row["id"])

    # Simulate the backfill scanner re-recording the exact same blob.
    await media_index.record_entry(
        rel_path=entry["rel_path"],
        content_hash="deadbeef",
        size_bytes=row["size_bytes"],
        scope=row["scope"],
        owner=row["owner"],
        category=row["category"],
        media_type=row["media_type"],
        source=row["source"],
        source_folder=row["source_folder"],
        filename=row["filename"],
        original_name=row["original_name"],
        capture_date=row["capture_date"],
    )

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    assert len(resp.json()["items"]) == 1


@pytest.mark.asyncio
async def test_delete_media_by_id_moves_to_trash_and_hides_from_list(authenticated_client: AsyncClient):
    """DELETE /api/v1/media/{id} is the only delete path available once the
    app browses by entry id — it must both move the file to trash AND mark
    media_index deleted immediately (no reconcile-pass wait, unlike a plain
    filesystem-level delete)."""
    await ingest(
        _chunks(b"delete me"), filename="del.jpg",
        dest=Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED),
        original_date=_epoch(2025, 1),
    )
    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    entry = resp.json()["items"][0]

    del_resp = await authenticated_client.delete(f"/api/v1/media/{entry['id']}")
    assert del_resp.status_code == 204

    resp2 = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    assert resp2.json()["items"] == []

    content_resp = await authenticated_client.get(f"/api/v1/media/{entry['id']}/content")
    assert content_resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_media_by_id_404_for_unknown_id(authenticated_client: AsyncClient):
    resp = await authenticated_client.delete("/api/v1/media/999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.security
async def test_delete_media_by_id_blocks_other_users_personal_file(client: AsyncClient, member_token: str):
    dest = Destination(scope=Scope.PERSONAL, owner="admin", mode=IngestMode.SORTED)
    await ingest(_chunks(b"private"), filename="p.jpg", dest=dest, original_date=_epoch(2025, 1))

    admin_login = await client.post("/api/v1/auth/login", json={"name": "admin", "pin": "0000"})
    admin_tok = admin_login.json()["accessToken"]
    resp = await client.get(
        "/api/v1/media", params={"scope": "personal", "owner": "admin"},
        headers={"Authorization": f"Bearer {admin_tok}"},
    )
    entry_id = resp.json()["items"][0]["id"]

    client.headers.update({"Authorization": f"Bearer {member_token}"})
    del_resp = await client.delete(f"/api/v1/media/{entry_id}")
    assert del_resp.status_code == 403


def _real_jpeg_bytes() -> bytes:
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(255, 0, 0)).save(buf, "jpeg")
    return buf.getvalue()


async def _real_jpeg_chunks():
    yield _real_jpeg_bytes()


@pytest.mark.asyncio
async def test_get_media_thumbnail_returns_jpeg(authenticated_client: AsyncClient):
    """Grid tiles must resolve to a small cached JPEG by entry id, not full
    /content bytes — same generation/caching path as /files/thumbnail,
    reached through the entry-id indirection instead of a physical path."""
    await ingest(
        _real_jpeg_chunks(), filename="tile.jpg",
        dest=Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED),
        original_date=_epoch(2025, 1),
    )
    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    entry_id = resp.json()["items"][0]["id"]

    thumb_resp = await authenticated_client.get(f"/api/v1/media/{entry_id}/thumbnail")
    assert thumb_resp.status_code == 200
    assert thumb_resp.headers["content-type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_get_media_thumbnail_404_for_unknown_id(authenticated_client: AsyncClient):
    resp = await authenticated_client.get("/api/v1/media/999999/thumbnail")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_media_filters_by_type(authenticated_client: AsyncClient):
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    await ingest(_chunks(b"photo"), filename="p.jpg", dest=dest, original_date=_epoch(2025, 1))
    await ingest(_chunks(b"video"), filename="v.mp4", dest=dest, original_date=_epoch(2025, 1))

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family", "type": "photo"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["filename"] == "p.jpg"

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family", "type": "video"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["filename"] == "v.mp4"

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    assert len(resp.json()["items"]) == 2


@pytest.mark.asyncio
async def test_list_media_returns_video_duration(authenticated_client: AsyncClient):
    """Duration is ffprobe-extracted at ingest time (see ingest._extract_video_duration)
    and stored on the entries row -- record it directly here rather than depending on a
    real video file/ffprobe being available in the test environment, and confirm it
    round-trips through the query response. Photos never carry a duration."""
    from app import media_index

    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    await ingest(_chunks(b"photo"), filename="p.jpg", dest=dest, original_date=_epoch(2025, 1))
    await ingest(_chunks(b"video"), filename="v.mp4", dest=dest, original_date=_epoch(2025, 1))

    rows = await media_index.query_entries(scope="family", limit=10)
    video_row = next(r for r in rows if r["filename"] == "v.mp4")
    await media_index.record_entry(
        rel_path=(await media_index.get_entry(video_row["id"]))["rel_path"],
        content_hash="videohash",
        size_bytes=video_row["size_bytes"],
        scope=video_row["scope"],
        owner=video_row["owner"],
        category=video_row["category"],
        media_type=video_row["media_type"],
        source=video_row["source"],
        source_folder=video_row["source_folder"],
        filename=video_row["filename"],
        original_name=video_row["original_name"],
        capture_date=video_row["capture_date"],
        duration=125.5,
    )

    resp = await authenticated_client.get("/api/v1/media", params={"scope": "family"})
    items = {i["filename"]: i for i in resp.json()["items"]}
    assert items["v.mp4"]["duration"] == 125.5
    assert items["p.jpg"]["duration"] is None


@pytest.mark.asyncio
async def test_list_media_sort_oldest_first(authenticated_client: AsyncClient):
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    await ingest(_chunks(b"old"), filename="old.jpg", dest=dest, original_date=_epoch(2024, 1))
    await ingest(_chunks(b"mid"), filename="mid.jpg", dest=dest, original_date=_epoch(2025, 1))
    await ingest(_chunks(b"new"), filename="new.jpg", dest=dest, original_date=_epoch(2025, 6))

    resp = await authenticated_client.get(
        "/api/v1/media", params={"scope": "family", "sortBy": "modified", "sortDir": "asc"},
    )
    items = resp.json()["items"]
    assert [i["filename"] for i in items] == ["old.jpg", "mid.jpg", "new.jpg"]


@pytest.mark.asyncio
async def test_list_media_sort_name_ascending(authenticated_client: AsyncClient):
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    await ingest(_chunks(b"b"), filename="banana.jpg", dest=dest, original_date=_epoch(2025, 1))
    await ingest(_chunks(b"a"), filename="apple.jpg", dest=dest, original_date=_epoch(2025, 1))
    await ingest(_chunks(b"c"), filename="cherry.jpg", dest=dest, original_date=_epoch(2025, 1))

    resp = await authenticated_client.get(
        "/api/v1/media", params={"scope": "family", "sortBy": "name", "sortDir": "asc"},
    )
    items = resp.json()["items"]
    assert [i["filename"] for i in items] == ["apple.jpg", "banana.jpg", "cherry.jpg"]


@pytest.mark.asyncio
async def test_list_media_sort_name_descending(authenticated_client: AsyncClient):
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    await ingest(_chunks(b"b"), filename="banana.jpg", dest=dest, original_date=_epoch(2025, 1))
    await ingest(_chunks(b"a"), filename="apple.jpg", dest=dest, original_date=_epoch(2025, 1))
    await ingest(_chunks(b"c"), filename="cherry.jpg", dest=dest, original_date=_epoch(2025, 1))

    resp = await authenticated_client.get(
        "/api/v1/media", params={"scope": "family", "sortBy": "name", "sortDir": "desc"},
    )
    items = resp.json()["items"]
    assert [i["filename"] for i in items] == ["cherry.jpg", "banana.jpg", "apple.jpg"]


@pytest.mark.asyncio
async def test_list_media_pagination_cursor_with_name_sort(authenticated_client: AsyncClient):
    dest = Destination(scope=Scope.FAMILY, mode=IngestMode.SORTED)
    for name in ["apple", "banana", "cherry", "date", "elderberry"]:
        await ingest(
            _chunks(name.encode()), filename=f"{name}.jpg",
            dest=dest, original_date=_epoch(2025, 1),
        )

    resp = await authenticated_client.get(
        "/api/v1/media", params={"scope": "family", "sortBy": "name", "sortDir": "asc", "pageSize": 2},
    )
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["nextCursor"] is not None

    resp2 = await authenticated_client.get(
        "/api/v1/media",
        params={"scope": "family", "sortBy": "name", "sortDir": "asc", "pageSize": 2, "cursor": body["nextCursor"]},
    )
    body2 = resp2.json()
    assert len(body2["items"]) == 2
    ids1 = {i["id"] for i in body["items"]}
    ids2 = {i["id"] for i in body2["items"]}
    assert ids1.isdisjoint(ids2)

    resp3 = await authenticated_client.get(
        "/api/v1/media",
        params={"scope": "family", "sortBy": "name", "sortDir": "asc", "pageSize": 2, "cursor": body2["nextCursor"]},
    )
    body3 = resp3.json()
    assert len(body3["items"]) == 1
    ids3 = {i["id"] for i in body3["items"]}
    assert ids1.isdisjoint(ids3) and ids2.isdisjoint(ids3)

    all_names = []
    for body in [body, body2, body3]:
        for i in body["items"]:
            all_names.append(i["filename"])
    assert all_names == ["apple.jpg", "banana.jpg", "cherry.jpg", "date.jpg", "elderberry.jpg"]


@pytest.mark.asyncio
async def test_list_media_rejects_invalid_sort_by(authenticated_client: AsyncClient):
    resp = await authenticated_client.get(
        "/api/v1/media", params={"scope": "family", "sortBy": "bogus"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_media_rejects_invalid_sort_dir(authenticated_client: AsyncClient):
    resp = await authenticated_client.get(
        "/api/v1/media", params={"scope": "family", "sortDir": "bogus"},
    )
    assert resp.status_code == 400


class TestTimestampsAreOneFormat:
    """
    /media and /files/list must agree on what a timestamp looks like.

    They did not: /media sent epoch seconds (1785689534) while /files/list sent
    "2026-07-22T21:25:31.179191Z". Every client then needed a branch to tell the two apart, and
    Hearth's adapter still carries the function that did it. Nothing failed loudly — the wrong
    branch just produced dates in 1970 — which is why this is pinned by a test rather than left to
    reviewers to notice.
    """

    def test_capture_date_is_iso_8601_not_epoch(self):
        from app.routes.media_routes import MediaEntry

        entry = MediaEntry(
            id=1, scope="family", category="Photos", source="test", filename="a.jpg",
            originalName="a.jpg", captureDate=1785689534, sizeBytes=10,
        )

        assert entry.capture_date == "2026-08-02T16:52:14Z"

    def test_a_value_that_is_already_iso_passes_through(self):
        from app.routes.media_routes import MediaEntry

        entry = MediaEntry(
            id=1, scope="family", category="Photos", source="test", filename="a.jpg",
            originalName="a.jpg", captureDate="2026-07-22T21:25:31.179191Z", sizeBytes=10,
        )

        assert entry.capture_date == "2026-07-22T21:25:31.179191Z"

    def test_a_missing_capture_date_stays_absent(self):
        from app.routes.media_routes import MediaEntry

        entry = MediaEntry(
            id=1, scope="family", category="Photos", source="test", filename="a.jpg",
            originalName="a.jpg", sizeBytes=10,
        )

        assert entry.capture_date is None


class TestPlaybackPositions:
    """
    Resume follows the person, not the device — and never leaks between people.

    A viewing history is a small but real disclosure, so the tests that matter most here are the
    isolation ones. Following the lesson from the media-scope leak earlier this week: check what a
    caller is *served*, not only that the wrong caller is refused.
    """

    async def test_a_position_is_stored_and_returned(self, authenticated_client):
        from app import media_index
        await media_index.record_entry(
            rel_path="/entertainment/Movies/f.mkv", content_hash="p1", size_bytes=10,
            scope="entertainment", owner=None, category="Movies", media_type="video/x-matroska",
            source="test", source_folder=None, filename="f.mkv", original_name="f.mkv",
            capture_date=1.0, mtime=1.0, duration=7200.0,
        )
        [(entry_id, _)] = [(e["id"], e) for e in [await media_index.get_entry(1)] if e] or [(1, None)]

        res = await authenticated_client.put(
            f"/api/v1/media/{entry_id}/position",
            json={"position": 600.0, "duration": 7200.0, "clientUpdatedAt": 1_700_000_000_000},
        )
        # 200 + an ack, not 204: the caller needs the version back to know whether its report won.
        assert res.status_code == 200, res.text
        assert res.json() == {"version": 1, "cleared": False, "applied": True}

        listed = (await authenticated_client.get("/api/v1/media/positions")).json()
        assert any(p["entryId"] == entry_id and p["position"] == 600.0 for p in listed)

    async def test_one_member_never_sees_another_members_positions(self, client, admin_token, member_token):
        """The disclosure that matters. Admin is deliberately the *writer* here."""
        from app import media_index
        await media_index.record_entry(
            rel_path="/entertainment/Movies/g.mkv", content_hash="p2", size_bytes=10,
            scope="entertainment", owner=None, category="Movies", media_type="video/x-matroska",
            source="test", source_folder=None, filename="g.mkv", original_name="g.mkv",
            capture_date=1.0, mtime=1.0, duration=7200.0,
        )
        entry = await media_index.get_entry(1)
        entry_id = entry["id"]

        await client.put(
            f"/api/v1/media/{entry_id}/position",
            json={"position": 900.0, "clientUpdatedAt": 1_700_000_000_000},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        others = (await client.get(
            "/api/v1/media/positions", headers={"Authorization": f"Bearer {member_token}"}
        )).json()

        assert others == [], "a member must not see what the admin has been watching"

    async def test_a_barely_started_item_is_not_remembered(self, authenticated_client):
        """Offering to resume the first few seconds of something is noise, not a feature."""
        from app import media_index
        await media_index.record_entry(
            rel_path="/entertainment/Movies/h.mkv", content_hash="p3", size_bytes=10,
            scope="entertainment", owner=None, category="Movies", media_type="video/x-matroska",
            source="test", source_folder=None, filename="h.mkv", original_name="h.mkv",
            capture_date=1.0, mtime=1.0, duration=7200.0,
        )
        entry_id = (await media_index.get_entry(1))["id"]

        await authenticated_client.put(
            f"/api/v1/media/{entry_id}/position", json={"position": 5.0, "duration": 7200.0, "clientUpdatedAt": 1_700_000_000_000}
        )

        assert (await authenticated_client.get("/api/v1/media/positions")).json() == []

    async def test_a_finished_item_stops_being_offered(self, authenticated_client):
        """A film watched to the credits must not sit in Continue Watching forever."""
        from app import media_index
        await media_index.record_entry(
            rel_path="/entertainment/Movies/i.mkv", content_hash="p4", size_bytes=10,
            scope="entertainment", owner=None, category="Movies", media_type="video/x-matroska",
            source="test", source_folder=None, filename="i.mkv", original_name="i.mkv",
            capture_date=1.0, mtime=1.0, duration=100.0,
        )
        entry_id = (await media_index.get_entry(1))["id"]

        await authenticated_client.put(
            f"/api/v1/media/{entry_id}/position", json={"position": 40.0, "duration": 100.0, "clientUpdatedAt": 1_700_000_000_000}
        )
        await authenticated_client.put(
            f"/api/v1/media/{entry_id}/position",
            # A LATER client timestamp — a real player advances it on every report. With the same
            # timestamp this is correctly refused as "not newer", which is the idempotency rule.
            json={"position": 99.0, "duration": 100.0, "clientUpdatedAt": 1_700_000_060_000},
        )

        assert (await authenticated_client.get("/api/v1/media/positions")).json() == []

    async def test_an_unknown_item_is_refused(self, authenticated_client):
        res = await authenticated_client.put(
            "/api/v1/media/999999/position", json={"position": 60.0, "clientUpdatedAt": 1_700_000_000_000}
        )

        assert res.status_code == 404


class TestAdminMustNameWhose:
    """
    An admin reading a member's personal files is a real capability. Reading *everyone's* by
    omission is not — it silently merged the household's private libraries into the admin's own
    gallery, which is how this was found.
    """

    async def test_admin_without_an_owner_is_refused(self, authenticated_client):
        res = await authenticated_client.get("/api/v1/media?scope=personal")

        assert res.status_code == 400
        assert "owner" in res.text.lower()

    async def test_admin_naming_an_owner_is_allowed(self, authenticated_client):
        res = await authenticated_client.get("/api/v1/media?scope=personal&owner=admin")

        assert res.status_code == 200

    async def test_family_scope_is_unaffected(self, authenticated_client):
        assert (await authenticated_client.get("/api/v1/media?scope=family")).status_code == 200
