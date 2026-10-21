"""
Token authentication for the web UI.

The server holds one secret token (generated at startup unless you supply one).
Present it once, as `?token=...` in the URL or through the sign-in form, and
the browser gets a random session cookie; the token itself is not kept in the
browser. Scripts can send `Authorization: Bearer <token>` instead.

This protects a single-user or small-team deployment. It is not an account
system: everyone holding the token sees every job.
"""

import hmac
import secrets
import time
from typing import Dict, Optional

from fastapi import Request

TOKEN_ENV = "PII_FAKER_TOKEN"
SESSION_COOKIE = "pii_faker_session"

# A supplied token must be hard to guess: the server does not rate-limit attempts.
MIN_TOKEN_LENGTH = 16
SESSION_SECONDS = 8 * 3600


def generate_token() -> str:
    """A fresh random token (256 bits)."""
    return secrets.token_urlsafe(32)


class Authenticator:
    """Checks tokens and manages browser sessions, all in memory."""

    def __init__(self, token: Optional[str] = None, session_seconds: int = SESSION_SECONDS):
        if token is not None and len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"The access token must be at least {MIN_TOKEN_LENGTH} characters long.")
        self.token = token or generate_token()
        self.generated = token is None
        self.session_seconds = session_seconds
        self._sessions: Dict[str, float] = {}

    def check_token(self, candidate: str) -> bool:
        """Constant-time comparison, so response timing reveals nothing."""
        return hmac.compare_digest(candidate.encode(), self.token.encode())

    def open_session(self) -> str:
        """Start a browser session and return its cookie value."""
        self._prune()
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = time.time() + self.session_seconds
        return session_id

    def close_session(self, session_id: Optional[str]) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    def is_authenticated(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() == "bearer" and credentials and self.check_token(credentials.strip()):
            return True

        session_id = request.cookies.get(SESSION_COOKIE)
        expires = self._sessions.get(session_id) if session_id else None
        if expires is None:
            return False
        if expires < time.time():
            self._sessions.pop(session_id, None)
            return False
        return True

    def _prune(self) -> None:
        now = time.time()
        for session_id in [s for s, expires in self._sessions.items() if expires < now]:
            del self._sessions[session_id]


def session_cookie_options(request: Request, max_age: int) -> dict:
    """Cookie settings: unreadable by scripts, never sent cross-site."""
    return {
        "key": SESSION_COOKIE,
        "max_age": max_age,
        "httponly": True,
        "samesite": "strict",
        "secure": request.url.scheme == "https",
        "path": "/",
    }
