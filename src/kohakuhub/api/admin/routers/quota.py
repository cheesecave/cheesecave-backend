"""Quota management endpoints for admin API."""

from fastapi import APIRouter, Depends, HTTPException
from peewee import fn
from pydantic import BaseModel

from kohakuhub import usage
from kohakuhub.db import Repository, User
from kohakuhub.logger import get_logger
from kohakuhub.api.admin.utils import verify_admin_token
from kohakuhub.api.quota.util import (
    get_storage_info,
    set_quota,
)
from kohakuhub.usage import namespace_usage

logger = get_logger("ADMIN")
router = APIRouter()


# ===== Models =====

# NOTE: Route order matters! Specific routes like /quota/overview must come
# BEFORE parameterized routes like /quota/{namespace} to avoid conflicts.


class SetQuotaRequest(BaseModel):
    """Request to set quota."""

    private_quota_bytes: int | None = None
    public_quota_bytes: int | None = None


# ===== Endpoints =====

# IMPORTANT: /quota/overview must come BEFORE /quota/{namespace}
# to avoid "overview" being interpreted as a namespace parameter!


@router.get("/quota/overview")
async def get_quota_overview(
    _admin: bool = Depends(verify_admin_token),
):
    """Get system-wide quota overview with warnings.

    Args:
        _admin: Admin authentication (dependency)

    Returns:
        Users/repos over quota, top consumers, system totals
    """

    R = Repository
    # Users over quota: only a user with a quota can be
    limited = list(
        User.select().where(
            (User.is_org == False)
            & (
                User.private_quota_bytes.is_null(False)
                | User.public_quota_bytes.is_null(False)
            )
        )
    )
    used = namespace_usage(user.username for user in limited)
    users_over = []
    for user in limited:
        private_used = used[user.username]["private"]
        public_used = used[user.username]["public"]
        private_pct = (
            (private_used / user.private_quota_bytes * 100)
            if user.private_quota_bytes
            else 0
        )
        public_pct = (
            (public_used / user.public_quota_bytes * 100)
            if user.public_quota_bytes
            else 0
        )

        if private_pct > 100 or public_pct > 100:
            users_over.append(
                {
                    "username": user.username,
                    "private_percentage": round(private_pct, 1),
                    "public_percentage": round(public_pct, 1),
                    "private_used": private_used,
                    "private_quota": user.private_quota_bytes,
                    "public_used": public_used,
                    "public_quota": user.public_quota_bytes,
                }
            )

    # Repos over quota: their own quota, or else their account's
    repos_over = []
    for repo in R.select(R, User).join(User, on=(R.owner == User.id)):
        quota = repo.quota_bytes
        if quota is None:
            quota = (
                repo.owner.private_quota_bytes
                if repo.private
                else repo.owner.public_quota_bytes
            )
        if quota and repo.used_bytes > quota:
            repos_over.append(
                {
                    "full_id": repo.full_id,
                    "repo_type": repo.repo_type,
                    "used_bytes": repo.used_bytes,
                    "quota_bytes": repo.quota_bytes,
                    "percentage": round(repo.used_bytes / quota * 100, 1),
                }
            )

    # Top consumers (users + orgs by total storage)
    total = fn.SUM(R.used_bytes)
    top = list(
        R.select(R.namespace, total.alias("total"))
        .group_by(R.namespace)
        .order_by(total.desc())
        .limit(10)
        .tuples()
    )
    orgs = {
        username
        for (username,) in User.select(User.username)
        .where(
            User.username.in_([namespace for namespace, _ in top])
            & (User.is_org == True)
        )
        .tuples()
    }
    top_consumers = [
        {
            "username": namespace,
            "is_org": namespace in orgs,
            "total_bytes": int(total_bytes),
        }
        for namespace, total_bytes in top
    ]

    # System totals (users' repositories, as before)
    users = usage.users_usage()
    total_private, total_public = users["private"], users["public"]
    total_lfs = R.select(fn.COALESCE(fn.SUM(R.lfs_bytes), 0)).scalar()

    return {
        "users_over_quota": users_over,
        "repos_over_quota": repos_over,
        "top_consumers": top_consumers,
        "system_storage": {
            "private_used": total_private,
            "public_used": total_public,
            "lfs_used": total_lfs,
            "total_used": total_private + total_public,
        },
    }


@router.get("/quota/{namespace}")
async def get_quota_admin(
    namespace: str,
    is_org: bool = False,
    _admin: bool = Depends(verify_admin_token),
):
    """Get storage quota information for a user or organization.

    Args:
        namespace: Username or organization name
        is_org: True if namespace is an organization
        _admin: Admin authentication (dependency)

    Returns:
        Quota information

    Raises:
        HTTPException: If namespace not found
    """

    # Check if namespace exists
    if is_org:
        entity = User.get_or_none((User.username == namespace) & (User.is_org == True))
    else:
        entity = User.get_or_none((User.username == namespace) & (User.is_org == False))

    if not entity:
        raise HTTPException(
            404,
            detail={
                "error": f"{'Organization' if is_org else 'User'} not found: {namespace}"
            },
        )

    info = get_storage_info(namespace, is_org)

    return {
        "namespace": namespace,
        "is_organization": is_org,
        **info,
    }


@router.put("/quota/{namespace}")
async def set_quota_admin(
    namespace: str,
    request: SetQuotaRequest,
    is_org: bool = False,
    _admin: bool = Depends(verify_admin_token),
):
    """Set storage quota for a user or organization (admin only).

    Args:
        namespace: Username or organization name
        request: Quota settings
        is_org: True if namespace is an organization
        _admin: Admin authentication (dependency)

    Returns:
        Updated quota information

    Raises:
        HTTPException: If namespace not found
    """

    # Check if namespace exists
    if is_org:
        entity = User.get_or_none((User.username == namespace) & (User.is_org == True))
    else:
        entity = User.get_or_none((User.username == namespace) & (User.is_org == False))

    if not entity:
        raise HTTPException(
            404,
            detail={
                "error": f"{'Organization' if is_org else 'User'} not found: {namespace}"
            },
        )

    info = set_quota(
        namespace,
        private_quota_bytes=request.private_quota_bytes,
        public_quota_bytes=request.public_quota_bytes,
        is_org=is_org,
    )

    logger.info(
        f"Admin set quota for {'org' if is_org else 'user'} {namespace}: "
        f"private={request.private_quota_bytes}, public={request.public_quota_bytes}"
    )

    return {
        "namespace": namespace,
        "is_organization": is_org,
        **info,
    }


@router.post("/quota/{namespace}/recalculate")
async def recalculate_quota_admin(
    namespace: str,
    is_org: bool = False,
    _admin: bool = Depends(verify_admin_token),
):
    """Recount the storage usage of a user's or organization's repositories (admin only).

    Schedules the ``usage.recount`` task for the namespace; the answer is the
    usage as it stands (kept up to date as repositories change).

    Args:
        namespace: Username or organization name
        is_org: True if namespace is an organization
        _admin: Admin authentication (dependency)

    Returns:
        Updated quota information

    Raises:
        HTTPException: If namespace not found
    """

    # Check if namespace exists
    if is_org:
        entity = User.get_or_none((User.username == namespace) & (User.is_org == True))
    else:
        entity = User.get_or_none((User.username == namespace) & (User.is_org == False))

    if not entity:
        raise HTTPException(
            404,
            detail={
                "error": f"{'Organization' if is_org else 'User'} not found: {namespace}"
            },
        )

    logger.info(
        f"Admin recalculating storage for {'org' if is_org else 'user'} {namespace}"
    )

    task_id = usage.enqueue_recount(namespace)
    info = get_storage_info(namespace, is_org)

    return {
        "namespace": namespace,
        "is_organization": is_org,
        "task_id": task_id,
        "already_pending": task_id is None,
        **info,
    }


@router.post("/repositories/recalculate-all")
async def recalculate_all_repo_storage_admin(
    namespace: str | None = None,
    _admin: bool = Depends(verify_admin_token),
):
    """Recount the storage usage of every repository, or one namespace's (admin only).

    Schedules the ``usage.recount`` background task, which reports how far
    the kept usage had drifted (``GET /admin/api/usage/recount``).
    """
    task_id = usage.enqueue_recount(namespace)
    logger.info(f"Admin started a usage recount of {namespace or 'every repository'}")
    return {"task_id": task_id, "already_pending": task_id is None}


@router.get("/usage/recount")
async def get_usage_recount(_admin: bool = Depends(verify_admin_token)):
    """The latest site-wide usage recount: its progress and drift report."""
    return usage.recount_status()


@router.post("/usage/recount")
async def start_usage_recount(_admin: bool = Depends(verify_admin_token)):
    """Recount every repository's storage usage and report the drift. One runs at a time."""
    task_id = usage.enqueue_recount()
    logger.info("Admin started the usage recount")
    return {"task_id": task_id, "already_pending": task_id is None}
