"""
AiHomeCloud Backend — FastAPI application.
Run with: python -m app.main (auto-configures TLS)
"""

import asyncio
import errno
import logging
import re
import tempfile
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from . import platform_profile, workload
from .config import settings
from .models import HealthResponse, RootResponse
from .limiter import limiter
import os  # noqa: E402 — used for env var check below

# Starlette buffers uploaded files through SpooledTemporaryFile -> tempdir.
# /tmp is a 1.9 GB tmpfs on this device — large files (>1.9 GB) would overflow it
# and produce a misleading "There was an error parsing the body" 422 error.
# Redirect to eMMC /var/tmp which has ~50 GB free.
_AHC_UPLOAD_TMP = "/var/tmp/ahc_uploads"  # nosec B108 — intentional: eMMC path, not world-writable /tmp
os.makedirs(_AHC_UPLOAD_TMP, exist_ok=True)
tempfile.tempdir = _AHC_UPLOAD_TMP
from .logging_config import configure_logging, set_request_id, reset_request_id
from .tls import ensure_tls_cert, is_self_signed
from .upload_guard import UploadSizeLimitMiddleware
from .board import detect_board
from .routes import (
    auth_routes,
    system_routes,
    monitor_routes,
    file_routes,
    trash_routes,
    jobs_routes,
    family_routes,
    service_routes,
    storage_routes,
    event_routes,
    network_routes,
    telegram_routes,
    telegram_upload_routes,
    backup_routes,
    web_upload_routes,
    webapp_routes,
    web_browser_routes,
    media_routes,
    activity_routes,
    local_backup_routes,
    bluetooth_routes,
    events_routes,
)

from datetime import datetime, timedelta

logger = logging.getLogger("aihomecloud.main")

_BOT_BACKOFF_SCHEDULE = [5, 10, 30, 60, 60]  # seconds between restart attempts
_BOT_MAX_RESTARTS = 5


async def _memory_diagnostics_loop() -> None:
    """Periodic self-RSS + cache-size logging. The production target is a 1GB-RAM SBC, and
    this backend was observed climbing to ~930MB RSS + ~930MB swap over about a day of normal
    use on a dev board with a 4x larger RAM budget — this loop exists to find out why, since
    CPython/glibc never return freed memory to the OS, so RSS is a high-water-mark, not a live
    gauge, and needs correlating against actual usage over time to distinguish a real leak from
    transient concurrent-request spikes. Cheap (one psutil call + a dict len every 60s); leave
    running permanently rather than behind a debug flag, since a soft alarm here is the whole
    point once a root cause is confirmed and fixed."""
    import psutil
    process = psutil.Process()
    _RSS_SOFT_ALARM = 300 * 1024 * 1024
    _RSS_HARD_ALARM = 400 * 1024 * 1024
    while True:
        try:
            await asyncio.sleep(60)
            rss = process.memory_info().rss
            available = psutil.virtual_memory().available
            try:
                from .routes.file_routes import _scan_cache
                scan_cache_len = len(_scan_cache)
            except Exception:
                scan_cache_len = -1
            level = "info"
            if rss >= _RSS_HARD_ALARM:
                level = "error"
            elif rss >= _RSS_SOFT_ALARM:
                level = "warning"
            getattr(logger, level)(
                "memory_diagnostics rss_mb=%.1f system_available_mb=%.1f scan_cache_entries=%d",
                rss / (1024 * 1024), available / (1024 * 1024), scan_cache_len,
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("memory_diagnostics_loop error: %s", exc)


async def _serve_http_explainer() -> None:
    """
    Serve the certificate explainer on port 80, if this board is allowed to bind it.

    Entirely optional: a board without CAP_NET_BIND_SERVICE, or with something else already on :80,
    logs one line and carries on. Losing an explanatory page must never stop the NAS from starting,
    and a hard failure here would be a spectacular way to brick a device over a courtesy.
    """
    import uvicorn  # noqa: PLC0415 — already a dependency; imported here to keep startup lean

    from .http_helper import build_app

    config = uvicorn.Config(
        build_app(), host="0.0.0.0", port=80, log_level="warning",  # nosec B104 — LAN appliance
        access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    except asyncio.CancelledError:
        raise
    except SystemExit as exc:
        # uvicorn calls sys.exit(1) when it cannot bind. SystemExit derives from BaseException, so
        # it slipped past `except Exception` and tore down the entire backend — the API, the family's
        # files, everything — because a courtesy page could not have port 80.
        logger.info("http explainer could not bind :80 (exit %s) — https on :%s is unaffected",
                    getattr(exc, "code", "?"), settings.port)
    except OSError as exc:
        logger.info("http explainer not started on :80 (%s) — https on :%s is unaffected",
                    exc, settings.port)
    except BaseException as exc:  # noqa: BLE001 — nothing here may take the service down
        logger.warning("http explainer stopped: %s", exc)


async def _backfill_video_durations() -> None:
    """
    Fill in durations the index never had, then stop for good.

    `duration` arrived as a later column migration, and the reconciler only revisits a file whose
    (size, mtime) signature changed — which for a film sitting untouched on a drive is never. The
    result on the production board was 334 of 334 videos with no duration, so the Watch shelf could
    not show a runtime for anything. A one-off pass at startup is the right shape: the work is
    finite, it converges, and once it converges this loop exits and costs nothing.

    Strictly opportunistic. Each file is an ffprobe subprocess, and on an 8-core A55 board a few
    hundred of those alongside someone uploading a folder is exactly the competition `workload`
    exists to prevent. Gated per batch, not once at the start.
    """
    from . import media_index
    from .ingest import _extract_video_duration

    await asyncio.sleep(30)  # let the service finish coming up before touching the disk
    loop = asyncio.get_running_loop()
    filled = 0
    try:
        while True:
            await workload.gate("video_duration_backfill", max_wait=6 * 60 * 60)
            batch = await media_index.videos_missing_duration(limit=25)
            if not batch:
                if filled:
                    logger.info("video_duration_backfill complete filled=%d", filled)
                return
            for entry_id, rel_path in batch:
                path = settings.nas_root / rel_path.lstrip("/")
                if not path.exists():
                    # Gone from disk; the reconciler owns removing the row, not this job. Record 0
                    # so the file does not reappear in this queue on every restart.
                    await media_index.set_duration(entry_id, 0.0)
                    continue
                duration = await loop.run_in_executor(None, _extract_video_duration, path)
                await media_index.set_duration(entry_id, float(duration or 0.0))
                filled += 1
            await asyncio.sleep(1)  # breathe between batches on a small board
    except asyncio.CancelledError:
        logger.info("video_duration_backfill cancelled filled=%d", filled)
    except Exception as exc:
        logger.error("video_duration_backfill failed after %d: %s", filled, exc)


async def _run_nightly_duplicate_scan() -> None:
    """Sleep until 4:00 AM then run the duplicate scanner (exact + similar), repeat daily."""
    # Nightly by design, but "night" is not a guarantee — wait for actual quiet.
    await workload.gate("nightly_duplicate_scan", max_wait=24 * 60 * 60)
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            from .duplicate_scanner import get_duplicate_scanner
            await get_duplicate_scanner()._scan_nas_for_duplicates()
            logger.info("Nightly duplicate scan complete (exact + similar)")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Nightly duplicate scan failed: %s", exc)


async def _run_nightly_local_backup_sync() -> None:
    """Sleep until 2:00 AM then sync protected folders + (if enabled) the whole media
    library to the local_backup secondary drive, repeat daily. Scheduled well before the
    4:00 AM duplicate scan so the two background jobs don't compete for disk I/O on a
    low-power SBC. Media sync runs second, in the same pass, under the same _sync_lock as
    the protected-folder sync -- never two rsyncs at once."""
    # Nightly by design, but "night" is not a guarantee — wait for actual quiet.
    await workload.gate("nightly_local_backup", max_wait=24 * 60 * 60)
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            from . import local_backup
            result = await local_backup.sync_protected_folders()
            logger.info("Nightly local_backup sync complete: %s", result)
            media_result = await local_backup.sync_media_library()
            logger.info("Nightly media library sync complete: %s", media_result)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Nightly local_backup sync failed: %s", exc)


def _similar_set_key(entry: dict) -> list:
    """Stable identity for a similar-image group -- its members' phashes, order-independent."""
    return sorted(c.get("phash_hex", "") for c in entry.get("copies", []))


async def _send_evening_duplicate_report() -> None:
    """Sleep until 6:00 PM then send the Telegram duplicate report, repeat daily.

    Only reports sets NOT already reported yesterday -- a set left unresolved (e.g. an
    intentional duplicate nobody has whitelisted, or a screenshot pHash false-positive)
    would otherwise repeat verbatim every single evening forever. "Already reported" is
    recomputed from the full current scan each run, so a resolved/whitelisted set drops
    out on its own without needing separate cleanup.
    """
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=18, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            from . import store as _store_mod
            exact = await _store_mod.get_value("duplicate_scan_results", default=[])
            similar = await _store_mod.get_value("similar_scan_results", default=[])
            if not exact and not similar:
                await _store_mod.set_value("duplicate_report_notified_exact", [])
                await _store_mod.set_value("duplicate_report_notified_similar", [])
                continue

            notified_exact = set(
                await _store_mod.get_value("duplicate_report_notified_exact", default=[])
            )
            notified_similar = {
                tuple(k) for k in
                await _store_mod.get_value("duplicate_report_notified_similar", default=[])
            }
            new_exact = [e for e in exact if e.get("hash") not in notified_exact]
            new_similar = [s for s in similar if tuple(_similar_set_key(s)) not in notified_similar]

            # Remember everything currently open (not just what's new) so tomorrow's
            # diff is accurate, and so resolved sets naturally age out of "notified".
            await _store_mod.set_value(
                "duplicate_report_notified_exact", [e.get("hash") for e in exact]
            )
            await _store_mod.set_value(
                "duplicate_report_notified_similar", [_similar_set_key(s) for s in similar]
            )

            if not new_exact and not new_similar:
                logger.info("Evening duplicate report: nothing new since yesterday, staying quiet")
                continue

            from .duplicate_scanner import get_duplicate_scanner
            msg = get_duplicate_scanner()._format_telegram_report(new_exact, new_similar)
            if msg is None:
                continue

            from . import telegram_bot as _tb_mod
            from .telegram.bot_core import _get_linked_ids
            if _tb_mod._application is not None:
                linked = await _get_linked_ids()
                for chat_id in linked:
                    with suppress(Exception):
                        await _tb_mod._application.bot.send_message(
                            chat_id=chat_id, text=msg, parse_mode="HTML"
                        )
            logger.info("Evening duplicate report sent to %d user(s)", len(linked) if _tb_mod._application is not None else 0)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Evening duplicate report failed: %s", exc)


async def _supervise_telegram_bot() -> None:
    """Watch the Telegram bot task and restart on crash with exponential backoff."""
    from .telegram_bot import start_bot as _start_bot
    from .config import settings as _settings

    if not _settings.telegram_bot_token:
        return  # bot not configured, nothing to supervise

    attempt = 0
    while True:
        await asyncio.sleep(10)  # flat health-check poll interval

        from . import telegram_bot as _tb_mod
        if _tb_mod._application is not None:
            # Bot is healthy — reset counter and continue polling
            attempt = 0
            continue

        # Bot is down — apply backoff before attempting restart
        attempt += 1
        if attempt > _BOT_MAX_RESTARTS:
            logger.error(
                "Telegram bot supervisor giving up after %d restart attempts",
                _BOT_MAX_RESTARTS,
            )
            return

        delay = _BOT_BACKOFF_SCHEDULE[min(attempt - 1, len(_BOT_BACKOFF_SCHEDULE) - 1)]
        logger.warning(
            "Telegram bot down — restart attempt %d/%d, waiting %ds",
            attempt, _BOT_MAX_RESTARTS, delay,
        )
        await asyncio.sleep(delay)

        try:
            await _start_bot()
            if _tb_mod._application is not None:
                logger.info("Telegram bot recovered after %d attempt(s)", attempt)
                attempt = 0
        except Exception as exc:
            logger.error("Telegram bot restart failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: ensure dirs exist, generate TLS cert, detect board, auto-remount saved storage device."""
    # Configure logging before any startup log lines.
    configure_logging(settings.log_level)

    logger.info(
        "backend_start",
        extra={
            "version": settings.backend_version,
            "data_dir": str(settings.data_dir),
            "nas_root": str(settings.nas_root),
            "port": settings.port,
        },
    )

    # Detect board configuration and store in app state
    app.state.board = detect_board()

    settings.data_dir.mkdir(parents=True, exist_ok=True)

    # Mark boards that were set up before the setup marker existed.
    #
    # Unauthenticated admin creation is gated on "this board has never been set up", recorded by a
    # durable marker rather than inferred from an empty user list — because a missing or corrupt
    # users.json also reads as empty, which reopened that path on boards that already have an
    # owner. Backfilling here rather than lazily means an already-installed board is protected from
    # its next restart, not from whenever someone next happens to call POST /users. (M-11.)
    from . import store as _store  # noqa: PLC0415 — local, keeps module import order unchanged
    await _store.backfill_setup_marker()

    settings.personal_path.mkdir(parents=True, exist_ok=True)
    settings.family_path.mkdir(parents=True, exist_ok=True)
    settings.entertainment_path.mkdir(parents=True, exist_ok=True)
    # Ensure family .inbox/ exists for auto-sorting of shared-folder uploads
    (settings.family_path / ".inbox").mkdir(exist_ok=True)

    # One-time migration: shared/ → family/ and entertainment/
    import shutil as _shutil
    old_shared = settings.nas_root / "shared"
    new_family = settings.family_path
    old_entertainment = old_shared / "Entertainment"
    new_entertainment = settings.entertainment_path

    if old_shared.exists() and not new_family.exists():
        logger.info("Migrating shared/ → family/ and entertainment/")
        try:
            if old_entertainment.exists():
                new_entertainment.mkdir(parents=True, exist_ok=True)
                for item in old_entertainment.iterdir():
                    _shutil.move(str(item), str(new_entertainment / item.name))
                old_entertainment.rmdir()
            _shutil.move(str(old_shared), str(new_family))
            logger.info("Migration complete: shared/ → family/")
        except (OSError, _shutil.Error) as e:
            logger.error("Migration failed: %s", e)

    # Auto-generate self-signed TLS cert if needed
    if settings.tls_enabled:
        try:
            cert, key = await ensure_tls_cert()
            logger.info("TLS enabled — cert=%s key=%s", cert, key)
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("TLS cert generation failed, running without TLS: %s", e)
            settings.tls_enabled = False

    logger.info("AiHomeCloud backend starting on %s:%s", settings.host, settings.port)
    logger.info("  NAS root : %s", settings.nas_root)
    logger.info("  Data dir : %s", settings.data_dir)
    logger.info("  TLS      : %s", 'enabled' if settings.tls_enabled else 'disabled')
    logger.info("CORS origins configured: %s", settings.cors_origins)

    # Log JWT secret provenance without revealing the secret value
    try:
        jwt_secret_file = settings.data_dir / "jwt_secret"
        if jwt_secret_file.exists():
            logger.info("JWT secret loaded from %s", jwt_secret_file)
        elif os.getenv("AHC_JWT_SECRET"):
            logger.info("JWT secret provided via environment variable")
        else:
            logger.info("JWT secret: using default placeholder (insecure)")
    except OSError:
        logger.debug("Unable to check JWT secret file existence")

    # Auto-remount previously-mounted storage device
    try:
        await storage_routes.try_auto_remount()
    except (OSError, RuntimeError, ValueError) as e:
        logger.error("Auto-remount failed: %s", e)

    # Auto-remount previously-mounted local_backup secondary drive (same reboot-survival
    # gap as primary storage above, but scoped to its own drive state).
    try:
        from . import local_backup as _local_backup
        await _local_backup.try_auto_remount()
    except (OSError, RuntimeError, ValueError) as e:
        logger.error("local_backup auto-remount failed: %s", e)

    # Ensure DLNA is on by default at backend startup (if service is installed).
    try:
        from .routes.storage_helpers import ensure_dlna_started_and_enabled

        await ensure_dlna_started_and_enabled()
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("DLNA startup ensure failed: %s", e)

    # Purge old refresh tokens (cleanup tokens.json) older than 30 days past expiry
    try:
        from . import store as _store_module
        from datetime import datetime, timedelta, timezone

        cutoff = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        removed = await _store_module.purge_expired_tokens(cutoff)
        if removed:
            logger.info("Purged %d expired refresh tokens", removed)
    except (OSError, ValueError):
        logger.debug("Token purge skipped or failed")

    # Clear expired pairing OTPs on startup
    try:
        from . import store as _store_module
        from datetime import datetime, timezone

        otp = await _store_module.get_otp()
        if otp and otp.get("expires_at"):
            if int(otp.get("expires_at", 0)) < int(datetime.now(timezone.utc).timestamp()):
                await _store_module.clear_otp()
                logger.info("Cleared expired pairing OTP on startup")
    except (OSError, ValueError):
        logger.debug("Pairing OTP cleanup skipped or failed")

    # Initialise document search index (FTS5)
    try:
        from .document_index import init_db as _init_doc_db
        await _init_doc_db()
    except (OSError, RuntimeError, ValueError) as e:
        logger.error("document_index init failed: %s", e)

    # Initialise upload idempotency keys (replay a retried upload, don't duplicate it)
    try:
        from .upload_idempotency import init_db as _init_idem_db, prune as _prune_idem
        _init_idem_db()
        # Old keys serve no purpose and the table is tiny; one sweep at boot is
        # enough for a household-scale server — no scheduler needed.
        _prune_idem()
    except (OSError, RuntimeError, ValueError) as e:
        logger.error("upload_idempotency init failed: %s", e)

    # Initialise the semantic-search vector store. Cheap and unconditional: the table must exist
    # before the first indexing run, and creating it costs nothing on a board that never uses it.
    # Skipping this was a real bug — the background job failed instantly with "no such table:
    # vectors", and every test masked it by calling init_db() in a fixture.
    try:
        from .vector_store import init_db as _init_vectors, migrate_legacy_db as _migrate_vectors
        _init_vectors()
        # A board upgrading to the sharded layout has a perfectly good pre-sharding `vectors.db`
        # that nothing reads any more — semantic search would answer every query with zero results
        # and no error. Migrating on startup is what makes the upgrade invisible instead of
        # silently broken. No-op once the legacy file has been retired.
        _migrate_vectors()
    except (OSError, RuntimeError, ValueError) as e:
        logger.error("vector_store init failed: %s", e)

    # Initialise media metadata index (category/source/capture-date per ingested file)
    try:
        from .media_index import init_db as _init_media_db
        await _init_media_db()
    except (OSError, RuntimeError, ValueError) as e:
        logger.error("media_index init failed: %s", e)

    # Startup hygiene: remove known backend test artifacts from user storage/index.
    try:
        from .hygiene import cleanup_startup_artifacts as _cleanup_startup_artifacts

        stats = await _cleanup_startup_artifacts()
        if any(stats.values()):
            logger.info("Startup hygiene cleanup completed: %s", stats)
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("Startup hygiene cleanup failed: %s", e)

    # Start InboxWatcher for auto-sorting uploaded files (opt-in via AHC_AUTO_SORT_ENABLED)
    if settings.auto_sort_enabled:
        try:
            from .file_sorter import get_watcher as _get_watcher
            _get_watcher().start()
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("InboxWatcher startup failed: %s", e)
    else:
        logger.info("InboxWatcher disabled — set AHC_AUTO_SORT_ENABLED=true to enable")

    # Start document index watcher for out-of-band file changes.
    try:
        from .index_watcher import get_index_watcher as _get_index_watcher
        _get_index_watcher().start()
    except (OSError, RuntimeError, ValueError) as e:
        logger.error("DocumentIndexWatcher startup failed: %s", e)

    # Start nightly media_index reconciler (prune + incremental re-index + bucket
    # counter repair) — separate from the document watcher above, which only
    # covers Documents/OCR, not the full Photos/Videos/media tree.
    try:
        from .media_reconciler import get_media_reconciler as _get_media_reconciler
        _get_media_reconciler().start()
    except (OSError, RuntimeError, ValueError) as e:
        logger.error("MediaReconciler startup failed: %s", e)

    # Start Telegram bot (optional — skipped if token not set)
    try:
        # Restore Telegram runtime settings from persisted config.
        saved_tg = await _store_module.get_value("telegram_config", default={})
        if isinstance(saved_tg, dict) and saved_tg:
            token = str(saved_tg.get("bot_token", "") or "").strip()
            if token:
                settings.telegram_bot_token = token  # type: ignore[misc]

            api_id = int(saved_tg.get("api_id", 0) or 0)
            api_hash = str(saved_tg.get("api_hash", "") or "")
            local_enabled = bool(saved_tg.get("local_api_enabled", False))

            settings.telegram_api_id = api_id  # type: ignore[misc]
            settings.telegram_api_hash = api_hash  # type: ignore[misc]
            settings.telegram_local_api_enabled = local_enabled  # type: ignore[misc]

        from .telegram_bot import start_bot as _start_bot
        await _start_bot()
    except (OSError, RuntimeError, ValueError) as e:
        logger.error("Telegram bot startup failed: %s", e)

    # Launch Telegram bot supervisor (restarts on crash with exponential backoff)
    app.state.bot_supervisor_task = asyncio.create_task(
        _supervise_telegram_bot(), name="telegram_bot_supervisor"
    )

    # Nightly duplicate scan at 4:00 AM + evening Telegram report at 6:00 PM
    app.state.dup_scan_task = asyncio.create_task(
        _run_nightly_duplicate_scan(), name="duplicate_scan_nightly"
    )
    app.state.duration_backfill_task = asyncio.create_task(
        _backfill_video_durations(), name="video_duration_backfill"
    )
    app.state.http_explainer_task = asyncio.create_task(
        _serve_http_explainer(), name="http_explainer"
    )
    app.state.dup_report_task = asyncio.create_task(
        _send_evening_duplicate_report(), name="duplicate_report_evening"
    )

    # Periodic self-RSS + cache-size logging (see docstring) — diagnosing memory footprint
    # ahead of moving from this 4GB dev board to a 1GB-RAM production SBC.
    app.state.memory_diagnostics_task = asyncio.create_task(
        _memory_diagnostics_loop(), name="memory_diagnostics"
    )

    # Nightly local_backup protected-folder sync at 2:00 AM
    app.state.local_backup_task = asyncio.create_task(
        _run_nightly_local_backup_sync(), name="local_backup_sync_nightly"
    )

    # Bring WiFi to its last desired state (both WiFi + Ethernet may be active
    # together), then start a background monitor that self-heals radio-state drift.
    try:
        from .wifi_manager import ensure_wifi_on_startup, start_wifi_monitor
        await ensure_wifi_on_startup()
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning("WiFi startup check failed: %s", e)
    start_wifi_monitor()

    # In-process mDNS advertisement — only where nothing else is already doing it. Linux
    # boards are covered by avahi-daemon (install.sh's configure_mdns()); starting a second
    # advertiser there would double-broadcast the same service type for no benefit. Windows
    # has no avahi equivalent at all, so this is the only advertiser it gets.
    if platform_profile.host_kind() == platform_profile.HostKind.WINDOWS:
        try:
            from . import mdns_advertiser
            await mdns_advertiser.start()
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("mDNS advertisement failed to start: %s", e)

    yield

    if platform_profile.host_kind() == platform_profile.HostKind.WINDOWS:
        try:
            from . import mdns_advertiser
            await mdns_advertiser.stop()
        except (OSError, RuntimeError, ValueError):
            logger.debug("mDNS advertiser shutdown skipped")

    # Cancel scheduled tasks
    for task_attr in ("bot_supervisor_task", "dup_scan_task", "dup_report_task", "memory_diagnostics_task", "local_backup_task", "duration_backfill_task", "http_explainer_task"):
        task = getattr(app.state, task_attr, None)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    # Stop Telegram bot
    try:
        from .telegram_bot import stop_bot as _stop_bot
        await _stop_bot()
    except (OSError, RuntimeError, ValueError):
        logger.debug("Telegram bot shutdown skipped")

    # Stop InboxWatcher (only if it was started)
    if settings.auto_sort_enabled:
        try:
            from .file_sorter import get_watcher as _get_watcher
            await _get_watcher().stop()
        except (OSError, RuntimeError, ValueError):
            logger.debug("InboxWatcher shutdown skipped")

    # Stop document index watcher
    try:
        from .index_watcher import get_index_watcher as _get_index_watcher
        await _get_index_watcher().stop()
    except (OSError, RuntimeError, ValueError):
        logger.debug("DocumentIndexWatcher shutdown skipped")

    # Stop media_index reconciler
    try:
        from .media_reconciler import get_media_reconciler as _get_media_reconciler
        await _get_media_reconciler().stop()
    except (OSError, RuntimeError, ValueError):
        logger.debug("MediaReconciler shutdown skipped")

    # Stop WiFi monitor
    try:
        from .wifi_manager import stop_wifi_monitor
        await stop_wifi_monitor()
    except (OSError, RuntimeError, ValueError):
        logger.debug("WiFi monitor stop skipped")

    # Close document index connection pool
    try:
        from .document_index import close_db as _close_doc_db
        await _close_doc_db()
    except (OSError, RuntimeError, ValueError):
        logger.debug("Document index pool close skipped")

    # Close media index connection pool
    try:
        from .upload_idempotency import close_db as _close_idem_db
        _close_idem_db()
        from .media_index import close_db as _close_media_db
        await _close_media_db()
    except (OSError, RuntimeError, ValueError):
        logger.debug("Media index pool close skipped")


app = FastAPI(
    title="AiHomeCloud API",
    version="0.1.0",
    description="Backend API for the AiHomeCloud home NAS",
    lifespan=lifespan,
)


@app.exception_handler(platform_profile.CapabilityUnavailable)
async def _capability_unavailable(request: Request, exc: platform_profile.CapabilityUnavailable):
    """
    501, not 400 or 503. The caller did nothing wrong, and this is not transient — the host
    lacks the hardware or tooling and always will. Naming the capability lets a client
    disable the control instead of retrying.
    """
    logger.info("capability_refused capability=%s path=%s", exc.capability.value, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={
            "detail": str(exc),
            "capability": exc.capability.value,
            "host": platform_profile.host_kind().value,
        },
    )


@app.exception_handler(OSError)
async def _os_error_handler(request: Request, exc: OSError):
    """L-2 fix (security audit 2026-08): a user-controlled name (family member, folder, upload
    filename, ...) that becomes a filesystem path component and exceeds the OS's max component
    length raises a raw OSError(ENAMETOOLONG) from whichever mkdir/rename call hit it -- dozens
    of call sites across the routes, none of which caught it, so it fell through as a generic
    500 (or worse, a raw traceback in a dev environment) instead of "that name is too long."

    Deliberately narrow: only ENAMETOOLONG gets the clean 400. Every other OSError re-raises and
    falls through to exactly the same unhandled-exception path as before this handler existed --
    this is not a general OSError-to-400 downgrade, which would risk masking a genuine disk
    failure as a client error.
    """
    if exc.errno == errno.ENAMETOOLONG:
        logger.warning("ename_too_long path=%s", request.url.path)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Name too long"},
        )
    raise exc

# Rate limiting (slowapi)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Restrict CORS to configured origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

# Security response headers middleware
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "0"
    # HSTS only once a CA has signed the certificate. Sent alongside a self-signed one it strips
    # Chrome's "Proceed anyway" link from the warning, locking every browser out of the board for a
    # year with no recovery short of chrome://net-internals/#hsts — see tls.is_self_signed.
    if settings.tls_enabled and not is_self_signed():
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Prevent caching of auth responses
    if request.url.path.startswith("/api/v1/auth") or request.url.path.startswith("/api/v1/login"):
        response.headers["Cache-Control"] = "no-store"
    return response

# Paths called very frequently — skip per-request logging to reduce overhead.
_QUIET_PATHS = frozenset({
    "/api/v1/files/list",
    "/api/v1/files/download",
    "/api/v1/files/upload",
    "/api/v1/monitor/ws",
    "/api/health",
})


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = uuid4().hex
    request.state.request_id = request_id
    token = set_request_id(request_id)

    path = request.url.path
    verbose = path not in _QUIET_PATHS

    # A person is doing something — background work must get out of the way. Health checks and the
    # other _QUIET_PATHS are excluded deliberately: a monitor polling every few seconds would
    # otherwise hold the board permanently "busy" and starve indexing forever.
    if verbose:
        workload.mark_user_active()

    if verbose:
        logger.info(
            "request_start",
            extra={"method": request.method, "path": path},
        )
    try:
        response = await call_next(request)
        if verbose or response.status_code >= 400:
            logger.info(
                "request_end",
                extra={
                    "method": request.method,
                    "path": path,
                    "status_code": response.status_code,
                },
            )
        return response
    finally:
        reset_request_id(token)

# Register all routers
app.include_router(auth_routes.router)
app.include_router(system_routes.router)
app.include_router(monitor_routes.router)
app.include_router(file_routes.router)
app.include_router(trash_routes.router)
app.include_router(jobs_routes.router)
app.include_router(family_routes.router)
app.include_router(service_routes.router)
app.include_router(storage_routes.router)
app.include_router(event_routes.router)
app.include_router(network_routes.router)
app.include_router(telegram_routes.router)
app.include_router(telegram_upload_routes.router)
app.include_router(backup_routes.router)
app.include_router(web_upload_routes.router)
app.include_router(web_browser_routes.router)
app.include_router(webapp_routes.router)
app.include_router(media_routes.router)
app.include_router(activity_routes.router)
app.include_router(events_routes.router)
app.include_router(local_backup_routes.router)
app.include_router(bluetooth_routes.router)


# Reject oversized bodies before anything buffers them.
#
# Registered LAST on purpose, which makes it OUTERMOST: Starlette prepends each add_middleware call,
# so the last registered wraps everything else. Order is the whole point here — a size cap that runs
# after the multipart parser has spooled the body to disk has already lost. It must see the request
# while the body is still unread ASGI messages.
app.add_middleware(UploadSizeLimitMiddleware, max_bytes=settings.max_upload_bytes)


async def _device_display_name() -> str:
    """
    The device's name as the user set it.

    Single source for every endpoint that reports identity. `/api/health` and `/` previously
    returned `settings.device_name` — the install-time default — while `/api/v1/system/info`
    returned the stored, user-renamed value. A board renamed to "radxa-nas" still announced
    itself as "My AiHomeCloud" on health, which is precisely the endpoint health's own docstring
    tells clients to use to tell devices apart. Found 2026-08-05 while auditing the TV app.
    """
    # Imported here, not at module scope, matching how the rest of this file reaches `store`.
    from . import store
    state = await store.get_device_state()
    return state.get("name", settings.device_name)


@app.get("/api/health", response_model=HealthResponse)
async def health():
    """Health check — unversioned, always available. Includes device identity (already
    exposed unauthenticated on `/`) so a client probing several known addresses in parallel
    can tell which physical device each live one is, in the same round-trip."""
    return {"status": "ok", "deviceName": await _device_display_name(), "serial": settings.device_serial}


@app.get("/", response_model=RootResponse)
async def root():
    return {
        "service": "AiHomeCloud",
        "version": settings.backend_version,
        "deviceName": await _device_display_name(),
        "serial": settings.device_serial,
    }


_VERSIONED_PATH_RE = re.compile(r"^v\d+/")


# Backward-compatible redirect: /api/... -> /api/v1/...
# Excluded from the OpenAPI schema deliberately. It is a redirect shim, not API
# surface: as a catch-all it would shadow every real path in a generated client, and
# sharing one handler across seven methods produced a duplicate operationId whose
# suffix varied with the interpreter's hash seed, making the generated spec unstable.
@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def redirect_api(path: str, request: Request):
    # Guard against an infinite redirect loop: if `path` already starts with a version segment
    # (e.g. "v1/..."), the real route genuinely doesn't exist under any version -- prepending
    # another "v1" would never converge, it would just keep matching this same catch-all forever.
    # Found live 2026-07-15: a client hitting a not-yet-deployed /api/v1/... endpoint looped
    # through this handler, accumulating one extra "/v1" per redirect, until the client's own
    # redirect-count limit finally aborted it -- a genuinely missing endpoint should 404, not loop.
    if _VERSIONED_PATH_RE.match(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    target = f"/api/v1/{path}"
    # Preserve method semantics with 308 Permanent Redirect
    return RedirectResponse(url=target, status_code=308)


# ── Entry point for python -m app.main ──────────────────────────────────────

if __name__ == "__main__":
    kwargs = {
        "app": "app.main:app",
        "host": settings.host,
        "port": settings.port,
        "log_level": "info",
    }
    if settings.tls_enabled:
        try:
            cert, key = asyncio.run(ensure_tls_cert())
            kwargs["ssl_certfile"] = str(cert)
            kwargs["ssl_keyfile"] = str(key)
        except (OSError, RuntimeError, ValueError):
            logger.warning("Starting without TLS")
            # Without this, the lifespan's own ensure_tls_cert() call (guarded by
            # settings.tls_enabled, see below) redundantly repeats the exact same
            # already-failed 30s-bounded wait -- doubling worst-case startup latency for no
            # reason, since this attempt already answered the question. On any platform
            # without a working cert-issuance path (Windows today: no ahc-issue-cert.path
            # equivalent exists yet), this made every single startup pay 60s instead of 30s.
            # Found 2026-08-20 running install_windows.ps1 on real Windows hardware.
            settings.tls_enabled = False
    uvicorn.run(**kwargs)
