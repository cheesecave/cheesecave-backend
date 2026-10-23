"""The admin token check, in one place.

``verify_admin_token`` (every ``/admin/api`` route) and ``get_current_user_or_admin`` (the repository routes that
also accept the admin token) both end here, so the throttle, the weak-token guard and the client rules cannot be
bypassed through the other door.

Order: admin enabled, token not weak, token sent, client allow-listed, client waiting, compare, record the result.
A waiting client is turned away *before* the comparison, so no guess can succeed during a wait.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from typing import NamedTuple

from fastapi import Header, HTTPException, Request

from kohakuhub.auth import admin_session
from kohakuhub.auth.admin_client import in_allowlist, resolve_client
from kohakuhub.auth.admin_throttle import SharedAdminAuthThrottle, ThrottleParams
from kohakuhub.config import cfg
from kohakuhub.logger import get_logger

logger = get_logger("ADMIN-AUTH")

DEFAULT_TOKEN = "change-me-in-production"

# What the check remembers about clients that sent a wrong token: shared by every worker through the database.
throttle = SharedAdminAuthThrottle()

# The look at the client's wait, the comparison and the record of a failure are one decision. Without this, threads
# (the repository routes run in a thread pool) that arrive together would all find "no wait yet" and each get a guess.
# Other workers decide at the same time, so a few more guesses than the free ones are possible, never a flood.
_decision_lock = threading.Lock()

_weak_reported = False
_warned_shared_client = False


class AdminTokenCheck(NamedTuple):
    outcome: str  # disabled | weak | missing | blocked | invalid | session | csrf | ok
    retry_after: int = 0
    detail: str = ""


def token_is_weak() -> bool:
    admin = cfg.admin
    if admin.allow_weak_token:
        return False
    return admin.secret_token == DEFAULT_TOKEN or len(admin.secret_token) < admin.min_token_length


def _params() -> ThrottleParams:
    admin = cfg.admin
    return ThrottleParams(
        free=admin.auth_free_failures,
        base_wait=admin.auth_base_wait_seconds,
        max_wait=max(admin.auth_max_wait_seconds, admin.auth_base_wait_seconds),
        reset_after=admin.auth_reset_seconds,
    )


def _matches(token: str) -> bool:
    # Hash both sides, then compare in constant time: the comparison leaks nothing about the secret's length or content.
    provided = hashlib.sha3_512(token.encode()).hexdigest()
    expected = hashlib.sha3_512(cfg.admin.secret_token.encode()).hexdigest()
    return secrets.compare_digest(provided, expected)


def _user_agent(request) -> str:
    headers = getattr(request, "headers", None)
    lines = headers.getlist("user-agent") if headers is not None and hasattr(headers, "getlist") else []
    return (lines[0] if lines else "-")[:80]


def _closed() -> AdminTokenCheck | None:
    """The admin API is off, or its token is too weak to be trusted: nothing gets in, by token or by session."""
    global _weak_reported
    admin = cfg.admin
    if not admin.enabled:
        return AdminTokenCheck("disabled")
    if token_is_weak():
        if not _weak_reported:
            _weak_reported = True
            logger.error(
                "Admin API is disabled: the admin token is the default value or shorter than "
                f"{admin.min_token_length} characters. Set a long random CHEESE_CAVE_ADMIN_SECRET_TOKEN "
                "(scripts/generate_secret.py makes one)."
            )
        return AdminTokenCheck("weak")
    return None


def check_admin_token(request, token: str | None) -> AdminTokenCheck:
    global _warned_shared_client
    admin = cfg.admin
    if (closed := _closed()) is not None:
        return closed
    if not token:
        return AdminTokenCheck("missing")

    identity = resolve_client(request, admin.auth_trusted_proxy_hops)
    counting = admin.auth_throttle_enabled and not in_allowlist(identity.address, admin.auth_allowlist)
    if counting and not identity.trusted:
        # Every client behind the proxy looks like one: waiting on that key would let one person lock out everybody.
        counting = False
        if not _warned_shared_client:
            _warned_shared_client = True
            logger.warning(
                "Admin token throttle is not counting: requests carry X-Forwarded-For but "
                "ADMIN_AUTH_TRUSTED_PROXY_HOPS is 0, so clients cannot be told apart. Set it to the number of "
                "proxies in front of the API (GET /admin/api/auth/client shows what the server computed)."
            )

    with _decision_lock:
        return _decide(request, token, identity, counting)


def _decide(request, token: str, identity, counting: bool) -> AdminTokenCheck:
    admin = cfg.admin
    if counting:
        wait = throttle.check(identity.key)
        if wait:
            return AdminTokenCheck("blocked", wait)

    if _matches(token):
        if counting:
            throttle.record_success(identity.key)
        return AdminTokenCheck("ok")

    wait = throttle.record_failure(identity.key, _params()) if counting else 0
    logger.warning(
        f"Invalid admin token from {identity.key} (failure {throttle.failures(identity.key) if counting else '?'}, "
        f"user agent {_user_agent(request)!r})"
    )
    if wait:
        logger.info(f"Admin token: {identity.key} must wait {int(wait)}s before its next attempt")
    if throttle.note_failure_for_alert(admin.auth_alert_per_minute):
        logger.warning(
            f"More than {admin.auth_alert_per_minute} failed admin token attempts in the last minute "
            "(from one or many clients)"
        )
    return AdminTokenCheck("invalid")


def check_admin_session(request) -> AdminTokenCheck:
    """The browser session of the admin panel, from the cookie alone. A cookie request that changes something is also
    checked for the panel's header and an allowed origin. Sets ``request.state.admin_session`` when it is valid."""
    if (closed := _closed()) is not None:
        return closed
    cookie = request.cookies.get(admin_session.COOKIE_NAME) if cfg.admin.session_enabled else None
    if not cookie:
        return AdminTokenCheck("missing")
    if (problem := admin_session.csrf_problem(request)) is not None:
        return AdminTokenCheck("csrf", detail=problem)
    session = admin_session.authenticate(cookie)
    if session is None:
        return AdminTokenCheck("session")
    request.state.admin_session = session
    return AdminTokenCheck("ok")


def raise_for(check: AdminTokenCheck) -> bool:
    """The HTTP answer of a check for the admin routes: True when the token is valid, else an HTTPException."""
    if check.outcome == "ok":
        return True
    if check.outcome == "disabled":
        raise HTTPException(503, detail={"error": "Admin API is disabled"})
    if check.outcome == "weak":
        raise HTTPException(503, detail={"error": "Admin API disabled: token too weak"})
    if check.outcome == "missing":
        raise HTTPException(401, detail={"error": "Admin token required in X-Admin-Token header"})
    if check.outcome == "session":
        raise HTTPException(401, detail={"error": "Admin session expired, revoked or unknown"})
    if check.outcome == "csrf":
        raise HTTPException(403, detail={"error": check.detail})
    if check.outcome == "blocked":
        raise HTTPException(
            429,
            detail={
                "error": f"Too many failed attempts, try again in {check.retry_after} s",
                "retry_after": check.retry_after,
            },
            headers={"Retry-After": str(check.retry_after)},
        )
    raise HTTPException(403, detail={"error": "Invalid admin token"})


async def verify_admin_token(request: Request, x_admin_token: str | None = Header(None)) -> bool:
    """FastAPI dependency of the admin routes: the token header decides when it is sent, else the session cookie."""
    if x_admin_token:
        return raise_for(check_admin_token(request, x_admin_token))
    return raise_for(check_admin_session(request))
