"""
AiHomeCloud Web Upload Portal — serves a self-contained HTML page at GET /web.

This is a LAN-accessible drag-and-drop file upload interface for desktop/laptop
users who cannot run the Flutter mobile app. It reuses existing API endpoints:
  - GET  /auth/users/names        → user picker
  - POST /auth/login              → PIN authentication
  - POST /api/v1/files/upload     → file upload with progress
  - GET  /web/check-duplicate     → name+size duplicate scan (this file)

No new upload logic — this route only serves HTML/CSS/JS.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from ..auth import get_current_user
from ..config import settings
from ..ingest import _resolve_identity

logger = logging.getLogger("aihomecloud.web")
router = APIRouter()

# Directories skipped during the duplicate scan (same set as duplicate_scanner.py)
_SKIP_DIRS = frozenset({".ahc_trash", ".inbox", ".git", "__pycache__", "lost+found"})


# ── Duplicate-check endpoint ──────────────────────────────────────────────────

@router.get("/web/check-duplicate")
async def web_check_duplicate(
    name: str = Query(..., min_length=1, max_length=512),
    size: int = Query(..., ge=0),
    user: dict = Depends(get_current_user),
) -> dict:
    """Scan NAS for a file whose name (case-insensitive) and size match.

    Used by the web upload portal before each upload to detect duplicates.
    Runs in a thread executor so it doesn't block the event loop.
    Returns: { "found": bool, "path": str | null }
    """
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Invalid filename")

    nas_root = settings.nas_root.resolve()
    name_lower = name.lower()
    caller_name, is_admin = await _resolve_identity(user)

    def _scan() -> str | None:
        stack: list[Path] = [nas_root]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except (PermissionError, OSError):
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir():
                        if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                            continue
                        # Other members' personal folders are private (same rule
                        # _authorize_path enforces everywhere else) — a duplicate check must
                        # not become a way to probe file names/sizes/paths across accounts.
                        if (
                            current == nas_root / "personal"
                            and not is_admin
                            and entry.name != caller_name
                        ):
                            continue
                        stack.append(entry)
                    elif entry.is_file():
                        if entry.name.lower() == name_lower and entry.stat().st_size == size:
                            return str(entry)
                except OSError:
                    continue
        return None

    loop = asyncio.get_running_loop()
    match = await loop.run_in_executor(None, _scan)
    return {"found": match is not None, "path": match}


# ── Self-contained HTML page ──────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AiHomeCloud</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🏠</text></svg>">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #121212;
      color: #ffffff;
      min-height: 100vh;
    }

    /* ── Views ─────────────────────────────────────────────────────────── */
    .view { display: none; }
    .view.active { display: flex; }

    /* ── User Picker ────────────────────────────────────────────────────── */
    #view-picker {
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 2rem;
      gap: 2rem;
    }

    .logo {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.5rem;
    }
    .logo-icon { font-size: 3rem; line-height: 1; }
    .logo-title {
      font-size: 1.75rem;
      font-weight: 700;
      color: #6C63FF;
      letter-spacing: -0.5px;
    }
    .logo-subtitle {
      font-size: 0.9rem;
      color: #888;
    }

    .user-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 1rem;
      width: 100%;
      max-width: 680px;
      justify-content: center;
    }

    .user-card {
      background: #1e1e1e;
      border: 1.5px solid #2a2a2a;
      border-radius: 16px;
      padding: 1.5rem 1rem;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.6rem;
      cursor: pointer;
      transition: border-color 0.15s, transform 0.1s, background 0.15s;
      user-select: none;
    }
    .user-card:hover {
      border-color: #6C63FF;
      background: #252535;
      transform: translateY(-2px);
    }
    .user-card:active { transform: translateY(0); }
    .user-card .avatar { font-size: 3rem; line-height: 1; }
    .user-card .uname {
      font-size: 0.95rem;
      font-weight: 600;
      text-align: center;
      color: #eee;
      word-break: break-word;
    }

    .picker-error {
      color: #ff6b6b;
      font-size: 0.875rem;
      text-align: center;
      min-height: 1.2em;
    }

    /* ── PIN Modal ──────────────────────────────────────────────────────── */
    #pin-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.7);
      backdrop-filter: blur(4px);
      z-index: 100;
      align-items: center;
      justify-content: center;
    }
    #pin-overlay.open { display: flex; }

    .pin-modal {
      background: #1e1e1e;
      border: 1.5px solid #2a2a2a;
      border-radius: 20px;
      padding: 2rem 2.5rem;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1.25rem;
      width: 100%;
      max-width: 340px;
    }
    .pin-modal .avatar-lg { font-size: 4rem; line-height: 1; }
    .pin-modal h2 { font-size: 1.1rem; font-weight: 600; color: #eee; }

    .pin-dots {
      display: flex;
      gap: 1rem;
    }
    .pin-dot {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      border: 2px solid #555;
      background: transparent;
      transition: background 0.1s, border-color 0.1s;
    }
    .pin-dot.filled { background: #6C63FF; border-color: #6C63FF; }

    .pin-keypad {
      display: grid;
      grid-template-columns: repeat(3, 64px);
      gap: 0.6rem;
    }
    .pin-key {
      height: 52px;
      border-radius: 12px;
      border: 1.5px solid #2a2a2a;
      background: #252525;
      color: #fff;
      font-size: 1.2rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.1s, border-color 0.1s;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .pin-key:hover { background: #333; border-color: #6C63FF; }
    .pin-key:active { background: #6C63FF; }
    .pin-key.del { font-size: 1rem; color: #aaa; }
    .pin-key.spacer { visibility: hidden; cursor: default; }

    .pin-error {
      color: #ff6b6b;
      font-size: 0.85rem;
      text-align: center;
      min-height: 1.2em;
    }
    .pin-cancel {
      background: none;
      border: none;
      color: #888;
      font-size: 0.875rem;
      cursor: pointer;
      text-decoration: underline;
    }
    .pin-cancel:hover { color: #ccc; }

    /* ── Duplicate Warning Modal ─────────────────────────────────────── */
    #dup-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.75);
      backdrop-filter: blur(4px);
      z-index: 200;
      align-items: center;
      justify-content: center;
      padding: 1rem;
    }
    #dup-overlay.open { display: flex; }

    .dup-modal {
      background: #1e1e1e;
      border: 1.5px solid #f59e0b;
      border-radius: 20px;
      padding: 2rem 2.5rem;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1rem;
      width: 100%;
      max-width: 480px;
    }
    .dup-modal .dup-icon { font-size: 2.5rem; line-height: 1; }
    .dup-modal h2 { font-size: 1.1rem; font-weight: 700; color: #f59e0b; }
    .dup-info { font-size: 0.875rem; color: #aaa; text-align: center; }
    .dup-filename {
      font-size: 0.9rem;
      font-weight: 600;
      color: #eee;
      text-align: center;
      word-break: break-all;
    }
    .dup-location-label { font-size: 0.75rem; color: #666; }
    .dup-path {
      font-size: 0.78rem;
      color: #aaa;
      background: #252525;
      border-radius: 8px;
      padding: 0.5rem 0.75rem;
      word-break: break-all;
      width: 100%;
      text-align: center;
    }
    .dup-actions {
      display: flex;
      flex-direction: column;
      gap: 0.6rem;
      width: 100%;
    }
    .btn-dup-save {
      background: #6C63FF;
      border: none;
      border-radius: 10px;
      color: #fff;
      font-size: 0.9rem;
      font-weight: 600;
      padding: 0.7rem 1rem;
      cursor: pointer;
      transition: background 0.15s;
      text-align: center;
    }
    .btn-dup-save:hover { background: #5a52e0; }
    .btn-dup-cancel {
      background: none;
      border: 1.5px solid #333;
      border-radius: 10px;
      color: #888;
      font-size: 0.875rem;
      padding: 0.6rem 1rem;
      cursor: pointer;
      transition: border-color 0.15s, color 0.15s;
      text-align: center;
    }
    .btn-dup-cancel:hover { border-color: #ff6b6b; color: #ff6b6b; }

    /* ── Upload Dashboard ───────────────────────────────────────────────── */
    #view-dashboard {
      flex-direction: column;
      min-height: 100vh;
    }

    .dash-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 1.5rem;
      height: 64px;
      background: #1a1a1a;
      border-bottom: 1px solid #2a2a2a;
      flex-shrink: 0;
    }
    .dash-header .greeting {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      font-weight: 600;
      font-size: 1rem;
      color: #eee;
    }
    .dash-header .greeting .logo-small { color: #6C63FF; font-size: 1.1rem; }
    .btn-logout {
      background: none;
      border: 1.5px solid #333;
      border-radius: 8px;
      color: #aaa;
      font-size: 0.875rem;
      padding: 0.4rem 0.9rem;
      cursor: pointer;
      transition: border-color 0.15s, color 0.15s;
    }
    .btn-logout:hover { border-color: #6C63FF; color: #fff; }

    .zones-grid {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 1rem;
      padding: 1rem;
      flex: 1;
      min-height: calc(100vh - 64px);
    }
    @media (max-width: 900px) {
      .zones-grid { grid-template-columns: 1fr; }
    }

    .zone {
      background: #1e1e1e;
      border-radius: 16px;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border: 1.5px solid #2a2a2a;
      transition: border-color 0.15s;
    }
    .zone-header {
      padding: 1rem 1.25rem 0.6rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-shrink: 0;
    }
    .zone-icon { font-size: 1.5rem; line-height: 1; }
    .zone-title { font-size: 1rem; font-weight: 700; color: #eee; }
    .zone-subtitle { font-size: 0.78rem; color: #666; margin-left: auto; }

    .zone-drop {
      margin: 0 1rem;
      border: 2px dashed #333;
      border-radius: 12px;
      min-height: 220px;
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      cursor: pointer;
      transition: border-color 0.15s, background 0.15s;
      padding: 1.5rem;
      pointer-events: none; /* zone handles drag; drop area handles click only */
    }
    .drop-icon { font-size: 2.5rem; opacity: 0.4; }
    .drop-hint { font-size: 0.875rem; color: #666; text-align: center; }
    .drop-hint span { color: #6C63FF; text-decoration: underline; }

    /* Drag-over state on the whole zone */
    .zone.dragover {
      border-color: #6C63FF;
    }
    .zone.dragover .zone-drop {
      border-color: #6C63FF;
      background: rgba(108,99,255,0.1);
    }
    /* Hover state: only the drop area reacts to mouse (not drag) */
    .zone:not(.dragover) .zone-drop:hover {
      border-color: #6C63FF;
      background: rgba(108,99,255,0.04);
    }

    .zone-queue {
      padding: 0.5rem 1rem 0;
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
      flex-shrink: 0;
    }

    /* ── Upload queue items ─────────────────────────────────────────────── */
    .upload-item {
      background: #252525;
      border-radius: 8px;
      padding: 0.45rem 0.65rem;
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
    }
    .upload-row {
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .upload-name {
      color: #ccc;
      font-size: 0.8rem;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 1;
      min-width: 0;
    }
    .upload-status {
      font-size: 0.75rem;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .upload-status.pending   { color: #888; }
    .upload-status.checking  { color: #f59e0b; }
    .upload-status.uploading { color: #6C63FF; }
    .upload-status.done      { color: #4caf50; }
    .upload-status.failed    { color: #ff6b6b; }
    .upload-status.skipped    { color: #888; }
    .upload-status.cancelled  { color: #888; }
    .upload-detail {
      font-size: 0.69rem;
      color: #666;
      padding-top: 0.15rem;
      display: none;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .upload-detail.visible { display: block; }
    .btn-clear-done {
      background: none;
      border: none;
      color: #555;
      font-size: 0.72rem;
      cursor: pointer;
      padding: 0.15rem 0.5rem;
      border-radius: 4px;
      transition: color 0.15s, background 0.15s;
      flex-shrink: 0;
    }
    .btn-clear-done:hover { color: #ccc; background: rgba(255,255,255,0.07); }
    .btn-cancel-all {
      background: none;
      border: none;
      color: #888;
      font-size: 0.72rem;
      cursor: pointer;
      padding: 0.15rem 0.5rem;
      border-radius: 4px;
      transition: color 0.15s, background 0.15s;
      flex-shrink: 0;
    }
    .btn-cancel-all:hover { color: #ff6b6b; background: rgba(255,107,107,0.08); }
    .btn-cancel-item {
      background: none;
      border: none;
      color: #888;
      font-size: 0.72rem;
      line-height: 1;
      cursor: pointer;
      padding: 0.1rem 0.25rem;
      border-radius: 3px;
      flex-shrink: 0;
      transition: color 0.1s;
    }
    .btn-cancel-item:hover { color: #ff6b6b; }
    .upload-bar-wrap {
      height: 3px;
      background: #2a2a2a;
      border-radius: 2px;
      overflow: hidden;
    }
    .upload-bar {
      height: 100%;
      background: #6C63FF;
      border-radius: 2px;
      width: 0%;
      transition: width 0.15s linear;
    }
    .upload-bar.done      { background: #4caf50; }
    .upload-bar.failed    { background: #ff6b6b; }
    .upload-bar.cancelled { background: #444; }
    .btn-retry {
      background: none;
      border: 1px solid #ff6b6b;
      border-radius: 4px;
      color: #ff6b6b;
      font-size: 0.7rem;
      padding: 0.15rem 0.4rem;
      cursor: pointer;
      flex-shrink: 0;
    }
    .btn-retry:hover { background: rgba(255,107,107,0.1); }
    .zone-stats {
      padding: 0.3rem 1rem 0.8rem;
      font-size: 0.75rem;
      color: #666;
      flex-shrink: 0;
      min-height: 1.6em;
    }
  </style>
</head>
<body>

<!-- ── User Picker View ─────────────────────────────────────────────── -->
<div id="view-picker" class="view active">
  <div class="logo">
    <div class="logo-icon">🏠</div>
    <div class="logo-title">AiHomeCloud</div>
    <div class="logo-subtitle">Choose your profile to continue</div>
  </div>

  <div id="user-grid" class="user-grid">
    <!-- populated by JS -->
  </div>

  <div id="picker-error" class="picker-error"></div>
</div>

<!-- ── PIN Modal ────────────────────────────────────────────────────── -->
<div id="pin-overlay">
  <div class="pin-modal">
    <div id="pin-avatar" class="avatar-lg">👤</div>
    <h2 id="pin-username">Enter PIN</h2>
    <div class="pin-dots">
      <div class="pin-dot" id="d0"></div>
      <div class="pin-dot" id="d1"></div>
      <div class="pin-dot" id="d2"></div>
      <div class="pin-dot" id="d3"></div>
    </div>
    <div class="pin-keypad" id="pin-keypad">
      <!-- keys injected by JS -->
    </div>
    <div id="pin-error" class="pin-error"></div>
    <button class="pin-cancel" id="pin-cancel">Cancel</button>
  </div>
</div>

<!-- ── Duplicate Warning Modal ──────────────────────────────────────── -->
<div id="dup-overlay">
  <div class="dup-modal">
    <div class="dup-icon">⚠️</div>
    <h2>Possible Duplicate</h2>
    <p class="dup-info">This file might already exist in AiHomeCloud:</p>
    <div class="dup-filename" id="dup-filename"></div>
    <div class="dup-location-label">Found at:</div>
    <div class="dup-path" id="dup-path"></div>
    <div class="dup-actions">
      <button class="btn-dup-save" id="btn-dup-save">Save as <span id="dup-newname"></span></button>
      <button class="btn-dup-cancel" id="btn-dup-cancel">Cancel upload</button>
    </div>
  </div>
</div>

<!-- ── Upload Dashboard View ──────────────────────────────────────── -->
<div id="view-dashboard" class="view">

  <header class="dash-header">
    <div class="greeting">
      <span class="logo-small">🏠</span>
      <span>AiHomeCloud &mdash; <span id="dash-username">…</span></span>
    </div>
    <button class="btn-logout" id="btn-logout">Logout</button>
  </header>

  <div class="zones-grid">

    <!-- Personal zone -->
    <div class="zone" id="zone-personal" data-zone="personal">
      <div class="zone-header">
        <span class="zone-icon">🗂️</span>
        <span class="zone-title">Personal</span>
        <span class="zone-subtitle">Your private files</span>
        <button class="btn-cancel-all" id="cancel-all-personal" style="display:none">Cancel all</button>
        <button class="btn-clear-done" id="clear-personal" style="display:none">Clear</button>
      </div>
      <div class="zone-drop" id="drop-personal">
        <div class="drop-icon">⬆️</div>
        <div class="drop-hint">Drag files here<br>or <span>click to browse</span></div>
      </div>
      <input type="file" id="file-personal" multiple style="display:none">
      <div class="zone-queue" id="queue-personal"></div>
      <div class="zone-stats" id="stats-personal"></div>
    </div>

    <!-- Family zone -->
    <div class="zone" id="zone-family" data-zone="family">
      <div class="zone-header">
        <span class="zone-icon">👨‍👩‍👧‍👦</span>
        <span class="zone-title">Family</span>
        <span class="zone-subtitle">Shared with everyone</span>
        <button class="btn-cancel-all" id="cancel-all-family" style="display:none">Cancel all</button>
        <button class="btn-clear-done" id="clear-family" style="display:none">Clear</button>
      </div>
      <div class="zone-drop" id="drop-family">
        <div class="drop-icon">⬆️</div>
        <div class="drop-hint">Drag files here<br>or <span>click to browse</span></div>
      </div>
      <input type="file" id="file-family" multiple style="display:none">
      <div class="zone-queue" id="queue-family"></div>
      <div class="zone-stats" id="stats-family"></div>
    </div>

    <!-- Entertainment zone -->
    <div class="zone" id="zone-entertainment" data-zone="entertainment">
      <div class="zone-header">
        <span class="zone-icon">🎬</span>
        <span class="zone-title">Entertainment</span>
        <span class="zone-subtitle">Movies, music &amp; more</span>
        <button class="btn-cancel-all" id="cancel-all-entertainment" style="display:none">Cancel all</button>
        <button class="btn-clear-done" id="clear-entertainment" style="display:none">Clear</button>
      </div>
      <div class="zone-drop" id="drop-entertainment">
        <div class="drop-icon">⬆️</div>
        <div class="drop-hint">Drag files here<br>or <span>click to browse</span></div>
      </div>
      <input type="file" id="file-entertainment" multiple style="display:none">
      <div class="zone-queue" id="queue-entertainment"></div>
      <div class="zone-stats" id="stats-entertainment"></div>
    </div>

  </div>
</div>

<script>
'use strict';

// ── State ────────────────────────────────────────────────────────────────
const state = {
  users: [],
  selectedUser: null,   // { name, icon_emoji, has_pin }
  pin: '',
  token: null,
  userName: null,
};
// Per-zone upload queues, busy flags, active entry tracker and summary counters
const uploadQueues  = { personal: [], family: [], entertainment: [] };
const uploadBusy    = { personal: false, family: false, entertainment: false };
const activeEntry   = { personal: null,  family: null,  entertainment: null  };
const uploadStats   = {
  personal:      { done: 0, failed: 0, skipped: 0, cancelled: 0 },
  family:        { done: 0, failed: 0, skipped: 0, cancelled: 0 },
  entertainment: { done: 0, failed: 0, skipped: 0, cancelled: 0 },
};

// ── View switching ───────────────────────────────────────────────────────
function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

// ── User Picker ──────────────────────────────────────────────────────────
async function loadUsers() {
  const grid = document.getElementById('user-grid');
  const err  = document.getElementById('picker-error');
  grid.innerHTML = '';
  err.textContent = '';

  let users;
  try {
    const res = await fetch('/api/v1/auth/users/names');
    if (!res.ok) throw new Error('Server error ' + res.status);
    const data = await res.json();
    users = data.users || [];
  } catch (e) {
    err.textContent = 'Could not load users: ' + e.message;
    return;
  }

  state.users = users;

  if (users.length === 0) {
    err.textContent = 'No users configured. Set up profiles in the mobile app first.';
    return;
  }

  users.forEach(u => {
    const card = document.createElement('div');
    card.className = 'user-card';
    card.innerHTML =
      '<div class="avatar">' + escHtml(u.icon_emoji || '👤') + '</div>' +
      '<div class="uname">' + escHtml(u.name) + '</div>';
    card.addEventListener('click', () => onUserSelect(u));
    grid.appendChild(card);
  });
}

function onUserSelect(user) {
  state.selectedUser = user;
  if (user.has_pin) {
    openPinModal(user);
  } else {
    doLogin(user.name, '');
  }
}

// ── Login ────────────────────────────────────────────────────────────────
async function doLogin(name, pin) {
  const pickerErr = document.getElementById('picker-error');
  const pinErr    = document.getElementById('pin-error');
  pickerErr.textContent = '';
  pinErr.textContent = '';

  let data;
  try {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, pin }),
    });
    data = await res.json();
    if (!res.ok) {
      const msg = data.detail || 'Login failed';
      if (state.selectedUser && state.selectedUser.has_pin) {
        pinErr.textContent = msg;
        resetPin();
      } else {
        pickerErr.textContent = msg;
      }
      return;
    }
  } catch (e) {
    const msg = 'Network error: ' + e.message;
    if (state.selectedUser && state.selectedUser.has_pin) {
      pinErr.textContent = msg;
      resetPin();
    } else {
      pickerErr.textContent = msg;
    }
    return;
  }

  // Store session
  sessionStorage.setItem('ahc_token', data.accessToken);
  sessionStorage.setItem('ahc_user',  data.user.name);
  state.token    = data.accessToken;
  state.userName = data.user.name;

  closePinModal();
  initDashboard();
  showView('view-dashboard');
}

// ── Dashboard ────────────────────────────────────────────────────────────
const ZONE_PATHS = {
  // Bug fix: raw username — no encodeURIComponent here; path is a filesystem
  // path, not a URL. encodeURIComponent is applied to the whole path as a
  // query-param value in uploadFile().
  personal:      () => '/srv/nas/personal/' + state.userName + '/',
  family:        () => '/srv/nas/family/',
  entertainment: () => '/srv/nas/entertainment/',
};

function initDashboard() {
  resetUploadState();
  document.getElementById('dash-username').textContent = state.userName || '';
  // NOTE: setupZone() is called once at boot (not here) to avoid duplicate
  // event listeners accumulating across logout/login cycles.
}

// ── Zone setup (called ONCE at boot, not on every login) ─────────────────
function setupZone(zoneId) {
  const zoneEl  = document.getElementById('zone-' + zoneId);
  const dropEl  = document.getElementById('drop-' + zoneId);
  const fileEl  = document.getElementById('file-' + zoneId);

  // Click on the drop area → open file picker
  // pointer-events on dropEl is none (CSS), so the click bubbles from zoneEl
  zoneEl.addEventListener('click', e => {
    // Only trigger file picker if the click wasn't on the queue/stats
    if (e.target.closest('.zone-queue, .zone-stats, .btn-retry, .btn-clear-done, .btn-cancel-all')) return;
    fileEl.click();
  });

  // File picker selection
  fileEl.addEventListener('change', () => {
    if (fileEl.files && fileEl.files.length) {
      onFilesSelected(zoneId, Array.from(fileEl.files));
      fileEl.value = '';  // reset so same file can be re-selected
    }
  });

  // Drag events on the whole zone (not just the drop area) so dragging onto
  // the zone header or stats row also works and the browser doesn't navigate
  // to the file URL when dropped outside the drop area.
  zoneEl.addEventListener('dragenter', e => { e.preventDefault(); zoneEl.classList.add('dragover'); });
  zoneEl.addEventListener('dragover',  e => { e.preventDefault(); zoneEl.classList.add('dragover'); });
  zoneEl.addEventListener('dragleave', e => {
    // Only remove when the pointer truly leaves the zone element
    if (!zoneEl.contains(e.relatedTarget)) zoneEl.classList.remove('dragover');
  });
  zoneEl.addEventListener('drop', e => {
    e.preventDefault();
    zoneEl.classList.remove('dragover');
    const files = Array.from(e.dataTransfer.files);
    if (files.length) onFilesSelected(zoneId, files);
  });
}

// Fallback: clear all dragover states if drag leaves the document entirely
document.addEventListener('dragleave', e => {
  if (!e.relatedTarget) {
    document.querySelectorAll('.zone.dragover').forEach(z => z.classList.remove('dragover'));
  }
});

// ── Upload engine ─────────────────────────────────────────────────────────
function resetUploadState() {
  ['personal', 'family', 'entertainment'].forEach(z => {
    uploadQueues[z] = [];
    uploadBusy[z]   = false;
    activeEntry[z]  = null;
    uploadStats[z]  = { done: 0, failed: 0, skipped: 0, cancelled: 0 };
  });
}

function onFilesSelected(zoneId, files) {
  files.forEach(f => {
    const entry  = { file: f, itemEl: null, abortFn: null, cancelled: false };
    const itemEl = createQueueItem(f.name, () => cancelEntry(zoneId, entry));
    entry.itemEl = itemEl;
    document.getElementById('queue-' + zoneId).appendChild(itemEl);
    uploadQueues[zoneId].push(entry);
  });
  updateCancelAllButton(zoneId);
  processQueue(zoneId);
}

function createQueueItem(name, cancelCb) {
  const el = document.createElement('div');
  el.className = 'upload-item';
  const nameHtml = escHtml(truncateName(name, 36));
  const nameFull = escHtml(name);
  el.innerHTML =
    '<div class="upload-row">' +
      '<span class="upload-name" title="' + nameFull + '">' + nameHtml + '</span>' +
      '<span class="upload-status pending">Queued</span>' +
      '<button class="btn-cancel-item" title="Cancel">\u2715</button>' +
    '</div>' +
    '<div class="upload-bar-wrap"><div class="upload-bar"></div></div>' +
    '<div class="upload-detail"></div>';
  el.querySelector('.btn-cancel-item').addEventListener('click', e => {
    e.stopPropagation();
    cancelCb();
  });
  return el;
}

function truncateName(str, max) {
  if (str.length <= max) return str;
  const dot = str.lastIndexOf('.');
  if (dot > 0 && str.length - dot <= 8) {
    return str.slice(0, max - (str.length - dot) - 1) + '\u2026' + str.slice(dot);
  }
  return str.slice(0, max - 1) + '\u2026';
}

function processQueue(zoneId) {
  if (uploadBusy[zoneId] || uploadQueues[zoneId].length === 0) return;
  uploadBusy[zoneId]  = true;
  const entry = uploadQueues[zoneId].shift();
  activeEntry[zoneId] = entry;
  uploadFile(zoneId, entry).then(() => {
    activeEntry[zoneId] = null;
    uploadBusy[zoneId]  = false;
    updateZoneStats(zoneId);
    updateClearButton(zoneId);
    updateCancelAllButton(zoneId);
    processQueue(zoneId);
  });
}

// ── Format helpers ────────────────────────────────────────────────────────
function fmtBytes(b) {
  if (b >= 1073741824) return (b / 1073741824).toFixed(2) + ' GB';
  if (b >= 1048576)    return (b / 1048576).toFixed(1)    + ' MB';
  if (b >= 1024)       return (b / 1024).toFixed(0)       + ' KB';
  return b + ' B';
}
function fmtSpeed(bps) {
  if (bps >= 1073741824) return (bps / 1073741824).toFixed(1) + ' GB/s';
  if (bps >= 1048576)    return (bps / 1048576).toFixed(1)    + ' MB/s';
  if (bps >= 1024)       return (bps / 1024).toFixed(0)       + ' KB/s';
  return bps + ' B/s';
}

// ── Cancel helpers ────────────────────────────────────────────────────────
function cancelEntry(zoneId, entry) {
  if (entry.abortFn) {
    // Checking or uploading — abort whatever is in flight
    entry.abortFn();
  } else {
    // Still pending in queue — remove silently
    const idx = uploadQueues[zoneId].indexOf(entry);
    if (idx !== -1) uploadQueues[zoneId].splice(idx, 1);
    entry.itemEl.remove();
    updateCancelAllButton(zoneId);
    updateClearButton(zoneId);
  }
}

function cancelAll(zoneId) {
  // Remove all pending entries from queue and DOM
  uploadQueues[zoneId].forEach(e => e.itemEl.remove());
  uploadQueues[zoneId] = [];
  // Abort the active upload/check if any
  if (activeEntry[zoneId] && activeEntry[zoneId].abortFn) {
    activeEntry[zoneId].abortFn();
  }
  updateCancelAllButton(zoneId);
  updateClearButton(zoneId);
}

function updateCancelAllButton(zoneId) {
  const btn = document.getElementById('cancel-all-' + zoneId);
  if (!btn) return;
  const hasActive = uploadBusy[zoneId] || uploadQueues[zoneId].length > 0;
  btn.style.display = hasActive ? '' : 'none';
}

// ── Clear-done helpers ─────────────────────────────────────────────────────
const _CLEARABLE = '.upload-status.done, .upload-status.failed, .upload-status.skipped, .upload-status.cancelled';
function clearDone(zoneId) {
  const queue = document.getElementById('queue-' + zoneId);
  if (!queue) return;
  queue.querySelectorAll('.upload-item').forEach(item => {
    if (item.querySelector(_CLEARABLE)) item.remove();
  });
  updateClearButton(zoneId);
}
function updateClearButton(zoneId) {
  const btn   = document.getElementById('clear-' + zoneId);
  const queue = document.getElementById('queue-' + zoneId);
  if (!btn || !queue) return;
  btn.style.display = queue.querySelector(_CLEARABLE) ? '' : 'none';
}

// ── Duplicate detection ───────────────────────────────────────────────────

/** Generate a `name(1).ext` variant for a filename. */
function addDupSuffix(name) {
  const dot = name.lastIndexOf('.');
  if (dot > 0) return name.slice(0, dot) + '(1)' + name.slice(dot);
  return name + '(1)';
}

/**
 * Ask the server if a file with this name+size already exists on the NAS.
 * Returns { found: bool, path: string } or null on network error.
 */
async function checkDuplicate(file, signal) {
  try {
    const params = new URLSearchParams({ name: file.name, size: String(file.size) });
    const res = await fetch('/web/check-duplicate?' + params.toString(), {
      headers: { 'Authorization': 'Bearer ' + state.token },
      signal,
    });
    if (res.status === 401) return null;
    if (!res.ok) return null;
    return await res.json();
  } catch (_) {
    return null;  // includes AbortError — treated as non-fatal
  }
}

/**
 * Show the duplicate warning modal.
 * Returns a Promise that resolves to { proceed: bool, newName: string }.
 */
function showDupModal(originalName, existingPath, newName) {
  return new Promise(resolve => {
    document.getElementById('dup-filename').textContent = originalName;
    document.getElementById('dup-path').textContent    = existingPath;
    document.getElementById('dup-newname').textContent = newName;
    document.getElementById('dup-overlay').classList.add('open');

    function close(result) {
      document.getElementById('dup-overlay').classList.remove('open');
      resolve(result);
    }

    document.getElementById('btn-dup-save').addEventListener(
      'click', () => close({ proceed: true, newName }), { once: true }
    );
    document.getElementById('btn-dup-cancel').addEventListener(
      'click', () => close({ proceed: false, newName: '' }), { once: true }
    );
  });
}

// ── Upload (async, with pre-flight duplicate check) ───────────────────────

async function uploadFile(zoneId, entry) {
  const { file, itemEl } = entry;
  const statusEl  = itemEl.querySelector('.upload-status');
  const barEl     = itemEl.querySelector('.upload-bar');
  const detailEl  = itemEl.querySelector('.upload-detail');
  const cancelBtn = itemEl.querySelector('.btn-cancel-item');

  // Shared helper — mark this item as cancelled in the UI
  function _markCancelled() {
    entry.abortFn = null;
    detailEl.className   = 'upload-detail';
    detailEl.textContent = '';
    barEl.classList.add('cancelled');
    statusEl.className   = 'upload-status cancelled';
    statusEl.textContent = '\u2715 Cancelled';
    if (cancelBtn) cancelBtn.style.display = 'none';
    uploadStats[zoneId].cancelled++;
  }

  // ── Step 1: duplicate check (cancellable via AbortController) ────────
  statusEl.className   = 'upload-status checking';
  statusEl.textContent = 'Checking\u2026';

  const checkCtrl  = new AbortController();
  entry.abortFn    = () => { entry.cancelled = true; checkCtrl.abort(); };

  let uploadName = file.name;
  const dupResult = await checkDuplicate(file, checkCtrl.signal);

  if (entry.cancelled) { _markCancelled(); return; }
  entry.abortFn = null;

  if (dupResult && dupResult.found) {
    const newName = addDupSuffix(file.name);
    const choice  = await showDupModal(file.name, dupResult.path, newName);

    if (entry.cancelled) { _markCancelled(); return; }

    if (!choice.proceed) {
      statusEl.className   = 'upload-status skipped';
      statusEl.textContent = '\u2296 Skipped';
      if (cancelBtn) cancelBtn.style.display = 'none';
      uploadStats[zoneId].skipped++;
      return;
    }
    uploadName = choice.newName;
    const nameEl = itemEl.querySelector('.upload-name');
    nameEl.textContent = truncateName(uploadName, 36);
    nameEl.title       = uploadName;
  }

  // ── Step 2: XHR upload (cancellable via xhr.abort()) ─────────────────
  statusEl.className   = 'upload-status uploading';
  statusEl.textContent = '0%';

  const path = ZONE_PATHS[zoneId]();
  const url  = '/api/v1/files/upload-stream'
             + '?path='     + encodeURIComponent(path)
             + '&filename=' + encodeURIComponent(uploadName);

  await new Promise(resolve => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('Authorization', 'Bearer ' + state.token);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');

    entry.abortFn = () => xhr.abort();

    let rateBase = 0, rateBaseTime = Date.now(), lastUiUpdate = 0;

    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      barEl.style.width    = pct + '%';
      statusEl.textContent = pct + '%';
      // Hide cancel once 100% sent — abort has no useful effect after this
      if (pct >= 100 && cancelBtn) cancelBtn.style.display = 'none';
      const now = Date.now();
      if (now - lastUiUpdate >= 300) {
        const dt   = (now - rateBaseTime) / 1000;
        const rate = dt > 0 ? (e.loaded - rateBase) / dt : 0;
        detailEl.className   = 'upload-detail visible';
        detailEl.textContent = fmtBytes(e.loaded) + ' / ' + fmtBytes(e.total)
                             + (rate > 500 ? '  \u00b7  ' + fmtSpeed(rate) : '');
        rateBase = e.loaded; rateBaseTime = now; lastUiUpdate = now;
      }
    };

    xhr.onabort = () => {
      detailEl.className = 'upload-detail';
      detailEl.textContent = '';
      _markCancelled();
      resolve();
    };

    xhr.onload = () => {
      entry.abortFn = null;
      detailEl.className   = 'upload-detail';
      detailEl.textContent = '';
      if (cancelBtn) cancelBtn.style.display = 'none';
      if (xhr.status === 401) {
        statusEl.className   = 'upload-status failed';
        statusEl.textContent = 'Session expired';
        barEl.classList.add('failed');
        uploadStats[zoneId].failed++;
        setTimeout(() => document.getElementById('btn-logout').click(), 1500);
        resolve();
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        barEl.style.width    = '100%';
        barEl.classList.add('done');
        statusEl.className   = 'upload-status done';
        statusEl.textContent = '\u2713 Done';
        uploadStats[zoneId].done++;
        resolve();
      } else {
        let errMsg = 'Failed (' + xhr.status + ')';
        try {
          const body = JSON.parse(xhr.responseText);
          errMsg = body.detail || errMsg;
        } catch(_) {}
        markFailed(zoneId, entry, itemEl, statusEl, barEl, errMsg, resolve);
      }
    };

    xhr.onerror = () => {
      entry.abortFn = null;
      detailEl.className   = 'upload-detail';
      detailEl.textContent = '';
      markFailed(zoneId, entry, itemEl, statusEl, barEl, 'Network error', resolve);
    };

    xhr.send(file);
  });
}

function markFailed(zoneId, entry, itemEl, statusEl, barEl, msg, resolve) {
  entry.abortFn = null;
  const cancelBtn = itemEl.querySelector('.btn-cancel-item');
  if (cancelBtn) cancelBtn.style.display = 'none';
  barEl.classList.add('failed');
  statusEl.className   = 'upload-status failed';
  statusEl.textContent = '\u2717 ' + msg;
  uploadStats[zoneId].failed++;
  const row      = itemEl.querySelector('.upload-row');
  const retryBtn = document.createElement('button');
  retryBtn.className   = 'btn-retry';
  retryBtn.textContent = 'Retry';
  retryBtn.addEventListener('click', () => {
    retryBtn.remove();
    entry.cancelled      = false;
    barEl.className      = 'upload-bar';
    barEl.style.width    = '0%';
    statusEl.className   = 'upload-status pending';
    statusEl.textContent = 'Queued';
    if (cancelBtn) cancelBtn.style.display = '';
    uploadStats[zoneId].failed = Math.max(0, uploadStats[zoneId].failed - 1);
    uploadQueues[zoneId].unshift(entry);
    updateZoneStats(zoneId);
    updateCancelAllButton(zoneId);
    processQueue(zoneId);
  });
  row.appendChild(retryBtn);
  resolve();
}

function updateZoneStats(zoneId) {
  const el = document.getElementById('stats-' + zoneId);
  if (!el) return;
  const { done, failed, skipped, cancelled } = uploadStats[zoneId];
  if (done + failed + skipped + cancelled === 0) { el.textContent = ''; return; }
  const parts = [];
  if (done)      parts.push(done      + ' uploaded');
  if (failed)    parts.push(failed    + ' failed');
  if (skipped)   parts.push(skipped   + ' skipped');
  if (cancelled) parts.push(cancelled + ' cancelled');
  el.textContent = parts.join(' \u00b7 ');
}

// ── PIN Modal ────────────────────────────────────────────────────────────
function openPinModal(user) {
  document.getElementById('pin-avatar').textContent   = user.icon_emoji || '👤';
  document.getElementById('pin-username').textContent = user.name;
  document.getElementById('pin-error').textContent    = '';
  resetPin();
  buildKeypad();
  document.getElementById('pin-overlay').classList.add('open');
}

function closePinModal() {
  document.getElementById('pin-overlay').classList.remove('open');
  resetPin();
}

function resetPin() {
  state.pin = '';
  updateDots();
}

function updateDots() {
  for (let i = 0; i < 4; i++) {
    document.getElementById('d' + i).classList.toggle('filled', i < state.pin.length);
  }
}

function buildKeypad() {
  const kp = document.getElementById('pin-keypad');
  kp.innerHTML = '';
  const keys = ['1','2','3','4','5','6','7','8','9','spacer','0','del'];
  keys.forEach(k => {
    const btn = document.createElement('button');
    btn.className = 'pin-key' + (k === 'del' ? ' del' : '') + (k === 'spacer' ? ' spacer' : '');
    btn.textContent = k === 'del' ? '\u232b' : k === 'spacer' ? '' : k;
    btn.type = 'button';
    if (k !== 'spacer') {
      btn.addEventListener('click', () => onPinKey(k));
    }
    kp.appendChild(btn);
  });
}

function onPinKey(key) {
  if (key === 'del') {
    state.pin = state.pin.slice(0, -1);
    updateDots();
    return;
  }
  if (state.pin.length >= 4) return;
  state.pin += key;
  updateDots();
  if (state.pin.length === 4) {
    doLogin(state.selectedUser.name, state.pin);
  }
}

// Close PIN modal on overlay click
document.getElementById('pin-overlay').addEventListener('click', e => {
  if (e.target === e.currentTarget) closePinModal();
});
document.getElementById('pin-cancel').addEventListener('click', closePinModal);

// Keyboard support for PIN
document.addEventListener('keydown', e => {
  if (!document.getElementById('pin-overlay').classList.contains('open')) return;
  if (e.key >= '0' && e.key <= '9') { onPinKey(e.key); return; }
  if (e.key === 'Backspace') { onPinKey('del'); return; }
  if (e.key === 'Escape') { closePinModal(); return; }
});

// ── Utility ──────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Logout ───────────────────────────────────────────────────────────────
document.getElementById('btn-logout').addEventListener('click', () => {
  sessionStorage.removeItem('ahc_token');
  sessionStorage.removeItem('ahc_user');
  state.token    = null;
  state.userName = null;
  state.selectedUser = null;
  // Clear queues, progress, stats and clear buttons so stale entries don't appear on next login
  ['personal', 'family', 'entertainment'].forEach(z => {
    const q = document.getElementById('queue-' + z);
    if (q) q.innerHTML = '';
    const s = document.getElementById('stats-' + z);
    if (s) s.textContent = '';
    const c = document.getElementById('clear-' + z);
    if (c) c.style.display = 'none';
    const ca = document.getElementById('cancel-all-' + z);
    if (ca) ca.style.display = 'none';
  });
  resetUploadState();
  showView('view-picker');
  loadUsers();
});

// ── Boot ──────────────────────────────────────────────────────────────────
// Set up zones once — NOT inside initDashboard() to prevent duplicate
// event listeners accumulating across logout/login cycles.
['personal', 'family', 'entertainment'].forEach(zone => {
  setupZone(zone);
  document.getElementById('clear-' + zone).addEventListener('click', () => clearDone(zone));
  document.getElementById('cancel-all-' + zone).addEventListener('click', () => cancelAll(zone));
});
// Keep the SPA on /web so the browser back button does not navigate away.
history.replaceState({ ahc: true }, '', '/web');
window.addEventListener('popstate', () => history.pushState({ ahc: true }, '', '/web'));
loadUsers();
</script>
</body>
</html>"""


@router.get("/web", response_class=HTMLResponse, include_in_schema=False)
async def web_upload_portal():
    """Serve the self-contained web upload portal (LAN-only drag-and-drop interface)."""
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    }
    return HTMLResponse(content=_HTML, status_code=200, headers=headers)
