"""
WebSocket endpoint — streams live system stats every 2 seconds.
Uses psutil for real CPU / RAM / temp / network / disk readings.
Respects board-specific thermal zone paths for accurate temperature readings.
"""

from __future__ import annotations

import asyncio
import json
import time

import psutil
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from shutil import disk_usage

from ..auth import decode_token
from ..config import settings
from .event_routes import emit_storage_warning

router = APIRouter(tags=["monitor"])

# Track boot time once
_boot_time = psutil.boot_time()

# WebSocket connection limits to prevent resource exhaustion
_MAX_WS_CONNECTIONS = 10
_MAX_WS_PER_USER = 3
_ws_connection_count = 0
_ws_connections_per_user: dict[str, int] = {}


def _pick_network_counters(preferred_iface: str | None = None):
    """Pick network counters for the preferred iface, else busiest non-loopback iface."""
    per_nic = psutil.net_io_counters(pernic=True)
    if not per_nic:
        return psutil.net_io_counters(), "all"

    if preferred_iface and preferred_iface in per_nic:
        return per_nic[preferred_iface], preferred_iface

    # Fallback: choose busiest non-loopback interface by total bytes.
    candidates = {k: v for k, v in per_nic.items() if not k.startswith("lo")}
    if not candidates:
        return psutil.net_io_counters(), "all"
    iface, counters = max(
        candidates.items(),
        key=lambda item: item[1].bytes_sent + item[1].bytes_recv,
    )
    return counters, iface


def _read_system_stats(thermal_zone_path: str = None, lan_interface: str = None, prev_net=None, prev_time=None) -> dict:
    """
    Gather real system metrics.
    
    Args:
        thermal_zone_path: Optional board-specific path to thermal zone temp file.
        prev_net: Previous network counters (per-connection state).
        prev_time: Previous timestamp (per-connection state).
    
    Returns:
        Tuple of (stats_dict, current_net_counters, current_time).
    """

    # CPU & RAM
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent

    # Temperature — try board-specific path first, then fall back to psutil sensors
    temp = 0.0
    
    # Try board-specific thermal zone path if available
    if thermal_zone_path:
        try:
            with open(thermal_zone_path, "r") as f:
                temp_millidegrees = int(f.read().strip())
                temp = temp_millidegrees / 1000.0  # Convert millidegrees C to degrees C
        except Exception:
            # Fall through to psutil approach
            pass
    
    # Fall back to psutil sensors if board path didn't work or wasn't provided
    if temp == 0.0:
        temps = psutil.sensors_temperatures()
        if temps:
            # CPU sensors by name. 'k10temp' (AMD) and 'zenpower' were missing until the x86
            # thin client was checked on hardware 2026-08-04 — it exposes only k10temp and
            # amdgpu, so it fell through to the arbitrary-first-sensor branch below and
            # happened to land on the right one purely because of dict ordering.
            for key in ("cpu_thermal", "soc_thermal", "coretemp", "k10temp", "zenpower",
                        "acpitz", "cpu-thermal"):
                if key in temps and temps[key]:
                    temp = temps[key][0].current
                    break
            if temp == 0.0:
                # Last resort: an unrecognised sensor set. Picking the first one means the
                # value could be a GPU, chipset or drive reading shown to the user as CPU
                # temperature — plausible, wrong, and indistinguishable from correct. Prefer
                # the hottest instead: on every board checked the CPU is the hottest component,
                # and it degrades to a defensible "hottest thing in this machine" rather than
                # an arbitrary one.
                readings = [s.current for group in temps.values() for s in group
                            if s.current is not None]
                if readings:
                    temp = max(readings)

    # Uptime
    uptime_sec = int(time.time() - _boot_time)

    # Network throughput (delta) — per-connection counters
    now_time = time.monotonic()
    now_net, active_iface = _pick_network_counters(lan_interface)
    if prev_net and prev_time:
        dt = max(now_time - prev_time, 0.25)
        up_mbps = ((now_net.bytes_sent - prev_net.bytes_sent) * 8) / (dt * 1_000_000)
        down_mbps = ((now_net.bytes_recv - prev_net.bytes_recv) * 8) / (dt * 1_000_000)
    else:
        up_mbps = 0.0
        down_mbps = 0.0

    # Disk
    try:
        usage = disk_usage(str(settings.nas_root))
        total_gb = usage.total / (1024 ** 3)
        used_gb = usage.used / (1024 ** 3)
    except Exception:
        total_gb = settings.total_storage_gb
        used_gb = 0.0

    stats = {
        "cpuPercent": round(cpu, 1),
        "ramPercent": round(ram, 1),
        "tempCelsius": round(temp, 1),
        "uptimeSeconds": uptime_sec,
        "networkUpMbps": round(up_mbps, 2),
        "networkDownMbps": round(down_mbps, 2),
        "networkInterface": active_iface,
        "storage": {
            "totalGB": round(total_gb, 1),
            "usedGB": round(used_gb, 1),
        },
    }
    return stats, now_net, now_time


@router.websocket("/ws/monitor")
async def monitor_ws(ws: WebSocket, token: str = Query(default=None)):
    """
    Stream system stats to the Flutter app every 1 second.
    Uses board-specific thermal zone path for accurate temperature readings.
    Requires JWT token as query parameter: /ws/monitor?token=<jwt>
    """
    # Authenticate before accepting
    if not token:
        await ws.close(code=4001, reason="Missing token")
        return
    try:
        claims = decode_token(token)
    except Exception:
        await ws.close(code=4003, reason="Invalid token")
        return

    user_id = claims.get("sub", "anonymous")

    global _ws_connection_count
    if _ws_connection_count >= _MAX_WS_CONNECTIONS:
        await ws.close(code=4029, reason="Too many connections")
        return
    if _ws_connections_per_user.get(user_id, 0) >= _MAX_WS_PER_USER:
        await ws.close(code=4029, reason="Too many connections for this user")
        return

    await ws.accept()
    _ws_connection_count += 1
    _ws_connections_per_user[user_id] = _ws_connections_per_user.get(user_id, 0) + 1

    # Get board-specific thermal zone path from app state
    board = getattr(ws.app.state, 'board', None)
    thermal_zone_path = board.thermal_zone_path if board else None

    # Prime the CPU meter (first call always returns 0)
    psutil.cpu_percent(interval=None)

    # Per-connection network counters (stick to detected LAN interface when possible)
    lan_interface = board.lan_interface if board else None
    prev_net, _ = _pick_network_counters(lan_interface)
    prev_time = time.monotonic()

    # Throttle storage warnings (at most once per 5 minutes)
    _last_storage_warn = 0

    try:
        while True:
            stats, prev_net, prev_time = _read_system_stats(
                thermal_zone_path, lan_interface, prev_net, prev_time
            )
            await ws.send_text(json.dumps(stats))

            # Check storage thresholds periodically
            storage = stats["storage"]
            total = storage["totalGB"]
            used = storage["usedGB"]
            if total > 0:
                pct = (used / total) * 100
                free = total - used
                now = time.time()
                if pct >= 85 and (now - _last_storage_warn) > 300:
                    _last_storage_warn = now
                    await emit_storage_warning(pct, free)

            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        pass
    except Exception:
        await ws.close()
    finally:
        _ws_connection_count -= 1
        user_count = _ws_connections_per_user.get(user_id, 1) - 1
        if user_count <= 0:
            _ws_connections_per_user.pop(user_id, None)
        else:
            _ws_connections_per_user[user_id] = user_count
