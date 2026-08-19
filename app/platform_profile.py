"""
What this host can actually do.

Every capability assumption in this backend is currently implicit: 56 calls to `systemctl`,
`nmcli`, `bluetoothctl`, `mkfs` and friends spread across eight files, each assuming a Linux
single-board computer with radios, systemd and raw disk access. Nothing declares that
assumption, so nothing can check it.

**This is not Windows speculation — the existing fleet already disagrees.** Neither the ROCK Pi
4A nor the x86 thin client has a WiFi or Bluetooth radio at all (confirmed on hardware
2026-07-19: `nmcli device status` and `rfkill list` are both empty on both). The app offers
those features on all three boards regardless, so a family member on the thin client can reach
a hotspot toggle that can never work. Declaring capability fixes that today.

It is also the prerequisite for answering the Windows question rather than debating it. A port
needs to know which of these it must implement, which it may refuse, and which are meaningless
off a board — and that list should be code, not a document that drifts.

Deliberately capability-based rather than OS-based. "Can this host manage systemd units?" is
the question the calling code actually has; "is this Linux?" is a proxy that is wrong on a
Linux box without radios, and would be wrong again on a future platform that has them.
"""

from __future__ import annotations

import functools
import logging
import os
import platform
import shutil
from enum import Enum
from pathlib import Path

logger = logging.getLogger("aihomecloud.platform")


class HostKind(str, Enum):
    """Coarse host family. Capabilities are derived from this plus real probing."""

    LINUX_SBC = "linux_sbc"        # a supported single-board computer — full capability
    LINUX_GENERIC = "linux"        # Linux, but not a board we know
    WINDOWS = "windows"
    MACOS = "macos"
    UNKNOWN = "unknown"


class Capability(str, Enum):
    """Things a route may need. Named for what the caller wants, not how it is implemented."""

    SERVICE_CONTROL = "service_control"    # start/stop/enable units (systemd today)
    NETWORK_RADIOS = "network_radios"      # WiFi client + hotspot (nmcli)
    BLUETOOTH = "bluetooth"                # pairing, power (bluetoothctl)
    RAW_DISK_OPS = "raw_disk_ops"          # partition, mkfs, mount, eject
    HOST_POWER = "host_power"              # reboot and shut down the machine itself
    BOARD_IDENTITY = "board_identity"      # thermal zones, CPU governor, device-tree model
    LOCAL_BOT_API = "local_bot_api"        # self-hosted telegram-bot-api service


def _detect_host() -> HostKind:
    """
    Host family, overridable with `AHC_PLATFORM` for tests and for exercising a target
    platform's guards from a developer machine.
    """
    override = os.environ.get("AHC_PLATFORM", "").strip().lower()
    if override:
        try:
            return HostKind(override)
        except ValueError:
            logger.warning("ignoring unknown AHC_PLATFORM=%r", override)

    system = platform.system().lower()
    if system == "windows":
        return HostKind.WINDOWS
    if system == "darwin":
        return HostKind.MACOS
    if system != "linux":
        return HostKind.UNKNOWN

    # A board exposes its model in the device tree. Absence means a generic Linux host —
    # a VM, a laptop, a container — which is not the same thing as an unsupported OS.
    if Path("/proc/device-tree/model").exists():
        return HostKind.LINUX_SBC
    return HostKind.LINUX_GENERIC


@functools.lru_cache(maxsize=1)
def host_kind() -> HostKind:
    kind = _detect_host()
    logger.info("platform_detected host=%s", kind.value)
    return kind


def _tool_present(name: str) -> bool:
    return shutil.which(name) is not None


def _has_wifi_radio() -> bool:
    """
    A wireless interface actually exists.

    Deliberately NOT `which nmcli`. Verified on hardware 2026-08-04: both the ROCK Pi 4A and
    the Cubie A5E ship nmcli and bluetoothctl, but only the Cubie has radios — the ROCK Pi
    reports zero wifi devices. Probing the tool would have claimed radio capability on a board
    with no radio, which is precisely the bug this module exists to prevent.

    sysfs rather than shelling out to nmcli: a directory check costs nothing and cannot hang.
    """
    net = Path("/sys/class/net")
    if not net.is_dir():
        return False
    try:
        return any((iface / "wireless").exists() or (iface / "phy80211").exists()
                   for iface in net.iterdir())
    except OSError:
        return False


def _has_bluetooth_controller() -> bool:
    """A Bluetooth controller actually exists — same reasoning as [_has_wifi_radio]."""
    bt = Path("/sys/class/bluetooth")
    try:
        return bt.is_dir() and any(bt.iterdir())
    except OSError:
        return False


@functools.lru_cache(maxsize=1)
def _capabilities() -> frozenset[Capability]:
    """
    Resolved once. Probes for the tool where the tool is the whole capability — a Linux box
    without `nmcli` cannot manage radios no matter what its kernel is, and pretending
    otherwise just moves the failure later and makes it uglier.
    """
    kind = host_kind()
    caps: set[Capability] = set()

    if kind in (HostKind.LINUX_SBC, HostKind.LINUX_GENERIC):
        if _tool_present("systemctl"):
            caps.add(Capability.SERVICE_CONTROL)
            caps.add(Capability.LOCAL_BOT_API)
        # Both the tool AND the hardware. Either alone is a false positive: nmcli is present
        # on boards with no radio, and a radio is unusable without the tool to drive it.
        if _tool_present("nmcli") and _has_wifi_radio():
            caps.add(Capability.NETWORK_RADIOS)
        if _tool_present("bluetoothctl") and _has_bluetooth_controller():
            caps.add(Capability.BLUETOOTH)
        if _tool_present("lsblk"):
            caps.add(Capability.RAW_DISK_OPS)
        caps.add(Capability.HOST_POWER)
        if kind is HostKind.LINUX_SBC:
            caps.add(Capability.BOARD_IDENTITY)

    # Windows and macOS deliberately declare nothing yet. That is a statement of current
    # fact, not a judgement: a native Windows port would add SERVICE_CONTROL (Windows
    # Services) and RAW_DISK_OPS (diskpart/WMI) here, and the rest would stay absent.

    return frozenset(caps)


def supports(capability: Capability) -> bool:
    return capability in _capabilities()


def capabilities() -> frozenset[Capability]:
    return _capabilities()


class CapabilityUnavailable(RuntimeError):
    """Raised when a route needs something this host cannot do."""

    def __init__(self, capability: Capability):
        self.capability = capability
        super().__init__(f"{capability.value} is not available on {host_kind().value}")


def require(capability: Capability) -> None:
    """
    Guard at the top of a route. Fails fast and legibly instead of letting the request reach
    a missing binary and surface as a confusing subprocess error several layers down.
    """
    if capability not in _capabilities():
        raise CapabilityUnavailable(capability)


def summary() -> dict:
    """Reportable snapshot — for diagnostics and for the platform-support conversation."""
    return {
        "host": host_kind().value,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "capabilities": sorted(c.value for c in _capabilities()),
        "missing": sorted(c.value for c in Capability if c not in _capabilities()),
    }


def _reset_for_tests() -> None:
    host_kind.cache_clear()
    _capabilities.cache_clear()
