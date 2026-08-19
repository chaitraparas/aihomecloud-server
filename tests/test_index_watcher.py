import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import settings


def _setup_nas(tmp_path: Path) -> Path:
    nas = tmp_path / "nas"
    (nas / "family" / "Documents").mkdir(parents=True)
    (nas / "personal" / "alice" / "Documents").mkdir(parents=True)
    settings.nas_root = nas
    return nas


def test_scan_documents_sync_only_indexable(tmp_path):
    """Watcher indexes all dirs under user/family/entertainment, skipping non-indexable types."""
    nas = _setup_nas(tmp_path)
    (nas / "family" / "Other").mkdir(parents=True)

    good1 = nas / "family" / "Documents" / "a.pdf"
    good2 = nas / "personal" / "alice" / "Documents" / "b.jpg"
    # notes.txt in family/Other is now indexed — watcher covers full family/ tree
    good3 = nas / "family" / "Other" / "notes.txt"
    skip1 = nas / "family" / "Documents" / "movie.mp4"  # non-indexable extension

    good1.write_text("pdf")
    good2.write_text("jpg")
    good3.write_text("text note")
    skip1.write_text("video")

    from app.index_watcher import _scan_documents_sync

    state = _scan_documents_sync()
    keys = set(state.keys())
    assert str(good1.resolve()) in keys
    assert str(good2.resolve()) in keys
    assert str(good3.resolve()) in keys   # now indexed — outside Documents/ is fine
    assert str(skip1.resolve()) not in keys  # .mp4 is never indexed


@pytest.mark.asyncio
async def test_sync_once_indexes_new_and_removes_deleted(tmp_path):
    nas = _setup_nas(tmp_path)
    f1 = nas / "personal" / "alice" / "Documents" / "doc1.txt"
    f2 = nas / "family" / "Documents" / "doc2.pdf"
    f1.write_text("hello")
    f2.write_text("world")

    from app.index_watcher import sync_once

    with patch("app.index_watcher.index_document", new=AsyncMock()) as mock_index, \
         patch("app.index_watcher.remove_document", new=AsyncMock()) as mock_remove:
        state1 = await sync_once(previous_state={})
        assert mock_index.await_count == 2
        assert mock_remove.await_count == 0

        # Delete one file and modify one file.
        f2.unlink()
        f1.write_text("hello v2")

        state2 = await sync_once(previous_state=state1)
        assert str(f1.resolve()) in state2
        assert str(f2.resolve()) not in state2

        # One remove call for deleted file; one re-index call for modified file.
        remove_args = [c.args[0] for c in mock_remove.await_args_list]
        assert str(f2.resolve()) in remove_args

        # Total index calls: initial 2 + 1 modified file on second pass.
        assert mock_index.await_count == 3


@pytest.mark.asyncio
async def test_sync_once_sets_added_by_from_path(tmp_path):
    nas = _setup_nas(tmp_path)
    alice_doc = nas / "personal" / "alice" / "Documents" / "x.txt"
    family_doc = nas / "family" / "Documents" / "y.txt"
    alice_doc.write_text("a")
    family_doc.write_text("b")

    from app.index_watcher import sync_once

    with patch("app.index_watcher.index_document", new=AsyncMock()) as mock_index:
        await sync_once(previous_state={})

    called = {(c.args[1], c.args[2]) for c in mock_index.await_args_list}
    assert ("x.txt", "alice") in called
    assert ("y.txt", "family") in called


def test_load_persisted_state_missing_file(tmp_path):
    """Returns None when state file doesn't exist."""
    from app.index_watcher import _load_persisted_state
    from app.config import settings
    settings.data_dir = tmp_path / "no_such_dir"
    result = _load_persisted_state()
    assert result is None


def test_save_and_load_persisted_state(tmp_path):
    """Round-trip: saved state can be loaded back."""
    from app.index_watcher import _load_persisted_state, _save_persisted_state
    from app.config import settings
    settings.data_dir = tmp_path

    state = {"/some/path/file.txt": (1234567890, 2048)}
    _save_persisted_state(state)
    loaded = _load_persisted_state()
    assert loaded == state


def test_load_persisted_state_corrupt_file(tmp_path):
    """Returns None for corrupt JSON without raising."""
    from app.index_watcher import _load_persisted_state
    from app.config import settings
    settings.data_dir = tmp_path
    (tmp_path / "index_watcher_state.json").write_text("{not valid json")
    result = _load_persisted_state()
    assert result is None
