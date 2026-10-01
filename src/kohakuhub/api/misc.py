"""Utility API endpoints for Kohaku Hub."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import yaml

from kohakuhub.config import cfg
from kohakuhub.db import User, UserOrganization
from kohakuhub.logger import get_logger
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.api.operation_capabilities import (
    get_repository_operation_capabilities,
)

logger = get_logger("UTILS")

router = APIRouter()


def _checkout_sha(start: Path) -> str | None:
    """The commit of the git checkout above ``start``, read from ``.git``."""
    for directory in (start, *start.parents):
        git = directory / ".git"
        if git.is_dir():
            head = (git / "HEAD").read_text().strip()
            if not head.startswith("ref: "):
                return head
            ref = head[5:]
            if (git / ref).is_file():
                return (git / ref).read_text().strip()
            packed = git / "packed-refs"
            for line in packed.read_text().splitlines() if packed.is_file() else []:
                sha, _, name = line.partition(" ")
                if name == ref:
                    return sha
            return None
    return None


def build_identity(start: Path | None = None) -> dict:
    """Which code this is: set by the image build (``--build-arg
    KOHAKU_HUB_GIT_SHA=...``), else read from the checkout it runs from."""
    return {
        "git_sha": os.environ.get("KOHAKU_HUB_GIT_SHA")
        or _checkout_sha(start or Path(__file__).resolve().parent),
        "build_time": os.environ.get("KOHAKU_HUB_BUILD_TIME") or None,
    }


@router.get("/version")
def get_version():
    """Get KohakuHub version and site information.

    This endpoint helps client libraries (like hfutils) detect if they're
    connecting to KohakuHub vs HuggingFace Hub.

    HuggingFace Hub returns 404 for this endpoint.
    KohakuHub returns site identification and version info.

    Returns:
        Site identification and version information
    """
    return {
        "api": "kohakuhub",
        "version": "0.0.1",
        "name": cfg.app.site_name,
        "build": build_identity(),
    }


@router.get("/site-config")
def get_site_config():
    """Get public site configuration.

    Returns public configuration settings that affect frontend behavior.

    Returns:
        Public site configuration
    """
    return {
        "site_name": cfg.app.site_name,
        "invitation_only": cfg.auth.invitation_only,
        "require_email_verification": cfg.auth.require_email_verification,
        "capabilities": {
            "repository_operations": get_repository_operation_capabilities(),
        },
    }


class ValidateYamlPayload(BaseModel):
    """Payload for YAML validation endpoint."""

    content: str
    repo_type: str = "model"


@router.post("/validate-yaml")
def validate_yaml(body: ValidateYamlPayload):
    """Validate YAML content (e.g., model card, dataset card).

    Args:
        body: Validation payload with YAML content

    Returns:
        Validation result
    """
    try:
        yaml.safe_load(body.content)
    except Exception as e:
        return {"valid": False}

    return {"valid": True}


@router.get("/whoami-v2")
def whoami_v2(user: User | None = Depends(get_optional_user)):
    """Get current user information (HuggingFace compatible).

    Matches HuggingFace Hub /api/whoami-v2 endpoint format.
    Returns user info if authenticated, 401 if not.
    """
    if not user:
        raise HTTPException(401, detail="Invalid user token")

    # Get user's organizations (organizations are User objects with is_org=True)
    user_orgs = (
        UserOrganization.select(UserOrganization, User)
        .join(User, on=(UserOrganization.organization == User.id))
        .where(UserOrganization.user == user)
    )

    orgs_list = []
    for uo in user_orgs:
        orgs_list.append(
            {
                "name": uo.organization.username,
                "fullname": uo.organization.username,
                "roleInOrg": uo.role,
            }
        )

    return {
        "type": "user",
        "id": str(user.id),
        "name": user.username,
        "fullname": user.username,
        "email": user.email,
        "emailVerified": user.email_verified,
        "canPay": False,
        "isPro": False,
        "orgs": orgs_list,
        "auth": {
            "type": "access_token",
            "accessToken": {"displayName": "Auto-generated token", "role": "write"},
        },
        # KohakuHub-specific fields
        "site": {
            "name": cfg.app.site_name,  # Configurable site name
            "api": "kohakuhub",  # Hardcoded API identifier
            "version": "0.0.1",  # Hardcoded version
        },
    }
