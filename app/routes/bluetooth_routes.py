"""
Bluetooth routes — radio power, scan, device list, pair/connect. See bluetooth_manager.py's
module docstring for the bluetoothctl scripting approach and this fleet's hardware caveats.
"""

import logging

from fastapi import APIRouter, Depends

from .. import platform_profile
from .. import bluetooth_manager
from ..auth import get_current_user, require_admin
from ..models import BluetoothConnectRequest, BluetoothDevice, BluetoothStatus, ToggleRequest
from ..models import BluetoothPowerResponse, SuccessResponse

logger = logging.getLogger("aihomecloud.bluetooth_routes")

router = APIRouter(prefix="/api/v1/bluetooth", tags=["bluetooth"])


@router.get("/status", response_model=BluetoothStatus)
async def bluetooth_status(_user: dict = Depends(get_current_user)):
    """Adapter presence, power state, and known devices (paired + last scan's results)."""
    status = await bluetooth_manager.get_status()
    return BluetoothStatus(
        adapterPresent=status["adapter_present"],
        enabled=status["enabled"],
        devices=[BluetoothDevice(**d) for d in status["devices"]],
    )


@router.put("/power", response_model=BluetoothPowerResponse)
async def bluetooth_power(body: ToggleRequest, _user: dict = Depends(require_admin)):
    platform_profile.require(platform_profile.Capability.BLUETOOTH)
    """Toggle the Bluetooth radio on/off."""
    ok = await bluetooth_manager.set_power(body.enabled)
    status = await bluetooth_manager.get_status()
    return {"success": ok, "enabled": status["enabled"]}


@router.post("/scan", response_model=list[BluetoothDevice])
async def bluetooth_scan(_user: dict = Depends(require_admin)):
    platform_profile.require(platform_profile.Capability.BLUETOOTH)
    """Scan for nearby devices (~8s) and return the resulting device list."""
    devices = await bluetooth_manager.scan()
    return [BluetoothDevice(**d) for d in devices]


@router.post("/pair", response_model=SuccessResponse)
async def bluetooth_pair(body: BluetoothConnectRequest, _user: dict = Depends(require_admin)):
    platform_profile.require(platform_profile.Capability.BLUETOOTH)
    """Pair with a device by address."""
    ok = await bluetooth_manager.pair(body.address)
    return {"success": ok}


@router.post("/connect", response_model=SuccessResponse)
async def bluetooth_connect(body: BluetoothConnectRequest, _user: dict = Depends(require_admin)):
    platform_profile.require(platform_profile.Capability.BLUETOOTH)
    """Connect to an already-paired device by address."""
    ok = await bluetooth_manager.connect(body.address)
    return {"success": ok}
