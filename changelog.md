# Changelog

---

## 2026-06-27 — Capture-time preservation on upload (Option A + B)

- **Embedded date → file mtime**: `file_routes.py` now sets each uploaded file's mtime to its embedded
  capture date so listings group/sort by when media was taken, not when it was uploaded. Helper
  `_extract_capture_epoch()` reads EXIF `DateTimeOriginal`/`DateTimeDigitized`/`DateTime` (Pillow) for
  images and `format_tags=creation_time` (ffprobe) for videos; `_apply_capture_time()` applies it via
  `os.utime`. Wired into **both** upload handlers (multipart `upload_file` + streaming `upload_file_stream`).
- **Client fallback for metadata-stripped media**: both handlers accept `original_date: float | None`
  (epoch seconds, query param). When a file carries no embedded date (e.g. numeric-named videos with no
  ffprobe `creation_time`), the mtime falls back to the client-supplied phone MediaStore date; otherwise
  it keeps the upload time. Silent best-effort — never raises.
- **Requires** `ffmpeg`/`ffprobe` installed on the board (added 2026-06-27).

---

## 2026-03-27 — Web Upload Portal (Tasks 1–6)

- **New route**: `GET /web` — self-contained HTML+CSS+JS page served by FastAPI (`web_upload_routes.py`)
- **User picker**: fetches `GET /auth/users/names`, shows emoji avatars, PIN modal with on-screen keypad + keyboard support, auto-submit on 4th digit
- **Upload dashboard**: 3 drag-and-drop zones (Personal / Family / Entertainment) in responsive CSS Grid (3-col desktop, 1-col mobile)
- **Upload engine**: XHR with `upload.onprogress` for real-time progress bars, sequential queue per zone, parallel across zones, retry button on failure, 401 auto-logout
- **Session management**: JWT stored in `sessionStorage`; logout clears all state; `history.replaceState` prevents back-button navigation away from SPA
- **Zero new dependencies**: reuses existing `/auth/login`, `/api/v1/files/upload`, path sandboxing, TLS

---

## 2026-03-27 — Auto Backup Redesign: All File Types, Categorisation, Daily Schedule, Telegram Notifications

- **All file types**: Removed media-only filter (`_isMediaFile`) from both `backup_runner.dart` and `backup_worker.dart` — backup now handles photos, videos, documents, audio, and any other file type
- **Auto-categorisation**: New `_categoryOf()` helper in runner + worker sorts files into `Photos/`, `Videos/`, `Documents/`, `Audio/`, `Other/` sub-folders by extension
- **Destination path change**: Removed hardcoded `/Photos` and `/Movies` suffixes — destination now maps to base folder (`/personal/<user>/`, `/family/`); category sub-folders are appended automatically
- **Daily 2:30 AM schedule**: Changed `schedulePeriodicBackup()` from 6-hour to 24-hour with `initialDelay` targeting 2:30 AM; uses `ExistingPeriodicWorkPolicy.replace`
- **Telegram notifications**: New `POST /api/v1/backup/notify` endpoint sends backup summary (success) or failure alert to all linked Telegram users; runner calls it after manual backup; worker calls it on scheduled backup failure
- **Removed entertainment destination**: `_VALID_DESTINATIONS` now `{personal, family}` only; updated all validators, models, and UI
- **UI updates**: Empty state text: "Back up your files automatically"; folder picker: "photos, documents, anything"; schedule notice: "Runs daily at 2:30 AM on WiFi · files sorted by type"; notification text: "files" instead of "photos"

## 2026-03-24 — Phone Auto Backup Phase 1

- **`backup_routes.py`** (new): 6 endpoints — `POST /check-duplicate`, `POST /record-hash`, `GET /status`, `POST /jobs`, `DELETE /jobs/{id}`, `POST /jobs/{id}/report`; SHA-256 dedup store (max 50 000 hashes), job config store (max 20 jobs)
- **`backup_batcher.dart`** (new): pure-Dart folder naming + batching; groups files by year-month into "Mar 2024" / "Mar 2024 (2)" folders (max 500/folder, never split mid-day); filename date parsing (WhatsApp, Screenshot_, generic YYYYMMDD)
- **`backup_worker.dart`** (new): WorkManager callback dispatcher + `BackupWorker` singleton; 6-hour periodic + on-demand one-shot tasks; WiFi-only constraint; SHA-256 → check → upload → record pipeline; local-notifications progress + summary
- **`backup_models.dart`** (new): `BackupJob`, `BackupStatus` with `statusSubtitle` helper
- **`backup_api.dart`** (new): ApiService extension with all 6 backup endpoint methods
- **`auto_backup_screen.dart`** (new): empty state / 3-step setup sheet (folder picker, destination card, WiFi confirm) / active job list with "Back up now" trigger
- **`more_screen.dart`**: replaced Trash tile with Auto Backup tile (showing live status subtitle); removed dead `_TrashCard` class
- **`main.dart`**: `BackupWorker.instance.initialize()` + `schedulePeriodicBackup()` on app startup
- **`AndroidManifest.xml`**: added `READ_MEDIA_IMAGES`, `READ_MEDIA_VIDEO`, `FOREGROUND_SERVICE`, `RECEIVE_BOOT_COMPLETED` permissions + WorkManager `InitializationProvider`
- **`pubspec.yaml`**: added `workmanager: ^0.5.0`, `exif: ^3.3.0`
- **Tests**: 14 new backend tests (`test_backup.py`); 16 new Flutter unit tests (`backup_batcher_test.dart`); all pass

---

## 2026-03-23 — Setup Wizard + Documentation Cleanup

- **Setup wizard:** Added `scripts/setup-wizard.sh` — interactive whiptail/dialog wizard for first-time device setup (device name, admin PIN, optional storage format, systemd install)
- **Stale docs removed:** Deleted gitlog.md, gitLogs.md, TASK_ROADMAP.md, audit3/ (4 completed fix files), docs/integration-testing-plan.md (unimplemented plan)
- **backend/README.md:** Trimmed outdated endpoint list and manual deploy steps; now cross-references kb/ docs
- **.gitignore:** Added audit3/, gitlog*.md, .ai-context/ patterns
- **README.md:** Added "First-Time Setup" section with wizard instructions; updated dev commands

## 2026-03-23 — Repo Cleanup, Provider Consolidation, Refactor Pass

- **Repo hygiene:** Removed 18 dev artifact files (BUG_REPORT.md, PROJECT_AUDIT.md, CODEBASE_AUDIT_2026_03_16.md, docs/archive/*, docs/audit/*); updated .gitignore
- **Provider consolidation:** Replaced `lib/providers.dart` barrel with specific imports in 17 Dart files; deleted the barrel file
- **Trash routes extraction:** Split trash helpers/endpoints (~180 lines) from `file_routes.py` into new `trash_routes.py`; registered in main.py
- **Dead code removal:** Removed unused imports in 10 backend files (board.py, main.py, models.py, wifi_manager.py, file_routes.py, monitor_routes.py, storage_helpers.py, auth_handlers.py, search_handlers.py, upload_handlers.py); fixed duplicate telegram import block; removed unused variable; fixed `_unlink_trash_item` import path in upload_handlers.py

---

## 2026-03-23 — Fix WebSocket token expiry and notification reconnect

- `system_api.dart` (`monitorSystemStats`): moved `token`/`uri` construction inside the retry loop so every reconnect uses the current (possibly refreshed) JWT — previously a stale token caused `code=4003` on every reconnect attempt after 1 hour
- `system_api.dart` (`notificationStream`): replaced single-shot `StreamController`+`listen()` with `async*` reconnect loop (same pattern as `monitorSystemStats`) — notification bell and overlay now survive network drops and token expiry
- `flutter-patterns.md`: added WebSocket Stream Patterns section documenting the URI-rebuild and `async*` retry patterns

---

## 2026-03-23 — Security: Trash Prefs Admin Guard + Pairing Safety Check

- `file_routes.py`: `PUT /api/v1/files/trash/prefs` now requires admin — non-admin family members cannot enable auto-delete (which would permanently delete everyone's data)
- `auth_routes.py`: Confirmed `_bg_wipe_stale_nas_dirs()` is already gated behind `if is_first_user:` (zero existing users) — no code change needed for pairing guard
- Tests: added `member_token` fixture to `conftest.py`; added `test_set_trash_prefs_requires_admin` (expects 403) and `test_set_trash_prefs_admin_succeeds` (expects 204)

---

## 2026-03-23 — Quick Wins: WAL mode, get_local_ip fallback, bot supervisor poll, unawaited persist

- `document_index.py`: Enabled `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` on all SQLite connections — eliminates FTS5 read/write contention during concurrent uploads/searches
- `config.py`: Rewrote `get_local_ip()` with interface-enumeration as primary method (works on LAN-only devices without internet route); routing-based discovery kept as fallback
- `main.py`: Fixed `_supervise_telegram_bot()` — now uses a flat 10s health-check poll and only applies backoff sleep when bot is actually down; removed pointless sleep on every healthy-check iteration
- `auth_session.dart`: Added `dart:async` import; used `unawaited(Future.wait(writes))` inside `_persistLogin` to explicitly annotate fire-and-forget disk persist

---

## 2026-03-23 — Backend Memory & Performance Fixes

- `auth_routes.py`: Added `_prune_failed_logins()` — expired lockout entries are now evicted on every `_record_failure()` call and at the top of the login handler, preventing unbounded dict growth from rotating IPs
- `file_routes.py`: Added `_evict_expired_scan_cache()` — expired scan cache entries are removed before every write, keeping `_scan_cache` bounded during long NAS browsing sessions
- `file_routes.py`: Offloaded `rglob` directory size calculation in `delete_file` to `loop.run_in_executor()` via `_calc_dir_size()` — prevents event loop blocking on large directories
- `store.py`: Fixed `get_value` to read `_cache` directly (bypassing `_get_cached`) so `None` stored values are returned from cache correctly rather than causing repeated disk reads
- Added targeted tests in `test_auth.py`, `test_file_routes.py`, `test_store.py` — 77 pass

---

## 2026-03-17 — Telegram Local API: Source Build (replaces Docker)

- Replaced Docker-based `setup-local-api` flow with source build from `tdlib/telegram-bot-api`
- Background job: installs build deps → clones → compiles (`-j2`) → installs binary → creates systemd service → health check → activates 2GB mode
- Sends Telegram confirmation message ("✅ 2 GB file mode is now active!") to all linked users on success
- Flutter: added confirmation dialog before build ("5-15 min") with Start Build / Cancel
- `install.sh`: added `cmake g++ libssl-dev zlib1g-dev gperf` to system packages; added sudoers entries for `cp`, `systemctl daemon-reload`, `systemctl enable`
- Added 8 tests for the setup-local-api endpoint (validation, auth, job creation, skip-when-active, dep-failure, confirmation message)
- 524 backend tests pass

## 2026-03-17 — Emoji Mojibake Fix + OCR Activation

- Fixed broken emoji in 6 telegram bot files (`bot_core.py`, `search_handlers.py`, `auth_handlers.py`, `telegram_upload_routes.py`, `telegram_bot.py`, `document_index.py`) — 4-byte emoji were double-encoded (UTF-8→cp1252→UTF-8 twice), producing garbled output like `ðŸ" No documents found` instead of `🔍 No documents found`
- Installed `tesseract-ocr`, `tesseract-ocr-eng`, `tesseract-ocr-hin` — OCR was completely non-functional (binary not present)
- Force re-indexed all 31 existing documents — now 30/31 have OCR text extracted; `search_documents('bapu')` returns correct results
- Added tesseract language packs to `install.sh` for future clean installs
- 516 backend tests pass — commit `aa40fbd`

## 2026-03-16 — Codebase Audit Fixes

**Critical:**
- `main.py`: Added `import asyncio` at module level — app was crashing at startup when Telegram bot was configured (BUG-C1)

**Bugs:**
- `bot_core.py`: Delete entertainment file from disk before raising `DuplicateFileError` — was leaking duplicate files (BUG-B1)
- `store.py`: Added `atomic_update()` helper for race-safe read-modify-write on kv.json; refactored 5 bot_core.py functions (`_add_linked_id`, `_add_pending_approval`, `_remove_pending_approval`, `_record_file_hash`, `_record_recent_file`) to use it (BUG-B2)
- `main_shell.dart`: Moved `_awaySheetDismissed = true` from sheet show to Dismiss button's onPressed — flag was being set before sheet actually appeared (BUG-B3)
- `bot_core.py` + `upload_handlers.py`: Renamed `DuplicateFileError.md5` field to `.sha256` — field was misnamed after SHA-256 migration (BUG-B4)

**Optimizations:**
- `bot_core.py`: `_check_duplicate()` now returns `(sha256, record)` tuple; both `_store_private_or_shared_file` and `_store_entertainment_file` reuse the hash — eliminates double SHA-256 computation per upload (OPT-1)
- `bot_core.py`: `_record_file_hash()` caps hash dict at 10,000 entries, evicting oldest by `saved_at` (OPT-2)
- `main_shell.dart`: Added `mounted` guard at top of ping timer callback to prevent Riverpod state errors after widget disposal (OPT-4)
- `splash_screen.dart`: Added 400ms warmup delay before navigating to dashboard — lets first LAN response arrive before shimmer shows (OPT-5)

**Config/Docs:**
- `copilot-instructions.md`: Fixed env prefix `CUBIE_` → `AHC_` (CFG-2)
- `deploy.sh`: Updated comment "Cubie device" → "AiHomeCloud device" (CFG-4)

**Skipped (already resolved or incorrect):**
- CFG-1 (port standardisation): All files already use port 8443 consistently
- CFG-3 (service vs install.sh): install.sh already deploys to `/opt/aihomecloud` with `venv/`
- OPT-3 (flutter_blue_plus unused): Actually used in `discovery_service.dart` for BLE fallback

**Tests:** 260 passed, 47 skipped, 0 failed

---

## 2026-03-17 — TASK-L1 through L5: Low-Priority Sprint

**Flutter:**
- `app_router.dart`: Added redirect guard for `/profile-creation` → `/scan-network` when `discoveryNotifierProvider.status != DiscoveryStatus.found`; fixed null-safety in `/profile-creation` builder `extra` cast (L1)
- `file_list_tile.dart`, `app_card.dart`, `main_shell.dart`, `folder_view.dart`: Added `Semantics` wrappers, `semanticLabel`/`tooltip` on all nav icons, `liveRegion: true` on reconnecting banner (L2)
- `lib/l10n/app_en.arb`: Added 30+ new keys (`navMore`, `shellReconnecting`, `shellUploadingProgress`, `moreScreenTitle`, `moreSectionSharing`, plus 25 more); `flutter gen-l10n` re-run; `AppLocalizations` wired in `main_shell.dart` (nav labels, banners) and `more_screen.dart` (all visible strings) (L3)

**Backend:**
- `backend/app/audit.py` (new): `audit_log(event, **kwargs)` emits `WARNING` to `aihomecloud.audit` logger with `"audit": True` in structured JSON output (L4)
- Audit calls wired in: `auth_routes.py` (`user_deleted_self`), `family_routes.py` (`family_member_removed`, `user_role_changed`), `file_routes.py` (`file_deleted`), `storage_routes.py` (`storage_formatted`) (L4)
- `file_routes.py`: `_scan_cache` dict with 7 s TTL; `_invalidate_scan_cache(dir_path)` called after delete, rename, create_folder, upload; `list_files` checks cache before running thread-pool scandir (L5)

**Tests:** 260 passed, 47 skipped, 0 failed

---

## 2026-03-16 — TASK-M1 through M10: Medium Sprint

**Flutter:**
- `folder_view.dart`: Pagination sentinel item now shows a standalone 24 px `CircularProgressIndicator` while `_loadingMore` is true, instead of a disabled button (M1)
- `system_api.dart`: `monitorSystemStats()` rewritten as `async*` generator; reconnects up to 30 times with 2–30 s exponential back-off; emits `ConnectionStatus.disconnected` after exhaustion and closes the stream (M3)
- `file_providers.dart` + `files_screen.dart`: Removed deferred `trashAutoDeleteProvider` and the 60-line auto-delete toggle UI card from `_TrashScreen` (M9)

**Backend:**
- `storage_routes.py`: `format_device` validates label with `re.match(r'^[a-zA-Z0-9_-]{1,16}$')`, raises HTTP 400 on invalid input (M5)
- `document_index.py`: Each `index_document()` call in `index_documents_under_path` is now wrapped in `asyncio.wait_for(..., timeout=120)`; logs warning and skips hung files (M6)
- `telegram_bot.py`: Duplicate-detection hash replaced MD5 → SHA-256 (`_compute_sha256`); existing cache naturally invalidates (M7). Both download paths now wrap `_download_to_path` in try/except and unlink partial temp files on failure with `logger.warning` (M8)
- `family_routes.py`: `_folder_size_gb_sync` now accepts `max_depth=5` and prunes `os.walk` below that level; `_folder_size_gb` wraps executor call in `asyncio.wait_for(timeout=10)` returning -1.0 on timeout; `logging` module added (M10)

**Already verified done (no code change needed):** M2 (`_DocSearchResults` had empty-state icon + text), M4 (`_startUpload` already had `onError` cleanup handler)

**Tests:** 260 passed, 2 skipped (unchanged)

---

## 2026-03-16 — TASK-C3 + TASK-C4: Race condition guard + CORS hardening

**Backend:**
- `store.py`: Added `_user_creation_lock = asyncio.Lock()` (separate from `_store_lock` to avoid deadlock; bcrypt completes before acquiring so hold time is minimal)
- `routes/auth_routes.py`: `create_user` hashes PIN before acquiring lock, then re-reads user list inside `_user_creation_lock` to guarantee atomic check-then-create for the first-user admin assignment
- `main.py`: CORS middleware now uses explicit `allow_methods=["GET","POST","PUT","DELETE","OPTIONS"]` and `allow_headers=["Authorization","Content-Type","X-Request-ID"]` instead of `["*"]`
- `tests/test_auth.py`: Added `test_concurrent_first_user_exactly_one_admin` — fires two simultaneous unauthenticated POST /users calls and asserts exactly one 201 (admin) and one 401

**Tests:** 260 passing, 2 skipped (hardware integration, up from 259)

---

## 2026-03-16 — TASK-C1 + TASK-C2: Admin promotion/demotion

**Backend:**
- `store.py`: Added `update_user_role(user_id, is_admin)` function
- `models.py`: Added `SetUserRoleRequest` Pydantic model with `isAdmin` alias
- `family_routes.py`: Added `PUT /api/v1/users/family/{user_id}/role` endpoint (admin-only; blocks demotion of last admin)
- `tests/test_auth.py`: 5 new tests — promote, demote, block-last-admin, 403 for non-admin, 404 for unknown

**Flutter:**
- `family_api.dart`: Added `setUserRole(userId, {required bool isAdmin})` method
- `app_card.dart`: Added `onLongPress` callback parameter to `AppCard`
- `family_screen.dart`: Long-press on member card opens bottom sheet with "Make Admin"/"Remove Admin" option; confirmation dialog before committing; admin-only visibility; full error handling via `friendlyError()`

---

## 2026-03-16 — Full Production Audit + Critical Fixes

**Audit:** Complete read-through of all backend (32 files) and Flutter (56 files) code.

**Fixed directly:**
- `auth_routes.py`: Pairing key/serial comparisons now use `hmac.compare_digest()` (timing attack prevention)
- `data_providers.dart`: `ServicesNotifier.toggle()` error callback now uses `friendlyError(e)` instead of raw `e.toString()`
- `family_screen.dart`: Add/remove member dialogs now have try/catch with `friendlyError()` snackbars
- `store.py`: `purge_expired_tokens()` now also removes revoked tokens (prevents unbounded file growth)

**Created:** Comprehensive `TASKS.md` with 25 prioritised tasks (4 critical, 8 high, 10 medium, 5 low) for other agents to work through.

---

## 2026-03-15 — Fix: infinite recursion in `friendlyError` causing spinner deadlock

**Bug:** After any failed login (wrong PIN) or failed profile creation, the spinner never cleared — buttons were stuck in loading state indefinitely.

**Root cause:** `friendlyError(e)` in `lib/core/error_utils.dart` had infinite recursion:
- `Exception("Invalid credentials").toString()` → `"Exception: Invalid credentials"`
- The "strip Exception: prefix" branch called `return friendlyError(Exception(inner))` 
- This re-wrapped the inner string in a new `Exception`, producing the *same* `.toString()` output
- Result: unbounded recursion → stack overflow inside the `catch` block's `setState`
- `_loading`/`_saving` flags were never reset; spinner froze permanently

**Fixes applied:**

- **`lib/core/error_utils.dart`**: Changed `return friendlyError(Exception(inner))` → `return friendlyError(inner)` (pass raw String, not a new Exception wrapper) — eliminates infinite recursion
- **`lib/screens/onboarding/pin_entry_screen.dart`** `_submit()`: Moved `_loading = false` into `finally` block (was only in `catch` — defensively correct now even if future exceptions occur)
- **`lib/screens/onboarding/pin_entry_screen.dart`** `_onUserTapped()`: Same `finally`-block pattern applied for `_loggingIn = false`

**After fix:**
- Wrong PIN → spinner clears, error "Invalid credentials" shown
- No auth token for admin-required endpoints → spinner clears, error message shown

---

## 2026-03-15 — New hardware bring-up: Rock Pi 4A + full backend setup audit

**Hardware:** Radxa ROCK Pi 4A (RK3399, Armbian Ubuntu 24.04 Noble, kernel 6.18-rockchip64).

**Root cause of app-not-finding-backend:** Backend service was never installed (`aihomecloud` system
user and `/opt/aihomecloud` do not exist on a fresh git clone). The backend had been running as an
orphaned process but was not auto-starting. avahi-daemon was installed but the mDNS service definition
file was not deployed to `/etc/avahi/services/`, so mDNS discovery was broken. App was falling back
to slow subnet scan.

**Fixes applied:**

- **`backend/app/main.py`**: Fixed deprecated `asyncio.get_event_loop().run_until_complete()` → `asyncio.run()`
- **`backend/app/routes/storage_helpers.py`**: Added `"-n"` (non-interactive) to all 6 sudo calls
- **`backend/app/routes/storage_routes.py`**: Added `"-n"` to all 13 sudo calls
- **`backend/app/routes/system_routes.py`**: Added `"-n"` to all 3 sudo calls
- **`backend/app/routes/service_routes.py`**: Added `"-n"` to 1 sudo call
- **`backend/app/board.py`**: Added `"Radxa ROCK Pi 4A"` to `KNOWN_BOARDS` and `"rock pi 4"` to `_BOARD_SUBSTRINGS`
- **`backend/tests/test_path_safety.py`**: Fixed `evil_link` test to use `tmp_path/nas` instead of real `/srv/nas` — was leaving a path-traversal symlink artifact in production directory
- **`backend/tests/test_telegram_bot.py`**: Updated `test_handle_auth_links_new_user` to reflect updated `_handle_auth` approval flow (Task 9 changed `/auth` from direct-link to pending-approval)
- **`scripts/dev-setup.sh`**: Full rewrite — now 10-step idempotent script: packages, avahi, venv creation, dirs, credentials, system-level service, conflict resolution, sudoers, artifact cleanup, health check
- **`~/.config/systemd/user/aihomecloud.service`**: Created user-level service (now superseded by system-level)
- **`/etc/avahi/services/aihomecloud.service`**: Deployed mDNS advertisement
- **`/etc/sudoers.d/aihomecloud`**: Installed passwordless sudo rules for storage ops

**Key lesson — sudo -n invariant:** All `run_command(["sudo", ...])` calls MUST include `"-n"`.
Without it, the call blocks for 30 s on a TTY-less daemon process (or on pytest). With `-n`, failure
is immediate (rc=1) and logged. The backend already had sudoers rules for this hardware so all storage
calls succeed in production.

**Key lesson — avahi setup is not automatic:** avahi-daemon may be installed but the mDNS service XML
file must be explicitly deployed to `/etc/avahi/services/`. The Flutter app uses `_aihomecloud-nas._tcp`
for instant discovery. Without it, discover takes 30+ seconds (full subnet scan).

**mDNS status:** Verified working — `avahi-browse -t _aihomecloud-nas._tcp` returns `AiHomeCloud on rockpi-4a` on both IPv4 and IPv6.

**Test results:** 256 passed, 0 failed (excludes `test_hardware_integration.py`).

**Docs updated:** `kb/hardware.md` (Rock Pi 4A added, Cubie-only refs fixed), `kb/setup-instructions.md` (full rewrite for new hardware), `kb/changelog.md` (this entry), `TASKS.md` (done items added).

---

## 2026-03-15 — Task 3+4: Remove AdGuard feature and rename all Cubie branding to AiHomeCloud

**Task 3 — AdGuard/AdBlock removal:**
- Deleted `lib/widgets/adblock_stats_widget.dart`, `backend/app/routes/adguard_routes.py`, `backend/tests/test_adguard.py`, `FIX_ADBLOCKING.md`, `scripts/install-adguard.sh`
- Removed `_AdBlockingCard` from `more_screen.dart`, `adGuardStatsSilentProvider` from `data_providers.dart`, AdGuard widget from `dashboard_screen.dart`, all 4 AdGuard methods from `services_network_api.dart`, `adguard` entry from `service_routes.py`, adguard settings from `config.py`, adguard router from `main.py`

**Task 4 — Cubie branding renamed to AiHomeCloud:**
- Android package: `com.cubiecloud.cubie_cloud` → `com.aihomecloud.app`; `MainActivity.kt` moved to new path; deep-link scheme `cubie://` → `aihomecloud://`
- Flutter package name: `cubie_cloud` → `aihomecloud` (pubspec.yaml + all 13 import files)
- `CubieDevice` → `AhcDevice` (models, routes, providers, tests, backend models)
- `CubieNotificationOverlay` → `AhcNotificationOverlay`; `isCubie` → `isAhc` in `DiscoveredHost`
- Backend env prefix: `CUBIE_` → `AHC_` across all Python files
- Logger names: `cubie.*` → `aihomecloud.*` in 23 Python files
- Paths: `/var/lib/cubie` → `/var/lib/aihomecloud`, `/opt/cubie` → `/opt/aihomecloud`
- Trash folder: `.cubie_trash` → `.ahc_trash`; serial format: `CUBIE-{mac}` → `AHC-{mac}`
- mDNS type: `_cubie-nas._tcp` → `_aihomecloud-nas._tcp`; QR scheme: `cubie://pair` → `aihomecloud://pair`
- Renamed `cubie-backend.service` → `aihomecloud.service`, `50-cubie-network.rules` → `50-aihomecloud-network.rules`
- Updated `deploy.sh`, `scripts/first-boot-setup.sh`, `backend/README.md`

**Docs updated:** `copilot-instructions.md`, `kb/architecture.md`, `kb/api-contracts.md`, `kb/changelog.md` — AdGuard entries removed, `CubieDevice`/`CubieNotificationOverlay` renamed, env prefix and path references corrected.

**Verified:** `dart analyze` 0 errors; `pytest` 251 passed, 3 pre-existing Windows failures.

**Known exception:** `backend/app/board.py` retains `"Radxa CUBIE A7A"` / `"Radxa CUBIE A7Z"` / `"cubie a7z"` — these are manufacturer hardware model strings read from `/proc/device-tree/model` and must not be changed.

---

## 2026-03-13 — Telegram bot polish: HTML messages, inline keyboard, 4 new commands

**Changes:** Full rewrite of `backend/app/telegram_bot.py` for improved UX.

**Behaviour changes:**
- All bot messages now use `parse_mode="HTML"` with bold/italic/code formatting
- File upload destination prompt replaced with inline keyboard (4 buttons: My Folder, Family Shared, Entertainment, Cancel) — keyboard is removed after tap
- Typing indicator (`send_chat_action`) added to `/list` and search handlers
- File type emojis shown in upload messages (📄/🖼️/🎬/🎵/🎤)
- Single success edit (status message edited in-place, no duplicate reply)
- Brand fix: "CubieCloud" → "AiHomeCloud" in all messages

**New commands:**
- `/status` — device health (CPU, RAM, temp, uptime, storage bar)
- `/cancel` — discard a pending file upload
- `/whoami` — show linked profile, Telegram handle, and personal folder
- `/unlink` — remove this Telegram account from AiHomeCloud

**`_handle_auth`:** Shows current personal folder name; allows switching folder with `/auth <name>`
**`_handle_help`:** Lists all 7 commands with HTML formatting

**Tests updated:** `backend/tests/test_telegram_bot.py` — 36 tests, all passing. Updated `_make_context()` to include `AsyncMock` for `bot.send_chat_action` and `bot.send_message`; replaced deprecated `_handle_pending_upload_choice` references with `_process_upload_choice`.

---

## 2026-03-13 — Ad Blocking state-machine fix

**Changes:** Implemented AdGuard setup/runtime state probing and rewired the More-screen Ad Blocking card to handle installation, app-enabled, service-running, and active-stats states explicitly.

**Backend changes:** Added `GET /api/v1/adguard/status` in `adguard_routes.py` returning `{installed, service_running, app_enabled}` without requiring `adguard_enabled=true`. Added test file `backend/tests/test_adguard.py` (4 tests passing).

**Frontend changes:** Added `getAdGuardStatus()` to `lib/services/api/services_network_api.dart`. Reworked `_AdBlockingCardState` in `lib/screens/main/more_screen.dart` to two-step load (`/status` then `/stats`), setup-instructions dialog, visible snackbar errors, service-stopped retry state, and refresh action in active stats card.

**Documentation updated:** Updated endpoint counts and AdGuard route docs in `kb/api-contracts.md`, `kb/architecture.md`, and `.github/copilot-instructions.md`.

---

## 2026-03-12 — Dashboard premium UI refactor

**Features completed:** Refactored `dashboard_screen.dart` UI layout to premium card-based structure while preserving existing Riverpod providers, API calls, and dark theme palette. Added reusable `AdBlockStatsWidget` and moved ad-block statistics into the Network card.

**Key decisions:**
- Kept state/data architecture unchanged (`deviceInfoProvider`, `systemStatsStreamProvider`, `storageDevicesProvider`, `networkStatusProvider`, `adGuardStatsSilentProvider`)
- Replaced compact system text row with a 4-ring metrics card (CPU, RAM, TEMP, UPTIME) using lightweight `CustomPainter`
- Simplified top status card to overall health messaging only (no hardware metric line)
- Embedded AdGuard stats under upload/download rows for clearer network context

---

## 2025-07-25 — Repository audit and KB rebuild

**Changes:** `copilot-instructions.md` rewritten from scratch. All `kb/` files verified against source code and updated. Created `kb/architecture.md`, `kb/features.md`, `kb/flutter-patterns.md`, `kb/backend-patterns.md`, `kb/changelog.md`. Rebuilt `kb/api-contracts.md` from 78 lines to full 62-endpoint reference. Created `tasks.md`. Removed stale root-level prompt artifacts.

**Key decisions:**
- `kb/changelog.md` replaces `logs.md` as the permanent record
- `tasks.md` replaces the non-existent `TASKSv2.md` reference
- Hardware doc corrected from "A7A" to "A7Z"
- Hardcoded IP `192.168.0.212` removed from documentation

**Inaccuracies corrected:**
- api-contracts.md had only 15 of 62 endpoints documented
- hardware.md title said "A7A" but board is Radxa Cubie A7Z
- copilot-instructions.md referenced non-existent `TASKSv2.md`
- copilot-instructions.md listed wrong onboarding screen names
- copilot-instructions.md missing 4 route files (adguard, tailscale, telegram, telegram_upload)
- critique.md bugs B1 (threading.Lock) and B3 (bcrypt blocking) already fixed in code

---

## 2025-07-25 — Trash overhaul

**Features completed:** Moved Trash from More tab to Files tab. Added auto-delete toggle (30-day limit, off by default). Added Telegram weekly trash warning (Saturday 10AM, >10 GB threshold).

**Key decisions:**
- Trash lives in Files tab, not More
- Auto-delete is opt-in, not default
- Quota-overflow purge always runs regardless of auto-delete setting

**Backend changes:** `file_routes.py` added `GET/PUT /api/v1/files/trash/prefs`. `telegram_bot.py` added `_trash_warning_loop()`.

**Frontend changes:** `files_screen.dart` got Trash card + `_TrashScreen`. `more_screen.dart` lost Trash section.

---

## 2025-07-25 — v5 Sprint (Tasks 0–7 + DLNA/SMB merge)

**Features completed:**
- `GET /network/status` endpoint
- StatTile overflow fix (aspect ratio 1.45→1.25)
- Family folder size fix (ensure personal folders exist before computing)
- Upload speed boost (chunk 1→4 MB, write buffer 256 KB→2 MB)
- Family tab moved into More screen, bottom nav reduced to 3 tabs
- Files tabs replaced with 2-folder explorer (Personal + Shared)
- Netflix-style avatar user picker on PIN entry screen
- DLNA + SMB merged into unified "TV & Computer Sharing" toggle

**Key decisions:**
- 3-tab nav: Home, Files, More (Family moved into More)
- Merged `samba` + `dlna` service toggles into single `media` service
- PIN entry uses avatar circles, not dropdown
- Files screen uses 2 folder cards, not 3-segment tab bar

**Commits:** `2fd4d7d`, `9640204`, `af399cb`, `9f0c835`, `fe674ee`, `22109d3`, `e3e139d`, `141d727`
## 2026-07-01 — Search re-scan repair + OCR reliability

- **`POST /api/v1/files/reindex`** (admin, background job): prune missing + re-scan; **`/reindex/cancel`**; job `progress {current,total}` on `GET /jobs/{id}`.
- **Re-scan repairs gaps:** re-OCRs files that are NEW or have EMPTY `ocr_text` (redo set) — no longer skips already-indexed-but-empty entries.
- **OCR reliability:** `OMP_NUM_THREADS=1` (single-thread tesseract so N parallel workers = N cores, no oversubscription), timeout 60→180s, **thermal-adaptive parallelism** (≤3 workers, fall back to 1 at ≥75°C), **downscale images ≤2200px before OCR**.
- Fixes: parallel multi-threaded tesseract was oversubscribing the CPU → 60s timeouts → empty OCR → unsearchable text.
