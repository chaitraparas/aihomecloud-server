"""
The plain-HTTP page that explains the certificate warning.

It exists because "your connection is not secure" reads identically whether you are looking at a
phishing site or at your own NAS, and only one of those deserves alarm. It is also the only thing on
this board reachable without TLS, so the tests care as much about what it does NOT do as what it
does.
"""

import pytest
from starlette.testclient import TestClient

from app.http_helper import build_app


@pytest.fixture
def http_client():
    return TestClient(build_app())


class TestExplaining:
    def test_it_explains_the_warning_rather_than_redirecting(self, http_client):
        """A redirect would land the person on the warning with no explanation — the exact problem."""
        res = http_client.get("/", follow_redirects=False)

        assert res.status_code == 200
        assert "not secure" in res.text
        assert "expected" in res.text.lower()

    def test_it_links_through_to_the_app_over_https(self, http_client):
        res = http_client.get("/", headers={"host": "cubie.local"})

        assert 'href="https://cubie.local:' in res.text
        assert "/app/" in res.text

    def test_it_tells_people_when_not_to_proceed(self, http_client):
        """Teaching a family to click through warnings is only safe with the caveat attached."""
        res = http_client.get("/")

        assert "not</em> proceed" in res.text or "not proceed" in res.text.lower()

    def test_any_path_gets_the_explanation(self, http_client):
        """Someone mistyping a path should still be told what is going on."""
        assert http_client.get("/somewhere/else").status_code == 200


class TestNotBeingAnAttackSurface:
    def test_a_hostile_host_header_is_escaped_not_reflected(self, http_client):
        """Host is attacker-controllable; it is display text here, so it must be escaped."""
        res = http_client.get("/", headers={"host": "evil<script>alert(1)</script>"})

        assert "<script>alert(1)</script>" not in res.text
        assert "&lt;script&gt;" in res.text

    def test_it_accepts_no_writes(self, http_client):
        for method in ("post", "put", "delete", "patch"):
            res = getattr(http_client, method)("/")
            assert res.status_code == 405, f"{method.upper()} should not be routed"

    def test_it_is_never_cached(self, http_client):
        assert "no-store" in http_client.get("/").headers.get("cache-control", "")

    def test_a_missing_host_header_still_renders(self, http_client):
        res = http_client.get("/", headers={"host": ""})

        assert res.status_code == 200
        assert "this box" in res.text
