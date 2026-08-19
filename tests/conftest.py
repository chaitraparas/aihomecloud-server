import pytest

# Ensure asyncio fixtures have HTTP client
from httpx import AsyncClient, ASGITransport

# Set environment variable before importing the app so Settings picks it up
# Note: event_loop fixture removed — pytest-asyncio 0.23.x auto-provides per-function event loops.

@pytest.fixture
async def client(tmp_path, monkeypatch):
    # Point the app data dir AND NAS root to temporary paths before FastAPI loads settings
    monkeypatch.setenv("AHC_DATA_DIR", str(tmp_path))

    nas_tmp = tmp_path / "nas"
    nas_tmp.mkdir()
    (nas_tmp / "shared").mkdir()
    (nas_tmp / "personal").mkdir()

    from app.config import settings
    from app.main import app
    from app import store
    from app import media_index

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "nas_root", nas_tmp)
    monkeypatch.setattr(settings, "skip_mount_check", True)
    # Background work yields to user activity in production (app/workload.py). In tests the
    # request that triggers a job also marks the board busy, so the job would sit out the quiet
    # period and the test would hang waiting for it. Off by default here; test_workload.py turns
    # it back on for the tests that are actually about this behaviour.
    monkeypatch.setattr(settings, "workload_yield_enabled", False, raising=False)

    # Clear module-level cache so stale data from previous tests is discarded
    store._cache.clear()

    # ASGITransport doesn't trigger the app's lifespan (which normally calls
    # media_index.init_db()), so any test touching ingest()/media routes
    # would otherwise hit "no such table: blobs" — init it directly here,
    # against the same monkeypatched (per-test, tmp_path-scoped) data_dir.
    await media_index.init_db()

    # Reset rate limiter storage and account lockout between tests
    try:
        from app.limiter import limiter
        limiter._storage.reset()
    except Exception:
        pass
    try:
        from app.routes.auth_routes import _failed_logins
        _failed_logins.clear()
    except Exception:
        pass

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    # Cleanup after test
    store._cache.clear()
    await media_index.close_db()


@pytest.fixture
async def admin_token(client: AsyncClient):
    """
    Create an admin user and return a valid JWT access token.
    Uses a short PIN (4 digits) to avoid bcrypt 72-byte limit issues.
    Named "admin" to match test expectations.
    """
    # Create first user (becomes admin) and login to obtain token
    name = "admin"
    pin = "0000"  # Keep PIN short to avoid bcrypt byte limit issues
    
    # Create user
    resp = await client.post("/api/v1/users", json={"name": name, "pin": pin})
    assert resp.status_code in (200, 201), f"User creation failed: {resp.text}"

    # Login
    resp = await client.post("/api/v1/auth/login", json={"name": name, "pin": pin})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    
    body = resp.json()
    token = body.get("accessToken")
    assert token, f"No accessToken in response: {body}"
    
    return token


@pytest.fixture
async def member_token(client: AsyncClient):
    """
    Create a non-admin user and return a JWT access token.
    The first user (admin) must be created first; the second user requires admin auth.
    """
    # Create admin user (first user, no auth required)
    await client.post("/api/v1/users", json={"name": "admin", "pin": "0000"})

    # Login as admin to obtain token for creating the second user
    resp = await client.post("/api/v1/auth/login", json={"name": "admin", "pin": "0000"})
    assert resp.status_code == 200, f"Admin login failed: {resp.text}"
    admin_tok = resp.json()["accessToken"]

    # Create non-admin member using admin auth
    resp = await client.post(
        "/api/v1/users",
        json={"name": "alice", "pin": "1111"},
        headers={"Authorization": f"Bearer {admin_tok}"},
    )
    assert resp.status_code in (200, 201), f"Member creation failed: {resp.text}"

    resp = await client.post("/api/v1/auth/login", json={"name": "alice", "pin": "1111"})
    assert resp.status_code == 200, f"Member login failed: {resp.text}"
    token = resp.json().get("accessToken")
    assert token
    return token


@pytest.fixture
async def authenticated_client(client: AsyncClient, admin_token: str):
    """
    Return a client with Authorization header pre-set with admin token.
    Use this in tests that need authentication.
    """
    client.headers.update({"Authorization": f"Bearer {admin_token}"})
    return client

@pytest.fixture(autouse=True)
def _reset_device_locks():
    """
    Clear the in-process storage device locks between tests.

    They are module-level state held for the lifetime of a background job. In production the
    job's `finally` releases them; under pytest the task is often never scheduled because the
    event loop closes first, so a claim leaks into the next test and the disk looks
    permanently busy. That produced a test which passed alone and failed in the suite.
    """
    from app import device_locks

    device_locks._reset_for_tests()
    yield
    device_locks._reset_for_tests()

@pytest.fixture(autouse=True)
def _assume_capable_host(monkeypatch):
    """
    Present the test suite with a fully-capable Linux board.

    Every test written before capability guards existed implicitly assumed one — they run on a
    developer Mac and expect nmcli-backed routes to work. That assumption was invisible; now
    it is declared here rather than each test discovering a 501.

    Tests that care about refusal (test_capability_guards.py) override this by setting
    AHC_PLATFORM and the probes themselves, which wins because they run after this fixture.
    """
    from app import platform_profile

    monkeypatch.setenv("AHC_PLATFORM", "linux_sbc")
    monkeypatch.setattr(platform_profile.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(platform_profile, "_has_wifi_radio", lambda: True)
    monkeypatch.setattr(platform_profile, "_has_bluetooth_controller", lambda: True)
    platform_profile._reset_for_tests()
    yield
    platform_profile._reset_for_tests()

