"""
In-process mDNS/DNS-SD advertisement via `zeroconf`, as a cross-platform alternative to
`avahi-daemon` (Linux-only, configured statically by install.sh's configure_mdns()).

Why this exists: Windows has no avahi equivalent to install, so a Windows port needs
*something* to advertise `_aihomecloud._tcp` — this is that something. `zeroconf` is pure
Python, speaks standard mDNS/DNS-SD (RFC 6762/6763), and is wire-compatible with whatever
resolves the record on the other end: Android's NsdDiscovery.kt uses the platform's native
NSD API, which only cares that a standard mDNS packet arrives, not which process sent it.
That's also why this module is safe to introduce without touching NsdDiscovery.kt at all.

Deliberately NOT wired into the Linux startup path by default (see main.py's lifespan) —
the 3 production boards already have a working avahi-based advertisement via install.sh, and
running two advertisers of the same service type on one host is a real, if usually harmless,
source of confusing mDNS behavior worth avoiding rather than risking on live boards for no
present benefit. This module exists so Windows (which needs it) and any future opt-in Linux
migration (which doesn't need it yet) share one implementation instead of two.
"""

from __future__ import annotations

import logging
import socket

from .config import get_local_ip, settings

logger = logging.getLogger("aihomecloud.mdns")

_SERVICE_TYPE = "_aihomecloud._tcp.local."

_zeroconf_instance = None  # type: ignore[var-annotated]
_service_info = None  # type: ignore[var-annotated]


async def start() -> bool:
    """
    Register this board's service record. Returns True on success, False on any failure —
    never raises, matching every other best-effort startup step in main.py's lifespan (a
    board with mDNS advertisement briefly not working is still fully usable by IP/manual
    entry; failing the whole backend over it would not be).

    Uses the async API (AsyncZeroconf/async_register_service), not the sync Zeroconf class —
    found live 2026-08-19: the sync register_service() internally waits on its own event
    loop via a blocking call, which deadlocks (zeroconf._exceptions.EventLoopBlocked) when
    called from inside a process that already has an asyncio event loop running, which a
    FastAPI lifespan always does. Caught by actually exercising this through main.py's real
    lifespan_context, not just a standalone unit test outside that context.
    """
    global _zeroconf_instance, _service_info

    try:
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
    except ImportError:
        logger.warning("mdns_advertiser: zeroconf not installed — skipping in-process advertisement")
        return False

    try:
        ip = get_local_ip()
        addr_bytes = socket.inet_aton(ip)
        device_name = (settings.device_name or "AiHomeCloud").strip() or "AiHomeCloud"
        # DNS-SD instance names must be unique on the network and are conventionally
        # "<human name>.<service type>" — device_serial keeps two boards with the same
        # device_name from colliding.
        instance_name = f"{device_name} ({settings.device_serial})"

        info = ServiceInfo(
            _SERVICE_TYPE,
            f"{instance_name}.{_SERVICE_TYPE}",
            addresses=[addr_bytes],
            port=settings.port,
            properties={"version": settings.backend_version},
            server=f"{socket.gethostname()}.local.",
        )

        azc = AsyncZeroconf()
        await azc.async_register_service(info)

        _zeroconf_instance = azc
        _service_info = info
        logger.info("mdns_advertiser: registered %s on %s:%s", instance_name, ip, settings.port)
        return True

    except OSError as e:
        # Covers: no local IP resolvable yet, socket bind failures, port already in use by
        # another mDNS responder. All recoverable by simply not advertising this boot.
        logger.warning("mdns_advertiser: registration failed — %s", e)
        return False


async def stop() -> None:
    """Unregister and close. Safe to call even if start() was never called or failed."""
    global _zeroconf_instance, _service_info

    if _zeroconf_instance is None:
        return

    try:
        if _service_info is not None:
            await _zeroconf_instance.async_unregister_service(_service_info)
        await _zeroconf_instance.async_close()
        logger.info("mdns_advertiser: stopped")
    except OSError as e:
        logger.warning("mdns_advertiser: shutdown had an error — %s", e)
    finally:
        _zeroconf_instance = None
        _service_info = None
