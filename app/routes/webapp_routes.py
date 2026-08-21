"""
Serve the AiHomeCloud web client as static files.

Built independently against the requirements spec, since confirmed genuinely wired to the real
API (not the mock-data prototype it was once mistaken for — see kb/status.md, 2026-08-20); it
ships here so the same board that serves the API also serves the page. That is not a convenience
— it is what makes the client usable at all. Loaded from anywhere else it would face a
cross-origin request to a host presenting a self-signed certificate the browser never had a
chance to accept, which no amount of client code fixes. Same-origin, both problems disappear.

Unauthenticated by design, exactly like `/web` and `/browse`: the page carries no secrets and every
API call it makes needs a bearer token, so serving the shell to an anonymous visitor discloses
nothing. Access control lives on the API, not on the HTML.

Files land in `app/static/webapp/` by two different platform-specific mechanisms that both target
this same directory: on Linux, staged at deploy time (`backend/scripts/stage_webapp.sh`), per the
standing rule that every board serves every feature from its own local state rather than reaching
for another board; on Windows, bundled directly into the installer's own payload
(`backend/installer/AiHomeCloud.iss`'s `[Files]` section), which install_windows.ps1's
Copy-BackendCode then copies wholesale into the real InstallDir along with everything else. Source
of truth for the page itself is `clients/web/` (a separate, proprietary directory, outside
`backend/`) — not this directory. `backend/` is AGPL-3.0; the web client is the branded product
surface and stays out of that boundary (see the repo root LICENSE for the full reasoning). Run the
stage script (or a fresh deploy, or rebuild the Windows installer) after editing anything under
`clients/web/`.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

router = APIRouter(tags=["webapp"])

_ROOT = Path(__file__).resolve().parent.parent / "static" / "webapp"

#: Only these load; anything else is a 404 rather than a path to probe with.
_ALLOWED_SUFFIXES = {".js", ".css", ".html", ".svg", ".png", ".webp", ".ico", ".woff2"}


def _no_store(path: Path) -> FileResponse:
    # Content-Security-Policy is the backstop for the escaping in the app's own JS. The webapp is
    # entirely self-hosted — no CDN, no external fonts, no analytics — so a strict policy costs
    # nothing and turns any future escaping slip from "stored XSS with a signed-in member's session"
    # into a blocked console error. 'unsafe-inline' is unavoidable for now: the screens build their
    # markup as template strings with inline style attributes, and removing that is a rewrite.
    _CSP = (
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
        "font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    )
    return FileResponse(
        path,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache",
                 "Content-Security-Policy": _CSP, "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer"},
    )


@router.get("/app", include_in_schema=False)
async def webapp_index_redirect():
    """
    Redirect to the trailing-slash form, which is the only reason the page works at all.

    `index.html` references its assets relatively (`css/app.css`, `js/app.js`). A browser at
    `/app` resolves those against `/`, so it requests `/css/app.css` — which does not exist —
    and every script and stylesheet fails. The page then renders blank with no error a person can
    see. At `/app/` the same relative paths resolve to `/app/css/app.css` and everything
    loads.

    Curl hides this completely: fetching `/app/js/app.js` by hand succeeds, so asset checks
    pass while a real browser gets nothing. Found 2026-08-07 by loading the page in headless
    Chromium and watching for failed requests.
    """
    return RedirectResponse(url="/app/", status_code=status.HTTP_308_PERMANENT_REDIRECT)


@router.get("/app/", response_class=HTMLResponse, include_in_schema=False)
async def webapp_index():
    index = _ROOT / "index.html"
    if not index.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The web client is not installed on this board")
    return _no_store(index)


@router.get("/app/{asset:path}", include_in_schema=False)
async def webapp_asset(asset: str):
    """
    Serve one asset, refusing anything that resolves outside the web client's directory.

    `asset:path` accepts slashes, which is the point (`js/app.js`) and also the risk — a crafted
    `../../` would otherwise read arbitrary files as the service user. Resolving both sides and
    comparing is the check that actually holds; matching on ".." in the string does not, because
    the encoded and symlinked forms slip past it.
    """
    target = (_ROOT / asset).resolve()
    try:
        target.relative_to(_ROOT.resolve())
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not target.is_file() or target.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return _no_store(target)

# The web client was called "Hearth" until 2026-08-08 and lived at /hearth. Renamed because two
# names for one product is a tax paid forever by everyone reading the code or the docs — it is the
# AiHomeCloud webapp, the browser counterpart to the AiHomeCloud app. These two routes exist so a
# bookmark or a chat link from before the rename still opens it rather than 404ing.
@router.get("/hearth", include_in_schema=False)
@router.get("/hearth/{_rest:path}", include_in_schema=False)
async def legacy_hearth_redirect(_rest: str = ""):
    return RedirectResponse(
        url=f"/app/{_rest}" if _rest else "/app/",
        status_code=status.HTTP_308_PERMANENT_REDIRECT,
    )
