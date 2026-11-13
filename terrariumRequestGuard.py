# -*- coding: utf-8 -*-
"""Refuse writes from other sites and oversized bodies (audit 2026-11-12, UI I3 and API I6).

The engine has no login by the owner's choice, so any web page open in a browser on the home
network could otherwise switch the relays: a form or fetch from another site carries no secret
the engine checks. Browsers always send Origin (or at least Referer) with such a request, so a
write whose Origin names another host is refused. Tools such as curl send neither and pass.

Kept apart from the webserver so it can be tested on its own.
"""
from urllib.parse import urlsplit

MAX_BODY = 64 * 1024
WRITES = {"POST", "PUT", "PATCH", "DELETE"}

CROSS_SITE = (403, "This request came from another site.")
TOO_LARGE = (413, "This request is too large.")


def _host_of(url: str):
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return (parts.netloc or "").lower()


def refusal(method: str, headers, content_length):
    """(status, sentence) when the request must be refused, else None.

    `headers` is any mapping with .get(); `content_length` is the declared body size or None/-1.
    """
    if content_length is not None and content_length > MAX_BODY:
        return TOO_LARGE

    if (method or "").upper() not in WRITES:
        return None

    host = (headers.get("Host") or "").lower()
    origin = headers.get("Origin")
    if origin is not None:
        # "null" is what a sandboxed or file:// page sends: never this server
        return None if origin != "null" and _host_of(origin) == host and host else CROSS_SITE

    referer = headers.get("Referer")
    if referer is not None:
        return None if _host_of(referer) == host and host else CROSS_SITE

    return None
