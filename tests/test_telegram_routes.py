"""
Tests for telegram_routes.py — config, unlink, pending endpoints.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


class TestTelegramConfigHelpers:
    def test_mask_token_long(self):
        from app.routes.telegram_routes import _mask_token
        token = "1234567890:ABCDEFghijklmnop"
        masked = _mask_token(token)
        assert masked.startswith("1234567890")
        assert masked.endswith("lmnop")
        assert len(masked) < len(token)

    def test_mask_token_short(self):
        from app.routes.telegram_routes import _mask_token
        token = "short"
        masked = _mask_token(token)
        assert len(masked) < len(token) or len(token) <= 15
        assert masked.startswith("sho")
        assert masked.endswith("ort")

    def test_bot_is_running_false(self):
        from app.routes.telegram_routes import _bot_is_running
        with patch("app.routes.telegram_routes._bot_is_running") as mock:
            mock.return_value = False
            assert mock() is False


class TestGetTelegramConfig:
    @pytest.mark.asyncio
    async def test_get_config(self, authenticated_client):
        with patch("app.routes.telegram_routes._store") as mock_store, \
             patch("app.routes.telegram_routes._bot_is_running", return_value=False):
            mock_store.get_value = AsyncMock(side_effect=[
                {"bot_token": "1234567890:SECRET", "local_api_enabled": False, "api_id": 0},
                [],
            ])
            resp = await authenticated_client.get("/api/v1/telegram/config")
            assert resp.status_code == 200
            data = resp.json()
            assert data["configured"] is True
            assert data["token_preview"]  # non-empty
            assert data["bot_running"] is False

    @pytest.mark.asyncio
    async def test_get_config_empty(self, authenticated_client):
        with patch("app.routes.telegram_routes._store") as mock_store, \
             patch("app.routes.telegram_routes._bot_is_running", return_value=False), \
             patch("app.routes.telegram_routes.settings") as mock_settings:
            mock_settings.telegram_bot_token = ""
            mock_settings.telegram_local_api_enabled = False
            mock_settings.telegram_api_id = 0
            mock_store.get_value = AsyncMock(side_effect=[{}, []])
            resp = await authenticated_client.get("/api/v1/telegram/config")
            assert resp.status_code == 200
            data = resp.json()
            assert data["configured"] is False


class TestSaveTelegramConfig:
    @pytest.mark.asyncio
    async def test_save_config_with_token(self, authenticated_client):
        with patch("app.routes.telegram_routes._store") as mock_store, \
             patch("app.routes.telegram_routes.settings") as mock_settings:
            mock_store.get_value = AsyncMock(return_value={})
            mock_store.set_value = AsyncMock()
            mock_settings.telegram_bot_token = ""

            # Mock the import of start_bot/stop_bot
            with patch.dict("sys.modules", {"app.telegram_bot": MagicMock(
                stop_bot=AsyncMock(),
                start_bot=AsyncMock(),
            )}):
                resp = await authenticated_client.post(
                    "/api/v1/telegram/config",
                    json={"bot_token": "NEW_TOKEN_123", "api_id": 0, "api_hash": "", "local_api_enabled": False}
                )
                assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_save_config_empty_token_with_saved(self, authenticated_client):
        with patch("app.routes.telegram_routes._store") as mock_store, \
             patch("app.routes.telegram_routes.settings") as mock_settings:
            mock_store.get_value = AsyncMock(return_value={"bot_token": "SAVED_TOKEN"})
            mock_store.set_value = AsyncMock()
            mock_settings.telegram_bot_token = ""
            # Provide empty token — should use saved
            with patch.dict("sys.modules", {"app.telegram_bot": MagicMock(
                stop_bot=AsyncMock(),
                start_bot=AsyncMock(),
            )}):
                resp = await authenticated_client.post(
                    "/api/v1/telegram/config",
                    json={"bot_token": "", "api_id": 0, "api_hash": "", "local_api_enabled": False}
                )
                assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_save_config_no_token_returns_422(self, authenticated_client):
        with patch("app.routes.telegram_routes._store") as mock_store, \
             patch("app.routes.telegram_routes.settings") as mock_settings:
            mock_store.get_value = AsyncMock(return_value={})
            mock_settings.telegram_bot_token = ""
            resp = await authenticated_client.post(
                "/api/v1/telegram/config",
                json={"bot_token": "", "api_id": 0, "api_hash": "", "local_api_enabled": False}
            )
            assert resp.status_code == 422


class TestGetLinkedAccounts:
    @pytest.mark.asyncio
    async def test_get_linked_with_owners(self, authenticated_client):
        with patch("app.routes.telegram_routes._store") as mock_store:
            mock_store.get_value = AsyncMock(side_effect=[
                [12345, 67890],
                {"12345": "paras", "67890": "chai"},
            ])
            resp = await authenticated_client.get("/api/v1/telegram/linked")
            assert resp.status_code == 200
            data = resp.json()
            assert data == [
                {"chat_id": 12345, "owner": "paras"},
                {"chat_id": 67890, "owner": "chai"},
            ]

    @pytest.mark.asyncio
    async def test_get_linked_missing_owner_defaults_unknown(self, authenticated_client):
        with patch("app.routes.telegram_routes._store") as mock_store:
            mock_store.get_value = AsyncMock(side_effect=[[12345], {}])
            resp = await authenticated_client.get("/api/v1/telegram/linked")
            assert resp.status_code == 200
            assert resp.json() == [{"chat_id": 12345, "owner": "unknown"}]

    @pytest.mark.asyncio
    async def test_get_linked_empty(self, authenticated_client):
        with patch("app.routes.telegram_routes._store") as mock_store:
            mock_store.get_value = AsyncMock(side_effect=[[], {}])
            resp = await authenticated_client.get("/api/v1/telegram/linked")
            assert resp.status_code == 200
            assert resp.json() == []


class TestUnlinkAccount:
    @pytest.mark.asyncio
    async def test_unlink(self, authenticated_client):
        async def _get_value(key, default=None):
            if key == "telegram_linked_ids":
                return [12345, 67890]
            if key == "telegram_chat_folder_owners":
                return {"12345": "alice", "67890": "bob"}
            return default

        state = {
            "telegram_linked_ids": [12345, 67890],
            "telegram_chat_folder_owners": {"12345": "alice", "67890": "bob"},
        }

        async def _atomic_update(key, fn, default=None):
            state[key] = fn(state.get(key, default))
            return state[key]

        with patch("app.routes.telegram_routes._store") as mock_store:
            mock_store.get_value = AsyncMock(side_effect=_get_value)
            # Revocation goes through atomic_update now: a plain read-modify-write racing another
            # writer would write back a list still containing the chat, so the unlink would report
            # success and do nothing. Asserting on the resulting STATE, not on the call.
            mock_store.atomic_update = _atomic_update
            resp = await authenticated_client.delete("/api/v1/telegram/linked/12345")
            assert resp.status_code == 204

            assert 12345 not in state["telegram_linked_ids"]
            assert 67890 in state["telegram_linked_ids"], "unrelated chat was revoked too"
            # Regression pin: previously only telegram_linked_ids was cleared, leaving
            # telegram_chat_folder_owners with a stale entry for the unlinked chat forever.
            assert "12345" not in state["telegram_chat_folder_owners"]
            assert state["telegram_chat_folder_owners"]["67890"] == "bob"

    @pytest.mark.asyncio
    async def test_unlink_tolerates_missing_folder_owner_entry(self, authenticated_client):
        """Unlinking a chat with no folder-owner mapping at all (e.g. never approved
        past the pending stage) must not raise — same tolerance as auth_handlers'
        equivalent bot-command path."""
        async def _get_value(key, default=None):
            if key == "telegram_linked_ids":
                return [12345]
            if key == "telegram_chat_folder_owners":
                return {}
            return default

        state = {"telegram_linked_ids": [12345], "telegram_chat_folder_owners": {}}

        async def _atomic_update(key, fn, default=None):
            state[key] = fn(state.get(key, default))
            return state[key]

        with patch("app.routes.telegram_routes._store") as mock_store:
            mock_store.get_value = AsyncMock(side_effect=_get_value)
            mock_store.atomic_update = _atomic_update
            resp = await authenticated_client.delete("/api/v1/telegram/linked/12345")
            assert resp.status_code == 204
            # The chat is gone from both keys and nothing raised. The previous version of this
            # assertion pinned an optimisation — "don't write folder_owners when there is nothing
            # to remove" — which the move to atomic_update deliberately drops: skipping the write
            # requires reading first and deciding outside the lock, which is the read-modify-write
            # race being removed. An unconditional idempotent update is the correct trade.
            assert state["telegram_linked_ids"] == []
            assert state["telegram_chat_folder_owners"] == {}


class TestCancelSetupLocalApi:
    @pytest.mark.asyncio
    async def test_cancel_marks_job_id(self, authenticated_client):
        from app.routes.telegram_routes import _setup_cancels
        _setup_cancels.clear()
        resp = await authenticated_client.post("/api/v1/telegram/setup-local-api/cancel?job_id=abc123")
        assert resp.status_code == 200
        assert resp.json() == {"cancelled": "abc123"}
        assert "abc123" in _setup_cancels
        _setup_cancels.clear()


class TestCheckCancelled:
    def test_not_cancelled_returns_false(self):
        from app.routes.telegram_routes import _check_cancelled, _setup_cancels
        _setup_cancels.clear()
        assert _check_cancelled("nope") is False

    def test_cancelled_marks_job_failed_and_clears_flag(self):
        from app.routes.telegram_routes import _check_cancelled, _setup_cancels
        with patch("app.routes.telegram_routes.update_job") as mock_update, \
             patch("app.routes.telegram_routes._cleanup_build") as mock_cleanup:
            _setup_cancels.clear()
            _setup_cancels.add("job1")
            assert _check_cancelled("job1") is True
            assert "job1" not in _setup_cancels
            mock_update.assert_called_once()
            mock_cleanup.assert_called_once()


class TestDisableLocalApi:
    @pytest.mark.asyncio
    async def test_disable_updates_config_and_stops_service(self, authenticated_client):
        with patch("app.routes.telegram_routes._store") as mock_store, \
             patch("app.routes.telegram_routes.settings") as mock_settings, \
             patch("app.routes.telegram_routes.run_command", new_callable=AsyncMock) as mock_run:
            mock_store.get_value = AsyncMock(return_value={"bot_token": "X", "local_api_enabled": True})
            mock_store.set_value = AsyncMock()
            mock_run.return_value = (0, "", "")
            with patch.dict("sys.modules", {"app.telegram_bot": MagicMock(
                stop_bot=AsyncMock(),
                start_bot=AsyncMock(),
            )}):
                resp = await authenticated_client.post("/api/v1/telegram/local-api/disable")
                assert resp.status_code == 204
                saved_call = mock_store.set_value.call_args[0][1]
                assert saved_call["local_api_enabled"] is False
                assert mock_settings.telegram_local_api_enabled is False
                stop_calls = [c for c in mock_run.call_args_list if "stop" in c[0][0]]
                assert len(stop_calls) == 1


class TestPendingApprovals:
    @pytest.mark.asyncio
    async def test_get_pending(self, authenticated_client):
        with patch("app.routes.telegram_routes._store") as mock_store:
            mock_store.get_value = AsyncMock(return_value=[
                {"chat_id": 111, "first_name": "Alice"},
            ])
            resp = await authenticated_client.get("/api/v1/telegram/pending")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["chat_id"] == 111
