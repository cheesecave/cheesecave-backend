"""Authentication API routes."""

import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from peewee import Case, IntegrityError, SqliteDatabase, fn

from kohakuhub import repository_invites
from kohakuhub.config import cfg
from kohakuhub.db import Invitation, Repository, Session, Token, User, db, utcnow
from kohakuhub.db_operations import (
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
    update_user,
)
from kohakuhub.logger import get_logger
from kohakuhub.api.validation import RESERVED_NAMES
from kohakuhub.auth.dependencies import get_current_user
from kohakuhub.auth.session import next_expiry, set_session_cookie
from kohakuhub.auth.email import safe_repository_return_path, send_verification_email
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
    invitation_code: str | None = Field(default=None, max_length=256, strict=True)
    repo_invitation_token: str | None = None
    repo_guest_repository_id: int | None = Field(default=None, gt=0, le=2**63 - 1, strict=True)


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


def _consume_site_invitation(invitation_token, user):
    """Consume a supplied registration invitation inside the account transaction."""
    invitation = get_invitation(invitation_token)
    if invitation is None or invitation.action != "register_account":
        raise HTTPException(400, detail="Invalid or unavailable invitation code")
    try:
        parameters = json.loads(invitation.parameters)
        if not isinstance(parameters, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, detail="Invalid or unavailable invitation code") from None
    available = (
        (Invitation.max_usage == -1)
        | (Invitation.max_usage.is_null() & Invitation.used_at.is_null())
        | (Invitation.usage_count < Invitation.max_usage)
    )
    now = utcnow()
    consumed = Invitation.update(
        usage_count=Invitation.usage_count + 1,
        used_at=fn.COALESCE(Invitation.used_at, now),
        used_by=Case(None, [(Invitation.used_at.is_null(), user.id)], Invitation.used_by),
    ).where(
        (Invitation.id == invitation.id) & (Invitation.expires_at > now) & available
    ).execute()
    if consumed != 1:
        raise HTTPException(400, detail="Invalid or unavailable invitation code")
    if org_id := parameters.get("org_id"):
        org = get_user_by_id(org_id)
        if org:
            create_user_organization(user, org, parameters.get("role", "member"))


def _registration_invitations(req, invitation_token):
    """Resolve generic inputs by stored identity; client labels never grant eligibility."""
    repo_token = req.repo_invitation_token
    if repo_token is not None:
        repo_token = repo_token.strip()
        if not repo_token or len(repo_token) > 256:
            raise HTTPException(400, detail="Invalid or unavailable invitation code")
    site_token = None
    for supplied in (req.invitation_code, invitation_token):
        if supplied is None or not supplied.strip():
            continue
        code = supplied.strip()
        if len(code) > 256:
            raise HTTPException(400, detail="Invalid or unavailable invitation code")
        site = get_invitation(code)
        repository = repository_invites.find_invite(code)
        if site is not None and repository is not None:
            raise HTTPException(400, detail="Invalid or unavailable invitation code")
        if site is not None:
            if site.action != "register_account" or (site_token and site_token != code):
                raise HTTPException(400, detail="Invalid or unavailable invitation code")
            site_token = code
        elif repository is not None:
            if repo_token and repo_token != code:
                raise HTTPException(400, detail="Invalid or unavailable invitation code")
            repo_token = code
        else:
            raise HTTPException(400, detail="Invalid or unavailable invitation code")
    return site_token, repo_token


@router.post("/register")
async def register(req: RegisterRequest, invitation_token: str | None = None, request: Request = None):
    """Register new user.

    Args:
        req: Registration request data
        invitation_token: Optional invitation token (required if invitation_only mode)

    Returns:
        Success response
    """
    invitation_token, repo_token = _registration_invitations(req, invitation_token)
    repository_url = None
    guest_repo_id = req.repo_guest_repository_id
    if repo_token and guest_repo_id is not None:
        raise HTTPException(400, detail="Invalid or unavailable invitation code")
    has_repository_invitation = bool(repo_token) or guest_repo_id is not None

    # Repository access never substitutes for the site's registration policy.
    if cfg.auth.invitation_only and not invitation_token:
        raise HTTPException(403, detail="Registration requires a valid invitation code")

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
    transaction_args = ("IMMEDIATE",) if (has_repository_invitation or invitation_token) and isinstance(db, SqliteDatabase) and db.transaction_depth() == 0 else ()
    with db.atomic(*transaction_args):
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
        try:
            with db.atomic():
                user = create_user(
                    username=req.username,
                    email=req.email,
                    password_hash=hash_password(req.password),
                    email_verified=not cfg.auth.require_email_verification,
                )
        except IntegrityError:
            raise HTTPException(400, detail="Username or email already exists") from None
        if invitation_token:
            _consume_site_invitation(invitation_token, user)
        if has_repository_invitation:
            if guest_repo_id is not None:
                repository = Repository.get_or_none(Repository.id == guest_repo_id)
                guest_secret = request.cookies.get(f"repo_guest_{guest_repo_id}", "") if request else ""
                if repository is None:
                    raise HTTPException(400, detail="Invalid or unavailable invitation code")
            else:
                row = repository_invites.find_invite(repo_token)
                if row is None:
                    raise HTTPException(400, detail="Invalid or unavailable invitation code")
                repository = row.repository
            try:
                if guest_repo_id is not None:
                    repository_invites.redeem_guest_account(repository, guest_secret, user)
                else:
                    repository_invites.redeem_account(repository, repo_token, user)
            except repository_invites.RepositoryInviteError as error:
                raise HTTPException(400, detail={"error": "Invalid or unavailable invitation code", "code": error.code}) from None
            if repository.repo_type in ("model", "dataset", "space"):
                repository_url = safe_repository_return_path(
                    f"/{repository.repo_type}s/{quote(repository.namespace, safe='')}/{quote(repository.name, safe='')}"
                )
            if repository_url is None:
                raise HTTPException(400, detail="Invalid or unavailable invitation code")

    logger.success(f"User registered: {user.username} (id={user.id})")

    # Send verification email if required
    repository_result = {"repository_url": repository_url} if repository_url else {}
    if cfg.auth.require_email_verification:
        token = generate_token()
        create_email_verification(
            user=user, token=token, expires_at=get_expiry_time(24)
        )

        email_args = (req.email, req.username, token)
        if repository_url:
            email_args += (repository_url,)
        verification_email = await asyncio.to_thread(send_verification_email, *email_args)
        if not verification_email:
            return {
                "success": True,
                "message": "User created but failed to send verification email",
                "email_verified": False,
                **repository_result,
            }

        return {
            "success": True,
            "message": "User created. Please check your email to verify your account.",
            "email_verified": False,
            **repository_result,
        }

    return {
        "success": True,
        "message": "User created successfully",
        "email_verified": True,
        **repository_result,
    }


@router.get("/verify-email")
async def verify_email(token: str, response: Response, return_to: str | None = None):
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
    target = safe_repository_return_path(return_to) or f"/{user.username}"
    redirect_response = RedirectResponse(url=target, status_code=302)
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
