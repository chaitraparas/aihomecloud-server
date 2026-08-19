"""Tests for the shared/ → family/ migration logic in main.py lifespan."""
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def mock_settings_with_paths(tmp_path: Path, monkeypatch):
    """Create a Settings instance with NAS root pointing to a temporary directory."""
    from app import config

    nas_root = tmp_path / "nas"
    nas_root.mkdir()

    # Monkeypatch the real stored fields that the @property paths derive from
    monkeypatch.setattr(config.settings, "nas_root", nas_root)
    
    return config.settings, nas_root


def test_migration_moves_shared_to_family(mock_settings_with_paths):
    """Verify that shared/ is moved to family/ during migration."""
    settings, nas_root = mock_settings_with_paths

    # Set up old shared/ structure
    old_shared = nas_root / "shared"
    old_shared.mkdir()
    (old_shared / "document.pdf").write_text("shared doc content")
    (old_shared / "photo.jpg").write_text("shared photo content")

    # Ensure family/ doesn't exist yet (so migration triggers)
    family_path = settings.family_path
    assert not family_path.exists()

    # Run migration logic (extracted from main.py lifespan)
    _run_migration(settings)

    # Verify old_shared is gone
    assert not old_shared.exists(), "old shared/ should be deleted after migration"

    # Verify family/ now exists with the files
    assert family_path.exists(), "family/ should exist after migration"
    assert (family_path / "document.pdf").exists()
    assert (family_path / "document.pdf").read_text() == "shared doc content"
    assert (family_path / "photo.jpg").exists()
    assert (family_path / "photo.jpg").read_text() == "shared photo content"


def test_migration_moves_entertainment_subdirectory(mock_settings_with_paths):
    """Verify that shared/Entertainment/ is moved to entertainment/ separately."""
    settings, nas_root = mock_settings_with_paths

    # Set up old shared/Entertainment/ structure
    old_shared = nas_root / "shared"
    old_shared.mkdir()
    old_entertainment = old_shared / "Entertainment"
    old_entertainment.mkdir()

    (old_shared / "document.pdf").write_text("shared doc")
    (old_entertainment / "movie.mkv").write_text("movie content")
    (old_entertainment / "song.mp3").write_text("song content")

    family_path = settings.family_path
    entertainment_path = settings.entertainment_path
    assert not family_path.exists()
    assert not entertainment_path.exists()

    # Run migration
    _run_migration(settings)

    # Verify old shared/ is gone
    assert not old_shared.exists()

    # Verify entertainment/ has the files (not in family/)
    assert entertainment_path.exists()
    assert (entertainment_path / "movie.mkv").exists()
    assert (entertainment_path / "movie.mkv").read_text() == "movie content"
    assert (entertainment_path / "song.mp3").exists()
    assert (entertainment_path / "song.mp3").read_text() == "song content"

    # Verify family/ has the non-Entertainment files
    assert family_path.exists()
    assert (family_path / "document.pdf").exists()
    assert (family_path / "document.pdf").read_text() == "shared doc"
    # Entertainment subdir should NOT be in family
    assert not (family_path / "Entertainment").exists()


def test_migration_skipped_if_family_already_exists(mock_settings_with_paths):
    """Verify migration is skipped if family/ already exists (one-time only)."""
    settings, nas_root = mock_settings_with_paths

    # Create both old_shared and new family/ (migration should skip)
    old_shared = nas_root / "shared"
    old_shared.mkdir()
    (old_shared / "file.txt").write_text("will not be moved")

    family_path = settings.family_path
    family_path.mkdir(parents=True)
    (family_path / "existing.txt").write_text("existing family file")

    # Run migration
    _run_migration(settings)

    # Verify old shared/ still exists (was not moved)
    assert old_shared.exists()
    assert (old_shared / "file.txt").exists()

    # Verify existing family/ was not modified
    assert (family_path / "existing.txt").exists()
    assert not (family_path / "file.txt").exists()


def test_migration_with_nested_entertainment_subdirs(mock_settings_with_paths):
    """Verify that nested subdirectories in Entertainment/ are moved correctly."""
    settings, nas_root = mock_settings_with_paths

    # Create nested structure in shared/Entertainment/
    old_shared = nas_root / "shared"
    old_shared.mkdir()
    old_entertainment = old_shared / "Entertainment"
    (old_entertainment / "Movies" / "Action").mkdir(parents=True)
    (old_entertainment / "Movies" / "Action" / "film.mkv").write_text("film")
    (old_entertainment / "Music" / "Pop").mkdir(parents=True)
    (old_entertainment / "Music" / "Pop" / "track.mp3").write_text("track")

    (old_shared / "shared_file.docx").write_text("shared doc")

    family_path = settings.family_path
    entertainment_path = settings.entertainment_path
    assert not family_path.exists()
    assert not entertainment_path.exists()

    # Run migration
    _run_migration(settings)

    # Verify entertainment/ has nested structure
    assert (entertainment_path / "Movies" / "Action" / "film.mkv").exists()
    assert (entertainment_path / "Movies" / "Action" / "film.mkv").read_text() == "film"
    assert (entertainment_path / "Music" / "Pop" / "track.mp3").exists()
    assert (entertainment_path / "Music" / "Pop" / "track.mp3").read_text() == "track"

    # Verify family/ has the shared file
    assert (family_path / "shared_file.docx").exists()
    assert (family_path / "shared_file.docx").read_text() == "shared doc"

    # Verify old shared/ is gone
    assert not old_shared.exists()


def test_migration_handles_empty_shared_directory(mock_settings_with_paths):
    """Verify migration works with an empty shared/ directory."""
    settings, nas_root = mock_settings_with_paths

    old_shared = nas_root / "shared"
    old_shared.mkdir()
    # No files in shared

    family_path = settings.family_path
    assert not family_path.exists()

    # Run migration
    _run_migration(settings)

    # Verify old shared/ is gone and family/ exists (even if empty)
    assert not old_shared.exists()
    assert family_path.exists()


def test_collect_inboxes_includes_family_inbox(tmp_path: Path, monkeypatch):
    """Verify _collect_inboxes() finds family/.inbox/ after migration."""
    from app import file_sorter, config

    # Set up directory structure
    personal_dir = tmp_path / "personal"
    family_dir = tmp_path / "family"
    entertainment_dir = tmp_path / "entertainment"
    user_inbox = personal_dir / "alice" / ".inbox"
    family_inbox = family_dir / ".inbox"
    entertainment_inbox = entertainment_dir / ".inbox"

    for d in (user_inbox, family_inbox, entertainment_inbox):
        d.mkdir(parents=True)

    monkeypatch.setattr(config.settings, "nas_root", tmp_path)

    inboxes = file_sorter._collect_inboxes()
    inbox_dirs = [str(inbox) for inbox, _ in inboxes]

    # Verify family inbox is found
    assert str(family_inbox) in inbox_dirs, (
        f"family/.inbox/ not found in collected inboxes: {inbox_dirs}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helper: simulate migration logic from main.py
# ─────────────────────────────────────────────────────────────────────────────


def _run_migration(settings):
    """Execute the migration logic from main.py lifespan (synchronous version)."""
    old_shared = settings.nas_root / "shared"
    new_family = settings.family_path
    old_entertainment = old_shared / "Entertainment"
    new_entertainment = settings.entertainment_path

    if old_shared.exists() and not new_family.exists():
        try:
            if old_entertainment.exists():
                new_entertainment.mkdir(parents=True, exist_ok=True)
                for item in old_entertainment.iterdir():
                    shutil.move(str(item), str(new_entertainment / item.name))
                old_entertainment.rmdir()
            shutil.move(str(old_shared), str(new_family))
        except (OSError, shutil.Error) as e:
            raise RuntimeError(f"Migration failed: {e}")
