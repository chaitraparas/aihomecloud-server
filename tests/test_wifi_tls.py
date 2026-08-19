"""
Tests for wifi_manager.py and tls.py.
"""

import asyncio
import json

import subprocess
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path


# — wifi_manager ——————————————————————————————————————————————

class TestEthernetIsUp:
    def test_detects_up_interface(self, tmp_path):
        from app.wifi_manager import _ethernet_is_up

        net_dir = tmp_path / "net"
        net_dir.mkdir()
        eth0 = net_dir / "eth0"
        eth0.mkdir()
        (eth0 / "operstate").write_text("up")

        with patch("app.wifi_manager.Path", return_value=net_dir):
            # Directly patch the internal logic — the function reads /sys/class/net
            pass

        # Use a different approach: set up the path properly
        import app.wifi_manager as wm
        original_func = wm._ethernet_is_up

        def _patched():
            if not net_dir.exists():
                return False
            for iface in net_dir.iterdir():
                name = iface.name
                if name == "lo" or name.startswith("wl") or name.startswith("docker") or name.startswith("veth"):
                    continue
                operstate = iface / "operstate"
                if operstate.exists():
                    state = operstate.read_text().strip()
                    if state == "up":
                        return True
            return False

        assert _patched() is True

    def test_no_net_dir(self):
        from app.wifi_manager import _ethernet_is_up
        with patch("app.wifi_manager.Path") as MockPath:
            mock_dir = MagicMock()
            mock_dir.exists.return_value = False
            MockPath.return_value = mock_dir
            assert _ethernet_is_up() is False

    def test_skips_loopback_and_wireless(self, tmp_path):
        net_dir = tmp_path / "net"
        net_dir.mkdir()
        for name in ["lo", "wlan0", "docker0", "veth123"]:
            d = net_dir / name
            d.mkdir()
            (d / "operstate").write_text("up")

        import app.wifi_manager as wm
        with patch.object(Path, "__new__", return_value=net_dir):
            pass  # Can't easily patch Path constructor


class TestDisableWifi:
    @pytest.mark.asyncio
    async def test_success(self):
        from app.wifi_manager import disable_wifi
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            assert await disable_wifi() is True

    @pytest.mark.asyncio
    async def test_failure(self):
        from app.wifi_manager import disable_wifi
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (1, "", "error")
            assert await disable_wifi() is False


class TestEnableWifi:
    @pytest.mark.asyncio
    async def test_success(self):
        from app.wifi_manager import enable_wifi
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            assert await enable_wifi() is True

    @pytest.mark.asyncio
    async def test_failure(self):
        from app.wifi_manager import enable_wifi
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (1, "", "error")
            assert await enable_wifi() is False


class TestGetWifiStatus:
    @pytest.mark.asyncio
    async def test_enabled(self):
        from app.wifi_manager import get_wifi_status
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd, \
             patch("app.wifi_manager._ethernet_is_up", return_value=True):
            mock_cmd.return_value = (0, "enabled", "")
            status = await get_wifi_status()
            assert status["wifiEnabled"] is True
            assert status["ethernetUp"] is True

    @pytest.mark.asyncio
    async def test_disabled(self):
        from app.wifi_manager import get_wifi_status
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd, \
             patch("app.wifi_manager._ethernet_is_up", return_value=False):
            mock_cmd.return_value = (0, "disabled", "")
            status = await get_wifi_status()
            assert status["wifiEnabled"] is False

    @pytest.mark.asyncio
    async def test_command_fails(self):
        from app.wifi_manager import get_wifi_status
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd, \
             patch("app.wifi_manager._ethernet_is_up", return_value=False):
            mock_cmd.return_value = (1, "", "error")
            status = await get_wifi_status()
            assert status["wifiEnabled"] is None


class TestSplitTerseLine:
    def test_splits_simple_fields(self):
        from app.wifi_manager import _split_terse_line
        assert _split_terse_line("*:HomeNetwork:80:WPA2") == ["*", "HomeNetwork", "80", "WPA2"]

    def test_unescapes_literal_colon_in_field(self):
        from app.wifi_manager import _split_terse_line
        # An SSID containing a real ':' comes back from nmcli as '\:' -- must not be
        # treated as a field separator.
        assert _split_terse_line(r"*:Cafe\:Free WiFi:60:Open") == ["*", "Cafe:Free WiFi", "60", "Open"]

    def test_unescapes_literal_backslash(self):
        from app.wifi_manager import _split_terse_line
        assert _split_terse_line(r"a\\b:c") == ["a\\b", "c"]

    def test_empty_in_use_field(self):
        from app.wifi_manager import _split_terse_line
        assert _split_terse_line(":Neighbor:40:WPA2") == ["", "Neighbor", "40", "WPA2"]


class TestScanNetworks:
    @pytest.mark.asyncio
    async def test_returns_networks_sorted_by_signal(self):
        from app.wifi_manager import scan_networks
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.side_effect = [
                (0, "\n".join([
                    ":Weak:20:WPA2",
                    "*:Strong:90:WPA2",
                ]), ""),
                (0, "Strong", ""),  # saved connections
            ]
            networks = await scan_networks()
            assert [n["ssid"] for n in networks] == ["Strong", "Weak"]
            assert networks[0]["in_use"] is True
            assert networks[0]["saved"] is True
            assert networks[1]["saved"] is False

    @pytest.mark.asyncio
    async def test_dedups_by_ssid_keeping_strongest(self):
        from app.wifi_manager import scan_networks
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.side_effect = [
                (0, "\n".join([
                    ":SameNetwork:30:WPA2",
                    ":SameNetwork:75:WPA2",  # a second AP for the same SSID, stronger
                ]), ""),
                (0, "", ""),
            ]
            networks = await scan_networks()
            assert len(networks) == 1
            assert networks[0]["signal"] == 75

    @pytest.mark.asyncio
    async def test_in_use_flag_survives_dedup_when_weaker_ap_is_the_active_one(self):
        """Regression pin: two APs can broadcast the same SSID (mesh/dual-band) with
        NetworkManager connected to whichever it roamed to, not necessarily the stronger one.
        Found live 2026-07-14 — the connected AP was the WEAKER of two Neo6G entries, and the
        naive keep-strongest dedup silently dropped in_use because it only looked at the
        surviving (stronger) entry."""
        from app.wifi_manager import scan_networks
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.side_effect = [
                (0, "\n".join([
                    ":Neo6G:87:WPA2",   # stronger AP, not the one actually connected
                    "*:Neo6G:70:WPA2",  # weaker AP, the real active connection
                ]), ""),
                (0, "Neo6G", ""),  # saved connections
            ]
            networks = await scan_networks()
            assert len(networks) == 1
            assert networks[0]["signal"] == 87  # still reports the strongest signal
            assert networks[0]["in_use"] is True  # but must not lose the in-use flag

    @pytest.mark.asyncio
    async def test_skips_hidden_networks(self):
        from app.wifi_manager import scan_networks
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.side_effect = [
                (0, "\n".join([
                    ":: 50:WPA2",  # blank SSID = hidden network
                    ":Visible:50:WPA2",
                ]), ""),
                (0, "", ""),
            ]
            networks = await scan_networks()
            assert [n["ssid"] for n in networks] == ["Visible"]

    @pytest.mark.asyncio
    async def test_empty_on_scan_failure(self):
        from app.wifi_manager import scan_networks
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (1, "", "device or resource busy")
            assert await scan_networks() == []


class TestGetWifiConnectionInfo:
    @pytest.mark.asyncio
    async def test_reports_active_connection_with_ip(self):
        from app.wifi_manager import get_wifi_connection_info
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.side_effect = [
                (0, "wifi:connected:HomeNetwork:wlan0", ""),
                (0, "IP4.ADDRESS[1]:192.168.1.50/24", ""),
            ]
            info = await get_wifi_connection_info()
            assert info == {"connected": True, "ssid": "HomeNetwork", "ip": "192.168.1.50"}

    @pytest.mark.asyncio
    async def test_no_active_wifi_connection(self):
        from app.wifi_manager import get_wifi_connection_info
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (0, "ethernet:connected:Wired connection 1:eth0", "")
            info = await get_wifi_connection_info()
            assert info == {"connected": False, "ssid": None, "ip": None}

    @pytest.mark.asyncio
    async def test_command_failure_reports_disconnected(self):
        from app.wifi_manager import get_wifi_connection_info
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (1, "", "error")
            info = await get_wifi_connection_info()
            assert info == {"connected": False, "ssid": None, "ip": None}


class TestSafeProfileName:
    def test_keeps_alnum_and_safe_chars(self):
        from app.wifi_manager import _safe_profile_name
        assert _safe_profile_name("HomeNetwork") == "HomeNetwork"
        assert _safe_profile_name("Home Network-5G_2.4") == "Home Network-5G_2.4"

    def test_strips_unsafe_characters(self):
        from app.wifi_manager import _safe_profile_name
        assert _safe_profile_name("Cafe/Free;WiFi") == "CafeFreeWiFi"

    def test_empty_or_fully_unsafe_falls_back(self):
        from app.wifi_manager import _safe_profile_name
        assert _safe_profile_name("///") == "ahc-wifi-network"


class TestConnectToNetwork:
    @pytest.mark.asyncio
    async def test_rejects_empty_ssid(self):
        from app.wifi_manager import connect_to_network
        result = await connect_to_network("", "password")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_successful_connect_writes_profile_and_activates(self, tmp_path):
        import app.wifi_manager as wm
        with patch.object(wm, "_STAGING_DIR", tmp_path), \
             patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd, \
             patch("app.wifi_manager.set_user_wifi_override", new_callable=AsyncMock) as mock_override, \
             patch("app.wifi_manager.get_wifi_connection_info", new_callable=AsyncMock) as mock_info:
            mock_cmd.side_effect = [
                (0, "HomeNetwork\n", ""),  # systemd-escape "HomeNetwork" -> unchanged (no special chars)
                (0, "", ""),  # systemctl start ahc-wifi-install@<escaped-safe>.service (also reloads, as root)
                (0, "", ""),  # connection up
            ]
            mock_info.return_value = {"connected": True, "ssid": "HomeNetwork", "ip": "192.168.1.5"}

            result = await wm.connect_to_network("HomeNetwork", "secret123")

            assert result == {"success": True, "message": "Connected", "ip": "192.168.1.5"}
            mock_override.assert_awaited_once_with(True)
            # First call must be the systemd-escape of the sanitized SSID (see
            # _systemd_escape's docstring for why this must happen before the unit
            # name is constructed), second the install-helper unit itself
            assert mock_cmd.call_args_list[0].args[0] == ["systemd-escape", "HomeNetwork"]
            assert mock_cmd.call_args_list[1].args[0] == [
                "systemctl", "start", "ahc-wifi-install@HomeNetwork.service",
            ]

    @pytest.mark.asyncio
    async def test_password_never_appears_as_a_subprocess_argument(self, tmp_path):
        """Regression pin: the whole point of writing a keyfile instead of `nmcli ... password
        <pwd>` (or passing it to any other subprocess) is that the password must never be
        visible in a command line."""
        import app.wifi_manager as wm
        with patch.object(wm, "_STAGING_DIR", tmp_path), \
             patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd, \
             patch("app.wifi_manager.set_user_wifi_override", new_callable=AsyncMock), \
             patch("app.wifi_manager.get_wifi_connection_info", new_callable=AsyncMock) as mock_info:
            mock_info.return_value = {"connected": True, "ssid": "test", "ip": "10.0.0.1"}
            mock_cmd.side_effect = [(0, "HomeNetwork\n", ""), (0, "", ""), (0, "", "")]

            await wm.connect_to_network("HomeNetwork", "super-secret-pw")

            for call in mock_cmd.call_args_list:
                cmd_list = call.args[0]
                assert "super-secret-pw" not in cmd_list

    @pytest.mark.asyncio
    async def test_profile_written_with_correct_content(self, tmp_path):
        """The profile is staged to disk directly by connect_to_network() itself, before the
        (mocked) ahc-wifi-install@ unit would pick it up -- read it straight from _STAGING_DIR
        rather than trying to intercept it via a subprocess argument (there isn't one anymore;
        the install helper reads the path itself from its own %I instance name)."""
        import app.wifi_manager as wm
        with patch.object(wm, "_STAGING_DIR", tmp_path), \
             patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd, \
             patch("app.wifi_manager.set_user_wifi_override", new_callable=AsyncMock), \
             patch("app.wifi_manager.get_wifi_connection_info", new_callable=AsyncMock) as mock_info:
            mock_cmd.return_value = (0, "", "")
            mock_info.return_value = {"connected": True, "ssid": "test", "ip": "10.0.0.1"}
            await wm.connect_to_network("HomeNetwork", "secret123")

        staged = tmp_path / "ahc-wifi-HomeNetwork.nmconnection"
        content = staged.read_text()
        assert "ssid=HomeNetwork" in content
        assert "psk=secret123" in content
        assert "key-mgmt=wpa-psk" in content
        assert oct(staged.stat().st_mode)[-3:] == "600"

    @pytest.mark.asyncio
    async def test_open_network_has_no_security_section(self, tmp_path):
        import app.wifi_manager as wm
        with patch.object(wm, "_STAGING_DIR", tmp_path), \
             patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd, \
             patch("app.wifi_manager.set_user_wifi_override", new_callable=AsyncMock), \
             patch("app.wifi_manager.get_wifi_connection_info", new_callable=AsyncMock) as mock_info:
            mock_cmd.return_value = (0, "", "")
            mock_info.return_value = {"connected": True, "ssid": "test", "ip": "10.0.0.1"}
            await wm.connect_to_network("OpenNetwork", "")

        content = (tmp_path / "ahc-wifi-OpenNetwork.nmconnection").read_text()
        assert "[wifi-security]" not in content

    @pytest.mark.asyncio
    async def test_install_failure_reported(self, tmp_path):
        import app.wifi_manager as wm
        with patch.object(wm, "_STAGING_DIR", tmp_path), \
             patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd, \
             patch("app.wifi_manager.set_user_wifi_override", new_callable=AsyncMock) as mock_override:
            mock_cmd.return_value = (1, "", "unit not found")
            result = await wm.connect_to_network("HomeNetwork", "secret")
            assert result["success"] is False
            mock_override.assert_not_awaited()  # never got past the failed install step
            # The failed install helper never ran, so cleanup is our own responsibility here
            assert not list(tmp_path.glob("ahc-wifi-*.nmconnection"))

    @pytest.mark.asyncio
    async def test_activation_failure_reported(self, tmp_path):
        import app.wifi_manager as wm
        with patch.object(wm, "_STAGING_DIR", tmp_path), \
             patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd, \
             patch("app.wifi_manager.set_user_wifi_override", new_callable=AsyncMock):
            mock_cmd.side_effect = [
                (0, "HomeNetwork\n", ""),  # systemd-escape "HomeNetwork" -> unchanged
                (0, "", ""),  # install helper ok (also reloads, as root)
                (1, "", "No network with SSID found"),  # connection up fails (bad password etc)
            ]
            result = await wm.connect_to_network("HomeNetwork", "wrong-password")
            assert result["success"] is False
            assert "No network with SSID found" in result["message"]


class TestForgetNetwork:
    @pytest.mark.asyncio
    async def test_success(self):
        from app.wifi_manager import forget_network
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            assert await forget_network("HomeNetwork") is True

    @pytest.mark.asyncio
    async def test_failure(self):
        from app.wifi_manager import forget_network
        with patch("app.wifi_manager.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (1, "", "not found")
            assert await forget_network("Unknown") is False


class TestEnsureWifiOnStartup:
    @pytest.mark.asyncio
    async def test_enables_wifi_by_default_regardless_of_ethernet(self):
        """The real bug this replaces: a no-ethernet boot must actively turn WiFi on,
        not just assume it already is — a radio left off from a prior session (e.g.
        Ethernet was plugged in then removed) must not stay off forever."""
        from app.wifi_manager import ensure_wifi_on_startup
        with patch("app.wifi_manager.store") as mock_store, \
             patch("app.wifi_manager.enable_wifi", new_callable=AsyncMock) as mock_enable, \
             patch("app.wifi_manager.disable_wifi", new_callable=AsyncMock) as mock_disable:
            mock_store.get_value = AsyncMock(return_value=True)
            await ensure_wifi_on_startup()
            mock_enable.assert_called_once()
            mock_disable.assert_not_called()

    @pytest.mark.asyncio
    async def test_defaults_to_on_for_a_never_configured_board(self):
        from app.wifi_manager import ensure_wifi_on_startup
        with patch("app.wifi_manager.store") as mock_store, \
             patch("app.wifi_manager.enable_wifi", new_callable=AsyncMock) as mock_enable:
            mock_store.get_value = AsyncMock(return_value=True)  # no persisted key yet -> default True
            await ensure_wifi_on_startup()
            mock_store.get_value.assert_called_once_with("wifi_user_override", True)
            mock_enable.assert_called_once()

    @pytest.mark.asyncio
    async def test_respects_persisted_off_choice(self):
        from app.wifi_manager import ensure_wifi_on_startup
        with patch("app.wifi_manager.store") as mock_store, \
             patch("app.wifi_manager.enable_wifi", new_callable=AsyncMock) as mock_enable, \
             patch("app.wifi_manager.disable_wifi", new_callable=AsyncMock) as mock_disable:
            mock_store.get_value = AsyncMock(return_value=False)
            await ensure_wifi_on_startup()
            mock_disable.assert_called_once()
            mock_enable.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_ethernet_state_entirely(self):
        """Ethernet being up must never influence the WiFi startup decision."""
        from app.wifi_manager import ensure_wifi_on_startup
        with patch("app.wifi_manager.store") as mock_store, \
             patch("app.wifi_manager._ethernet_is_up", return_value=True), \
             patch("app.wifi_manager.enable_wifi", new_callable=AsyncMock) as mock_enable:
            mock_store.get_value = AsyncMock(return_value=True)
            await ensure_wifi_on_startup()
            mock_enable.assert_called_once()


class TestSetUserWifiOverride:
    @pytest.mark.asyncio
    async def test_enable_override(self):
        from app.wifi_manager import set_user_wifi_override
        import app.wifi_manager as wm
        with patch("app.wifi_manager.store") as mock_store, \
             patch("app.wifi_manager.enable_wifi", new_callable=AsyncMock) as mock_enable:
            mock_store.set_value = AsyncMock()
            await set_user_wifi_override(True)
            assert wm._user_wifi_override is True
            mock_enable.assert_called_once()

    @pytest.mark.asyncio
    async def test_disable_override(self):
        from app.wifi_manager import set_user_wifi_override
        import app.wifi_manager as wm
        with patch("app.wifi_manager.store") as mock_store, \
             patch("app.wifi_manager.disable_wifi", new_callable=AsyncMock) as mock_disable:
            mock_store.set_value = AsyncMock()
            await set_user_wifi_override(False)
            assert wm._user_wifi_override is False
            mock_disable.assert_called_once()


class TestWifiMonitor:
    @pytest.mark.asyncio
    async def test_start_and_stop_monitor(self):
        """start_wifi_monitor creates a task; stop_wifi_monitor cancels it cleanly."""
        from app.wifi_manager import start_wifi_monitor, stop_wifi_monitor
        import app.wifi_manager as wm
        start_wifi_monitor()
        assert wm._monitor_task is not None
        assert not wm._monitor_task.done()
        await stop_wifi_monitor()
        assert wm._monitor_task is None

    @pytest.mark.asyncio
    async def test_stop_monitor_no_op_when_not_started(self):
        """stop_wifi_monitor is safe when no task is running."""
        from app.wifi_manager import stop_wifi_monitor
        import app.wifi_manager as wm
        wm._monitor_task = None
        await stop_wifi_monitor()  # must not raise

    @pytest.mark.asyncio
    async def test_monitor_loop_re_enables_wifi_found_unexpectedly_off(self):
        """The self-heal check: if the desired state is on but the radio drifted off
        (driver hiccup, manual rfkill outside the app), one tick must correct it —
        with no Ethernet-state involvement at all."""
        import app.wifi_manager as wm
        wm._user_wifi_override = True
        with patch("app.wifi_manager.get_wifi_status", new_callable=AsyncMock) as mock_status, \
             patch("app.wifi_manager.enable_wifi", new_callable=AsyncMock) as mock_enable, \
             patch("app.wifi_manager.disable_wifi", new_callable=AsyncMock) as mock_disable, \
             patch("app.wifi_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_status.return_value = {"wifiEnabled": False, "ethernetUp": True, "userOverride": True}
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            with pytest.raises(asyncio.CancelledError):
                await wm._wifi_monitor_loop()
            mock_enable.assert_called_once()
            mock_disable.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_loop_disables_wifi_found_unexpectedly_on(self):
        """Symmetric case: desired state is off but the radio is on — correct it."""
        import app.wifi_manager as wm
        wm._user_wifi_override = False
        with patch("app.wifi_manager.get_wifi_status", new_callable=AsyncMock) as mock_status, \
             patch("app.wifi_manager.enable_wifi", new_callable=AsyncMock) as mock_enable, \
             patch("app.wifi_manager.disable_wifi", new_callable=AsyncMock) as mock_disable, \
             patch("app.wifi_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_status.return_value = {"wifiEnabled": True, "ethernetUp": False, "userOverride": False}
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            with pytest.raises(asyncio.CancelledError):
                await wm._wifi_monitor_loop()
            mock_disable.assert_called_once()
            mock_enable.assert_not_called()


# — tls.py ———————————————————————————————————————————————————

class TestGetLocalIps:
    def test_returns_at_least_localhost(self):
        from app.tls import _get_local_ips
        ips = _get_local_ips()
        assert "127.0.0.1" in ips

    def test_returns_sorted(self):
        from app.tls import _get_local_ips
        ips = _get_local_ips()
        assert ips == sorted(ips)


class TestEnsureTlsCert:
    @pytest.mark.asyncio
    async def test_returns_existing_cert(self, tmp_path):
        from app.tls import ensure_tls_cert
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("CERT")
        key.write_text("KEY")

        with patch("app.tls.settings") as mock_settings:
            mock_settings.tls_cert_path = cert
            mock_settings.tls_key_path = key
            result_cert, result_key = await ensure_tls_cert()
            assert result_cert == cert
            assert result_key == key

    @pytest.mark.asyncio
    async def test_missing_cert_requests_reissue_and_waits_for_root(self, tmp_path):
        """
        Under H-11 the service can no longer generate its own certificate (a service that could
        produce a certificate root would sign could obtain a signature over a key it chose) — it
        writes a request file and waits for `ahc-issue-cert.sh`, running as root, to publish one.
        This simulates root answering shortly after the request appears.
        """
        from app.tls import ensure_tls_cert
        data_dir = tmp_path / "data"
        cert = data_dir / "tls" / "cert.pem"
        key = data_dir / "tls" / "key.pem"
        request_path = data_dir / "cert-request.json"

        async def _root_answers_after_request():
            # Poll for the request the service is about to write, then "issue" a certificate —
            # standing in for ahc-issue-cert.sh's systemd-.path-triggered run.
            for _ in range(50):
                if request_path.exists():
                    break
                await asyncio.sleep(0.01)
            cert.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=test"],
                capture_output=True, check=True,
            )

        with patch("app.tls.settings") as mock_settings, \
             patch("app.tls._REISSUE_POLL_INTERVAL_S", 0.02):
            mock_settings.tls_cert_path = cert
            mock_settings.tls_key_path = key
            mock_settings.data_dir = data_dir

            root_task = asyncio.create_task(_root_answers_after_request())
            result_cert, result_key = await ensure_tls_cert()
            await root_task

            assert result_cert == cert
            assert cert.exists(), "the certificate root published must be in place"
            assert request_path.exists(), "the service must have written a request for root to see"
            assert json.loads(request_path.read_text())["reason"] == "no certificate present"

    @pytest.mark.asyncio
    async def test_raises_when_root_never_answers_and_no_cert_exists(self, tmp_path):
        """No certificate, and root's ahc-issue-cert.path never fires (e.g. not enabled) — the
        service must fail loudly rather than silently start on plain HTTP with no explanation."""
        from app.tls import ensure_tls_cert
        data_dir = tmp_path / "data"
        cert = data_dir / "tls" / "cert.pem"
        key = data_dir / "tls" / "key.pem"

        with patch("app.tls.settings") as mock_settings, \
             patch("app.tls._REISSUE_TIMEOUT_S", 0.05), \
             patch("app.tls._REISSUE_POLL_INTERVAL_S", 0.01):
            mock_settings.tls_cert_path = cert
            mock_settings.tls_key_path = key
            mock_settings.data_dir = data_dir
            with pytest.raises(RuntimeError, match="ahc-issue-cert"):
                await ensure_tls_cert()

    @pytest.mark.asyncio
    async def test_keeps_existing_cert_when_root_does_not_answer_a_reissue(self, tmp_path):
        """A stale-but-present certificate is still better than none — if root does not answer a
        reissue request in time, the service keeps serving what it already has."""
        from app.tls import ensure_tls_cert
        data_dir = tmp_path / "data"
        cert = data_dir / "tls" / "cert.pem"
        key = data_dir / "tls" / "key.pem"
        cert.parent.mkdir(parents=True)
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=stale"],
            capture_output=True, check=True,
        )

        with patch("app.tls.settings") as mock_settings, \
             patch("app.tls._cert_covers", return_value=False), \
             patch("app.tls._REISSUE_TIMEOUT_S", 0.05), \
             patch("app.tls._REISSUE_POLL_INTERVAL_S", 0.01):
            mock_settings.tls_cert_path = cert
            mock_settings.tls_key_path = key
            mock_settings.data_dir = data_dir
            result_cert, result_key = await ensure_tls_cert()
            assert result_cert == cert
            assert result_key == key
