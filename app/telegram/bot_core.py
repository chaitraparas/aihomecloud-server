"""
Telegram bot for document retrieval from AiHomeCloud.

Bot commands:
  /start — welcome + prompt to /auth if not linked
  /auth  — link Telegram account to AiHomeCloud
  /list  — last 10 indexed documents
  /help  — show all commands
  <text> — full-text search; 0 results → message; 1 → send file; 2-5 → numbered list
  <num>  — send the nth file from the last search

Security: users must send /auth to link their Telegram account before accessing
any data. Linked chat IDs are persisted in the KV store.

The bot is entirely optional — it is only started when AHC_TELEGRAM_BOT_TOKEN is
configured.  If python-telegram-bot is not installed the startup silently skips.
"""

import asyncio
from contextlib import suppress
from datetime import datetime
import hashlib
import html
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Optional

logger = logging.getLogger("aihomecloud.telegram_bot")

import mimetypes

from .. import store as _store
from .. import document_index as _docidx
from .. import media_index
from ..file_sorter import _sort_file, _unique_dest, _destination_folder, _date_bucket_dir
from ..config import settings
from ..ingest import Scope, _dedup_lookup, _apply_capture_time, _extract_video_duration, _resolve_capture_epoch

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None  # type: ignore[assignment,misc]
    InlineKeyboardMarkup = None  # type: ignore[assignment,misc]


_POLL_TIMEOUT_SECONDS = 2
_STOP_TIMEOUT_SECONDS = 5
_UPLOAD_PROGRESS_INTERVAL_SECONDS = 15

# Per-chat last-search results {chat_id: [{"path": ..., "filename": ..., ...}]}
_last_results: dict[int, list[dict]] = {}


@dataclass
class PendingUpload:
    file_id: str
    filename: str
    kind: str
    file_size: int = 0
    caption: str = ""
    created_at: float = 0.0  # time.monotonic() timestamp

    def __post_init__(self) -> None:
        if self.created_at == 0.0:
            import time as _time
            self.created_at = _time.monotonic()


_PENDING_MAX_ENTRIES = 100
_PENDING_TTL_SECONDS = 300  # 5 minutes

# Per-chat pending upload selection state.
_pending_uploads: dict[int, PendingUpload] = {}


def _cleanup_pending_uploads() -> None:
    """Remove expired entries and enforce max size."""
    import time as _time
    now = _time.monotonic()
    # Remove expired
    expired = [k for k, v in _pending_uploads.items()
               if now - v.created_at > _PENDING_TTL_SECONDS]
    for k in expired:
        del _pending_uploads[k]
    # Enforce max size — evict oldest
    if len(_pending_uploads) > _PENDING_MAX_ENTRIES:
        by_age = sorted(_pending_uploads.items(), key=lambda x: x[1].created_at)
        for k, _ in by_age[:len(_pending_uploads) - _PENDING_MAX_ENTRIES]:
            del _pending_uploads[k]

# Per-chat duplicate-detection pending state.
_pending_duplicates: dict[int, dict] = {}

# ---------------------------------------------------------------------------
# Per-chat rate limiting — sliding window (30 commands / 60 seconds)
# ---------------------------------------------------------------------------
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_MAX = 30     # max commands per window
_chat_timestamps: dict[int, list[float]] = {}


def _is_rate_limited(chat_id: int) -> bool:
    """Return True if chat_id has exceeded the per-minute command limit."""
    import time as _time
    now = _time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW
    # setdefault returns the same list object every time — mutate in place so
    # concurrent readers always see a consistent view of the same list.
    timestamps = _chat_timestamps.setdefault(chat_id, [])
    timestamps[:] = [t for t in timestamps if t > cutoff]
    if len(timestamps) >= _RATE_LIMIT_MAX:
        return True
    timestamps.append(now)
    return False


class DuplicateFileError(Exception):
    """Raised when an uploaded file matches an existing SHA-256 hash."""

    def __init__(self, sha256: str, existing: dict, temp_path: Path) -> None:
        self.sha256 = sha256
        self.existing = existing
        self.temp_path = temp_path

# Weekly trash-warning scheduler task
_TRASH_WARNING_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB


# ---------------------------------------------------------------------------
# Access control — linked chat IDs persisted in KV store
# ---------------------------------------------------------------------------

async def _get_linked_ids() -> set[int]:
    """Return set of linked Telegram chat IDs from KV store."""
    ids = await _store.get_value("telegram_linked_ids", default=[])
    return {int(i) for i in ids if str(i).lstrip("-").isdigit()}


async def _add_linked_id(chat_id: int) -> None:
    """Persistently link a new chat_id."""
    def _add(ids):
        if chat_id not in ids:
            ids.append(chat_id)
        return ids
    await _store.atomic_update("telegram_linked_ids", _add, default=[])


# ---------------------------------------------------------------------------
# Pending approval helpers (Task 9)
# ---------------------------------------------------------------------------

async def _get_pending_approvals() -> list[dict]:
    """Return the list of pending Telegram auth approval requests."""
    return await _store.get_value("telegram_pending_approvals", default=[])


async def _add_pending_approval(chat_id: int, username: str, first_name: str) -> None:
    """Add a chat_id to the pending-approval list."""
    def _add(items):
        if not any(p["chat_id"] == chat_id for p in items):
            items.append({
                "chat_id": chat_id,
                "username": username,
                "first_name": first_name,
                "requested_at": datetime.now().isoformat(),
            })
        return items
    await _store.atomic_update("telegram_pending_approvals", _add, default=[])


async def _remove_pending_approval(chat_id: int) -> None:
    """Remove a chat_id from the pending-approval list."""
    await _store.atomic_update(
        "telegram_pending_approvals",
        lambda items: [p for p in items if p["chat_id"] != chat_id],
        default=[],
    )


async def _get_admin_chat_ids() -> set[int]:
    """Return Telegram chat IDs whose folder owner is an AHC admin user."""
    users = await _store.get_users()
    admin_names = {
        str(u.get("name", "")).casefold()
        for u in users
        if u.get("is_admin")
    }
    if not admin_names:
        return set()
    mapping = await _store.get_value("telegram_chat_folder_owners", default={})
    result: set[int] = set()
    for str_id, owner_name in mapping.items():
        if str(owner_name).casefold() in admin_names:
            try:
                result.add(int(str_id))
            except ValueError:
                pass
    return result


async def _set_chat_folder_owner(chat_id: int, username: str) -> None:
    """Persist preferred personal-folder owner for a Telegram chat."""

    # atomic_update: this mapping is also what _get_admin_chat_ids reads to decide who is a Telegram
    # admin, so a lost write here does not just misplace a folder preference — it can drop or
    # resurrect an admin binding. Read-modify-write on a shared key is not safe under concurrency.
    await _store.atomic_update(
        "telegram_chat_folder_owners",
        lambda mapping: {**mapping, str(chat_id): username},
        default={},
    )


async def _get_chat_folder_owner(chat_id: int) -> Optional[str]:
    """Return preferred personal-folder owner for a Telegram chat if configured."""

    mapping = await _store.get_value("telegram_chat_folder_owners", default={})
    value = mapping.get(str(chat_id))
    return value if isinstance(value, str) and value.strip() else None


async def _resolve_personal_owner(chat_id: int, preferred_name: str = "") -> Optional[str]:
    """
    Which member's personal folder this chat may use, or None when that cannot be established.

    Returns None rather than guessing. It used to fall back to the **admin** user when nothing
    matched, and that was a full privilege escalation on the default path, not an edge case
    (2026-08-08 audit, C-7):

      * `preferred_name` is the requester's own Telegram display name — they choose it freely.
      * Most real display names do not match an AiHomeCloud member exactly, so the fallback fired
        for almost every genuine pairing.
      * The chat was then bound to the admin's identity, which meant reading the ADMIN's personal
        folder...
      * ...and `_get_admin_chat_ids` derives admin-ness from exactly this binding, so the chat also
        became a Telegram **admin** — able to run /approve and /deny.

    So approving one family member's pairing request handed them the admin's private files and the
    power to approve everyone else. Two of the tests covering this asserted the fallback as
    intended behaviour.

    Auto-resolution now only ever matches a NON-admin member, and only on an exact name match.
    Binding a chat to an admin identity is deliberately not reachable from here — it requires an
    authenticated admin acting through the API (see approve_telegram_request), where the caller has
    actually proved who they are with a PIN rather than by typing a name into Telegram.
    """
    explicit = await _get_chat_folder_owner(chat_id)
    if explicit:
        return explicit

    wanted = preferred_name.strip().casefold()
    if not wanted:
        return None

    for user in await _store.get_users():
        # Admins are excluded on purpose: a display name is not proof of identity, and this is the
        # path an unauthenticated stranger reaches. An exact match on a non-admin member is the
        # most this can safely conclude.
        if user.get("is_admin"):
            continue
        if str(user.get("name", "")).casefold() == wanted:
            return str(user["name"])
    return None


async def _member_exists(name: str, *, allow_admin: bool = False) -> bool:
    """Whether [name] is a real member, used to validate an explicit binding before storing it."""
    wanted = name.strip().casefold()
    if not wanted:
        return False
    for user in await _store.get_users():
        if user.get("is_admin") and not allow_admin:
            continue
        if str(user.get("name", "")).casefold() == wanted:
            return True
    return False


def _sanitize_filename(name: str, default_stem: str = "telegram_file") -> str:
    """Return a filesystem-safe file name."""
    candidate = Path(name or default_stem).name.strip()
    if not candidate or candidate in (".", ".."):
        candidate = default_stem
    return candidate


def _esc(text: str) -> str:
    """Escape text for interpolation into a parse_mode="HTML" Telegram message.

    M-3 fix (security audit 2026-08): Telegram profile fields (first_name, username), any
    text a user types (e.g. `/auth <name>`), and uploaded filenames are all fully
    attacker-controlled and were being interpolated into HTML-parsed messages unescaped —
    including messages shown to an *admin* or broadcast to every linked family member, not
    just the requester's own chat. A crafted `<a href=...>` in a display name breaks out of
    the surrounding tag and renders as real markup, not literal text — apply this at every
    site where such a field crosses into someone else's message, not the requester's own
    self-directed replies (self-injection into your own client is not a security boundary)."""
    return html.escape(str(text))


def _human_size(num_bytes: int) -> str:
    """Return a compact human-readable byte size string."""
    if num_bytes <= 0:
        return "unknown size"
    mb = num_bytes / (1024 * 1024)
    if mb < 1024:
        return f"{mb:.1f} MB"
    gb = mb / 1024
    return f"{gb:.2f} GB"


def _is_too_large_telegram_file_error(exc: Exception) -> bool:
    return "file is too big" in str(exc).casefold()


def _is_timeout_error(exc: Exception) -> bool:
    return "timed out" in str(exc).casefold() or "timeout" in str(exc).casefold()


def _format_elapsed(total_seconds: float) -> str:
    """Return elapsed duration as h m s."""
    seconds = max(0, int(total_seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_avg_speed(num_bytes: int, elapsed_seconds: float) -> str:
    """Return average speed text (e.g. 3.2 MB/s)."""
    if num_bytes <= 0 or elapsed_seconds <= 0:
        return "n/a"
    return f"{_human_size(int(num_bytes / elapsed_seconds))}/s"


async def _safe_edit_text(message, text: str, parse_mode: str = "HTML") -> None:
    """Best-effort edit for status messages (ignore edit failures)."""
    if message is None:
        return
    try:
        await message.edit_text(text, parse_mode=parse_mode)
    except Exception:
        return


async def _upload_progress_heartbeat(message, filename: str, size_text: str, target_label: str, started_at: float) -> None:
    """Periodically update the same Telegram message while download is in progress."""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(_UPLOAD_PROGRESS_INTERVAL_SECONDS)
        elapsed = _format_elapsed(loop.time() - started_at)
        await _safe_edit_text(
            message,
            (
                f"📥 <b>Downloading…</b>\n\n"
                f"📄 <code>{filename}</code>\n"
                f"📦 {size_text}\n"
                f"📂 {target_label}\n"
                f"⏱ {elapsed}\n\n"
                "<i>Large files can take a few minutes.</i>"
            ),
        )


class DownloadStallError(Exception):
    """Raised when a Telegram file download shows no byte progress for the stall window."""


class InsufficientDiskSpaceError(Exception):
    """Raised when the destination filesystem doesn't have enough free space for the
    incoming file, checked before the transfer starts rather than discovered mid-download."""

    def __init__(self, needed: int, free: int):
        self.needed = needed
        self.free = free
        super().__init__(f"Need {needed} bytes, only {free} free")


_DOWNLOAD_STALL_POLL_INTERVAL_SECONDS = 5
# Required headroom beyond the file's own size — 2GB mode files can be large enough that
# "just barely fits" still risks starving concurrent writes (thumbnail cache, WAL files,
# other in-flight uploads) of the little space left.
_DISK_SPACE_MARGIN_BYTES = 200 * 1024 * 1024


async def _download_to_path(bot, file_id: str, dest_path: Path) -> Path:
    """Download a Telegram file to *dest_path* via a `.uploading` temp + atomic rename,
    with a watchdog that aborts the download if its on-disk size stops growing.

    This is the fix for a real incident: python-telegram-bot's configured connect/read/
    write/pool timeouts only bound each individual socket operation, which does NOT catch
    a connection that trickles bytes slowly enough to keep resetting the per-op timer —
    a download can hang with zero actual progress well past its own configured timeout,
    with no self-recovery (previously only diagnosable by manually sampling the process's
    /proc/<pid>/io byte counters). This polls the temp file's actual size on disk instead —
    the same signal, built in, applied to every download automatically.

    Also checks free disk space against the file's reported size before starting — found
    live 2026-07-15 auditing this path for large (up to 2GB, "2GB mode") movie/anime
    downloads: without this, a multi-GB transfer could run for minutes over a slow mobile
    connection only to fail right at the very end once the disk actually filled, instead of
    being rejected immediately with a clear reason.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_name(dest_path.name + ".uploading")
    temp_path.unlink(missing_ok=True)

    telegram_file = await bot.get_file(file_id)
    file_size = getattr(telegram_file, "file_size", None) or 0
    if file_size:
        free = shutil.disk_usage(dest_path.parent).free
        if file_size + _DISK_SPACE_MARGIN_BYTES > free:
            raise InsufficientDiskSpaceError(needed=file_size, free=free)

    download_task = asyncio.ensure_future(
        telegram_file.download_to_drive(custom_path=str(temp_path))
    )

    last_size = 0
    stalled_for = 0
    try:
        while True:
            done, _pending = await asyncio.wait(
                {download_task}, timeout=_DOWNLOAD_STALL_POLL_INTERVAL_SECONDS,
            )
            if download_task in done:
                await download_task  # propagate any download exception
                break
            current_size = temp_path.stat().st_size if temp_path.exists() else 0
            if current_size > last_size:
                last_size = current_size
                stalled_for = 0
            else:
                stalled_for += _DOWNLOAD_STALL_POLL_INTERVAL_SECONDS
                if stalled_for >= settings.telegram_stall_timeout:
                    download_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await download_task
                    raise DownloadStallError(
                        f"Download stall timeout — no progress for {stalled_for}s "
                        f"(stuck at {current_size} bytes)"
                    )
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    temp_path.rename(dest_path)
    return dest_path


# ---------------------------------------------------------------------------
# Duplicate-detection helpers (Task 10)
# ---------------------------------------------------------------------------

def _compute_sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _nas_rel_path(path: Path) -> str:
    """Convert an absolute filesystem path to the NAS-relative form ingest.py's
    dedup index stores paths in (e.g. "/family/Photos/foo.jpg")."""
    return "/" + str(path.resolve().relative_to(settings.nas_root.resolve())).replace("\\", "/")


# Telegram previously kept its own separate SHA-256 dedup index ("telegram_file_hashes"),
# meaning a file sent via Telegram and the same file arriving via direct upload or Sync
# were never recognized as duplicates of each other. Now uses ingest.py's shared,
# scope-keyed lookup (_dedup_lookup, backed by media_index) directly — recording happens
# via media_index.record_entry, the single canonical hash record. See
# _store_private_or_shared_file and _store_entertainment_file below.


async def _record_recent_file(chat_id: int, filename: str, path: str, size: int) -> None:
    """Prepend an entry to the recent-files list (max 5 entries)."""
    def _add(recent):
        recent.insert(0, {
            "filename": filename,
            "path": path,
            "size": size,
            "chat_id": chat_id,
            "saved_at": datetime.now().isoformat(),
        })
        return recent[:5]
    await _store.atomic_update("telegram_recent_files", _add, default=[])


async def _store_private_or_shared_file(
    bot, pending: PendingUpload, base_dir: Path, added_by: str, scope: Scope,
) -> Path:
    """Download to .inbox, sort immediately, and index if it lands in Documents/."""

    inbox_dir = base_dir / ".inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    temp_name = _sanitize_filename(pending.filename)
    # _unique_dest (not a raw inbox/temp_name write) — otherwise a second same-named
    # Telegram upload landing before the sort worker picks up the first would silently
    # overwrite it. Same bug class as telegram_upload_routes.py's _sort_uploaded_file
    # (fixed 2026-07-15), found here auditing the sibling direct-chat-upload path while
    # hardening this pipeline for large movie/anime downloads — _store_entertainment_file
    # already guards this correctly via _unique_dest at its own final-placement step.
    temp_path = _unique_dest(inbox_dir, temp_name)
    try:
        await _download_to_path(bot, pending.file_id, temp_path)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
                logger.warning("download_cleanup removed partial file temp=%s", temp_path)
            except OSError:
                pass
        raise

    # Duplicate detection against the shared, scope-keyed dedup lookup (same one direct
    # uploads and Sync use) rather than a Telegram-only index — see module note above.
    owner = added_by if scope == Scope.PERSONAL else None
    file_sha = _compute_sha256(temp_path)
    existing = await _dedup_lookup(scope, owner, file_sha)
    if existing:
        raise DuplicateFileError(sha256=file_sha, existing=existing, temp_path=temp_path)

    # Correct the mtime to the real capture time BEFORE sorting — _sort_file's
    # date-bucket allocation keys on capture time (with mtime as fallback), so
    # the correction has to land first, not after the file is already placed.
    capture_epoch = _apply_capture_time(temp_path)

    dest = _sort_file(temp_path, base_dir, check_age=False)
    if dest is None:
        raise RuntimeError("Failed to sort downloaded file")

    # Date-bucketing means dest's direct parent is a month dir, not the
    # category dir — derive the category from the path relative to base_dir.
    category = dest.relative_to(base_dir).parts[0]
    if category == "Documents":
        await _docidx.index_document(str(dest), dest.name, added_by)

    media_type, _ = mimetypes.guess_type(dest.name)
    await media_index.record_entry(
        rel_path=_nas_rel_path(dest),
        content_hash=file_sha,
        size_bytes=dest.stat().st_size,
        scope=scope.value,
        owner=owner,
        category=category,
        media_type=media_type,
        source="telegram",
        filename=dest.name,
        original_name=pending.filename,
        capture_date=capture_epoch,
        mtime=dest.stat().st_mtime,
        duration=_extract_video_duration(dest),
    )
    return dest


def _place_entertainment_file(temp_path: Path, safe_name: str) -> tuple[Path, str, float]:
    """Move a fully-downloaded temp file into its final allocator-assigned
    entertainment location: <category>/<YYYY>/<MM[-bN]>/<name> — the same
    date-bucket allocation every other write path uses (structural ≤500
    files/dir), replacing the old flat write into <category>/. Shared by the
    normal flow below and upload_handlers._handle_keep (duplicate override).
    Returns (dest_path, category_folder, capture_epoch)."""
    entertainment_dir = settings.entertainment_path
    folder_name = _destination_folder(Path(safe_name), base_dir=entertainment_dir)
    category_dir = entertainment_dir / folder_name
    capture_epoch = _resolve_capture_epoch(temp_path, safe_name)
    final_dir = _date_bucket_dir(category_dir, capture_epoch)
    final_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _unique_dest(final_dir, safe_name)
    temp_path.rename(dest_path)
    try:
        os.utime(dest_path, (capture_epoch, capture_epoch))
    except OSError:
        pass
    return dest_path, folder_name, capture_epoch


async def _store_entertainment_file(bot, pending: PendingUpload) -> Path:
    """Download file into the shared Entertainment folder, sorted into its Movies/Series/
    Music sub-folder by extension — the same categorizer (_destination_folder) used
    everywhere else — and date-bucketed through the same allocator as every other
    write path. Downloads to a .uploading temp first so the dedup check and bucket
    allocation both happen before the file appears under its final name."""

    entertainment_dir = settings.entertainment_path
    safe_name = _sanitize_filename(pending.filename, default_stem="telegram_media")
    folder_name = _destination_folder(Path(safe_name), base_dir=entertainment_dir)
    temp_dir = entertainment_dir / folder_name
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / (safe_name + ".uploading")
    try:
        await _download_to_path(bot, pending.file_id, temp_path)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
                logger.warning("download_cleanup removed partial file dest=%s", temp_path)
            except OSError:
                pass
        raise

    file_sha = _compute_sha256(temp_path)
    existing = await _dedup_lookup(Scope.ENTERTAINMENT, None, file_sha)
    if existing:
        # Temp file is kept on disk so /keep can finalize it without a
        # re-download — upload_handlers._handle_keep places it via
        # _place_entertainment_file; /skip unlinks it.
        raise DuplicateFileError(sha256=file_sha, existing=existing, temp_path=temp_path)

    dest_path, category, capture_epoch = _place_entertainment_file(temp_path, safe_name)

    media_type, _ = mimetypes.guess_type(dest_path.name)
    await media_index.record_entry(
        rel_path=_nas_rel_path(dest_path),
        content_hash=file_sha,
        size_bytes=dest_path.stat().st_size,
        scope=Scope.ENTERTAINMENT.value,
        owner=None,
        category=category,
        media_type=media_type,
        source="telegram",
        filename=dest_path.name,
        original_name=pending.filename,
        capture_date=capture_epoch,
        mtime=dest_path.stat().st_mtime,
        duration=_extract_video_duration(dest_path),
    )
    return dest_path


def _file_type_emoji(kind: str) -> str:
    """Return an emoji for a given file kind."""
    return {
        "document": "📄",
        "video":    "🎬",
        "audio":    "🎵",
        "photo":    "🖼",
        "voice":    "🎙",
    }.get(kind, "📁")


def _make_destination_keyboard(chat_id: int):
    """Return a 4-button inline keyboard for upload destination choice."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤  My Folder",        callback_data=f"dest:{chat_id}:1")],
        [InlineKeyboardButton("👨\u200d👩\u200d👧  Family Shared",   callback_data=f"dest:{chat_id}:2")],
        [InlineKeyboardButton("🎬  Entertainment",   callback_data=f"dest:{chat_id}:3")],
        [InlineKeyboardButton("❌  Cancel",            callback_data=f"dest:{chat_id}:cancel")],
    ])

# ---------------------------------------------------------------------------
# Module-proxy: resolve the shim module at call-time for test patching.
# ---------------------------------------------------------------------------

import sys as _sys

def _tb():
    """Return app.telegram_bot so test monkey-patches propagate."""
    return _sys.modules["app.telegram_bot"]




async def _is_allowed(chat_id: int) -> bool:
    """Return True if chat_id has linked their account via /auth."""
    linked = await _get_linked_ids()
    return chat_id in linked





async def _check_allowed_and_rate(update) -> bool:
    """Return True if the user is linked AND not rate-limited.

    Sends an appropriate reply if either check fails.
    """
    chat_id = update.effective_chat.id
    if not await _tb()._is_allowed(chat_id):
        await update.message.reply_text(
            "\U0001f512 Send /auth first to link your account.",
            parse_mode="HTML",
        )
        return False
    if _is_rate_limited(chat_id):
        await update.message.reply_text(
            "\u23f3 Please wait a moment \u2014 too many requests.",
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Command + Message handlers
# ---------------------------------------------------------------------------




def _storage_bar(percent: float, width: int = 10) -> str:
    """Return a text progress bar for storage, e.g. \u2593\u2593\u2593\u2593\u2593\u2591\u2591\u2591\u2591\u2591 50%."""
    filled = round(percent / 100 * width)
    bar = "\u2593" * filled + "\u2591" * (width - filled)
    return bar





# ---------------------------------------------------------------------------
# Trash warning scheduler
# ---------------------------------------------------------------------------


async def _trash_warning_loop() -> None:
    """Hourly loop — sends a Telegram notification on Saturday at 10 AM when total
    trash exceeds 10 GB.  Fires at most once per ISO week via the KV store."""

    while True:
        try:
            await asyncio.sleep(3600)

            now = datetime.now()

            # ── 85 % storage check (daily at hour 9) ────────────────────
            if now.hour == 9:
                storage_warn_day = f"{now.date()}"
                if await _store.get_value("storage_warn_day", default="") != storage_warn_day:
                    try:
                        usage = shutil.disk_usage(settings.nas_root)
                        pct   = usage.used / usage.total * 100 if usage.total else 0
                        if pct >= 85:
                            linked_ids = await _get_linked_ids()
                            if linked_ids and _tb()._application is not None:
                                used_gb  = usage.used  / (1024 ** 3)
                                total_gb = usage.total / (1024 ** 3)
                                bar      = _storage_bar(int(pct))
                                smsg = (
                                    f"💾 <b>Storage almost full</b>\n\n"
                                    f"{bar} {pct:.0f}%\n"
                                    f"Used: <b>{used_gb:.1f} GB</b> / {total_gb:.1f} GB\n\n"
                                    "Free up space by emptying the Trash or removing files."
                                )
                                for cid in linked_ids:
                                    with suppress(Exception):
                                        await _tb()._application.bot.send_message(
                                            chat_id=cid, text=smsg, parse_mode="HTML"
                                        )
                                await _store.set_value("storage_warn_day", storage_warn_day)
                    except Exception as _se:
                        logger.warning("storage 85%% check error: %s", _se)

            # ── Saturday 10 AM trash check ──────────────────────────────
            if now.weekday() != 5 or now.hour != 10:  # weekday 5 = Saturday
                continue

            iso_week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
            if await _store.get_value("trash_warn_week", default="") == iso_week:
                continue  # already sent this week

            items = await _store.get_trash_items()
            total_bytes: int = sum(i.get("sizeBytes", 0) for i in items)
            if total_bytes < _TRASH_WARNING_BYTES:
                continue

            linked_ids = await _get_linked_ids()
            if not linked_ids or _tb()._application is None:
                continue

            total_gb = total_bytes / (1024 ** 3)
            msg = (
                f"🗑 <b>Trash is getting full</b>\n\n"
                f"Total trash: <b>{total_gb:.1f} GB</b> (threshold: 10 GB)\n\n"
                "Tap <b>Empty Trash</b> to free up space."
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Empty Trash", callback_data="trash:empty"),
            ]])
            for chat_id in linked_ids:
                with suppress(Exception):
                    await _tb()._application.bot.send_message(
                        chat_id=chat_id, text=msg, parse_mode="HTML", reply_markup=kb
                    )

            await _store.set_value("trash_warn_week", iso_week)
            logger.info(
                "Trash warning sent to %d user(s) (%.1f GB)", len(linked_ids), total_gb
            )

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("trash_warning_loop error: %s", exc)


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------

async def _is_admin_chat(chat_id: int) -> bool:
    """Return True if the chat_id belongs to an admin user."""
    return chat_id in await _get_admin_chat_ids()


# ---------------------------------------------------------------------------
# Approval & deny handlers (Task 9)
# ---------------------------------------------------------------------------

