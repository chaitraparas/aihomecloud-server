"""
Structured JSON logging configuration for the backend.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import jsonlogger


_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_ctx.get()
        return True


def set_request_id(request_id: str) -> Any:
    return _request_id_ctx.set(request_id)


def reset_request_id(token: Any) -> None:
    _request_id_ctx.reset(token)


def configure_logging(log_level: str) -> None:
    """Configure root logger to emit structured JSON lines."""
    level = getattr(logging, (log_level or "INFO").upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s %(module)s %(request_id)s",
            rename_fields={
                "asctime": "ts",
                "levelname": "level",
                "message": "msg",
            },
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)

    # httpx (used internally by python-telegram-bot to talk to the local Bot API server) logs
    # every request at INFO as "HTTP Request: POST http://.../bot<TOKEN>/getUpdates ..." --
    # the bot token sits directly in the URL path (that's how the Telegram Bot API is designed),
    # so at the root's default INFO level this wrote the live token in plaintext to the systemd
    # journal on every single poll cycle -- found live 2026-07-15, a real exposure since journal
    # contents get read routinely for diagnostics (including by AI agents). httpcore capped the
    # same way as a precaution: it's httpx's own underlying transport and logs at INFO too,
    # though its connection-pool-event messages don't carry the URL the same way. Real errors
    # from either still surface at WARNING/ERROR.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
