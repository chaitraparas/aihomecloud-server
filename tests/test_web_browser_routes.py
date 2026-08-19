"""Regression test for SEC-1 (2026-07-16 audit): /api/v1/files/media-token minted a
token for ANY path with no ownership check, unlike every sibling file endpoint —
letting an authenticated non-admin family member read another member's private
files via /browse/raw. See test_idor_eval_task.py for the sibling-endpoint pattern
this mirrors.
"""
import io

import pytest
from httpx import AsyncClient


async def _create_member(client: AsyncClient, admin_token: str, name: str, pin: str) -> str:
    resp = await client.post(
        "/api/v1/users",
        json={"name": name, "pin": pin},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code in (200, 201), f"create {name} failed: {resp.text}"
    resp = await client.post("/api/v1/auth/login", json={"name": name, "pin": pin})
    assert resp.status_code == 200, f"login {name} failed: {resp.text}"
    return resp.json()["accessToken"]


@pytest.mark.asyncio
@pytest.mark.security
async def test_media_token_refuses_another_members_personal_path(client: AsyncClient, admin_token: str):
    bob_token = await _create_member(client, admin_token, "bob", "2222")
    alice_token = await _create_member(client, admin_token, "alice", "1111")

    resp = await client.post(
        "/api/v1/files/upload?path=/personal/bob/",
        files={"file": ("secret.jpg", io.BytesIO(b"bob's private photo"), "image/jpeg")},
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert resp.status_code == 201, f"bob's own upload should succeed: {resp.text}"

    resp = await client.get(
        "/api/v1/files/media-token?path=/personal/bob/secret.jpg",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp.status_code == 403, (
        f"alice minted a media token for bob's private file — IDOR not fixed, "
        f"got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
@pytest.mark.security
async def test_media_token_still_works_for_own_personal_path(client: AsyncClient, admin_token: str):
    alice_token = await _create_member(client, admin_token, "alice", "1111")

    resp = await client.post(
        "/api/v1/files/upload?path=/personal/alice/",
        files={"file": ("mine.jpg", io.BytesIO(b"alice's own photo"), "image/jpeg")},
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp.status_code == 201

    resp = await client.get(
        "/api/v1/files/media-token?path=/personal/alice/mine.jpg",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp.status_code == 200, "fix must not also block a member's access to their OWN files"
    assert "token" in resp.json()


@pytest.mark.asyncio
@pytest.mark.security
async def test_media_token_still_works_for_shared_family_path(client: AsyncClient, admin_token: str):
    resp = await client.post(
        "/api/v1/files/upload?path=/family/",
        files={"file": ("shared.jpg", io.BytesIO(b"a shared family photo"), "image/jpeg")},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201

    bob_token = await _create_member(client, admin_token, "bob", "2222")
    resp = await client.get(
        "/api/v1/files/media-token?path=/family/shared.jpg",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert resp.status_code == 200, "the fix must not restrict shared (non-personal) locations"
