"""
Board abstraction layer for multi-SBC support (Cubie A7A, Cubie A7Z, Raspberry Pi 4, etc).

Handles:
- Board model detection from /proc/device-tree/model
- Thermal zone path resolution (auto-detect or fallback)
- LAN interface discovery (auto-detect or fallback)
- CPU governor path mapping
"""

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BoardConfig:
    """Immutable hardware board configuration."""
    model_name: str
    thermal_zone_path: str
    lan_interface: str
    cpu_governor_path: str


# Known board configurations (thermal_zone_path and lan_interface are auto-detected at runtime)
# Keys are exact DTB model strings from /proc/device-tree/model
KNOWN_BOARDS: dict[str, BoardConfig] = {
    # Radxa CUBIE A7A — Allwinner A527 (sun60iw2) SoC
    "sun60iw2": BoardConfig(
        model_name="Radxa CUBIE A7A",
        thermal_zone_path="",  # Will be overridden by auto-detection
        lan_interface="",  # Will be overridden by auto-detection
        cpu_governor_path="/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
    ),
    # Radxa CUBIE A7Z (Rockchip-based variant)
    "Radxa CUBIE A7Z": BoardConfig(
        model_name="Radxa CUBIE A7Z",
        thermal_zone_path="",  # Will be overridden by auto-detection
        lan_interface="",  # Will be overridden by auto-detection
        cpu_governor_path="/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
    ),
    "Raspberry Pi 4 Model B": BoardConfig(
        model_name="Raspberry Pi 4 Model B",
        thermal_zone_path="",  # Will be overridden by auto-detection
        lan_interface="",  # Will be overridden by auto-detection
        cpu_governor_path="/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
    ),
    # Radxa ROCK Pi 4A (RK3399 SoC, Armbian Ubuntu 24.04, LAN: end0)
    "Radxa ROCK Pi 4A": BoardConfig(
        model_name="Radxa ROCK Pi 4A",
        thermal_zone_path="",  # Will be overridden by auto-detection
        lan_interface="",  # Will be overridden by auto-detection
        cpu_governor_path="/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
    ),
}

# Substring patterns for fuzzy board matching (used when exact DTB string isn't in KNOWN_BOARDS)
# Maps lowercase substrings → canonical model names in KNOWN_BOARDS
_BOARD_SUBSTRINGS: list[tuple[str, str]] = [
    ("sun60iw2", "sun60iw2"),       # Allwinner A527 / Cubie A7A
    ("cubie a7z", "Radxa CUBIE A7Z"),
    ("raspberry pi 4", "Raspberry Pi 4 Model B"),
    ("rock pi 4", "Radxa ROCK Pi 4A"),
]

# Fallback default board (thermal_zone_path and lan_interface are auto-detected at runtime)
DEFAULT_BOARD = BoardConfig(
    model_name="unknown",
    thermal_zone_path="",  # Will be overridden by auto-detection
    lan_interface="",  # Will be overridden by auto-detection
    cpu_governor_path="/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
)


def find_thermal_zone() -> str:
    """
    Auto-detect the appropriate thermal zone path by scanning /sys/class/thermal/.

    Scans thermal_zone0, thermal_zone1, etc. for the first zone whose 'type' file
    contains 'cpu', 'soc', 'pkg', or 'acpi' (case-insensitive). Falls back to
    thermal_zone0 if no matching zone is found.

    The 'pkg'/'acpi' patterns cover x86 ACPI thermal zones, commonly typed
    'x86_pkg_temp' or 'acpitz' -- neither matches 'cpu'/'soc' alone, which are
    ARM/SoC-board conventions (found auditing x86 thin-client support, 2026-07-17).

    Returns:
        Path string like "/sys/class/thermal/thermal_zone0/temp" (or thermal_zone1, etc).
    """
    thermal_base = Path("/sys/class/thermal")

    # Try to find a thermal zone with cpu or soc in its type
    try:
        if thermal_base.exists():
            # List all thermal_zone* directories
            zones = sorted(thermal_base.glob("thermal_zone*"))
            for zone_dir in zones:
                type_file = zone_dir / "type"
                try:
                    with open(type_file, "r") as f:
                        zone_type = f.read().strip().lower()
                        if any(kw in zone_type for kw in ("cpu", "soc", "pkg", "acpi")):
                            thermal_zone_path = str(zone_dir / "temp")
                            logger.info(
                                "thermal_zone_detected path=%s zone_type=%s",
                                thermal_zone_path,
                                zone_type,
                            )
                            return thermal_zone_path
                except (FileNotFoundError, OSError):
                    # Zone directory exists but can't read type, continue to next
                    continue
    except Exception as e:
        logger.debug(
            "thermal_zone_scan_error error=%s",
            str(e),
        )
    
    # Fallback to thermal_zone0
    fallback_path = "/sys/class/thermal/thermal_zone0/temp"
    logger.warning(
        "thermal_zone_fallback path=%s reason=no_cpu_or_soc_zone_found",
        fallback_path,
    )
    return fallback_path


def find_lan_interface() -> str:
    """
    Auto-detect the primary Ethernet LAN interface by scanning /sys/class/net/.
    
    Scans network interfaces in /sys/class/net/, skips loopback (lo), and returns
    the first interface with type file containing "1" (Ethernet type).
    Falls back to eth0 if no Ethernet interface is found.
    
    Returns:
        Interface name like "eth0", "end0", "enp1s0", or fallback "eth0".
    """
    net_base = Path("/sys/class/net")
    
    # Try to find the first Ethernet interface
    try:
        if net_base.exists():
            # List all network interfaces
            interfaces = sorted(net_base.iterdir())
            for iface_dir in interfaces:
                iface_name = iface_dir.name
                
                # Skip loopback
                if iface_name == "lo":
                    continue
                
                # Check interface type (1 = Ethernet)
                type_file = iface_dir / "type"
                try:
                    with open(type_file, "r") as f:
                        iface_type = f.read().strip()
                        if iface_type == "1":  # Ethernet type
                            logger.info(
                                "lan_interface_detected interface=%s type=%s",
                                iface_name,
                                iface_type,
                            )
                            return iface_name
                except (FileNotFoundError, OSError):
                    # Interface directory exists but can't read type, continue to next
                    continue
    except Exception as e:
        logger.debug(
            "lan_interface_scan_error error=%s",
            str(e),
        )
    
    # Fallback to eth0
    fallback_iface = "eth0"
    logger.info(
        "lan_interface_fallback interface=%s reason=no_ethernet_interface_found",
        fallback_iface,
    )
    return fallback_iface


def detect_board() -> BoardConfig:
    """
    Detect the current board by reading /proc/device-tree/model.
    Auto-detects thermal zone path by scanning /sys/class/thermal/.
    Auto-detects LAN interface by scanning /sys/class/net/.
    
    Falls back to DEFAULT_BOARD if detection fails or board not recognized.
    Logs the detected board, thermal zone, and LAN interface at startup.
    
    Returns:
        BoardConfig matching the detected board, with auto-detected thermal zone path and LAN interface.
    """
    model_name = None
    
    try:
        with open("/proc/device-tree/model", "r") as f:
            model_name = f.read().rstrip("\x00\n")  # Strip null bytes and newlines
    except (FileNotFoundError, OSError) as e:
        logger.warning(
            "board_detection_failed path=/proc/device-tree/model error=%s",
            str(e),
        )
        thermal_zone_path = find_thermal_zone()
        lan_interface = find_lan_interface()
        board = BoardConfig(
            model_name=DEFAULT_BOARD.model_name,
            thermal_zone_path=thermal_zone_path,
            lan_interface=lan_interface,
            cpu_governor_path=DEFAULT_BOARD.cpu_governor_path,
        )
        logger.info("board_detected model_name=%s reason=fallback", board.model_name)
        return board
    
    # Look up in known boards (exact match first)
    if model_name in KNOWN_BOARDS:
        base_board = KNOWN_BOARDS[model_name]
        thermal_zone_path = find_thermal_zone()
        lan_interface = find_lan_interface()
        board = BoardConfig(
            model_name=base_board.model_name,
            thermal_zone_path=thermal_zone_path,
            lan_interface=lan_interface,
            cpu_governor_path=base_board.cpu_governor_path,
        )
        logger.info(
            "board_detected model_name=%s thermal_zone=%s lan_interface=%s",
            board.model_name,
            board.thermal_zone_path,
            board.lan_interface,
        )
        return board

    # Substring / fuzzy match for DTB strings that embed the SoC identifier
    model_lower = model_name.lower()
    for substring, known_key in _BOARD_SUBSTRINGS:
        if substring in model_lower:
            base_board = KNOWN_BOARDS[known_key]
            thermal_zone_path = find_thermal_zone()
            lan_interface = find_lan_interface()
            board = BoardConfig(
                model_name=base_board.model_name,
                thermal_zone_path=thermal_zone_path,
                lan_interface=lan_interface,
                cpu_governor_path=base_board.cpu_governor_path,
            )
            logger.info(
                "board_detected model_name=%s dtb_string=%s match=substring thermal_zone=%s lan_interface=%s",
                board.model_name,
                model_name,
                board.thermal_zone_path,
                board.lan_interface,
            )
            return board
    
    # Unknown board, use defaults with auto-detected thermal zone and LAN interface
    thermal_zone_path = find_thermal_zone()
    lan_interface = find_lan_interface()
    board = BoardConfig(
        model_name=DEFAULT_BOARD.model_name,
        thermal_zone_path=thermal_zone_path,
        lan_interface=lan_interface,
        cpu_governor_path=DEFAULT_BOARD.cpu_governor_path,
    )
    logger.warning(
        "board_unknown model_name=%s falling_back_to=%s",
        model_name,
        DEFAULT_BOARD.model_name,
    )
    logger.info(
        "board_detected model_name=%s thermal_zone=%s lan_interface=%s reason=unknown_model_fallback",
        board.model_name,
        board.thermal_zone_path,
        board.lan_interface,
    )
    return board
