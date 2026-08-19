"""
Tests for activity_routes.py — the persisted, queryable activity/audit log.
"""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_list_activity_events_requires_auth(client):
    resp = await client.get("/api/v1/activity/events")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_list_activity_events_empty(authenticated_client):
    with patch("app.routes.activity_routes.store") as mock_store:
        mock_store.get_activity_events = AsyncMock(return_value=[])
        resp = await authenticated_client.get("/api/v1/activity/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["totalCount"] == 0
        assert data["page"] == 1
        assert data["pageSize"] == 50


@pytest.mark.asyncio
async def test_list_activity_events_paginates(authenticated_client):
    fake_events = [
        {"event": f"event-{i}", "timestamp": float(i)} for i in range(10)
    ]
    with patch("app.routes.activity_routes.store") as mock_store:
        mock_store.get_activity_events = AsyncMock(return_value=fake_events)
        resp = await authenticated_client.get(
            "/api/v1/activity/events", params={"page": 2, "page_size": 3},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalCount"] == 10
        assert data["page"] == 2
        assert data["pageSize"] == 3
        # page 2 of size 3 -> items[3:6]
        assert [item["event"] for item in data["items"]] == ["event-3", "event-4", "event-5"]


@pytest.mark.asyncio
async def test_list_activity_events_rejects_invalid_page_size(authenticated_client):
    resp = await authenticated_client.get(
        "/api/v1/activity/events", params={"page_size": 500},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_audit_log_persists_via_the_real_store(client):
    """End-to-end (no mocks): audit_log() should result in a fetchable event via
    store.get_activity_events(), not just a log line -- the actual gap this
    feature closes. Depends on the `client` fixture purely for its data_dir/
    tmp_path isolation (see conftest.py), not for the HTTP client itself."""
    import asyncio
    from app.audit import audit_log
    from app import store

    store._cache.clear()
    events_before = await store.get_activity_events()

    audit_log("test_event", actor_id="user123", detail="something happened")
    # audit_log() schedules persistence as a background task (fire-and-forget) --
    # give the event loop a beat to actually run it before asserting.
    await asyncio.sleep(0.05)

    events_after = await store.get_activity_events()
    assert len(events_after) == len(events_before) + 1
    assert events_after[0]["event"] == "test_event"
    assert events_after[0]["actor_id"] == "user123"
    assert events_after[0]["detail"] == "something happened"
    assert "timestamp" in events_after[0]

    store._cache.clear()
