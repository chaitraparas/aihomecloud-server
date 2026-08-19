"""
Tests for _wipe_stale_nas_dirs / _has_real_content (auth_routes.py).

M5 audit regression: main.py's lifespan startup unconditionally creates family/.inbox/ on every
boot, before _wipe_stale_nas_dirs ever runs. The original emptiness check (any(d.rglob("*")))
treated that single always-present scaffolding dir as "real content," so the wipe silently
aborted on every installation, forever — a genuine first-time re-provision (e.g. after a lost
users.json) would never actually clean stale app dirs as intended.
"""

from app.config import settings
from app.routes.auth_routes import _has_real_content, _wipe_stale_nas_dirs


def _reset_nas(tmp_path):
    settings.data_dir = tmp_path
    settings.nas_root = tmp_path / "nas"


class TestHasRealContent:
    def test_genuinely_empty_dir_has_no_real_content(self, tmp_path):
        _reset_nas(tmp_path)
        d = settings.nas_root / "family"
        d.mkdir(parents=True)
        assert _has_real_content(d) is False

    def test_dir_containing_only_inbox_scaffolding_has_no_real_content(self, tmp_path):
        _reset_nas(tmp_path)
        d = settings.nas_root / "family"
        (d / ".inbox").mkdir(parents=True)
        (d / ".inbox" / "placeholder.txt").write_text("scaffolding")
        assert _has_real_content(d) is False, (
            "family/.inbox/ is app-created scaffolding created unconditionally on every "
            "startup — it must not count as real user data"
        )

    def test_dir_with_inbox_plus_a_real_file_has_real_content(self, tmp_path):
        _reset_nas(tmp_path)
        d = settings.nas_root / "family"
        (d / ".inbox").mkdir(parents=True)
        (d / "vacation.jpg").write_bytes(b"real photo bytes")
        assert _has_real_content(d) is True

    def test_dir_with_nested_real_file_has_real_content(self, tmp_path):
        _reset_nas(tmp_path)
        d = settings.nas_root / "personal" / "alice"
        (d / "Photos" / "2026").mkdir(parents=True)
        (d / "Photos" / "2026" / "photo.jpg").write_bytes(b"data")
        assert _has_real_content(d) is True


class TestWipeStaleNasDirs:
    def test_wipes_dirs_containing_only_inbox_scaffolding(self, tmp_path):
        """The exact audit scenario: a fresh install where main.py already created
        family/.inbox/ before first-user setup runs — the wipe must still proceed."""
        _reset_nas(tmp_path)
        family = settings.nas_root / "family"
        personal = settings.nas_root / "personal"
        (family / ".inbox").mkdir(parents=True)
        personal.mkdir(parents=True)

        _wipe_stale_nas_dirs()

        assert not family.exists(), "family/ (only .inbox scaffolding) must be wiped"
        assert not personal.exists(), "personal/ (genuinely empty) must be wiped"

    def test_refuses_to_wipe_when_real_content_present_alongside_inbox(self, tmp_path):
        """Safety must be preserved: real data anywhere still aborts the entire wipe."""
        _reset_nas(tmp_path)
        family = settings.nas_root / "family"
        personal = settings.nas_root / "personal"
        (family / ".inbox").mkdir(parents=True)
        (family / "shared_photo.jpg").write_bytes(b"real data")
        personal.mkdir(parents=True)

        _wipe_stale_nas_dirs()

        assert family.exists(), "must not wipe a dir with real content"
        assert (family / "shared_photo.jpg").exists(), "real file must survive untouched"
        assert personal.exists(), "abort must be all-or-nothing — personal/ must also survive"
