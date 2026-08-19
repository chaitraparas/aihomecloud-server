"""
Regression tests for the 2026-07-30 files findings closed on 2026-08-04.

Finding 4 — MISDIAGNOSED in the original report, which claimed `_calc_dir_size` "crashes
uncaught on PermissionError". It does not: `rglob` silently skips a directory it cannot read
and returns normally. The real defect is the opposite — a silent UNDER-COUNT, with the
unreadable subtree contributing zero and nothing saying so. Now walked with `os.walk`, whose
`onerror` makes the shortfall visible in the log. Deliberately not raised: the delete itself
still succeeds, since `shutil.move` renames the parent regardless of a child's permissions.

Finding 5 — `_scandir_list` returned an empty list when `scandir` itself failed, so an
unmounted drive rendered as "Empty folder" with no signal anything was wrong.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routes.file_routes import _calc_dir_size, _scandir_list


def _unreadable(path: Path) -> bool:
    """Chmod 000 does nothing when the test runs as root, which would silently no-op these."""
    path.chmod(0o000)
    try:
        os.listdir(path)
        return False
    except PermissionError:
        return True
    finally:
        pass


class TestCalcDirSize:

    def test_sums_a_readable_tree(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"12345")
        nested = tmp_path / "sub"
        nested.mkdir()
        (nested / "b.txt").write_bytes(b"123")
        assert _calc_dir_size(tmp_path) == 8

    def test_an_empty_tree_is_zero(self, tmp_path):
        assert _calc_dir_size(tmp_path) == 0

    def test_an_unreadable_subtree_is_skipped_and_logged_not_raised(self, tmp_path, caplog):
        # The original finding predicted a raise. Verified against Python 3.12: rglob (and
        # os.walk without onerror) simply skip it. So the contract asserted here is the real
        # one — the readable part is still counted, and the shortfall is logged.
        (tmp_path / "ok.txt").write_bytes(b"12345")
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "hidden.txt").write_bytes(b"xyz")

        if not _unreadable(locked):
            locked.chmod(0o755)
            pytest.skip("running as root — chmod 000 does not deny access")

        try:
            with caplog.at_level("WARNING"):
                total = _calc_dir_size(tmp_path)
            assert total == 5, "the readable part must still be counted"
            assert any("dir_size_undercounted" in r.getMessage() for r in caplog.records), \
                "an unreadable subtree must leave a trace"
        finally:
            locked.chmod(0o755)

    def test_a_file_vanishing_mid_walk_does_not_abort_the_total(self, tmp_path):
        # A race with another writer must not fail the whole calculation; only a directory
        # that cannot be read is fatal.
        (tmp_path / "a.txt").write_bytes(b"12345")
        dangling = tmp_path / "gone.txt"
        dangling.symlink_to(tmp_path / "does-not-exist")
        assert _calc_dir_size(tmp_path) == 5


class TestScandirList:

    def test_lists_a_readable_directory(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"1")
        (tmp_path / "sub").mkdir()
        paged, total = _scandir_list(tmp_path, str(tmp_path), "name", False, 0, 50)
        assert total == 2
        assert {e["name"] for e in paged} == {"a.txt", "sub"}

    def test_an_unreadable_directory_is_logged_rather_than_silently_empty(self, tmp_path, caplog):
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "inside.txt").write_bytes(b"1")

        if not _unreadable(locked):
            locked.chmod(0o755)
            pytest.skip("running as root — chmod 000 does not deny access")

        try:
            with caplog.at_level("WARNING"):
                paged, total = _scandir_list(locked, str(tmp_path), "name", False, 0, 50)
            # The return contract is unchanged on purpose — callers and the UI both expect a
            # list. What must not happen is failing in total silence.
            assert total == 0 and paged == []
            assert any("scandir_failed" in r.message or "scandir_failed" in r.getMessage()
                       for r in caplog.records), "unreadable folder should be logged"
        finally:
            locked.chmod(0o755)
