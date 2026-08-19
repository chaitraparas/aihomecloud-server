"""
M8 audit: media_index (SQL) and ingest.py's ingest_hashes pruning (pure Python) independently
implemented "does this path belong under this prefix", run together on every profile delete
(auth_routes.delete_my_profile) against the same prefix argument. Writing this test's
equivalence cases caught a real, live divergence: SQLite's LIKE is case-insensitive by default,
so the SQL-only version matched "/personal/alicE/" against a "/personal/alice" prefix, while
the Python version (plain str.startswith) correctly rejected it.

Fixed by making media_index.path_is_under_prefix the single canonical rule both stores defer
to — ingest.py imports it directly (pure Python, cheap), and media_index's own
_mark_deleted_by_prefix_sync uses a broad, deliberately over-inclusive SQL LIKE scan only to
cheaply narrow candidates on an indexed column, then re-checks every candidate against this
exact function before deleting anything. This test now guards two things: that the shared
predicate itself is correct (TestPythonPredicate), and that media_index's actual delete
behavior matches what the predicate says for the same cases (TestMediaIndexMatchesPredicate) —
so a future change to the candidate-narrowing SQL can never silently reintroduce a mismatch.
"""

import pytest

from app import media_index
from app.media_index import path_is_under_prefix


CASES: list[tuple[str, str, bool]] = [
    # (path, prefix, expected membership)
    ("/personal/alice", "/personal/alice", True),  # exact match
    ("/personal/alice/photo.jpg", "/personal/alice", True),  # direct child
    ("/personal/alice/Photos/2026/photo.jpg", "/personal/alice", True),  # nested child
    ("/personal/alice2/photo.jpg", "/personal/alice", False),  # sibling sharing a string prefix
    ("/personal/alicE/photo.jpg", "/personal/alice", False),  # case-sensitive, not a match
    ("/personal/ali/photo.jpg", "/personal/alice", False),  # unrelated shorter sibling
    ("/personal/alice", "/personal/alice/", False),  # prefix itself has a trailing slash
    ("/personal/other", "/personal/alice", False),  # unrelated path entirely
    # LIKE metacharacters in the prefix must be treated as literal text, not wildcards.
    ("/personal/a_b/photo.jpg", "/personal/a_b", True),
    ("/personal/axb/photo.jpg", "/personal/a_b", False),  # "_" must not act as a wildcard
    ("/personal/a%b/photo.jpg", "/personal/a%b", True),
    ("/personal/aXYb/photo.jpg", "/personal/a%b", False),  # "%" must not act as a wildcard
]


class TestPythonPredicate:
    @pytest.mark.parametrize("path,prefix,expected", CASES)
    def test_path_is_under_prefix(self, path, prefix, expected):
        assert path_is_under_prefix(path, prefix) == expected


class TestMediaIndexMatchesPredicate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path,prefix,expected", CASES)
    async def test_media_index_prefix_delete_matches_predicate(
        self, client, path, prefix, expected
    ):
        """Seeds one real media_index row at *path*, prunes by *prefix* via the actual
        production function, and asserts the row was (or wasn't) deleted exactly as
        path_is_under_prefix predicts for the same (path, prefix) pair."""
        await media_index.record_entry(
            rel_path=path,
            content_hash=f"hash-{abs(hash((path, prefix)))}",
            size_bytes=100,
            scope="personal",
            owner="alice",
            category="Photos",
            media_type="image/jpeg",
            source="direct_upload",
            filename=path.rsplit("/", 1)[-1] or "root",
        )
        entries_before = await media_index.query_entries(scope="personal", owner="alice")
        seeded = next((e for e in entries_before if e["filename"] == (path.rsplit("/", 1)[-1] or "root")), None)
        assert seeded is not None, "seeding failed"

        await media_index.mark_entries_deleted_by_prefix(prefix)

        entries_after = await media_index.query_entries(scope="personal", owner="alice")
        still_present = any(e["id"] == seeded["id"] for e in entries_after)

        assert still_present == (not expected), (
            f"media_index prefix-delete for path={path!r} prefix={prefix!r} "
            f"disagreed with path_is_under_prefix (expected deleted={expected})"
        )
