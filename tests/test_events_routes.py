"""
Tests for events_routes.py — activation-funnel telemetry. See
kb/telemetry_architecture_decision.md for the architecture this implements.
"""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_record_event_requires_auth(client):
    resp = await client.post("/api/v1/events", json={"eventName": "app_installed"})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_funnel_requires_auth(client):
    resp = await client.get("/api/v1/events/funnel")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_funnel_requires_admin(client, member_token):
    client.headers.update({"Authorization": f"Bearer {member_token}"})
    resp = await client.get("/api/v1/events/funnel")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_record_event_rejects_unknown_event_name(authenticated_client):
    resp = await authenticated_client.post(
        "/api/v1/events", json={"eventName": "definitely_not_a_real_event"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_record_event_accepts_a_known_event_name(authenticated_client):
    with patch("app.routes.events_routes.store") as mock_store:
        mock_store.append_funnel_event = AsyncMock()
        resp = await authenticated_client.post(
            "/api/v1/events", json={"eventName": "first_backup_completed"},
        )
        assert resp.status_code == 201
        mock_store.append_funnel_event.assert_awaited_once()
        recorded = mock_store.append_funnel_event.await_args.args[0]
        assert recorded["event"] == "first_backup_completed"
        assert "timestamp" in recorded


@pytest.mark.asyncio
async def test_funnel_counts_tally_by_event_name(authenticated_client):
    fake_events = (
        [{"event": "app_installed", "timestamp": 1}] * 5
        + [{"event": "first_backup_completed", "timestamp": 2}] * 2
    )
    with patch("app.routes.events_routes.store") as mock_store:
        mock_store.get_funnel_events = AsyncMock(return_value=fake_events)
        resp = await authenticated_client.get("/api/v1/events/funnel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["counts"] == {"app_installed": 5, "first_backup_completed": 2}
        assert data["totalEvents"] == 7


@pytest.mark.asyncio
async def test_funnel_event_persists_via_the_real_store(authenticated_client):
    """End-to-end (no mocks): posting an event should result in a fetchable count via
    store.get_funnel_events() -- the same real-persistence discipline as
    test_activity_routes.py's equivalent test, not just checking the HTTP response shape."""
    from app import store

    store._cache.clear()
    before = await store.get_funnel_events()

    resp = await authenticated_client.post(
        "/api/v1/events", json={"eventName": "server_connected"},
    )
    assert resp.status_code == 201

    after = await store.get_funnel_events()
    assert len(after) == len(before) + 1
    assert after[0]["event"] == "server_connected"
    assert after[0]["username"]  # JWT subject is a generated user ID, not the display name

    store._cache.clear()
