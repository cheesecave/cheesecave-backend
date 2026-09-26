"""Repository management endpoints for admin API."""

from fastapi import APIRouter, Depends, HTTPException
from peewee import fn

from kohakuhub.db import Commit, File, Repository
from kohakuhub.db_operations import get_file
from kohakuhub.logger import get_logger
from kohakuhub.api.admin.utils import verify_admin_token
from kohakuhub.api.quota.util import get_repo_storage_info
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo

logger = get_logger("ADMIN")
router = APIRouter()


@router.get("/repositories")
async def list_repositories_admin(
    search: str | None = None,
    repo_type: str | None = None,
    namespace: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _admin: bool = Depends(verify_admin_token),
):
    """List all repositories with filters and storage information.

    Args:
        search: Search by repository full_id or name (optional)
        repo_type: Filter by repository type (model/dataset/space)
        namespace: Filter by namespace
        limit: Maximum number to return
        offset: Offset for pagination
        _admin: Admin authentication (dependency)

    Returns:
        List of repositories with metadata and storage info
    """

    query = Repository.select()

    # Add search filter if provided
    if search:
        query = query.where(
            (Repository.full_id.contains(search)) | (Repository.name.contains(search))
        )

    if repo_type:
        query = query.where(Repository.repo_type == repo_type)
    if namespace:
        query = query.where(Repository.namespace == namespace)

    # Get total count before limiting
    total_count = query.count()

    query = query.order_by(Repository.created_at.desc()).limit(limit).offset(offset)

    repos = []
    for repo in query:
        # Get owner username (using FK)
        owner_username = repo.owner.username if repo.owner else "unknown"

        # Get storage info
        storage_info = get_repo_storage_info(repo)

        repos.append(
            {
                "id": repo.id,
                "repo_type": repo.repo_type,
                "namespace": repo.namespace,
                "name": repo.name,
                "full_id": repo.full_id,
                "private": repo.private,
                "owner_id": repo.owner.id if repo.owner else None,
                "owner_username": owner_username,
                "created_at": repo.created_at.isoformat(),
                # Storage information
                "quota_bytes": storage_info["quota_bytes"],
                "used_bytes": storage_info["used_bytes"],
                "percentage_used": storage_info["percentage_used"],
                "is_inheriting": storage_info["is_inheriting"],
            }
        )

    return {
        "repositories": repos,
        "total": total_count,
        "limit": limit,
        "offset": offset,
        "search": search,
    }


@router.get("/repositories/{repo_type}/{namespace}/{name}")
async def get_repository_admin(
    repo_type: str,
    namespace: str,
    name: str,
    _admin: bool = Depends(verify_admin_token),
):
    """Get detailed repository information.

    Args:
        repo_type: Repository type
        namespace: Repository namespace
        name: Repository name
        _admin: Admin authentication (dependency)

    Returns:
        Repository details including file count and commit count

    Raises:
        HTTPException: If repository not found
    """

    repo = Repository.get_or_none(
        Repository.repo_type == repo_type,
        Repository.namespace == namespace,
        Repository.name == name,
    )

    if not repo:
        raise HTTPException(
            404,
            detail={"error": f"Repository not found: {repo_type}/{namespace}/{name}"},
        )

    # Get owner (using FK)
    owner = repo.owner

    # Count active files only (using FK)
    file_count = (
        File.select()
        .where((File.repository == repo) & (File.is_deleted == False))
        .count()
    )

    # Count commits (using FK)
    commit_count = Commit.select().where(Commit.repository == repo).count()

    # Get total file size for active files only (using FK)
    total_size = (
        File.select(fn.SUM(File.size).alias("total"))
        .where((File.repository == repo) & (File.is_deleted == False))
        .scalar()
        or 0
    )

    # Get storage info
    storage_info = get_repo_storage_info(repo)

    return {
        "id": repo.id,
        "repo_type": repo.repo_type,
        "namespace": repo.namespace,
        "name": repo.name,
        "full_id": repo.full_id,
        # The LakeFS repository behind it; since #108 a renamed repository
        # keeps its original one, whose readable prefix shows the old name.
        "lakefs_repo": resolve_lakefs_repo(repo),
        "private": repo.private,
        "owner_id": owner.id if owner else None,
        "owner_username": owner.username if owner else "unknown",
        "created_at": repo.created_at.isoformat(),
        "file_count": file_count,
        "commit_count": commit_count,
        "total_size": total_size,
        # Storage information
        "quota_bytes": storage_info["quota_bytes"],
        "used_bytes": storage_info["used_bytes"],
        "percentage_used": storage_info["percentage_used"],
        "is_inheriting": storage_info["is_inheriting"],
    }


@router.get("/repositories/{repo_type}/{namespace}/{name}/files")
async def get_repository_files_admin(
    repo_type: str,
    namespace: str,
    name: str,
    ref: str = "main",
    _admin: bool = Depends(verify_admin_token),
):
    """Get enhanced file list with LFS metadata and version counts.

    Args:
        repo_type: Repository type
        namespace: Repository namespace
        name: Repository name
        ref: Branch or commit reference
        _admin: Admin authentication (dependency)

    Returns:
        File list with LFS info, version counts, and SHA256
    """

    repo = Repository.get_or_none(
        Repository.repo_type == repo_type,
        Repository.namespace == namespace,
        Repository.name == name,
    )

    if not repo:
        raise HTTPException(
            404,
            detail={"error": f"Repository not found: {repo_type}/{namespace}/{name}"},
        )

    # Get file tree from LakeFS
    lakefs_repo = resolve_lakefs_repo(repo)
    client = get_lakefs_client()

    try:
        response = await client.list_objects(
            repository=lakefs_repo,
            ref=ref,
            amount=1000,
        )

        files = []
        for obj in response.get("results", []):
            # Get file metadata from DB
            file_record = get_file(repo, obj["path"])

            # Count versions for this path
            version_count = (
                File.select()
                .where((File.repository == repo) & (File.path_in_repo == obj["path"]))
                .count()
            )

            files.append(
                {
                    "path": obj["path"],
                    "size": obj.get("size_bytes", 0),
                    "checksum": obj.get("checksum"),
                    "mtime": obj.get("mtime"),
                    "is_lfs": file_record.lfs if file_record else False,
                    "sha256": (
                        file_record.sha256 if file_record and file_record.lfs else None
                    ),
                    "version_count": version_count,
                }
            )

        return {"files": files, "ref": ref, "count": len(files)}

    except Exception as e:
        logger.error(f"Failed to list files for {repo.full_id}: {e}")
        raise HTTPException(500, detail={"error": "Failed to list repository files"})


@router.get("/repositories/{repo_type}/{namespace}/{name}/storage-breakdown")
async def get_repository_storage_breakdown(
    repo_type: str,
    namespace: str,
    name: str,
    _admin: bool = Depends(verify_admin_token),
):
    """Get storage analytics for repository.

    Args:
        repo_type: Repository type
        namespace: Repository namespace
        name: Repository name
        _admin: Admin authentication (dependency)

    Returns:
        Storage breakdown with regular vs LFS, deduplication stats
    """
    repo = Repository.get_or_none(
        Repository.repo_type == repo_type,
        Repository.namespace == namespace,
        Repository.name == name,
    )

    if not repo:
        raise HTTPException(
            404,
            detail={"error": f"Repository not found: {repo_type}/{namespace}/{name}"},
        )

    # Calculate regular vs LFS storage
    regular_size = (
        File.select(fn.SUM(File.size))
        .where(
            (File.repository == repo) & (File.is_deleted == False) & (File.lfs == False)
        )
        .scalar()
        or 0
    )

    lfs_size = (
        File.select(fn.SUM(File.size))
        .where(
            (File.repository == repo) & (File.is_deleted == False) & (File.lfs == True)
        )
        .scalar()
        or 0
    )

    lfs_object_count = (
        File.select()
        .where(
            (File.repository == repo) & (File.is_deleted == False) & (File.lfs == True)
        )
        .count()
    )

    # Count unique LFS objects
    unique_lfs = (
        File.select(File.sha256)
        .where(
            (File.repository == repo) & (File.is_deleted == False) & (File.lfs == True)
        )
        .distinct()
        .count()
    )

    # Calculate deduplication savings
    dedup_savings = 0
    if lfs_object_count > 0 and unique_lfs > 0:
        dedup_savings = lfs_size * (lfs_object_count - unique_lfs) / lfs_object_count

    return {
        "regular_files_size": regular_size,
        "lfs_files_size": lfs_size,
        "total_size": regular_size + lfs_size,
        "lfs_object_count": lfs_object_count,
        "unique_lfs_objects": unique_lfs,
        "deduplication_savings": int(dedup_savings),
    }
