"""FastAPI dependencies for authentication."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Request

from kohakuhub.auth.admin_token import check_admin_token, raise_for
from kohakuhub.db import Session, Token, User
from kohakuhub.logger import get_logger
from kohakuhub.auth.session import renew_session
from kohakuhub.auth.utils import hash_token
from kohakuhub.auth.external_token_parser import parse_auth_header

logger = get_logger("AUTH")


def get_user_by_token(token_string: str) -> Optional[User]:
    """The active user an API token belongs to, or None.

    A token that is found counts as used (``last_used``), whether or not its
    user turns out to be active.
    """
    token = Token.get_or_none(Token.token_hash == hash_token(token_string))
    if not token:
        logger.debug("Token not found or invalid")
        return None

    Token.update(last_used=datetime.now(timezone.utc)).where(
        Token.id == token.id
    ).execute()

    user = token.user  # Use ForeignKey relationship
    if user and user.is_active:
        logger.debug(f"Authenticated via token: {user.username} (token_id={token.id})")
        return user
    logger.warning(f"Token {token.id} found but user inactive or not found")
    return None


def get_current_user(
    request: Request,
    session_id: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> User:
    """Get current authenticated user from session or token.

    Also parses external fallback tokens from Authorization header and stores
    them in request.state for use by fallback system.
    """

    session_id = str(session_id) if session_id is not None else None
    authorization = str(authorization) if authorization is not None else None

    # Parse external tokens from Authorization header
    auth_token, external_tokens = parse_auth_header(authorization)

    # Store external tokens in request.state for fallback decorators
    request.state.external_tokens = external_tokens

    # Log authentication attempt
    auth_method = []
    if session_id:
        auth_method.append(f"session={session_id[:8]}...")
    if auth_token:
        auth_method.append("token=Bearer ***")
    if external_tokens:
        auth_method.append(f"external_tokens={len(external_tokens)} sources")

    if auth_method:
        logger.debug(f"Auth attempt: {', '.join(auth_method)}")

    # Try session-based auth first (web UI)
    if session_id:
        session = Session.get_or_none(
            (Session.session_id == session_id)
            & (Session.expires_at > datetime.now(timezone.utc))
        )
        if session:
            user = session.user  # Use ForeignKey relationship
            if user and user.is_active:
                logger.debug(
                    f"Authenticated via session: {user.username} (session={session_id[:8]}...)"
                )
                renew_session(request, session)
                return user
            else:
                logger.warning(
                    f"Session {session_id[:8]}... found but user inactive or not found"
                )
        else:
            logger.debug(f"Session {session_id[:8]}... not found or expired")

    # Try token-based auth (API) - use parsed auth_token
    if auth_token:
        user = get_user_by_token(auth_token)
        if user:
            return user

    logger.debug("Authentication failed - no valid session or token")
    raise HTTPException(401, detail="Not authenticated")


def get_optional_user(
    request: Request,
    session_id: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> Optional[User]:
    """Get current user if authenticated, otherwise None.

    Also parses external fallback tokens from Authorization header and stores
    them in request.state for use by fallback system (even if auth fails).
    """
    try:
        return get_current_user(request, session_id, authorization)
    except HTTPException:
        # Still parse external tokens even if auth fails (for fallback access)
        _, external_tokens = parse_auth_header(authorization)
        request.state.external_tokens = external_tokens
        return None


def get_external_tokens(request: Request) -> dict[str, str]:
    """Extract external tokens from request state (populated by auth).

    Returns empty dict if no external tokens were provided.
    """
    return getattr(request.state, "external_tokens", {})


def get_current_user_or_admin(
    request: Request,
    session_id: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None),
) -> tuple[User | None, bool]:
    """Get current user or check admin token.

    Returns tuple of (user, is_admin):
    - If admin token valid: (None, True)
    - If user authenticated: (User, False)
    - If neither: raises 401

    This allows endpoints to accept both user auth AND admin auth.
    """
    # Try admin token first: the same check as the admin routes (throttle, weak-token guard, client rules).
    if x_admin_token:
        check = check_admin_token(request, x_admin_token)
        if check.outcome == "ok":
            # Admin token valid - set flag in request.state
            request.state.is_admin = True
            logger.debug("Authenticated as admin")
            return (None, True)
        if check.outcome == "blocked":
            raise_for(check)
        # Anything else (wrong, weak, admin API off): the token does not count, a user login still may.

    # Try user authentication
    try:
        user = get_current_user(request, session_id, authorization)
        request.state.is_admin = False
        return (user, False)
    except HTTPException:
        raise HTTPException(401, detail="Not authenticated")
