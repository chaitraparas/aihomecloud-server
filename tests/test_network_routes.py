"""
Tests for network_routes.py — WiFi status, toggle, LAN network status.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


class TestNetworkStatus:
    @pytest.mark.asyncio
    async def test_network_status(self, authenticated_client):
        """Test basic network status returns expected fields."""
        with patch("app.routes.network_routes.Path") as mock_path_cls, \
             patch("app.routes.network_routes.get_wifi_connection_info", new_callable=AsyncMock) as mock_conn:
            net_dir = MagicMock()
            net_dir.exists.return_value = False
            mock_path_cls.return_value = net_dir
            mock_conn.return_value = {"connected": False, "ssid": None, "ip": None}
            resp = await authenticated_client.get("/api/v1/network/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "lan_connected" in data or "lanConnected" in data

    @pytest.mark.asyncio
    async def test_network_status_reports_active_wifi_connection(self, authenticated_client):
        """Regression pin: wifiConnected/wifiSsid/wifiIp used to be hardcoded False/None
        regardless of actual state -- must now reflect a real active connection."""
        with patch("app.routes.network_routes.Path") as mock_path_cls, \
             patch("app.routes.network_routes.get_wifi_connection_info", new_callable=AsyncMock) as mock_conn:
            net_dir = MagicMock()
            net_dir.exists.return_value = False
            mock_path_cls.return_value = net_dir
            mock_conn.return_value = {"connected": True, "ssid": "HomeNetwork", "ip": "192.168.1.50"}
            resp = await authenticated_client.get("/api/v1/network/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["wifiConnected"] is True
            assert data["wifiSsid"] == "HomeNetwork"
            assert data["wifiIp"] == "192.168.1.50"


class TestWifiStatus:
    @pytest.mark.asyncio
    async def test_wifi_status(self, authenticated_client):
        with patch("app.routes.network_routes.get_wifi_status", new_callable=AsyncMock) as mock_ws:
            # Must match what wifi_manager.get_wifi_status actually returns. It emits
            # camelCase; this mock used to use snake_case, which no code path ever
            # produces, and the test still passed because nothing validated the shape.
            mock_ws.return_value = {
                "wifiEnabled": False,
                "ethernetUp": True,
                "userOverride": True,
            }
            resp = await authenticated_client.get("/api/v1/network/wifi")
            assert resp.status_code == 200
            assert resp.json() == {"wifiEnabled": False, "ethernetUp": True, "userOverride": True}


class TestToggleWifi:
    @pytest.mark.asyncio
    async def test_toggle_wifi(self, authenticated_client):
        with patch("app.routes.network_routes.set_user_wifi_override", new_callable=AsyncMock), \
             patch("app.routes.network_routes.get_wifi_status", new_callable=AsyncMock) as mock_ws:
            mock_ws.return_value = {
                "wifiEnabled": True,
                "ethernetUp": False,
                "userOverride": True,
            }
            resp = await authenticated_client.put(
                "/api/v1/network/wifi",
                json={"enabled": True},
            )
            assert resp.status_code == 200
            assert resp.json() == {"wifiEnabled": True, "ethernetUp": False, "userOverride": True}


class TestWifiScan:
    @pytest.mark.asyncio
    async def test_wifi_scan_returns_networks(self, authenticated_client):
        with patch("app.routes.network_routes.scan_networks", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = [
                {"ssid": "HomeNetwork", "signal": 80, "security": "WPA2", "in_use": True, "saved": True},
                {"ssid": "Neighbor", "signal": 40, "security": "WPA2", "in_use": False, "saved": False},
            ]
            resp = await authenticated_client.get("/api/v1/network/wifi/scan")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 2
            assert data[0]["ssid"] == "HomeNetwork"
            assert data[0]["inUse"] is True

    @pytest.mark.asyncio
    async def test_wifi_scan_empty_on_failure(self, authenticated_client):
        with patch("app.routes.network_routes.scan_networks", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = []
            resp = await authenticated_client.get("/api/v1/network/wifi/scan")
            assert resp.status_code == 200
            assert resp.json() == []


class TestWifiConnect:
    @pytest.mark.asyncio
    async def test_connect_success(self, authenticated_client):
        with patch("app.routes.network_routes.connect_to_network", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = {"success": True, "message": "Connected", "ip": "192.168.1.5"}
            resp = await authenticated_client.post(
                "/api/v1/network/wifi/connect", json={"ssid": "HomeNetwork", "password": "secret123"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["ip"] == "192.168.1.5"
            mock_connect.assert_awaited_once_with("HomeNetwork", "secret123")

    @pytest.mark.asyncio
    async def test_connect_failure_reported(self, authenticated_client):
        with patch("app.routes.network_routes.connect_to_network", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = {"success": False, "message": "Wrong password", "ip": None}
            resp = await authenticated_client.post(
                "/api/v1/network/wifi/connect", json={"ssid": "HomeNetwork", "password": "wrong"},
            )
            assert resp.status_code == 200
            assert resp.json()["success"] is False

    @pytest.mark.asyncio
    async def test_connect_open_network_no_password_required(self, authenticated_client):
        with patch("app.routes.network_routes.connect_to_network", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = {"success": True, "message": "Connected", "ip": "10.0.0.5"}
            resp = await authenticated_client.post(
                "/api/v1/network/wifi/connect", json={"ssid": "OpenNetwork"},
            )
            assert resp.status_code == 200
            mock_connect.assert_awaited_once_with("OpenNetwork", "")


class TestWifiForget:
    @pytest.mark.asyncio
    async def test_forget_success(self, authenticated_client):
        with patch("app.routes.network_routes.forget_network", new_callable=AsyncMock) as mock_forget:
            mock_forget.return_value = True
            resp = await authenticated_client.post("/api/v1/network/wifi/forget", json={"ssid": "OldNetwork"})
            assert resp.status_code == 200
            assert resp.json() == {"success": True}
            mock_forget.assert_awaited_once_with("OldNetwork")


class TestHotspotStatus:
    @pytest.mark.asyncio
    async def test_hotspot_status_no_adapter(self, authenticated_client):
        """Rock Pi 4A / the x86 thin client's expected live response -- no WiFi radio."""
        with patch("app.routes.network_routes.get_hotspot_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = {"adapter_present": False, "enabled": False, "ssid": None}
            resp = await authenticated_client.get("/api/v1/network/hotspot")
            assert resp.status_code == 200
            data = resp.json()
            assert data["adapterPresent"] is False
            assert data["enabled"] is False

    @pytest.mark.asyncio
    async def test_hotspot_status_enabled(self, authenticated_client):
        with patch("app.routes.network_routes.get_hotspot_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = {"adapter_present": True, "enabled": True, "ssid": "MyHotspot"}
            resp = await authenticated_client.get("/api/v1/network/hotspot")
            assert resp.status_code == 200
            data = resp.json()
            assert data["enabled"] is True
            assert data["ssid"] == "MyHotspot"


class TestHotspotEnable:
    @pytest.mark.asyncio
    async def test_enable_success(self, authenticated_client):
        with patch("app.routes.network_routes.enable_hotspot", new_callable=AsyncMock) as mock_enable:
            mock_enable.return_value = {"success": True, "message": "Hotspot started"}
            resp = await authenticated_client.post(
                "/api/v1/network/hotspot/enable", json={"ssid": "MyHotspot", "password": "hunter2pass"},
            )
            assert resp.status_code == 200
            assert resp.json()["success"] is True
            mock_enable.assert_awaited_once_with("MyHotspot", "hunter2pass")

    @pytest.mark.asyncio
    async def test_enable_no_wifi_radio_reported_cleanly(self, authenticated_client):
        with patch("app.routes.network_routes.enable_hotspot", new_callable=AsyncMock) as mock_enable:
            mock_enable.return_value = {"success": False, "message": "This device has no WiFi radio"}
            resp = await authenticated_client.post(
                "/api/v1/network/hotspot/enable", json={"ssid": "MyHotspot", "password": "hunter2pass"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "no WiFi radio" in data["message"]


class TestHotspotDisable:
    @pytest.mark.asyncio
    async def test_disable_success(self, authenticated_client):
        with patch("app.routes.network_routes.disable_hotspot", new_callable=AsyncMock) as mock_disable:
            mock_disable.return_value = True
            resp = await authenticated_client.post("/api/v1/network/hotspot/disable")
            assert resp.status_code == 200
            assert resp.json()["success"] is True
