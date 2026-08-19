"""
Tests for bluetooth_routes.py -- status, power toggle, scan, pair, connect.
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestBluetoothStatus:
    @pytest.mark.asyncio
    async def test_status_no_adapter(self, authenticated_client):
        """Rock Pi 4A / the x86 thin client's expected live response -- no Bluetooth
        controller present."""
        with patch("app.routes.bluetooth_routes.bluetooth_manager.get_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = {"adapter_present": False, "enabled": False, "devices": []}
            resp = await authenticated_client.get("/api/v1/bluetooth/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["adapterPresent"] is False
            assert data["enabled"] is False
            assert data["devices"] == []

    @pytest.mark.asyncio
    async def test_status_with_devices(self, authenticated_client):
        with patch("app.routes.bluetooth_routes.bluetooth_manager.get_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = {
                "adapter_present": True,
                "enabled": True,
                "devices": [
                    {"address": "AA:BB:CC:DD:EE:01", "name": "Phone", "paired": True, "connected": True},
                ],
            }
            resp = await authenticated_client.get("/api/v1/bluetooth/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["enabled"] is True
            assert data["devices"][0]["name"] == "Phone"
            assert data["devices"][0]["connected"] is True


class TestBluetoothPower:
    @pytest.mark.asyncio
    async def test_power_toggle_on(self, authenticated_client):
        with patch("app.routes.bluetooth_routes.bluetooth_manager.set_power", new_callable=AsyncMock) as mock_set, \
             patch("app.routes.bluetooth_routes.bluetooth_manager.get_status", new_callable=AsyncMock) as mock_status:
            mock_set.return_value = True
            mock_status.return_value = {"adapter_present": True, "enabled": True, "devices": []}
            resp = await authenticated_client.put("/api/v1/bluetooth/power", json={"enabled": True})
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["enabled"] is True
            mock_set.assert_awaited_once_with(True)


class TestBluetoothScan:
    @pytest.mark.asyncio
    async def test_scan_returns_discovered_devices(self, authenticated_client):
        with patch("app.routes.bluetooth_routes.bluetooth_manager.scan", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = [
                {"address": "AA:BB:CC:DD:EE:01", "name": "Headphones", "paired": False, "connected": False},
            ]
            resp = await authenticated_client.post("/api/v1/bluetooth/scan")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["name"] == "Headphones"

    @pytest.mark.asyncio
    async def test_scan_empty_when_nothing_found(self, authenticated_client):
        with patch("app.routes.bluetooth_routes.bluetooth_manager.scan", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = []
            resp = await authenticated_client.post("/api/v1/bluetooth/scan")
            assert resp.status_code == 200
            assert resp.json() == []


class TestBluetoothPairConnect:
    @pytest.mark.asyncio
    async def test_pair_success(self, authenticated_client):
        with patch("app.routes.bluetooth_routes.bluetooth_manager.pair", new_callable=AsyncMock) as mock_pair:
            mock_pair.return_value = True
            resp = await authenticated_client.post("/api/v1/bluetooth/pair", json={"address": "AA:BB:CC:DD:EE:01"})
            assert resp.status_code == 200
            assert resp.json() == {"success": True}
            mock_pair.assert_awaited_once_with("AA:BB:CC:DD:EE:01")

    @pytest.mark.asyncio
    async def test_connect_failure_reported(self, authenticated_client):
        with patch("app.routes.bluetooth_routes.bluetooth_manager.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = False
            resp = await authenticated_client.post("/api/v1/bluetooth/connect", json={"address": "AA:BB:CC:DD:EE:01"})
            assert resp.status_code == 200
            assert resp.json() == {"success": False}
