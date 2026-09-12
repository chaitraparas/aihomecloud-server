"""Telegram bot handlers — search handlers."""

import shutil
import uuid
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from .bot_core import (
    logger,
    _check_allowed_and_rate, _is_admin_chat,
    _pending_uploads, _last_results,
    _PENDING_MAX_ENTRIES,
    _storage_bar,
    _get_chat_folder_owner,
)

_PAGE_SIZE = 20
# Per-chat current page offset for paginated search results.
_last_page: dict[int, int] = {}
from ..config import settings
from .. import store as _store

from .. import document_index as _docidx

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None  # type: ignore[assignment,misc]
    InlineKeyboardMarkup = None  # type: ignore[assignment,misc]

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Trash helper
# ---------------------------------------------------------------------------

async def _move_to_dup_trash(path: Path, owner: str) -> None:
    """Move *path* to the per-owner trash folder and record the item in the trash store.

    Mirrors the soft-delete logic in file_routes.delete_file so that files
    deleted through the duplicate review UI can be restored from the app.
    Raises OSError / Exception on failure — callers should catch and report.
    """
    filename = path.name
    ts = int(datetime.now(timezone.utc).timestamp())
    trash_name = f"{ts}_{filename}"

    user_trash_dir = settings.trash_dir / owner
    user_trash_dir.mkdir(parents=True, exist_ok=True)

    trash_path = user_trash_dir / trash_name
    counter = 1
    while trash_path.exists():
        trash_path = user_trash_dir / f"{ts}_{counter}_{filename}"
        counter += 1

    size_bytes = path.stat().st_size if path.is_file() else 0
    try:
        original_path = "/" + str(path.relative_to(settings.nas_root.resolve())).replace("\\", "/")
    except ValueError:
        original_path = str(path)

    item = {
        "id": str(uuid.uuid4()),
        "originalPath": original_path,
        "trashPath": str(trash_path),
        "filename": filename,
        "deletedAt": datetime.now(timezone.utc).isoformat(),
        "sizeBytes": size_bytes,
        "deletedBy": owner,
    }

    # Record metadata before moving — roll back if move fails
    items = await _store.get_trash_items()
    items.append(item)
    await _store.save_trash_items(items)

    try:
        shutil.move(str(path), str(trash_path))
    except Exception:
        items = [i for i in items if i["id"] != item["id"]]
        await _store.save_trash_items(items)
        raise

    logger.info("dup_trash owner=%s file=%s trash=%s", owner, path.name, trash_path)


async def _handle_help(update, context) -> None:  # type: ignore[type-arg]
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return

    owner = await _get_chat_folder_owner(chat_id) or "admin"
    is_admin = await _is_admin_chat(chat_id)
    admin_section = (
        "\n<b>Admin</b>\n"
        "• /approve &lt;chat_id&gt; — approve a pending request\n"
        "• /deny &lt;chat_id&gt; — deny a pending request\n"
    ) if is_admin else ""
    await update.message.reply_text(
        "🏠 <b>AiHomeCloud Bot</b>\n\n"
        "<b>Commands</b>\n"
        "• /list — last 10 indexed documents\n"
        "• /recent — your last 5 uploaded files\n"
        "• /storage — storage usage\n"
        "• /status — device health\n"
        "• /whoami — your linked profile\n"
        "• /cancel — discard a pending upload\n"
        "• /unlink — disconnect this Telegram account\n"
        "• /help — this message\n\n"
        "<b>Search</b>\n"
        "• Type any word to search files\n"
        "• Reply with a number to receive that file\n\n"
        "<b>Upload</b>\n"
        "• Send any file — tap where to save it\n"
        "• Supports documents, photos, videos, audio\n"
        "• Use /keep or /skip when a duplicate is detected\n"
        f"• Files save to your <b>{owner}</b> folder by default\n\n"
        + admin_section
        + "<i>Examples: aadhaar, pan card, invoice, passport</i>",
        parse_mode="HTML",
    )


async def _handle_list(update, context) -> None:  # type: ignore[type-arg]
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    # Scope to whoever this chat is linked to. Unscoped, this listed the newest documents belonging
    # to EVERY member and then offered "reply with a number" to download one — a full cross-member
    # read of private documents (the help text's own examples are aadhaar, PAN, passport).
    owner = await _get_chat_folder_owner(chat_id) or ""
    role = "admin" if await _is_admin_chat(chat_id) else "member"
    docs = await _docidx.list_recent_documents(limit=10, user_role=role, username=owner)
    if not docs:
        await update.message.reply_text(
            "📂 <i>No documents indexed yet.</i>\n\nSend a file to start building your library.",
            parse_mode="HTML",
        )
        return

    _last_results[chat_id] = docs
    if len(_last_results) > _PENDING_MAX_ENTRIES:
        oldest = next(iter(_last_results))
        del _last_results[oldest]
    lines = []
    for i, d in enumerate(docs, 1):
        added_by = d.get("added_by", "?")
        lines.append(f"{i}. <code>{d['filename']}</code>  <i>({added_by})</i>")

    await update.message.reply_text(
        "📄 <b>Recent documents</b>\n\n"
        + "\n".join(lines)
        + "\n\n<i>Reply with a number to receive the file.</i>",
        parse_mode="HTML",
    )




async def _handle_message(update, context) -> None:  # type: ignore[type-arg]
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return

    text = (update.message.text or "").strip()

    # Numeric reply → send file from previous search / list
    if text.isdigit():
        prev = _last_results.get(chat_id, [])
        idx = int(text) - 1
        if 0 <= idx < len(prev):
            await _send_file(update, prev[idx])
        else:
            await update.message.reply_text(
                "❓ <i>Invalid number.</i> Search for something first or use /list.",
                parse_mode="HTML",
            )
        return

    # Full-text search — fetch up to 200 results so pagination works in-memory.
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    # search_documents has always supported scoping; this call hardcoded admin and an empty
    # username, which disabled it. Pass the real identity of the linked chat instead.
    _owner = await _get_chat_folder_owner(chat_id) or ""
    _role = "admin" if await _is_admin_chat(chat_id) else "member"
    results = await _docidx.search_documents(
        query=text, limit=200, user_role=_role, username=_owner
    )

    if not results:
        await update.message.reply_text(
            f"🔍 <i>No documents found for</i> <b>{text}</b>.\n\n"
            "Try a different word or /list to browse recent files.",
            parse_mode="HTML",
        )
        return

    _last_results[chat_id] = results
    _last_page[chat_id] = 0
    if len(_last_results) > _PENDING_MAX_ENTRIES:
        oldest = next(iter(_last_results))
        del _last_results[oldest]
    await _render_search_page(update.message, chat_id, results, offset=0, keyword=text)




def _build_search_page_text(results: list, offset: int, keyword: str = "") -> str:
    """Build the message text for one page of search results."""
    total = len(results)
    page = results[offset: offset + _PAGE_SIZE]
    showing_to = min(offset + _PAGE_SIZE, total)

    if keyword:
        header = f"🔍 <b>{total} document{'s' if total != 1 else ''} found for '{keyword}'</b>"
    else:
        header = f"🔍 <b>{total} document{'s' if total != 1 else ''} found</b>"

    if total > _PAGE_SIZE:
        header += f"\n<i>Showing {offset + 1}–{showing_to}</i>"

    lines = []
    for i, r in enumerate(page):
        abs_num = offset + i + 1  # 1-based absolute index
        try:
            parent = Path(r["path"]).parent.name
            folder_hint = (
                f"  <i>{parent}</i>"
                if parent not in ("personal", "family", "entertainment")
                else ""
            )
        except Exception:
            folder_hint = ""
        lines.append(f"{abs_num}. <code>{r['filename']}</code>{folder_hint}")

    return header + "\n\n" + "\n".join(lines) + "\n\n<i>Reply with a number to receive the file.</i>"


def _build_search_page_markup(chat_id: int, offset: int, total: int):
    """Build Prev / Next inline keyboard for paginated results, or None."""
    if InlineKeyboardButton is None:
        return None
    nav: list = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            "◀ Prev 20",
            callback_data=f"searchpage:{chat_id}:{max(0, offset - _PAGE_SIZE)}",
        ))
    if offset + _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(
            "Next 20 ▶",
            callback_data=f"searchpage:{chat_id}:{offset + _PAGE_SIZE}",
        ))
    if not nav:
        return None
    return InlineKeyboardMarkup([nav])


async def _render_search_page(
    message,
    chat_id: int,
    results: list,
    offset: int,
    keyword: str = "",
) -> None:
    """Send a new message with one page of search results + Prev/Next buttons."""
    text = _build_search_page_text(results, offset, keyword)
    markup = _build_search_page_markup(chat_id, offset, len(results))
    await message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def _handle_search_page_callback(update, context) -> None:  # type: ignore[type-arg]
    """Inline keyboard callback: navigate to a different page of search results."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.edit_message_text("❌ Invalid pagination data.")
        return

    try:
        chat_id = int(parts[1])
        offset = int(parts[2])
    except ValueError:
        await query.edit_message_text("❌ Invalid pagination data.")
        return

    results = _last_results.get(chat_id)
    if not results:
        await query.edit_message_text(
            "⚠️ <i>Search results expired — please search again.</i>",
            parse_mode="HTML",
        )
        return

    offset = max(0, min(offset, len(results) - 1))
    _last_page[chat_id] = offset

    text = _build_search_page_text(results, offset)
    markup = _build_search_page_markup(chat_id, offset, len(results))
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def _send_file(update, doc: dict) -> None:
    nas_path = doc.get("path", "")
    abs_path = settings.nas_root / nas_path.lstrip("/")
    p = Path(str(abs_path))
    if not p.exists() or not p.is_file():
        if nas_path:
            await _docidx.remove_document(nas_path)
        await update.message.reply_text(
            f"⚠️ <b>File not found</b>\n\n"
            f"<code>{doc.get('filename', '?')}</code>\n\n"
            "<i>It may have been moved or deleted. The index has been updated.</i>",
            parse_mode="HTML",
        )
        return
    with open(p, "rb") as fh:
        await update.message.reply_document(document=fh, filename=doc.get("filename", p.name))


# ---------------------------------------------------------------------------
# New commands: /status, /cancel, /whoami, /unlink
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# New commands: /status, /cancel, /whoami, /unlink
# ---------------------------------------------------------------------------


async def _handle_status(update, context) -> None:  # type: ignore[type-arg]
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    if psutil is None:
        await update.message.reply_text("⚠️ System stats unavailable (psutil not installed).")
        return

    try:
        import time as _time

        cpu = psutil.cpu_percent(interval=0.3)
        ram = psutil.virtual_memory()
        ram_pct = round(ram.percent, 1)
        ram_used_gb = round(ram.used / (1024 ** 3), 1)
        ram_total_gb = round(ram.total / (1024 ** 3), 1)

        # Uptime
        boot_time = psutil.boot_time()
        uptime_sec = int(_time.time() - boot_time)
        hours, rem = divmod(uptime_sec, 3600)
        minutes, _ = divmod(rem, 60)
        if hours >= 24:
            uptime_str = f"{hours // 24}d {hours % 24}h"
        else:
            uptime_str = f"{hours}h {minutes}m"

        # Temperature
        temp_str = "n/a"
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for key in ("cpu_thermal", "soc_thermal", "coretemp"):
                    entries = temps.get(key, [])
                    if entries:
                        temp_str = f"{entries[0].current:.0f}\u00b0C"
                        break
        except Exception:
            pass

        # Storage
        try:
            usage = psutil.disk_usage(str(settings.nas_root))
            used_gb = round(usage.used / (1024 ** 3), 1)
            total_gb_s = round(usage.total / (1024 ** 3), 1)
            free_gb = round(usage.free / (1024 ** 3), 1)
            pct = round(usage.used / usage.total * 100, 1)
            storage_bar = _storage_bar(pct)
            storage_str = f"{storage_bar}  {used_gb} / {total_gb_s} GB ({pct}%)"
        except Exception:
            storage_str = "unavailable"
            free_gb = 0.0

        # Health indicator
        if cpu > 80 or ram_pct > 85:
            health_icon, health_text = "🔴", "High load"
        elif cpu > 50 or ram_pct > 70:
            health_icon, health_text = "🟡", "Moderate"
        else:
            health_icon, health_text = "🟢", "Healthy"

        await update.message.reply_text(
            f"🖥 <b>AiHomeCloud Status</b>  {health_icon} {health_text}\n\n"
            f"\u23f1 Uptime:  <b>{uptime_str}</b>\n"
            f"\U0001f9e0 CPU:     <b>{cpu:.0f}%</b>\n"
            f"\U0001f4be RAM:     <b>{ram_used_gb} / {ram_total_gb} GB</b>  ({ram_pct}%)\n"
            f"\U0001f321 Temp:    <b>{temp_str}</b>\n\n"
            f"\U0001f4bd Storage\n{storage_str}\n"
            f"<i>{free_gb} GB free</i>",
            parse_mode="HTML",
        )

    except Exception as exc:
        logger.warning("telegram_status_error: %s", exc)
        await update.message.reply_text(
            "⚠️ <i>Could not read device status.</i>",
            parse_mode="HTML",
        )




async def _handle_cancel(update, context) -> None:  # type: ignore[type-arg]
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return

    had_pending = chat_id in _pending_uploads
    _pending_uploads.pop(chat_id, None)
    _last_results.pop(chat_id, None)

    if had_pending:
        await update.message.reply_text(
            "\u274c <b>Upload cancelled.</b>\n\n<i>Send a new file whenever you're ready.</i>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "\u2705 <i>Nothing to cancel.</i>",
            parse_mode="HTML",
        )




async def _handle_whoami(update, context) -> None:  # type: ignore[type-arg]
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return

    owner = await _get_chat_folder_owner(chat_id) or "admin"
    first_name = update.effective_user.first_name or "there"
    tg_username = update.effective_user.username
    tg_line = f"@{tg_username}" if tg_username else f"ID: {chat_id}"

    await update.message.reply_text(
        f"👤 <b>{first_name}</b>  ({tg_line})\n\n"
        f"Personal folder: <b>{owner}</b>\n\n"
        "<i>To switch folder: /auth &lt;name&gt;</i>",
        parse_mode="HTML",
    )




# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_bytes(b: int) -> str:
    if b < 1024 * 1024:
        return f"{b / 1024:.0f} KB"
    if b < 1024 ** 3:
        return f"{b / (1024 * 1024):.1f} MB"
    return f"{b / (1024 ** 3):.2f} GB"


def _short_path(full_path: str, owner: str) -> str:
    """Show owner/subfolder/filename without the long /srv/nas/personal/... prefix."""
    try:
        p = Path(full_path)
        rel = p.relative_to(settings.nas_root)
        parts = rel.parts
        # personal/Owner/Subfolder/file.jpg → Owner/Subfolder/file.jpg
        if len(parts) >= 3 and parts[0] == settings.personal_base:
            return "/".join(parts[1:])
        return "/".join(parts)
    except Exception:
        return Path(full_path).name


def _dup_summary_text(exact: list, similar: list, ran_at: str | None) -> str:
    date_note = f"  <i>Last scan: {ran_at[:10] if ran_at else 'never'}</i>"
    if not exact and not similar:
        return f"✅ <b>No duplicates found.</b>{date_note}\n\n<i>Scan runs nightly at 4 AM.</i>"

    wasted = sum(r.get("sizeBytes", 0) * (len(r.get("copies", [])) - 1) for r in exact)
    lines = [f"📊 <b>Duplicate Summary</b>{date_note}\n"]
    if exact:
        lines.append(f"🔴 <b>Exact copies:</b> {len(exact)} sets · {_fmt_bytes(wasted)} recoverable")
    else:
        lines.append("🔴 <b>Exact copies:</b> none")
    if similar:
        lines.append(f"🟡 <b>Similar images:</b> {len(similar)} sets (WhatsApp compressed vs originals)")
    else:
        lines.append("🟡 <b>Similar images:</b> none")
    lines.append("\n<i>Scan runs nightly at 4 AM.</i>")
    return "\n".join(lines)


def _dup_summary_markup(exact: list, similar: list, is_admin: bool):
    if InlineKeyboardMarkup is None:
        return None
    rows = []
    # Review Exact/Review Similar lead into household-wide duplicate data (every family
    # member's personal/<name>/ folder) -- gated behind is_admin like Auto-clean and Scan Now
    # below, so a non-admin chat never even sees a button whose handler will reject it.
    if is_admin:
        review_row = []
        if exact:
            review_row.append(InlineKeyboardButton(
                f"🗑 Review Exact ({len(exact)})", callback_data="dupexact:0"
            ))
        if similar:
            review_row.append(InlineKeyboardButton(
                f"📸 Review Similar ({len(similar)})", callback_data="dupsim:0"
            ))
        if review_row:
            rows.append(review_row)
        if exact:
            rows.append([InlineKeyboardButton("⚡ Auto-clean Exact", callback_data="dupauto:ask")])
        rows.append([InlineKeyboardButton("🔄 Scan Now", callback_data="dupscan:now")])
    return InlineKeyboardMarkup(rows) if rows else None


# ── /duplicates command ───────────────────────────────────────────────────────

async def _handle_duplicates(update, context) -> None:  # type: ignore[type-arg]
    """Show duplicate summary with Review / Auto-clean / Scan Now buttons."""
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    exact = await _store.get_value("duplicate_scan_results", default=[])
    similar = await _store.get_value("similar_scan_results", default=[])
    ran_at = await _store.get_value("duplicate_scan_ran_at", default=None)
    is_admin = await _is_admin_chat(chat_id)

    await update.message.reply_text(
        _dup_summary_text(exact, similar, ran_at),
        parse_mode="HTML",
        reply_markup=_dup_summary_markup(exact, similar, is_admin),
    )


# ── /scan command ─────────────────────────────────────────────────────────────

async def _handle_scan(update, context) -> None:  # type: ignore[type-arg]
    """Trigger an immediate duplicate scan (admin only)."""
    import asyncio as _asyncio
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return
    if not await _is_admin_chat(chat_id):
        await update.message.reply_text("🔒 <i>Admin access required.</i>", parse_mode="HTML")
        return
    from ..duplicate_scanner import get_duplicate_scanner, DuplicateScanner
    scanner = get_duplicate_scanner()
    if scanner.is_scanning:
        await update.message.reply_text("⏳ <i>A scan is already in progress.</i>", parse_mode="HTML")
        return

    bot = context.bot

    async def _run_and_notify() -> None:
        try:
            exact, similar = await scanner._scan_nas_for_duplicates()
            report = DuplicateScanner._format_telegram_report(exact, similar)
            if report:
                await bot.send_message(chat_id=chat_id, text=report, parse_mode="HTML")
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text="✅ <b>Scan complete</b> — no duplicates found.",
                    parse_mode="HTML",
                )
        except Exception as exc:
            logger.error("manual_scan_failed chat_id=%s error=%s", chat_id, exc)
            await bot.send_message(
                chat_id=chat_id,
                text="❌ Scan failed. Check server logs.",
                parse_mode="HTML",
            )

    _asyncio.create_task(_run_and_notify(), name="telegram_dup_scan")
    await update.message.reply_text(
        "🔍 <b>Duplicate scan started</b> (exact + similar images).\n\n"
        "I'll notify you here when it's done. This may take a few minutes.",
        parse_mode="HTML",
    )


# ── Exact review ──────────────────────────────────────────────────────────────

def _exact_set_text(entry: dict, idx: int, total: int) -> str:
    size_str = _fmt_bytes(entry.get("sizeBytes", 0))
    filename = entry.get("filename", "?")
    copies = entry.get("copies", [])
    lines = [
        f"🔴 <b>Exact Duplicate — {idx + 1} of {total}</b>\n",
        f"📄 <code>{filename}</code> · {size_str}\n",
    ]
    for i, c in enumerate(copies, 1):
        short = _short_path(c["path"], c["owner"])
        lines.append(f"Copy {i} · <b>{c['owner']}</b>: <code>{short}</code>")
    return "\n".join(lines)


def _exact_set_markup(entry: dict, idx: int, total: int):
    if InlineKeyboardMarkup is None:
        return None
    hash_prefix = entry.get("hash", "")[:16]
    copies = entry.get("copies", [])
    rows = []
    del_row = []
    for i, c in enumerate(copies):
        del_row.append(InlineKeyboardButton(
            f"🗑 Del {c['owner']} copy", callback_data=f"dupexactdel:{hash_prefix}:{i}"
        ))
    rows.append(del_row)
    rows.append([InlineKeyboardButton(
        "✅ Keep all, don't ask again", callback_data=f"dupexactkeep:{hash_prefix}"
    )])
    nav = [InlineKeyboardButton("⏭ Skip", callback_data=f"dupexact:{idx + 1}")]
    if idx > 0:
        nav.insert(0, InlineKeyboardButton("◀", callback_data=f"dupexact:{idx - 1}"))
    if idx + 1 < total:
        nav.append(InlineKeyboardButton("▶", callback_data=f"dupexact:{idx + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("↩ Summary", callback_data="dups:summary")])
    return InlineKeyboardMarkup(rows)


async def _handle_dupexact_callback(update, context) -> None:  # type: ignore[type-arg]
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    if not await _is_admin_chat(chat_id):
        await query.edit_message_text("🔒 Admin access required.")
        return
    parts = query.data.split(":")
    try:
        idx = int(parts[1])
    except (IndexError, ValueError):
        idx = 0
    exact = await _store.get_value("duplicate_scan_results", default=[])
    if not exact:
        await query.edit_message_text("✅ No exact duplicates remain.", parse_mode="HTML")
        return
    idx = max(0, min(idx, len(exact) - 1))
    await query.edit_message_text(
        _exact_set_text(exact[idx], idx, len(exact)),
        parse_mode="HTML",
        reply_markup=_exact_set_markup(exact[idx], idx, len(exact)),
    )


async def _handle_dupexactdel_callback(update, context) -> None:  # type: ignore[type-arg]
    """Delete one copy from an exact-duplicate set, then show updated set."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    if not await _is_admin_chat(chat_id):
        await query.edit_message_text("🔒 Admin access required.")
        return
    parts = query.data.split(":")
    if len(parts) != 3:
        await query.edit_message_text("❌ Invalid data.")
        return
    _, hash_prefix, idx_str = parts
    try:
        copy_idx = int(idx_str)
    except ValueError:
        await query.edit_message_text("❌ Invalid index.")
        return

    exact = await _store.get_value("duplicate_scan_results", default=[])
    entry = next((r for r in exact if r.get("hash", "").startswith(hash_prefix)), None)
    if entry is None:
        await query.edit_message_text("⚠️ Set already resolved.")
        return
    copies = entry.get("copies", [])
    if copy_idx >= len(copies):
        await query.edit_message_text("⚠️ Copy index out of range.")
        return

    target_path = Path(copies[copy_idx]["path"])
    if not target_path.exists():
        await query.edit_message_text(
            f"⚠️ Already gone: <code>{target_path.name}</code>", parse_mode="HTML"
        )
        return
    owner = copies[copy_idx].get("owner", "admin")
    try:
        await _move_to_dup_trash(target_path, owner)
    except Exception as exc:
        logger.warning("dupexactdel trash_failed path=%s error=%s", target_path, exc)
        await query.edit_message_text(f"❌ Move to trash failed: {exc}")
        return

    # Remove from document index
    try:
        nas_rel = "/" + str(target_path.relative_to(settings.nas_root.resolve())).replace("\\", "/")
        await _docidx.remove_document(nas_rel)
    except Exception:
        pass

    path_str = str(target_path)

    def _prune(stored: list) -> list:
        out = []
        for r in stored:
            remaining = [c for c in r.get("copies", []) if c["path"] != path_str]
            if len(remaining) >= 2:
                out.append({**r, "copies": remaining})
        return out

    await _store.atomic_update("duplicate_scan_results", _prune, default=[])
    updated = await _store.get_value("duplicate_scan_results", default=[])

    # Find current position of this set (or move to next)
    new_idx = next(
        (i for i, r in enumerate(updated) if r.get("hash", "").startswith(hash_prefix)), 0
    )
    if not updated:
        await query.edit_message_text("✅ All exact duplicates resolved!", parse_mode="HTML")
        return
    new_idx = max(0, min(new_idx, len(updated) - 1))
    await query.edit_message_text(
        f"✅ Deleted <code>{target_path.name}</code>\n\n"
        + _exact_set_text(updated[new_idx], new_idx, len(updated)),
        parse_mode="HTML",
        reply_markup=_exact_set_markup(updated[new_idx], new_idx, len(updated)),
    )


async def _handle_dupexactkeep_callback(update, context) -> None:  # type: ignore[type-arg]
    """Mark an exact-duplicate set as 'keep all' -- whitelist the hash and remove from results.

    Without this, an intentional duplicate (e.g. saved via /keep, or shared to family)
    resurfaces in every nightly rescan and every evening report forever, with no way to
    permanently silence it. Mirrors the similar-image "Keep both" whitelist.
    """
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    if not await _is_admin_chat(chat_id):
        await query.edit_message_text("🔒 Admin access required.")
        return
    parts = query.data.split(":")
    if len(parts) != 2:
        await query.edit_message_text("❌ Invalid data.")
        return
    hash_prefix = parts[1]

    exact = await _store.get_value("duplicate_scan_results", default=[])
    entry = next((r for r in exact if r.get("hash", "").startswith(hash_prefix)), None)
    if entry is None:
        await query.edit_message_text("⚠️ Set already resolved.")
        return
    full_hash = entry["hash"]

    exact_whitelist = list(await _store.get_value("duplicate_exact_whitelist", default=[]))
    if full_hash not in exact_whitelist:
        exact_whitelist.append(full_hash)
        await _store.set_value("duplicate_exact_whitelist", exact_whitelist)

    def _prune(stored: list) -> list:
        return [r for r in stored if r.get("hash") != full_hash]

    await _store.atomic_update("duplicate_scan_results", _prune, default=[])
    updated = await _store.get_value("duplicate_scan_results", default=[])

    if not updated:
        await query.edit_message_text(
            "✅ <b>Kept all copies.</b>\n\nAll exact duplicates reviewed! 🎉",
            parse_mode="HTML",
        )
        return
    new_idx = next(
        (i for i, r in enumerate(updated) if r.get("hash", "").startswith(hash_prefix)), 0
    )
    new_idx = max(0, min(new_idx, len(updated) - 1))
    await query.edit_message_text(
        f"✅ <b>Kept all copies.</b> Won't appear in future scans unless files change.\n\n"
        + _exact_set_text(updated[new_idx], new_idx, len(updated)),
        parse_mode="HTML",
        reply_markup=_exact_set_markup(updated[new_idx], new_idx, len(updated)),
    )


# ── Auto-clean ────────────────────────────────────────────────────────────────

async def _handle_dupauto_callback(update, context) -> None:  # type: ignore[type-arg]
    """Auto-clean exact duplicates — ask confirmation first."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    if not await _is_admin_chat(chat_id):
        await query.edit_message_text("🔒 Admin access required.")
        return
    action = query.data.split(":")[-1]

    if action == "ask":
        exact = await _store.get_value("duplicate_scan_results", default=[])
        if not exact:
            await query.edit_message_text("✅ No exact duplicates to clean.", parse_mode="HTML")
            return
        total_files = sum(len(r.get("copies", [])) - 1 for r in exact)
        wasted = sum(r.get("sizeBytes", 0) * (len(r.get("copies", [])) - 1) for r in exact)
        await query.edit_message_text(
            f"⚠️ <b>Auto-clean Exact Duplicates</b>\n\n"
            f"Will delete <b>{total_files} duplicate files</b> ({_fmt_bytes(wasted)}).\n"
            f"Keeps the largest copy of each set.\n\n"
            f"<i>This cannot be undone.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, delete all", callback_data="dupauto:go"),
                InlineKeyboardButton("❌ Cancel", callback_data="dups:summary"),
            ]]),
        )

    elif action == "go":
        exact = await _store.get_value("duplicate_scan_results", default=[])
        deleted, failed = 0, 0
        for entry in exact:
            copies = entry.get("copies", [])
            # Keep copy with largest path size (index 0 — already sorted largest first by scanner)
            # Delete the rest
            for copy in copies[1:]:
                p = Path(copy["path"])
                if not p.exists():
                    deleted += 1
                    continue
                try:
                    await _move_to_dup_trash(p, copy.get("owner", "admin"))
                    deleted += 1
                    try:
                        nas_rel = "/" + str(p.relative_to(settings.nas_root.resolve())).replace("\\", "/")
                        await _docidx.remove_document(nas_rel)
                    except Exception:
                        pass
                except Exception:
                    failed += 1
        await _store.set_value("duplicate_scan_results", [])
        result_line = f"✅ <b>Auto-clean complete.</b>\n\nDeleted {deleted} file{'s' if deleted != 1 else ''}"
        if failed:
            result_line += f" ({failed} failed)"
        await query.edit_message_text(result_line + ".", parse_mode="HTML")


# ── Summary callback (from scan-complete notification or ↩ Summary button) ───

async def _handle_dups_summary_callback(update, context) -> None:  # type: ignore[type-arg]
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    exact = await _store.get_value("duplicate_scan_results", default=[])
    similar = await _store.get_value("similar_scan_results", default=[])
    ran_at = await _store.get_value("duplicate_scan_ran_at", default=None)
    is_admin = await _is_admin_chat(chat_id)
    await query.edit_message_text(
        _dup_summary_text(exact, similar, ran_at),
        parse_mode="HTML",
        reply_markup=_dup_summary_markup(exact, similar, is_admin),
    )


async def _handle_dupscan_callback(update, context) -> None:  # type: ignore[type-arg]
    """Inline 'Scan Now' button — admin only."""
    import asyncio as _asyncio
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    if not await _is_admin_chat(chat_id):
        await query.edit_message_text("🔒 Admin access required.")
        return
    from ..duplicate_scanner import get_duplicate_scanner
    scanner = get_duplicate_scanner()
    if scanner.is_scanning:
        await query.edit_message_text("⏳ <i>A scan is already running.</i>", parse_mode="HTML")
        return
    _asyncio.create_task(scanner._scan_nas_for_duplicates(), name="telegram_dup_scan_inline")
    await query.edit_message_text(
        "🔍 <b>Scan started</b> (exact + similar images).\n\nUse /duplicates to check results.",
        parse_mode="HTML",
    )


# ── Similar image review ──────────────────────────────────────────────────────

def _sim_set_text(entry: dict, idx: int, total: int) -> str:
    copies = entry.get("copies", [])
    lines = [f"📸 <b>Similar Images — {idx + 1} of {total}</b>\n"]
    for i, c in enumerate(copies):
        short = _short_path(c["path"], c["owner"])
        dims = f"{c['width']}×{c['height']}" if c.get("width") and c.get("height") else "unknown size"
        label = "🏆 High res" if i == 0 else f"📱 Copy {i + 1}"
        lines.append(
            f"{label}: <b>{_fmt_bytes(c['size_bytes'])}</b> · {dims}\n"
            f"   <code>{short}</code>"
        )
    return "\n\n".join(lines)


def _sim_set_markup(idx: int, total: int):
    if InlineKeyboardMarkup is None:
        return None
    rows = [
        [
            InlineKeyboardButton("👁 See both", callback_data=f"dupsimboth:{idx}"),
            InlineKeyboardButton("✅ Keep both", callback_data=f"dupsimkeepboth:{idx}"),
        ],
        [
            InlineKeyboardButton("🏆 Keep large", callback_data=f"dupsimdel:{idx}:small"),
            InlineKeyboardButton("📱 Keep small", callback_data=f"dupsimdel:{idx}:large"),
        ],
        [
            InlineKeyboardButton("⏭ Skip", callback_data=f"dupsim:{idx + 1}"),
        ],
    ]
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"dupsim:{idx - 1}"))
    if idx + 1 < total:
        nav.append(InlineKeyboardButton("▶", callback_data=f"dupsim:{idx + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("↩ Summary", callback_data="dups:summary")])
    return InlineKeyboardMarkup(rows)


async def _handle_dupsim_callback(update, context) -> None:  # type: ignore[type-arg]
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    if not await _is_admin_chat(chat_id):
        await query.edit_message_text("🔒 Admin access required.")
        return
    parts = query.data.split(":")
    try:
        idx = int(parts[1])
    except (IndexError, ValueError):
        idx = 0
    similar = await _store.get_value("similar_scan_results", default=[])
    if not similar:
        await query.edit_message_text("✅ No similar image sets remain.", parse_mode="HTML")
        return
    idx = max(0, min(idx, len(similar) - 1))
    await query.edit_message_text(
        _sim_set_text(similar[idx], idx, len(similar)),
        parse_mode="HTML",
        reply_markup=_sim_set_markup(idx, len(similar)),
    )


async def _handle_dupsimboth_callback(update, context) -> None:  # type: ignore[type-arg]
    """Send both similar files to the chat then re-show action buttons."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    if not await _is_admin_chat(chat_id):
        await query.edit_message_text("🔒 Admin access required.")
        return
    parts = query.data.split(":")
    try:
        idx = int(parts[1])
    except (IndexError, ValueError):
        idx = 0
    similar = await _store.get_value("similar_scan_results", default=[])
    if not similar or idx >= len(similar):
        await query.edit_message_text("⚠️ Set no longer available.", parse_mode="HTML")
        return
    copies = similar[idx].get("copies", [])
    for c in copies:
        p = Path(c["path"])
        if p.exists() and p.is_file():
            try:
                with open(p, "rb") as fh:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=fh,
                        filename=c["filename"],
                        caption=f"{_fmt_bytes(c['size_bytes'])} · {c['width']}×{c['height']}",
                    )
            except Exception as exc:
                logger.warning("dupsimboth send failed path=%s: %s", p, exc)
    # Re-send action buttons as a new message
    await context.bot.send_message(
        chat_id=chat_id,
        text=_sim_set_text(similar[idx], idx, len(similar)),
        parse_mode="HTML",
        reply_markup=_sim_set_markup(idx, len(similar)),
    )


async def _handle_dupsimkeepboth_callback(update, context) -> None:  # type: ignore[type-arg]
    """Mark a similar-image set as 'keep both' — whitelist the phash pair and remove from results."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    if not await _is_admin_chat(chat_id):
        await query.edit_message_text("🔒 Admin access required.")
        return
    parts = query.data.split(":")
    try:
        idx = int(parts[1])
    except (IndexError, ValueError):
        idx = 0

    similar = await _store.get_value("similar_scan_results", default=[])
    if not similar or idx >= len(similar):
        await query.edit_message_text("⚠️ Set no longer available.", parse_mode="HTML")
        return

    copies = similar[idx].get("copies", [])

    # Record all phash pairs from this set into the whitelist so future scans skip them
    phashes = [c.get("phash_hex", "") for c in copies if c.get("phash_hex")]
    if len(phashes) >= 2:
        whitelist = list(await _store.get_value("similar_phash_whitelist", default=[]))
        existing_pairs = {(p[0], p[1]) for p in whitelist if isinstance(p, list) and len(p) == 2}
        for a in range(len(phashes)):
            for b in range(a + 1, len(phashes)):
                pair = sorted([phashes[a], phashes[b]])
                key = (pair[0], pair[1])
                if key not in existing_pairs:
                    whitelist.append(pair)
                    existing_pairs.add(key)
        await _store.set_value("similar_phash_whitelist", whitelist)

    # Remove this set from results
    def _prune(stored: list) -> list:
        return [s for i, s in enumerate(stored) if i != idx]

    await _store.atomic_update("similar_scan_results", _prune, default=[])
    updated = await _store.get_value("similar_scan_results", default=[])

    if not updated:
        await query.edit_message_text(
            "✅ <b>Kept both files.</b>\n\n"
            "All similar image sets reviewed! 🎉",
            parse_mode="HTML",
        )
    else:
        next_idx = min(idx, len(updated) - 1)
        await query.edit_message_text(
            f"✅ <b>Kept both.</b> Won't appear in future scans unless files change.\n\n"
            f"{_sim_set_text(updated[next_idx], next_idx, len(updated))}",
            parse_mode="HTML",
            reply_markup=_sim_set_markup(next_idx, len(updated)),
        )


async def _handle_dupsimdel_callback(update, context) -> None:  # type: ignore[type-arg]
    """Delete 'large' or 'small' copy from a similar-image set."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    if not await _is_admin_chat(chat_id):
        await query.edit_message_text("🔒 Admin access required.")
        return
    parts = query.data.split(":")
    if len(parts) != 3:
        await query.edit_message_text("❌ Invalid data.")
        return
    try:
        idx = int(parts[1])
    except ValueError:
        await query.edit_message_text("❌ Invalid index.")
        return
    which = parts[2]  # "large" or "small"

    similar = await _store.get_value("similar_scan_results", default=[])
    if idx >= len(similar):
        await query.edit_message_text("⚠️ Set no longer available.", parse_mode="HTML")
        return
    copies = similar[idx].get("copies", [])
    # copies[0] = largest (highest quality), copies[-1] = smallest (most compressed)
    target_copy = copies[0] if which == "large" else copies[-1]
    target_path = Path(target_copy["path"])

    if not target_path.exists():
        await query.edit_message_text(
            f"⚠️ Already gone: <code>{target_path.name}</code>", parse_mode="HTML"
        )
    else:
        try:
            await _move_to_dup_trash(target_path, target_copy.get("owner", "admin"))
            # Remove from document index
            try:
                nas_rel = "/" + str(target_path.relative_to(settings.nas_root.resolve())).replace("\\", "/")
                await _docidx.remove_document(nas_rel)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("dupsimdel trash_failed path=%s error=%s", target_path, exc)
            await query.edit_message_text(f"❌ Move to trash failed: {exc}")
            return

    # Remove this set from results and show next
    def _prune_sim(stored: list) -> list:
        return [s for i, s in enumerate(stored) if i != idx]

    await _store.atomic_update("similar_scan_results", _prune_sim, default=[])
    updated = await _store.get_value("similar_scan_results", default=[])

    action_word = "Kept large" if which == "small" else "Kept small"
    if not updated:
        await query.edit_message_text(
            f"✅ {action_word}, deleted <code>{target_path.name}</code>.\n\n"
            "All similar image sets reviewed!",
            parse_mode="HTML",
        )
        return
    new_idx = max(0, min(idx, len(updated) - 1))
    await query.edit_message_text(
        f"✅ {action_word}, deleted <code>{target_path.name}</code>.\n\n"
        + _sim_set_text(updated[new_idx], new_idx, len(updated)),
        parse_mode="HTML",
        reply_markup=_sim_set_markup(new_idx, len(updated)),
    )


# ── legacy skip callback (kept for any in-flight messages) ───────────────────

async def _handle_dupskip_callback(update, context) -> None:  # type: ignore[type-arg]
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)


async def _handle_mount(update, context) -> None:  # type: ignore[type-arg]
    """Try to recover/remount the NAS drive — useful after a power cut."""
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return
    if not await _is_admin_chat(chat_id):
        await update.message.reply_text(
            "🔒 <i>Admin access required.</i>", parse_mode="HTML"
        )
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        import httpx
        async with httpx.AsyncClient(verify=False, timeout=30) as client:  # nosec B501 — localhost self-signed cert, intentional
            from ..config import settings as _settings
            base = f"https://localhost:{_settings.https_port}"

            # Get admin token from the running service's internal token exchange
            # using the first admin user's credentials would require a password.
            # Instead call the recover endpoint directly with a service-internal call.
            resp = await client.post(
                f"{base}/api/v1/storage/recover",
                headers={"X-Internal-Service": "telegram-bot"},
            )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "already_mounted":
                await update.message.reply_text(
                    f"✅ <b>Storage is already mounted.</b>\n"
                    f"<code>{data.get('device', '')}</code>",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"✅ <b>Storage remounted successfully.</b>\n"
                    f"<code>{data.get('device', '')}</code>",
                    parse_mode="HTML",
                )
        elif resp.status_code == 404:
            await update.message.reply_text(
                "❌ <b>No NAS drive found.</b>\n\n"
                "<i>Make sure the USB/NVMe drive is plugged in, then try again.</i>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"❌ <b>Remount failed.</b>\n<i>{resp.text[:200]}</i>",
                parse_mode="HTML",
            )
    except Exception as exc:
        # Fallback: call the storage routes directly (same process)
        try:
            from ..routes.storage_routes import _scan_for_unmounted_nas_drive, _do_mount_device
            from .. import store as _store
            from ..config import settings as _settings

            nas_root = str(_settings.nas_root)
            # Check already mounted
            with open("/proc/mounts") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].rstrip("/") == nas_root.rstrip("/"):
                        await update.message.reply_text(
                            f"✅ <b>Storage is already mounted.</b>\n<code>{parts[0]}</code>",
                            parse_mode="HTML",
                        )
                        return

            candidate = await _scan_for_unmounted_nas_drive()
            if not candidate:
                await update.message.reply_text(
                    "❌ <b>No NAS drive found.</b>\n\n"
                    "<i>Make sure the USB/NVMe drive is plugged in, then try again.</i>",
                    parse_mode="HTML",
                )
                return

            ok = await _do_mount_device(candidate)
            if ok:
                await update.message.reply_text(
                    f"✅ <b>Storage remounted.</b>\n<code>{candidate}</code>",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Remount failed for</b> <code>{candidate}</code>.\n"
                    "<i>Check that the drive is not corrupted.</i>",
                    parse_mode="HTML",
                )
        except Exception as inner_exc:
            logger.error("telegram_mount_error: %s / %s", exc, inner_exc)
            await update.message.reply_text(
                "❌ <b>Remount error.</b> Check the service logs.",
                parse_mode="HTML",
            )


async def _handle_storage_cmd(update, context) -> None:  # type: ignore[type-arg]
    """Show storage usage for the NAS root."""
    if not await _check_allowed_and_rate(update):
        return
    try:
        usage    = shutil.disk_usage(settings.nas_root)
        pct      = int(usage.used / usage.total * 100) if usage.total else 0
        used_gb  = usage.used  / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        free_gb  = usage.free  / (1024 ** 3)
        bar      = _storage_bar(pct)
        await update.message.reply_text(
            f"💾 <b>Storage</b>\n\n"
            f"{bar} {pct}%\n"
            f"Used:  <b>{used_gb:.1f} GB</b>\n"
            f"Free:  <b>{free_gb:.1f} GB</b>\n"
            f"Total: <b>{total_gb:.1f} GB</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Failed to read storage: {exc}", parse_mode="HTML")


