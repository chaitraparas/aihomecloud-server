"""Startup hygiene utilities for keeping user NAS storage clean."""

from __future__ import annotations

import logging
from pathlib import Path

from .config import settings
from .document_index import remove_documents_by_filename_patterns, remove_missing_documents

logger = logging.getLogger("aihomecloud.hygiene")

# Restrictive allowlist: only known test artifacts, never generic *.txt.
_TEST_ARTIFACT_PATTERNS: tuple[str, ...] = (
    "hwtest_*.txt",
    "stress_*.txt",
)


def _scan_test_artifacts() -> list[Path]:
    matches: list[Path] = []
    roots = [settings.personal_path, settings.family_path]
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for pattern in _TEST_ARTIFACT_PATTERNS:
            matches.extend(root.rglob(pattern))
    # Keep only files to avoid accidental directory deletion.
    return [p for p in matches if p.is_file()]


async def cleanup_startup_artifacts() -> dict[str, int]:
    """Remove known non-user test artifacts from NAS and document index."""
    files = _scan_test_artifacts()

    # Remove from document index first so index never points to non-existent files,
    # even if a subsequent file deletion fails.
    removed_pattern_rows = await remove_documents_by_filename_patterns(list(_TEST_ARTIFACT_PATTERNS))

    deleted = 0
    for p in files:
        try:
            p.unlink()
            deleted += 1
        except OSError:
            logger.warning("hygiene_delete_failed path=%s", p)

    removed_missing_rows = await remove_missing_documents()

    stats = {
        "deleted_files": deleted,
        "removed_pattern_rows": removed_pattern_rows,
        "removed_missing_rows": removed_missing_rows,
    }
    logger.info("hygiene_cleanup stats=%s", stats)
    return stats
