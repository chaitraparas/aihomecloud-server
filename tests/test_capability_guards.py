"""
Capability guards on routes, and the 501 they produce.

These exist because the failure they prevent is invisible in a passing test suite: on a host
without the hardware, an unguarded route reaches a missing binary and surfaces as a confusing
subprocess error several layers down — or, on the fleet today, silently does nothing useful.
"""

import pytest
from httpx import AsyncClient

from app import platform_profile as pp
from app.platform_profile import Capability


@pytest.fixture(autouse=True)
def _reset():
    pp._reset_for_tests()
    yield
    pp._reset_for_tests()


def _no_radios(monkeypatch):
    """The real ROCK Pi shape: tools installed, hardware absent."""
    monkeypatch.setattr(pp, "_has_wifi_radio", lambda: False)
    monkeypatch.setattr(pp, "_has_bluetooth_controller", lambda: False)
    monkeypatch.setenv("AHC_PLATFORM", "linux_sbc")
    pp._reset_for_tests()


def _full_board(monkeypatch):
    monkeypatch.setattr(pp, "_has_wifi_radio", lambda: True)
    monkeypatch.setattr(pp, "_has_bluetooth_controller", lambda: True)
    monkeypatch.setattr(pp.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setenv("AHC_PLATFORM", "linux_sbc")
    pp._reset_for_tests()


@pytest.mark.asyncio
async def test_wifi_change_is_refused_on_a_board_with_no_radio(
    authenticated_client: AsyncClient, monkeypatch
):
    _no_radios(monkeypatch)
    resp = await authenticated_client.put("/api/v1/network/wifi", json={"enabled": True})
    assert resp.status_code == 501
    body = resp.json()
    # The client needs to know WHICH capability, so it can disable that control specifically
    # rather than treating the whole board as broken.
    assert body["capability"] == "network_radios"
    assert "host" in body


@pytest.mark.asyncio
async def test_bluetooth_power_is_refused_without_a_controller(
    authenticated_client: AsyncClient, monkeypatch
):
    _no_radios(monkeypatch)
    resp = await authenticated_client.put("/api/v1/bluetooth/power", json={"enabled": True})
    assert resp.status_code == 501
    assert resp.json()["capability"] == "bluetooth"


@pytest.mark.asyncio
async def test_the_guard_runs_before_any_work(authenticated_client: AsyncClient, monkeypatch):
    """
    A guard placed after the handler has started doing things is not a guard. Refusing must
    not depend on nmcli being absent — on this fleet nmcli is present and the radio is not.
    """
    _no_radios(monkeypatch)
    monkeypatch.setattr(pp.shutil, "which", lambda n: f"/usr/bin/{n}")  # tools ARE installed
    pp._reset_for_tests()
    resp = await authenticated_client.post("/api/v1/network/wifi/forget", json={"ssid": "x"})
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_a_capable_board_is_not_refused(authenticated_client: AsyncClient, monkeypatch):
    # The guard must not become a blanket denial — that would break the Cubie.
    _full_board(monkeypatch)
    resp = await authenticated_client.get("/api/v1/network/wifi/scan")
    assert resp.status_code != 501


@pytest.mark.asyncio
async def test_capabilities_endpoint_reports_both_sides(
    authenticated_client: AsyncClient, monkeypatch
):
    _no_radios(monkeypatch)
    monkeypatch.setattr(pp.shutil, "which", lambda n: f"/usr/bin/{n}")
    pp._reset_for_tests()
    resp = await authenticated_client.get("/api/v1/system/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert "network_radios" in body["missing"]
    assert "bluetooth" in body["missing"]
    assert "raw_disk_ops" in body["capabilities"]
    assert body["host"] == "linux_sbc"


@pytest.mark.asyncio
async def test_capabilities_requires_authentication(client: AsyncClient):
    # It describes the hardware fairly precisely; no reason to hand that to an unpaired caller.
    resp = await client.get("/api/v1/system/capabilities")
    assert resp.status_code in (401, 403)
