"""
Enforce the upload size cap at the transport layer, before anything buffers the body.

`settings.max_upload_bytes` used to be checked inside `ingest()`, which is far too late. By the
time a route handler runs, Starlette has already parsed the multipart body and spooled it to a
`SpooledTemporaryFile` — on disk once it passes the spool threshold. So the sequence for a hostile
or buggy client was: write the entire body to the board's disk, *then* look at how big it is and
return 413. On a 1 GB-RAM board with a family's photos on the same filesystem, an unauthenticated
attempt to fill the disk succeeds long before the check it is supposed to fail.

`/upload-stream` exists precisely because of this and does the right thing, but it was opt-in, and
a limit that depends on clients choosing the safe endpoint is not a limit.

This runs as raw ASGI rather than `BaseHTTPMiddleware` deliberately: BaseHTTPMiddleware reads the
request body itself to hand a `Request` to the next layer, which would reintroduce the buffering
this exists to prevent. At this level the body is still just `http.request` messages that have not
been read yet, so a rejection genuinely costs nothing.

Two checks, because either alone is insufficient:

  * `Content-Length` — rejects the honest oversized upload before a single byte is read.
  * a running byte count over the wrapped `receive` — `Content-Length` is client-supplied and may
    be absent entirely (`Transfer-Encoding: chunked`) or simply a lie. Counting what actually
    arrives is what makes the cap real.
"""

from __future__ import annotations

import logging

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("aihomecloud.upload_guard")

#: Only these can carry a body worth guarding. GET/HEAD/DELETE with a body is not something this
#: API accepts anyway, and leaving them unwrapped keeps the hot read path untouched.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class UploadSizeLimitMiddleware:
    """Reject request bodies over `max_bytes` without buffering them first."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # `max_bytes` of 0 means unlimited, matching how settings.max_upload_bytes documents itself.
        if scope["type"] != "http" or self.max_bytes <= 0 or scope["method"] not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    logger.warning(
                        "rejecting upload to %s: declared %s bytes exceeds cap %s",
                        scope.get("path", "?"), content_length, self.max_bytes,
                    )
                    await self._reject(send)
                    return
            except ValueError:
                pass  # unparseable — the counting path below still covers it

        received = 0
        over_limit = False

        async def counting_receive() -> Message:
            nonlocal received, over_limit
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    over_limit = True
                    # Present it to the app as a completed (truncated) body rather than letting it
                    # keep awaiting more. The app's own error handling then unwinds normally while
                    # the 413 below is what actually reaches the client.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        response_started = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                if over_limit:
                    # The app is answering a body it never fully received; its status would be
                    # misleading. Replace it with the truthful one.
                    logger.warning(
                        "rejecting upload to %s: body exceeded cap %s at %s bytes",
                        scope.get("path", "?"), self.max_bytes, received,
                    )
                    await self._reject(send)
                    response_started = True
                    return
                response_started = True
            elif over_limit and response_started:
                return  # 413 already sent; drop the app's body frames
            await send(message)

        await self.app(scope, counting_receive, guarded_send)

    async def _reject(self, send: Send) -> None:
        body = b'{"detail":"File exceeds the maximum upload size"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # The connection carries an unread body we are refusing to drain. Without this the
                # client keeps sending into a socket nobody is reading and the next request on the
                # same connection desynchronises.
                (b"connection", b"close"),
            ],
        })
        await send({"type": "http.response.body", "body": body})
