"""
UploadSizeLimitMiddleware — the cap has to bite before anything buffers the body.

`settings.max_upload_bytes` was previously only checked inside `ingest()`, by which point Starlette
had already parsed the multipart body and spooled it to disk. So an oversized upload filled the
board's storage and *then* got its 413, which on a 1 GB-RAM board sharing a filesystem with the
family's photos is the disk-fill it was supposed to prevent. (2026-08-08 audit, H-3.)

The assertion that matters throughout is not the status code — it is `consumed`, which records how
many body bytes the downstream app actually received.
"""

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.upload_guard import UploadSizeLimitMiddleware

CAP = 1024


@pytest.fixture
def harness():
    """A minimal ASGI app behind the middleware, recording every body length it receives."""
    consumed: list[int] = []

    async def sink(request):
        body = await request.body()
        consumed.append(len(body))
        return JSONResponse({"got": len(body)}, status_code=201)

    app = Starlette(routes=[Route("/upload", sink, methods=["POST"])])
    app.add_middleware(UploadSizeLimitMiddleware, max_bytes=CAP)
    return TestClient(app), consumed


def test_body_under_the_cap_passes_through_intact(harness):
    client, consumed = harness
    response = client.post("/upload", content=b"x" * 500)
    assert response.status_code == 201
    assert consumed == [500], "a legitimate upload must reach the app unchanged"


def test_oversized_content_length_is_refused_without_reading_the_body(harness):
    """
    The point of the fix: rejected before a single byte reaches the application.

    An empty `consumed` is the whole assertion. A 413 alone would also be produced by the old
    behaviour — after the body had already been written to disk.
    """
    client, consumed = harness
    response = client.post("/upload", content=b"x" * (CAP * 5))
    assert response.status_code == 413
    assert consumed == [], "body was buffered before rejection — the cap is not doing its job"


def test_cap_is_enforced_when_content_length_is_absent(harness):
    """
    `Content-Length` is client-supplied: it can be omitted (chunked) or simply be a lie.

    Counting bytes as they arrive is what makes the limit real rather than advisory, so this is the
    case that decides whether the header check is a shortcut or the entire implementation.
    """
    client, consumed = harness

    def chunked():
        for _ in range(10):
            yield b"x" * 500      # 5000 bytes total, no Content-Length

    response = client.post("/upload", content=chunked())
    assert response.status_code == 413
    assert all(n < CAP * 5 for n in consumed), "full oversized body reached the app"


def test_unparseable_content_length_still_falls_back_to_counting(harness):
    client, consumed = harness
    response = client.post(
        "/upload", content=b"x" * (CAP * 3), headers={"Content-Length": "not-a-number"},
    )
    # Either rejection path is acceptable; silently accepting the oversized body is not.
    assert response.status_code in (400, 413)
    assert all(n < CAP * 3 for n in consumed)


def test_zero_means_unlimited(harness):
    """`max_upload_bytes: int = 0` documents itself as unlimited — honour that, do not block all."""
    consumed: list[int] = []

    async def sink(request):
        consumed.append(len(await request.body()))
        return JSONResponse({}, status_code=201)

    app = Starlette(routes=[Route("/upload", sink, methods=["POST"])])
    app.add_middleware(UploadSizeLimitMiddleware, max_bytes=0)
    response = TestClient(app).post("/upload", content=b"x" * (CAP * 10))
    assert response.status_code == 201
    assert consumed == [CAP * 10]


def test_bodyless_methods_are_left_alone(harness):
    """GET/HEAD must not be wrapped — no reason to touch the hot read path."""
    client, _ = harness
    assert client.get("/upload").status_code == 405   # route rejects the method, guard is invisible


# ---------------------------------------------------------------------------
# Path containment (2026-08-08 adversarial pass)
#
# Two resolvers guarded the NAS root with `str(candidate).startswith(str(nas_root))`. That is a
# string prefix test, not a containment test: "/srv/nas" prefixes "/srv/nasty". Since .resolve()
# has already collapsed any "..", the comparison was the only thing standing between a caller and
# a path outside the root. Kept here rather than in a route test because the bug is in the shared
# helper, and it is the helper's contract that must not regress.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resolver_module,resolver_name", [
    ("app.routes.backup_routes", "_resolve_dup_path"),
    ("app.routes.local_backup_routes", "_resolve_protected_path"),
])
def test_sibling_directory_sharing_a_name_prefix_is_rejected(resolver_module, resolver_name,
                                                             monkeypatch, tmp_path):
    """`<root>/../<root-name>ty/x` must not pass as being inside `<root>`."""
    import importlib
    from fastapi import HTTPException

    from app.config import settings

    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    (tmp_path / "nasty").mkdir()          # the sibling whose name extends the root's
    monkeypatch.setattr(settings, "nas_root", nas_root)

    module = importlib.import_module(resolver_module)
    resolve = getattr(module, resolver_name)

    with pytest.raises(HTTPException) as exc:
        resolve("../nasty/secrets")
    assert exc.value.status_code == 400

    # URL-encoded traversal must not slip past either — these decode before resolving, which is
    # the correct order, and this pins that ordering.
    with pytest.raises(HTTPException):
        resolve("%2e%2e/nasty/secrets")


def test_legitimate_path_inside_the_root_still_resolves(monkeypatch, tmp_path):
    import importlib

    from app.config import settings

    nas_root = tmp_path / "nas"
    (nas_root / "family").mkdir(parents=True)
    monkeypatch.setattr(settings, "nas_root", nas_root)

    module = importlib.import_module("app.routes.local_backup_routes")
    assert module._resolve_protected_path("family") == (nas_root / "family").resolve()
