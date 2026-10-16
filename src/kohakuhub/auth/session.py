"""How long a login session lasts, how using it renews it, and the cookie that carries it.

A session lasts ``session_expire_hours`` after its last use: a request made with it
moves ``expires_at`` forward and the cookie's ``Max-Age`` with it, so someone who
comes back within the window never has to sign in again. ``session_absolute_days``
caps that from the login (0 = no cap), so a stolen cookie cannot be kept alive for
ever. ``created_at`` is the login time, so the cap needs no column of its own.

Renewal writes to the database at most about once an hour per session: a request only
renews when that would move the expiry forward by ``RENEW_INTERVAL`` or more. That one
comparison also keeps a session at its cap, and one made under a longer window than the
current setting, from being touched.
"""

from datetime import datetime, timedelta, timezone

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from kohakuhub.config import cfg
from kohakuhub.db import Session

COOKIE_NAME = "session_id"
RENEW_INTERVAL = timedelta(hours=1)


def _utc(value: datetime) -> datetime:
    """Peewee returns naive datetimes from a zone-less column; they are UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def next_expiry(created_at: datetime, now: datetime) -> datetime:
    """The expiry of a session used at ``now``: the idle window, but not past the cap."""
    expiry = now + timedelta(hours=cfg.auth.session_expire_hours)
    if cfg.auth.session_absolute_days > 0:
        expiry = min(
            expiry, _utc(created_at) + timedelta(days=cfg.auth.session_absolute_days)
        )
    return expiry


def set_session_cookie(
    response: Response, session_id: str, expires_at: datetime, now: datetime | None = None
) -> None:
    """Set the session cookie to live exactly as long as the session does."""
    now = now or datetime.now(timezone.utc)
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        httponly=True,
        max_age=max(0, int((expires_at - now).total_seconds())),
        samesite="lax",
    )


def renew_session(
    request: Request, session: Session, now: datetime | None = None
) -> None:
    """Push a used session's expiry forward, and tell the middleware to resend its cookie."""
    now = now or datetime.now(timezone.utc)
    expiry = next_expiry(session.created_at, now)
    if expiry - _utc(session.expires_at) < RENEW_INTERVAL:
        return
    # Only ever forward, and only a row that is still there: a logout or a renewal that
    # got in first is not undone.
    renewed = (
        Session.update(expires_at=expiry)
        .where((Session.id == session.id) & (Session.expires_at < expiry))
        .execute()
    )
    if renewed:
        request.state.session_renewal = (session.session_id, expiry)


class SessionRenewalMiddleware(BaseHTTPMiddleware):
    """Resend the session cookie of a session the request renewed."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        renewal = getattr(request.state, "session_renewal", None)
        if renewal:
            set_session_cookie(response, *renewal)
        return response
