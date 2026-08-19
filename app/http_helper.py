"""
A plain-HTTP page on port 80 whose only job is to explain the certificate warning.

Someone typing `cubie.local` into a browser gets HTTP on port 80, and today that is a connection
refused — so they try `https://cubie.local:8443`, meet "Your connection is not secure", and have no
way to tell a self-hosted box on their own network apart from a genuinely dangerous site. Both
readings look identical in the browser, and the honest one is the less alarming one.

So this serves one page: what the warning means, why it is expected here, and a link through. It is
better to explain the warning once than to leave a family assuming their NAS is broken or unsafe.

Deliberately minimal, because it is the only thing on this box reachable without TLS:

  * one route, every path, GET only — nothing to POST to, no parameters read, no state touched
  * no API surface, no proxying, no session or token handling of any kind
  * static text; the only dynamic value is the host the browser already typed, echoed HTML-escaped
  * no redirect to HTTPS. A 301 would land the person on the warning with no explanation, which is
    the situation this exists to fix.
"""

from __future__ import annotations

import html
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

logger = logging.getLogger("aihomecloud.http_helper")

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AiHomeCloud &mdash; opening your box</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#FBF7F0; color:#2B241B; display:grid; place-items:center; min-height:100vh; }}
  @media (prefers-color-scheme: dark) {{ body {{ background:#17140F; color:#EFE7DA; }} }}
  main {{ max-width:34rem; padding:2rem 1.5rem; }}
  h1 {{ font-size:1.6rem; margin:0 0 .25rem; }}
  .sub {{ opacity:.7; margin:0 0 1.75rem; }}
  .card {{ background:rgba(127,127,127,.09); border-radius:14px; padding:1.1rem 1.2rem; margin:1rem 0; }}
  .card h2 {{ font-size:1rem; margin:0 0 .4rem; }}
  .card p {{ margin:0; font-size:.95rem; opacity:.85; }}
  a.go {{ display:inline-block; margin-top:1.25rem; background:#B4471F; color:#fff;
          text-decoration:none; padding:.8rem 1.4rem; border-radius:11px; font-weight:600; }}
  code {{ background:rgba(127,127,127,.16); padding:.1rem .35rem; border-radius:5px; font-size:.9em; }}
  .foot {{ margin-top:1.75rem; font-size:.82rem; opacity:.6; }}
</style></head>
<body><main>
  <h1>Your box is here</h1>
  <p class="sub">One thing to know before you open it.</p>

  <div class="card">
    <h2>Your browser will say &ldquo;not secure&rdquo;. That is expected.</h2>
    <p>The connection <em>is</em> encrypted. What your browser cannot do is check with an outside
       authority that this really is your box &mdash; because it is on your own network, not the
       public internet, and no authority on earth is allowed to vouch for a home address.</p>
  </div>

  <div class="card">
    <h2>What to do</h2>
    <p>Open the link below, then choose <strong>Advanced</strong> &rarr;
       <strong>Proceed to {host}</strong>. Your browser remembers this, so you are asked once per
       device, not every time.</p>
  </div>

  <div class="card">
    <h2>When you should <em>not</em> proceed</h2>
    <p>Only continue if you typed this address yourself and you are on your own home network. If you
       followed a link from a message or an email, close this page.</p>
  </div>

  <a class="go" href="https://{host}:{port}/app/">Open AiHomeCloud</a>

  <p class="foot">The AiHomeCloud app for Android does not show this warning &mdash; it recognises
     your box by its own key instead of asking an outside authority.</p>
</main></body></html>
"""


async def _explain(request: Request) -> HTMLResponse:
    # The Host header is attacker-controllable in general, so it is HTML-escaped and used only as
    # display text and in a link back to this same box. Falling back to the configured hostname
    # keeps the page useful when a client sends no Host at all.
    raw_host = (request.headers.get("host") or "").split(":")[0]
    host = html.escape(raw_host) if raw_host else "this box"
    from .config import settings  # local import keeps this module importable in isolation

    port = int(getattr(settings, "port", 8443) or 8443)
    return HTMLResponse(
        _PAGE.format(host=host, port=port),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def build_app() -> Starlette:
    """Every path, GET only — a mistyped URL should still get the explanation."""
    return Starlette(routes=[Route("/{path:path}", _explain, methods=["GET", "HEAD"])])
