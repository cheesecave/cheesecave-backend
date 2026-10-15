"""Name validation and conflict checking endpoints."""

import re

from fastapi import APIRouter
from pydantic import BaseModel

from kohakuhub.db import Repository, User
from kohakuhub.utils.names import normalize_name, repo_name_problem

router = APIRouter()

# Reserved names that cannot be used as usernames or organization names
RESERVED_NAMES = {
    # Route names
    "models",
    "datasets",
    "spaces",
    "admin",
    "organizations",
    "api",
    "org",
    "auth",
    # Common system names
    "settings",
    "new",
    "login",
    "register",
    "logout",
    "docs",
    "swagger",
    "health",
    "version",
    # Special paths
    "resolve",
    "tree",
    "blob",
    "commit",
    "commits",
    "branch",
    "branches",
    "tag",
    "tags",
    "upload",
    "edit",
    # Admin paths
    "fallback-sources",
    "cache",
    "stats",
    "quota",
}


class CheckNameRequest(BaseModel):
    """Request to check if name is available."""

    name: str
    namespace: str | None = None  # For repository names
    type: str | None = None  # For repository type (model/dataset/space)


class CheckNameResponse(BaseModel):
    """Response for name availability check."""

    available: bool
    normalized_name: str
    conflict_with: str | None = None  # What the name conflicts with
    message: str


@router.post("/api/validate/check-name")
async def check_name_availability(req: CheckNameRequest) -> CheckNameResponse:
    """Check if a username, organization name, or repository name is available.

    This endpoint checks for:
    - Exact matches
    - Normalized conflicts (e.g., 'My-Repo' conflicts with 'my_repo')

    Args:
        req: Name check request

    Returns:
        Availability information
    """
    name = req.name.strip()
    normalized = normalize_name(name)

    # Check if name is reserved (only for usernames/org names, not repos)
    if not (req.namespace and req.type):
        if name.lower() in RESERVED_NAMES or normalized in RESERVED_NAMES:
            return CheckNameResponse(
                available=False,
                normalized_name=normalized,
                conflict_with=name,
                message=f"Name '{name}' is reserved and cannot be used",
            )

    # Check repository name
    if req.namespace and req.type:
        if problem := repo_name_problem(name):
            return CheckNameResponse(
                available=False,
                normalized_name=normalized,
                conflict_with=None,
                message=f"Invalid repository name '{name[:100]}': {problem}",
            )
        # Check if repository exists with this name
        existing = Repository.get_or_none(
            (Repository.repo_type == req.type)
            & (Repository.namespace == req.namespace)
            & (Repository.name == name)
        )

        if existing:
            return CheckNameResponse(
                available=False,
                normalized_name=normalized,
                conflict_with=f"{req.namespace}/{name}",
                message=f"Repository {req.namespace}/{name} already exists",
            )

        # Check for normalized conflicts
        all_repos = Repository.select().where(
            (Repository.repo_type == req.type) & (Repository.namespace == req.namespace)
        )

        for repo in all_repos:
            if normalize_name(repo.name) == normalized:
                return CheckNameResponse(
                    available=False,
                    normalized_name=normalized,
                    conflict_with=f"{req.namespace}/{repo.name}",
                    message=f"Repository name conflicts with existing repository: {repo.name} (case-insensitive)",
                )

        return CheckNameResponse(
            available=True,
            normalized_name=normalized,
            message="Repository name is available",
        )

    # Check username
    existing_user = User.get_or_none(User.username == name)
    if existing_user:
        return CheckNameResponse(
            available=False,
            normalized_name=normalized,
            conflict_with=name,
            message=f"Username {name} is already taken",
        )

    # Check for normalized username conflicts
    all_users = User.select()
    for user in all_users:
        if normalize_name(user.username) == normalized:
            return CheckNameResponse(
                available=False,
                normalized_name=normalized,
                conflict_with=user.username,
                message=f"Username conflicts with existing user: {user.username} (case-insensitive)",
            )

    # Check organization name (now unified with User model using normalized_name for efficiency)
    # Use User.normalized_name for O(1) lookup instead of O(n) loop
    existing_org = User.get_or_none(
        (User.normalized_name == normalized) & (User.is_org == True)
    )
    if existing_org:
        return CheckNameResponse(
            available=False,
            normalized_name=normalized,
            conflict_with=existing_org.username,
            message=f"Name conflicts with existing organization: {existing_org.username} (case-insensitive)",
        )

    return CheckNameResponse(
        available=True, normalized_name=normalized, message="Name is available"
    )
