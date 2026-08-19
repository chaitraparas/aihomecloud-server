"""
Tests for family / user management routes (P2-Task 23).

Covers RBAC enforcement (admin vs member), CRUD operations, and
guard conditions (last-admin lockout, empty name).
"""

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# List family
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_family_requires_auth(client: AsyncClient):
    """GET /users/family without a token → 401 Unauthorized."""
    resp = await client.get("/api/v1/users/family")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_family_returns_all_users(client: AsyncClient, admin_token: str):
    """Admin can list all family members."""
    resp = await client.get(
        "/api/v1/users/family",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1  # at least the admin user created by the fixture


@pytest.mark.asyncio
async def test_list_family_member_can_list(client: AsyncClient, member_token: str):
    """Members can also list family — no admin restriction on list."""
    resp = await client.get(
        "/api/v1/users/family",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_list_family_includes_admin_flag(client: AsyncClient, admin_token: str):
    """Admin user returned in list has isAdmin=True."""
    resp = await client.get(
        "/api/v1/users/family",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    admins = [u for u in resp.json() if u.get("isAdmin")]
    assert len(admins) >= 1


# ---------------------------------------------------------------------------
# Add family member
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_family_requires_admin(client: AsyncClient, member_token: str):
    """Member cannot add family members — admin only."""
    resp = await client.post(
        "/api/v1/users/family",
        json={"name": "newuser"},
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_add_family_admin_creates_member(client: AsyncClient, admin_token: str):
    """Admin can add a new family member."""
    resp = await client.post(
        "/api/v1/users/family",
        json={"name": "newmember"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "newmember"
    assert data["isAdmin"] is False


@pytest.mark.asyncio
async def test_add_family_empty_name_returns_400(client: AsyncClient, admin_token: str):
    """Adding a member with an empty or blank name → 400."""
    resp = await client.post(
        "/api/v1/users/family",
        json={"name": "   "},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_add_family_requires_auth(client: AsyncClient):
    """No token → 401 Unauthorized."""
    resp = await client.post("/api/v1/users/family", json={"name": "noauth"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Remove family member
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remove_family_requires_admin(client: AsyncClient, member_token: str):
    """Member cannot remove family members — admin only."""
    resp = await client.delete(
        "/api/v1/users/family/user_nonexistent",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_remove_family_not_found(client: AsyncClient, admin_token: str):
    """Deleting a non-existent user → 404."""
    resp = await client.delete(
        "/api/v1/users/family/user_doesnotexist",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remove_family_success(client: AsyncClient, admin_token: str):
    """Admin can remove an existing non-admin member."""
    # First add a member to remove
    add_resp = await client.post(
        "/api/v1/users/family",
        json={"name": "to_remove"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert add_resp.status_code == 201
    user_id = add_resp.json()["id"]

    # Now remove
    del_resp = await client.delete(
        f"/api/v1/users/family/{user_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert del_resp.status_code == 204

    # Verify gone
    list_resp = await client.get(
        "/api/v1/users/family",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    names = [u["name"] for u in list_resp.json()]
    assert "to_remove" not in names


# ---------------------------------------------------------------------------
# Set family role
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_role_requires_admin(client: AsyncClient, member_token: str):
    """Member cannot change roles — admin only."""
    resp = await client.put(
        "/api/v1/users/family/user_xyz/role",
        json={"isAdmin": True},
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_set_role_not_found(client: AsyncClient, admin_token: str):
    """Updating role for a non-existent user → 404."""
    resp = await client.put(
        "/api/v1/users/family/user_doesnotexist/role",
        json={"isAdmin": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_role_blocks_demoting_last_admin(client: AsyncClient, admin_token: str):
    """Cannot demote the only admin — prevents lockout."""
    # Get the admin user's ID
    list_resp = await client.get(
        "/api/v1/users/family",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    admins = [u for u in list_resp.json() if u["isAdmin"]]
    assert len(admins) >= 1
    admin_id = admins[0]["id"]

    resp = await client.put(
        f"/api/v1/users/family/{admin_id}/role",
        json={"isAdmin": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 400
    assert "only admin" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_set_role_promotes_member_to_admin(client: AsyncClient, admin_token: str):
    """Admin can promote a member to admin."""
    # Add a non-admin member
    add_resp = await client.post(
        "/api/v1/users/family",
        json={"name": "new_admin_candidate"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert add_resp.status_code == 201
    user_id = add_resp.json()["id"]
    assert add_resp.json()["isAdmin"] is False

    # Promote
    promo_resp = await client.put(
        f"/api/v1/users/family/{user_id}/role",
        json={"isAdmin": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert promo_resp.status_code == 204

    # Verify
    list_resp = await client.get(
        "/api/v1/users/family",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    updated = next(u for u in list_resp.json() if u["id"] == user_id)
    assert updated["isAdmin"] is True


@pytest.mark.asyncio
async def test_set_role_demotes_one_of_two_admins(client: AsyncClient, admin_token: str):
    """Can demote an admin if there is another admin remaining."""
    # Promote a member to admin first
    add_resp = await client.post(
        "/api/v1/users/family",
        json={"name": "second_admin"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert add_resp.status_code == 201
    second_id = add_resp.json()["id"]

    await client.put(
        f"/api/v1/users/family/{second_id}/role",
        json={"isAdmin": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    # Now demote second admin — should succeed since first admin still exists
    resp = await client.put(
        f"/api/v1/users/family/{second_id}/role",
        json={"isAdmin": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 204
