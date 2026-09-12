"""
Tests for document_index.py — FTS5 full-text search.
All tests use a temp SQLite DB (settings.data_dir set to tmp_path by conftest).
"""

import asyncio
import pytest
from pathlib import Path

from app.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_index(tmp_path: Path):
    """Ensure DB is at tmp_path and create the FTS5 table."""
    settings.data_dir = tmp_path
    from app.document_index import init_db
    await init_db()


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_db_creates_table(tmp_path):
    """init_db() creates the doc_index FTS5 virtual table."""
    await _make_index(tmp_path)
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "docs.db"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "doc_index" in tables


@pytest.mark.asyncio
async def test_init_db_idempotent(tmp_path):
    """Calling init_db() twice does not raise."""
    await _make_index(tmp_path)
    await _make_index(tmp_path)  # second call should be a no-op


# ---------------------------------------------------------------------------
# index_document + search_documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_index_and_search_text_file(tmp_path):
    """A .txt file is indexed by reading its content; search returns it."""
    await _make_index(tmp_path)
    # Create a fake NAS structure
    nas = tmp_path / "nas"
    doc_dir = nas / "personal" / "alice" / "Documents"
    doc_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = doc_dir / "invoice_2024.txt"
    doc.write_text("Total amount due: 5000 rupees for consulting services")

    from app.document_index import index_document, search_documents
    await index_document(str(doc), doc.name, "alice")

    results = await search_documents("consulting", user_role="admin", username="alice")
    assert len(results) == 1
    assert results[0]["filename"] == "invoice_2024.txt"
    assert results[0]["added_by"] == "alice"


@pytest.mark.asyncio
async def test_index_and_search_md_file(tmp_path):
    """A .md file is indexed by content."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    doc_dir = nas / "shared" / "Documents"
    doc_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = doc_dir / "readme.md"
    doc.write_text("# Project AiHomeCloud\nBackup and NAS storage solution.")

    from app.document_index import index_document, search_documents
    await index_document(str(doc), doc.name, "shared")

    results = await search_documents("NAS storage", user_role="admin", username="")
    assert len(results) == 1
    assert results[0]["filename"] == "readme.md"


@pytest.mark.asyncio
async def test_search_empty_query_returns_empty(tmp_path):
    """Empty query returns [] without hitting the DB."""
    await _make_index(tmp_path)
    settings.nas_root = tmp_path / "nas"

    from app.document_index import search_documents
    results = await search_documents("   ")
    assert results == []


@pytest.mark.asyncio
async def test_search_no_results(tmp_path):
    """Query that matches nothing returns empty list."""
    await _make_index(tmp_path)
    settings.nas_root = tmp_path / "nas"

    from app.document_index import search_documents
    results = await search_documents("zzznomatch999", user_role="admin", username="")
    assert results == []


@pytest.mark.asyncio
async def test_search_bad_fts_query_returns_empty(tmp_path):
    """Malformed FTS query (bare AND operator) returns [] instead of raising."""
    await _make_index(tmp_path)
    settings.nas_root = tmp_path / "nas"

    from app.document_index import search_documents
    # FTS5 raises OperationalError for bare AND — must not propagate
    results = await search_documents("AND", user_role="admin", username="")
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Member scope filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_member_cannot_see_other_users_docs(tmp_path):
    """Member 'bob' cannot see alice's personal Documents."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    alice_dir = nas / "personal" / "alice" / "Documents"
    alice_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = alice_dir / "secret.txt"
    doc.write_text("Alice's private data: confidential")

    from app.document_index import index_document, search_documents
    await index_document(str(doc), doc.name, "alice")

    # bob is a member — should NOT see alice's file
    results = await search_documents("confidential", user_role="member", username="bob")
    assert results == []


@pytest.mark.asyncio
async def test_member_username_with_like_wildcard_does_not_leak_other_users_docs(tmp_path):
    """
    M3 regression: a member named "a_b" searching must not match "/personal/axb/..." — SQL
    LIKE treats a bare "_" as a single-character wildcard. Before the fix, the username was
    interpolated into the LIKE pattern unescaped, so this specific (if unlucky) name would leak
    another member's document search hits.
    """
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    axb_dir = nas / "personal" / "axb" / "Documents"
    axb_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = axb_dir / "secret.txt"
    doc.write_text("axb private data: confidential")

    from app.document_index import index_document, search_documents
    await index_document(str(doc), doc.name, "axb")

    # "a_b" must NOT match "axb" via the unescaped "_" wildcard.
    results = await search_documents("confidential", user_role="member", username="a_b")
    assert results == [], "unescaped LIKE wildcard in username must not match a different user's docs"


@pytest.mark.asyncio
async def test_member_can_see_own_docs(tmp_path):
    """Member can see their own Documents."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    alice_dir = nas / "personal" / "alice" / "Documents"
    alice_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = alice_dir / "passport.txt"
    doc.write_text("Passport number: XYZ12345")

    from app.document_index import index_document, search_documents
    await index_document(str(doc), doc.name, "alice")

    results = await search_documents("passport", user_role="member", username="alice")
    assert len(results) == 1
    assert results[0]["filename"] == "passport.txt"


@pytest.mark.asyncio
async def test_member_can_see_shared_docs(tmp_path):
    """Member can see shared Documents."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    shared_dir = nas / "family" / "Documents"
    shared_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = shared_dir / "company_policy.txt"
    doc.write_text("Leave policy: 21 days per year")

    from app.document_index import index_document, search_documents
    await index_document(str(doc), doc.name, "shared")

    results = await search_documents("leave policy", user_role="member", username="bob")
    assert len(results) == 1
    assert results[0]["filename"] == "company_policy.txt"


@pytest.mark.asyncio
async def test_admin_sees_all_docs(tmp_path):
    """Admin can see documents from all users."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    (nas / "personal" / "alice" / "Documents").mkdir(parents=True)
    (nas / "personal" / "bob" / "Documents").mkdir(parents=True)
    settings.nas_root = nas

    alice_doc = nas / "personal" / "alice" / "Documents" / "alicedata.txt"
    bob_doc = nas / "personal" / "bob" / "Documents" / "bobdata.txt"
    alice_doc.write_text("alice bankstatement details")
    bob_doc.write_text("bob bankstatement details")

    from app.document_index import index_document, search_documents
    await index_document(str(alice_doc), alice_doc.name, "alice")
    await index_document(str(bob_doc), bob_doc.name, "bob")

    results = await search_documents("bankstatement", user_role="admin", username="admin")
    assert len(results) == 2


# ---------------------------------------------------------------------------
# remove_document
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remove_document(tmp_path):
    """remove_document() removes the entry from the index."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    doc_dir = nas / "personal" / "alice" / "Documents"
    doc_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = doc_dir / "to_delete.txt"
    doc.write_text("temporary document for deletion test")

    from app.document_index import index_document, remove_document, search_documents
    await index_document(str(doc), doc.name, "alice")

    # Confirm it's indexed
    results = await search_documents("temporary document", user_role="admin", username="")
    assert len(results) == 1

    # Remove and confirm it's gone
    await remove_document(str(doc))
    results = await search_documents("temporary document", user_role="admin", username="")
    assert results == []


@pytest.mark.asyncio
async def test_remove_document_nas_path(tmp_path):
    """remove_document() also works with NAS-relative paths."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    doc_dir = nas / "personal" / "alice" / "Documents"
    doc_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = doc_dir / "nas_path.txt"
    doc.write_text("document for nas path removal test")

    from app.document_index import index_document, remove_document, search_documents
    await index_document(str(doc), doc.name, "alice")

    # Remove using NAS-relative path
    await remove_document("/personal/alice/Documents/nas_path.txt")
    results = await search_documents("nas path removal", user_role="admin", username="")
    assert results == []


# ---------------------------------------------------------------------------
# Upsert behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_re_index_updates_content(tmp_path):
    """Re-indexing a path replaces the old content."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    doc_dir = nas / "personal" / "alice" / "Documents"
    doc_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = doc_dir / "notes.txt"
    doc.write_text("original content banana")

    from app.document_index import index_document, search_documents
    await index_document(str(doc), doc.name, "alice")

    results = await search_documents("banana", user_role="admin", username="")
    assert len(results) == 1

    # Update file and re-index
    doc.write_text("updated content mango")
    await index_document(str(doc), doc.name, "alice")

    # Old content gone
    results_old = await search_documents("banana", user_role="admin", username="")
    assert results_old == []

    # New content found
    results_new = await search_documents("mango", user_role="admin", username="")
    assert len(results_new) == 1


# ---------------------------------------------------------------------------
# OCR: missing binaries degrade gracefully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_index_missing_file_does_not_raise(tmp_path):
    """index_document() on a non-existent file stores '' and does not raise."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    (nas / "personal" / "alice" / "Documents").mkdir(parents=True)
    settings.nas_root = nas

    from app.document_index import index_document, search_documents
    # Path does not exist — should complete without exception
    fake_path = str(nas / "personal" / "alice" / "Documents" / "ghost.pdf")
    await index_document(fake_path, "ghost.pdf", "alice")


# ---------------------------------------------------------------------------
# Search endpoint (integration via AsyncClient)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_endpoint_requires_auth(client):
    """GET /api/v1/files/search without token → 401."""
    resp = await client.get("/api/v1/files/search", params={"q": "test"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_search_endpoint_empty_results(client, admin_token):
    """Search endpoint returns empty list when index is empty."""
    resp = await client.get(
        "/api/v1/files/search",
        params={"q": "zzznomatch"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["results"] == []
    assert body["query"] == "zzznomatch"


@pytest.mark.asyncio
async def test_search_endpoint_returns_indexed_doc(client, admin_token, tmp_path):
    """After indexing a document, the search endpoint finds it."""
    from app.config import settings
    nas = settings.nas_root
    doc_dir = nas / "personal" / "admin" / "Documents"
    doc_dir.mkdir(parents=True, exist_ok=True)

    doc = doc_dir / "test_search.txt"
    doc.write_text("unique searchable content for endpoint test rubidium")

    from app.document_index import init_db, index_document
    await init_db()
    await index_document(str(doc), doc.name, "admin")

    resp = await client.get(
        "/api/v1/files/search",
        params={"q": "rubidium"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["results"][0]["filename"] == "test_search.txt"


# ---------------------------------------------------------------------------
# Query normalization & alias expansion
# ---------------------------------------------------------------------------

def test_normalize_query_aliases():
    """Known aliases expand to FTS5 OR queries."""
    from app.document_index import _normalize_query
    result = _normalize_query("aadhar")
    assert "aadhaar" in result
    assert "aadhar" in result
    assert "OR" in result

    result2 = _normalize_query("pan")
    assert "pancard" in result2


def test_normalize_query_prefix_matching():
    """Single short word gets prefix expansion."""
    from app.document_index import _normalize_query
    result = _normalize_query("hello")
    assert "hello*" in result


def test_normalize_query_passthrough():
    """Multi-word queries pass through unchanged."""
    from app.document_index import _normalize_query
    assert _normalize_query("foo bar baz") == "foo bar baz"


@pytest.mark.asyncio
async def test_search_aadhar_misspelling_finds_aadhaar(tmp_path):
    """Searching 'aadhar' (common misspelling) finds docs containing 'aadhaar'."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    doc_dir = nas / "personal" / "admin" / "Documents"
    doc_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = doc_dir / "id_card.txt"
    doc.write_text("Unique Identification Authority AADHAAR card number 1234 5678 9012")

    from app.document_index import index_document, search_documents
    await index_document(str(doc), doc.name, "admin")

    # 'aadhar' (single a) should still find the doc containing 'AADHAAR'
    results = await search_documents("aadhar", user_role="admin", username="")
    assert len(results) >= 1
    assert any(r["filename"] == "id_card.txt" for r in results)


@pytest.mark.asyncio
async def test_search_dl_alias_finds_license(tmp_path):
    """Searching 'dl' finds docs about driving license."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    doc_dir = nas / "personal" / "admin" / "Documents"
    doc_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = doc_dir / "driving.txt"
    doc.write_text("Driving licence number KA53 valid till 2036")

    from app.document_index import index_document, search_documents
    await index_document(str(doc), doc.name, "admin")

    results = await search_documents("dl", user_role="admin", username="")
    assert len(results) >= 1
    assert any(r["filename"] == "driving.txt" for r in results)


# ---------------------------------------------------------------------------
# list_recent_documents — LIKE-escaping (same class as _search_sync's own_prefix)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_recent_username_with_underscore_wildcard_does_not_leak_other_users_docs(tmp_path):
    """A member named "a_b" listing recents must not match "/personal/axb/..." — SQL LIKE
    treats a bare "_" as a single-character wildcard. Mirrors
    test_member_username_with_like_wildcard_does_not_leak_other_users_docs for _search_sync."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    axb_dir = nas / "personal" / "axb" / "Documents"
    axb_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = axb_dir / "secret.txt"
    doc.write_text("axb private data")

    from app.document_index import index_document, list_recent_documents
    await index_document(str(doc), doc.name, "axb")

    results = await list_recent_documents(limit=10, user_role="member", username="a_b")
    assert results == [], "unescaped LIKE wildcard in username must not match a different user's docs"


@pytest.mark.asyncio
async def test_list_recent_username_with_percent_wildcard_does_not_leak_other_users_docs(tmp_path):
    """A member named "a%" listing recents must not match "/personal/anything/..." — "%" is the
    zero-or-more-characters LIKE wildcard."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    other_dir = nas / "personal" / "anything" / "Documents"
    other_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = other_dir / "secret.txt"
    doc.write_text("someone else's private data")

    from app.document_index import index_document, list_recent_documents
    await index_document(str(doc), doc.name, "anything")

    results = await list_recent_documents(limit=10, user_role="member", username="a%")
    assert results == [], "unescaped '%' wildcard in username must not match a different user's docs"


@pytest.mark.asyncio
async def test_list_recent_username_with_literal_backslash_is_treated_literally(tmp_path):
    """A username containing a literal backslash must not be interpreted as an escape
    character itself — it should only ever match a folder named with that exact backslash."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    real_dir = nas / "personal" / "a" / "Documents"
    real_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = real_dir / "secret.txt"
    doc.write_text("a's private data")

    from app.document_index import index_document, list_recent_documents
    await index_document(str(doc), doc.name, "a")

    # username "a\_" must not degrade into matching plain "a" via a mishandled backslash.
    results = await list_recent_documents(limit=10, user_role="member", username="a\\_")
    assert results == [], "a literal backslash in the username must not enable an escape bypass"


@pytest.mark.asyncio
async def test_list_recent_still_returns_own_docs_for_a_normal_username(tmp_path):
    """Positive control: escaping must not break the ordinary case of a member seeing their
    own recently indexed documents."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    alice_dir = nas / "personal" / "alice" / "Documents"
    alice_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = alice_dir / "invoice.txt"
    doc.write_text("alice's own document")

    from app.document_index import index_document, list_recent_documents
    await index_document(str(doc), doc.name, "alice")

    results = await list_recent_documents(limit=10, user_role="member", username="alice")
    assert len(results) == 1
    assert results[0]["filename"] == "invoice.txt"


# ---------------------------------------------------------------------------
# remove_documents_by_prefix — LIKE-escaping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remove_prefix_with_underscore_in_folder_name_does_not_delete_sibling_folder(tmp_path):
    """Deleting/renaming a folder literally named "a_b" must not also purge the index for an
    unrelated sibling folder "axb" — "_" is a single-character LIKE wildcard."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    ab_dir = nas / "personal" / "a_b" / "Documents"
    axb_dir = nas / "personal" / "axb" / "Documents"
    ab_dir.mkdir(parents=True)
    axb_dir.mkdir(parents=True)
    settings.nas_root = nas

    ab_doc = ab_dir / "file.txt"
    ab_doc.write_text("a_b's file")
    axb_doc = axb_dir / "file.txt"
    axb_doc.write_text("axb's file")

    from app.document_index import index_document, remove_documents_by_prefix, list_recent_documents
    await index_document(str(ab_doc), ab_doc.name, "a_b")
    await index_document(str(axb_doc), axb_doc.name, "axb")

    removed = await remove_documents_by_prefix(str(nas / "personal" / "a_b"))

    assert removed == 1, "must remove exactly the intended folder's one doc, not axb's too"
    remaining = await list_recent_documents(limit=10, user_role="admin", username="")
    assert any(r["filename"] == "file.txt" and "axb" in r["path"] for r in remaining), (
        "unescaped '_' wildcard must not have deleted the unrelated sibling folder's index entry"
    )


@pytest.mark.asyncio
async def test_remove_prefix_with_percent_in_folder_name_does_not_delete_unrelated_folder(tmp_path):
    """A folder literally named "a%b" must not, via the unescaped "%" wildcard, purge the
    index for an unrelated folder like "aXYZb"."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    percent_dir = nas / "personal" / "a%b" / "Documents"
    unrelated_dir = nas / "personal" / "aXYZb" / "Documents"
    percent_dir.mkdir(parents=True)
    unrelated_dir.mkdir(parents=True)
    settings.nas_root = nas

    percent_doc = percent_dir / "file.txt"
    percent_doc.write_text("a%b's file")
    unrelated_doc = unrelated_dir / "file.txt"
    unrelated_doc.write_text("aXYZb's file")

    from app.document_index import index_document, remove_documents_by_prefix, list_recent_documents
    await index_document(str(percent_doc), percent_doc.name, "a%b")
    await index_document(str(unrelated_doc), unrelated_doc.name, "aXYZb")

    removed = await remove_documents_by_prefix(str(nas / "personal" / "a%b"))

    assert removed == 1, "must remove exactly the intended folder's one doc, not aXYZb's too"
    remaining = await list_recent_documents(limit=10, user_role="admin", username="")
    assert any(r["filename"] == "file.txt" and "aXYZb" in r["path"] for r in remaining), (
        "unescaped '%' wildcard must not have deleted the unrelated folder's index entry"
    )


@pytest.mark.asyncio
async def test_remove_prefix_still_deletes_the_real_matching_docs(tmp_path):
    """Positive control: escaping must not break ordinary prefix deletion."""
    await _make_index(tmp_path)
    nas = tmp_path / "nas"
    doc_dir = nas / "personal" / "bob" / "Documents"
    doc_dir.mkdir(parents=True)
    settings.nas_root = nas

    doc = doc_dir / "file.txt"
    doc.write_text("bob's file")

    from app.document_index import index_document, remove_documents_by_prefix, list_recent_documents
    await index_document(str(doc), doc.name, "bob")

    removed = await remove_documents_by_prefix(str(nas / "personal" / "bob"))

    assert removed == 1
    remaining = await list_recent_documents(limit=10, user_role="admin", username="")
    assert remaining == []
