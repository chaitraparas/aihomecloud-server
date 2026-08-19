"""
Tests for wifi_manager.py's systemd instance-name escaping.

Found live 2026-07-15: connect_to_network() passed the raw sanitized SSID directly
as a systemd unit *instance* name (ahc-wifi-install@<raw>.service). A bare instance
can't contain a space at all (systemctl start would fail outright for any SSID with
one, since _safe_profile_name explicitly allows spaces), and a raw hyphen is misread
by the unit's %I as an *encoded slash* -- "My-Home-WiFi" arrives at the install
script as "My/Home/WiFi". Fixed by running the name through `systemd-escape` before
constructing the unit name; the .service file's ExecStart keeps %I (not %i) to
correctly reverse that exact encoding back to the original string.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_systemd_escape_calls_the_real_binary_and_returns_stripped_output():
    from app.wifi_manager import _systemd_escape

    with patch(
        "app.wifi_manager.run_command",
        new_callable=AsyncMock,
        return_value=(0, "My\\x2dHome\\x2dWiFi\n", ""),
    ) as mock_run:
        result = await _systemd_escape("My-Home-WiFi")

    mock_run.assert_awaited_once_with(["systemd-escape", "My-Home-WiFi"], timeout=5)
    assert result == "My\\x2dHome\\x2dWiFi"


@pytest.mark.asyncio
async def test_systemd_escape_raises_on_failure():
    from app.wifi_manager import _systemd_escape

    with patch(
        "app.wifi_manager.run_command",
        new_callable=AsyncMock,
        return_value=(1, "", "systemd-escape: command not found"),
    ):
        with pytest.raises(RuntimeError, match="systemd-escape failed"):
            await _systemd_escape("My-Home-WiFi")


@pytest.mark.asyncio
async def test_connect_to_network_starts_the_escaped_unit_not_the_raw_name(tmp_path, monkeypatch):
    """The systemctl call must use the systemd-escaped name (e.g. My\\x2dHome\\x2dWiFi),
    never the raw hyphenated/spaced SSID directly -- that's the exact bug found live."""
    import app.wifi_manager as wifi_manager

    monkeypatch.setattr(wifi_manager, "_STAGING_DIR", tmp_path)

    escape_calls = []

    async def fake_systemd_escape(value: str) -> str:
        escape_calls.append(value)
        return value.replace("-", "\\x2d").replace(" ", "\\x20")

    systemctl_calls = []

    async def fake_run_command(cmd, timeout=30):
        if cmd[:2] == ["systemctl", "start"]:
            systemctl_calls.append(cmd[2])
            return (0, "", "")
        if cmd[:2] == ["nmcli", "connection"]:
            return (0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("app.wifi_manager._systemd_escape", side_effect=fake_systemd_escape), \
         patch("app.wifi_manager.run_command", side_effect=fake_run_command), \
         patch("app.wifi_manager.set_user_wifi_override", new_callable=AsyncMock), \
         patch("app.wifi_manager.get_wifi_connection_info", new_callable=AsyncMock,
               return_value={"connected": True, "ssid": "My-Home-WiFi", "ip": "192.168.0.50"}):
        result = await wifi_manager.connect_to_network("My-Home-WiFi", "hunter2")

    assert result["success"] is True
    assert escape_calls == ["My-Home-WiFi"]
    assert systemctl_calls == ["ahc-wifi-install@My\\x2dHome\\x2dWiFi.service"]
    # never the raw, unescaped instance -- that's the exact live bug
    assert "ahc-wifi-install@My-Home-WiFi.service" not in systemctl_calls


# ─── Hotspot (item 7) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_hotspot_status_reports_no_adapter_when_no_wifi_device():
    """Rock Pi 4A / the x86 thin client -- no wifi-type device in `nmcli device status` at
    all. Confirmed live this session: both boards' `nmcli device status` lists only
    ethernet/loopback devices."""
    from app.wifi_manager import get_hotspot_status

    with patch(
        "app.wifi_manager.run_command", new_callable=AsyncMock,
        return_value=(0, "eth0:ethernet\nlo:loopback", ""),
    ):
        status = await get_hotspot_status()

    assert status == {"adapter_present": False, "enabled": False, "ssid": None}


@pytest.mark.asyncio
async def test_get_hotspot_status_reports_disabled_when_wifi_in_client_mode():
    from app.wifi_manager import get_hotspot_status

    async def fake_run_command(cmd, timeout=10):
        if cmd == ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"]:
            return (0, "wlan0:wifi\nlo:loopback", "")
        if cmd == ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", "wlan0"]:
            return (0, "GENERAL.CONNECTION:Neo6G", "")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("app.wifi_manager.run_command", side_effect=fake_run_command):
        status = await get_hotspot_status()

    assert status == {"adapter_present": True, "enabled": False, "ssid": None}


@pytest.mark.asyncio
async def test_get_hotspot_status_reports_enabled_with_ssid_when_active():
    from app.wifi_manager import get_hotspot_status

    async def fake_run_command(cmd, timeout=10):
        if cmd == ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"]:
            return (0, "wlan0:wifi", "")
        if cmd == ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", "wlan0"]:
            return (0, "GENERAL.CONNECTION:ahc-hotspot", "")
        if cmd == ["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", "ahc-hotspot"]:
            return (0, "802-11-wireless.ssid:MyHotspot", "")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("app.wifi_manager.run_command", side_effect=fake_run_command):
        status = await get_hotspot_status()

    assert status == {"adapter_present": True, "enabled": True, "ssid": "MyHotspot"}


@pytest.mark.asyncio
async def test_enable_hotspot_rejects_empty_ssid():
    from app.wifi_manager import enable_hotspot

    result = await enable_hotspot("", "somepassword")

    assert result == {"success": False, "message": "SSID cannot be empty"}


@pytest.mark.asyncio
async def test_enable_hotspot_rejects_short_password():
    from app.wifi_manager import enable_hotspot

    result = await enable_hotspot("MyHotspot", "short")

    assert result["success"] is False
    assert "8 characters" in result["message"]


@pytest.mark.asyncio
async def test_enable_hotspot_fails_cleanly_with_no_wifi_radio():
    """The expected result on Rock Pi 4A / the x86 thin client -- no WiFi hardware to put into
    AP mode, confirmed live. Must not attempt the nmcli hotspot command at all."""
    from app.wifi_manager import enable_hotspot

    with patch(
        "app.wifi_manager.run_command", new_callable=AsyncMock,
        return_value=(0, "eth0:ethernet", ""),
    ) as mock_run:
        result = await enable_hotspot("MyHotspot", "hunter2pass")

    assert result == {"success": False, "message": "This device has no WiFi radio"}
    mock_run.assert_awaited_once()  # only the interface lookup, nothing else


@pytest.mark.asyncio
async def test_enable_hotspot_stages_config_and_starts_the_escaped_unit(tmp_path, monkeypatch):
    """`nmcli device wifi hotspot` is root-only at the D-Bus policy level for the unprivileged
    aihomecloud user (confirmed live 2026-07-19, same class of bug as
    Settings.ReloadConnections) -- enable_hotspot must go through the ahc-hotspot-enable@
    host-namespace root helper, staging iface/ssid/password to a file for it to read, never
    call `nmcli device wifi hotspot` directly from inside the service."""
    import app.wifi_manager as wifi_manager
    from app.wifi_manager import enable_hotspot

    monkeypatch.setattr(wifi_manager, "_HOTSPOT_STAGING_DIR", tmp_path)

    escape_calls = []

    async def fake_systemd_escape(value: str) -> str:
        escape_calls.append(value)
        return value.replace("-", "\\x2d")

    systemctl_calls = []
    staged_contents = {}

    async def fake_run_command(cmd, timeout=30):
        if cmd == ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"]:
            return (0, "wlan0:wifi", "")
        if cmd == ["nmcli", "-t", "-f", "DEVICE,STATE", "device", "status"]:
            # Radio already "ready" on the very first poll -- test_wait_for_wifi_device_ready_*
            # below covers the actual polling/retry/timeout behavior in isolation.
            return (0, "wlan0:connected", "")
        if cmd[:2] == ["systemctl", "start"]:
            unit = cmd[2]
            systemctl_calls.append(unit)
            # Read back whatever was staged, matching what ahc-hotspot.sh itself would read.
            for f in tmp_path.iterdir():
                staged_contents[f.name] = f.read_text()
            return (0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("app.wifi_manager._systemd_escape", side_effect=fake_systemd_escape), \
         patch("app.wifi_manager.run_command", side_effect=fake_run_command), \
         patch("app.wifi_manager.set_user_wifi_override", new_callable=AsyncMock) as mock_override, \
         patch("app.wifi_manager.enable_wifi", new_callable=AsyncMock) as mock_enable_wifi:
        result = await enable_hotspot("My-Hotspot", "hunter2pass")

    assert result == {"success": True, "message": "Hotspot started"}
    assert systemctl_calls == ["ahc-hotspot-enable@My\\x2dHotspot.service"]
    assert staged_contents == {"ahc-hotspot-My-Hotspot.conf": "wlan0\nMy-Hotspot\nhunter2pass\n"}
    # override must be set, and the radio explicitly turned on, BEFORE the hotspot command --
    # found live 2026-07-19: on a board where Ethernet is active, the auto-disable-wifi policy
    # has already turned the radio off, and `nmcli device wifi hotspot` fails outright on a
    # disabled radio ("device is not available").
    mock_override.assert_awaited_once_with(True)
    mock_enable_wifi.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_wifi_device_ready_polls_until_no_longer_unavailable():
    """`nmcli radio wifi on` returns before the device is actually usable (found live
    2026-07-19, real device confirmed on the Cubie A5E: ~2-3s to cycle unavailable ->
    disconnected -> connected) -- must poll, not assume it's immediate."""
    from app.wifi_manager import _wait_for_wifi_device_ready

    responses = iter(["wlan0:unavailable", "wlan0:unavailable", "wlan0:disconnected"])

    async def fake_run_command(cmd, timeout=30):
        return (0, next(responses), "")

    with patch("app.wifi_manager.run_command", side_effect=fake_run_command), \
         patch("app.wifi_manager.asyncio.sleep", new_callable=AsyncMock):
        ready = await _wait_for_wifi_device_ready("wlan0")

    assert ready is True


@pytest.mark.asyncio
async def test_wait_for_wifi_device_ready_times_out_if_still_unavailable():
    from app.wifi_manager import _wait_for_wifi_device_ready

    async def fake_run_command(cmd, timeout=30):
        return (0, "wlan0:unavailable", "")

    with patch("app.wifi_manager.run_command", side_effect=fake_run_command), \
         patch("app.wifi_manager.asyncio.sleep", new_callable=AsyncMock):
        ready = await _wait_for_wifi_device_ready("wlan0", attempts=3)

    assert ready is False


@pytest.mark.asyncio
async def test_enable_hotspot_fails_cleanly_if_radio_never_becomes_ready(monkeypatch):
    import app.wifi_manager as wifi_manager
    from app.wifi_manager import enable_hotspot

    async def fake_run_command(cmd, timeout=30):
        if cmd == ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"]:
            return (0, "wlan0:wifi", "")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("app.wifi_manager.run_command", side_effect=fake_run_command), \
         patch("app.wifi_manager.set_user_wifi_override", new_callable=AsyncMock), \
         patch("app.wifi_manager.enable_wifi", new_callable=AsyncMock), \
         patch("app.wifi_manager._wait_for_wifi_device_ready", new_callable=AsyncMock, return_value=False):
        result = await enable_hotspot("MyHotspot", "hunter2pass")

    assert result == {"success": False, "message": "WiFi radio did not become ready in time"}


@pytest.mark.asyncio
async def test_enable_hotspot_open_network_stages_empty_password(tmp_path, monkeypatch):
    import app.wifi_manager as wifi_manager
    from app.wifi_manager import enable_hotspot

    monkeypatch.setattr(wifi_manager, "_HOTSPOT_STAGING_DIR", tmp_path)

    async def fake_run_command(cmd, timeout=30):
        if cmd == ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"]:
            return (0, "wlan0:wifi", "")
        if cmd == ["nmcli", "-t", "-f", "DEVICE,STATE", "device", "status"]:
            return (0, "wlan0:connected", "")
        if cmd[:2] == ["systemctl", "start"]:
            return (0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("app.wifi_manager._systemd_escape", new_callable=AsyncMock, return_value="OpenHotspot"), \
         patch("app.wifi_manager.run_command", side_effect=fake_run_command), \
         patch("app.wifi_manager.set_user_wifi_override", new_callable=AsyncMock), \
         patch("app.wifi_manager.enable_wifi", new_callable=AsyncMock):
        result = await enable_hotspot("OpenHotspot", "")

    assert result["success"] is True
    staged = list(tmp_path.iterdir())
    assert len(staged) == 1
    assert staged[0].read_text() == "wlan0\nOpenHotspot\n\n"


@pytest.mark.asyncio
async def test_disable_hotspot_success_and_failure():
    from app.wifi_manager import disable_hotspot

    with patch("app.wifi_manager.run_command", new_callable=AsyncMock, return_value=(0, "", "")) as mock_run:
        assert await disable_hotspot() is True
    mock_run.assert_awaited_once_with(["systemctl", "start", "ahc-hotspot-disable.service"], timeout=15)

    with patch("app.wifi_manager.run_command", new_callable=AsyncMock, return_value=(1, "", "unit not found")):
        assert await disable_hotspot() is False


# ---------------------------------------------------------------------------
# Control characters in an SSID or passphrase (2026-08-08 audit)
#
# Both staged files are read by ROOT helpers, and both interpolated these values verbatim, so a
# newline changed the structure of what root parsed. Found while sweeping the whole
# root-consumes-service-writable-path class rather than by the council, which only looked at the
# two instances it already knew about.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,label",
    [
        ("Home\nSUPERSECRET", "newline"),
        ("Home\r\nid=evil", "CRLF"),
        ("Home\x00pwn", "NUL"),
        ("Home\x1b[2J", "escape"),
        ("Home\x7f", "DEL"),
    ],
)
def test_reject_unsafe_value_refuses_control_characters(value, label):
    from app.wifi_manager import UnsafeWifiValue, _reject_unsafe_value

    with pytest.raises(UnsafeWifiValue):
        _reject_unsafe_value(value, field="SSID", max_bytes=32)


def test_reject_unsafe_value_allows_a_realistic_ssid():
    """Spaces and punctuation are ordinary in real SSIDs — over-refusing breaks real networks."""
    from app.wifi_manager import _reject_unsafe_value

    for ok in ["My Home 5G", "café-wifi", "Paras' iPhone", "TP-Link_A1B2"]:
        _reject_unsafe_value(ok, field="SSID", max_bytes=32)


def test_reject_unsafe_value_enforces_the_802_11_length_limit():
    from app.wifi_manager import UnsafeWifiValue, _reject_unsafe_value

    with pytest.raises(UnsafeWifiValue):
        _reject_unsafe_value("x" * 33, field="SSID", max_bytes=32)
    # Multi-byte characters count as BYTES, not code points — 32 emoji is far past the limit.
    with pytest.raises(UnsafeWifiValue):
        _reject_unsafe_value("😀" * 32, field="SSID", max_bytes=32)


@pytest.mark.asyncio
async def test_hotspot_refuses_a_newline_ssid_instead_of_opening_an_unprotected_network(tmp_path,
                                                                                        monkeypatch):
    """
    The hotspot config is iface/ssid/password on three lines, read back with `sed -n '<n>p'`.

    An SSID ending in a newline pushes the password down to line 4, leaving line 3 — the field the
    root helper treats as the password — EMPTY. The helper then takes the no-password branch and
    brings the hotspot up as an OPEN network, while the app reports the password was applied. A
    silent downgrade to an unprotected network, which is why this is refused rather than stripped.
    """
    from app import wifi_manager

    monkeypatch.setattr(wifi_manager, "_HOTSPOT_STAGING_DIR", tmp_path)
    monkeypatch.setattr(wifi_manager, "_find_wifi_interface", AsyncMock(return_value="wlan0"))
    monkeypatch.setattr(wifi_manager, "set_user_wifi_override", AsyncMock())
    monkeypatch.setattr(wifi_manager, "enable_wifi", AsyncMock())
    monkeypatch.setattr(wifi_manager, "_wait_for_wifi_device_ready", AsyncMock(return_value=True))

    with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_run:
        result = await wifi_manager.enable_hotspot("Home\nx", "realpassword")

    assert result["success"] is False
    assert "control characters" in result["message"]
    mock_run.assert_not_awaited()          # nothing was asked of root at all
    assert list(tmp_path.iterdir()) == []  # and nothing was staged for root to read


@pytest.mark.asyncio
async def test_connect_refuses_an_ssid_that_would_inject_keyfile_sections(tmp_path, monkeypatch):
    """
    connect_to_network builds a NetworkManager keyfile by interpolating the SSID verbatim, and root
    installs the result into /etc/NetworkManager/system-connections/ as 0600 root:root. A newline
    in the SSID therefore adds arbitrary keys and sections to a root-owned system config file.
    """
    from app import wifi_manager

    monkeypatch.setattr(wifi_manager, "_STAGING_DIR", tmp_path)

    with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_run:
        result = await wifi_manager.connect_to_network(
            "Home\npermissions=user:root:\n[vpn]\nservice-type=pwn", "password123"
        )

    assert result["success"] is False
    assert "control characters" in result["message"]
    mock_run.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []
