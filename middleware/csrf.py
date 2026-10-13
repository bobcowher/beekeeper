"""Refuse state-changing requests that a browser sent from another site.

Browsers name the sending page in Origin (and Referer). A form or fetch on a
malicious page could otherwise POST to Beekeeper — create a project, start a
pipeline task — using the visitor's session, or with no session at all when
auth is disabled. Requests carrying neither header (curl, the MCP server,
scripts) are not browser cross-site requests and pass through.
"""
from urllib.parse import urlsplit

from flask import abort, request

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _sender_host():
    origin = request.headers.get("Origin")
    if origin is not None:
        return urlsplit(origin).netloc if origin != "null" else ""
    referer = request.headers.get("Referer")
    return urlsplit(referer).netloc if referer is not None else None


def register_csrf_protection(app):
    if app.config.get("SESSION_COOKIE_SAMESITE") is None:   # Flask's default is None
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    @app.before_request
    def refuse_cross_site_writes():
        if request.method in SAFE_METHODS:
            return None
        sender = _sender_host()
        if sender is not None and sender != request.host:
            abort(403, description="Cross-site request refused")
        return None
