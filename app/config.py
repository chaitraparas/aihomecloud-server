"""
AiHomeCloud backend configuration.
All settings can be overridden via environment variables prefixed with AHC_.
"""

import os
import secrets
import socket
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

JWT_SECRET_FILE = Path("/var/lib/aihomecloud/jwt_secret")
PAIRING_KEY_FILE = Path("/var/lib/aihomecloud/pairing_key")
DEFAULT_CORS_ORIGINS = ["http://localhost", "http://localhost:3000"]


def _read_bundled_backend_version() -> str:
    """Reads the version written by the Android app's bundleBackendForInstaller Gradle task
    (a VERSION file placed alongside this one, containing the app's own versionName) -- present
    in every real deploy, since backend code only ever ships bundled inside the AiHomeCloud APK
    now (Play-only distribution policy, 2026-07-29). Falls back to "dev" when running from a
    plain git checkout with no bundle step (local development on a workstation)."""
    version_file = Path(__file__).parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "dev"


def generate_jwt_secret(secret_file: Path = JWT_SECRET_FILE) -> str:
    """Return the existing JWT secret or generate and atomically persist one.

    Uses O_CREAT|O_EXCL to avoid a TOCTOU race between checking existence and
    writing — only one concurrent starter writes the file; the other reads it.
    """
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    tmp = secret_file.with_suffix(".tmp")
    try:
        fd = os.open(str(secret_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, secret.encode())
        finally:
            os.close(fd)
        return secret
    except FileExistsError:
        # The file existing does not mean it holds a usable secret. O_CREAT creates it before
        # os.write fills it, so a power cut in that window — routine on an SBC someone unplugs —
        # leaves a zero-byte file that every later boot happily reads as "". PyJWT then refuses
        # ("HMAC key must not be empty") and the board cannot issue a single token: every login
        # fails, with an error that points at JWT rather than at a truncated file.
        # A disk-full short write reaches the same state. (2026-08-09 adversarial sweep.)
        existing = secret_file.read_text().strip()
        if len(existing) >= 32:
            return existing
        logger_ = __import__("logging").getLogger("aihomecloud.config")
        logger_.error(
            "jwt_secret at %s is empty or too short (%d chars) — regenerating. "
            "Existing sessions will be invalidated.", secret_file, len(existing),
        )
        secret_file.unlink(missing_ok=True)
        return generate_jwt_secret(secret_file)
    except Exception:
        # Clean up partial tmp if it exists, then re-raise
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def generate_pairing_key(key_file: Path = PAIRING_KEY_FILE) -> str:
    """Return the existing pairing key or generate and atomically persist one."""
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(16)
    try:
        fd = os.open(str(key_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, key.encode())
        finally:
            os.close(fd)
        return key
    except FileExistsError:
        # Same truncation window as the JWT secret, but this one fails OPEN rather than closed:
        # the pairing check is `hmac.compare_digest(body.key, settings.pairing_key)`, and
        # compare_digest("", "") is True — so an empty pairing key lets any caller satisfy it by
        # sending an empty string. Not reachable on the current fleet (all three boards set
        # AHC_PAIRING_KEY in the unit, so this generator never runs), but it is one unit-template
        # change away from being reachable. (2026-08-09 adversarial sweep.)
        existing = key_file.read_text().strip()
        if len(existing) >= 16:
            return existing
        logger_ = __import__("logging").getLogger("aihomecloud.config")
        logger_.error(
            "pairing key at %s is empty or too short (%d chars) — regenerating. "
            "Devices must pair again.", key_file, len(existing),
        )
        key_file.unlink(missing_ok=True)
        return generate_pairing_key(key_file)


def generate_device_serial() -> str:
    """Generate a device serial from the machine's MAC address."""
    import uuid
    mac = uuid.getnode()
    mac_hex = f"{mac:012x}".upper()
    return f"AHC-{mac_hex[-6:]}"


def generate_hotspot_password() -> str:
    """Generate a random 12-character hotspot password."""
    return secrets.token_urlsafe(9)  # yields 12 chars


def get_local_ip() -> str:
    """Get the device's primary local IP address.

    Tries interface enumeration first (works on LAN-only devices with no internet
    route).  Falls back to routing-based discovery, then 127.0.0.1.
    """
    # Method 1: Enumerate interfaces — works even without a default route.
    try:
        hostname = socket.gethostname()
        addrs = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for addr in addrs:
            ip = addr[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    # Method 2: Routing-based (original approach — requires default route).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


class Settings(BaseSettings):
    model_config = {"env_prefix": "AHC_"}

    # ── Server ────────────────────────────────────────────────────────────────
    # Intentional for appliance-style LAN service exposure.
    host: str = "0.0.0.0"  # nosec B104
    port: int = 8443
    log_level: str = "INFO"
    cors_origins: list[str] = DEFAULT_CORS_ORIGINS.copy()

    # ── TLS ────────────────────────────────────────────────────────────────────
    tls_enabled: bool = True
    tls_cert_file: str = ""  # auto-resolved to cert_dir/cert.pem if empty
    tls_key_file: str = ""   # auto-resolved to cert_dir/key.pem if empty

    # ── JWT ────────────────────────────────────────────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 1  # 1 hour — use refresh tokens for longer sessions

    # ── Device ────────────────────────────────────────────────────────────────
    device_serial: str = ""  # auto-generated from MAC address if empty
    device_name: str = "My AiHomeCloud"
    # Tracks the Android app's own versionName (android/app/build.gradle.kts) now, not an
    # independent number — see _read_bundled_backend_version() above. Was previously tracked
    # independently ("different codebases, different release cadences" was the reasoning), but
    # that stopped being true once backend code started shipping bundled inside the APK itself
    # (bundleBackendForInstaller) with Play as the sole distribution channel for both: they now
    # always ship together as one artifact, so a mismatched number here would just be confusing,
    # not meaningful. Previously "firmware_version" — renamed since that name implied
    # OS/bootloader-level updates, not this backend's own code version.
    backend_version: str = Field(default_factory=_read_bundled_backend_version)
    pairing_key: str = ""  # auto-generated and persisted if empty

    # ── Storage ───────────────────────────────────────────────────────────────
    nas_root: Path = Path("/srv/nas")
    personal_base: str = "personal"
    family_dir: str = "family"
    entertainment_dir: str = "entertainment"
    total_storage_gb: float = 500.0
    skip_mount_check: bool = False  # set True in tests to bypass is_mount()

    # Secondary drive for local_backup.py's selective folder backup (a USB stick/SD
    # card, not another full-size NAS drive — see docs on why this is a separate
    # mountpoint from nas_root, not a subdirectory of it: the whole point is that a
    # single physical failure of the primary drive must not also take out the copy).
    backup_root: Path = Path("/mnt/ahc_backup")

    # ── Upload ────────────────────────────────────────────────────────────────
    upload_chunk_size: int = 4 * 1024 * 1024  # 4 MB — fewer async cycles on ARM
    # Uploads per minute per client. The old 120 was a hard ceiling of 2 files/second, which is
    # below what a phone syncing its own camera roll needs: measured against this board, 4
    # concurrent uploaders had 47 of 60 requests rejected with 429 and throughput *fell* below
    # sequential. The limit exists to bound abuse, not to pace a paired device backing up its own
    # photos — LocalSend, solving the same problem, rate-limits sessions rather than files.
    upload_rate_limit: str = "1200/minute"

    # --- workload priority -------------------------------------------------
    # User activity preempts background work. See app/workload.py.
    workload_yield_enabled: bool = True
    # How long after the last user request the board counts as quiet. Doubled automatically on a
    # memory-tight board, where guessing wrong is far more expensive.
    workload_quiet_seconds: float = 5.0
    # Below this much available RAM, treat the board as tight. The Cubie A5E sits at ~546 MB
    # available with 66 MB already in swap, so it lands on the cautious side of this line; the
    # ROCK Pi at ~3.1 GB does not.
    workload_low_memory_mb: int = 800
    max_upload_bytes: int = 25 * 1024 * 1024 * 1024  # 25 GB (0 = unlimited)
    # M-4 fix (security audit 2026-08): max_upload_bytes bounds the request body at the
    # transport layer (UploadSizeLimitMiddleware) but is sized for real file uploads, not a
    # profile picture — /users/avatar used to read the whole body into one in-memory `bytes`
    # object with no cap of its own, so anything up to that 25 GB transport limit was a legal
    # request that would OOM a 1 GB-RAM board. 10 MB is generous for any real avatar image.
    max_avatar_bytes: int = 10 * 1024 * 1024  # 10 MB

    # ── Document indexing / OCR ────────────────────────────────────────────────
    # Enabled by default — tesseract (images) + pdftotext (PDFs) must be installed.
    # Disable via AHC_OCR_ENABLED=false if tools are not available.
    ocr_enabled: bool = True
    ocr_languages: str = "eng+hin"             # AHC_OCR_LANGUAGES — tesseract lang codes ('+' separated)
    document_index_pool_size: int = 3          # AHC_DOCUMENT_INDEX_POOL_SIZE — SQLite connection pool
    document_index_cache_ttl: int = 300        # AHC_DOCUMENT_INDEX_CACHE_TTL — search cache TTL (seconds)
    document_index_interval: int = 20          # AHC_DOCUMENT_INDEX_INTERVAL — watcher polling interval (seconds)
    media_index_pool_size: int = 2              # AHC_MEDIA_INDEX_POOL_SIZE — SQLite connection pool for media.db

    # ── Semantic search ────────────────────────────────────────────────────────
    # Which embedding model this board uses. Boards differ by an order of magnitude in RAM and
    # CPU, so this is per-board rather than global — but see embedding.MODELS before changing it:
    # every model has a different vector dimension, so switching one forces a full re-embed of
    # that board's library. Vectors from two models are not comparable and are never mixed.
    embedding_model: str = "bge-small-en-v1.5"   # AHC_EMBEDDING_MODEL
    embedding_batch_size: int = 32               # AHC_EMBEDDING_BATCH_SIZE — items per inference call
    embedding_threads: int = 4                   # AHC_EMBEDDING_THREADS — never all cores; the board also serves files
    embedding_quantise: bool = False             # AHC_EMBEDDING_QUANTISE — int8 storage: 4x less RAM, ~2x slower search
    # A full index of 10 lakh items is hours of pinned CPU on a passively cooled board that is
    # also serving video. This is the duty cycle: sleep this many milliseconds after each batch.
    # 0 disables throttling. Measured effect is in docs/PHASE4_PRODUCTION_SCALE_PROGRESS.md.
    embedding_throttle_ms: int = 50              # AHC_EMBEDDING_THROTTLE_MS
    # Work per pass. A first run on a huge library produces slices rather than one enormous list,
    # so progress is visible early and a cancel never loses more than one slice.
    embedding_slice_size: int = 20_000           # AHC_EMBEDDING_SLICE_SIZE

    # ── Auth ───────────────────────────────────────────────────────────────────
    bcrypt_rounds: int = 10                    # AHC_BCRYPT_ROUNDS — bcrypt work factor (10 ≈ 0.1s on ARM)

    # ── Event bus ─────────────────────────────────────────────────────────────
    event_queue_size: int = 100               # AHC_EVENT_QUEUE_SIZE — per-subscriber queue depth
    event_max_recent: int = 50                # AHC_EVENT_MAX_RECENT — recent events kept in memory

    # ── Job store ─────────────────────────────────────────────────────────────
    job_max_count: int = 100                  # AHC_JOB_MAX_COUNT — max tracked jobs
    job_ttl_hours: int = 1                    # AHC_JOB_TTL_HOURS — job retention window

    # ── File auto-sorting ──────────────────────────────────────────────────────
    # Disabled by default — polls every 30s and walks .inbox/ directories.
    # Enable via AHC_AUTO_SORT_ENABLED=true or use the /files/sort-now endpoint.
    auto_sort_enabled: bool = False

    # ── Telegram Bot (optional — disabled if token is empty) ─────────────────
    telegram_bot_token: str = ""   # AHC_TELEGRAM_BOT_TOKEN
    telegram_allowed_ids: str = ""  # AHC_TELEGRAM_ALLOWED_IDS — comma-sep chat IDs; empty = no restriction
    telegram_api_id: int = 0          # from my.telegram.org — needed for local server
    telegram_api_hash: str = ""       # from my.telegram.org — needed for local server
    telegram_local_api_enabled: bool = False   # True when local server is running
    telegram_local_api_url: str = "http://127.0.0.1:8081"  # local server address
    telegram_download_timeout: int = 600  # AHC_TELEGRAM_DOWNLOAD_TIMEOUT — seconds for file transfers
    # Separate from telegram_download_timeout above: that one bounds each individual
    # socket operation (connect/read/write/pool), which does NOT catch a connection that
    # trickles bytes slowly enough to keep resetting the per-op timer. This bounds the gap
    # between actual bytes landing on disk — a download that stalls for this long is killed
    # and cleaned up instead of hanging indefinitely with no self-recovery.
    telegram_stall_timeout: int = 60  # AHC_TELEGRAM_STALL_TIMEOUT — seconds of zero progress before abort


    # ── Data (JSON-file-based persistence for users, services, etc.) ─────────
    data_dir: Path = Path("/var/lib/aihomecloud")

    # ── Backend self-update (distinct from app_update_dir below, which serves the ANDROID
    # APK) -- POST /system/update stages an uploaded backend_bundle.tar here, then triggers
    # scripts/ahc-apply-backend-update.sh (root, outside this process's own sandbox) to extract,
    # verify, and symlink-swap it live. Real Settings fields (not data_dir-relative
    # properties, unlike most paths below) because the root-context apply script needs a fixed,
    # install.sh-created location independent of whatever AHC_DATA_DIR this particular process
    # instance was started with -- overridable via AHC_UPDATE_STAGING_DIR/AHC_UPDATE_STATUS_FILE
    # for tests, same convention as every other AHC_-prefixed setting in this class.
    update_staging_dir: Path = Path("/opt/aihomecloud/update_staging")
    # Lives INSIDE update_staging_dir deliberately, not as a standalone /opt/aihomecloud/*
    # path -- ReadWritePaths= in aihomecloud.service only whitelists update_staging_dir's
    # whole tree, so any file created inside it is automatically writable under
    # ProtectSystem=strict with no extra systemd config needed. A standalone sibling path
    # would need its own ReadWritePaths= entry, and (found live 2026-08-01) systemd's
    # handling of a whitelisted path that doesn't already exist on first boot is exactly
    # the kind of assumption this project's own convention says to verify on hardware, not
    # infer -- keeping this inside an already-covered directory sidesteps the question
    # entirely rather than relying on that unverified behavior.
    update_status_file: Path = Path("/opt/aihomecloud/update_staging/update_status")

    @property
    def personal_path(self) -> Path:
        return self.nas_root / self.personal_base

    @property
    def family_path(self) -> Path:
        return self.nas_root / self.family_dir

    @property
    def entertainment_path(self) -> Path:
        return self.nas_root / self.entertainment_dir

    @property
    def avatars_dir(self) -> Path:
        # Shared (NOT under personal/) so every family member's device can render
        # the profile picker — _authorize_path treats non-personal paths as readable.
        return self.nas_root / ".avatars"

    @property
    def users_file(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def services_file(self) -> Path:
        return self.data_dir / "services.json"

    @property
    def storage_file(self) -> Path:
        return self.data_dir / "storage.json"

    @property
    def tokens_file(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def cert_dir(self) -> Path:
        return self.data_dir / "tls"

    @property
    def tls_cert_path(self) -> Path:
        return Path(self.tls_cert_file) if self.tls_cert_file else self.cert_dir / "cert.pem"

    @property
    def tls_key_path(self) -> Path:
        return Path(self.tls_key_file) if self.tls_key_file else self.cert_dir / "key.pem"

    @property
    def identity_statement_path(self) -> Path:
        """Root-owned, root-written (ahc-issue-cert.sh) signed SPKI rotation statement — see
        docs/security/audit-2026-08/H-11_SPKI_ROTATION_DESIGN.md. Served verbatim, unauthenticated,
        by GET /api/v1/system/identity; this service never writes to this path."""
        return self.data_dir / "identity" / "statement.json"

    @property
    def app_update_dir(self) -> Path:
        """Where a new Android app build + manifest.json are placed to publish an update from
        this board -- each board serves its own independently (see system_routes.py's
        /app-update endpoints). Not auto-created; the deploy step creates it (mkdir -p) when
        the first release is published."""
        return self.data_dir / "app_updates"

    @property
    def trash_dir(self) -> Path:
        """Hidden trash directory at the root of the NAS mount."""
        return self.nas_root / ".ahc_trash"

    @property
    def trash_file(self) -> Path:
        """JSON metadata file for trash items."""
        return self.data_dir / "trash.json"

    @property
    def index_watcher_state_file(self) -> Path:
        """JSON snapshot of document index watcher state (persisted across restarts)."""
        return self.data_dir / "index_watcher_state.json"

    @property
    def jobs_file(self) -> Path:
        """JSON file for persisting long-running job status across restarts."""
        return self.data_dir / "jobs.json"

    @property
    def activity_log_file(self) -> Path:
        """JSON file for the persisted, queryable activity/audit trail (app/audit.py)."""
        return self.data_dir / "activity_log.json"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        """Accept comma-separated env var values for CORS origins.

        A wildcard "*" origin is rejected outright: main.py hardcodes
        allow_credentials=True on the CORS middleware, and browsers already refuse
        credentialed requests against a wildcard origin per the CORS spec (so "*" here would
        just quietly break every browser client) — but nothing stops an operator from setting
        AHC_CORS_ORIGINS=* anyway, and a non-browser HTTP client wouldn't necessarily enforce
        that spec restriction, so it's still worth failing loudly at startup rather than
        shipping a config that's simultaneously non-functional and a credential-exposure risk.
        """

        def _reject_wildcard(origins: list[str]) -> list[str]:
            if "*" in origins:
                raise ValueError(
                    "AHC_CORS_ORIGINS cannot include '*' — this server always sends "
                    "allow_credentials=True, and a wildcard origin combined with credentials "
                    "is rejected by browsers and is a credential-exposure risk for any client "
                    "that doesn't enforce that. List explicit origins instead."
                )
            return origins

        if value is None:
            return DEFAULT_CORS_ORIGINS.copy()

        if isinstance(value, str):
            origins = [item.strip() for item in value.split(",") if item.strip()]
            return _reject_wildcard(origins) or DEFAULT_CORS_ORIGINS.copy()

        if isinstance(value, list):
            return _reject_wildcard(value) or DEFAULT_CORS_ORIGINS.copy()

        return DEFAULT_CORS_ORIGINS.copy()


settings = Settings()

# Ensure a persistent JWT secret exists when the env var is not provided.
if not os.getenv("AHC_JWT_SECRET") and settings.jwt_secret == "change-me-in-production":
    try:
        settings.jwt_secret = generate_jwt_secret(settings.data_dir / "jwt_secret")
    except PermissionError:
        # CI / test environment — can't write to disk, use in-memory secret
        settings.jwt_secret = secrets.token_hex(32)
    except Exception:
        import logging as _logging
        _logging.getLogger("aihomecloud.config").critical(
            "FATAL: Cannot generate JWT secret and no AHC_JWT_SECRET env var set. "
            "Refusing to start with insecure default."
        )
        raise SystemExit(1)

# Auto-generate pairing key if not provided.
if not settings.pairing_key:
    try:
        settings.pairing_key = generate_pairing_key(settings.data_dir / "pairing_key")
    except PermissionError:
        settings.pairing_key = secrets.token_urlsafe(16)

# Auto-generate device serial from MAC address if not provided.
if not settings.device_serial:
    settings.device_serial = generate_device_serial()
