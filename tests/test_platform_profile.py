"""
Tests for platform_profile — declaring what a host can actually do.

The point of these is that capability is *derived*, not asserted: the same code must report
different capability on a board with radios, a board without, and a Windows host. Faking the
OS alone is not enough — the tool probe has to be faked too, because a Linux box without
`nmcli` genuinely cannot manage radios.
"""

import pytest

from app import platform_profile as pp
from app.platform_profile import Capability, CapabilityUnavailable, HostKind


@pytest.fixture(autouse=True)
def _reset():
    pp._reset_for_tests()
    yield
    pp._reset_for_tests()


def _pretend(monkeypatch, host: str, tools: set[str], device_tree: bool = False,
             wifi: bool = False, bluetooth: bool = False):
    """
    Fake a host. Tools and hardware are separate arguments on purpose — verified on real
    boards that they come apart: both the ROCK Pi and the Cubie ship nmcli and bluetoothctl,
    and only the Cubie has radios. An earlier version of this helper conflated them, and the
    "board without radios" test passed for the wrong reason.
    """
    monkeypatch.setenv("AHC_PLATFORM", host)
    monkeypatch.setattr(pp.shutil, "which", lambda n: f"/usr/bin/{n}" if n in tools else None)
    monkeypatch.setattr(pp.Path, "exists", lambda self: device_tree and "device-tree" in str(self))
    monkeypatch.setattr(pp, "_has_wifi_radio", lambda: wifi)
    monkeypatch.setattr(pp, "_has_bluetooth_controller", lambda: bluetooth)
    pp._reset_for_tests()


# --- detection -------------------------------------------------------------

def test_the_override_selects_a_host(monkeypatch):
    monkeypatch.setenv("AHC_PLATFORM", "windows")
    pp._reset_for_tests()
    assert pp.host_kind() is HostKind.WINDOWS


def test_an_unknown_override_is_ignored_rather_than_fatal(monkeypatch):
    # A typo in an env var must not take the backend down at import.
    monkeypatch.setenv("AHC_PLATFORM", "solaris")
    pp._reset_for_tests()
    assert pp.host_kind() in set(HostKind)


# --- capability derivation -------------------------------------------------

def test_a_full_board_reports_everything(monkeypatch):
    _pretend(monkeypatch, "linux_sbc", {"systemctl", "nmcli", "bluetoothctl", "lsblk"},
             device_tree=True, wifi=True, bluetooth=True)
    caps = pp.capabilities()
    for expected in (Capability.SERVICE_CONTROL, Capability.NETWORK_RADIOS,
                     Capability.BLUETOOTH, Capability.RAW_DISK_OPS,
                     Capability.HOST_POWER, Capability.BOARD_IDENTITY):
        assert expected in caps


def test_a_board_without_radios_does_not_claim_them(monkeypatch):
    # The real fleet, and the exact shape that broke the first version of this module: the
    # ROCK Pi HAS nmcli and bluetoothctl installed and has NO radios. Tools present, hardware
    # absent — capability must follow the hardware.
    _pretend(monkeypatch, "linux_sbc", {"systemctl", "nmcli", "bluetoothctl", "lsblk"},
             device_tree=True, wifi=False, bluetooth=False)
    assert not pp.supports(Capability.NETWORK_RADIOS)
    assert not pp.supports(Capability.BLUETOOTH)
    assert pp.supports(Capability.RAW_DISK_OPS)
    assert pp.supports(Capability.SERVICE_CONTROL)


def test_generic_linux_is_not_a_board(monkeypatch):
    # A VM or laptop runs the same OS but has no thermal zones or device-tree model, so
    # board identity must not be claimed.
    _pretend(monkeypatch, "linux", {"systemctl", "nmcli", "lsblk"}, device_tree=False, wifi=True)
    assert not pp.supports(Capability.BOARD_IDENTITY)
    assert pp.supports(Capability.SERVICE_CONTROL)


def test_windows_declares_nothing_yet(monkeypatch):
    # A statement of current fact. A native port would add SERVICE_CONTROL and RAW_DISK_OPS
    # here; until it exists, claiming them would be a lie the guards depend on.
    _pretend(monkeypatch, "windows", {"systemctl", "nmcli", "lsblk"})
    assert pp.capabilities() == frozenset()


def test_a_linux_host_missing_the_tool_cannot_do_the_thing(monkeypatch):
    # Capability is not OS identity. Without nmcli there is no radio management, whatever
    # the kernel says.
    _pretend(monkeypatch, "linux", {"systemctl"}, device_tree=False)
    assert not pp.supports(Capability.NETWORK_RADIOS)


# --- the guard -------------------------------------------------------------

def test_require_passes_when_supported(monkeypatch):
    _pretend(monkeypatch, "linux_sbc", {"systemctl", "nmcli", "lsblk"},
             device_tree=True, wifi=True)
    pp.require(Capability.NETWORK_RADIOS)  # must not raise


def test_require_raises_naming_the_capability(monkeypatch):
    _pretend(monkeypatch, "windows", set())
    with pytest.raises(CapabilityUnavailable) as excinfo:
        pp.require(Capability.NETWORK_RADIOS)
    assert excinfo.value.capability is Capability.NETWORK_RADIOS
    # The message has to name both sides, or a support conversation starts with a guess.
    assert "network_radios" in str(excinfo.value)
    assert "windows" in str(excinfo.value)


# --- reporting -------------------------------------------------------------

def test_summary_lists_what_is_missing_as_well_as_present(monkeypatch):
    # "What would a port have to implement" should be readable off a running host.
    _pretend(monkeypatch, "linux_sbc", {"systemctl", "lsblk"}, device_tree=True)
    s = pp.summary()
    assert s["host"] == "linux_sbc"
    assert "raw_disk_ops" in s["capabilities"]
    assert "network_radios" in s["missing"]
    assert "bluetooth" in s["missing"]


def test_capabilities_are_resolved_once(monkeypatch):
    # Probing runs shutil.which per capability; doing that on every request would put
    # filesystem lookups in the hot path of every guarded route.
    calls = []
    monkeypatch.setenv("AHC_PLATFORM", "linux")
    monkeypatch.setattr(pp.shutil, "which", lambda n: calls.append(n) or "/usr/bin/x")
    pp._reset_for_tests()
    pp.capabilities(); pp.capabilities(); pp.capabilities()
    first = len(calls)
    pp.capabilities()
    assert len(calls) == first


def test_a_radio_without_its_tool_is_still_unusable(monkeypatch):
    # The other half of the same rule. Hardware present, driver tool absent — nothing can
    # actually be done with it, so the capability must not be claimed.
    _pretend(monkeypatch, "linux", {"systemctl"}, wifi=True, bluetooth=True)
    assert not pp.supports(Capability.NETWORK_RADIOS)
    assert not pp.supports(Capability.BLUETOOTH)
