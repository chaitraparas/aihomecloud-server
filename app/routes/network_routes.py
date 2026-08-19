"""
Network routes — WiFi status, user toggle, and LAN network status.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import platform_profile
from .. import bluetooth_manager
from ..auth import get_current_user, require_admin
from ..models import (
    WifiStatusResponse,
    SuccessResponse,
    HotspotActionResult,
    HotspotConfigRequest,
    HotspotStatus,
    NetworkStatus,
    WifiConnectRequest,
    WifiConnectionResult,
    WifiForgetRequest,
    WifiNetwork,
)
from ..subprocess_runner import run_command
from ..wifi_manager import (
    connect_to_network,
    disable_hotspot,
    enable_hotspot,
    forget_network,
    get_hotspot_status,
    get_wifi_connection_info,
    get_wifi_status,
    scan_networks,
    set_user_wifi_override,
)

logger = logging.getLogger("aihomecloud.network")

router = APIRouter(prefix="/api/v1", tags=["network"])


@router.get("/network/status", response_model=NetworkStatus)
async def network_status(_user: dict = Depends(get_current_user)):
    """Return LAN-only network state for ethernet-connected device."""
    lan_connected = False
    lan_ip = None
    lan_speed = None

    net_dir = Path("/sys/class/net")
    if net_dir.exists():
        for iface in sorted(net_dir.iterdir()):
            name = iface.name
            if name == "lo" or name.startswith("wl") or name.startswith("docker") or name.startswith("veth"):
                continue
            operstate = iface / "operstate"
            if operstate.exists():
                try:
                    state = operstate.read_text().strip()
                except OSError:
                    continue
                if state != "up":
                    continue
                lan_connected = True
                # Get IP address
                rc, out, _ = await run_command(["ip", "-4", "addr", "show", name])
                if rc == 0:
                    for line in out.splitlines():
                        line = line.strip()
                        if line.startswith("inet "):
                            lan_ip = line.split()[1].split("/")[0]
                            break
                # Get link speed
                speed_file = iface / "speed"
                if speed_file.exists():
                    try:
                        speed_val = speed_file.read_text().strip()
                        if speed_val.lstrip("-").isdigit() and int(speed_val) > 0:
                            lan_speed = f"{speed_val} Mb/s"
                    except OSError:
                        pass
                break

    wifi = await get_wifi_status()
    wifi_conn = await get_wifi_connection_info()
    hotspot = await get_hotspot_status()
    bt = await bluetooth_manager.get_status()

    return NetworkStatus(
        wifiEnabled=bool(wifi.get("wifiEnabled")),
        wifiConnected=wifi_conn["connected"],
        wifiSsid=wifi_conn["ssid"],
        wifiIp=wifi_conn["ip"],
        hotspotEnabled=hotspot["enabled"],
        hotspotSsid=hotspot["ssid"],
        bluetoothEnabled=bt["enabled"],
        lanConnected=lan_connected,
        lanIp=lan_ip,
        lanSpeed=lan_speed,
        gateway=None,
        dns=None,
    )


@router.get("/network/wifi", response_model=WifiStatusResponse)
async def wifi_status(_user: dict = Depends(get_current_user)):
    """Return WiFi radio state, Ethernet link status, and user override flag."""
    return await get_wifi_status()


class WifiToggleRequest(BaseModel):
    enabled: bool


@router.put("/network/wifi", response_model=WifiStatusResponse)
async def toggle_wifi(body: WifiToggleRequest, _user: dict = Depends(require_admin)):
    platform_profile.require(platform_profile.Capability.NETWORK_RADIOS)
    """Toggle WiFi radio."""
    await set_user_wifi_override(body.enabled)
    return await get_wifi_status()


@router.get("/network/wifi/scan", response_model=list[WifiNetwork])
async def wifi_scan(_user: dict = Depends(require_admin)):
    platform_profile.require(platform_profile.Capability.NETWORK_RADIOS)
    """Scan for available Wi-Fi networks, deduped by SSID, strongest signal first."""
    networks = await scan_networks()
    return [WifiNetwork(**n) for n in networks]


@router.post("/network/wifi/connect", response_model=WifiConnectionResult)
async def wifi_connect(body: WifiConnectRequest, _user: dict = Depends(require_admin)):
    platform_profile.require(platform_profile.Capability.NETWORK_RADIOS)
    """Join a Wi-Fi network — writes a connection profile and activates it."""
    result = await connect_to_network(body.ssid, body.password)
    return WifiConnectionResult(**result)


@router.post("/network/wifi/forget", response_model=SuccessResponse)
async def wifi_forget(body: WifiForgetRequest, _user: dict = Depends(require_admin)):
    platform_profile.require(platform_profile.Capability.NETWORK_RADIOS)
    """Remove a saved Wi-Fi connection profile."""
    ok = await forget_network(body.ssid)
    return {"success": ok}


@router.get("/network/hotspot", response_model=HotspotStatus)
async def hotspot_status(_user: dict = Depends(get_current_user)):
    """Whether this board's WiFi device is currently running as an access point."""
    status = await get_hotspot_status()
    return HotspotStatus(adapterPresent=status["adapter_present"], enabled=status["enabled"], ssid=status["ssid"])


@router.post("/network/hotspot/enable", response_model=HotspotActionResult)
async def hotspot_enable(body: HotspotConfigRequest, _user: dict = Depends(require_admin)):
    """Start the hotspot with the given SSID/password."""
    result = await enable_hotspot(body.ssid, body.password)
    return HotspotActionResult(**result)


@router.post("/network/hotspot/disable", response_model=HotspotActionResult)
async def hotspot_disable(_user: dict = Depends(require_admin)):
    """Stop the hotspot."""
    ok = await disable_hotspot()
    return HotspotActionResult(success=ok, message="Hotspot stopped" if ok else "Failed to stop hotspot")
