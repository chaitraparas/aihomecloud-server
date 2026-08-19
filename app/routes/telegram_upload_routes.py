"""
One-time upload endpoint for Telegram bot large-file handling.

When a user sends a file >20 MB to the Telegram bot, the bot generates a
short-lived upload token and sends a clickable link.  The user opens the link
on their phone, picks the file, and uploads it directly to the NAS — no
Telegram size limits apply.

Security:
  - Tokens are single-use and expire after 15 minutes.
  - No JWT or session auth required (the token IS the auth).
  - Tokens are bound to a specific chat_id and destination.
  - Uploaded file size is bounded by settings.max_upload_bytes.
"""

import asyncio
import html
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Request
from fastapi.responses import HTMLResponse

from ..config import settings
from ..models import MessageResponse
from ..limiter import limiter

logger = logging.getLogger("aihomecloud.telegram_upload")

router = APIRouter(tags=["telegram-upload"])

_TOKEN_TTL_SECONDS = 15 * 60  # 15 minutes


@dataclass
class UploadToken:
    token: str
    chat_id: int
    destination: str          # "private", "shared", "entertainment"
    owner: str                # personal folder owner
    filename: str             # suggested filename from Telegram
    created_at: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > _TOKEN_TTL_SECONDS


# In-memory token store — tokens are single-use and short-lived.
_upload_tokens: dict[str, UploadToken] = {}


def create_upload_token(
    chat_id: int,
    destination: str,
    owner: str,
    filename: str,
) -> str:
    """Create a one-time upload token and return it."""
    _purge_expired()
    token = secrets.token_urlsafe(32)
    _upload_tokens[token] = UploadToken(
        token=token,
        chat_id=chat_id,
        destination=destination,
        owner=owner,
        filename=filename,
    )
    return token


def pop_upload_token(token: str) -> Optional[UploadToken]:
    """Consume and return a valid token, or None if expired/missing."""
    _purge_expired()
    ut = _upload_tokens.pop(token, None)
    if ut is None or ut.expired:
        return None
    return ut


def _purge_expired() -> None:
    """Remove all expired tokens."""
    now = time.monotonic()
    expired = [k for k, v in _upload_tokens.items()
               if (now - v.created_at) > _TOKEN_TTL_SECONDS]
    for k in expired:
        _upload_tokens.pop(k, None)


def get_upload_url(request: Request, token: str) -> str:
    """Build the full upload URL for the given token."""
    return str(request.url_for("telegram_upload_form", token=token))


# ---------------------------------------------------------------------------
# HTML upload form (mobile-friendly)
# ---------------------------------------------------------------------------

_UPLOAD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AiHomeCloud Upload</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh; padding: 1rem;
  }
  .card {
    background: #16213e; border-radius: 16px; padding: 2rem;
    max-width: 420px; width: 100%; box-shadow: 0 8px 32px rgba(0,0,0,.3);
  }
  h1 { font-size: 1.3rem; margin-bottom: .5rem; color: #64ffda; }
  .info { font-size: .9rem; color: #aaa; margin-bottom: 1.5rem; }
  .dest { color: #bb86fc; font-weight: bold; }
  label.file-label {
    display: block; padding: 1.2rem; border: 2px dashed #64ffda44;
    border-radius: 12px; text-align: center; cursor: pointer;
    margin-bottom: 1rem; transition: border-color .2s;
  }
  label.file-label:hover { border-color: #64ffda; }
  input[type=file] { display: none; }
  .filename { font-size: .85rem; color: #64ffda; margin-top: .5rem; word-break: break-all; }
  button {
    width: 100%; padding: 1rem; border: none; border-radius: 12px;
    background: #64ffda; color: #1a1a2e; font-size: 1.1rem;
    font-weight: bold; cursor: pointer; transition: opacity .2s;
  }
  button:disabled { opacity: .4; cursor: not-allowed; }
  .progress { display: none; margin-top: 1rem; }
  .progress-bar {
    height: 8px; border-radius: 4px; background: #333;
    overflow: hidden; margin-bottom: .5rem;
  }
  .progress-fill {
    height: 100%; background: #64ffda; width: 0%;
    transition: width .3s;
  }
  .progress-text { font-size: .85rem; color: #aaa; text-align: center; }
  .result { margin-top: 1rem; padding: 1rem; border-radius: 8px; text-align: center; display: none; }
  .result.ok { background: #1b5e20; color: #a5d6a7; }
  .result.err { background: #b71c1c; color: #ef9a9a; }
</style>
</head>
<body>
<div class="card">
  <h1>📤 AiHomeCloud Upload</h1>
  <p class="info">
    Upload <strong>{{FILENAME}}</strong> to
    <span class="dest">{{DESTINATION}}</span>.
    <br>Link expires in 15 minutes.
  </p>
  <form id="form" enctype="multipart/form-data">
    <label class="file-label" id="drop-zone">
      Tap to select file
      <input type="file" id="file-input" name="file">
      <div class="filename" id="file-name"></div>
    </label>
    <button type="submit" id="btn" disabled>Upload</button>
  </form>
  <div class="progress" id="progress">
    <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
    <div class="progress-text" id="progress-text">0%</div>
  </div>
  <div class="result" id="result"></div>
</div>
<script>
const fileInput = document.getElementById('file-input');
const fileName = document.getElementById('file-name');
const btn = document.getElementById('btn');
const form = document.getElementById('form');
const progress = document.getElementById('progress');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');
const result = document.getElementById('result');

fileInput.addEventListener('change', () => {
  if (fileInput.files.length) {
    fileName.textContent = fileInput.files[0].name;
    btn.disabled = false;
  }
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!fileInput.files.length) return;
  btn.disabled = true;
  progress.style.display = 'block';
  result.style.display = 'none';

  const fd = new FormData();
  fd.append('file', fileInput.files[0]);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', window.location.href);

  xhr.upload.addEventListener('progress', (ev) => {
    if (ev.lengthComputable) {
      const pct = Math.round(ev.loaded / ev.total * 100);
      progressFill.style.width = pct + '%';
      progressText.textContent = pct + '%';
    }
  });

  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      let msg = 'Upload complete!';
      try { msg = JSON.parse(xhr.responseText).message || msg; } catch {}
      result.className = 'result ok';
      result.textContent = '✅ ' + msg;
    } else {
      let msg = 'Upload failed.';
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
      result.className = 'result err';
      result.textContent = '❌ ' + msg;
      btn.disabled = false;
    }
    result.style.display = 'block';
  };

  xhr.onerror = () => {
    result.className = 'result err';
    result.textContent = '❌ Network error. Check your connection.';
    result.style.display = 'block';
    btn.disabled = false;
  };

  xhr.send(fd);
});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/api/telegram-upload/{token}", response_class=HTMLResponse, name="telegram_upload_form")
async def upload_form(token: str):
    """Serve the one-time upload HTML form."""
    _purge_expired()
    ut = _upload_tokens.get(token)
    if ut is None or ut.expired:
        raise HTTPException(status_code=410, detail="Upload link expired or invalid.")

    dest_labels = {
        "private": f"Private personal ({ut.owner})",
        "shared": "Shared personal",
        "entertainment": "Entertainment",
    }
    # L-5 fix (security audit 2026-08): ut.filename is Telegram's own filename for whatever the
    # sender uploaded -- fully attacker-controlled -- interpolated straight into HTML with no
    # escaping. create_upload_token() (the only thing that populates _upload_tokens) has no
    # caller yet, so this is unreachable today, but latent: the module's own docstring describes
    # the intended flow (bot sends this link to a user, who opens it in a browser), and nothing
    # about that design implies the filename is trusted. Escaped here rather than left for
    # whoever wires up the caller to remember.
    page = _UPLOAD_HTML.replace("{{FILENAME}}", html.escape(ut.filename))
    page = page.replace("{{DESTINATION}}", html.escape(dest_labels.get(ut.destination, ut.destination)))
    return HTMLResponse(content=page)


@router.post("/api/telegram-upload/{token}", response_model=MessageResponse)
@limiter.limit("20/minute")
async def upload_file(request: Request, token: str, file: UploadFile = File(...)):
    """Receive the uploaded file, store it, and notify the user via Telegram."""
    ut = pop_upload_token(token)
    if ut is None:
        raise HTTPException(status_code=410, detail="Upload link expired or already used.")

    from ..file_sorter import _sort_file, _unique_dest
    from ..document_index import index_document
    from .file_routes import BLOCKED_EXTENSIONS

    filename = Path(file.filename or ut.filename).name
    if not filename or filename in (".", ".."):
        filename = ut.filename

    # Guard: blocked extension
    ext = Path(filename).suffix.lower()
    if ext in BLOCKED_EXTENSIONS:
        raise HTTPException(status_code=422, detail=f"File type '{ext}' is not allowed.")

    # Guard: file size
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum upload size of {settings.max_upload_bytes} bytes.",
        )

    try:
        if ut.destination == "entertainment":
            dest_dir = settings.entertainment_path
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = _unique_dest(dest_dir, filename)
            await _write_upload(file, dest_path)
            final_path = dest_path
            target_label = "entertainment"
        elif ut.destination == "shared":
            base_dir = settings.family_path
            final_path = await _sort_uploaded_file(file, filename, base_dir, "family")
            target_label = "family"
        else:  # "private"
            base_dir = settings.personal_path / ut.owner
            final_path = await _sort_uploaded_file(file, filename, base_dir, ut.owner)
            target_label = f"private personal ({ut.owner})"

        # Notify via Telegram
        await _notify_telegram(
            ut.chat_id,
            f"✅ Upload complete! Saved to {target_label}: {final_path.name}",
        )

        logger.info(
            "telegram_upload_completed chat_id=%s dest=%s file=%s",
            ut.chat_id, ut.destination, final_path.name,
        )
        return {"message": f"Saved to {target_label}: {final_path.name}"}

    except Exception as exc:
        logger.error(
            "telegram_upload_failed chat_id=%s file=%s error=%s",
            ut.chat_id, filename, exc,
        )
        await _notify_telegram(ut.chat_id, f"⚠️ Upload failed for {filename}. Please try again.")
        raise HTTPException(status_code=500, detail="Upload processing failed.")


async def _write_upload(upload_file: UploadFile, dest_path: Path) -> None:
    """Stream an uploaded file to disk using a thread executor for writes."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    chunk_size = settings.upload_chunk_size
    loop = asyncio.get_running_loop()
    with open(dest_path, "wb") as f:
        while True:
            chunk = await upload_file.read(chunk_size)
            if not chunk:
                break
            await loop.run_in_executor(None, f.write, chunk)


async def _sort_uploaded_file(
    upload_file: UploadFile,
    filename: str,
    base_dir: Path,
    added_by: str,
) -> Path:
    """Write to .inbox, sort, and optionally index."""
    from ..file_sorter import _sort_file, _unique_dest
    from ..document_index import index_document

    inbox = base_dir / ".inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    # _unique_dest (not a raw inbox/filename write) — otherwise a second same-named
    # Telegram upload landing before the sort worker picks up the first would silently
    # truncate it via open(..., "wb"). Found 2026-07-15 auditing overwrite-safety across
    # every write path; mirrors the "entertainment" branch above, which already does this.
    temp_path = _unique_dest(inbox, filename)
    await _write_upload(upload_file, temp_path)

    dest = _sort_file(temp_path, base_dir, check_age=False)
    if dest is None:
        raise RuntimeError(f"Failed to sort uploaded file: {filename}")

    if dest.parent.name == "Documents":
        await index_document(str(dest), dest.name, added_by)

    return dest


async def _notify_telegram(chat_id: int, message: str) -> None:
    """Send a notification message to a Telegram chat via the bot."""
    from ..telegram_bot import _application
    if _application is None:
        logger.debug("Cannot notify Telegram — bot not running")
        return
    try:
        await _application.bot.send_message(chat_id=chat_id, text=message)
    except Exception as exc:
        logger.warning("Telegram notification failed chat_id=%s: %s", chat_id, exc)
