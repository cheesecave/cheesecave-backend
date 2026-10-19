"""Authentication API routes."""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

from kohakuhub.config import cfg
from kohakuhub.db import Session, Token, User, db
from kohakuhub.db_operations import (
    check_invitation_available,
    create_email_verification,
    create_session,
    create_token,
    create_user,
    create_user_organization,
    delete_email_verification,
    delete_token,
    get_email_verification,
    get_invitation,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    list_user_tokens,
    mark_invitation_used,
    update_user,
)
from kohakuhub.logger import get_logger
from kohakuhub.api.validation import RESERVED_NAMES
from kohakuhub.auth.dependencies import get_current_user
from kohakuhub.auth.session import next_expiry, set_session_cookie
from kohakuhub.auth.email import send_verification_email
from kohakuhub.auth.utils import (
    generate_session_secret,
    generate_token,
    get_expiry_time,
    hash_password,
    hash_token,
    verify_password,
)
from kohakuhub.utils.names import normalize_name
from kohakuhub.utils.datetime_utils import safe_isoformat
from kohakuhub.password_policy import Password
from kohakuhub.password_reset import find_valid, claim, iso, revoke_links

logger = get_logger("AUTH")

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: Password


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateTokenRequest(BaseModel):
    name: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: Password


class PasswordResetCheckRequest(BaseModel):
    token: str


class PasswordResetRequest(BaseModel):
    token: str
    new_password: Password


# One answer for a link that never existed, one that expired, one that was used and one that was withdrawn, so a link
# tells nobody anything about accounts or about what happened to it.
INVALID_RESET_LINK = "This reset link is invalid, expired or already used."


@router.post("/register")
async def register(req: RegisterRequest, invitation_token: str | None = None):
    """Register new user.

    Args:
        req: Registration request data
        invitation_token: Optional invitation token (required if invitation_only mode)

    Returns:
        Success response
    """
    # Check if registration requires invitation
    if cfg.auth.invitation_only:
        if not invitation_token:
            raise HTTPException(
                403,
                detail="Registration is invitation-only. Please use an invitation link or contact the administrator.",
            )

        # Validate invitation token
        invitation = get_invitation(invitation_token)
        if not invitation:
            raise HTTPException(400, detail="Invalid invitation token")

        # Check if invitation is for registration
        if invitation.action != "register_account":
            raise HTTPException(400, detail="Invalid invitation type for registration")

        # Check if invitation is available
        is_available, error_msg = check_invitation_available(invitation)
        if not is_available:
            raise HTTPException(400, detail=error_msg)

    logger.info(f"Registration attempt for username: {req.username}")

    # Check if username is reserved
    if (
        req.username.lower() in RESERVED_NAMES
        or normalize_name(req.username) in RESERVED_NAMES
    ):
        logger.warning(f"Registration failed: username '{req.username}' is reserved")
        raise HTTPException(
            400, detail=f"Username '{req.username}' is reserved and cannot be used"
        )

    # Check if username or email already exists and create user atomically
    with db.atomic():
        if get_user_by_username(req.username):
            logger.warning(
                f"Registration failed: username '{req.username}' already exists"
            )
            raise HTTPException(400, detail="Username already exists")

        if get_user_by_email(req.email):
            logger.warning(f"Registration failed: email '{req.email}' already exists")
            raise HTTPException(400, detail="Email already exists")

        # Check for normalized name conflicts (efficient O(1) lookup using indexed column)
        normalized = normalize_name(req.username)
        existing_normalized = User.get_or_none(User.normalized_name == normalized)
        if existing_normalized:
            logger.warning(
                f"Registration failed: username conflicts with '{existing_normalized.username}'"
            )
            entity_type = "organization" if existing_normalized.is_org else "user"
            raise HTTPException(
                400,
                detail=f"Username conflicts with existing {entity_type}: {existing_normalized.username}",
            )

        # Create user
        user = create_user(
            username=req.username,
            email=req.email,
            password_hash=hash_password(req.password),
            email_verified=not cfg.auth.require_email_verification,
        )

    logger.success(f"User registered: {user.username} (id={user.id})")

    # If registration used an invitation, mark it and add to org if specified
    if cfg.auth.invitation_only and invitation_token:
        invitation = get_invitation(invitation_token)
        if invitation:
            try:
                params = json.loads(invitation.parameters)
                org_id = params.get("org_id")

                with db.atomic():
                    mark_invitation_used(invitation, user)

                    # Add user to organization if specified
                    if org_id:
                        org = get_user_by_id(org_id)
                        if org:
                            role = params.get("role", "member")
                            create_user_organization(user, org, role)
                            logger.success(
                                f"User {user.username} added to org {org.username} as {role}"
                            )
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to process invitation on registration: {e}")

    # Send verification email if required
    if cfg.auth.require_email_verification:
        token = generate_token()
        create_email_verification(
            user=user, token=token, expires_at=get_expiry_time(24)
        )

        verification_email = await asyncio.to_thread(
            send_verification_email, req.email, req.username, token
        )
        if not verification_email:
            return {
                "success": True,
                "message": "User created but failed to send verification email",
                "email_verified": False,
            }

        return {
            "success": True,
            "message": "User created. Please check your email to verify your account.",
            "email_verified": False,
        }

    return {
        "success": True,
        "message": "User created successfully",
        "email_verified": True,
    }


@router.get("/verify-email")
async def verify_email(token: str, response: Response):
    """Verify email with token and automatically log in user."""
    logger.info(f"Email verification attempt with token: {token[:8]}...")

    verification = get_email_verification(token)

    if not verification:
        logger.warning(f"Invalid verification token: {token[:8]}...")
        return RedirectResponse(
            url=f"/?error=invalid_token&message=Invalid+or+expired+verification+token",
            status_code=302,
        )

    # Ensure expires_at is timezone-aware for comparison
    expires_at = verification.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at <= datetime.now(timezone.utc):
        logger.warning(f"Expired verification token: {token[:8]}...")
        # Redirect to login with error message
        return RedirectResponse(
            url=f"/?error=invalid_token&message=Invalid+or+expired+verification+token",
            status_code=302,
        )

    # Get user from foreign key
    user = verification.user
    if not user:
        logger.error(f"User not found for verification token: {token[:8]}...")
        return RedirectResponse(url="/?error=user_not_found", status_code=302)

    # Update user, delete verification, and create session atomically
    with db.atomic():
        update_user(user, email_verified=True)
        delete_email_verification(verification)

        logger.success(f"Email verified for user: {user.username}")

        # Create session for auto-login
        session_id = generate_token()
        session_secret = generate_session_secret()

        now = datetime.now(timezone.utc)
        expires_at = next_expiry(now, now)
        create_session(
            session_id=session_id,
            user=user,
            secret=session_secret,
            expires_at=expires_at,
        )

    # Set session cookie
    redirect_response = RedirectResponse(url=f"/{user.username}", status_code=302)
    set_session_cookie(redirect_response, session_id, expires_at, now)

    logger.success(
        f"Auto-login session created for: {user.username} (session={session_id[:8]}...)"
    )

    return redirect_response


@router.post("/login")
async def login(req: LoginRequest, response: Response):
    """Login and create session."""

    logger.info(f"Login attempt for user: {req.username}")

    user = get_user_by_username(req.username)

    if not user or not verify_password(req.password, user.password_hash):
        logger.warning(f"Failed login attempt for: {req.username}")
        raise HTTPException(401, detail="Invalid username or password")

    if not user.is_active:
        logger.warning(f"Login attempt for disabled account: {req.username}")
        raise HTTPException(403, detail="Account is disabled")

    if cfg.auth.require_email_verification and not user.email_verified:
        logger.warning(f"Login attempt with unverified email: {req.username}")
        raise HTTPException(403, detail="Please verify your email first")

    # Create session
    session_id = generate_token()
    session_secret = generate_session_secret()

    now = datetime.now(timezone.utc)
    expires_at = next_expiry(now, now)
    create_session(
        session_id=session_id,
        user=user,
        secret=session_secret,
        expires_at=expires_at,
    )

    # Set cookie
    set_session_cookie(response, session_id, expires_at, now)

    logger.success(f"User logged in: {user.username} (session={session_id[:8]}...)")

    return {
        "success": True,
        "message": "Logged in successfully",
        "username": user.username,
        "session_secret": session_secret,
    }


@router.post("/logout")
async def logout(response: Response, user: User = Depends(get_current_user)):
    """Logout and destroy session."""

    logger.info(f"Logout request for user: {user.username}")

    # Delete all user sessions
    deleted_count = Session.delete().where(Session.user == user).execute()

    # Clear cookie
    response.delete_cookie(key="session_id")

    logger.success(
        f"User logged out: {user.username} ({deleted_count} session(s) deleted)"
    )

    return {"success": True, "message": "Logged out successfully"}


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session_id: str | None = Cookie(None),
):
    """Change the password of the signed-in account. Every other session ends; access tokens keep working."""

    if not verify_password(req.current_password, user.password_hash):
        logger.warning(f"Password change refused (wrong current password): {user.username}")
        raise HTTPException(403, detail="Current password is incorrect")

    with db.atomic():
        update_user(user, password_hash=hash_password(req.new_password))
        others = Session.delete().where(Session.user == user)
        if session_id:
            others = others.where(Session.session_id != session_id)
        ended = others.execute()
        # a link that was waiting was made for an account whose password has just changed
        revoke_links(user)

    logger.success(f"Password changed: {user.username} ({ended} other session(s) ended)")
    return {"success": True, "message": "Password changed"}


def _valid_reset_link(token: str):
    """The row and the account a reset link opens, or the one refusal every bad link gets."""
    row = find_valid(token)
    user = row.user if row else None
    if user is None or not user.is_active:
        raise HTTPException(400, detail=INVALID_RESET_LINK)
    return row, user


@router.post("/password-reset/check")
async def check_password_reset_link(req: PasswordResetCheckRequest):
    """What the reset page asks first: whose account does this link open, and until when."""

    row, user = _valid_reset_link(req.token)
    return {"username": user.username, "expires_at": iso(row.expires_at)}


@router.post("/password-reset")
async def reset_password(req: PasswordResetRequest):
    """Set a new password with a link an administrator handed over. It signs every session out and nobody in."""

    row, user = _valid_reset_link(req.token)

    with db.atomic():
        # the link is used up first: of two requests with the same link only one gets past this
        if not claim(row):
            raise HTTPException(400, detail=INVALID_RESET_LINK)
        update_user(user, password_hash=hash_password(req.new_password))
        ended = Session.delete().where(Session.user == user).execute()

    logger.success(f"Password reset with a link: {user.username} ({ended} session(s) ended)")
    return {"success": True, "message": "Password changed", "username": user.username}


@router.get("/me")
def get_me(user: User = Depends(get_current_user)):
    """Get current user info (internal endpoint)."""

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "email_verified": user.email_verified,
        "created_at": safe_isoformat(user.created_at),
    }


@router.get("/tokens")
async def list_tokens(user: User = Depends(get_current_user)):
    """List user's API tokens."""

    tokens = list_user_tokens(user)

    return {
        "tokens": [
            {
                "id": t.id,
                "name": t.name,
                "last_used": safe_isoformat(t.last_used) if t.last_used else None,
                "created_at": safe_isoformat(t.created_at),
            }
            for t in tokens
        ]
    }


@router.post("/tokens/create")
async def create_token_endpoint(
    req: CreateTokenRequest, user: User = Depends(get_current_user)
):
    """Create new API token."""

    # Generate token
    token_str = generate_token()
    token_hash_val = hash_token(token_str)

    # Save to database
    token = create_token(user=user, token_hash=token_hash_val, name=req.name)

    # Get session secret for encryption (if in web session)
    session = Session.get_or_none(Session.user == user)
    session_secret = session.secret if session else None

    return {
        "success": True,
        "token": token_str,
        "token_id": token.id,
        "session_secret": session_secret,
        "message": "Token created. Save it securely - you won't see it again!",
    }


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: int, user: User = Depends(get_current_user)):
    """Revoke an API token."""

    token = Token.get_or_none((Token.id == token_id) & (Token.user == user))

    if not token:
        raise HTTPException(404, detail="Token not found")

    delete_token(token.id)

    return {"success": True, "message": "Token revoked successfully"}
