"""
Duplicate scanner — SHA-256 exact duplicates + perceptual-hash near-duplicates.

Exact (SHA-256):
  Two files with identical SHA-256 → guaranteed same content → safe to delete one.

Similar (dHash perceptual hash, Hamming ≤ 10):
  Images that look identical but were re-compressed (e.g. WhatsApp forward vs
  original camera shot).  Larger file = higher quality.

Schedules (configured in main.py):
  04:00 AM — full scan (both exact + similar)
  On upload — incremental check of the new file against cached hashes
  On demand — /scan Telegram command (admin only)

Results stored in kv.json:
  "duplicate_scan_results"  — list of exact-duplicate sets
  "similar_scan_results"    — list of similar-image sets
  "duplicate_scan_ran_at"   — ISO timestamp of last full scan

pHash cache stored in data_dir/phash.db (SQLite) to avoid re-hashing
unchanged files on subsequent scans.
"""

import asyncio
import hashlib
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import settings
from . import store

logger = logging.getLogger("aihomecloud.duplicate_scanner")

# ── tunables ─────────────────────────────────────────────────────────────────
_MIN_SIZE_BYTES = 1 * 1024           # skip files < 1 KB (empty/placeholder files)
_HASH_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB streaming reads for SHA-256
_PHASH_HAMMING_THRESHOLD = 10        # images with distance ≤ this are "similar"
_PHASH_MAX_IMAGES = 5_000            # union-find is O(n²) — cap to avoid multi-minute scans
_PHASH_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".bmp", ".tiff", ".tif",
})
_SKIP_DIR_NAMES = frozenset({".ahc_trash", ".inbox", ".git", "__pycache__", "lost+found"})

# ── optional pHash dependencies ───────────────────────────────────────────────
try:
    import imagehash as _imagehash
    from PIL import Image as _PILImage
    _PHASH_AVAILABLE = True
except ImportError:
    _imagehash = None  # type: ignore[assignment]
    _PILImage = None   # type: ignore[assignment]
    _PHASH_AVAILABLE = False
    logger.warning(
        "imagehash / Pillow not installed — similar-image scan disabled. "
        "Install with: pip install imagehash Pillow"
    )


# ── SHA-256 helpers ───────────────────────────────────────────────────────────

def _stream_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _owner_from_path(path: Path) -> str:
    try:
        rel = path.relative_to(settings.nas_root)
        parts = rel.parts
        if not parts:
            return "unknown"
        top = parts[0]
        if top == settings.personal_base and len(parts) > 1:
            return parts[1]
        return top
    except ValueError:
        return "unknown"


def _collect_files(root: Path) -> list[Path]:
    """Walk root recursively, skipping hidden/system dirs; return files ≥ _MIN_SIZE_BYTES."""
    result: list[Path] = []
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in _SKIP_DIR_NAMES and not entry.name.startswith("."):
                        stack.append(entry)
                elif entry.is_file():
                    if entry.stat().st_size >= _MIN_SIZE_BYTES:
                        result.append(entry)
            except OSError:
                continue
    return result


def _scan_sync(root: Path) -> list[dict]:
    """Blocking SHA-256 scan. Returns exact-duplicate sets sorted largest-first."""
    files = _collect_files(root)
    logger.info("duplicate_scan starting files_found=%d", len(files))
    hash_map: dict[str, list[Path]] = {}
    for path in files:
        try:
            digest = _stream_sha256(path)
        except (PermissionError, OSError):
            continue
        hash_map.setdefault(digest, []).append(path)

    results: list[dict] = []
    wasted = 0
    for digest, paths in hash_map.items():
        if len(paths) < 2:
            continue
        try:
            size = paths[0].stat().st_size
        except OSError:
            size = 0
        copies = [{"path": str(p), "owner": _owner_from_path(p)} for p in paths]
        results.append({"hash": digest, "filename": paths[0].name, "sizeBytes": size, "copies": copies})
        wasted += size * (len(paths) - 1)

    results.sort(key=lambda r: r["sizeBytes"], reverse=True)
    logger.info("duplicate_scan complete dup_sets=%d wasted_bytes=%d", len(results), wasted)
    return results


# ── pHash / SQLite cache ──────────────────────────────────────────────────────

def _phash_db_path() -> Path:
    return settings.data_dir / "phash.db"


def _phash_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_phash_db_path()), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS phash_cache (
            path      TEXT PRIMARY KEY,
            phash_hex TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            width     INTEGER DEFAULT 0,
            height    INTEGER DEFAULT 0,
            mtime_ns  INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _compute_phash_sync(path: Path) -> Optional[tuple[str, int, int]]:
    """Return (phash_hex, width, height) or None on failure."""
    if not _PHASH_AVAILABLE:
        return None
    try:
        img = _PILImage.open(path)
        width, height = img.size
        h = _imagehash.dhash(img)
        return str(h), width, height
    except Exception as exc:
        logger.debug("phash_failed path=%s error=%s", path.name, exc)
        return None


def _phash_hex_to_int(hex_str: str) -> int:
    return int(hex_str, 16)


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _collect_image_files(root: Path) -> list[Path]:
    """Like _collect_files but returns only image extensions."""
    result: list[Path] = []
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in _SKIP_DIR_NAMES and not entry.name.startswith("."):
                        stack.append(entry)
                elif entry.is_file():
                    if (entry.suffix.lower() in _PHASH_EXTENSIONS
                            and entry.stat().st_size >= _MIN_SIZE_BYTES):
                        result.append(entry)
            except OSError:
                continue
    return result


def _scan_sync_similar(root: Path) -> list[dict]:
    """Blocking pHash scan of a single root. Thin wrapper around _scan_sync_similar_files."""
    return _scan_sync_similar_files(_collect_image_files(root))


def _scan_sync_similar_files(
    images: list[Path],
    whitelist: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Core pHash scan over a pre-collected list of image paths.

    whitelist: set of sorted (phash_a, phash_b) tuples the user chose to keep —
    groups where every pair is whitelisted are omitted from results.
    """
    if not _PHASH_AVAILABLE:
        logger.info("similar_scan skipped — imagehash not installed")
        return []

    if len(images) > _PHASH_MAX_IMAGES:
        logger.warning(
            "similar_scan capped images_found=%d max=%d — only first %d scanned",
            len(images), _PHASH_MAX_IMAGES, _PHASH_MAX_IMAGES,
        )
        images = images[:_PHASH_MAX_IMAGES]

    logger.info("similar_scan starting images_found=%d", len(images))

    conn = _phash_conn()
    # entries: (path, phash_hex, phash_int, size_bytes, width, height)
    entries: list[tuple[Path, str, int, int, int, int]] = []

    try:
        for path in images:
            try:
                st = path.stat()
                mtime_ns = int(st.st_mtime_ns)
                size_bytes = int(st.st_size)
                path_str = str(path)

                row = conn.execute(
                    "SELECT phash_hex, width, height, mtime_ns, size_bytes FROM phash_cache WHERE path = ?",
                    (path_str,),
                ).fetchone()

                if row and row["mtime_ns"] == mtime_ns and row["size_bytes"] == size_bytes:
                    phash_hex = row["phash_hex"]
                    phash_int = _phash_hex_to_int(phash_hex)
                    width, height = row["width"], row["height"]
                else:
                    result = _compute_phash_sync(path)
                    if result is None:
                        continue
                    phash_hex, width, height = result
                    phash_int = _phash_hex_to_int(phash_hex)
                    conn.execute(
                        "INSERT OR REPLACE INTO phash_cache (path, phash_hex, size_bytes, width, height, mtime_ns)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (path_str, phash_hex, size_bytes, width, height, mtime_ns),
                    )
                    conn.commit()

                entries.append((path, phash_hex, phash_int, size_bytes, width, height))
            except OSError:
                continue
    finally:
        conn.close()

    # Union-Find grouping by Hamming distance
    n = len(entries)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _hamming(entries[i][2], entries[j][2]) <= _PHASH_HAMMING_THRESHOLD:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    similar_sets: list[dict] = []
    for group_indices in groups.values():
        if len(group_indices) < 2:
            continue
        # Sort group members by size desc (largest = highest quality)
        group_indices.sort(key=lambda i: entries[i][3], reverse=True)
        copies = []
        for i in group_indices:
            path, phash_hex, _, size_bytes, width, height = entries[i]
            copies.append({
                "path": str(path),
                "filename": path.name,
                "phash_hex": phash_hex,
                "size_bytes": size_bytes,
                "width": width,
                "height": height,
                "owner": _owner_from_path(path),
            })
        # Skip groups where every pair of copies has been whitelisted by the user
        if whitelist:
            group_phashes = [c["phash_hex"] for c in copies]
            all_whitelisted = all(
                tuple(sorted([group_phashes[a], group_phashes[b]])) in whitelist
                for a in range(len(group_phashes))
                for b in range(a + 1, len(group_phashes))
            )
            if all_whitelisted:
                continue
        similar_sets.append({"copies": copies})

    # Sort sets: largest primary file first
    similar_sets.sort(key=lambda s: s["copies"][0]["size_bytes"], reverse=True)
    logger.info("similar_scan complete similar_sets=%d", len(similar_sets))
    return similar_sets


def _check_single_sync(path: Path) -> list[dict]:
    """Check one newly-uploaded image against cached pHashes. Returns similar sets found."""
    if not _PHASH_AVAILABLE or path.suffix.lower() not in _PHASH_EXTENSIONS:
        return []
    result = _compute_phash_sync(path)
    if result is None:
        return []
    phash_hex, width, height = result
    new_int = _phash_hex_to_int(phash_hex)
    try:
        size_bytes = path.stat().st_size
        mtime_ns = int(path.stat().st_mtime_ns)
    except OSError:
        return []

    conn = _phash_conn()
    # Cache this new file
    conn.execute(
        "INSERT OR REPLACE INTO phash_cache (path, phash_hex, size_bytes, width, height, mtime_ns)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (str(path), phash_hex, size_bytes, width, height, mtime_ns),
    )
    conn.commit()  # must commit before close so the new entry persists

    # Find similar cached entries
    rows = conn.execute(
        "SELECT path, phash_hex, size_bytes, width, height FROM phash_cache WHERE path != ?",
        (str(path),),
    ).fetchall()
    conn.close()

    matches = []
    for row in rows:
        cached_int = _phash_hex_to_int(row["phash_hex"])
        if _hamming(new_int, cached_int) <= _PHASH_HAMMING_THRESHOLD:
            p = Path(row["path"])
            if p.exists():
                matches.append({
                    "path": row["path"],
                    "filename": p.name,
                    "size_bytes": row["size_bytes"],
                    "width": row["width"],
                    "height": row["height"],
                    "owner": _owner_from_path(p),
                })

    if not matches:
        return []

    # Build a set: new file + all matches, sorted by size desc
    all_copies = [{
        "path": str(path),
        "filename": path.name,
        "size_bytes": size_bytes,
        "width": width,
        "height": height,
        "owner": _owner_from_path(path),
    }] + matches
    all_copies.sort(key=lambda c: c["size_bytes"], reverse=True)
    return [{"copies": all_copies}]


# ── DuplicateScanner ──────────────────────────────────────────────────────────

class DuplicateScanner:
    def __init__(self) -> None:
        self._scanning = False

    @property
    def is_scanning(self) -> bool:
        return self._scanning

    async def _scan_nas_for_duplicates(self) -> tuple[list[dict], list[dict]]:
        """Full scan: SHA-256 exact + pHash similar. Persists results. Returns (exact, similar).

        Scans only user data directories (personal/, family/, entertainment/) — never
        backups/, system dirs, or SD card paths.
        """
        if self._scanning:
            logger.info("duplicate_scan already in progress — skipping")
            return [], []

        # Build list of user-data roots that actually exist
        user_roots: list[Path] = []
        # personal/<username>/ subdirs
        personal_base = settings.personal_path
        if personal_base.is_dir():
            for user_dir in personal_base.iterdir():
                if user_dir.is_dir() and not user_dir.name.startswith("."):
                    user_roots.append(user_dir)
        if settings.family_path.is_dir():
            user_roots.append(settings.family_path)
        if settings.entertainment_path.is_dir():
            user_roots.append(settings.entertainment_path)

        if not user_roots:
            logger.warning("duplicate_scan: no user data roots found — aborting")
            return [], []

        logger.info("duplicate_scan roots=%s", [str(r) for r in user_roots])

        self._scanning = True
        try:
            loop = asyncio.get_running_loop()

            def _exact_all() -> list[dict]:
                all_exact: list[dict] = []
                seen_hashes: dict[str, dict] = {}
                for root in user_roots:
                    for entry in _scan_sync(root):
                        h = entry["hash"]
                        if h in seen_hashes:
                            seen_hashes[h]["copies"].extend(entry["copies"])
                        else:
                            seen_hashes[h] = entry
                            all_exact.append(entry)
                # Re-filter: keep only sets with ≥2 copies after merge
                merged = [e for e in all_exact if len(e["copies"]) >= 2]
                merged.sort(key=lambda r: r["sizeBytes"], reverse=True)
                return merged

            # Load whitelist in async context before entering executor thread
            whitelist_raw = await store.get_value("similar_phash_whitelist", default=[])
            whitelist: set[tuple[str, str]] = {
                (pair[0], pair[1]) for pair in whitelist_raw
                if isinstance(pair, list) and len(pair) == 2
            }

            def _similar_all() -> list[dict]:
                # Collect images from all roots first, then do one union-find pass
                # so cross-root near-duplicates are caught too.
                all_images: list[Path] = []
                for root in user_roots:
                    all_images.extend(_collect_image_files(root))
                return _scan_sync_similar_files(all_images, whitelist=whitelist)

            exact = await loop.run_in_executor(None, _exact_all)
            similar = await loop.run_in_executor(None, _similar_all)
            await store.set_value("duplicate_scan_results", exact)
            await store.set_value("similar_scan_results", similar)
            await store.set_value("duplicate_scan_ran_at", datetime.now().isoformat())
            logger.info(
                "full_scan_complete exact_sets=%d similar_sets=%d",
                len(exact), len(similar),
            )
            return exact, similar
        finally:
            self._scanning = False

    async def scan_single_file_for_similar(self, path: Path) -> list[dict]:
        """Incremental check: compare one new file against cached pHashes."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: _check_single_sync(path))

    @staticmethod
    def _format_telegram_report(exact: list[dict], similar: list[dict]) -> Optional[str]:
        if not exact and not similar:
            return None

        def _fmt(b: int) -> str:
            if b < 1024 * 1024:
                return f"{b / 1024:.0f} KB"
            if b < 1024 ** 3:
                return f"{b / (1024 * 1024):.1f} MB"
            return f"{b / (1024 ** 3):.2f} GB"

        date_str = datetime.now().strftime("%d %b %Y")
        lines = [f"📊 <b>Duplicate Scan — {date_str}</b>\n"]

        if exact:
            wasted = sum(r["sizeBytes"] * (len(r["copies"]) - 1) for r in exact)
            lines.append(f"🔴 <b>Exact copies:</b> {len(exact)} sets · {_fmt(wasted)} recoverable")
        if similar:
            lines.append(f"🟡 <b>Similar images:</b> {len(similar)} sets (likely compressed vs original)")

        lines.append("\n<i>Use /duplicates to review and clean up.</i>")
        return "\n".join(lines)


_scanner = DuplicateScanner()


def get_duplicate_scanner() -> DuplicateScanner:
    return _scanner
