"""Explicit browser invitation APIs; ordinary HF/Git/download authorization is unchanged."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

from kohakuhub import repository_invites as invites
from kohakuhub.auth.dependencies import get_current_user, get_current_user_or_admin, get_optional_user
from kohakuhub.auth.email import safe_repository_return_path
from kohakuhub.auth.permissions import RepoReadDeniedError, check_repo_manage_permission, check_repo_read_permission
from kohakuhub.db import (
    Repository, RepositoryAccessEvent, RepositoryAccessGrant, RepositoryAccessRequest,
    RepositoryGuestSession, RepositoryInvite, User, utcnow,
)
from kohakuhub.db_operations import get_repository, get_user_by_username
from kohakuhub.utils.lakefs import get_lakefs_client, lakefs_status, resolve_lakefs_repo

router = APIRouter()
RepoType = Literal["model", "dataset", "space"]
BASE = "/{repo_type}s/{namespace}/{name}/access"
MAX_INVITE_BYTES = 65536


def _iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _unavailable():
    return HTTPException(404, detail={"error": "Repository not found"}, headers={"X-Error-Code": "RepoNotFound"})


def _repo(repo_type, namespace, name):
    repo = get_repository(repo_type, namespace, name)
    if repo is None:
        raise _unavailable()
    return repo


def _readable(repo, user, is_admin=False):
    try:
        return check_repo_read_permission(repo, user, is_admin=is_admin)
    except RepoReadDeniedError:
        return False


def _code_repo(repo_type, namespace, name):
    repo = get_repository(repo_type, namespace, name)
    if repo is None:
        raise HTTPException(400, detail={"error": "Invalid or unavailable repository invitation.", "code": "invalid_invitation"})
    return repo


def _manager(repo, identity):
    user, is_admin = identity
    if not repo.invite_mode and not _readable(repo, user, is_admin):
        raise _unavailable()
    check_repo_manage_permission(repo, user, is_admin=is_admin)
    return user, is_admin


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except invites.RepositoryInviteError as error:
        raise HTTPException(400, detail={"error": str(error), "code": error.code}) from None


def _request_data(row):
    if row is None:
        return None
    return {"id": row.id, "user_id": row.user_id, "username": row.user.username if row.user_id else None,
            "email": row.email, "message": row.message, "status": row.status,
            "created_at": _iso(row.created_at), "reviewed_at": _iso(row.reviewed_at)}


def _grant_data(row):
    return {"id": row.id, "user_id": row.user_id, "username": row.user.username, "source": row.source,
            "created_at": _iso(row.created_at), "expires_at": _iso(row.expires_at), "revoked_at": _iso(row.revoked_at)}


def _invite_data(row):
    return {key: getattr(row, key) for key in ("id", "label", "max_redemptions", "redemption_count", "max_guest_sessions", "guest_session_count", "guest_ttl_seconds")} | {
        "created_at": _iso(row.created_at), "expires_at": _iso(row.expires_at),
        "grant_expires_at": _iso(row.grant_expires_at), "revoked_at": _iso(row.revoked_at),
        "request_id": row.access_request_id,
    }


def guest_cookie_name(repo):
    return f"repo_guest_{repo.id}"


def guest_cookie_path(repo):
    # Only these explicit browser endpoints consume this cookie. A guest is never a User.
    return "/"


def guest_for_request(repo, request):
    return invites.get_guest_session(repo, request.cookies.get(guest_cookie_name(repo), ""))


async def _invitation_markdown(repo):
    client = get_lakefs_client()
    source = resolve_lakefs_repo(repo)
    async def read():
        head = (await client.get_branch(repository=source, branch="main"))["commit_id"]
        if not head:
            return b""
        return await client.get_object_prefix(source, head, "INVITE.MD", MAX_INVITE_BYTES + 1)
    try:
        content = await asyncio.wait_for(read(), 5)
    except Exception as error:
        if lakefs_status(error) == 404:
            return None
        raise HTTPException(503, detail={"error": "Invitation instructions are temporarily unavailable"}) from None
    if len(content) > MAX_INVITE_BYTES or content.startswith(b"version https://git-lfs.github.com/spec/"):
        return None  # Do not follow LFS pointers or any resource referenced in the public instructions.
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


@router.get(BASE)
async def access_state(repo_type: RepoType, namespace: str, name: str, request: Request, response: Response,
                       user: User | None = Depends(get_optional_user)):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Cookie, Authorization"
    repo = get_repository(repo_type, namespace, name)
    if repo is None:
        # No local metadata exists: leave external fallback resolution to the ordinary route.
        return {"access": "standard", "visibility": None, "can_manage": False, "invite_markdown": None}
    try:
        check_repo_read_permission(repo, user)
    except RepoReadDeniedError:
        session = guest_for_request(repo, request)
        if session:
            registration = {"registration_repository_id": repo.id} if invites.guest_registration_available(session) else {}
            return {"access": "guest", "visibility": repo.visibility, "can_manage": False, "invite_markdown": None} | registration
        if not repo.invite_mode:
            # Match the nonexistent-local result. Ordinary loaders still apply
            # the masked HF denial and never fall through to an upstream source.
            return {"access": "standard", "visibility": None, "can_manage": False, "invite_markdown": None}
        return {"access": "invite", "visibility": "invite", "can_manage": False,
                "invite_markdown": await _invitation_markdown(repo)}
    can_manage = False
    if user:
        try:
            can_manage = check_repo_manage_permission(repo, user)
        except HTTPException:
            pass
    return {"access": "full", "visibility": repo.visibility, "can_manage": can_manage, "invite_markdown": None}


class ApplicationBody(BaseModel):
    email: EmailStr | None = None
    message: str = Field(default="", max_length=4000)


@router.post(BASE + "/requests")
def apply(repo_type: RepoType, namespace: str, name: str, body: ApplicationBody,
          response: Response, user: User | None = Depends(get_optional_user)):
    response.headers["Cache-Control"] = "no-store"
    repo = _repo(repo_type, namespace, name)
    if not repo.invite_mode and not _readable(repo, user):
        raise _unavailable()
    row, receipt = _call(invites.submit_request, repo, user,
                         email=str(body.email) if body.email else None, message=body.message)
    return {"request": _request_data(row), "receipt": receipt}


@router.get(BASE + "/request")
def my_request(repo_type: RepoType, namespace: str, name: str, response: Response,
               receipt: str | None = Header(None, alias="X-Repository-Request-Receipt", max_length=256),
               user: User | None = Depends(get_optional_user)):
    response.headers["Cache-Control"] = "no-store"
    repo = _repo(repo_type, namespace, name)
    row = _call(invites.get_request_status, repo, user, receipt=receipt)
    if row is None and not repo.invite_mode and not _readable(repo, user):
        raise _unavailable()
    return {"request": _request_data(row)}


@router.get(BASE + "/manage")
def manage(repo_type: RepoType, namespace: str, name: str, response: Response,
           limit: int = 100, offset: int = 0, identity=Depends(get_current_user_or_admin),
           section: Literal["requests", "invitations", "grants", "guests", "events"] | None = None):
    repo = _repo(repo_type, namespace, name)
    _manager(repo, identity)
    response.headers["Cache-Control"] = "no-store"
    if section is not None and (not 1 <= limit <= 100 or offset < 0):
        raise HTTPException(422, detail={"error": "Page size must be between 1 and 100 and offset cannot be negative"})
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    tables = {
        "requests": (RepositoryAccessRequest, _request_data),
        "invitations": (RepositoryInvite, _invite_data),
        "grants": (RepositoryAccessGrant, _grant_data),
        "guests": (RepositoryGuestSession, lambda row: {
            "id": row.id, "invite_id": row.invite_id, "created_at": _iso(row.created_at),
            "expires_at": _iso(row.expires_at), "revoked_at": _iso(row.revoked_at),
        }),
        "events": (RepositoryAccessEvent, lambda row: {
            "id": row.id, "action": row.action, "actor_id": row.actor_id,
            "subject_user_id": row.subject_user_id, "created_at": _iso(row.created_at),
            "detail": json.loads(row.detail),
        }),
    }
    result, totals = {}, {}
    for key in ([section] if section is not None else tables):
        model, serialize = tables[key]
        query = model.select().where(model.repository == repo.id)
        totals[key] = query.count()
        result[key] = [serialize(row) for row in query.order_by(model.id.desc()).offset(offset).limit(limit)]
    return result | {
        "totals": totals,
        "limit": limit, "offset": offset,
        "has_more": {key: total > offset + limit for key, total in totals.items()},
    }


class ReviewBody(BaseModel):
    approve: bool
    grant_expires_at: datetime | None = None


@router.post(BASE + "/requests/{request_id}/review")
def review(repo_type: RepoType, namespace: str, name: str, request_id: int, body: ReviewBody,
           identity=Depends(get_current_user_or_admin)):
    repo = _repo(repo_type, namespace, name)
    user, admin = _manager(repo, identity)
    row = _call(invites.review_request, repo, request_id, user, approve=body.approve,
                grant_expires_at=body.grant_expires_at, is_admin=admin)
    return {"request": _request_data(row)}


class InviteBody(BaseModel):
    expires_days: int = Field(default=7, ge=1, le=90)
    max_redemptions: int = Field(default=1, ge=1, le=1, strict=True, deprecated=True)
    max_guest_sessions: int = Field(default=1, ge=1, le=1, strict=True, deprecated=True)
    guest_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    grant_expires_at: datetime | None = None
    label: str = Field(default="", max_length=100)
    request_id: int | None = None


@router.post(BASE + "/invites")
def issue(repo_type: RepoType, namespace: str, name: str, body: InviteBody, response: Response,
          identity=Depends(get_current_user_or_admin)):
    repo = _repo(repo_type, namespace, name)
    user, admin = _manager(repo, identity)
    application = None
    if body.request_id is not None:
        application = RepositoryAccessRequest.get_or_none(
            (RepositoryAccessRequest.id == body.request_id) & (RepositoryAccessRequest.repository == repo.id)
        )
        if application is None:
            raise HTTPException(404, detail={"error": "Application not found"})
    row, token = _call(invites.issue_invite, repo, user, expires_at=utcnow() + timedelta(days=body.expires_days),
                       max_redemptions=body.max_redemptions, max_guest_sessions=body.max_guest_sessions,
                       guest_ttl_seconds=body.guest_ttl_seconds, grant_expires_at=body.grant_expires_at,
                       label=body.label, access_request=application, is_admin=admin)
    response.headers["Cache-Control"] = "no-store"
    return {"invitation": _invite_data(row), "token": token}


@router.post(BASE + "/invites/{invite_id}/revoke")
def revoke_invite(repo_type: RepoType, namespace: str, name: str, invite_id: int,
                  identity=Depends(get_current_user_or_admin)):
    repo = _repo(repo_type, namespace, name)
    user, admin = _manager(repo, identity)
    return {"invitation": _invite_data(_call(invites.revoke_invite, repo, invite_id, user, is_admin=admin))}


class GrantBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    expires_at: datetime | None = None


@router.post(BASE + "/grants")
def grant(repo_type: RepoType, namespace: str, name: str, body: GrantBody,
          identity=Depends(get_current_user_or_admin)):
    repo = _repo(repo_type, namespace, name)
    actor, admin = _manager(repo, identity)
    target = get_user_by_username(body.username)
    if target is None or target.is_org or not target.is_active:
        raise HTTPException(404, detail={"error": "Active account not found"})
    return {"grant": _grant_data(_call(invites.grant_account, repo, actor, target,
                                      expires_at=body.expires_at, is_admin=admin))}


@router.post(BASE + "/grants/{grant_id}/revoke")
def revoke_grant(repo_type: RepoType, namespace: str, name: str, grant_id: int,
                 identity=Depends(get_current_user_or_admin)):
    repo = _repo(repo_type, namespace, name)
    actor, admin = _manager(repo, identity)
    return {"grant": _grant_data(_call(invites.revoke_grant, repo, grant_id, actor, is_admin=admin))}


class CodeBody(BaseModel):
    token: str = Field(min_length=1, max_length=256)


@router.post("/repo-access/redeem")
def redeem_code(body: CodeBody, response: Response, user: User = Depends(get_current_user)):
    """Exchange a code only after normal login, then return its repository destination."""
    response.headers["Cache-Control"] = "no-store"
    token = body.token.strip()
    row = invites.find_invite(token)
    target = None
    if row is not None:
        repo = row.repository
        if repo.repo_type in ("model", "dataset", "space"):
            target = safe_repository_return_path(
                f"/{repo.repo_type}s/{quote(repo.namespace, safe='')}/{quote(repo.name, safe='')}"
            )
    if target is None:
        raise HTTPException(400, detail={"error": "Invalid or unavailable invitation code", "code": "invalid_invitation"},
                            headers={"Cache-Control": "no-store"})
    try:
        grant = invites.redeem_account(repo, token, user)
    except invites.RepositoryInviteError as error:
        raise HTTPException(400, detail={"error": "Invalid or unavailable invitation code", "code": error.code},
                            headers={"Cache-Control": "no-store"}) from None
    return {"success": True, "grant": _grant_data(grant), "repository_url": target}


@router.post(BASE + "/bind")
def bind(repo_type: RepoType, namespace: str, name: str, body: CodeBody,
         user: User = Depends(get_current_user)):
    return {"success": True, "grant": _grant_data(_call(invites.redeem_account, _code_repo(repo_type, namespace, name), body.token, user))}


@router.post(BASE + "/guest")
def guest_login(repo_type: RepoType, namespace: str, name: str, body: CodeBody, request: Request, response: Response):
    repo = _code_repo(repo_type, namespace, name)
    session, secret = _call(invites.redeem_guest, repo, body.token)
    response.set_cookie(guest_cookie_name(repo), secret, httponly=True, secure=request.url.scheme == "https",
                        samesite="lax", path=guest_cookie_path(repo), max_age=max(0, int((session.expires_at - utcnow()).total_seconds())))
    response.headers["Cache-Control"] = "no-store"
    return {"access": "guest", "expires_at": _iso(session.expires_at)}


@router.delete(BASE + "/guest")
def guest_logout(repo_type: RepoType, namespace: str, name: str, request: Request, response: Response):
    repo = get_repository(repo_type, namespace, name)
    session = guest_for_request(repo, request) if repo else None
    if session:
        RepositoryGuestSession.update(revoked_at=utcnow()).where(RepositoryGuestSession.id == session.id).execute()
        response.delete_cookie(guest_cookie_name(repo), path=guest_cookie_path(repo))
    response.headers["Cache-Control"] = "no-store"
    return {"success": True}


@router.post(BASE + "/guests/{guest_id}/revoke")
def revoke_guest(repo_type: RepoType, namespace: str, name: str, guest_id: int,
                 identity=Depends(get_current_user_or_admin)):
    repo = _repo(repo_type, namespace, name)
    actor, admin = _manager(repo, identity)
    _call(invites.revoke_guest_session, repo, guest_id, actor, is_admin=admin)
    return {"success": True}
