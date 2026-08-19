"""
M-11 — unauthenticated first-admin creation must be bounded by "never set up", not "store is empty".

`POST /users` bootstraps the first owner without authentication. The gate was
`len(await store.get_users()) == 0`, and that asks the wrong question: `_read_json` returns `[]`
when `users.json` is missing or unrecoverably corrupt, so a damaged store on an **established**
board — one already holding a family's photos — silently reopened unauthenticated admin creation to
anyone on the LAN.

The concurrency angle was already sound: `store._user_creation_lock` serialises the
read → check → write, and the service runs a single uvicorn worker so an asyncio lock is
process-wide. Those properties are pinned here anyway, because they are load-bearing and nothing
else asserted them.

The invariant, stated once:

    Unauthenticated admin creation is permitted only on a board that has never completed setup.
    Once setup completes, that path is closed permanently, whatever the user store currently says.
"""

import asyncio

import pytest

from app import store


def client_app_state():
    from app.main import app
    return app.state


@pytest.fixture
def fresh_board(monkeypatch):
    """
    A board that has never completed setup.

    Deliberately does NOT set `settings.data_dir` — the `client` fixture already points it at its
    own tmp_path, and overriding it here would put the marker somewhere the running app never
    looks, so every assertion about the marker would be measuring the wrong file.
    """
    from app.config import settings

    # `store._user_creation_lock` is a module-level asyncio.Lock created at import. asyncio binds
    # such a lock to the first event loop that awaits it, and pytest-asyncio gives every test its
    # own loop — so in a full-suite run the lock is still bound to an earlier test's loop and
    # awaiting it raises "bound to a different event loop". Not a production concern (one uvicorn
    # worker, one loop for the life of the process), but the test must start from a lock bound to
    # the loop it is actually running on, or it measures the harness instead of the code.
    monkeypatch.setattr(store, "_user_creation_lock", asyncio.Lock())

    store._set_cached("users", None)
    marker = store.setup_marker_path()
    if marker.exists():
        marker.unlink()
    yield settings.data_dir
    store._set_cached("users", None)


async def _post_user(client, name, pin="1234", token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.post(
        "/api/v1/users", json={"name": name, "pin": pin, "icon_emoji": ""}, headers=headers
    )


@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    """
    A writable data_dir for the marker helpers tested in isolation.

    Separate from `fresh_board` on purpose: these tests do not use the `client` fixture, so nothing
    else has repointed `settings.data_dir` and it would otherwise be the production default, which
    does not exist on a dev machine. The route tests must NOT use this — there, the client fixture
    owns data_dir and a second override would put the marker where the app never looks.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store._set_cached("users", None)
    yield tmp_path
    store._set_cached("users", None)


class TestSetupMarker:
    def test_absent_on_a_fresh_board(self, marker_dir):
        assert store.setup_completed() is False

    @pytest.mark.asyncio
    async def test_written_and_idempotent(self, marker_dir):
        await store.mark_setup_completed()
        assert store.setup_completed() is True
        await store.mark_setup_completed()          # must not raise or duplicate
        assert store.setup_completed() is True

    def test_unreadable_marker_fails_closed(self, marker_dir, monkeypatch):
        """
        If the marker cannot be read, assume setup HAS happened.

        Failing open here would mean an I/O error on the data directory reopens unauthenticated
        admin creation — the exact condition this guards. Refusing a legitimate first-run instead is
        visible, recoverable, and needs a human.
        """
        def boom(self):
            raise OSError("simulated I/O failure")

        monkeypatch.setattr("pathlib.Path.exists", boom)
        assert store.setup_completed() is True

    @pytest.mark.asyncio
    async def test_backfill_marks_a_board_that_already_has_users(self, marker_dir, monkeypatch):
        """
        Boards installed before the marker existed must be protected from their next restart.

        Without this backfill an existing board keeps the old behaviour until someone happens to
        call POST /users — but the protection is needed *before* the store gets damaged.
        """
        async def fake_users():
            return [{"id": "u1", "name": "Paras", "is_admin": True}]

        monkeypatch.setattr(store, "get_users", fake_users)
        assert store.setup_completed() is False
        await store.backfill_setup_marker()
        assert store.setup_completed() is True

    @pytest.mark.asyncio
    async def test_backfill_leaves_a_genuinely_fresh_board_alone(self, marker_dir, monkeypatch):
        async def no_users():
            return []

        monkeypatch.setattr(store, "get_users", no_users)
        await store.backfill_setup_marker()
        assert store.setup_completed() is False, "a never-set-up board must still be able to bootstrap"


class TestFirstAdminCreation:
    @pytest.mark.asyncio
    async def test_corrupt_store_on_an_established_board_cannot_bootstrap_an_admin(
        self, client, fresh_board, monkeypatch
    ):
        """
        THE regression test for M-11.

        Board has completed setup; its user list then reads empty because users.json was lost or
        corrupted. An unauthenticated caller must NOT be able to create an admin. Against the old
        `len(existing) == 0` gate this returns 201 with `isAdmin: true`.
        """
        await store.mark_setup_completed()

        async def empty_users():
            return []

        monkeypatch.setattr(store, "get_users", empty_users)

        response = await _post_user(client, "Attacker")

        assert response.status_code == 503, (
            f"unauthenticated admin creation succeeded on an established board "
            f"({response.status_code}) — M-11 has regressed"
        )
        assert "already set up" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_genuinely_fresh_board_can_still_bootstrap(self, client, fresh_board, monkeypatch):
        """The fix must not break the flow it guards — first run still works, unauthenticated."""
        users: list = []

        async def get_users():
            return list(users)

        async def add_user(name, pin=None, is_admin=False, icon_emoji=""):
            u = {"id": f"u{len(users)}", "name": name, "pin": pin, "is_admin": is_admin}
            users.append(u)
            return u

        monkeypatch.setattr(store, "get_users", get_users)
        monkeypatch.setattr(store, "add_user", add_user)

        response = await _post_user(client, "Paras")

        assert response.status_code == 201, response.text
        assert response.json()["isAdmin"] is True
        assert store.setup_completed() is True, "marker must be written as part of first creation"

    @pytest.mark.asyncio
    async def test_second_unauthenticated_create_is_rejected(self, client, fresh_board, monkeypatch):
        """Once an owner exists, the door is shut for unauthenticated callers."""
        users: list = []

        async def get_users():
            return list(users)

        async def add_user(name, pin=None, is_admin=False, icon_emoji=""):
            u = {"id": f"u{len(users)}", "name": name, "pin": pin, "is_admin": is_admin}
            users.append(u)
            return u

        monkeypatch.setattr(store, "get_users", get_users)
        monkeypatch.setattr(store, "add_user", add_user)

        assert (await _post_user(client, "Paras")).status_code == 201
        second = await _post_user(client, "Intruder")
        assert second.status_code == 401

    @pytest.mark.asyncio
    async def test_two_concurrent_bootstraps_yield_exactly_one_admin(
        self, client, fresh_board, monkeypatch
    ):
        """
        Two simultaneous first-run creates must produce one admin, not two.

        `_user_creation_lock` already provided this; nothing asserted it. The `await` inside
        add_user is deliberate — it forces a scheduling point, so an unlocked implementation
        genuinely interleaves and both callers see an empty store.
        """
        users: list = []

        async def get_users():
            return list(users)

        async def add_user(name, pin=None, is_admin=False, icon_emoji=""):
            await asyncio.sleep(0)      # yield, so a missing lock actually interleaves
            u = {"id": f"u{len(users)}", "name": name, "pin": pin, "is_admin": is_admin}
            users.append(u)
            return u

        monkeypatch.setattr(store, "get_users", get_users)
        monkeypatch.setattr(store, "add_user", add_user)

        # Reset the per-IP rate limiter. create_user is limited to 5/minute, and in a full-suite
        # run earlier tests share this client's address — so without this the second request comes
        # back 429 and the test measures throttling rather than the lock it is about.
        limiter = getattr(client_app_state(), "limiter", None)
        if limiter is not None and hasattr(limiter, "reset"):
            limiter.reset()

        results = await asyncio.gather(
            _post_user(client, "Paras"), _post_user(client, "Chaitra"), return_exceptions=True,
        )
        # Do NOT silently drop exceptions here: an exception is a distinct failure mode from a
        # rejected request, and swallowing it turns a real bug into a confusing count mismatch.
        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"a bootstrap request raised instead of responding: {raised!r}"

        codes = sorted(r.status_code for r in results)
        assert codes == [201, 401], f"expected exactly one bootstrap to succeed, got {codes}"
        admins = [u for u in users if u.get("is_admin")]
        assert len(admins) == 1, f"expected exactly one admin, got {len(admins)}"

    @pytest.mark.asyncio
    async def test_duplicate_name_is_checked_inside_the_lock(self, client, fresh_board, monkeypatch):
        """
        Two concurrent creates of the SAME name must not both succeed.

        The personal folder is `personal_path / name`, so two profiles sharing a name share one
        directory — and deleting either then deletes the other's photos. The check used to run
        before the lock was taken, so both callers passed it.
        """
        users: list = [{"id": "u0", "name": "Paras", "is_admin": True}]

        async def get_users():
            return list(users)

        async def add_user(name, pin=None, is_admin=False, icon_emoji=""):
            await asyncio.sleep(0)
            u = {"id": f"u{len(users)}", "name": name, "pin": pin, "is_admin": is_admin}
            users.append(u)
            return u

        monkeypatch.setattr(store, "get_users", get_users)
        monkeypatch.setattr(store, "add_user", add_user)
        await store.mark_setup_completed()

        # Unauthenticated, so these stop at the 401 gate — the point is that the duplicate name is
        # rejected on the authorization path too, not that creation succeeds.
        result = await _post_user(client, "paras")
        assert result.status_code in (401, 409)
        assert len([u for u in users if u["name"].lower() == "paras"]) == 1
