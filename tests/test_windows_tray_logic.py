"""
Pure-logic tests for app/windows_tray.py -- the tray helper itself defers every pywin32/pystray
import into the functions that actually need them (same pattern as windows_secrets.py), so the
handful of pure functions (URL building, status labels, icon color, port resolution) are
importable and testable on any platform. The pystray event loop and win32serviceutil calls
themselves are Windows-only and untested here -- same caveat as every other Windows-only module
in this project until real hardware is available.
"""

import pytest

from app.windows_tray import dashboard_url, icon_color, resolve_port, status_label


class TestDashboardUrl:
    def test_default_port(self):
        assert dashboard_url(8443) == "https://localhost:8443/app/"

    def test_custom_port(self):
        assert dashboard_url(9000) == "https://localhost:9000/app/"


class TestStatusLabel:
    def test_running(self):
        assert status_label(True) == "AiHomeCloud — Running"

    def test_stopped(self):
        assert status_label(False) == "AiHomeCloud — Stopped"


class TestIconColor:
    def test_running_is_green(self):
        assert icon_color(True) == "#1E8E5A"

    def test_stopped_is_red(self):
        assert icon_color(False) == "#8A2B23"

    def test_unknown_is_amber_not_green_or_red(self):
        color = icon_color(None)
        assert color == "#B4740E"
        assert color != icon_color(True)
        assert color != icon_color(False)


class TestResolvePort:
    def test_reads_ahc_port_env_var(self, monkeypatch):
        monkeypatch.setenv("AHC_PORT", "9443")
        assert resolve_port() == 9443

    def test_defaults_to_8443_when_unset(self, monkeypatch):
        monkeypatch.delenv("AHC_PORT", raising=False)
        assert resolve_port() == 8443

    def test_falls_back_on_garbage_value(self, monkeypatch):
        monkeypatch.setenv("AHC_PORT", "not-a-port")
        assert resolve_port() == 8443


class TestServiceControlSddlAce:
    """Mirror of Grant-ServiceControlToUsers' SDDL-ACE-insertion logic
    (backend/install_windows.ps1) -- PowerShell/sc.exe can't run here, so this proves the
    string-manipulation logic in isolation, same rationale as the NAS-root validation mirror."""

    NEW_ACE = "(A;;RPWPLC;;;AU)"

    @staticmethod
    def insert_ace(current_sd: str, new_ace: str) -> str:
        import re

        m = re.match(r"^(D:[A-Z]*)((?:\([^)]*\))*)(S:.*)?$", current_sd)
        if not m:
            return current_sd + new_ace
        prefix, aces, sacl = m.groups()
        return prefix + aces + new_ace + (sacl or "")

    def test_inserts_before_sacl_when_present(self):
        sd = "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)S:(AU;FA;KA;;;WD)"
        result = self.insert_ace(sd, self.NEW_ACE)
        assert result == (
            "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)"
            "(A;;RPWPLC;;;AU)S:(AU;FA;KA;;;WD)"
        )

    def test_appends_when_no_sacl(self):
        sd = "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)"
        result = self.insert_ace(sd, self.NEW_ACE)
        assert result == sd + self.NEW_ACE
        assert result.endswith(self.NEW_ACE)

    def test_result_always_contains_exactly_one_new_ace(self):
        sd = "D:PAI(A;;CCLCSWRPWPDTLOCRRC;;;SY)"
        result = self.insert_ace(sd, self.NEW_ACE)
        assert result.count(self.NEW_ACE) == 1
