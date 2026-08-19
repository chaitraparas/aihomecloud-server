"""
Tests for logging_config.py, jobs_routes.py, and main.py middleware/health.
"""

import logging
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.logging_config import (
    configure_logging,
    set_request_id,
    reset_request_id,
    _RequestIdFilter,
    _request_id_ctx,
)


# — logging_config ————————————————————————————————————————————

class TestRequestIdContextVar:
    def test_default_value(self):
        assert _request_id_ctx.get() == "-"

    def test_set_and_reset(self):
        token = set_request_id("abc-123")
        assert _request_id_ctx.get() == "abc-123"
        reset_request_id(token)
        assert _request_id_ctx.get() == "-"


class TestRequestIdFilter:
    def test_adds_request_id(self):
        f = _RequestIdFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        token = set_request_id("test-id")
        try:
            result = f.filter(record)
            assert result is True
            assert record.request_id == "test-id"
        finally:
            reset_request_id(token)


class TestConfigureLogging:
    def test_sets_up_handler(self):
        configure_logging("DEBUG")
        root = logging.getLogger()
        assert len(root.handlers) >= 1
        assert root.level == logging.DEBUG

    def test_info_level(self):
        configure_logging("INFO")
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_invalid_level_defaults_to_info(self):
        configure_logging("INVALID")
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_none_defaults_to_info(self):
        configure_logging(None)
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_httpx_capped_below_root_even_at_debug(self):
        # httpx logs "HTTP Request: POST http://.../bot<TOKEN>/getUpdates ..." at INFO -- the
        # bot token sits directly in that URL. Even a DEBUG root level (which an admin might
        # reasonably set for troubleshooting) must not drop the token back into the journal.
        configure_logging("DEBUG")
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING


# — jobs_routes ———————————————————————————————————————————————

class TestJobsRoute:
    @pytest.mark.asyncio
    async def test_get_job_not_found(self, client):
        resp = await client.post("/api/v1/users", json={"name": "admin", "pin": "0000"})
        resp = await client.post("/api/v1/auth/login", json={"name": "admin", "pin": "0000"})
        token = resp.json()["accessToken"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.get("/api/v1/jobs/nonexistent-id", headers=headers)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_job_found(self, client):
        resp = await client.post("/api/v1/users", json={"name": "admin", "pin": "0000"})
        resp = await client.post("/api/v1/auth/login", json={"name": "admin", "pin": "0000"})
        token = resp.json()["accessToken"]
        headers = {"Authorization": f"Bearer {token}"}

        # Decode the token to get the actual user_id (sub claim)
        import jwt as jwt_mod
        from app.config import settings
        payload = jwt_mod.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        uid = payload["sub"]

        from app.job_store import create_job, update_job, JobStatus
        job = create_job(user_id=uid)
        update_job(job.id, status=JobStatus.completed, result={"ok": True})

        resp = await client.get(f"/api/v1/jobs/{job.id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["result"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_get_job_wrong_user(self, client):
        # First user becomes admin automatically
        await client.post("/api/v1/users", json={"name": "admin1", "pin": "0000"})
        admin_resp = await client.post("/api/v1/auth/login", json={"name": "admin1", "pin": "0000"})
        admin_headers = {"Authorization": f"Bearer {admin_resp.json()['accessToken']}"}

        # Create a non-admin second user (requires admin auth)
        await client.post("/api/v1/users", json={"name": "user2", "pin": "2222"}, headers=admin_headers)
        resp = await client.post("/api/v1/auth/login", json={"name": "user2", "pin": "2222"})
        token = resp.json()["accessToken"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.job_store import create_job
        job = create_job(user_id="other-user-id")

        resp = await client.get(f"/api/v1/jobs/{job.id}", headers=headers)
        assert resp.status_code == 404


# — main.py health + middleware ———————————————————————————————

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_endpoint(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestSecurityHeaders:
    @pytest.mark.asyncio
    async def test_nosniff_header(self, client):
        resp = await client.get("/api/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    @pytest.mark.asyncio
    async def test_frame_deny(self, client):
        resp = await client.get("/api/health")
        assert resp.headers.get("X-Frame-Options") == "DENY"

    @pytest.mark.asyncio
    async def test_xss_header(self, client):
        resp = await client.get("/api/health")
        assert resp.headers.get("X-XSS-Protection") == "0"

    @pytest.mark.asyncio
    async def test_auth_cache_control(self, client):
        resp = await client.post("/api/v1/auth/login", json={"name": "x", "pin": "0"})
        assert resp.headers.get("Cache-Control") == "no-store"


class TestRequestIdMiddleware:
    @pytest.mark.asyncio
    async def test_request_completes_with_id(self, client):
        # Just verify a normal request goes through the middleware stack
        resp = await client.get("/api/health")
        assert resp.status_code == 200


class TestBackwardCompatApiRedirect:
    """The /api/{path} -> /api/v1/{path} shim for pre-versioning clients."""

    @pytest.mark.asyncio
    async def test_unversioned_path_redirects_to_v1(self, client):
        resp = await client.get("/api/system/info", follow_redirects=False)
        assert resp.status_code == 308
        assert resp.headers["location"] == "/api/v1/system/info"

    @pytest.mark.asyncio
    async def test_already_versioned_but_genuinely_missing_path_404s_not_loops(self, client):
        # Regression pin: found live 2026-07-15 -- a request under /api/v1/... whose route
        # genuinely doesn't exist on this deployment (e.g. an endpoint from a newer app version
        # than the backend) fell through to this same catch-all, which re-prepended "v1" on every
        # redirect with no way to converge, looping until the client's own redirect-limit gave up.
        resp = await client.get("/api/v1/this-route-does-not-exist", follow_redirects=False)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_double_versioned_path_404s_not_loops(self, client):
        resp = await client.get("/api/v1/v1/system/info", follow_redirects=False)
        assert resp.status_code == 404
