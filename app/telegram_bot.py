"""
Telegram bot — public API shim.

All code lives in ``app.telegram`` sub-package.  This module re-exports every
name so that existing imports (``import app.telegram_bot as tb``) and test
monkey-patches (``patch("app.telegram_bot._is_allowed", ...)``) keep working.
"""

from app.telegram.bot_core import (  # noqa: F401 — re-export
    _tb, logger,
    PendingUpload, DuplicateFileError, InsufficientDiskSpaceError,
    _POLL_TIMEOUT_SECONDS, _STOP_TIMEOUT_SECONDS,
    _UPLOAD_PROGRESS_INTERVAL_SECONDS,
    _PENDING_MAX_ENTRIES, _PENDING_TTL_SECONDS,
    _RATE_LIMIT_WINDOW, _RATE_LIMIT_MAX, _TRASH_WARNING_BYTES,
    _last_results, _pending_uploads, _pending_duplicates, _chat_timestamps,
    _cleanup_pending_uploads, _is_rate_limited,
    _is_allowed, _check_allowed_and_rate,
    _get_linked_ids, _add_linked_id,
    _get_pending_approvals, _add_pending_approval, _remove_pending_approval,
    _get_admin_chat_ids, _is_admin_chat,
    _set_chat_folder_owner, _get_chat_folder_owner,
    _resolve_personal_owner, _member_exists,
    _sanitize_filename, _human_size,
    _is_too_large_telegram_file_error, _is_timeout_error,
    _format_elapsed, _format_avg_speed, _storage_bar,
    _file_type_emoji,
    _safe_edit_text, _upload_progress_heartbeat, _download_to_path,
    _make_destination_keyboard,
    _compute_sha256, _record_recent_file,
    _store_private_or_shared_file, _store_entertainment_file,
    _trash_warning_loop,
)

from app.telegram.auth_handlers import (  # noqa: F401
    _handle_start, _handle_auth, _handle_unlink,
    _handle_approval_callback, _handle_approve_command, _handle_deny_command,
)

from app.telegram.search_handlers import (  # noqa: F401
    _handle_help, _handle_list, _handle_message, _send_file,
    _handle_status, _handle_cancel, _handle_whoami, _handle_storage_cmd,
    _handle_duplicates, _handle_scan,
    _handle_dupskip_callback,
    _handle_dupexact_callback, _handle_dupexactdel_callback,
    _handle_dupauto_callback, _handle_dups_summary_callback, _handle_dupscan_callback,
    _handle_dupsim_callback, _handle_dupsimboth_callback, _handle_dupsimdel_callback,
    _handle_dupsimkeepboth_callback,
    _handle_mount,
    _handle_search_page_callback,
)

from app.telegram.upload_handlers import (  # noqa: F401
    _handle_media_message, _handle_destination_callback, _process_upload_choice,
    _handle_keep, _handle_skip, _handle_recent,
    _handle_delete_recent_callback, _handle_empty_trash_callback,
)

import asyncio
from contextlib import suppress

from app.config import settings
from app.telegram.bot_core import (
    _POLL_TIMEOUT_SECONDS, _STOP_TIMEOUT_SECONDS,
)

# Lifecycle state — lives HERE so ``tb._application = X`` in tests works.
_application = None
_trash_warning_task = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start_bot() -> None:
    """Initialise and start the Telegram bot.  No-op if token is not configured."""
    global _application

    if not settings.telegram_bot_token:
        logger.info("Telegram bot token not set — bot disabled")
        return

    try:
        from telegram.ext import (
            ApplicationBuilder, CommandHandler, MessageHandler,
            CallbackQueryHandler, filters,
        )
    except ImportError:
        logger.warning("python-telegram-bot not installed — Telegram bot disabled")
        return

    try:
        builder = (
            ApplicationBuilder()
            .token(settings.telegram_bot_token)
            .connect_timeout(settings.telegram_download_timeout)
            .read_timeout(settings.telegram_download_timeout)
            .write_timeout(settings.telegram_download_timeout)
            .pool_timeout(settings.telegram_download_timeout)
        )

        # Use local Bot API server if configured — removes 20MB file limit
        if settings.telegram_local_api_enabled and settings.telegram_local_api_url:
            local_url = settings.telegram_local_api_url.rstrip("/")
            builder = (
                builder
                .base_url(f"{local_url}/bot")
                .base_file_url(f"{local_url}/file/bot")
                .local_mode(True)
            )
            logger.info("Telegram bot using local API at %s (2GB file limit)",
                        settings.telegram_local_api_url)
        else:
            logger.info("Telegram bot using cloud API (20MB file limit)")

        _application = builder.build()

        _application.add_handler(CommandHandler("start",   _handle_start))
        _application.add_handler(CommandHandler("auth",    _handle_auth))
        _application.add_handler(CommandHandler("help",    _handle_help))
        _application.add_handler(CommandHandler("list",    _handle_list))
        _application.add_handler(CommandHandler("status",  _handle_status))
        _application.add_handler(CommandHandler("cancel",  _handle_cancel))
        _application.add_handler(CommandHandler("whoami",  _handle_whoami))
        _application.add_handler(CommandHandler("unlink",  _handle_unlink))
        _application.add_handler(CommandHandler("recent",  _handle_recent))
        _application.add_handler(CommandHandler("storage",    _handle_storage_cmd))
        _application.add_handler(CommandHandler("mount",      _handle_mount))
        _application.add_handler(CommandHandler("duplicates", _handle_duplicates))
        _application.add_handler(CommandHandler("scan",       _handle_scan))
        _application.add_handler(CommandHandler("keep",       _handle_keep))
        _application.add_handler(CommandHandler("skip",    _handle_skip))
        _application.add_handler(CommandHandler("approve", _handle_approve_command))
        _application.add_handler(CommandHandler("deny",    _handle_deny_command))
        _application.add_handler(
            MessageHandler(
                filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE,
                _handle_media_message,
            )
        )
        _application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message)
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_destination_callback, pattern=r"^dest:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_approval_callback, pattern=r"^approval:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_delete_recent_callback, pattern=r"^delrecent:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_empty_trash_callback, pattern=r"^trash:empty$")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_dupskip_callback, pattern=r"^dupskip:")
        )
        # New duplicate UI callbacks
        _application.add_handler(
            CallbackQueryHandler(_handle_dups_summary_callback, pattern=r"^dups:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_dupscan_callback, pattern=r"^dupscan:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_dupexact_callback, pattern=r"^dupexact:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_dupexactdel_callback, pattern=r"^dupexactdel:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_dupauto_callback, pattern=r"^dupauto:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_dupsim_callback, pattern=r"^dupsim:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_dupsimboth_callback, pattern=r"^dupsimboth:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_dupsimdel_callback, pattern=r"^dupsimdel:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_dupsimkeepboth_callback, pattern=r"^dupsimkeepboth:")
        )
        _application.add_handler(
            CallbackQueryHandler(_handle_search_page_callback, pattern=r"^searchpage:")
        )

        await asyncio.wait_for(_application.initialize(), timeout=15)
        await _application.start()
        await _application.updater.start_polling(
            drop_pending_updates=True,
            poll_interval=0.0,
            timeout=_POLL_TIMEOUT_SECONDS,
            read_timeout=settings.telegram_download_timeout,
            write_timeout=settings.telegram_download_timeout,
            connect_timeout=settings.telegram_download_timeout,
            pool_timeout=settings.telegram_download_timeout,
        )

        logger.info("Telegram bot started polling")

        # Start weekly trash-warning scheduler
        global _trash_warning_task
        _trash_warning_task = asyncio.create_task(
            _trash_warning_loop(), name="trash_warning_loop"
        )
    except Exception as exc:
        logger.error("Telegram bot failed to start: %s", exc)
        _application = None






async def stop_bot() -> None:
    """Gracefully shut down the Telegram bot."""
    global _application, _trash_warning_task

    # Cancel the trash warning scheduler first
    if _trash_warning_task and not _trash_warning_task.done():
        _trash_warning_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await _trash_warning_task
    _trash_warning_task = None

    if _application is None:
        return

    async def _await_with_timeout(coro, label: str) -> None:
        try:
            await asyncio.wait_for(coro, timeout=_STOP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Telegram bot %s timed out after %ss", label, _STOP_TIMEOUT_SECONDS)

    try:
        if getattr(_application, "updater", None) is not None:
            await _await_with_timeout(_application.updater.stop(), "updater.stop")
        await _await_with_timeout(_application.stop(), "application.stop")
        await _await_with_timeout(_application.shutdown(), "application.shutdown")
        _last_results.clear()
        _pending_uploads.clear()
        logger.info("Telegram bot stopped")
    except Exception as exc:
        logger.warning("Telegram bot stop error: %s", exc)
    finally:
        _application = None

