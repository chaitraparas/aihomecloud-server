"""
AiHomeCloud Web File Browser — serves a self-contained HTML page at GET /browse.

A Sweech-inspired LAN file browser that lets any device on the local network:
  • Browse the NAS folder tree
  • Preview images, video, audio, and PDF files inline
  • Download any file
  • Upload files via drag-and-drop or file picker

All functionality is in one self-contained HTML page with no external dependencies.

Reuses existing API endpoints:
  - GET  /api/v1/auth/users/names  → user picker
  - POST /api/v1/auth/login        → PIN authentication
  - GET  /api/v1/files/list        → directory listing
  - GET  /api/v1/files/download    → file download (fetch + blob)
  - POST /api/v1/files/upload      → file upload
  GET /browse/raw                  → inline file serving with query-param token (media preview)
"""

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from ..auth import get_current_user
from ..config import settings
from .file_routes import _authorize_path, _safe_resolve

logger = logging.getLogger("aihomecloud.web_browser")
router = APIRouter()

_STREAM_THRESHOLD = 1 * 1024 * 1024  # 1 MB
_CHUNK = 256 * 1024                   # 256 KB


# ── Short-lived, path-scoped media tokens ───────────────────────────────────
#
# Instead of leaking a long-lived full-access JWT as a URL query param (which
# lands in access logs, browser history, Referer headers, and every <img>/
# <video> src), media previews use a 60-second HMAC token scoped to one path.

_MEDIA_TOKEN_TTL = 60  # seconds


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _make_media_token(path: str, exp: int) -> str:
    payload = json.dumps({"path": path, "exp": exp}, separators=(",", ":")).encode()
    sig = hmac.new(
        settings.jwt_secret.encode(),
        f"{path}|{exp}".encode(),
        hashlib.sha256,
    ).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def _verify_media_token(token: str, path: str) -> bool:
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = json.loads(_b64url_decode(payload_b64))
        exp = int(payload["exp"])
        tok_path = payload["path"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False

    if exp <= int(time.time()):
        return False
    if tok_path != path:
        return False

    expected = hmac.new(
        settings.jwt_secret.encode(),
        f"{tok_path}|{exp}".encode(),
        hashlib.sha256,
    ).digest()
    try:
        provided = _b64url_decode(sig_b64)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, provided)


@router.get("/api/v1/files/media-token")
async def media_token(
    path: str = Query(..., description="NAS path to mint a media token for"),
    user: dict = Depends(get_current_user),
):
    """
    Mint a short-lived (60s), path-scoped, HMAC-signed token for inline media
    preview. Requires a normal Bearer JWT. The returned token is only valid for
    the exact *path* requested and expires quickly, so it is safe to embed in a
    URL query param (unlike the full-access JWT).
    """
    resolved = _safe_resolve(path)
    await _authorize_path(resolved, user)
    exp = int(time.time()) + _MEDIA_TOKEN_TTL
    return {"token": _make_media_token(path, exp), "expires_in": _MEDIA_TOKEN_TTL}


# ── Raw file proxy (used for inline media preview with a short-lived token) ──

@router.get("/browse/raw")
async def browse_raw(
    path: str = Query(..., description="NAS path to serve inline"),
    token: str = Query(..., description="Short-lived path-scoped media token"),
):
    """
    Serve a NAS file inline for use as an <img>, <video>, <audio>, or <iframe> src.
    Accepts a short-lived, path-scoped media token (see /api/v1/files/media-token)
    rather than a long-lived JWT, so nothing sensitive leaks into the URL.
    """
    if not _verify_media_token(token, path):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    resolved = _safe_resolve(path)
    if not resolved.exists() or resolved.is_dir():
        raise HTTPException(status_code=404, detail="Not found")

    mime, _ = mimetypes.guess_type(resolved.name)
    mime = mime or "application/octet-stream"
    file_size = resolved.stat().st_size

    if file_size > _STREAM_THRESHOLD:
        async def _iter():
            with open(resolved, "rb") as f:
                while True:
                    chunk = f.read(_CHUNK)
                    if not chunk:
                        break
                    yield chunk
        return StreamingResponse(
            _iter(),
            media_type=mime,
            headers={"Content-Length": str(file_size)},
        )

    return FileResponse(
        path=str(resolved),
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{resolved.name}"'},
    )


# ── Self-contained HTML page ──────────────────────────────────────────────────

@router.get("/browse", response_class=HTMLResponse)
async def browse():
    return HTMLResponse(_HTML)


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AiHomeCloud — Files</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🏠</text></svg>">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg: #0f0f18;
      --surface: #1a1a2e;
      --surface2: #242438;
      --border: #2e2e48;
      --primary: #7c6af7;
      --primary-dim: #5c4dd4;
      --text: #e8e8f0;
      --text2: #9090b0;
      --text3: #606080;
      --error: #ff5555;
      --success: #55cc88;
      --radius: 12px;
      --radius-sm: 8px;
    }

    html, body {
      height: 100%;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      font-size: 14px;
      line-height: 1.5;
    }

    /* ── Scrollbar ─────────────────────────────────────────────────────── */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

    /* ── Views ─────────────────────────────────────────────────────────── */
    .view { display: none; height: 100%; }
    .view.active { display: flex; }

    /* ════════════════════════════════════════════
       LOGIN VIEW
    ════════════════════════════════════════════ */
    #view-login {
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 32px 16px;
      background: radial-gradient(ellipse at 50% 0%, #1e1a4a 0%, var(--bg) 70%);
    }

    .login-logo {
      font-size: 48px;
      margin-bottom: 8px;
    }
    .login-title {
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0.3px;
      margin-bottom: 4px;
    }
    .login-sub {
      color: var(--text2);
      font-size: 13px;
      margin-bottom: 48px;
    }

    .user-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 28px 36px;
      justify-content: center;
      max-width: 480px;
      margin-bottom: 40px;
    }

    .user-tile {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 10px;
      cursor: pointer;
      transition: transform .15s;
    }
    .user-tile:hover { transform: scale(1.06); }
    .user-tile.selected .avatar-ring { outline: 3px solid #fff; }

    .avatar {
      width: 80px; height: 80px;
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 32px;
      font-weight: 700;
      color: #fff;
      position: relative;
      transition: box-shadow .2s;
    }
    .user-tile.selected .avatar {
      box-shadow: 0 0 0 3px #fff, 0 0 20px 4px var(--primary);
    }
    .avatar-letter { font-size: 30px; font-weight: 700; }

    .user-name {
      font-size: 13px;
      color: var(--text2);
      font-weight: 500;
    }
    .user-tile.selected .user-name { color: #fff; font-weight: 600; }

    .login-spinner {
      width: 28px; height: 28px;
      border: 3px solid rgba(255,255,255,.2);
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin .7s linear infinite;
    }

    .pin-section {
      display: none;
      flex-direction: column;
      align-items: center;
      gap: 12px;
      width: 100%;
      max-width: 280px;
      animation: fadeUp .22s ease;
    }
    .pin-section.visible { display: flex; }

    .pin-label {
      color: var(--text2);
      font-size: 13px;
      align-self: flex-start;
    }

    .pin-input {
      width: 100%;
      background: var(--surface);
      border: 1.5px solid var(--border);
      border-radius: var(--radius-sm);
      color: #fff;
      font-size: 22px;
      letter-spacing: 10px;
      padding: 12px 16px;
      text-align: center;
      outline: none;
      transition: border-color .2s;
      -webkit-text-security: disc;
    }
    .pin-input:focus { border-color: var(--primary); }

    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      padding: 11px 20px;
      border: none; border-radius: var(--radius-sm);
      font-size: 14px; font-weight: 600;
      cursor: pointer; transition: opacity .15s, transform .1s;
    }
    .btn:active { transform: scale(.97); }
    .btn:disabled { opacity: .5; cursor: default; }
    .btn-primary { background: var(--primary); color: #fff; }
    .btn-primary:hover:not(:disabled) { background: var(--primary-dim); }
    .btn-ghost { background: transparent; color: var(--text2); }
    .btn-ghost:hover { color: var(--text); background: var(--surface2); }
    .btn-danger { background: transparent; color: var(--error); }
    .btn-danger:hover { background: rgba(255,85,85,.1); }

    .error-msg {
      color: var(--error);
      font-size: 12px;
      text-align: center;
    }

    /* ════════════════════════════════════════════
       BROWSER VIEW
    ════════════════════════════════════════════ */
    #view-browser {
      flex-direction: column;
      height: 100vh;
      overflow: hidden;
    }

    /* ── Header ──────────────────────────────── */
    .header {
      display: flex; align-items: center; gap: 12px;
      padding: 0 20px;
      height: 56px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
      z-index: 10;
    }
    .header-logo { font-size: 22px; flex-shrink: 0; }
    .breadcrumbs {
      display: flex; align-items: center; gap: 4px;
      flex: 1; overflow: hidden;
    }
    .crumb {
      color: var(--text2); font-size: 13px; white-space: nowrap;
      cursor: pointer; padding: 3px 5px; border-radius: 4px;
    }
    .crumb:hover { color: var(--text); background: var(--surface2); }
    .crumb.active { color: var(--text); font-weight: 600; cursor: default; }
    .crumb.active:hover { background: transparent; }
    .crumb-sep { color: var(--text3); font-size: 13px; flex-shrink: 0; }

    .header-user {
      display: flex; align-items: center; gap: 8px;
      flex-shrink: 0;
    }
    .header-avatar {
      width: 30px; height: 30px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 14px; font-weight: 700; color: #fff;
    }
    .header-username { font-size: 13px; color: var(--text2); }

    /* ── Toolbar ─────────────────────────────── */
    .toolbar {
      display: flex; align-items: center; gap: 8px;
      padding: 10px 20px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
      background: var(--bg);
    }
    .toolbar-left { display: flex; align-items: center; gap: 8px; flex: 1; }
    .toolbar-right { display: flex; align-items: center; gap: 6px; }

    .upload-btn {
      display: flex; align-items: center; gap: 6px;
      padding: 7px 14px;
      background: var(--primary); color: #fff;
      border: none; border-radius: var(--radius-sm);
      font-size: 13px; font-weight: 600; cursor: pointer;
      transition: background .15s;
    }
    .upload-btn:hover { background: var(--primary-dim); }

    #upload-input { display: none; }

    .sort-select {
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      color: var(--text);
      padding: 6px 10px;
      font-size: 12px;
      cursor: pointer;
      outline: none;
    }

    .view-toggle {
      display: flex; gap: 2px;
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 3px;
    }
    .view-btn {
      background: none; border: none; cursor: pointer;
      color: var(--text3); padding: 4px 7px; border-radius: 5px;
      font-size: 14px; transition: background .15s, color .15s;
    }
    .view-btn.active { background: var(--surface); color: var(--text); }

    .item-count { color: var(--text3); font-size: 12px; }

    /* ── File area ───────────────────────────── */
    .file-area {
      flex: 1; overflow-y: auto;
      padding: 20px;
      position: relative;
    }

    /* Drop overlay */
    .drop-overlay {
      display: none;
      position: absolute; inset: 0;
      background: rgba(124, 106, 247, 0.15);
      border: 3px dashed var(--primary);
      border-radius: var(--radius);
      z-index: 100;
      align-items: center; justify-content: center;
      flex-direction: column; gap: 12px;
      pointer-events: none;
    }
    .drop-overlay.visible { display: flex; }
    .drop-icon { font-size: 52px; }
    .drop-label { font-size: 18px; font-weight: 600; color: var(--primary); }

    /* Empty state */
    .empty-state {
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; gap: 12px;
      height: 200px;
      color: var(--text3);
    }
    .empty-icon { font-size: 48px; }
    .empty-label { font-size: 14px; }

    /* Loading state */
    .loading-state {
      display: flex; align-items: center; justify-content: center;
      height: 200px;
    }
    .spinner {
      width: 36px; height: 36px;
      border: 3px solid var(--border);
      border-top-color: var(--primary);
      border-radius: 50%;
      animation: spin .7s linear infinite;
    }

    /* ── Grid view ───────────────────────────── */
    .file-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
      gap: 12px;
    }

    .file-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px 12px 12px;
      cursor: pointer;
      display: flex; flex-direction: column; align-items: center; gap: 8px;
      transition: background .15s, border-color .15s, transform .12s;
      text-align: center; position: relative;
    }
    .file-card:hover {
      background: var(--surface2);
      border-color: var(--primary);
      transform: translateY(-2px);
    }

    .file-icon { font-size: 38px; line-height: 1; }

    .file-thumb {
      width: 72px; height: 72px;
      object-fit: cover;
      border-radius: 6px;
      background: var(--surface2);
    }

    .file-name {
      font-size: 12px; font-weight: 500;
      word-break: break-word;
      max-width: 110px;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .file-meta {
      font-size: 10px; color: var(--text3);
    }

    .download-badge {
      position: absolute; top: 6px; right: 6px;
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 2px 5px;
      font-size: 10px;
      color: var(--text3);
      opacity: 0;
      transition: opacity .15s;
    }
    .file-card:hover .download-badge { opacity: 1; }

    /* ── List view ───────────────────────────── */
    .file-list { display: flex; flex-direction: column; gap: 2px; }

    .file-row {
      display: flex; align-items: center; gap: 12px;
      padding: 9px 12px;
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: background .12s;
    }
    .file-row:hover { background: var(--surface); }

    .row-icon { font-size: 22px; flex-shrink: 0; width: 28px; text-align: center; }

    .row-thumb {
      width: 28px; height: 28px;
      object-fit: cover; border-radius: 4px;
      flex-shrink: 0;
    }

    .row-name {
      flex: 1; font-size: 13px; font-weight: 500;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .row-size { color: var(--text3); font-size: 12px; width: 80px; text-align: right; flex-shrink: 0; }
    .row-date { color: var(--text3); font-size: 12px; width: 130px; text-align: right; flex-shrink: 0; }
    .row-action {
      width: 28px; flex-shrink: 0; display: flex; align-items: center; justify-content: center;
      opacity: 0; transition: opacity .15s;
    }
    .file-row:hover .row-action { opacity: 1; }
    .dl-btn {
      background: none; border: none; cursor: pointer;
      color: var(--text2); font-size: 16px; padding: 3px;
      border-radius: 4px;
    }
    .dl-btn:hover { background: var(--surface2); color: var(--primary); }

    /* ── Upload progress ─────────────────────── */
    .upload-progress {
      position: fixed; bottom: 24px; right: 24px;
      width: 300px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
      z-index: 200;
      display: none;
      flex-direction: column; gap: 10px;
      box-shadow: 0 8px 32px rgba(0,0,0,.4);
    }
    .upload-progress.visible { display: flex; }
    .upload-header { display: flex; align-items: center; justify-content: space-between; }
    .upload-title { font-size: 13px; font-weight: 600; }
    .upload-close { background: none; border: none; cursor: pointer; color: var(--text3); font-size: 16px; }
    .upload-files { display: flex; flex-direction: column; gap: 8px; max-height: 200px; overflow-y: auto; }
    .upload-file { display: flex; flex-direction: column; gap: 4px; }
    .upload-file-name { font-size: 12px; color: var(--text2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .progress-bar { height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
    .progress-fill { height: 100%; background: var(--primary); border-radius: 2px; transition: width .2s; }
    .progress-fill.done { background: var(--success); }
    .progress-fill.error { background: var(--error); }

    /* ── Preview modal ───────────────────────── */
    .modal-overlay {
      display: none;
      position: fixed; inset: 0;
      background: rgba(0,0,0,.85);
      z-index: 300;
      align-items: center; justify-content: center;
      padding: 20px;
    }
    .modal-overlay.visible { display: flex; }

    .modal {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      max-width: 90vw;
      max-height: 90vh;
      display: flex; flex-direction: column;
      overflow: hidden;
      box-shadow: 0 20px 60px rgba(0,0,0,.6);
    }

    .modal-header {
      display: flex; align-items: center; gap: 12px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }
    .modal-filename {
      flex: 1; font-size: 14px; font-weight: 600;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .modal-dl-btn {
      background: var(--primary); color: #fff;
      border: none; border-radius: var(--radius-sm);
      padding: 6px 14px; font-size: 12px; font-weight: 600;
      cursor: pointer; flex-shrink: 0;
    }
    .modal-close {
      background: none; border: none; cursor: pointer;
      color: var(--text2); font-size: 20px; flex-shrink: 0;
      padding: 4px; border-radius: 4px;
    }
    .modal-close:hover { background: var(--surface2); color: var(--text); }

    .modal-body {
      flex: 1; overflow: auto;
      display: flex; align-items: center; justify-content: center;
      min-height: 200px; min-width: 300px;
      padding: 8px;
    }

    .preview-img {
      max-width: 100%; max-height: 75vh;
      object-fit: contain; border-radius: 6px;
    }
    .preview-video { max-width: 100%; max-height: 75vh; border-radius: 6px; }
    .preview-audio { width: 320px; }
    .preview-pdf { width: 80vw; height: 75vh; border: none; border-radius: 6px; }
    .preview-text {
      background: var(--bg);
      border-radius: 6px;
      padding: 16px;
      font-family: monospace;
      font-size: 12px;
      white-space: pre-wrap;
      word-break: break-all;
      max-width: 700px;
      max-height: 75vh;
      overflow: auto;
      color: var(--text);
      width: 100%;
    }

    /* ── Pagination ──────────────────────────── */
    .pagination {
      display: flex; align-items: center; justify-content: center; gap: 10px;
      padding: 16px 0 4px;
    }
    .page-btn {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius-sm); color: var(--text2);
      padding: 6px 14px; font-size: 12px; cursor: pointer;
    }
    .page-btn:hover:not(:disabled) { border-color: var(--primary); color: var(--text); }
    .page-btn:disabled { opacity: .4; cursor: default; }
    .page-info { color: var(--text3); font-size: 12px; }

    /* ── Animations ──────────────────────────── */
    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes fadeUp {
      from { opacity: 0; transform: translateY(8px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    /* ── Mobile ──────────────────────────────── */
    @media (max-width: 600px) {
      .header { padding: 0 12px; }
      .toolbar { padding: 8px 12px; }
      .file-area { padding: 12px; }
      .row-date { display: none; }
      .file-grid { grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); gap: 8px; }
    }
  </style>
</head>
<body>

<!-- ════════════════════════════════════════
     LOGIN VIEW
════════════════════════════════════════ -->
<div id="view-login" class="view active">
  <div class="login-logo">🏠</div>
  <div class="login-title">AiHomeCloud</div>
  <div class="login-sub">Choose your profile</div>

  <div class="user-grid" id="user-grid">
    <div class="loading-state"><div class="spinner"></div></div>
  </div>

  <div class="pin-section" id="pin-section">
    <div class="pin-label" id="pin-label">PIN for <strong></strong></div>
    <input type="password" class="pin-input" id="pin-input"
           maxlength="16" inputmode="numeric"
           placeholder="••••" autocomplete="off">
    <div class="error-msg" id="login-error"></div>
    <button class="btn btn-ghost" onclick="clearSelection()">← Back</button>
  </div>
</div>

<!-- ════════════════════════════════════════
     BROWSER VIEW
════════════════════════════════════════ -->
<div id="view-browser" class="view">

  <!-- Header -->
  <div class="header">
    <div class="header-logo">🏠</div>
    <div class="breadcrumbs" id="breadcrumbs"></div>
    <div class="header-user">
      <div class="header-avatar" id="header-avatar"></div>
      <span class="header-username" id="header-username"></span>
      <button class="btn btn-ghost" style="padding:6px 10px;font-size:12px" onclick="logout()">Sign out</button>
    </div>
  </div>

  <!-- Toolbar -->
  <div class="toolbar">
    <div class="toolbar-left">
      <button class="upload-btn" onclick="triggerUpload()">
        <span>⬆</span> Upload
      </button>
      <input type="file" id="upload-input" multiple onchange="handleFileSelect(event)">
      <select class="sort-select" id="sort-select" onchange="changeSort()">
        <option value="name">Sort: Name</option>
        <option value="modified">Sort: Date</option>
        <option value="size">Sort: Size</option>
      </select>
      <button class="btn btn-ghost" style="padding:5px 8px;font-size:11px" id="sort-dir-btn" onclick="toggleSortDir()">↑ ASC</button>
    </div>
    <div class="toolbar-right">
      <span class="item-count" id="item-count"></span>
      <div class="view-toggle">
        <button class="view-btn active" id="btn-grid" onclick="setView('grid')" title="Grid view">⊞</button>
        <button class="view-btn" id="btn-list" onclick="setView('list')" title="List view">☰</button>
      </div>
    </div>
  </div>

  <!-- File area -->
  <div class="file-area" id="file-area"
       ondragenter="onDragEnter(event)"
       ondragover="onDragOver(event)"
       ondragleave="onDragLeave(event)"
       ondrop="onDrop(event)">

    <div class="drop-overlay" id="drop-overlay">
      <div class="drop-icon">📂</div>
      <div class="drop-label">Drop files to upload here</div>
    </div>

    <div id="file-content"></div>

    <div class="pagination" id="pagination" style="display:none">
      <button class="page-btn" id="page-prev" onclick="prevPage()">← Prev</button>
      <span class="page-info" id="page-info"></span>
      <button class="page-btn" id="page-next" onclick="nextPage()">Next →</button>
    </div>
  </div>

</div>

<!-- Upload progress toast -->
<div class="upload-progress" id="upload-progress">
  <div class="upload-header">
    <span class="upload-title">Uploading</span>
    <button class="upload-close" onclick="closeUploadToast()">✕</button>
  </div>
  <div class="upload-files" id="upload-files"></div>
</div>

<!-- Preview modal -->
<div class="modal-overlay" id="modal-overlay" onclick="closeModal(event)">
  <div class="modal" id="modal" onclick="event.stopPropagation()">
    <div class="modal-header">
      <span class="modal-filename" id="modal-filename"></span>
      <button class="modal-dl-btn" onclick="downloadCurrent()">⬇ Download</button>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
'use strict';

// ─────────────────────────────────────────────────────────────────────────────
// Constants & State
// ─────────────────────────────────────────────────────────────────────────────
const BASE = '/api/v1';
const PAGE_SIZE = 200;

let token = sessionStorage.getItem('ahc_token') || '';
let currentUser = sessionStorage.getItem('ahc_user') || '';
let currentPath = '/';
let currentItems = [];
let viewMode = localStorage.getItem('ahc_view') || 'grid';
let sortBy = 'name';
let sortAsc = true;
let currentPage = 0;
let totalItems = 0;
let selectedUser = null;
let currentPreviewPath = null;
let dragCounter = 0;

// ─────────────────────────────────────────────────────────────────────────────
// API helpers
// ─────────────────────────────────────────────────────────────────────────────

async function api(method, path, body, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const r = await fetch(BASE + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
    ...opts,
  });
  if (!r.ok) {
    const text = await r.text();
    let msg = text;
    try {
      const detail = JSON.parse(text).detail;
      if (typeof detail === 'string') msg = detail;
      else if (Array.isArray(detail)) msg = detail.map(d => d.msg || JSON.stringify(d)).join(', ');
      else if (detail) msg = JSON.stringify(detail);
    } catch { /* keep raw text */ }
    throw new Error(msg || r.statusText);
  }
  const ct = r.headers.get('Content-Type') || '';
  if (ct.includes('application/json')) return r.json();
  return r;
}

// ─────────────────────────────────────────────────────────────────────────────
// Init
// ─────────────────────────────────────────────────────────────────────────────
(async function init() {
  if (token) {
    try {
      await loadBrowser();
      return;
    } catch (e) {
      token = ''; sessionStorage.removeItem('ahc_token');
    }
  }
  showLogin();
  loadUsers();
})();

// ─────────────────────────────────────────────────────────────────────────────
// Views
// ─────────────────────────────────────────────────────────────────────────────
function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}
function showLogin() { showView('view-login'); }
function showBrowser() { showView('view-browser'); }

// ─────────────────────────────────────────────────────────────────────────────
// Login
// ─────────────────────────────────────────────────────────────────────────────
const AVATAR_COLORS = [
  ['#E8A84C','#E86C4C'], ['#4C9BE8','#6C4CE8'], ['#4CE88A','#4CE8D8'],
  ['#E84CA8','#9B50E8'], ['#9B59B6','#6C3483'], ['#1ABC9C','#0E8C7A'],
];

async function loadUsers() {
  const grid = document.getElementById('user-grid');
  try {
    const data = await api('GET', '/auth/users/names');
    const users = data.users || [];
    if (users.length === 0) {
      grid.innerHTML = '<div style="color:var(--text3);font-size:13px">No profiles found on this device.</div>';
      return;
    }
    grid.innerHTML = '';
    users.forEach((u, i) => {
      const colors = AVATAR_COLORS[i % AVATAR_COLORS.length];
      const tile = document.createElement('div');
      tile.className = 'user-tile';
      tile.dataset.name = u.name;
      tile.dataset.hasPin = u.has_pin;
      tile.innerHTML = `
        <div class="avatar" style="background:linear-gradient(135deg,${colors[0]},${colors[1]})">
          ${u.icon_emoji || `<span class="avatar-letter">${u.name[0].toUpperCase()}</span>`}
        </div>
        <div class="user-name">${esc(u.name)}</div>
      `;
      tile.onclick = () => selectUser(u, tile, colors[0]);
      grid.appendChild(tile);
    });
  } catch (e) {
    grid.innerHTML = `<div class="error-msg">Could not load users: ${esc(e.message)}</div>`;
  }
}

function selectUser(user, tile, color) {
  document.querySelectorAll('.user-tile').forEach(t => t.classList.remove('selected'));
  tile.classList.add('selected');
  selectedUser = user;

  if (!user.has_pin) {
    loginWithPin(user, '');
    return;
  }

  const sec = document.getElementById('pin-section');
  sec.querySelector('strong').textContent = user.name;
  sec.classList.add('visible');
  document.getElementById('pin-input').value = '';
  document.getElementById('login-error').textContent = '';
  document.getElementById('pin-input').focus();

  // Auto-submit after 600ms debounce
  let debounce;
  const input = document.getElementById('pin-input');
  const handler = () => {
    clearTimeout(debounce);
    if (input.value.length >= 4) {
      debounce = setTimeout(() => loginWithPin(user, input.value), 600);
    }
  };
  input.oninput = handler;
  input.onkeydown = (e) => { if (e.key === 'Enter') { clearTimeout(debounce); loginWithPin(user, input.value); } };
}

function clearSelection() {
  selectedUser = null;
  document.querySelectorAll('.user-tile').forEach(t => t.classList.remove('selected'));
  document.getElementById('pin-section').classList.remove('visible');
  document.getElementById('login-error').textContent = '';
}

async function loginWithPin(user, pin) {
  const errEl = document.getElementById('login-error');
  const tile = document.querySelector(`.user-tile[data-name="${CSS.escape(user.name)}"]`);
  const avatarEl = tile ? tile.querySelector('.avatar') : null;
  const originalContent = avatarEl ? avatarEl.innerHTML : '';

  errEl.textContent = '';
  if (avatarEl) avatarEl.innerHTML = '<div class="login-spinner"></div>';

  try {
    const data = await api('POST', '/auth/login', { name: user.name, pin: pin || '' });
    token = data.accessToken;
    currentUser = data.user?.name || user.name;
    sessionStorage.setItem('ahc_token', token);
    sessionStorage.setItem('ahc_user', currentUser);
    await loadBrowser();
  } catch (e) {
    if (avatarEl) avatarEl.innerHTML = originalContent;
    const msg = e.message || '';
    errEl.textContent = (msg.includes('401') || msg.toLowerCase().includes('invalid') || msg.toLowerCase().includes('incorrect') || msg.toLowerCase().includes('pin'))
      ? 'Incorrect PIN' : (msg || 'Login failed');
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Browser
// ─────────────────────────────────────────────────────────────────────────────
async function loadBrowser() {
  showBrowser();
  setViewMode(viewMode);

  // Set user display
  document.getElementById('header-username').textContent = currentUser;
  const av = document.getElementById('header-avatar');
  av.textContent = currentUser[0]?.toUpperCase() || '?';
  av.style.background = 'linear-gradient(135deg,#7c6af7,#4c9be8)';

  await navigate('/');
}

async function navigate(path) {
  currentPath = path;
  currentPage = 0;
  await loadDirectory();
}

async function loadDirectory() {
  const content = document.getElementById('file-content');
  content.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
  document.getElementById('pagination').style.display = 'none';

  renderBreadcrumbs();

  try {
    const qs = new URLSearchParams({
      path: currentPath,
      sort_by: sortBy,
      sort_dir: sortAsc ? 'asc' : 'desc',
      page: currentPage,
      page_size: PAGE_SIZE,
    });
    const data = await api('GET', `/files/list?${qs}`);
    currentItems = data.items || [];
    totalItems = data.totalCount || currentItems.length;

    document.getElementById('item-count').textContent =
      totalItems + ' item' + (totalItems !== 1 ? 's' : '');

    if (currentItems.length === 0) {
      content.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">📂</div>
          <div class="empty-label">Empty folder</div>
        </div>`;
      return;
    }

    renderFiles();
    renderPagination(data.totalCount, data.page, data.pageSize);
  } catch (e) {
    content.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-label">${esc(e.message)}</div></div>`;
  }
}

function renderBreadcrumbs() {
  const el = document.getElementById('breadcrumbs');
  const parts = currentPath.replace(/\/+$/, '').split('/').filter(Boolean);
  let html = `<span class="crumb${parts.length === 0 ? ' active' : ''}" onclick="navigate('/')">Home</span>`;
  let built = '/';
  for (let i = 0; i < parts.length; i++) {
    built += parts[i] + '/';
    const p = built;
    const isLast = i === parts.length - 1;
    html += `<span class="crumb-sep">›</span>`;
    html += `<span class="crumb${isLast ? ' active' : ''}" onclick="navigate('${p.replace(/'/g, "\\'")}')">${esc(parts[i])}</span>`;
  }
  el.innerHTML = html;
}

function renderFiles() {
  const content = document.getElementById('file-content');
  if (viewMode === 'grid') {
    content.innerHTML = '<div class="file-grid" id="file-grid"></div>';
    const grid = document.getElementById('file-grid');
    currentItems.forEach(item => grid.appendChild(makeCard(item)));
  } else {
    content.innerHTML = '<div class="file-list" id="file-list"></div>';
    const list = document.getElementById('file-list');
    currentItems.forEach(item => list.appendChild(makeRow(item)));
  }
}

function makeCard(item) {
  const card = document.createElement('div');
  card.className = 'file-card';
  const isImg = isImage(item.mimeType);
  card.innerHTML = `
    ${isImg ? `<img class="file-thumb" loading="lazy" onerror="this.outerHTML='<div class=\\"file-icon\\">${fileIcon(item)}</div>'">` : `<div class="file-icon">${fileIcon(item)}</div>`}
    <div class="file-name">${esc(item.name)}</div>
    <div class="file-meta">${item.isDirectory ? '' : fmtSize(item.sizeBytes)}</div>
    ${!item.isDirectory ? '<div class="download-badge">⬇</div>' : ''}
  `;
  if (isImg) {
    const img = card.querySelector('img.file-thumb');
    rawUrl(item.path).then(u => { if (img) img.src = u; }).catch(() => {
      if (img) img.outerHTML = '<div class="file-icon">' + fileIcon(item) + '</div>';
    });
  }
  card.onclick = () => onItemClick(item);
  return card;
}

function makeRow(item) {
  const row = document.createElement('div');
  row.className = 'file-row';
  const isImg = isImage(item.mimeType);
  row.innerHTML = `
    ${isImg ? `<img class="row-thumb" loading="lazy" onerror="this.outerHTML='<div class=\\"row-icon\\">${fileIcon(item)}</div>'">` : `<div class="row-icon">${fileIcon(item)}</div>`}
    <div class="row-name">${esc(item.name)}</div>
    <div class="row-size">${item.isDirectory ? '—' : fmtSize(item.sizeBytes)}</div>
    <div class="row-date">${fmtDate(item.modified)}</div>
    <div class="row-action">
      ${!item.isDirectory ? `<button class="dl-btn" title="Download" onclick="event.stopPropagation();downloadFile('${item.path.replace(/'/g, "\\'")}','${esc(item.name).replace(/'/g, "\\'")}')">⬇</button>` : ''}
    </div>
  `;
  if (isImg) {
    const img = row.querySelector('img.row-thumb');
    rawUrl(item.path).then(u => { if (img) img.src = u; }).catch(() => {
      if (img) img.outerHTML = '<div class="row-icon">' + fileIcon(item) + '</div>';
    });
  }
  row.onclick = () => onItemClick(item);
  return row;
}

// ─────────────────────────────────────────────────────────────────────────────
// Item click → navigate or preview
// ─────────────────────────────────────────────────────────────────────────────
function onItemClick(item) {
  if (item.isDirectory) {
    navigate(item.path);
    return;
  }
  const m = item.mimeType || '';
  if (isImage(m) || isVideo(m) || isAudio(m) || m === 'application/pdf' || m.startsWith('text/')) {
    openPreview(item);
  } else {
    downloadFile(item.path, item.name);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Preview modal
// ─────────────────────────────────────────────────────────────────────────────
async function openPreview(item) {
  currentPreviewPath = item.path;
  document.getElementById('modal-filename').textContent = item.name;
  const body = document.getElementById('modal-body');
  body.innerHTML = '<div class="spinner"></div>';
  document.getElementById('modal-overlay').classList.add('visible');

  const m = item.mimeType || '';
  let url = '';
  if (isImage(m) || isVideo(m) || isAudio(m) || m === 'application/pdf') {
    try {
      url = await rawUrl(item.path);
    } catch (e) {
      body.innerHTML = `<div class="empty-state"><div>Could not load file</div></div>`;
      return;
    }
  }

  if (isImage(m)) {
    body.innerHTML = `<img class="preview-img" src="${url}" alt="${esc(item.name)}">`;
  } else if (isVideo(m)) {
    body.innerHTML = `<video class="preview-video" src="${url}" controls autoplay></video>`;
  } else if (isAudio(m)) {
    body.innerHTML = `<audio class="preview-audio" src="${url}" controls autoplay></audio>`;
  } else if (m === 'application/pdf') {
    body.innerHTML = `<iframe class="preview-pdf" src="${url}"></iframe>`;
  } else if (m.startsWith('text/')) {
    // Fetch and display text
    fetch('/api/v1/files/download?path=' + encodeURIComponent(item.path), {
      headers: { 'Authorization': 'Bearer ' + token }
    }).then(r => r.text()).then(text => {
      body.innerHTML = `<pre class="preview-text">${esc(text)}</pre>`;
    }).catch(() => {
      body.innerHTML = `<div class="empty-state"><div>Could not load file</div></div>`;
    });
  }
}

function closeModal(e) {
  if (e && e.target !== document.getElementById('modal-overlay') && e.type === 'click') return;
  document.getElementById('modal-overlay').classList.remove('visible');
  document.getElementById('modal-body').innerHTML = '';
  currentPreviewPath = null;
}

function downloadCurrent() {
  if (currentPreviewPath) {
    const name = currentPreviewPath.split('/').pop();
    downloadFile(currentPreviewPath, name);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Download
// ─────────────────────────────────────────────────────────────────────────────
async function downloadFile(path, name) {
  try {
    const r = await fetch('/api/v1/files/download?path=' + encodeURIComponent(path), {
      headers: { 'Authorization': 'Bearer ' + token }
    });
    if (!r.ok) throw new Error(r.statusText);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name || 'file';
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 5000);
  } catch (e) {
    alert('Download failed: ' + e.message);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Upload
// ─────────────────────────────────────────────────────────────────────────────
function triggerUpload() {
  document.getElementById('upload-input').click();
}

function handleFileSelect(e) {
  uploadFiles(Array.from(e.target.files));
  e.target.value = '';
}

// Drag & drop
function onDragEnter(e) {
  e.preventDefault();
  dragCounter++;
  document.getElementById('drop-overlay').classList.add('visible');
}
function onDragOver(e) { e.preventDefault(); }
function onDragLeave(e) {
  dragCounter--;
  if (dragCounter <= 0) {
    dragCounter = 0;
    document.getElementById('drop-overlay').classList.remove('visible');
  }
}
function onDrop(e) {
  e.preventDefault();
  dragCounter = 0;
  document.getElementById('drop-overlay').classList.remove('visible');
  const files = Array.from(e.dataTransfer.files);
  if (files.length) uploadFiles(files);
}

async function uploadFiles(files) {
  if (!files.length) return;

  const toast = document.getElementById('upload-progress');
  const fileList = document.getElementById('upload-files');
  toast.classList.add('visible');

  const items = files.map((file, i) => {
    const id = 'uf' + Date.now() + i;
    const el = document.createElement('div');
    el.className = 'upload-file';
    el.innerHTML = `
      <div class="upload-file-name">${esc(file.name)}</div>
      <div class="progress-bar"><div class="progress-fill" id="${id}" style="width:0%"></div></div>
    `;
    fileList.appendChild(el);
    return { file, id };
  });

  for (const { file, id } of items) {
    const bar = document.getElementById(id);
    try {
      const form = new FormData();
      form.append('file', file);
      const url = '/api/v1/files/upload?path=' + encodeURIComponent(currentPath);
      await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', url);
        xhr.setRequestHeader('Authorization', 'Bearer ' + token);
        xhr.upload.onprogress = e => {
          if (e.lengthComputable) bar.style.width = (e.loaded / e.total * 100) + '%';
        };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            bar.style.width = '100%';
            bar.classList.add('done');
            resolve();
          } else {
            bar.classList.add('error');
            let msg = xhr.statusText;
            try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
            reject(new Error(msg));
          }
        };
        xhr.onerror = () => { bar.classList.add('error'); reject(new Error('Network error')); };
        xhr.send(form);
      });
    } catch (e) {
      if (bar) bar.classList.add('error');
    }
  }

  // Refresh directory after all uploads
  await loadDirectory();
}

function closeUploadToast() {
  document.getElementById('upload-progress').classList.remove('visible');
  document.getElementById('upload-files').innerHTML = '';
}

// ─────────────────────────────────────────────────────────────────────────────
// Sort & view
// ─────────────────────────────────────────────────────────────────────────────
function changeSort() {
  sortBy = document.getElementById('sort-select').value;
  currentPage = 0;
  loadDirectory();
}

function toggleSortDir() {
  sortAsc = !sortAsc;
  document.getElementById('sort-dir-btn').textContent = sortAsc ? '↑ ASC' : '↓ DESC';
  currentPage = 0;
  loadDirectory();
}

function setView(mode) {
  viewMode = mode;
  localStorage.setItem('ahc_view', mode);
  setViewMode(mode);
  if (currentItems.length) renderFiles();
}

function setViewMode(mode) {
  document.getElementById('btn-grid').classList.toggle('active', mode === 'grid');
  document.getElementById('btn-list').classList.toggle('active', mode === 'list');
}

// ─────────────────────────────────────────────────────────────────────────────
// Pagination
// ─────────────────────────────────────────────────────────────────────────────
function renderPagination(total, page, pageSize) {
  const pag = document.getElementById('pagination');
  if (!total || total <= pageSize) { pag.style.display = 'none'; return; }
  pag.style.display = 'flex';
  const totalPages = Math.ceil(total / pageSize);
  document.getElementById('page-info').textContent = `Page ${page + 1} of ${totalPages} (${total} items)`;
  document.getElementById('page-prev').disabled = page === 0;
  document.getElementById('page-next').disabled = page >= totalPages - 1;
}

function prevPage() { if (currentPage > 0) { currentPage--; loadDirectory(); } }
function nextPage() { currentPage++; loadDirectory(); }

// ─────────────────────────────────────────────────────────────────────────────
// Auth
// ─────────────────────────────────────────────────────────────────────────────
function logout() {
  token = ''; currentUser = '';
  sessionStorage.removeItem('ahc_token');
  sessionStorage.removeItem('ahc_user');
  selectedUser = null;
  document.getElementById('pin-section').classList.remove('visible');
  document.getElementById('login-error').textContent = '';
  loadUsers();
  showLogin();
}

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────
async function rawUrl(path) {
  // Mint a short-lived, path-scoped media token (Bearer-authenticated) and use
  // that in the URL instead of the long-lived JWT, which must never appear in a
  // URL (access logs, history, Referer, src attributes).
  const r = await fetch('/api/v1/files/media-token?path=' + encodeURIComponent(path), {
    headers: { 'Authorization': 'Bearer ' + token }
  });
  if (!r.ok) throw new Error('media-token failed');
  const data = await r.json();
  return '/browse/raw?path=' + encodeURIComponent(path) + '&token=' + encodeURIComponent(data.token);
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtSize(bytes) {
  if (!bytes) return '0 B';
  const units = ['B','KB','MB','GB','TB'];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + units[i];
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
}

function isImage(m) { return m && m.startsWith('image/'); }
function isVideo(m) { return m && m.startsWith('video/'); }
function isAudio(m) { return m && m.startsWith('audio/'); }

function fileIcon(item) {
  if (item.isDirectory) return '📁';
  const m = item.mimeType || '';
  if (isImage(m)) return '🖼️';
  if (isVideo(m)) return '🎬';
  if (isAudio(m)) return '🎵';
  if (m === 'application/pdf') return '📄';
  if (m.startsWith('text/')) return '📝';
  if (m.includes('zip') || m.includes('tar') || m.includes('compressed')) return '🗜️';
  if (m.includes('word') || m.includes('document')) return '📃';
  if (m.includes('sheet') || m.includes('excel')) return '📊';
  if (m.includes('presentation') || m.includes('powerpoint')) return '📑';
  return '📎';
}

// Keyboard: Escape closes modal
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const overlay = document.getElementById('modal-overlay');
    if (overlay.classList.contains('visible')) {
      closeModal();
    }
  }
});
</script>
</body>
</html>
"""
