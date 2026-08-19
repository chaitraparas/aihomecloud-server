"""
The semantic-search endpoint.

The case that matters most is the one where the feature is *not* installed: onnxruntime is an
optional ~200 MB dependency, so most boards will hit that path, and it must be a clean, typed
refusal rather than a 500 from an import error.
"""

import numpy as np
import pytest
from httpx import AsyncClient

from app import embedding, vector_store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(vector_store.settings, "data_dir", tmp_path)
    vector_store._reset_for_tests()
    vector_store.init_db()
    yield
    vector_store.close_pool()


def unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(vector_store.DIM).astype(np.float32)
    return v / np.linalg.norm(v)


class TestWhenNotInstalled:
    @pytest.mark.asyncio
    async def test_refuses_with_501_rather_than_crashing(
        self, authenticated_client: AsyncClient, monkeypatch
    ):
        monkeypatch.setattr(embedding, "available", lambda *a, **k: False)

        resp = await authenticated_client.get("/api/v1/files/search/semantic?q=beach")

        # 501, not 503: this is not "try later", it is "this board does not do that", which is
        # what tells a client to stop offering the feature.
        assert resp.status_code == 501
        assert "not installed" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_does_not_import_the_runtime_to_answer(
        self, authenticated_client: AsyncClient, monkeypatch
    ):
        # If answering the unavailable case needed onnxruntime, every board without it would get
        # a 500 from the import instead of the 501 above.
        monkeypatch.setattr(embedding, "available", lambda *a, **k: False)
        called = False

        def explode(*a, **k):
            nonlocal called
            called = True
            raise AssertionError("provider must not be constructed when unavailable")

        monkeypatch.setattr(embedding, "OnnxEmbeddingProvider", explode)
        resp = await authenticated_client.get("/api/v1/files/search/semantic?q=beach")

        assert resp.status_code == 501
        assert called is False


class TestWhenInstalled:
    @pytest.mark.asyncio
    async def test_returns_ranked_hits(self, authenticated_client: AsyncClient, monkeypatch):
        target = unit(1)
        vector_store.upsert("Photos/beach.jpg", target, model="fake", content_key="k1")
        vector_store.upsert("Documents/tax.pdf", -target, model="fake", content_key="k2")

        class FakeProvider:
            model_name = "fake"
            def embed_query(self, text): return target

        monkeypatch.setattr(embedding, "available", lambda *a, **k: True)
        monkeypatch.setattr(embedding, "OnnxEmbeddingProvider", lambda *a, **k: FakeProvider())

        resp = await authenticated_client.get("/api/v1/files/search/semantic?q=beach%20photos")
        body = resp.json()

        assert resp.status_code == 200
        assert [r["path"] for r in body["results"]] == ["Photos/beach.jpg", "Documents/tax.pdf"]
        assert body["results"][0]["filename"] == "beach.jpg"
        assert body["count"] == 2
        assert body["model"] == "fake"
        assert body["query"] == "beach photos"

    @pytest.mark.asyncio
    async def test_an_empty_index_is_an_empty_result_not_an_error(
        self, authenticated_client: AsyncClient, monkeypatch
    ):
        class FakeProvider:
            model_name = "fake"
            def embed_query(self, text): return unit(9)

        monkeypatch.setattr(embedding, "available", lambda *a, **k: True)
        monkeypatch.setattr(embedding, "OnnxEmbeddingProvider", lambda *a, **k: FakeProvider())

        resp = await authenticated_client.get("/api/v1/files/search/semantic?q=anything")

        assert resp.status_code == 200
        assert resp.json()["results"] == []


class TestGuards:
    @pytest.mark.asyncio
    async def test_requires_authentication(self, client: AsyncClient):
        resp = await client.get("/api/v1/files/search/semantic?q=beach")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_rejects_an_empty_query(self, authenticated_client: AsyncClient):
        resp = await authenticated_client.get("/api/v1/files/search/semantic?q=")
        assert resp.status_code == 422
