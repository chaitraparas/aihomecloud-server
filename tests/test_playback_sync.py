"""
Playback-position synchronisation semantics.

The governing invariant:

    The board's clock must never determine which client's offline playback state wins.

These boards are SBCs without an RTC — their clock comes from NTP and can be wrong or jump
backwards. So conflict resolution compares the *client-supplied* timestamp of the stored state
against the *client-supplied* timestamp of the incoming report, and never consults
`playback_positions.updated_at` (which this board writes) for ordering.

`version` is a synchronisation counter, not a time. It says "the stored state changed". It is
never used to decide which state is newer.

Several tests here are written specifically to fail against two tempting wrong implementations:

  * **`max(position)`** — rejected because rewinding is legitimate. The furthest position is not
    the newest state.
  * **ordering by the board's `updated_at`** — rejected because it compares a phone's clock to an
    SBC's, which are unrelated timebases.
"""

import pytest

from app import media_index

T0 = 1_700_000_000_000       # a client's epoch-ms clock
MINUTE = 60_000


@pytest.fixture
async def entry(client):
    """One indexed video. `client` is required: it points the DBs at a per-test tmp_path."""
    await media_index.record_entry(
        rel_path="/entertainment/Movies/sync.mkv", content_hash="sync1", size_bytes=10,
        scope="entertainment", owner=None, category="Movies", media_type="video/x-matroska",
        source="test", source_folder=None, filename="sync.mkv", original_name="sync.mkv",
        capture_date=1.0, mtime=1.0, duration=3600.0,
    )
    e = await media_index.get_entry(1)
    return e["id"]


async def _put(username, entry_id, position, ts, duration=3600.0):
    return await media_index.set_position(username, entry_id, position, duration, ts)


class TestConflictResolution:
    async def test_a_newer_client_report_wins_and_advances_the_version(self, entry):
        first = await _put("paras", entry, 100.0, T0)
        second = await _put("paras", entry, 200.0, T0 + MINUTE)

        assert first == {"version": 1, "cleared": False, "applied": True}
        assert second == {"version": 2, "cleared": False, "applied": True}
        assert (await media_index.positions("paras"))[0]["position"] == 200.0

    async def test_a_rewind_from_a_newer_client_report_wins(self, entry):
        """
        The case that kills `max(position)`.

        Rewinding is a normal thing to do. A later report at an EARLIER position is the newest
        state and must be stored. An implementation that keeps the furthest position fails here.
        """
        await _put("paras", entry, 1800.0, T0)
        await _put("paras", entry, 300.0, T0 + MINUTE)

        stored = (await media_index.positions("paras"))[0]
        assert stored["position"] == 300.0, "a legitimate rewind was discarded — max() semantics"

    async def test_a_stale_report_never_overwrites_newer_stored_state(self, entry):
        """A device offline for days must not clobber what another device did yesterday."""
        await _put("paras", entry, 900.0, T0 + 10 * MINUTE)
        result = await _put("paras", entry, 120.0, T0)          # the stale flush

        assert result["applied"] is False
        assert result["version"] == 1, "a refused write must not advance the version"
        assert (await media_index.positions("paras"))[0]["position"] == 900.0

    async def test_equal_timestamps_do_not_overwrite(self, entry):
        """
        Equal is not newer.

        This is also what makes a retry idempotent, so the rule is load-bearing in two places.
        """
        await _put("paras", entry, 100.0, T0)
        result = await _put("paras", entry, 500.0, T0)

        assert result["applied"] is False
        assert (await media_index.positions("paras"))[0]["position"] == 100.0

    async def test_a_retried_identical_report_is_a_no_op(self, entry):
        """Network timeout then retry: same version back, no state transition."""
        first = await _put("paras", entry, 640.0, T0)
        retry = await _put("paras", entry, 640.0, T0)

        assert retry["applied"] is False
        assert retry["version"] == first["version"]
        assert (await media_index.positions("paras"))[0]["position"] == 640.0

    async def test_the_boards_own_clock_is_not_used_for_ordering(self, entry, monkeypatch):
        """
        The invariant, tested directly.

        Two reports are written; the second carries an OLDER client timestamp. Whatever the board's
        own `updated_at` says — and it will say the second write is later, because that column is
        `strftime('now')` — the older client state must lose.
        """
        await _put("paras", entry, 800.0, T0 + MINUTE)
        await _put("paras", entry, 50.0, T0)

        rows = await media_index.positions("paras")
        assert rows[0]["position"] == 800.0, (
            "the later-arriving report won despite an older client timestamp — "
            "the board's clock is being used for ordering"
        )


class TestMalformedTimestamps:
    """A broken clock must degrade, never crash and never wedge synchronisation."""

    @pytest.mark.parametrize("bad", [None, "", "abc", float("nan"), float("inf"), -5, [], {}])
    async def test_malformed_timestamps_do_not_raise(self, entry, bad):
        result = await _put("paras", entry, 100.0, bad)
        assert isinstance(result["version"], int)

    async def test_an_absurd_future_timestamp_is_clamped_not_rejected(self, entry):
        """
        Clamped rather than refused.

        Refusing would let a device with a broken clock wedge itself out of syncing permanently.
        Clamping keeps it participating — and losing conflicts, which is the right outcome.
        """
        result = await _put("paras", entry, 100.0, 10**19)
        assert result["applied"] is True
        assert (await media_index.positions("paras"))[0]["clientUpdatedAt"] <= 4102444800000


class TestCompletionClearing:
    async def test_finishing_clears_the_resume_point(self, entry):
        await _put("paras", entry, 1800.0, T0)
        result = await _put("paras", entry, 3500.0, T0 + MINUTE)      # >= 95%

        assert result["cleared"] is True
        assert await media_index.positions("paras") == []

    async def test_below_the_floor_is_not_offered(self, entry):
        result = await _put("paras", entry, 10.0, T0)                 # < 30s
        assert result["cleared"] is True
        assert await media_index.positions("paras") == []

    async def test_a_stale_report_cannot_resurrect_a_finished_item(self, entry):
        """
        Why clearing writes a tombstone instead of deleting the row.

        Deleting loses the timestamp. A stale in-flight report arriving after completion would then
        find no stored state, count as "first write", and put a finished film back into Continue
        Watching.
        """
        await _put("paras", entry, 1800.0, T0)
        await _put("paras", entry, 3500.0, T0 + 2 * MINUTE)           # completed
        late = await _put("paras", entry, 1900.0, T0 + MINUTE)        # was in flight

        assert late["applied"] is False
        assert await media_index.positions("paras") == [], "a finished item came back"

    async def test_replaying_the_completion_report_stays_cleared(self, entry):
        await _put("paras", entry, 1800.0, T0)
        await _put("paras", entry, 3500.0, T0 + MINUTE)
        await _put("paras", entry, 3500.0, T0 + MINUTE)               # duplicate retry

        assert await media_index.positions("paras") == []


class TestIdentityAndIsolation:
    async def test_resume_survives_a_rename_because_it_is_keyed_by_entry_id(self, entry):
        """
        The reason identity is the entry id and not the path.

        The row is keyed by entry_id, so moving the file does not orphan the position.
        """
        await _put("paras", entry, 1200.0, T0)

        # Rename the file for real, at the storage layer: the blob's rel_path changes while the
        # entry id stays put. A path-keyed resume point would be orphaned here; an entry-id-keyed
        # one is untouched.
        def _rename():
            with media_index._get_conn() as conn:
                conn.execute(
                    "UPDATE blobs SET rel_path = ? WHERE id = "
                    "(SELECT blob_id FROM entries WHERE id = ?)",
                    ("/entertainment/Movies/renamed-by-user.mkv", entry),
                )
                conn.commit()
        _rename()

        rows = await media_index.positions("paras")
        assert rows and rows[0]["entryId"] == entry and rows[0]["position"] == 1200.0, \
            "resume did not survive a rename — identity is still tied to the path"
        # And the lookup now resolves the NEW path to the same identity.
        found = await media_index.entry_ids_for_paths(["/entertainment/Movies/renamed-by-user.mkv"])
        assert found["/entertainment/Movies/renamed-by-user.mkv"] == entry

    async def test_two_members_positions_are_independent(self, entry):
        await _put("paras", entry, 100.0, T0)
        await _put("chai", entry, 2000.0, T0)

        assert (await media_index.positions("paras"))[0]["position"] == 100.0
        assert (await media_index.positions("chai"))[0]["position"] == 2000.0

    async def test_versions_are_per_user_and_do_not_leak(self, entry):
        await _put("paras", entry, 100.0, T0)
        await _put("paras", entry, 200.0, T0 + MINUTE)
        first_for_chai = await _put("chai", entry, 300.0, T0)

        assert first_for_chai["version"] == 1, "version counter is shared between members"


class TestCrossDeviceRestore:
    async def test_a_second_device_sees_what_the_first_reported(self, entry):
        """phone -> server -> phone. The TV half lives in the ahcplay repo and is not built yet."""
        await _put("paras", entry, 1500.0, T0)

        rows = await media_index.positions("paras")
        assert rows[0]["position"] == 1500.0
        assert rows[0]["version"] == 1
        assert rows[0]["clientUpdatedAt"] == T0


class TestEntryIdLookup:
    async def test_paths_map_to_entry_ids_and_unknown_paths_are_absent(self, entry):
        found = await media_index.entry_ids_for_paths(
            ["/entertainment/Movies/sync.mkv", "/entertainment/Movies/not-indexed.mkv"]
        )
        assert found["/entertainment/Movies/sync.mkv"] == entry
        assert "/entertainment/Movies/not-indexed.mkv" not in found, \
            "an unindexed file must have NO entry id — never a path-based fallback"

    async def test_an_empty_request_does_not_query(self, entry):
        assert await media_index.entry_ids_for_paths([]) == {}
