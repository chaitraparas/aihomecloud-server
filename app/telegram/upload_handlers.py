"""Telegram bot handlers — upload handlers."""

import asyncio
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Optional

import mimetypes

from .bot_core import (
    _tb, logger,
    _check_allowed_and_rate,
    _pending_uploads, _pending_duplicates,
    _cleanup_pending_uploads,
    PendingUpload, DuplicateFileError, InsufficientDiskSpaceError,
    _sanitize_filename, _human_size, _file_type_emoji,
    _make_destination_keyboard,
    _safe_edit_text, _upload_progress_heartbeat,
    _format_elapsed, _format_avg_speed,
    _is_too_large_telegram_file_error, _is_timeout_error,
    _record_recent_file, _nas_rel_path,
    _place_entertainment_file,
    _esc,
)
from ..file_sorter import _sort_file
from ..config import settings
from ..ingest import Scope, _apply_capture_time, _extract_video_duration
from .. import media_index
from .. import store as _store

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None  # type: ignore[assignment,misc]
    InlineKeyboardMarkup = None  # type: ignore[assignment,misc]

from ..routes.trash_routes import _unlink_trash_item




async def _handle_media_message(update, context) -> None:  # type: ignore[type-arg]
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return

    message = update.message
    pending: Optional[PendingUpload] = None

    if getattr(message, "document", None):
        doc = message.document
        pending = PendingUpload(
            file_id=doc.file_id,
            filename=_sanitize_filename(getattr(doc, "file_name", "telegram_document")),
            kind="document",
            file_size=int(getattr(doc, "file_size", 0) or 0),
            caption=(message.caption or "").strip(),
        )
    elif getattr(message, "video", None):
        video = message.video
        pending = PendingUpload(
            file_id=video.file_id,
            filename=_sanitize_filename(getattr(video, "file_name", "telegram_video.mp4")),
            kind="video",
            file_size=int(getattr(video, "file_size", 0) or 0),
            caption=(message.caption or "").strip(),
        )
    elif getattr(message, "audio", None):
        audio = message.audio
        pending = PendingUpload(
            file_id=audio.file_id,
            filename=_sanitize_filename(getattr(audio, "file_name", "telegram_audio.mp3")),
            kind="audio",
            file_size=int(getattr(audio, "file_size", 0) or 0),
            caption=(message.caption or "").strip(),
        )
    elif getattr(message, "photo", None):
        photo = message.photo[-1]
        pending = PendingUpload(
            file_id=photo.file_id,
            filename="telegram_photo.jpg",
            kind="photo",
            file_size=int(getattr(photo, "file_size", 0) or 0),
            caption=(message.caption or "").strip(),
        )
    elif getattr(message, "voice", None):
        voice = message.voice
        pending = PendingUpload(
            file_id=voice.file_id,
            filename=f"voice_{voice.file_unique_id}.ogg",
            kind="voice",
            file_size=int(getattr(voice, "file_size", 0) or 0),
            caption=(message.caption or "").strip(),
        )

    if pending is None:
        await update.message.reply_text(
            "⚠️ <i>Unsupported file type.</i>\n\nSend a document, photo, video, or audio.",
            parse_mode="HTML",
        )
        return

    _cleanup_pending_uploads()
    _pending_uploads[chat_id] = pending

    emoji = _file_type_emoji(pending.kind)
    size_text = _human_size(pending.file_size) if pending.file_size else "unknown size"
    await update.message.reply_text(
        f"{emoji} <b>{pending.filename}</b>\n"
        f"<i>{size_text}</i>\n\n"
        "Where would you like to save this?",
        parse_mode="HTML",
        reply_markup=_make_destination_keyboard(chat_id),
    )




async def _handle_destination_callback(update, context) -> None:  # type: ignore[type-arg]
    """Handle inline keyboard taps for upload destination."""
    query = update.callback_query
    await query.answer()  # dismiss loading spinner on button

    data = query.data or ""
    # Format: "dest:{chat_id}:{choice}"
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "dest":
        return

    chat_id = int(parts[1])
    choice = parts[2]

    if choice == "cancel":
        _pending_uploads.pop(chat_id, None)
        await query.edit_message_text(
            "\u274c Upload cancelled.",
            parse_mode="HTML",
        )
        return

    if choice not in {"1", "2", "3"}:
        return

    # Remove the keyboard so buttons can't be tapped twice
    await query.edit_message_reply_markup(reply_markup=None)

    pending = _pending_uploads.get(chat_id)
    if pending is None:
        await query.edit_message_text(
            "⚠️ <i>No pending upload found. Please resend the file.</i>",
            parse_mode="HTML",
        )
        return

    # Delete the "where to save?" message so only the result remains in chat.
    try:
        await query.message.delete()
    except Exception:
        pass  # Message may be too old or already deleted — upload must still proceed.

    await _process_upload_choice(query, context, chat_id, choice, pending)




async def _process_upload_choice(
    source,
    context,
    chat_id: int,
    choice: str,
    pending: PendingUpload,
) -> None:
    """Process an upload destination choice from either a callback query or text message."""

    owner_hint = ""
    if hasattr(source, "effective_user") and source.effective_user:
        owner_hint = source.effective_user.username or source.effective_user.first_name or ""
    elif hasattr(source, "from_user") and source.from_user:
        owner_hint = source.from_user.username or source.from_user.first_name or ""

    owner = await _tb()._resolve_personal_owner(chat_id, owner_hint)
    if choice == "1" and not owner:
        # No personal folder is bound to this chat. Previously the resolver would have answered
        # "admin" here and the file would have been written into the ADMIN's private folder --
        # a wrong destination chosen silently, which for an upload means the file is somewhere the
        # sender cannot see and someone else can. Refuse and say so. (2026-08-08 audit, C-7.)
        await context.bot.send_message(
            chat_id=chat_id,
            text=("\U0001f464 <b>No personal folder is set for this chat.</b>\n\n"
                  "Ask an admin to assign one, or choose Family Shared instead."),
            parse_mode="HTML",
        )
        return

    dest_labels = {
        "1": f"\U0001f464 My Folder ({owner})",
        "2": "\U0001f468\u200d\U0001f469\u200d\U0001f467 Family Shared",
        "3": "\U0001f3ac Entertainment",
    }
    target_label = dest_labels[choice]
    emoji = _file_type_emoji(pending.kind)
    size_text = _human_size(pending.file_size) if pending.file_size else "unknown size"

    loop = asyncio.get_running_loop()
    started_at = loop.time()

    status_message = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"\U0001f4e5 <b>Downloading\u2026</b>\n\n"
            f"{emoji} <code>{pending.filename}</code>\n"
            f"\U0001f4e6 {size_text}\n"
            f"\U0001f4c2 {target_label}\n"
            f"\u23f1 Just started"
        ),
        parse_mode="HTML",
    )

    progress_task = None
    size_mb = pending.file_size / (1024 * 1024) if pending.file_size else 0
    if size_mb >= 5:
        progress_task = asyncio.create_task(
            _upload_progress_heartbeat(
                status_message,
                pending.filename,
                size_text,
                target_label,
                started_at,
            )
        )

    try:
        if choice == "1":
            base_dir = settings.personal_path / owner
            dest = await _tb()._store_private_or_shared_file(
                context.bot, pending, base_dir, owner, Scope.PERSONAL,
            )
        elif choice == "2":
            base_dir = settings.family_path
            dest = await _tb()._store_private_or_shared_file(
                context.bot, pending, base_dir, "family", Scope.FAMILY,
            )
        else:
            dest = await _tb()._store_entertainment_file(context.bot, pending)

        _pending_uploads.pop(chat_id, None)
        actual_bytes = dest.stat().st_size
        elapsed_seconds = loop.time() - started_at

        await _record_recent_file(chat_id, dest.name, str(dest), actual_bytes)

        # Incremental similar-image check for new uploads (background, non-blocking)
        asyncio.create_task(
            _check_new_file_for_similar(context.bot, chat_id, dest),
            name=f"phash_check_{dest.name}",
        )

        await _safe_edit_text(
            status_message,
            (
                f"\u2705 <b>Saved</b>\n\n"
                f"{emoji} <code>{dest.name}</code>\n"
                f"\U0001f4c2 {target_label}\n"
                f"\U0001f4e6 {_human_size(actual_bytes)}\n"
                f"\u26a1 {_format_avg_speed(actual_bytes, elapsed_seconds)}  "
                f"\u23f1 {_format_elapsed(elapsed_seconds)}"
            ),
        )

    except DuplicateFileError as dup:
        # Keep temp file on disk so /keep can proceed without re-downloading
        if choice == "1":
            _dup_base_dir = settings.personal_path / owner
        elif choice == "2":
            _dup_base_dir = settings.family_path
        else:
            _dup_base_dir = None  # entertainment: temp_path is already final destination
        _pending_duplicates[chat_id] = {
            "sha256": dup.sha256,
            "existing": dup.existing,
            "temp_path": dup.temp_path,
            "base_dir": str(_dup_base_dir) if _dup_base_dir else None,
            "choice": choice,
            "pending": pending,
            "owner": owner,
        }
        saved_on = dup.existing.get("saved_at", "")[:10]
        await _safe_edit_text(
            status_message,
            (
                f"\u26a0\ufe0f <b>Duplicate detected</b>\n\n"
                f"This file already exists as "
                f"<code>{_esc(dup.existing.get('filename', '?'))}</code>\n"
                f"Saved on: <i>{saved_on}</i>\n\n"
                "Use /keep to save anyway, or /skip to cancel."
            ),
        )

    except Exception as exc:
        logger.warning(
            "telegram_upload_failed chat_id=%s file=%s error=%s",
            chat_id, pending.filename, exc,
        )
        elapsed_seconds = loop.time() - started_at
        elapsed_text = _format_elapsed(elapsed_seconds)

        if _is_too_large_telegram_file_error(exc):
            await _safe_edit_text(
                status_message,
                (
                    f"\u26a0\ufe0f <b>File too large</b>\n\n"
                    f"{emoji} <code>{pending.filename}</code>\n"
                    f"\u23f1 Failed after {elapsed_text}\n\n"
                    "Ask your admin to enable <b>Large File mode</b> in AiHomeCloud:\n"
                    "<i>More \u2192 Telegram Bot \u2192 Large file mode (up to 2 GB)</i>"
                ),
            )
        elif isinstance(exc, InsufficientDiskSpaceError):
            await _safe_edit_text(
                status_message,
                (
                    f"\U0001f4be <b>Not enough storage space</b>\n\n"
                    f"{emoji} <code>{pending.filename}</code> needs {_human_size(exc.needed)}, "
                    f"only {_human_size(exc.free)} free.\n\n"
                    "Free up space on the NAS and try again."
                ),
            )
        elif _is_timeout_error(exc):
            await _safe_edit_text(
                status_message,
                (
                    f"\u23f1 <b>Download timed out</b>\n\n"
                    f"{emoji} <code>{pending.filename}</code>\n"
                    f"Failed after {elapsed_text}\n\n"
                    "Network may be slow. Please try again."
                ),
            )
        else:
            await _safe_edit_text(
                status_message,
                (
                    f"\u274c <b>Save failed</b>\n\n"
                    f"{emoji} <code>{pending.filename}</code>\n"
                    f"\u23f1 {elapsed_text}\n\n"
                    "Please try again."
                ),
            )
    finally:
        if progress_task is not None:
            progress_task.cancel()
            with suppress(asyncio.CancelledError):
                await progress_task




# ---------------------------------------------------------------------------
# Duplicate file handlers (Task 10)
# ---------------------------------------------------------------------------

async def _handle_keep(update, context) -> None:  # type: ignore[type-arg]
    """Keep (save) a file that was flagged as a duplicate."""
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return
    dup = _pending_duplicates.pop(chat_id, None)
    if not dup:
        await update.message.reply_text("No duplicate pending. Send a file first.", parse_mode="HTML")
        return
    temp_path = Path(dup["temp_path"])
    base_dir_str = dup.get("base_dir")
    scope = {"1": Scope.PERSONAL, "2": Scope.FAMILY}.get(dup.get("choice"), Scope.ENTERTAINMENT)
    try:
        if base_dir_str:
            # Private or family: sort from .inbox into the right category+bucket subfolder
            dest_path = _sort_file(temp_path, Path(base_dir_str), check_age=False)
            if dest_path is None:
                raise RuntimeError("Failed to sort file into destination folder")
            capture_epoch = _apply_capture_time(dest_path)
            category = dest_path.relative_to(Path(base_dir_str)).parts[0]
            owner = dup.get("owner") if scope == Scope.PERSONAL else None
            original_name = dup["pending"].filename
        else:
            # Entertainment: temp_path is a downloaded-but-unplaced .uploading file
            # (kept on disk specifically so /keep can finalize without a re-download —
            # see _store_entertainment_file's DuplicateFileError). Place it through the
            # same bucket allocator as a fresh, non-duplicate save.
            pending = dup["pending"]
            safe_name = _sanitize_filename(pending.filename, default_stem="telegram_media")
            dest_path, category, capture_epoch = _place_entertainment_file(temp_path, safe_name)
            owner = None
            original_name = pending.filename
        media_type, _ = mimetypes.guess_type(dest_path.name)
        await media_index.record_entry(
            rel_path=_nas_rel_path(dest_path),
            content_hash=dup["sha256"],
            size_bytes=dest_path.stat().st_size,
            scope=scope.value,
            owner=owner,
            category=category,
            media_type=media_type,
            source="telegram",
            filename=dest_path.name,
            original_name=original_name,
            capture_date=capture_epoch,
            mtime=dest_path.stat().st_mtime,
            duration=_extract_video_duration(dest_path),
        )
        await _record_recent_file(chat_id, dest_path.name, str(dest_path), dup.get("file_size", 0))
        await update.message.reply_text(
            f"✅ <b>Saved</b> <code>{dest_path.name}</code>", parse_mode="HTML"
        )
    except Exception as exc:
        logger.warning("keep_handler_error chat_id=%s error=%s", chat_id, exc)
        await update.message.reply_text("❌ Save failed. Please try uploading again.", parse_mode="HTML")




async def _handle_skip(update, context) -> None:  # type: ignore[type-arg]
    """Skip (discard) a file that was flagged as a duplicate."""
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return
    dup = _pending_duplicates.pop(chat_id, None)
    if not dup:
        await update.message.reply_text("No duplicate pending.", parse_mode="HTML")
        return
    try:
        temp_path = Path(dup["temp_path"])
        if temp_path.exists():
            temp_path.unlink()
    except Exception:
        pass
    await update.message.reply_text(
        "🗑 Duplicate skipped — file discarded.", parse_mode="HTML"
    )




async def _handle_recent(update, context) -> None:  # type: ignore[type-arg]
    """Show the user's last 5 uploaded files with delete buttons."""
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return
    all_recent = await _store.get_value("telegram_recent_files", default=[])
    mine = [r for r in all_recent if r.get("chat_id") == chat_id][-5:]
    if not mine:
        await update.message.reply_text("📂 No recent uploads yet.", parse_mode="HTML")
        return
    rows = []
    for i, r in enumerate(reversed(mine), 1):
        sz     = r.get("size", 0)
        sz_str = f"{sz/(1024*1024):.1f} MB" if sz >= 1024*1024 else f"{sz//1024} KB"
        rows.append([InlineKeyboardButton(
            f"🗑 Delete {r['filename']} ({sz_str})",
            callback_data=f"delrecent:{i-1}:{chat_id}",
        )])
    lines_txt = [f"{i}. <code>{r['filename']}</code>" for i, r in enumerate(reversed(mine), 1)]
    await update.message.reply_text(
        "📂 <b>Recent uploads</b>\n\n" + "\n".join(lines_txt),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )




async def _handle_delete_recent_callback(update, context) -> None:  # type: ignore[type-arg]
    """Handle delete-button press from /recent list."""
    query = update.callback_query
    await query.answer()
    parts     = query.data.split(":")
    idx       = int(parts[1])
    owner_cid = int(parts[2])
    all_recent = await _store.get_value("telegram_recent_files", default=[])
    mine       = [r for r in all_recent if r.get("chat_id") == owner_cid]
    rev_mine   = list(reversed(mine))
    if idx >= len(rev_mine):
        await query.edit_message_text("❌ File not found.", parse_mode="HTML")
        return
    target = rev_mine[idx]
    p = Path(target.get("path", ""))
    try:
        if p.exists():
            p.unlink()
    except Exception as exc:
        await query.edit_message_text(f"❌ Delete failed: {exc}", parse_mode="HTML")
        return
    key        = target.get("path", "")
    all_recent = [r for r in all_recent if r.get("path") != key]
    await _store.set_value("telegram_recent_files", all_recent)
    await query.edit_message_text(
        f"✅ Deleted <code>{target['filename']}</code>", parse_mode="HTML"
    )




async def _handle_empty_trash_callback(update, context) -> None:  # type: ignore[type-arg]
    """Handle Empty Trash inline-button press from trash warning."""
    query = update.callback_query
    await query.answer()
    from .. import store as _st
    items  = await _st.get_trash_items()
    errors = 0
    for item in items:
        try:
            _unlink_trash_item(item)
        except Exception:
            errors += 1
    await _st.save_trash_items([])
    if errors:
        await query.edit_message_text(
            f"⚠️ Trash emptied with {errors} error(s).", parse_mode="HTML"
        )
    else:
        await query.edit_message_text("✅ <b>Trash emptied.</b>", parse_mode="HTML")

# ---------------------------------------------------------------------------
# Incremental pHash check after upload
# ---------------------------------------------------------------------------

async def _check_new_file_for_similar(bot, chat_id: int, dest_path) -> None:
    """Background task: check newly uploaded file against cached pHashes.
    Notifies the user if a visually similar file already exists in storage.
    """
    try:
        from ..duplicate_scanner import get_duplicate_scanner
        from ..telegram.search_handlers import _fmt_bytes, _short_path, _sim_set_markup
        from ..config import settings as _settings

        scanner = get_duplicate_scanner()
        sets = await scanner.scan_single_file_for_similar(dest_path)
        if not sets:
            return

        entry = sets[0]
        copies = entry.get("copies", [])
        # Only notify if there's at least one OTHER file (not just the one just uploaded)
        others = [c for c in copies if c["path"] != str(dest_path)]
        if not others:
            return

        lines = ["⚠️ <b>Similar file already exists!</b>\n"]
        for i, c in enumerate(copies):
            short = _short_path(c["path"], c["owner"])
            dims = f"{c['width']}×{c['height']}" if c.get("width") and c.get("height") else ""
            label = "📤 New" if c["path"] == str(dest_path) else f"📁 Existing {i}"
            lines.append(
                f"{label}: <b>{_fmt_bytes(c['size_bytes'])}</b>"
                + (f" · {dims}" if dims else "")
                + f"\n   <code>{short}</code>"
            )
        lines.append("\n<i>Use /duplicates → Review Similar to manage.</i>")

        await bot.send_message(chat_id=chat_id, text="\n\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        logger.debug("phash_check_failed dest=%s error=%s", dest_path, exc)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

