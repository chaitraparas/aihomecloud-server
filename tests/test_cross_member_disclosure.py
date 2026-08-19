"""
Cross-member disclosure through housekeeping routes.

The product's core promise is that a personal scope is private *from other household members*. The
August 2026 audit found three routes that honoured path-safety but not ownership — duplicates,
local-backup browse/download, and the Telegram document listing. Path-safety stops traversal; it
says nothing about whose files these are.
"""

import pytest

from app.routes.backup_routes import _visible_duplicate_sets


class TestDuplicateResultsScoping:
    """The scan runs fleet-wide; its results carry full paths and owners."""

    def _sets(self):
        return [
            {"paths": ["/personal/alice/Photos/a.jpg", "/personal/alice/Photos/copy.jpg"]},
            {"paths": ["/personal/bob/Docs/passport.pdf", "/shared/Docs/passport.pdf"]},
            {"paths": ["/shared/Photos/x.jpg", "/shared/Photos/y.jpg"]},
        ]

    def test_a_member_sees_only_their_own_and_shared(self):
        visible = _visible_duplicate_sets(self._sets(), "alice", is_admin=False)
        paths = [p for s in visible for p in s["paths"]]

        assert not any("/personal/bob/" in p for p in paths), "leaked another member's paths"
        assert any("/personal/alice/" in p for p in paths)

    def test_a_mixed_set_is_withheld_entirely(self):
        """
        Bob's set pairs his private file with a shared copy. Showing it partially redacted would
        still tell Alice that a file matching that shared one exists in someone's private folder.
        """
        visible = _visible_duplicate_sets(self._sets(), "alice", is_admin=False)

        assert all("/personal/bob/" not in p for s in visible for p in s["paths"])

    def test_shared_only_sets_remain_visible(self):
        visible = _visible_duplicate_sets(self._sets(), "alice", is_admin=False)

        assert any(s["paths"][0].startswith("/shared/") for s in visible), \
            "scoping should not hide genuinely shared duplicates"

    def test_admin_still_sees_everything(self):
        assert len(_visible_duplicate_sets(self._sets(), "admin", is_admin=True)) == 3

    def test_an_unknown_caller_gets_no_personal_paths(self):
        visible = _visible_duplicate_sets(self._sets(), "", is_admin=False)

        assert all("/personal/" not in p for s in visible for p in s["paths"])


class TestBackupPathAuthorization:
    """A backup copy is the same bytes and the same promise as the original."""

    @pytest.mark.parametrize("path,caller,allowed", [
        ("/personal/alice/Docs/x.pdf", "alice", True),
        ("/personal/alice/Docs/x.pdf", "bob", False),
        ("/personal/Alice/Docs/x.pdf", "alice", True),   # case-insensitive owner match
        ("/shared/Docs/x.pdf", "bob", True),
        ("/family/Photos/x.jpg", "bob", True),
    ])
    async def test_owner_rule(self, path, caller, allowed, monkeypatch):
        from fastapi import HTTPException
        from app.routes import local_backup_routes as lbr

        async def fake_identity(_user):
            return caller, False
        monkeypatch.setattr(lbr, "_resolve_identity", fake_identity)

        if allowed:
            await lbr._authorize_backup_path(path, {})
        else:
            with pytest.raises(HTTPException) as e:
                await lbr._authorize_backup_path(path, {})
            assert e.value.status_code == 403

    async def test_admin_may_read_any_backup(self, monkeypatch):
        from app.routes import local_backup_routes as lbr

        async def fake_identity(_user):
            return "root", True
        monkeypatch.setattr(lbr, "_resolve_identity", fake_identity)

        await lbr._authorize_backup_path("/personal/alice/Docs/x.pdf", {})
