"""
Tests for bluetooth_manager.py's bluetoothctl scripting -- power toggle, device list parsing,
scan (via external `timeout`), pair/connect, and the adapter_present distinction that backs the
"No Bluetooth adapter" UI state (Rock Pi 4A / the x86 thin client have no controller at all in
this fleet, confirmed live this session).
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_is_adapter_present_true_when_bluetoothctl_lists_a_controller():
    from app.bluetooth_manager import is_adapter_present

    with patch(
        "app.bluetooth_manager.run_command", new_callable=AsyncMock,
        return_value=(0, "Controller AA:BB:CC:DD:EE:FF cubie [default]", ""),
    ):
        assert await is_adapter_present() is True


@pytest.mark.asyncio
async def test_is_adapter_present_false_when_no_controller():
    """Rock Pi 4A / the x86 thin client: `bluetoothctl list` returns rc=0 but empty output
    (or bluetoothctl itself isn't meaningfully usable) -- no controller present."""
    from app.bluetooth_manager import is_adapter_present

    with patch("app.bluetooth_manager.run_command", new_callable=AsyncMock, return_value=(0, "", "")):
        assert await is_adapter_present() is False


@pytest.mark.asyncio
async def test_get_power_state_parses_powered_yes_and_no():
    from app.bluetooth_manager import get_power_state

    with patch(
        "app.bluetooth_manager.run_command", new_callable=AsyncMock,
        return_value=(0, "Controller AA:BB:CC:DD:EE:FF\n\tPowered: yes\n\tDiscoverable: no", ""),
    ):
        assert await get_power_state() is True

    with patch(
        "app.bluetooth_manager.run_command", new_callable=AsyncMock,
        return_value=(0, "Controller AA:BB:CC:DD:EE:FF\n\tPowered: no", ""),
    ):
        assert await get_power_state() is False


@pytest.mark.asyncio
async def test_set_power_calls_bluetoothctl_power_on_and_off():
    from app.bluetooth_manager import set_power

    with patch("app.bluetooth_manager.run_command", new_callable=AsyncMock, return_value=(0, "", "")) as mock_run:
        assert await set_power(True) is True
    mock_run.assert_awaited_once_with(["bluetoothctl", "power", "on"], timeout=10)

    with patch("app.bluetooth_manager.run_command", new_callable=AsyncMock, return_value=(0, "", "")) as mock_run:
        assert await set_power(False) is True
    mock_run.assert_awaited_once_with(["bluetoothctl", "power", "off"], timeout=10)


@pytest.mark.asyncio
async def test_set_power_returns_false_on_failure():
    from app.bluetooth_manager import set_power

    with patch(
        "app.bluetooth_manager.run_command", new_callable=AsyncMock,
        return_value=(1, "", "org.bluez.Error.Failed"),
    ):
        assert await set_power(True) is False


@pytest.mark.asyncio
async def test_list_devices_parses_device_lines_and_fetches_status():
    from app.bluetooth_manager import list_devices

    async def fake_run_command(cmd, timeout=10):
        if cmd == ["bluetoothctl", "devices"]:
            return (0, "Device AA:BB:CC:DD:EE:01 Living Room Speaker\nDevice AA:BB:CC:DD:EE:02 Phone", "")
        if cmd == ["bluetoothctl", "info", "AA:BB:CC:DD:EE:01"]:
            return (0, "\tPaired: yes\n\tConnected: yes", "")
        if cmd == ["bluetoothctl", "info", "AA:BB:CC:DD:EE:02"]:
            return (0, "\tPaired: yes\n\tConnected: no", "")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("app.bluetooth_manager.run_command", side_effect=fake_run_command):
        devices = await list_devices()

    assert devices == [
        {"address": "AA:BB:CC:DD:EE:01", "name": "Living Room Speaker", "paired": True, "connected": True},
        {"address": "AA:BB:CC:DD:EE:02", "name": "Phone", "paired": True, "connected": False},
    ]


@pytest.mark.asyncio
async def test_list_devices_returns_empty_on_failure():
    from app.bluetooth_manager import list_devices

    with patch(
        "app.bluetooth_manager.run_command", new_callable=AsyncMock,
        return_value=(1, "", "No default controller available"),
    ):
        assert await list_devices() == []


@pytest.mark.asyncio
async def test_scan_runs_under_external_timeout_then_lists_devices():
    """Scanning has no synchronous one-shot subcommand of its own -- `bluetoothctl scan on` is
    run under the external `timeout` command and killed after N seconds, then the resulting
    device list is read back separately."""
    from app.bluetooth_manager import scan

    calls = []

    async def fake_run_command(cmd, timeout=None):
        calls.append(cmd)
        if cmd[:2] == ["timeout", "10"]:
            return (124, "", "")  # timeout's own exit code when it had to kill the child
        if cmd == ["bluetoothctl", "devices"]:
            return (0, "Device AA:BB:CC:DD:EE:01 Headphones", "")
        if cmd[:2] == ["bluetoothctl", "info"]:
            return (0, "\tPaired: no\n\tConnected: no", "")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("app.bluetooth_manager.run_command", side_effect=fake_run_command):
        devices = await scan(seconds=10)

    assert calls[0] == ["timeout", "10", "bluetoothctl", "scan", "on"]
    assert devices == [{"address": "AA:BB:CC:DD:EE:01", "name": "Headphones", "paired": False, "connected": False}]


@pytest.mark.asyncio
async def test_scan_clamps_duration_to_3_to_30_seconds():
    from app.bluetooth_manager import scan

    calls = []

    async def fake_run_command(cmd, timeout=None):
        calls.append(cmd)
        if cmd[0] == "timeout":
            return (124, "", "")
        return (0, "", "")

    with patch("app.bluetooth_manager.run_command", side_effect=fake_run_command):
        await scan(seconds=1)
        await scan(seconds=999)

    assert calls[0] == ["timeout", "3", "bluetoothctl", "scan", "on"]
    assert calls[2] == ["timeout", "30", "bluetoothctl", "scan", "on"]


@pytest.mark.asyncio
async def test_pair_and_connect_report_success_and_failure():
    from app.bluetooth_manager import connect, pair

    with patch("app.bluetooth_manager.run_command", new_callable=AsyncMock, return_value=(0, "", "")):
        assert await pair("AA:BB:CC:DD:EE:01") is True
        assert await connect("AA:BB:CC:DD:EE:01") is True

    with patch(
        "app.bluetooth_manager.run_command", new_callable=AsyncMock,
        return_value=(1, "", "Failed to pair: org.bluez.Error.AuthenticationFailed"),
    ):
        assert await pair("AA:BB:CC:DD:EE:01") is False

    with patch(
        "app.bluetooth_manager.run_command", new_callable=AsyncMock,
        return_value=(1, "", "Failed to connect: org.bluez.Error.NotReady"),
    ):
        assert await connect("AA:BB:CC:DD:EE:01") is False


@pytest.mark.asyncio
async def test_get_status_reports_no_adapter_without_probing_further():
    """Rock Pi 4A / the x86 thin client's expected result -- confirmed live this session
    (`ls /sys/class/bluetooth/` is empty on both). Must not call power/device-list at all once
    adapter absence is known."""
    from app.bluetooth_manager import get_status

    with patch("app.bluetooth_manager.is_adapter_present", new_callable=AsyncMock, return_value=False) as mock_present, \
         patch("app.bluetooth_manager.get_power_state", new_callable=AsyncMock) as mock_power, \
         patch("app.bluetooth_manager.list_devices", new_callable=AsyncMock) as mock_list:
        status = await get_status()

    assert status == {"adapter_present": False, "enabled": False, "devices": []}
    mock_present.assert_awaited_once()
    mock_power.assert_not_awaited()
    mock_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_status_skips_device_list_when_radio_off():
    from app.bluetooth_manager import get_status

    with patch("app.bluetooth_manager.is_adapter_present", new_callable=AsyncMock, return_value=True), \
         patch("app.bluetooth_manager.get_power_state", new_callable=AsyncMock, return_value=False), \
         patch("app.bluetooth_manager.list_devices", new_callable=AsyncMock) as mock_list:
        status = await get_status()

    assert status == {"adapter_present": True, "enabled": False, "devices": []}
    mock_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_status_includes_devices_when_radio_on():
    from app.bluetooth_manager import get_status

    devices = [{"address": "AA:BB:CC:DD:EE:01", "name": "Phone", "paired": True, "connected": True}]
    with patch("app.bluetooth_manager.is_adapter_present", new_callable=AsyncMock, return_value=True), \
         patch("app.bluetooth_manager.get_power_state", new_callable=AsyncMock, return_value=True), \
         patch("app.bluetooth_manager.list_devices", new_callable=AsyncMock, return_value=devices):
        status = await get_status()

    assert status == {"adapter_present": True, "enabled": True, "devices": devices}
