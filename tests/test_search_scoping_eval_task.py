"""Targeted regression test for the /search identity-scoping fix (eval task:
search-scoping-wrong-identity, gold commit 32d62ec — same commit as the IDOR fix; the
identity resolution bug was fixed alongside it). Authored for llm-eval-harness Tier A
grading — no prior test covered this. See docs/decisions/0002 in that project.
"""
import pytest
from httpx import AsyncClient

from app.document_index import index_document, init_db

# The ASGITransport test client doesn't run FastAPI lifespan startup, so the FTS5
# doc_index table (normally created by app.main's lifespan -> document_index.init_db())
# never gets created — no existing test hit this gap because no existing test touched
# search/document_index at all.
@pytest.fixture(autouse=True)
async def _init_search_db(client: AsyncClient):
    await init_db()


async def _create_member(client: AsyncClient, admin_token: str, name: str, pin: str) -> str:
    resp = await client.post(
        "/api/v1/users", json={"name": name, "pin": pin},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code in (200, 201), f"create {name} failed: {resp.text}"
    resp = await client.post("/api/v1/auth/login", json={"name": name, "pin": pin})
    assert resp.status_code == 200
    return resp.json()["accessToken"]


@pytest.mark.asyncio
async def test_member_search_finds_own_indexed_document(client: AsyncClient, admin_token: str):
    alice_token = await _create_member(client, admin_token, "alice", "1111")

    await index_document(
        path="/personal/alice/Documents/tax_notes_uniquetoken837.txt",
        filename="tax_notes_uniquetoken837.txt",
        added_by="alice",
    )

    resp = await client.get(
        "/api/v1/files/search?q=uniquetoken837",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1, (
        f"member search found 0 results for their own indexed doc — "
        f"identity scoping bug not fixed: {data}"
    )


@pytest.mark.asyncio
async def test_admin_search_sees_member_documents(client: AsyncClient, admin_token: str):
    await _create_member(client, admin_token, "alice", "1111")

    await index_document(
        path="/personal/alice/Documents/tax_notes_uniquetoken951.txt",
        filename="tax_notes_uniquetoken951.txt",
        added_by="alice",
    )

    resp = await client.get(
        "/api/v1/files/search?q=uniquetoken951",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1, (
        f"admin search missed a member's document — admin was treated as a plain "
        f"member (is_admin not resolved from JWT sub): {data}"
    )
