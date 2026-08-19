"""Telegram bot package — split from the monolithic telegram_bot.py.

Public API is re-exported by the ``app.telegram_bot`` shim module so that
existing test imports (``import app.telegram_bot as tb``) and monkey-patches
(``patch("app.telegram_bot._is_allowed", ...)``) continue to work unchanged.
"""
