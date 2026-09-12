"""
Authorization regression tests for the Telegram duplicate-photo-review feature
(app/telegram/search_handlers.py).

Background: the duplicate scanner builds `duplicate_scan_results` / `similar_scan_results` by
walking EVERY family member's personal/<name>/ folder -- this is household-wide, cross-member
data. Its sibling actions (Auto-clean, Scan Now, and the delete callbacks) were already
correctly gated behind `_is_admin_chat`, but a 2026-09 audit found several callback handlers on
this SAME data with no such gate: a non-admin, linked family-member chat could enumerate another
member's duplicate-photo sets, or -- worst case -- have the bot push the actual file to them via
`_handle_dupsimboth_callback`.

Fixed handlers covered here:
  - `_dup_summary_markup` -- Review Exact/Review Similar buttons are now only shown to admins,
    so a non-admin doesn't even see a button that leads to a callback that will reject them.
  - `_handle_dupexact_callback` -- viewing an exact-duplicate set (filenames, owners, paths).
  - `_handle_dupexactkeep_callback` -- was missing the check its sibling
    `_handle_dupexactdel_callback` already had.
  - `_handle_dupsim_callback` -- viewing a similar-image set (filenames, owners, paths).
  - `_handle_dupsimboth_callback` -- the worst one: sent the actual file to the chat with ZERO
    auth check pre-fix.
  - `_handle_dupsimkeepboth_callback` -- found during this fix's own sweep of the file (not one
    of the originally-reported handlers): had no check, and its response also renders the next
    queued set's owner/path via `_sim_set_text`.

Each class proves BOTH halves of the invariant: a non-admin chat is rejected (and causes no
side effect / no data leak), and a genuinely-admin chat still gets the normal behavior --
mirroring the existing pattern already used for `_handle_dupexactdel_callback` and friends.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_callback_update(data: str, chat_id: int = 555) -> MagicMock:
    """Build a minimal mock telegram Update carrying a callback_query, mirroring
    tests/test_telegram_bot.py's _make_update() helper for message-based updates."""
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.chat.id = chat_id

    update = MagicMock()
    update.callback_query = query
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot.send_document = AsyncMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _exact_entry(owner: str = "bob", filename: str = "passport.pdf") -> dict:
    return {
        "hash": "a" * 64,
        "filename": filename,
        "sizeBytes": 1234,
        "copies": [
            {"path": f"/srv/nas/personal/{owner}/Docs/{filename}", "owner": owner},
            {"path": f"/srv/nas/shared/Docs/{filename}", "owner": owner},
        ],
    }


def _similar_entry(tmp_path, owner: str = "bob") -> dict:
    big = tmp_path / f"{owner}_original.jpg"
    small = tmp_path / f"{owner}_compressed.jpg"
    big.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 100)
    small.write_bytes(b"\xff\xd8\xff\xe0" + b"y" * 20)
    return {
        "copies": [
            {
                "path": str(big), "owner": owner, "filename": big.name,
                "width": 3000, "height": 2000, "size_bytes": 104,
                "phash_hex": "abc123",
            },
            {
                "path": str(small), "owner": owner, "filename": small.name,
                "width": 800, "height": 600, "size_bytes": 24,
                "phash_hex": "abc124",
            },
        ],
    }


def _patch_is_admin(is_admin: bool):
    return patch(
        "app.telegram.search_handlers._is_admin_chat",
        new=AsyncMock(return_value=is_admin),
    )


# ---------------------------------------------------------------------------
# _dup_summary_markup -- button visibility
# ---------------------------------------------------------------------------

class TestDupSummaryMarkupAdminGating:
    def test_non_admin_sees_no_review_buttons(self):
        from app.telegram.search_handlers import _dup_summary_markup
        markup = _dup_summary_markup(exact=[_exact_entry()], similar=[{"copies": []}], is_admin=False)
        assert markup is None, (
            "a non-admin chat must not even see Review Exact/Review Similar buttons -- "
            "they lead to callbacks that reject non-admins anyway"
        )

    def test_admin_sees_review_buttons(self):
        from app.telegram.search_handlers import _dup_summary_markup
        markup = _dup_summary_markup(exact=[_exact_entry()], similar=[{"copies": []}], is_admin=True)
        assert markup is not None
        callback_datas = {
            btn.callback_data for row in markup.inline_keyboard for btn in row
        }
        assert "dupexact:0" in callback_datas
        assert "dupsim:0" in callback_datas


# ---------------------------------------------------------------------------
# _handle_dupexact_callback -- viewing an exact-duplicate set
# ---------------------------------------------------------------------------

class TestDupExactCallbackAuthz:
    async def test_non_admin_cannot_view_exact_duplicate_set(self):
        from app.telegram.search_handlers import _handle_dupexact_callback
        update = _make_callback_update("dupexact:0")
        with _patch_is_admin(False), \
             patch("app.telegram.search_handlers._store.get_value", new=AsyncMock(return_value=[_exact_entry()])) as mock_get:
            await _handle_dupexact_callback(update, _make_context())

        msg = update.callback_query.edit_message_text.call_args[0][0]
        assert "admin" in msg.lower()
        # Never even reads the household-wide data for a rejected caller.
        mock_get.assert_not_called()

    async def test_admin_can_view_exact_duplicate_set(self):
        from app.telegram.search_handlers import _handle_dupexact_callback
        entry = _exact_entry(owner="bob", filename="passport.pdf")
        update = _make_callback_update("dupexact:0")
        with _patch_is_admin(True), \
             patch("app.telegram.search_handlers._store.get_value", new=AsyncMock(return_value=[entry])):
            await _handle_dupexact_callback(update, _make_context())

        msg = update.callback_query.edit_message_text.call_args[0][0]
        assert "passport.pdf" in msg
        assert "bob" in msg


# ---------------------------------------------------------------------------
# _handle_dupexactkeep_callback -- was missing the check its sibling
# _handle_dupexactdel_callback already had
# ---------------------------------------------------------------------------

class TestDupExactKeepCallbackAuthz:
    async def test_non_admin_cannot_whitelist_an_exact_duplicate_set(self):
        from app.telegram.search_handlers import _handle_dupexactkeep_callback
        entry = _exact_entry()
        update = _make_callback_update(f"dupexactkeep:{entry['hash'][:16]}")
        with _patch_is_admin(False), \
             patch("app.telegram.search_handlers._store.get_value", new=AsyncMock(return_value=[entry])) as mock_get, \
             patch("app.telegram.search_handlers._store.atomic_update", new=AsyncMock()) as mock_atomic, \
             patch("app.telegram.search_handlers._store.set_value", new=AsyncMock()) as mock_set:
            await _handle_dupexactkeep_callback(update, _make_context())

        msg = update.callback_query.edit_message_text.call_args[0][0]
        assert "admin" in msg.lower()
        mock_get.assert_not_called()
        mock_atomic.assert_not_called()
        mock_set.assert_not_called()

    async def test_admin_can_whitelist_an_exact_duplicate_set(self):
        from app.telegram.search_handlers import _handle_dupexactkeep_callback
        entry = _exact_entry()
        update = _make_callback_update(f"dupexactkeep:{entry['hash'][:16]}")

        state = {"duplicate_scan_results": [entry], "duplicate_exact_whitelist": []}

        async def fake_get_value(key, default=None):
            return state.get(key, default)

        async def fake_set_value(key, value):
            state[key] = value

        async def fake_atomic_update(key, fn, default=None):
            state[key] = fn(state.get(key, default))
            return state[key]

        with _patch_is_admin(True), \
             patch("app.telegram.search_handlers._store.get_value", new=fake_get_value), \
             patch("app.telegram.search_handlers._store.set_value", new=fake_set_value), \
             patch("app.telegram.search_handlers._store.atomic_update", new=fake_atomic_update):
            await _handle_dupexactkeep_callback(update, _make_context())

        assert entry["hash"] in state["duplicate_exact_whitelist"]
        assert state["duplicate_scan_results"] == []


# ---------------------------------------------------------------------------
# _handle_dupsim_callback -- viewing a similar-image set
# ---------------------------------------------------------------------------

class TestDupSimCallbackAuthz:
    async def test_non_admin_cannot_view_similar_image_set(self, tmp_path):
        from app.telegram.search_handlers import _handle_dupsim_callback
        entry = _similar_entry(tmp_path)
        update = _make_callback_update("dupsim:0")
        with _patch_is_admin(False), \
             patch("app.telegram.search_handlers._store.get_value", new=AsyncMock(return_value=[entry])) as mock_get:
            await _handle_dupsim_callback(update, _make_context())

        msg = update.callback_query.edit_message_text.call_args[0][0]
        assert "admin" in msg.lower()
        mock_get.assert_not_called()

    async def test_admin_can_view_similar_image_set(self, tmp_path):
        from app.telegram.search_handlers import _handle_dupsim_callback
        entry = _similar_entry(tmp_path, owner="bob")
        update = _make_callback_update("dupsim:0")
        with _patch_is_admin(True), \
             patch("app.telegram.search_handlers._store.get_value", new=AsyncMock(return_value=[entry])):
            await _handle_dupsim_callback(update, _make_context())

        msg = update.callback_query.edit_message_text.call_args[0][0]
        assert "bob" in msg


# ---------------------------------------------------------------------------
# _handle_dupsimboth_callback -- THE critical one: sends the actual file to the chat
# ---------------------------------------------------------------------------

class TestDupSimBothCallbackAuthz:
    """Pre-fix, this handler had ZERO auth check and directly open()'d + send_document'd
    another family member's private photo to whichever linked chat sent the callback."""

    async def test_non_admin_cannot_receive_another_members_private_photo(self, tmp_path):
        from app.telegram.search_handlers import _handle_dupsimboth_callback
        entry = _similar_entry(tmp_path, owner="bob")
        update = _make_callback_update("dupsimboth:0", chat_id=999)  # a non-admin member's chat
        ctx = _make_context()

        with _patch_is_admin(False), \
             patch("app.telegram.search_handlers._store.get_value", new=AsyncMock(return_value=[entry])) as mock_get:
            await _handle_dupsimboth_callback(update, ctx)

        msg = update.callback_query.edit_message_text.call_args[0][0]
        assert "admin" in msg.lower()
        # The actual proof: the file must never be transmitted to the rejected caller.
        ctx.bot.send_document.assert_not_called()
        mock_get.assert_not_called()

    async def test_admin_can_receive_the_files(self, tmp_path):
        from app.telegram.search_handlers import _handle_dupsimboth_callback
        entry = _similar_entry(tmp_path, owner="bob")
        update = _make_callback_update("dupsimboth:0", chat_id=1)  # the admin's own chat
        ctx = _make_context()

        with _patch_is_admin(True), \
             patch("app.telegram.search_handlers._store.get_value", new=AsyncMock(return_value=[entry])):
            await _handle_dupsimboth_callback(update, ctx)

        assert ctx.bot.send_document.call_count == 2
        sent_filenames = {
            call.kwargs["filename"] for call in ctx.bot.send_document.call_args_list
        }
        assert sent_filenames == {"bob_original.jpg", "bob_compressed.jpg"}


# ---------------------------------------------------------------------------
# _handle_dupsimkeepboth_callback -- found beyond the originally-reported handlers
# ---------------------------------------------------------------------------

class TestDupSimKeepBothCallbackAuthz:
    async def test_non_admin_cannot_whitelist_a_similar_image_set(self, tmp_path):
        from app.telegram.search_handlers import _handle_dupsimkeepboth_callback
        entry = _similar_entry(tmp_path, owner="bob")
        update = _make_callback_update("dupsimkeepboth:0")

        with _patch_is_admin(False), \
             patch("app.telegram.search_handlers._store.get_value", new=AsyncMock(return_value=[entry])) as mock_get, \
             patch("app.telegram.search_handlers._store.atomic_update", new=AsyncMock()) as mock_atomic, \
             patch("app.telegram.search_handlers._store.set_value", new=AsyncMock()) as mock_set:
            await _handle_dupsimkeepboth_callback(update, _make_context())

        msg = update.callback_query.edit_message_text.call_args[0][0]
        assert "admin" in msg.lower()
        mock_get.assert_not_called()
        mock_atomic.assert_not_called()
        mock_set.assert_not_called()

    async def test_admin_can_whitelist_a_similar_image_set(self, tmp_path):
        from app.telegram.search_handlers import _handle_dupsimkeepboth_callback
        entry = _similar_entry(tmp_path, owner="bob")
        update = _make_callback_update("dupsimkeepboth:0")

        state = {"similar_scan_results": [entry], "similar_phash_whitelist": []}

        async def fake_get_value(key, default=None):
            return state.get(key, default)

        async def fake_set_value(key, value):
            state[key] = value

        async def fake_atomic_update(key, fn, default=None):
            state[key] = fn(state.get(key, default))
            return state[key]

        with _patch_is_admin(True), \
             patch("app.telegram.search_handlers._store.get_value", new=fake_get_value), \
             patch("app.telegram.search_handlers._store.set_value", new=fake_set_value), \
             patch("app.telegram.search_handlers._store.atomic_update", new=fake_atomic_update):
            await _handle_dupsimkeepboth_callback(update, _make_context())

        assert state["similar_scan_results"] == []
        assert ["abc123", "abc124"] in state["similar_phash_whitelist"]
