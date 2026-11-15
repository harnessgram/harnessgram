"""The Telegram constants and checks the configuration needs, in a module that imports almost nothing.

telegram.py (the HTTP client) imports http.client and urllib.request, which cost tens of milliseconds at start-up;
config.py, which every command and every hook loads, needs only these names. telegram.py re-exports them.
"""
from __future__ import annotations

import re
import urllib.parse

DEFAULT_API = "https://api.telegram.org"
DOWNLOAD_LIMIT = 20 * 1024 * 1024  # cloud Bot API getFile limit
CLOUD_UPLOAD_LIMIT = 50 * 1024 * 1024  # cloud Bot API upload limit
LOCAL_LIMIT = 2000 * 1024 * 1024  # a self-hosted `telegram-bot-api --local` server, both directions


def download_limit(local: bool = False) -> int:
    return LOCAL_LIMIT if local else DOWNLOAD_LIMIT


def upload_limit(local: bool = False) -> int:
    return LOCAL_LIMIT if local else CLOUD_UPLOAD_LIMIT


def valid_api_base(url: str) -> bool:
    """https anywhere, plain http only to this machine (the token is part of every URL)."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return False
    if not host or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"))


TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
