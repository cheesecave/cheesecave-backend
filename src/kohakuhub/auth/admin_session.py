"""Browser sessions of the admin panel.

The admin panel exchanges the shared admin token once for a session: a random id held in an HttpOnly cookie, whose
hash is the only thing the database keeps. The page never holds the token afterwards, a reload restores the session, one
session can be revoked, and changing the shared token ends all of them (each session carries a keyed fingerprint of the
token it was made with). ``X-Admin-Token`` keeps working for scripts and is checked first.

A session ends at its idle end (moved forward by use, never past the absolute end). A cookie request that changes
something also has to carry the panel's own header and an allowed ``Origin``: a page of another site can send neither.

Keep this module free of ``kohakuhub.api`` imports: the worker imports the cleanup that uses it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import Response

from kohakuhub.auth.admin_client import resolve_client
from kohakuhub.config import cfg
from kohakuhub.db import AdminSession

COOKIE_NAME = "admin_session"
COOKIE_PATH = "/admin"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "cheesecave-admin"
# What a read of the last use costs: written at most this often
TOUCH_INTERVAL = timedelta(seconds=60)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _utc(value: datetime) -> datetime:
    """The database hands back naive datetimes from a zone-less column; they are UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def hash_session_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def fingerprint(token: str) -> str:
    """A keyed hash of the shared token: it ends every session when the token changes, and says nothing of the token."""
    key = cfg.auth.session_secret.encode()
    return hmac.new(key, b"admin-session-v1:" + token.encode(), hashlib.sha256).hexdigest()


def _limits(now: datetime) -> tuple[datetime, datetime]:
    """The idle end of a session used at ``now`` and its absolute end, given the settings."""
    idle = now + timedelta(minutes=cfg.admin.session_idle_minutes)
    absolute = now + timedelta(hours=cfg.admin.session_absolute_hours)
    return min(idle, absolute), absolute


def create(request, now: datetime | None = None) -> tuple[str, AdminSession]:
    """Make a session for the shared token; returns the id (to give the browser once) and the row."""
    now = now or datetime.now(timezone.utc)
    session_id = secrets.token_urlsafe(32)
    idle_end, absolute_end = _limits(now)
    identity = resolve_client(request, cfg.admin.auth_trusted_proxy_hops)
    headers = getattr(request, "headers", None)
    agent = (headers.get("user-agent") if headers is not None else None) or ""
    row = AdminSession.create(
        token_hash=hash_session_id(session_id),
        secret_fingerprint=fingerprint(cfg.admin.secret_token),
        created_at=now,
        last_used_at=now,
        expires_at=idle_end,
        absolute_expires_at=absolute_end,
        ip=identity.address,
        user_agent=agent[:200] or None,
    )
    return session_id, row


def authenticate(session_id: str | None, now: datetime | None = None) -> AdminSession | None:
    """The session a cookie value stands for, or ``None``. Using it moves its idle end forward."""
    if not session_id:
        return None
    now = now or datetime.now(timezone.utc)
    row = AdminSession.get_or_none(AdminSession.token_hash == hash_session_id(session_id))
    if row is None or row.revoked_at is not None:
        return None
    if _utc(row.expires_at) <= now or _utc(row.absolute_expires_at) <= now:
        return None
    if not hmac.compare_digest(row.secret_fingerprint or "", fingerprint(cfg.admin.secret_token)):
        # The shared token is not the one this session was made with: it is over, for good
        AdminSession.update(revoked_at=now).where(
            (AdminSession.secret_fingerprint != fingerprint(cfg.admin.secret_token))
            & (AdminSession.revoked_at.is_null())
        ).execute()
        return None
    if now - _utc(row.last_used_at) >= TOUCH_INTERVAL:
        idle_end = min(now + timedelta(minutes=cfg.admin.session_idle_minutes), _utc(row.absolute_expires_at))
        AdminSession.update(last_used_at=now, expires_at=idle_end).where(AdminSession.id == row.id).execute()
        row.last_used_at, row.expires_at = now, idle_end
    return row


def revoke(row: AdminSession, now: datetime | None = None) -> None:
    AdminSession.update(revoked_at=now or datetime.now(timezone.utc)).where(
        (AdminSession.id == row.id) & (AdminSession.revoked_at.is_null())
    ).execute()


def revoke_others(keep_id: int, now: datetime | None = None) -> int:
    return (
        AdminSession.update(revoked_at=now or datetime.now(timezone.utc))
        .where((AdminSession.id != keep_id) & (AdminSession.revoked_at.is_null()))
        .execute()
    )


def active_sessions(now: datetime | None = None) -> list[AdminSession]:
    now = now or datetime.now(timezone.utc)
    return list(
        AdminSession.select()
        .where(
            AdminSession.revoked_at.is_null()
            & (AdminSession.expires_at > now)
            & (AdminSession.absolute_expires_at > now)
        )
        .order_by(AdminSession.created_at.desc(), AdminSession.id.desc())
    )


def cookie_secure() -> bool:
    configured = cfg.admin.session_cookie_secure
    return configured if configured is not None else cfg.app.base_url.lower().startswith("https://")


def set_cookie(response: Response, session_id: str, absolute_expires_at: datetime, now: datetime | None = None) -> None:
    """The cookie lives as long as the session can (the idle end is the server's to enforce)."""
    now = now or datetime.now(timezone.utc)
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        max_age=max(0, int((_utc(absolute_expires_at) - now).total_seconds())),
        path=COOKIE_PATH,
        httponly=True,
        secure=cookie_secure(),
        samesite="strict",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path=COOKIE_PATH, httponly=True, secure=cookie_secure(), samesite="strict")


def _origin_of(value: str) -> str | None:
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def allowed_origins() -> set[str]:
    """The public address of the hub and the origins the operator added: the places the admin panel is served from."""
    found = {_origin_of(cfg.app.base_url), *(_origin_of(entry) for entry in cfg.admin.session_allowed_origins)}
    return {origin for origin in found if origin}


def csrf_problem(request) -> str | None:
    """Why a cookie request that changes something is refused, or ``None``. Reads need nothing more than the cookie."""
    if request.method.upper() in SAFE_METHODS:
        return None
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return "Missing X-Requested-With: cheesecave-admin header"
    origin = request.headers.get("origin") or ""
    if _origin_of(origin) not in allowed_origins():
        return "Origin not allowed for an admin session"
    return None
