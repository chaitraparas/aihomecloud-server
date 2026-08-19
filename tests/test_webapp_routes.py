"""
Serving the AiHomeCloud webapp web client.

The interesting tests here are the refusals. `/app/{asset:path}` accepts slashes on purpose so
`js/app.js` resolves — which is exactly what makes traversal possible if the guard is wrong. These
assert the guard holds for the encoded and dotted forms, not just the obvious one.
"""

import pytest
from httpx import AsyncClient


class TestServing:
    async def test_index_is_served(self, client: AsyncClient):
        r = await client.get("/app/")

        assert r.status_code == 200
        assert "AiHomeCloud" in r.text

    async def test_bare_path_redirects_to_the_trailing_slash(self, client: AsyncClient):
        """
        Without this the page loads and then renders blank.

        index.html references assets relatively, so a browser at `/webapp` requests
        `/css/app.css` rather than `/app/css/app.css` and every script fails. Curl does not
        show it — fetching the prefixed URL by hand works fine — so this needs its own test.
        """
        r = await client.get("/app", follow_redirects=False)

        assert r.status_code == 308
        assert r.headers["location"] == "/app/"

    async def test_relative_asset_paths_resolve_after_the_redirect(self, client: AsyncClient):
        """The actual property that matters: what a browser ends up requesting."""
        r = await client.get("/app", follow_redirects=True)
        assert r.status_code == 200

        # Same relative reference the HTML uses, resolved against the final URL.
        asset = await client.get(str(r.url.join("css/app.css")))
        assert asset.status_code == 200

    async def test_index_is_not_cached(self, client: AsyncClient):
        """A stale shell against a moved API is a confusing failure; never cache it."""
        r = await client.get("/app/")

        assert "no-store" in r.headers.get("cache-control", "")

    async def test_assets_load(self, client: AsyncClient):
        for asset in ("js/app.js", "js/api.js", "css/app.css"):
            r = await client.get(f"/app/{asset}")
            assert r.status_code == 200, asset
            assert len(r.content) > 0, asset

    async def test_no_auth_needed_for_the_shell(self, client: AsyncClient):
        """The page carries no secrets; every API call it makes still needs a bearer token."""
        r = await client.get("/app/", headers={})

        assert r.status_code == 200


class TestTraversalIsRefused:
    @pytest.mark.parametrize("attack", [
        "../../../etc/passwd",
        "../../config.py",
        "..%2f..%2fconfig.py",
        "js/../../../etc/hosts",
        "....//....//config.py",
    ])
    async def test_paths_outside_the_webapp_directory_are_refused(
        self, client: AsyncClient, attack: str
    ):
        r = await client.get(f"/app/{attack}")

        assert r.status_code == 404, f"{attack} returned {r.status_code}"
        assert "root:" not in r.text
        assert "SECRET" not in r.text.upper()

    async def test_a_real_backend_file_is_not_reachable(self, client: AsyncClient):
        r = await client.get("/app/../routes/webapp_routes.py")

        assert r.status_code == 404

    async def test_disallowed_suffixes_are_refused(self, client: AsyncClient):
        """Even inside the directory, only web asset types load — not .md or .py."""
        r = await client.get("/app/README.md")

        assert r.status_code == 404


class TestTheOldNameStillWorks:
    """
    The web client was called Hearth and lived at /hearth until 2026-08-08.

    Renaming is cheap; breaking every bookmark and every link already sent to a family member is
    not. These redirects are the whole reason the rename could be done at all.
    """

    async def test_the_old_root_redirects(self, client):
        res = await client.get("/hearth", follow_redirects=False)

        assert res.status_code == 308
        assert res.headers["location"] == "/app/"

    async def test_the_old_trailing_slash_redirects(self, client):
        res = await client.get("/hearth/", follow_redirects=False)

        assert res.status_code == 308
        assert res.headers["location"] == "/app/"

    async def test_a_deep_link_keeps_its_path(self, client):
        """A link to a specific screen must land on that screen, not the front door."""
        res = await client.get("/hearth/js/app.js", follow_redirects=False)

        assert res.status_code == 308
        assert res.headers["location"] == "/app/js/app.js"
