import pytest

from app.config import settings


@pytest.mark.asyncio
async def test_cleanup_startup_artifacts_removes_known_test_files(tmp_path):
    settings.data_dir = tmp_path
    settings.nas_root = tmp_path / "nas"
    settings.personal_path.mkdir(parents=True, exist_ok=True)
    settings.family_path.mkdir(parents=True, exist_ok=True)

    doc_dir = settings.personal_path / "admin" / "Documents"
    doc_dir.mkdir(parents=True, exist_ok=True)

    keep = doc_dir / "notes.txt"
    drop1 = doc_dir / "hwtest_abcd1234.txt"
    drop2 = doc_dir / "stress_efgh5678.txt"
    keep.write_text("keep me")
    drop1.write_text("drop me")
    drop2.write_text("drop me too")

    from app.document_index import init_db, index_document, search_documents
    await init_db()
    await index_document(str(keep), keep.name, "admin")
    await index_document(str(drop1), drop1.name, "admin")
    await index_document(str(drop2), drop2.name, "admin")

    from app.hygiene import cleanup_startup_artifacts
    stats = await cleanup_startup_artifacts()

    assert stats["deleted_files"] == 2
    assert not drop1.exists()
    assert not drop2.exists()
    assert keep.exists()

    # Pattern artifacts removed from index, normal user file stays.
    stress_results = await search_documents("stress", user_role="admin", username="")
    hwtest_results = await search_documents("hwtest", user_role="admin", username="")
    keep_results = await search_documents("keep", user_role="admin", username="")
    assert stress_results == []
    assert hwtest_results == []
    assert any(r["filename"] == "notes.txt" for r in keep_results)
