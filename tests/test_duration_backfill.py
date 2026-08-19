"""
Durations the index never had get filled in out of band.

`duration` arrived as a later column migration and the reconciler only revisits a file whose
(size, mtime) signature changed — so on the production board 334 of 334 videos had no duration and
never would have. The failure mode to guard against is not "the backfill does nothing" but "the
backfill never stops": a probe that fails must not put the file back in the queue forever.
"""

import pytest

from app import media_index


@pytest.fixture
async def indexed(client, tmp_path, monkeypatch):
    """One video with no duration, one photo, one video already probed."""
    await media_index.record_entry(
        rel_path="/family/Videos/holiday.mp4", content_hash="h1", size_bytes=10,
        scope="family", owner=None, category="Videos", media_type="video/mp4",
        source="test", source_folder=None, filename="holiday.mp4",
        original_name="holiday.mp4", capture_date=1.0, mtime=1.0, duration=None,
    )
    await media_index.record_entry(
        rel_path="/family/Photos/a.jpg", content_hash="h2", size_bytes=10,
        scope="family", owner=None, category="Photos", media_type="image/jpeg",
        source="test", source_folder=None, filename="a.jpg",
        original_name="a.jpg", capture_date=1.0, mtime=1.0, duration=None,
    )
    await media_index.record_entry(
        rel_path="/family/Videos/done.mp4", content_hash="h3", size_bytes=10,
        scope="family", owner=None, category="Videos", media_type="video/mp4",
        source="test", source_folder=None, filename="done.mp4",
        original_name="done.mp4", capture_date=1.0, mtime=1.0, duration=42.0,
    )


class TestFindingWhatIsMissing:
    async def test_only_unprobed_videos_are_returned(self, indexed):
        found = {rel for _, rel in await media_index.videos_missing_duration()}

        assert found == {"/family/Videos/holiday.mp4"}

    async def test_photos_are_never_queued(self, indexed):
        """A photo has no duration and never will; queueing it would never converge."""
        rels = {rel for _, rel in await media_index.videos_missing_duration()}

        assert not any(r.endswith(".jpg") for r in rels)

    async def test_the_limit_is_honoured(self, indexed):
        assert len(await media_index.videos_missing_duration(limit=0)) == 0


class TestRecordingTheResult:
    async def test_a_probed_duration_is_stored(self, indexed):
        [(entry_id, _)] = await media_index.videos_missing_duration()

        await media_index.set_duration(entry_id, 118.5)

        assert await media_index.videos_missing_duration() == []

    async def test_a_failed_probe_still_leaves_the_queue(self, indexed):
        """
        The convergence property. A file ffprobe cannot read today it cannot read tomorrow, so
        leaving it NULL would re-probe the same broken file on every single restart.
        """
        [(entry_id, _)] = await media_index.videos_missing_duration()

        await media_index.set_duration(entry_id, 0.0)

        assert await media_index.videos_missing_duration() == [], "0.0 means 'probed, no answer'"
