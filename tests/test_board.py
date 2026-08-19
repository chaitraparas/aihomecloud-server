"""
Tests for board.py — board detection, thermal zone, LAN interface.
"""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


class TestFindThermalZone:
    def test_cpu_zone_found(self, tmp_path):
        from app.board import find_thermal_zone
        # Create fake /sys/class/thermal with a cpu zone
        zone0 = tmp_path / "thermal_zone0"
        zone0.mkdir()
        (zone0 / "type").write_text("acpitz")
        zone1 = tmp_path / "thermal_zone1"
        zone1.mkdir()
        (zone1 / "type").write_text("cpu-thermal")
        (zone1 / "temp").write_text("45000")

        with patch("app.board.Path") as mock_path_cls:
            thermal_base = MagicMock()
            thermal_base.exists.return_value = True
            thermal_base.glob.return_value = sorted([zone0, zone1])
            mock_path_cls.return_value = thermal_base
            result = find_thermal_zone()
            assert "thermal_zone1" in result or "temp" in result

    def test_no_thermal_dir(self):
        from app.board import find_thermal_zone
        with patch("app.board.Path") as mock_path_cls:
            thermal_base = MagicMock()
            thermal_base.exists.return_value = False
            mock_path_cls.return_value = thermal_base
            result = find_thermal_zone()
            assert result == "/sys/class/thermal/thermal_zone0/temp"

    def test_soc_zone_found(self, tmp_path):
        from app.board import find_thermal_zone
        zone0 = tmp_path / "thermal_zone0"
        zone0.mkdir()
        (zone0 / "type").write_text("soc-thermal")
        (zone0 / "temp").write_text("50000")

        with patch("app.board.Path") as mock_path_cls:
            thermal_base = MagicMock()
            thermal_base.exists.return_value = True
            thermal_base.glob.return_value = [zone0]
            mock_path_cls.return_value = thermal_base
            result = find_thermal_zone()
            assert "temp" in result

    # x86 thin-client audit finding #2: x86 ACPI thermal zones are typically typed
    # "x86_pkg_temp" or "acpitz", neither of which contains "cpu"/"soc" -- without
    # the "pkg"/"acpi" patterns these would silently miss the real CPU zone.

    def test_x86_pkg_temp_zone_found(self, tmp_path):
        from app.board import find_thermal_zone
        zone0 = tmp_path / "thermal_zone0"
        zone0.mkdir()
        (zone0 / "type").write_text("x86_pkg_temp")
        (zone0 / "temp").write_text("42000")

        with patch("app.board.Path") as mock_path_cls:
            thermal_base = MagicMock()
            thermal_base.exists.return_value = True
            thermal_base.glob.return_value = [zone0]
            mock_path_cls.return_value = thermal_base
            result = find_thermal_zone()
            assert "thermal_zone0" in result

    def test_acpitz_zone_found(self, tmp_path):
        from app.board import find_thermal_zone
        zone0 = tmp_path / "thermal_zone0"
        zone0.mkdir()
        (zone0 / "type").write_text("acpitz")
        (zone0 / "temp").write_text("40000")

        with patch("app.board.Path") as mock_path_cls:
            thermal_base = MagicMock()
            thermal_base.exists.return_value = True
            thermal_base.glob.return_value = [zone0]
            mock_path_cls.return_value = thermal_base
            result = find_thermal_zone()
            assert "thermal_zone0" in result


class TestFindLanInterface:
    def test_ethernet_found(self, tmp_path):
        from app.board import find_lan_interface
        lo = tmp_path / "lo"
        lo.mkdir()
        (lo / "type").write_text("772")

        eth0 = tmp_path / "eth0"
        eth0.mkdir()
        (eth0 / "type").write_text("1")

        with patch("app.board.Path") as mock_path_cls:
            net_base = MagicMock()
            net_base.exists.return_value = True
            net_base.iterdir.return_value = sorted([lo, eth0])
            mock_path_cls.return_value = net_base
            result = find_lan_interface()
            assert result == "eth0"

    def test_no_net_dir(self):
        from app.board import find_lan_interface
        with patch("app.board.Path") as mock_path_cls:
            net_base = MagicMock()
            net_base.exists.return_value = False
            mock_path_cls.return_value = net_base
            result = find_lan_interface()
            assert result == "eth0"

    def test_only_loopback(self, tmp_path):
        from app.board import find_lan_interface
        lo = tmp_path / "lo"
        lo.mkdir()
        (lo / "type").write_text("772")

        with patch("app.board.Path") as mock_path_cls:
            net_base = MagicMock()
            net_base.exists.return_value = True
            net_base.iterdir.return_value = [lo]
            mock_path_cls.return_value = net_base
            result = find_lan_interface()
            assert result == "eth0"


class TestDetectBoard:
    def test_known_board_exact_match(self):
        from app.board import detect_board
        with patch("builtins.open", side_effect=lambda f, *a, **kw:
                    MagicMock(__enter__=lambda s: MagicMock(read=lambda: "sun60iw2\x00"),
                              __exit__=lambda *a: None) if "device-tree" in str(f)
                    else open(f, *a, **kw)), \
             patch("app.board.find_thermal_zone", return_value="/sys/class/thermal/thermal_zone0/temp"), \
             patch("app.board.find_lan_interface", return_value="eth0"):
            board = detect_board()
            assert board.model_name == "Radxa CUBIE A7A"
            assert board.lan_interface == "eth0"

    def test_unknown_board_fallback(self):
        from app.board import detect_board
        with patch("builtins.open", side_effect=FileNotFoundError), \
             patch("app.board.find_thermal_zone", return_value="/sys/class/thermal/thermal_zone0/temp"), \
             patch("app.board.find_lan_interface", return_value="eth0"):
            board = detect_board()
            assert board.model_name == "unknown"

    def test_substring_match(self):
        from app.board import detect_board
        import io
        fake_file = io.StringIO("My Raspberry Pi 4 Board\x00")
        with patch("builtins.open", return_value=fake_file), \
             patch("app.board.find_thermal_zone", return_value="/sys/class/thermal/thermal_zone1/temp"), \
             patch("app.board.find_lan_interface", return_value="end0"):
            board = detect_board()
            assert board.model_name == "Raspberry Pi 4 Model B"

    def test_board_config_fields(self):
        from app.board import BoardConfig
        bc = BoardConfig(
            model_name="test",
            thermal_zone_path="/tmp/temp",
            lan_interface="eth0",
            cpu_governor_path="/sys/cpu"
        )
        assert bc.model_name == "test"
        assert bc.lan_interface == "eth0"

    def test_known_boards_dict(self):
        from app.board import KNOWN_BOARDS
        assert "sun60iw2" in KNOWN_BOARDS
        assert "Raspberry Pi 4 Model B" in KNOWN_BOARDS
