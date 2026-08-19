"""Telegram bot handlers — auth handlers."""

from contextlib import suppress

from .bot_core import (
    _tb,
    _check_allowed_and_rate, _is_admin_chat,
    _pending_uploads, _last_results,
    _add_linked_id,
    _get_pending_approvals, _remove_pending_approval,
    _get_chat_folder_owner, _set_chat_folder_owner,
    _esc,
)
from .. import store as _store

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None  # type: ignore[assignment,misc]
    InlineKeyboardMarkup = None  # type: ignore[assignment,misc]




# ---------------------------------------------------------------------------
# Command + Message handlers
# ---------------------------------------------------------------------------

async def _handle_start(update, context) -> None:  # type: ignore[type-arg]
    chat_id = update.effective_chat.id
    first_name = update.effective_user.first_name or "there"

    if not await _tb()._is_allowed(chat_id):
        await update.message.reply_text(
            f"👋 <b>Hi {first_name}!</b>\n\n"
            "This is a private AiHomeCloud. Send /auth to link your account and get access.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        f"🏠 <b>Welcome back, {first_name}!</b>\n\n"
        "Type anything to search your files.\n"
        "Send a file to save it to your cloud.\n\n"
        "Use /help to see all commands.",
        parse_mode="HTML",
    )




async def _handle_auth(update, context) -> None:  # type: ignore[type-arg]
    chat_id = update.effective_chat.id
    first_name = update.effective_user.first_name or "there"
    requested_owner = ""
    text = (update.message.text or "").strip()
    if " " in text:
        requested_owner = text.split(" ", 1)[1].strip()

    if await _tb()._is_allowed(chat_id):
        owner = await _get_chat_folder_owner(chat_id)
        # Switching your own folder is an admin action, and the old copy here advertised the
        # opposite ("To switch folder: /auth <name>"). It never actually worked — the resolver
        # returns the existing binding first, so the write was a no-op — but telling a member they
        # can rebind themselves is exactly the wrong instruction to publish, and the underlying
        # write would have honoured it the moment that early return changed. (2026-08-08 audit.)
        if requested_owner and not await _is_admin_chat(chat_id):
            await update.message.reply_text(
                f"🔒 <b>Only an admin can change which folder a chat uses.</b>\n\n"
                f"👤 Your personal folder: <b>{owner or 'not set'}</b>",
                parse_mode="HTML",
            )
            return
        if requested_owner and await _tb()._member_exists(requested_owner, allow_admin=True):
            await _set_chat_folder_owner(chat_id, requested_owner)
            owner = requested_owner
        elif requested_owner:
            await update.message.reply_text(
                f"❌ <b>No member named {requested_owner}.</b>", parse_mode="HTML",
            )
            return
        await update.message.reply_text(
            f"✅ <b>Already linked, {first_name}</b>\n\n"
            f"👤 Personal folder: <b>{owner or 'not set — ask an admin to assign one'}</b>",
            parse_mode="HTML",
        )
        return

    username = update.effective_user.username or ""
    await _tb()._add_pending_approval(chat_id, username, first_name)
    admin_ids = await _tb()._get_admin_chat_ids()
    if admin_ids and _tb()._application is not None:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approval:approve:{chat_id}"),
            InlineKeyboardButton("❌ Deny",    callback_data=f"approval:deny:{chat_id}"),
        ]])
        req_text = (
            f"👤 <b>New pairing request</b>\n\n"
            f"Name: <b>{_esc(first_name)}</b>\n"
            f"Username: @{_esc(username)}\n"
            f"Chat ID: <code>{chat_id}</code>\n"
            f"Folder request: <b>{_esc(requested_owner) or '(default)'}</b>"
        )
        for admin_id in admin_ids:
            with suppress(Exception):
                await _tb()._application.bot.send_message(
                    chat_id=admin_id, text=req_text,
                    parse_mode="HTML", reply_markup=kb,
                )
    await update.message.reply_text(
        "⏳ <b>Access request sent.</b>\n\n"
        "An admin will review your request.\n"
        "You'll receive a message here when approved.",
        parse_mode="HTML",
    )



async def _handle_unlink(update, context) -> None:  # type: ignore[type-arg]
    chat_id = update.effective_chat.id
    if not await _check_allowed_and_rate(update):
        return
    # atomic_update, not get-then-set. Revocation is the one operation that must not be lost to a
    # concurrent writer: a plain read-modify-write racing with any other update to this key writes
    # back a list still containing chat_id, and the unlink silently does nothing while reporting
    # success. A revocation that fails quietly is worse than one that errors.
    # (2026-08-08 audit, adversarial pass.)
    await _store.atomic_update(
        "telegram_linked_ids",
        lambda items: [i for i in items if int(i) != chat_id],
        default=[],
    )

    # Without this, an unlinked chat's folder-owner preference stays behind forever — a stale
    # mapping entry referencing a chat that no longer exists in telegram_linked_ids at all.
    await _store.atomic_update(
        "telegram_chat_folder_owners",
        lambda mapping: {k: v for k, v in mapping.items() if k != str(chat_id)},
        default={},
    )

    _pending_uploads.pop(chat_id, None)
    _last_results.pop(chat_id, None)

    await update.message.reply_text(
        "🔓 <b>Account unlinked.</b>\n\n"
        "<i>Your Telegram account has been removed from AiHomeCloud.\n"
        "Send /auth to link again.</i>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Trash warning scheduler
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Approval & deny handlers (Task 9)
# ---------------------------------------------------------------------------

async def _handle_approval_callback(update, context) -> None:  # type: ignore[type-arg]
    """Handle Approve/Deny inline-button presses from admin chat."""
    query = update.callback_query
    await query.answer()

    # The sibling /approve command checks this and the button did not, which is not a difference
    # the two should have: the keyboard is only *sent* to admin chats, but a button is not an
    # authorization. Add the bot to a family group and every member of that group can see and press
    # the admin's Approve button; a demoted admin's old messages keep working too. Authorize the
    # presser, not the message. (2026-08-08 audit.)
    if not await _is_admin_chat(update.effective_chat.id):
        with suppress(Exception):
            await query.edit_message_text("🔒 Admin only.")
        return

    parts = query.data.split(":")
    action         = parts[1]
    target_chat_id = int(parts[2])

    if action == "approve":
        pendings = await _get_pending_approvals()
        info = next((p for p in pendings if p["chat_id"] == target_chat_id), {})
        first_name = info.get("first_name", "User")
        await _add_linked_id(target_chat_id)
        # first_name is the requester's own Telegram display name. It is a hint, never a claim of
        # identity — _resolve_personal_owner matches it only against non-admin members and returns
        # None when it cannot tell, instead of the old silent fall back to the admin account.
        owner = await _tb()._resolve_personal_owner(target_chat_id, first_name)
        if owner:
            await _set_chat_folder_owner(target_chat_id, owner)
        await _remove_pending_approval(target_chat_id)
        if _tb()._application is not None:
            with suppress(Exception):
                await _tb()._application.bot.send_message(
                    chat_id=target_chat_id,
                    text=(
                        f"✅ <b>Access approved! Welcome, {first_name}.</b>\n\n"
                        + (f"👤 Personal folder: <b>{owner}</b>\n\n" if owner else
                           "👤 <i>No personal folder assigned yet — an admin can set one.</i>\n\n") +
                        "• Type anything to <b>search documents</b>\n"
                        "• Send a file to <b>save it to your cloud</b>\n"
                        "• /help — all commands"
                    ),
                    parse_mode="HTML",
                )
        await query.edit_message_text(
            f"✅ Approved <b>{_esc(first_name)}</b> (chat_id: <code>{target_chat_id}</code>)",
            parse_mode="HTML",
        )
    else:
        await _remove_pending_approval(target_chat_id)
        if _tb()._application is not None:
            with suppress(Exception):
                await _tb()._application.bot.send_message(
                    chat_id=target_chat_id,
                    text="❌ <b>Access denied.</b>\n\nContact the device owner to request access.",
                    parse_mode="HTML",
                )
        await query.edit_message_text(
            f"❌ Denied request from chat_id <code>{target_chat_id}</code>",
            parse_mode="HTML",
        )




async def _handle_approve_command(update, context) -> None:  # type: ignore[type-arg]
    """Admin command: /approve <chat_id>"""
    chat_id = update.effective_chat.id
    if not await _is_admin_chat(chat_id):
        await update.message.reply_text("🔒 Admin only.", parse_mode="HTML")
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /approve &lt;chat_id&gt;", parse_mode="HTML")
        return
    target = int(args[0])
    pendings = await _get_pending_approvals()
    info = next((p for p in pendings if p["chat_id"] == target), {})
    first_name = info.get("first_name", "User")
    await _add_linked_id(target)
    owner = await _tb()._resolve_personal_owner(target, first_name)
    if owner:
        await _set_chat_folder_owner(target, owner)
    await _remove_pending_approval(target)
    if _tb()._application is not None:
        with suppress(Exception):
            await _tb()._application.bot.send_message(
                chat_id=target,
                text=f"✅ <b>Access approved! Welcome, {first_name}.</b>\n\n/help to see commands.",
                parse_mode="HTML",
            )
    await update.message.reply_text(f"✅ Approved <code>{target}</code>", parse_mode="HTML")




async def _handle_deny_command(update, context) -> None:  # type: ignore[type-arg]
    """Admin command: /deny <chat_id>"""
    chat_id = update.effective_chat.id
    if not await _is_admin_chat(chat_id):
        await update.message.reply_text("🔒 Admin only.", parse_mode="HTML")
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /deny &lt;chat_id&gt;", parse_mode="HTML")
        return
    target = int(args[0])
    await _remove_pending_approval(target)
    if _tb()._application is not None:
        with suppress(Exception):
            await _tb()._application.bot.send_message(
                chat_id=target,
                text="❌ <b>Access denied.</b>\n\nContact the device owner.",
                parse_mode="HTML",
            )
    await update.message.reply_text(f"❌ Denied <code>{target}</code>", parse_mode="HTML")


# ---------------------------------------------------------------------------
# Duplicate file handlers (Task 10)
# ---------------------------------------------------------------------------

