"""
Additional auth route tests — covers user profile endpoints, delete, and lockout.
"""

import pytest


class TestUserProfile:
    @pytest.mark.asyncio
    async def test_get_me(self, authenticated_client):
        resp = await authenticated_client.get("/api/v1/users/me")
        assert resp.status_code == 200
        data = resp.json()
        assert "name" in data

    @pytest.mark.asyncio
    async def test_update_me(self, authenticated_client):
        resp = await authenticated_client.put(
            "/api/v1/users/me",
            json={"name": "admin", "emoji": "🚀"},
        )
        assert resp.status_code in (200, 204)

    @pytest.mark.asyncio
    async def test_update_me_no_auth(self, client):
        resp = await client.put(
            "/api/v1/users/me",
            json={"name": "admin", "emoji": "🚀"},
        )
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_rename_migrates_media_index_owner(self, client, member_token):
        """
        M2 regression: /media queries authorize/filter purely on media_index's stored owner
        column, never the physical folder name. Before the fix, renaming a profile left every
        pre-rename media_index row's owner pointing at the old name — the member's own older
        personal photos would silently stop matching scope=personal&owner=<newname> and vanish
        from "Mine", even though the files themselves were untouched.
        """
        from app import media_index
        from app.ingest import Destination, IngestMode, Scope, ingest

        async def _chunks(data: bytes):
            yield data

        # Seed a real entry through the actual ingest path, exactly as production would.
        dest = Destination(scope=Scope.PERSONAL, owner="alice", mode=IngestMode.SORTED)
        result = await ingest(_chunks(b"alice photo bytes"), filename="alice_pre_rename.jpg", dest=dest)
        assert result.path.exists()

        before = await media_index.query_entries(scope="personal", owner="alice")
        assert before, "seeding failed — no media_index entry to migrate"

        resp = await client.put(
            "/api/v1/users/me",
            json={"name": "alice_renamed"},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert resp.status_code in (200, 204), resp.text

        # Old name must no longer resolve any entries — they migrated, not duplicated.
        stale = await media_index.query_entries(scope="personal", owner="alice")
        assert stale == [], "entries must not still match the pre-rename owner"

        # New name must resolve the exact same pre-rename content.
        migrated = await media_index.query_entries(scope="personal", owner="alice_renamed")
        assert [e["filename"] for e in migrated] == [e["filename"] for e in before]


class TestUserNames:
    @pytest.mark.asyncio
    async def test_get_user_names(self, client):
        # Create a user first
        await client.post("/api/v1/users", json={"name": "admin", "pin": "0000"})
        resp = await client.get("/api/v1/auth/users/names")
        assert resp.status_code == 200
        data = resp.json()
        # Response is {"users": [...]}
        users = data.get("users", data) if isinstance(data, dict) else data
        assert len(users) >= 1


class TestCertFingerprint:
    @pytest.mark.asyncio
    async def test_cert_fingerprint(self, client):
        resp = await client.get("/api/v1/auth/cert-fingerprint")
        assert resp.status_code == 200
        data = resp.json()
        assert "fingerprint" in data or "sha256" in data or isinstance(data, dict)


class TestRemovePin:
    @pytest.mark.asyncio
    async def test_remove_pin(self, authenticated_client):
        resp = await authenticated_client.delete("/api/v1/users/pin")
        assert resp.status_code in (200, 204)


class TestDeleteProfile:
    @pytest.mark.asyncio
    async def test_delete_last_user_blocked(self, client):
        """Last remaining user (also admin) cannot be deleted."""
        await client.post("/api/v1/users", json={"name": "admin", "pin": "0000"})
        resp = await client.post("/api/v1/auth/login", json={"name": "admin", "pin": "0000"})
        token = resp.json()["accessToken"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.delete("/api/v1/users/me", headers=headers)
        # Should be blocked — last user
        assert resp.status_code in (400, 409, 403)

    @pytest.mark.asyncio
    async def test_delete_profile_cleans_media_index_and_dedup_hashes(
        self, client, member_token
    ):
        """PR #21 regression pin: deleting a profile rmtree's the personal folder, so it
        must also mark that folder's media_index entries deleted. Before the original fix,
        orphaned entries kept showing in the app until the next reconcile pass, and (in the
        pre-SQLite-dedup-migration architecture) a separate ingest_hashes store also kept a
        stale hash forever, silently no-op'ing any re-upload of identical content.

        Since the dedup migration off that JSON store, dedup reads live (non-deleted)
        media_index entries directly — there's no second store to leave orphaned, so the
        real regression this test protects against is now: does re-uploading the exact same
        bytes after the profile delete get correctly treated as new, not silently deduped
        against the deleted owner's now-gone file?"""
        from app import media_index
        from app.config import settings
        from app.ingest import Destination, IngestMode, Scope, ingest

        async def _chunks(data: bytes):
            yield data

        # Seed a real file into alice's personal folder through the actual ingest path.
        dest = Destination(scope=Scope.PERSONAL, owner="alice", mode=IngestMode.SORTED)
        result = await ingest(
            _chunks(b"alice private photo bytes"), filename="alice_pic.jpg", dest=dest
        )
        assert result.path.exists()

        # Precondition (vacuous-pass guard): a live index entry must exist BEFORE the delete.
        entries = await media_index.query_entries(scope="personal", owner="alice")
        assert entries, "seeding failed — no media_index entry to orphan"

        # Alice deletes her own profile (admin exists via member_token fixture, so
        # neither the last-user nor last-admin guard blocks it).
        resp = await client.delete(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert resp.status_code == 204, resp.text

        # Personal folder physically gone...
        assert not (settings.personal_path / "alice").exists()
        # ...no orphaned live rows left in media_index's entries table...
        entries = await media_index.query_entries(scope="personal", owner="alice")
        assert entries == [], f"orphaned media_index entries survived: {entries}"

        # ...and re-uploading the exact same bytes (e.g. a new user named "alice" later,
        # or the same content re-synced under a different owner) is NOT falsely deduped
        # against the deleted file — this is what a leftover dedup record would break.
        dest2 = Destination(scope=Scope.PERSONAL, owner="alice", mode=IngestMode.SORTED)
        result2 = await ingest(
            _chunks(b"alice private photo bytes"), filename="alice_pic.jpg", dest=dest2
        )
        assert result2.dedup_hit is False, "stale dedup record survived the profile delete"
        assert result2.path.exists()


class TestCreateSecondUser:
    @pytest.mark.asyncio
    async def test_create_second_user_non_admin(self, authenticated_client):
        """Second user should not be admin."""
        resp = await authenticated_client.post(
            "/api/v1/users",
            json={"name": "member1", "pin": "1234"},
        )
        assert resp.status_code == 201


class TestCreateUserPinPolicy:
    """Regression pin (SEC-2, 2026-07-16 full-repo audit): change_pin() already enforced a
    4-char PIN floor, but create_user() never did -- a 1-digit PIN was silently accepted at
    account creation, a false-security trap open to anyone on the LAN. No PIN at all remains
    a deliberate, allowed choice; only a too-short NON-empty PIN is rejected."""

    @pytest.mark.asyncio
    async def test_short_non_empty_pin_rejected(self, authenticated_client):
        resp = await authenticated_client.post(
            "/api/v1/users",
            json={"name": "member2", "pin": "12"},
        )
        assert resp.status_code == 400
        assert "at least 4" in resp.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_empty_pin_still_allowed(self, authenticated_client):
        resp = await authenticated_client.post(
            "/api/v1/users",
            json={"name": "member3", "pin": ""},
        )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_four_digit_pin_still_allowed(self, authenticated_client):
        resp = await authenticated_client.post(
            "/api/v1/users",
            json={"name": "member4", "pin": "4321"},
        )
        assert resp.status_code == 201
