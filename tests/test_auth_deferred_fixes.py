"""
Regression tests for the 2026-07-30 auth findings closed on 2026-08-04.

Finding 9 — create_user did not enforce unique names. store.add_user appends
unconditionally, and the personal folder is `personal_path / name`, so two profiles called
"Paras" shared /srv/nas/personal/Paras/ and deleting either account deleted the other's
photos. That is silent data loss, which is why it gets a test rather than a comment.

Finding 8 — logout revoked whatever refresh token it was handed without checking the token
belonged to the caller.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_duplicate_name_is_rejected(client: AsyncClient, admin_token: str):
    headers = {"Authorization": f"Bearer {admin_token}"}
    first = await client.post(
        "/api/v1/users", json={"name": "Prutha", "pin": "1234"}, headers=headers
    )
    assert first.status_code == 201

    duplicate = await client.post(
        "/api/v1/users", json={"name": "Prutha", "pin": "5678"}, headers=headers
    )
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_duplicate_name_is_rejected_case_insensitively(client: AsyncClient, admin_token: str):
    # A case-sensitive check would still collide on a case-insensitive filesystem, and would
    # be confusing on any filesystem. PUT /users/me already compares case-insensitively.
    headers = {"Authorization": f"Bearer {admin_token}"}
    assert (
        await client.post("/api/v1/users", json={"name": "Prutha", "pin": "1234"}, headers=headers)
    ).status_code == 201

    for variant in ("prutha", "PRUTHA", "PrUtHa"):
        resp = await client.post(
            "/api/v1/users", json={"name": variant, "pin": "5678"}, headers=headers
        )
        assert resp.status_code == 409, f"'{variant}' should collide with 'Prutha'"


@pytest.mark.asyncio
async def test_surrounding_whitespace_does_not_defeat_the_check(client: AsyncClient, admin_token: str):
    headers = {"Authorization": f"Bearer {admin_token}"}
    assert (
        await client.post("/api/v1/users", json={"name": "Prutha", "pin": "1234"}, headers=headers)
    ).status_code == 201
    resp = await client.post(
        "/api/v1/users", json={"name": "  Prutha  ", "pin": "5678"}, headers=headers
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_a_genuinely_different_name_is_still_allowed(client: AsyncClient, admin_token: str):
    # The check must not be so eager that it blocks legitimate accounts.
    headers = {"Authorization": f"Bearer {admin_token}"}
    assert (
        await client.post("/api/v1/users", json={"name": "Prutha", "pin": "1234"}, headers=headers)
    ).status_code == 201
    assert (
        await client.post("/api/v1/users", json={"name": "Prutha2", "pin": "5678"}, headers=headers)
    ).status_code == 201


@pytest.mark.asyncio
async def test_logout_does_not_revoke_another_users_refresh_token(
    client: AsyncClient, admin_token: str
):
    """
    Two real accounts. One logs out presenting the OTHER's refresh token; that token must
    still work afterwards.
    """
    headers = {"Authorization": f"Bearer {admin_token}"}
    assert (
        await client.post("/api/v1/users", json={"name": "Victim", "pin": "1111"}, headers=headers)
    ).status_code == 201
    assert (
        await client.post("/api/v1/users", json={"name": "Other", "pin": "2222"}, headers=headers)
    ).status_code == 201

    victim = await client.post("/api/v1/auth/login", json={"name": "Victim", "pin": "1111"})
    other = await client.post("/api/v1/auth/login", json={"name": "Other", "pin": "2222"})
    assert victim.status_code == 200 and other.status_code == 200

    victim_refresh = victim.json()["refreshToken"]
    other_access = other.json()["accessToken"]

    # "Other" tries to revoke "Victim"'s session.
    logout = await client.post(
        "/api/v1/auth/logout",
        json={"refreshToken": victim_refresh},
        headers={"Authorization": f"Bearer {other_access}"},
    )
    assert logout.status_code == 204  # silent by design — must not confirm the token is real

    # The victim's token must still work.
    refreshed = await client.post("/api/v1/auth/refresh", json={"refreshToken": victim_refresh})
    assert refreshed.status_code == 200, "another user's logout revoked this session"


@pytest.mark.asyncio
async def test_logout_still_revokes_your_own_token(client: AsyncClient, admin_token: str):
    # The ownership check must not break the actual feature.
    headers = {"Authorization": f"Bearer {admin_token}"}
    assert (
        await client.post("/api/v1/users", json={"name": "Self", "pin": "3333"}, headers=headers)
    ).status_code == 201

    session = await client.post("/api/v1/auth/login", json={"name": "Self", "pin": "3333"})
    refresh = session.json()["refreshToken"]
    access = session.json()["accessToken"]

    logout = await client.post(
        "/api/v1/auth/logout",
        json={"refreshToken": refresh},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert logout.status_code == 204

    reused = await client.post("/api/v1/auth/refresh", json={"refreshToken": refresh})
    assert reused.status_code != 200, "own token should be revoked after logout"


@pytest.mark.asyncio
async def test_rehash_does_not_revert_a_pin_changed_mid_flight(client: AsyncClient, admin_token: str):
    """
    Finding 7. The background bcrypt-rehash task hashes the PIN captured at login. Hashing is
    deliberately slow, so a user can change their PIN before it finishes — and without a
    compare-and-swap the task writes a hash of the OLD pin over the new one, silently
    reverting them. The old PIN starts working again and the new one does not.

    Driven through the store directly rather than by racing real timing, which would be flaky.
    """
    from app import store
    from app.auth import hash_password, verify_password
    from app.routes.auth_routes import _rehash_pin

    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post(
        "/api/v1/users", json={"name": "Racer", "pin": "1111"}, headers=headers
    )
    assert created.status_code == 201

    users = await store.get_users()
    racer = next(u for u in users if u["name"] == "Racer")
    hash_at_login = racer["pin"]

    # The user changes their PIN while the rehash is still in flight.
    new_hash = await hash_password("2222")
    assert await store.update_user_pin(racer["id"], new_hash)

    # The stale background task now completes, carrying the OLD pin and the OLD hash.
    await _rehash_pin(racer["id"], "1111", hash_at_login)

    users = await store.get_users()
    racer = next(u for u in users if u["id"] == racer["id"])
    assert await verify_password("2222", racer["pin"]), "the new PIN must survive"
    assert not await verify_password("1111", racer["pin"]), "the old PIN must not come back"


@pytest.mark.asyncio
async def test_rehash_still_upgrades_when_nothing_changed(client: AsyncClient, admin_token: str):
    # The guard must not disable the feature it protects.
    from app import store
    from app.auth import verify_password
    from app.routes.auth_routes import _rehash_pin

    headers = {"Authorization": f"Bearer {admin_token}"}
    assert (
        await client.post("/api/v1/users", json={"name": "Quiet", "pin": "3333"}, headers=headers)
    ).status_code == 201

    users = await store.get_users()
    quiet = next(u for u in users if u["name"] == "Quiet")
    original = quiet["pin"]

    await _rehash_pin(quiet["id"], "3333", original)

    users = await store.get_users()
    quiet = next(u for u in users if u["id"] == quiet["id"])
    assert quiet["pin"] != original, "an undisturbed rehash should still replace the hash"
    assert await verify_password("3333", quiet["pin"]), "and the PIN must still work"
