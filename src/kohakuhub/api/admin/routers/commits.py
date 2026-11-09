"""Commit history endpoints for admin API."""

from fastapi import APIRouter, Depends

from kohakuhub.db import Commit, Repository
from kohakuhub.logger import get_logger
from kohakuhub.api.admin.utils import verify_admin_token

logger = get_logger("ADMIN")
router = APIRouter()


@router.get("/commits")
async def list_commits_admin(
    repo_full_id: str | None = None,
    repo_type: str | None = None,
    username: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _admin: bool = Depends(verify_admin_token),
):
    """List commits with filters.

    Args:
        repo_full_id: Filter by repository full ID (every type sharing it, unless repo_type is given)
        repo_type: Restrict to model / dataset / space
        username: Filter by author username
        limit: Maximum number to return
        offset: Offset for pagination
        _admin: Admin authentication (dependency)

    Returns:
        List of commits
    """

    query = Commit.select()

    if repo_full_id:
        repos = Repository.select().where(Repository.full_id == repo_full_id)
        if repo_type:
            repos = repos.where(Repository.repo_type == repo_type)
        repos = list(repos)
        if not repos:
            # An unknown repository has no commits; ignoring the filter would list every commit.
            return {"commits": [], "limit": limit, "offset": offset}
        query = query.where(Commit.repository << repos)
    elif repo_type:
        query = query.where(Commit.repo_type == repo_type)
    if username:
        query = query.where(Commit.username == username)

    query = query.order_by(Commit.created_at.desc()).limit(limit).offset(offset)

    commits = []
    for commit in query:
        commits.append(
            {
                "id": commit.id,
                "commit_id": commit.commit_id,
                "repo_full_id": (
                    commit.repository.full_id if commit.repository else None
                ),
                "repo_type": commit.repo_type,
                "branch": commit.branch,
                "user_id": commit.author.id if commit.author else None,
                "username": commit.username,
                "message": commit.message,
                "description": commit.description,
                "created_at": commit.created_at.isoformat(),
            }
        )

    return {"commits": commits, "limit": limit, "offset": offset}
