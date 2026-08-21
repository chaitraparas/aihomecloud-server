"""
Windows system-tray helper for AiHomeCloud -- Start/Stop the backend service, open the
dashboard, quit the tray icon. The backend itself runs headless as two NSSM services
(AiHomeCloud, AiHomeCloudCertIssuer); this is a separate, always-running-per-user process with
no equivalent on Linux (the SBCs are headless by design, no desktop session to put a tray icon
in) -- entrypoint is `pythonw.exe -m app.windows_tray`, launched at login via the
HKLM Run key install_windows.ps1 registers, never as a Windows service itself, since services
cannot show tray/GUI in modern Windows session isolation.

Privilege note (see install_windows.ps1's Grant-ServiceControlToUser): starting/stopping the
`AiHomeCloud` service requires SERVICE_START/SERVICE_STOP rights that a non-Administrator does
not have by default. The installer grants those specifically to the AiHomeCloud service object
for the Authenticated Users group -- this process itself runs with no elevation and gains no
other privilege from that grant (no file/TLS/identity access change).

"Quit" here means close the tray icon only -- the NAS keeps running in the background
regardless (that's the whole point of a NAS), matching "Start/Stop Server" being a separate,
explicit menu action from "Quit".

UNTESTED ON REAL WINDOWS -- written with no Windows machine available (see
kb/handoff_windows_installer_2026-08-20.md). pywin32/pystray imports are deferred into the
functions that need them (matching windows_secrets.py's own pattern) so the handful of pure
functions below stay importable and testable on any platform -- see
tests/test_windows_tray_logic.py.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("aihomecloud.windows_tray")

_SERVICE_NAME = "AiHomeCloud"


# ---------------------------------------------------------------------------------------------
# Pure logic -- no pywin32/pystray import here, so this half is testable on any platform.
# ---------------------------------------------------------------------------------------------

def dashboard_url(port: int) -> str:
    """Same host/port the health check in install_windows.ps1 polls -- localhost only, this
    process runs on the same machine as the service it's a tray for."""
    return f"https://localhost:{port}/app/"


def status_label(running: bool) -> str:
    return "AiHomeCloud — Running" if running else "AiHomeCloud — Stopped"


def resolve_port() -> int:
    """Reads the same AHC_PORT the NSSM service itself is configured with
    (install_windows.ps1's Install-Service -> AppEnvironmentExtra) rather than importing the
    full app.config module -- this process has no need for the rest of that settings surface,
    and importing it would pull in FastAPI/pydantic for no reason."""
    raw = os.environ.get("AHC_PORT", "8443")
    try:
        return int(raw)
    except ValueError:
        logger.warning("AHC_PORT=%r is not a valid port, falling back to 8443", raw)
        return 8443


def icon_color(running: bool | None) -> str:
    """running=None means "status unknown/query failed" -- distinct from both states, shown
    amber rather than defaulting to either green or red (which would silently misreport)."""
    if running is None:
        return "#B4740E"
    return "#1E8E5A" if running else "#8A2B23"


# ---------------------------------------------------------------------------------------------
# Windows-only glue -- pywin32/pystray imported lazily, only reached when actually running.
# ---------------------------------------------------------------------------------------------

def _query_running() -> bool | None:
    try:
        import win32service  # noqa: PLC0415 -- Windows-only, deliberately deferred
        import win32serviceutil  # noqa: PLC0415
    except ImportError:
        logger.warning("pywin32 not available -- cannot query service status")
        return None
    try:
        status = win32serviceutil.QueryServiceStatus(_SERVICE_NAME)[1]
        return status == win32service.SERVICE_RUNNING
    except Exception:  # noqa: BLE001 -- any SCM error just means "unknown", not a crash
        logger.exception("failed to query %s service status", _SERVICE_NAME)
        return None


def _make_icon_image(color_hex: str):
    from PIL import Image, ImageDraw  # noqa: PLC0415 -- Pillow, already a pinned dependency

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=color_hex)
    return img


def _start_service() -> None:
    import win32serviceutil  # noqa: PLC0415

    try:
        win32serviceutil.StartService(_SERVICE_NAME)
    except Exception:  # noqa: BLE001 -- surfaced via tray notification, not a crash
        logger.exception("failed to start %s", _SERVICE_NAME)
        raise


def _stop_service() -> None:
    import win32serviceutil  # noqa: PLC0415

    try:
        win32serviceutil.StopService(_SERVICE_NAME)
    except Exception:  # noqa: BLE001
        logger.exception("failed to stop %s", _SERVICE_NAME)
        raise


def run() -> None:
    """Blocking entrypoint -- `python -m app.windows_tray` calls this."""
    import webbrowser  # noqa: PLC0415

    import pystray  # noqa: PLC0415 -- Windows-only in practice (requirements.txt pins it
    # sys_platform == "win32"), deferred so importing this module elsewhere never fails

    port = resolve_port()
    icon_holder: dict[str, object] = {}

    def refresh_icon() -> None:
        running = _query_running()
        icon_holder["icon"].icon = _make_icon_image(icon_color(running))
        icon_holder["icon"].title = status_label(bool(running)) if running is not None else \
            "AiHomeCloud — status unknown"

    def on_start(icon, item):  # noqa: ANN001, ARG001 -- pystray callback signature
        try:
            _start_service()
            icon.notify("AiHomeCloud server starting…", "AiHomeCloud")
        except Exception:  # noqa: BLE001
            icon.notify("Could not start the server — check the service logs.", "AiHomeCloud")
        refresh_icon()

    def on_stop(icon, item):  # noqa: ANN001, ARG001
        try:
            _stop_service()
            icon.notify("AiHomeCloud server stopped.", "AiHomeCloud")
        except Exception:  # noqa: BLE001
            icon.notify("Could not stop the server — check the service logs.", "AiHomeCloud")
        refresh_icon()

    def on_open(icon, item):  # noqa: ANN001, ARG001
        webbrowser.open(dashboard_url(port))

    def on_quit(icon, item):  # noqa: ANN001, ARG001
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Start Server", on_start),
        pystray.MenuItem("Stop Server", on_stop),
        pystray.MenuItem("Open Dashboard", on_open),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon("AiHomeCloud", _make_icon_image(icon_color(None)), "AiHomeCloud", menu)
    icon_holder["icon"] = icon
    refresh_icon()
    icon.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
