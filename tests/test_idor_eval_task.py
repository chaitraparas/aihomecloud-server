"""Targeted regression test for the personal-folder IDOR fix (eval task:
personal-folder-idor, gold commit 32d62ec). No such test existed in the repo before —
authored for llm-eval-harness Tier A grading. See docs/decisions/0002 in that project
for why this had to be written rather than relying on the existing suite.
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
async def test_member_cannot_list_another_members_personal_folder(client: AsyncClient, admin_token: str):
    bob_token = await _create_member(client, admin_token, "bob", "2222")
    alice_token = await _create_member(client, admin_token, "alice", "1111")

    # bob uploads a file to his own personal folder
    resp = await client.post(
        "/api/v1/files/upload?path=/personal/bob/",
        files={"file": ("secret.txt", io.BytesIO(b"bob's private data"), "text/plain")},
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert resp.status_code == 201, f"bob's own upload should succeed: {resp.text}"

    # alice must NOT be able to list bob's personal folder
    resp = await client.get(
        "/api/v1/files/list?path=/personal/bob/",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp.status_code == 403, (
        f"alice listed bob's personal folder — IDOR not fixed, got {resp.status_code}: {resp.text}"
    )

    # alice must NOT be able to download bob's file directly either
    resp = await client.get(
        "/api/v1/files/download?path=/personal/bob/secret.txt",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp.status_code == 403, (
        f"alice downloaded bob's file — IDOR not fixed, got {resp.status_code}"
    )


@pytest.mark.asyncio
@pytest.mark.security
async def test_member_can_still_access_own_personal_folder(client: AsyncClient, admin_token: str):
    alice_token = await _create_member(client, admin_token, "alice", "1111")

    resp = await client.post(
        "/api/v1/files/upload?path=/personal/alice/",
        files={"file": ("mine.txt", io.BytesIO(b"alice's data"), "text/plain")},
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp.status_code == 201

    resp = await client.get(
        "/api/v1/files/list?path=/personal/alice/",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp.status_code == 200, "fix must not also block a member's access to their OWN folder"
