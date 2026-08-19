"""
Tests for telegram/bot_core.py — pure helper functions and utilities.
Covers: _sanitize_filename, _human_size, _format_elapsed, _format_avg_speed,
        _is_rate_limited, _cleanup_pending_uploads, _is_too_large_telegram_file_error,
        _is_timeout_error, _compute_sha256, _file_type_emoji, _storage_bar,
        _check_duplicate, _record_file_hash, _record_recent_file,
        _get_linked_ids, _add_linked_id, _get_pending_approvals, _add_pending_approval,
        _remove_pending_approval, _resolve_personal_owner, _is_admin_chat,
        _get_chat_folder_owner, _set_chat_folder_owner, PendingUpload, DuplicateFileError
"""

import pytest
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock


def _fake_atomic_store(initial: dict):
    """
    Stand-in for store.atomic_update that actually applies the callback.

    Asserting "set_value was called" would pass even if the callback were wrong; these updates are
    revocation, so the test has to check what the new value ACTUALLY becomes.
    """
    state = dict(initial)

    async def atomic_update(key, fn, default=None):
        state[key] = fn(state.get(key, default))
        return state[key]

    async def get_value(key, default=None):
        return state.get(key, default)

    return state, atomic_update, get_value



# ── Pure helpers (sync) ─────────────────────────────────────────────────────

class TestSanitizeFilename:
    def test_normal_filename(self):
        from app.telegram.bot_core import _sanitize_filename
        assert _sanitize_filename("report.pdf") == "report.pdf"

    def test_empty_string(self):
        from app.telegram.bot_core import _sanitize_filename
        assert _sanitize_filename("") == "telegram_file"

    def test_dot_only(self):
        from app.telegram.bot_core import _sanitize_filename
        assert _sanitize_filename(".") == "telegram_file"

    def test_dotdot(self):
        from app.telegram.bot_core import _sanitize_filename
        assert _sanitize_filename("..") == "telegram_file"

    def test_custom_default(self):
        from app.telegram.bot_core import _sanitize_filename
        assert _sanitize_filename("", default_stem="photo") == "photo"

    def test_path_traversal_stripped(self):
        from app.telegram.bot_core import _sanitize_filename
        result = _sanitize_filename("/etc/passwd")
        assert "/" not in result
        assert result == "passwd"


class TestHumanSize:
    def test_zero(self):
        from app.telegram.bot_core import _human_size
        assert _human_size(0) == "unknown size"

    def test_negative(self):
        from app.telegram.bot_core import _human_size
        assert _human_size(-1) == "unknown size"

    def test_megabytes(self):
        from app.telegram.bot_core import _human_size
        assert "MB" in _human_size(5 * 1024 * 1024)

    def test_gigabytes(self):
        from app.telegram.bot_core import _human_size
        assert "GB" in _human_size(2 * 1024 * 1024 * 1024)


class TestFormatElapsed:
    def test_seconds_only(self):
        from app.telegram.bot_core import _format_elapsed
        assert _format_elapsed(45) == "45s"

    def test_minutes_and_seconds(self):
        from app.telegram.bot_core import _format_elapsed
        assert _format_elapsed(125) == "2m 5s"

    def test_hours(self):
        from app.telegram.bot_core import _format_elapsed
        assert _format_elapsed(3661) == "1h 1m 1s"

    def test_zero(self):
        from app.telegram.bot_core import _format_elapsed
        assert _format_elapsed(0) == "0s"

    def test_negative(self):
        from app.telegram.bot_core import _format_elapsed
        assert _format_elapsed(-5) == "0s"


class TestFormatAvgSpeed:
    def test_normal(self):
        from app.telegram.bot_core import _format_avg_speed
        result = _format_avg_speed(10 * 1024 * 1024, 2.0)
        assert "MB/s" in result

    def test_zero_bytes(self):
        from app.telegram.bot_core import _format_avg_speed
        assert _format_avg_speed(0, 1.0) == "n/a"

    def test_zero_elapsed(self):
        from app.telegram.bot_core import _format_avg_speed
        assert _format_avg_speed(1024, 0) == "n/a"


class TestErrorDetection:
    def test_too_large_true(self):
        from app.telegram.bot_core import _is_too_large_telegram_file_error
        assert _is_too_large_telegram_file_error(Exception("File is too big")) is True

    def test_too_large_false(self):
        from app.telegram.bot_core import _is_too_large_telegram_file_error
        assert _is_too_large_telegram_file_error(Exception("network error")) is False

    def test_timeout_true(self):
        from app.telegram.bot_core import _is_timeout_error
        assert _is_timeout_error(Exception("Connection timed out")) is True

    def test_timeout_keyword(self):
        from app.telegram.bot_core import _is_timeout_error
        assert _is_timeout_error(Exception("Read timeout")) is True

    def test_timeout_false(self):
        from app.telegram.bot_core import _is_timeout_error
        assert _is_timeout_error(Exception("server error")) is False


class TestFileTypeEmoji:
    def test_known_types(self):
        from app.telegram.bot_core import _file_type_emoji
        assert _file_type_emoji("document")  # non-empty
        assert _file_type_emoji("video")
        assert _file_type_emoji("audio")
        assert _file_type_emoji("photo")
        assert _file_type_emoji("voice")

    def test_unknown_type(self):
        from app.telegram.bot_core import _file_type_emoji
        result = _file_type_emoji("unknown_type")
        assert result  # has a fallback emoji


class TestStorageBar:
    def test_zero_percent(self):
        from app.telegram.bot_core import _storage_bar
        bar = _storage_bar(0)
        assert "\u2591" in bar  # light shade
        assert len(bar) == 10

    def test_hundred_percent(self):
        from app.telegram.bot_core import _storage_bar
        bar = _storage_bar(100)
        assert "\u2593" in bar  # dark shade

    def test_fifty_percent(self):
        from app.telegram.bot_core import _storage_bar
        bar = _storage_bar(50)
        assert len(bar) == 10


class TestRateLimiting:
    def setup_method(self):
        from app.telegram.bot_core import _chat_timestamps
        _chat_timestamps.clear()

    def test_not_limited(self):
        from app.telegram.bot_core import _is_rate_limited
        assert _is_rate_limited(123) is False

    def test_limited_after_max(self):
        from app.telegram.bot_core import _is_rate_limited, _chat_timestamps
        # Fill up with 30 timestamps (at the limit)
        _chat_timestamps[999] = [time.monotonic()] * 30
        assert _is_rate_limited(999) is True

    def test_old_entries_pruned(self):
        from app.telegram.bot_core import _is_rate_limited, _chat_timestamps
        # Old entries beyond 60s window should be pruned
        _chat_timestamps[888] = [time.monotonic() - 120] * 30
        assert _is_rate_limited(888) is False


class TestCleanupPendingUploads:
    def setup_method(self):
        from app.telegram.bot_core import _pending_uploads
        _pending_uploads.clear()

    def test_remove_expired(self):
        from app.telegram.bot_core import (
            _cleanup_pending_uploads, _pending_uploads, PendingUpload,
        )
        # Create an expired entry (created_at far in the past)
        _pending_uploads[1] = PendingUpload(
            file_id="f1", filename="a.pdf", kind="document",
            created_at=time.monotonic() - 600,
        )
        _cleanup_pending_uploads()
        assert 1 not in _pending_uploads

    def test_keep_fresh(self):
        from app.telegram.bot_core import (
            _cleanup_pending_uploads, _pending_uploads, PendingUpload,
        )
        _pending_uploads[2] = PendingUpload(
            file_id="f2", filename="b.pdf", kind="document",
            created_at=time.monotonic(),
        )
        _cleanup_pending_uploads()
        assert 2 in _pending_uploads

    def test_enforce_max_size(self):
        from app.telegram.bot_core import (
            _cleanup_pending_uploads, _pending_uploads, PendingUpload,
        )
        now = time.monotonic()
        for i in range(120):
            _pending_uploads[i] = PendingUpload(
                file_id=f"f{i}", filename=f"{i}.pdf", kind="document",
                created_at=now + i * 0.001,
            )
        _cleanup_pending_uploads()
        assert len(_pending_uploads) <= 100


class TestPendingUploadDataclass:
    def test_creation(self):
        from app.telegram.bot_core import PendingUpload
        p = PendingUpload(file_id="abc", filename="test.pdf", kind="document", file_size=1024)
        assert p.file_id == "abc"
        assert p.filename == "test.pdf"
        assert p.created_at > 0

    def test_default_timestamp(self):
        from app.telegram.bot_core import PendingUpload
        p = PendingUpload(file_id="x", filename="y", kind="photo")
        assert p.created_at > 0


class TestDuplicateFileError:
    def test_creation(self, tmp_path):
        from app.telegram.bot_core import DuplicateFileError
        p = tmp_path / "test.txt"
        p.write_text("hello")
        err = DuplicateFileError(sha256="abc", existing={"path": "/a"}, temp_path=p)
        assert err.sha256 == "abc"
        assert err.temp_path == p


class TestComputeSha256:
    def test_known_hash(self, tmp_path):
        from app.telegram.bot_core import _compute_sha256
        f = tmp_path / "test.bin"
        f.write_bytes(b"hello world")
        h = _compute_sha256(f)
        assert len(h) == 64
        assert isinstance(h, str)


# ── Async helpers ────────────────────────────────────────────────────────────
#
# TestCheckDuplicate / TestRecordFileHash formerly lived here, covering
# Telegram's own separate SHA-256 dedup index (_check_duplicate/_record_file_hash,
# backed by a "telegram_file_hashes" store). Both functions were removed when
# Telegram was unified onto the shared, scope-keyed dedup path
# (ingest._dedup_lookup + media_index.record_entry — see bot_core.py's module
# note above _record_recent_file) — a file sent via Telegram and the same file
# arriving via direct upload or Sync are now recognized as duplicates of each
# other, which a Telegram-only index could never do. The shared path's dedup
# behavior is covered by tests/test_ingest.py's dedup tests.

class TestRecordRecentFile:
    @pytest.mark.asyncio
    async def test_record(self):
        from app.telegram.bot_core import _record_recent_file
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.atomic_update = AsyncMock()
            await _record_recent_file(123, "file.pdf", "/path", 1024)
            mock_store.atomic_update.assert_called_once()


class TestLinkedIds:
    @pytest.mark.asyncio
    async def test_get_empty(self):
        from app.telegram.bot_core import _get_linked_ids
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_value = AsyncMock(return_value=[])
            ids = await _get_linked_ids()
            assert ids == set()

    @pytest.mark.asyncio
    async def test_get_with_ids(self):
        from app.telegram.bot_core import _get_linked_ids
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_value = AsyncMock(return_value=[123, 456])
            ids = await _get_linked_ids()
            assert ids == {123, 456}

    @pytest.mark.asyncio
    async def test_add_linked_id(self):
        from app.telegram.bot_core import _add_linked_id
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.atomic_update = AsyncMock()
            await _add_linked_id(789)
            mock_store.atomic_update.assert_called_once()


class TestPendingApprovals:
    @pytest.mark.asyncio
    async def test_get_empty(self):
        from app.telegram.bot_core import _get_pending_approvals
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_value = AsyncMock(return_value=[])
            result = await _get_pending_approvals()
            assert result == []

    @pytest.mark.asyncio
    async def test_add_pending(self):
        from app.telegram.bot_core import _add_pending_approval
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.atomic_update = AsyncMock()
            await _add_pending_approval(123, "user1", "User One")
            mock_store.atomic_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_pending(self):
        from app.telegram.bot_core import _remove_pending_approval
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.atomic_update = AsyncMock()
            await _remove_pending_approval(123)
            mock_store.atomic_update.assert_called_once()


class TestChatFolderOwner:
    @pytest.mark.asyncio
    async def test_set_owner_uses_an_atomic_update(self):
        """
        Binding must go through atomic_update, and must preserve other chats' bindings.

        This mapping is also what _get_admin_chat_ids reads to decide who is a Telegram admin, so a
        lost read-modify-write here can drop or resurrect an admin binding, not merely a folder
        preference. The old test asserted only that set_value was called at all, which a wrong
        callback would also satisfy. (2026-08-08 audit, adversarial pass.)
        """
        from app.telegram.bot_core import _set_chat_folder_owner
        state, atomic_update, get_value = _fake_atomic_store(
            {"telegram_chat_folder_owners": {"222": "bob"}}
        )
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.atomic_update = atomic_update
            mock_store.get_value = get_value
            await _set_chat_folder_owner(111, "alice")

        assert state["telegram_chat_folder_owners"] == {"222": "bob", "111": "alice"}

    @pytest.mark.asyncio
    async def test_get_owner_exists(self):
        from app.telegram.bot_core import _get_chat_folder_owner
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_value = AsyncMock(return_value={"111": "alice"})
            result = await _get_chat_folder_owner(111)
            assert result == "alice"

    @pytest.mark.asyncio
    async def test_get_owner_missing(self):
        from app.telegram.bot_core import _get_chat_folder_owner
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_value = AsyncMock(return_value={})
            result = await _get_chat_folder_owner(111)
            assert result is None


class TestResolvePersonalOwner:
    @pytest.mark.asyncio
    async def test_explicit_owner(self):
        from app.telegram.bot_core import _resolve_personal_owner
        with patch("app.telegram.bot_core._get_chat_folder_owner", new_callable=AsyncMock, return_value="alice"):
            result = await _resolve_personal_owner(111)
            assert result == "alice"

    @pytest.mark.asyncio
    async def test_preferred_name_match(self):
        from app.telegram.bot_core import _resolve_personal_owner
        with patch("app.telegram.bot_core._get_chat_folder_owner", new_callable=AsyncMock, return_value=None), \
             patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_users = AsyncMock(return_value=[
                {"name": "Bob", "is_admin": False},
                {"name": "Alice", "is_admin": True},
            ])
            result = await _resolve_personal_owner(111, preferred_name="bob")
            assert result == "Bob"

    @pytest.mark.asyncio
    async def test_never_falls_back_to_the_admin_account(self):
        """
        An unresolvable name must yield None, not the admin.

        This test previously asserted the opposite — that resolution falls back to the admin user —
        and that fallback was a privilege escalation on the DEFAULT path. `preferred_name` is the
        requester's own Telegram display name, most real display names match no member, so almost
        every genuine pairing bound the chat to the admin: the admin's personal folder became that
        member's "My Folder", and because _get_admin_chat_ids derives admin-ness from this same
        mapping, the chat also became a Telegram admin able to run /approve. (2026-08-08 audit.)
        """
        from app.telegram.bot_core import _resolve_personal_owner
        with patch("app.telegram.bot_core._get_chat_folder_owner", new_callable=AsyncMock, return_value=None), \
             patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_users = AsyncMock(return_value=[
                {"name": "admin", "is_admin": True},
                {"name": "Bob", "is_admin": False},
            ])
            assert await _resolve_personal_owner(111, preferred_name="nobody") is None
            assert await _resolve_personal_owner(111) is None

    @pytest.mark.asyncio
    async def test_an_admin_name_is_not_claimable_by_typing_it(self):
        """Naming the admin must not bind to the admin — a display name is not proof of identity."""
        from app.telegram.bot_core import _resolve_personal_owner
        with patch("app.telegram.bot_core._get_chat_folder_owner", new_callable=AsyncMock, return_value=None), \
             patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_users = AsyncMock(return_value=[
                {"name": "Paras", "is_admin": True},
                {"name": "Bob", "is_admin": False},
            ])
            assert await _resolve_personal_owner(111, preferred_name="Paras") is None
            assert await _resolve_personal_owner(111, preferred_name="paras") is None
            # a non-admin member still resolves normally
            assert await _resolve_personal_owner(111, preferred_name="bob") == "Bob"

    @pytest.mark.asyncio
    async def test_no_users(self):
        from app.telegram.bot_core import _resolve_personal_owner
        with patch("app.telegram.bot_core._get_chat_folder_owner", new_callable=AsyncMock, return_value=None), \
             patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_users = AsyncMock(return_value=[])
            assert await _resolve_personal_owner(111) is None

    @pytest.mark.asyncio
    async def test_member_exists_gates_admin_names_behind_a_flag(self):
        """
        _member_exists is what validates an EXPLICIT binding, so it must distinguish the two cases:
        the authenticated-admin API path may bind an admin identity, in-Telegram paths may not.
        """
        from app.telegram.bot_core import _member_exists
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_users = AsyncMock(return_value=[
                {"name": "Paras", "is_admin": True},
                {"name": "Bob", "is_admin": False},
            ])
            assert await _member_exists("Bob") is True
            assert await _member_exists("Paras") is False
            assert await _member_exists("Paras", allow_admin=True) is True
            assert await _member_exists("nobody", allow_admin=True) is False
            assert await _member_exists("") is False


class TestAdminChatIds:
    @pytest.mark.asyncio
    async def test_get_admin_chat_ids(self):
        from app.telegram.bot_core import _get_admin_chat_ids
        with patch("app.telegram.bot_core._store") as mock_store:
            mock_store.get_users = AsyncMock(return_value=[
                {"name": "admin", "is_admin": True},
            ])
            mock_store.get_value = AsyncMock(return_value={"111": "admin"})
            result = await _get_admin_chat_ids()
            assert 111 in result

    @pytest.mark.asyncio
    async def test_is_admin_chat_true(self):
        from app.telegram.bot_core import _is_admin_chat
        with patch("app.telegram.bot_core._get_admin_chat_ids", new_callable=AsyncMock, return_value={111}):
            assert await _is_admin_chat(111) is True

    @pytest.mark.asyncio
    async def test_is_admin_chat_false(self):
        from app.telegram.bot_core import _is_admin_chat
        with patch("app.telegram.bot_core._get_admin_chat_ids", new_callable=AsyncMock, return_value={111}):
            assert await _is_admin_chat(222) is False


class TestIsAllowed:
    @pytest.mark.asyncio
    async def test_allowed(self):
        from app.telegram.bot_core import _is_allowed
        with patch("app.telegram.bot_core._get_linked_ids", new_callable=AsyncMock, return_value={42}):
            assert await _is_allowed(42) is True

    @pytest.mark.asyncio
    async def test_not_allowed(self):
        from app.telegram.bot_core import _is_allowed
        with patch("app.telegram.bot_core._get_linked_ids", new_callable=AsyncMock, return_value={42}):
            assert await _is_allowed(99) is False
