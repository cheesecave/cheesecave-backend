"""Invitation API endpoints."""

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from kohakuhub.config import cfg
from kohakuhub.constants import (
    ERROR_INVALID_INVITATION_DATA,
    ERROR_INVITATION_NOT_FOUND,
    ERROR_ORG_NOT_FOUND,
)
from kohakuhub.db import Invitation, User, UserOrganization, db
from kohakuhub.db_operations import (
    check_invitation_available,
    create_invitation,
    create_user_organization,
    delete_invitation,
    get_invitation,
    get_organization,
    get_user_by_email,
    get_user_by_id,
    get_user_organization,
    list_org_invitations,
    mark_invitation_used,
)
from kohakuhub.logger import get_logger
from kohakuhub.auth.dependencies import get_current_user
from kohakuhub.auth.email import send_org_invitation_email

logger = get_logger("INVITATION")

router = APIRouter(prefix="/invitations", tags=["invitations"])


class CreateOrgInvitationRequest(BaseModel):
    """Request to create organization invitation."""

    email: Optional[EmailStr] = None  # Optional for reusable invitations
    role: str = "member"  # visitor, member, admin
    max_usage: Optional[int] = None  # None=one-time, -1=unlimited, N=max N uses
    expires_days: int = 7  # Days until expiration (default 7)


class InvitationResponse(BaseModel):
    """Invitation details response."""

    token: str
    action: str
    org_name: Optional[str] = None
    role: Optional[str] = None
    inviter_username: Optional[str] = None
    expires_at: str
    used_at: Optional[str] = None


@router.post("/org/{org_name}/create")
async def create_org_invitation(
    org_name: str,
    req: CreateOrgInvitationRequest,
    user: User = Depends(get_current_user),
):
    """Create invitation to join an organization.

    Args:
        org_name: Organization name
        req: Invitation details (email, role, max_usage, expires_days)
        user: Current authenticated user

    Returns:
        Invitation token and link
    """
    # Validate role
    if req.role not in ["visitor", "member", "admin"]:
        raise HTTPException(
            400, detail="Invalid role. Must be 'visitor', 'member', or 'admin'"
        )

    # Get organization
    org = get_organization(org_name)
    if not org:
        raise HTTPException(404, detail=ERROR_ORG_NOT_FOUND)

    # Check if user is admin of the organization
    user_org = get_user_organization(user, org)
    if not user_org or user_org.role not in ["admin", "super-admin"]:
        raise HTTPException(
            403, detail="Not authorized to invite members to this organization"
        )

    # For email-specific invitations, check if user is already a member
    if req.email:
        invitee_user = get_user_by_email(req.email)
        if invitee_user:
            existing_membership = get_user_organization(invitee_user, org)
            if existing_membership:
                raise HTTPException(
                    400, detail="User is already a member of this organization"
                )

    # Generate invitation token
    token = secrets.token_urlsafe(32)

    # Set expiration based on expires_days parameter
    expires_at = datetime.now(timezone.utc) + timedelta(days=req.expires_days)

    # Create invitation parameters
    parameters = json.dumps(
        {
            "org_id": org.id,
            "org_name": org.username,
            "role": req.role,
            "email": req.email,  # May be None for reusable invitations
        }
    )

    # Create invitation atomically
    with db.atomic():
        invitation = create_invitation(
            token=token,
            action="join_org",
            parameters=parameters,
            created_by=user,
            expires_at=expires_at,
            max_usage=req.max_usage,
        )

    # Send email if SMTP enabled and email provided (async)
    if req.email:
        await asyncio.to_thread(
            send_org_invitation_email,
            req.email,
            org.username,
            user.username,
            token,
            req.role,
        )

    # Determine invitation type for logging
    invite_type = "reusable" if req.max_usage else "single-use"
    if req.email:
        logger.success(
            f"Invitation created by {user.username} for {req.email} to join {org.username} as {req.role}"
        )
    else:
        logger.success(
            f"{invite_type.capitalize()} invitation created by {user.username} for {org.username} as {req.role}"
        )

    invitation_link = f"{cfg.app.base_url}/invite/{token}"

    return {
        "success": True,
        "token": token,
        "invitation_link": invitation_link,
        "expires_at": expires_at.isoformat(),
        "max_usage": req.max_usage,
        "is_reusable": req.max_usage is not None,
    }


@router.get("/{token}")
async def get_invitation_details(token: str):
    """Get invitation details without accepting it.

    Args:
        token: Invitation token

    Returns:
        Invitation details
    """
    invitation = get_invitation(token)

    if not invitation:
        raise HTTPException(404, detail=ERROR_INVITATION_NOT_FOUND)

    # Check availability
    is_available, error_msg = check_invitation_available(invitation)
    is_expired = "expired" in (error_msg or "").lower() if not is_available else False

    # Parse parameters
    try:
        params = json.loads(invitation.parameters)
    except json.JSONDecodeError:
        raise HTTPException(500, detail=ERROR_INVALID_INVITATION_DATA)

    # Get inviter username (created_by is now a ForeignKey to User)
    inviter_username = (
        invitation.created_by.username if invitation.created_by else "Unknown"
    )

    response = {
        "action": invitation.action,
        "inviter_username": inviter_username,
        "expires_at": invitation.expires_at.isoformat(),
        "is_expired": is_expired,
        "is_available": is_available,
        "error_message": error_msg,
        "max_usage": invitation.max_usage,
        "usage_count": invitation.usage_count,
        "is_reusable": invitation.max_usage is not None,
    }

    # Add action-specific details
    if invitation.action == "join_org":
        response["org_name"] = params.get("org_name")
        response["role"] = params.get("role")
        response["email"] = params.get("email")

    return response


def _handle_join_org_action(invitation: Invitation, user: User, params: dict) -> dict:
    """Handle join_org invitation action.

    Args:
        invitation: Invitation object
        user: User accepting invitation
        params: Parsed invitation parameters

    Returns:
        Success response dict
    """
    org_id = params.get("org_id")
    role = params.get("role", "member")

    # Get organization object
    org = get_user_by_id(org_id)
    if not org:
        raise HTTPException(404, detail=ERROR_ORG_NOT_FOUND)

    # Check if user is already a member
    existing_membership = get_user_organization(user, org)
    if existing_membership:
        raise HTTPException(400, detail="You are already a member of this organization")

    # Add user to organization
    with db.atomic():
        create_user_organization(user, org, role)
        mark_invitation_used(invitation, user)

    org_name = params.get("org_name", "the organization")
    logger.success(
        f"User {user.username} accepted invitation to join {org_name} as {role}"
    )

    return {
        "success": True,
        "message": f"You have successfully joined {org_name} as a {role}",
        "org_name": org_name,
        "role": role,
    }


def _handle_register_account_action(
    invitation: Invitation, user: User, params: dict
) -> dict:
    """Handle register_account invitation action.

    Args:
        invitation: Invitation object
        user: User accepting invitation (already registered)
        params: Parsed invitation parameters

    Returns:
        Success response dict
    """
    # For register invitations, mark as used and optionally add to org
    with db.atomic():
        mark_invitation_used(invitation, user)

        # If invitation includes org membership, add user to org
        org_id = params.get("org_id")
        if org_id:
            org = get_user_by_id(org_id)
            if org:
                role = params.get("role", "member")
                existing_membership = get_user_organization(user, org)
                if not existing_membership:
                    create_user_organization(user, org, role)
                    org_name = params.get("org_name", "the organization")
                    logger.success(
                        f"User {user.username} registered via invitation and joined {org_name} as {role}"
                    )

    logger.success(f"User {user.username} used registration invitation")

    return {
        "success": True,
        "message": "Account registration invitation accepted",
    }


@router.post("/{token}/accept")
async def accept_invitation(token: str, user: User = Depends(get_current_user)):
    """Accept an invitation.

    Args:
        token: Invitation token
        user: Current authenticated user

    Returns:
        Success message
    """
    invitation = get_invitation(token)

    if not invitation:
        raise HTTPException(404, detail=ERROR_INVITATION_NOT_FOUND)

    # Check if invitation is available
    is_available, error_msg = check_invitation_available(invitation)
    if not is_available:
        raise HTTPException(400, detail=error_msg)

    # Parse parameters
    try:
        params = json.loads(invitation.parameters)
    except json.JSONDecodeError:
        raise HTTPException(500, detail=ERROR_INVALID_INVITATION_DATA)

    # Execute action based on type using match-case
    match invitation.action:
        case "join_org":
            return _handle_join_org_action(invitation, user, params)
        case "register_account":
            return _handle_register_account_action(invitation, user, params)
        case _:
            raise HTTPException(
                400, detail=f"Unknown invitation action: {invitation.action}"
            )


@router.get("/org/{org_name}/list")
async def list_organization_invitations(
    org_name: str, user: User = Depends(get_current_user)
):
    """List pending invitations for an organization (admin only).

    Args:
        org_name: Organization name
        user: Current authenticated user

    Returns:
        List of invitations
    """
    # Get organization
    org = get_organization(org_name)
    if not org:
        raise HTTPException(404, detail=ERROR_ORG_NOT_FOUND)

    # Check if user is admin of the organization
    user_org = get_user_organization(user, org)
    if not user_org or user_org.role not in ["admin", "super-admin"]:
        raise HTTPException(403, detail="Not authorized to view invitations")

    # Get all invitations for this organization
    invitations = list_org_invitations(org)

    # Format response
    result = []
    for inv in invitations:
        try:
            params = json.loads(inv.parameters)
            # created_by is now a ForeignKey to User
            inviter_username = inv.created_by.username if inv.created_by else "Unknown"

            # Check if invitation is still available
            is_available, _ = check_invitation_available(inv)

            result.append(
                {
                    "id": inv.id,
                    "token": inv.token,
                    "email": params.get("email"),
                    "role": params.get("role"),
                    "created_by": inviter_username,
                    "created_at": inv.created_at.isoformat(),
                    "expires_at": inv.expires_at.isoformat(),
                    "max_usage": inv.max_usage,
                    "usage_count": inv.usage_count,
                    "is_reusable": inv.max_usage is not None,
                    "is_available": is_available,
                    "used_at": inv.used_at.isoformat() if inv.used_at else None,
                    "is_pending": is_available and inv.usage_count == 0,
                }
            )
        except (json.JSONDecodeError, KeyError):
            continue

    return {"invitations": result}


@router.delete("/{token}")
async def delete_invitation_endpoint(
    token: str, user: User = Depends(get_current_user)
):
    """Delete/cancel an invitation (admin only).

    Args:
        token: Invitation token
        user: Current authenticated user

    Returns:
        Success message
    """
    invitation = get_invitation(token)

    if not invitation:
        raise HTTPException(404, detail=ERROR_INVITATION_NOT_FOUND)

    # Parse parameters to get org_id
    try:
        params = json.loads(invitation.parameters)
    except json.JSONDecodeError:
        raise HTTPException(500, detail=ERROR_INVALID_INVITATION_DATA)

    # Check authorization based on action type
    if invitation.action == "join_org":
        org_id = params.get("org_id")
        org = get_user_by_id(org_id)
        if not org:
            raise HTTPException(404, detail=ERROR_ORG_NOT_FOUND)

        user_org = get_user_organization(user, org)

        if not user_org or user_org.role not in ["admin", "super-admin"]:
            raise HTTPException(403, detail="Not authorized to delete this invitation")
    elif invitation.created_by_id != user.id:
        # Other actions have no organization to check against: only the creator
        # (system-made invitations have none; the admin API manages those)
        raise HTTPException(403, detail="Not authorized to delete this invitation")

    # Delete invitation
    delete_invitation(invitation)

    logger.info(f"Invitation {token[:8]}... deleted by {user.username}")

    return {"success": True, "message": "Invitation deleted successfully"}
