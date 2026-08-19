"""
Bluetooth management — radio power, device scan/list, pair/connect, via bluetoothctl.

bluetoothctl accepts most subcommands as one-shot CLI args (bluez 5.5x+) without dropping into
its interactive REPL: `power on/off`, `devices`, `info <addr>`, `pair <addr>`, `connect <addr>`
all run and exit on their own. Scanning is the one exception — `bluetoothctl scan on` streams
and never exits by itself, so it's run under the external `timeout` command instead (kills
bluetoothctl after N seconds; whatever it discovered in that window is left in bluez's device
cache, then read back via `devices`) — the same end result `nmcli device wifi list --rescan yes`
gives synchronously in wifi_manager.py, just achieved differently since bluetoothctl has no
synchronous one-shot scan subcommand of its own.

No board in this fleet currently has Bluetooth hardware wired up for live-testing this module
end-to-end (see the deploy session's report): only the Cubie A5E has a real controller (hci0),
and it's off-limits for radio toggling per the standing hardware-safety rule. Rock Pi 4A and the
x86 thin client — the two boards cleared for destructive testing — have no Bluetooth adapter at
all. `is_adapter_present()`/`get_status()`'s adapter_present flag exists specifically so the app
can show a clear "No Bluetooth adapter" state on those two boards instead of a toggle that
silently does nothing.
"""

from __future__ import annotations

import logging
import re

from .subprocess_runner import run_command

logger = logging.getLogger("aihomecloud.bluetooth")

_DEVICE_LINE_RE = re.compile(r"^Device\s+([0-9A-Fa-f:]{17})\s+(.*)$")


async def is_adapter_present() -> bool:
    """True if the system has a Bluetooth controller at all — distinct from "off"."""
    rc, out, _ = await run_command(["bluetoothctl", "list"], timeout=5)
    return rc == 0 and out.strip() != ""


async def get_power_state() -> bool:
    """True if the (first/default) adapter's radio is powered on."""
    rc, out, _ = await run_command(["bluetoothctl", "show"], timeout=5)
    if rc != 0:
        return False
    for line in out.splitlines():
        if line.strip().startswith("Powered:"):
            return line.strip().endswith("yes")
    return False


async def set_power(enabled: bool) -> bool:
    """Toggle the Bluetooth radio via bluetoothctl. Returns True on success."""
    rc, _, err = await run_command(["bluetoothctl", "power", "on" if enabled else "off"], timeout=10)
    if rc != 0:
        logger.warning("Failed to set Bluetooth power=%s: %s", enabled, err)
        return False
    return True


async def _device_status(address: str) -> dict:
    """Paired/connected flags for one device, via `bluetoothctl info`."""
    rc, out, _ = await run_command(["bluetoothctl", "info", address], timeout=5)
    paired = False
    connected = False
    if rc == 0:
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Paired:"):
                paired = s.endswith("yes")
            elif s.startswith("Connected:"):
                connected = s.endswith("yes")
    return {"paired": paired, "connected": connected}


async def list_devices() -> list[dict]:
    """Every device bluez currently knows about (paired previously, or seen during the most
    recent scan), with per-device paired/connected state."""
    rc, out, err = await run_command(["bluetoothctl", "devices"], timeout=10)
    if rc != 0:
        logger.warning("Failed to list Bluetooth devices: %s", err)
        return []
    devices = []
    for line in out.splitlines():
        m = _DEVICE_LINE_RE.match(line.strip())
        if not m:
            continue
        address, name = m.group(1), m.group(2)
        status = await _device_status(address)
        devices.append({"address": address, "name": name or address, **status})
    return devices


async def scan(seconds: int = 8) -> list[dict]:
    """Scan for nearby devices for [seconds] (clamped to 3-30s), then return the resulting
    device list (same shape as list_devices) — scanning populates bluez's device cache, it
    doesn't return results directly."""
    seconds = max(3, min(seconds, 30))
    rc, _, err = await run_command(["timeout", str(seconds), "bluetoothctl", "scan", "on"], timeout=seconds + 5)
    # `timeout` exits 124 when it had to kill the child -- that's the EXPECTED path here (the
    # scan is meant to run the full duration and get killed), not a failure to log.
    if rc not in (0, 124):
        logger.warning("Bluetooth scan failed: %s", err)
    return await list_devices()


async def pair(address: str) -> bool:
    rc, _, err = await run_command(["bluetoothctl", "pair", address], timeout=20)
    if rc != 0:
        logger.warning("Failed to pair with %s: %s", address, err)
        return False
    return True


async def connect(address: str) -> bool:
    rc, _, err = await run_command(["bluetoothctl", "connect", address], timeout=15)
    if rc != 0:
        logger.warning("Failed to connect to %s: %s", address, err)
        return False
    return True


async def get_status() -> dict:
    """Aggregated status for the settings screen: adapter presence, power, known devices."""
    present = await is_adapter_present()
    if not present:
        return {"adapter_present": False, "enabled": False, "devices": []}
    enabled = await get_power_state()
    devices = await list_devices() if enabled else []
    return {"adapter_present": True, "enabled": enabled, "devices": devices}
