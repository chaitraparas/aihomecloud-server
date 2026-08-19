"""
Identity reported by the unauthenticated endpoints.
"""

import pytest

from app import main, store


class TestDeviceNameIsTheRenamedOne:
    """
    `/api/health` and `/` must report the name the user set, not the install-time default.

    These endpoints exist partly so a client probing several addresses can tell devices apart —
    health's own docstring says so. Reporting the default defeats that for exactly the users who
    bothered to rename anything. Found on real hardware 2026-08-05: a board renamed `radxa-nas`
    still announced `My AiHomeCloud` on both endpoints while `/api/v1/system/info` was correct.
    """

    @pytest.mark.asyncio
    async def test_health_reports_the_renamed_device(self, client, monkeypatch):
        async def renamed():
            return {"name": "radxa-nas"}
        monkeypatch.setattr(store, "get_device_state", renamed)

        resp = await client.get("/api/health")

        assert resp.status_code == 200
        assert resp.json()["deviceName"] == "radxa-nas"

    @pytest.mark.asyncio
    async def test_root_reports_the_renamed_device(self, client, monkeypatch):
        async def renamed():
            return {"name": "radxa-nas"}
        monkeypatch.setattr(store, "get_device_state", renamed)

        resp = await client.get("/")

        assert resp.status_code == 200
        assert resp.json()["deviceName"] == "radxa-nas"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_configured_default_when_never_renamed(self, client, monkeypatch):
        async def never_renamed():
            return {}
        monkeypatch.setattr(store, "get_device_state", never_renamed)

        resp = await client.get("/api/health")

        assert resp.status_code == 200
        assert resp.json()["deviceName"] == main.settings.device_name
