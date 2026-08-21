"""
Real advertise-and-discover round trip, not a mock. A separate Zeroconf browser instance
independently resolves what mdns_advertiser.start() registers — this is the actual thing
Android's NsdDiscovery.kt depends on working, so a test that only checks internal state
(e.g. that _service_info was set) would pass even if the service were never really
discoverable on the network. Skipped automatically if `zeroconf` isn't installed.

start()/stop() are async (AsyncZeroconf, not the sync Zeroconf class) — found live
2026-08-19 that the sync API deadlocks (EventLoopBlocked) when called from a process that
already has an asyncio event loop running, which main.py's FastAPI lifespan always does.
Caught by exercising this through the real lifespan_context, not just this file's tests.
"""

import time

import pytest

zeroconf = pytest.importorskip("zeroconf")

from app import mdns_advertiser  # noqa: E402


@pytest.fixture(autouse=True)
async def _clean_state():
    yield
    await mdns_advertiser.stop()


@pytest.mark.asyncio
async def test_advertised_service_is_independently_discoverable(monkeypatch):
    from zeroconf import ServiceBrowser, Zeroconf

    monkeypatch.setattr("app.mdns_advertiser.get_local_ip", lambda: "127.0.0.1")

    class _Settings:
        device_name = "Test Board"
        device_serial = "AHC-TEST-0001"
        port = 8443
        backend_version = "0.0.0-test"

    monkeypatch.setattr("app.mdns_advertiser.settings", _Settings())

    assert await mdns_advertiser.start() is True

    found = {}

    class _Listener:
        def add_service(self, zc, service_type, name):
            # A real board on the same network segment may already be advertising this exact
            # service type (confirmed live in dev: an existing AiHomeCloud board's avahi
            # broadcast showed up here too) — filter to this test's own instance rather than
            # assuming the first result found is the one just registered.
            if not name.startswith("Test Board (AHC-TEST-0001)"):
                return
            info = zc.get_service_info(service_type, name)
            if info:
                found["name"] = name
                found["port"] = info.port
                found["version"] = info.properties.get(b"version")

        def remove_service(self, zc, service_type, name):
            pass

        def update_service(self, zc, service_type, name):
            pass

    # Deliberately the SYNC Zeroconf/ServiceBrowser here, as an independent verifier using a
    # different code path than mdns_advertiser's own AsyncZeroconf — but the sync class's
    # blocking calls can't run on this test's own event loop thread (same class of conflict
    # start()/stop() had to work around), so this runs on a plain thread instead.
    def _browse_sync():
        browser_zc = Zeroconf()
        try:
            ServiceBrowser(browser_zc, mdns_advertiser._SERVICE_TYPE, _Listener())
            deadline = time.time() + 5
            while not found and time.time() < deadline:
                time.sleep(0.1)
        finally:
            browser_zc.close()

    import asyncio
    await asyncio.to_thread(_browse_sync)

    assert found.get("name", "").startswith("Test Board (AHC-TEST-0001)")
    assert found.get("port") == 8443
    assert found.get("version") == b"0.0.0-test"


@pytest.mark.asyncio
async def test_stop_before_start_is_a_safe_no_op():
    await mdns_advertiser.stop()  # must not raise


@pytest.mark.asyncio
async def test_start_without_zeroconf_installed_returns_false(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "zeroconf":
            raise ImportError("simulated: zeroconf not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    assert await mdns_advertiser.start() is False
