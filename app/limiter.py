"""Shared slowapi rate limiter instance — imported by main.py and route modules."""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
