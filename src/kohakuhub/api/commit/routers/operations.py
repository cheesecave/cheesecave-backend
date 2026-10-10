"""Commit creation endpoint - Refactored version with smaller functions."""

from datetime import datetime, timezone
from enum import Enum
import asyncio
import base64
import hashlib
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from kohakuhub import path_commits, usage
from kohakuhub.config import cfg
from kohakuhub.db import File, Repository, User, db
from kohakuhub.db_operations import (
    create_commit,
    create_file,
    delete_file,
    get_effective_lfs_threshold,
    get_file,
    should_use_lfs,
    update_file,
)
from kohakuhub.logger import get_logger
from kohakuhub.auth.dependencies import get_current_user
from kohakuhub.auth.permissions import check_repo_write_permission
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo
from kohakuhub.utils.repo_paths import MAX_PATH_BYTES, too_long
from kohakuhub.utils.s3 import get_object_metadata, object_exists
from kohakuhub.api.repo.utils import operation_lock
from kohakuhub.api.repo.utils.gc import track_lfs_object
from kohakuhub.lfs_gc import (
    LfsObjectUnavailable,
    claim_for_commit,
    lfs_oid,
    record_evicted_versions,
)
from kohakuhub.storage_cleanup import enqueue_lfs_collection, record_head_change
from kohakuhub.api.repo.utils.hf import HFErrorCode, ensure_revision_in_history

logger = get_logger("FILE")
router = APIRouter()


class RepoType(str, Enum):
    """Repository type enumeration."""

    model = "model"
    dataset = "dataset"
    space = "space"


def build_public_repo_path(repo_type: RepoType, repo_id: str) -> str:
    """Return the HF-compatible repository path used in commit responses."""
    return f"{repo_type.value}s/{repo_id}"


def calculate_git_blob_sha1(content: bytes) -> str:
    """Calculate SHA1 hash in git blob format.

    Git uses: sha1(f'blob {size}\\0' + content)

    Args:
        content: File content bytes

    Returns:
        SHA1 hex digest
    """
    size = len(content)
    sha = hashlib.sha1()
    sha.update(f"blob {size}\0".encode("utf-8"))
    sha.update(content)
    return sha.hexdigest()


async def process_regular_file(
    path: str,
    content_b64: str,
    encoding: str,
    repo: Repository,
    lakefs_repo: str,
    revision: str,
) -> bool:
    """Process regular file with inline base64 content.

    IMPORTANT: This should only be used for small files (< LFS threshold).
    Large files MUST use the lfsFile operation to avoid duplication.

    Args:
        path: File path in repository
        content_b64: Base64 encoded content
        encoding: Content encoding
        repo: Repository object
        lakefs_repo: LakeFS repository name
        revision: Branch name

    Returns:
        True if file was changed, False if unchanged

    Raises:
        HTTPException: If processing fails or file is too large for standard commit
    """
    if not encoding.startswith("base64"):
        raise HTTPException(400, detail={"error": f"Invalid file operation for {path}"})

    # Decode content
    try:
        data = base64.b64decode(content_b64)
    except Exception as e:
        raise HTTPException(400, detail={"error": f"Failed to decode base64: {e}"})

    # Check file size against LFS threshold (use repo-specific settings)
    file_size = len(data)
    lfs_threshold = get_effective_lfs_threshold(repo)

    # Also check if file suffix requires LFS
    if should_use_lfs(repo, path, file_size):
        # File should use LFS (either by size or suffix rule)
        raise HTTPException(
            400,
            detail={
                "error": f"File {path} should use LFS (size: {file_size} bytes, threshold: {lfs_threshold} bytes). "
                f"Files >= {lfs_threshold} bytes or matching LFS suffix rules must be uploaded through Git LFS. "
                f"Use 'lfsFile' operation instead of 'file' operation.",
                "file_size": file_size,
                "lfs_threshold": lfs_threshold,
                "suggested_operation": "lfsFile",
            },
        )

    # Calculate git blob SHA1 for non-LFS files (HuggingFace format)
    git_blob_sha1 = calculate_git_blob_sha1(data)

    # Check if file unchanged (deduplication) against this branch's own row (#11)
    existing = get_file(repo, path, branch=revision)
    # ``get_file`` only returns active rows, so an unchanged match is always skipped
    if existing and existing.sha256 == git_blob_sha1 and existing.size == len(data):
        logger.info(f"Skipping unchanged file: {path}")
        return False

    logger.info(f"Uploading regular file: {path} ({file_size} bytes)")

    # Upload to LakeFS
    try:
        client = get_lakefs_client()
        await client.upload_object(
            repository=lakefs_repo,
            branch=revision,
            path=path,
            content=data,
        )
    except Exception as e:
        raise HTTPException(500, detail={"error": f"Failed to upload {path}: {e}"})

    # Update database - store git blob SHA1 in sha256 column for non-LFS files
    File.insert(
        repository=repo,
        branch=revision,
        path_in_repo=path,
        size=len(data),
        sha256=git_blob_sha1,
        lfs=False,
        is_deleted=False,
        owner=repo.owner,
    ).on_conflict(
        conflict_target=(File.repository, File.branch, File.path_in_repo),
        update={
            File.sha256: git_blob_sha1,
            File.size: len(data),
            File.lfs: False,  # Explicitly set to False
            File.is_deleted: False,  # File is active (un-delete if previously deleted)
            File.updated_at: datetime.now(timezone.utc),
        },
    ).execute()

    return True


async def _claim_lfs_object(oid: str, lfs_key: str, exists: bool | None = None) -> None:
    """Protect an LFS object from garbage collection while this commit links it.

    Answers 409 when the object is being collected or is gone, so the client
    uploads it again instead of committing a pointer to missing content (#114).
    """
    if exists is None:
        exists = await object_exists(cfg.s3.bucket, lfs_key)
    try:
        revived = claim_for_commit(oid, exists)
        if revived and not await object_exists(cfg.s3.bucket, lfs_key):
            raise LfsObjectUnavailable(oid)
    except LfsObjectUnavailable:
        raise HTTPException(
            409,
            detail={
                "error": f"LFS object {oid} is not available (garbage collected or missing). "
                "Upload it again and retry the commit."
            },
        )


async def process_lfs_file(
    path: str,
    oid: str,
    size: int,
    algo: str,
    repo: Repository,
    lakefs_repo: str,
    revision: str,
) -> tuple[bool, dict | None]:
    """Process LFS file that was uploaded to S3.

    Args:
        path: File path in repository
        oid: Object ID (SHA256 hash)
        size: File size in bytes
        algo: Hash algorithm (default: sha256)
        repo: Repository object
        lakefs_repo: LakeFS repository name
        revision: Branch name

    Returns:
        Tuple of (changed: bool, lfs_tracking_info: dict | None)

    Raises:
        HTTPException: If processing fails
    """
    if not oid:
        raise HTTPException(400, detail={"error": f"Missing OID for LFS file {path}"})

    # Check for existing file (including deleted files to detect re-upload) on this branch (#11)
    existing = File.get_or_none(
        (File.repository == repo) & (File.branch == revision) & (File.path_in_repo == path)
    )

    # Track old LFS object for potential deletion
    old_lfs_oid = None
    if existing and existing.lfs and existing.sha256 != oid:
        old_lfs_oid = existing.sha256
        logger.info(f"File {path} will be replaced: {old_lfs_oid} → {oid}")

    # Check if same content (including deleted files)
    # If same sha256+size, DON'T create new LFSObjectHistory
    same_content = existing and existing.sha256 == oid and existing.size == size

    if same_content:
        if existing.is_deleted:
            logger.info(
                f"[PROCESS_LFS_FILE] Re-uploading deleted file: {path} (sha256={oid[:8]}, size={size:,}) "
                f"- RESTORING in LakeFS (reusing existing LFSObjectHistory)"
            )
            # File was deleted, now being restored
            # Need to link physical address in LakeFS to restore the file
            # But DON'T create new LFSObjectHistory (already exists)

            # Construct S3 physical address
            lfs_key = f"lfs/{oid[:2]}/{oid[2:4]}/{oid}"
            physical_address = f"s3://{cfg.s3.bucket}/{lfs_key}"
            await _claim_lfs_object(oid, lfs_key)

            # Link the physical S3 object to LakeFS to restore
            try:
                staging_metadata = {
                    "staging": {
                        "physical_address": physical_address,
                    },
                    "checksum": f"{algo}:{oid}",
                    "size_bytes": size,
                }

                client = get_lakefs_client()
                await client.link_physical_address(
                    repository=lakefs_repo,
                    branch=revision,
                    path=path,
                    staging_metadata=staging_metadata,
                )

                logger.success(
                    f"Successfully restored LFS file in LakeFS: {path} "
                    f"(oid: {oid[:8]}, size: {size}, physical: {physical_address})"
                )

            except Exception as e:
                logger.exception(
                    f"Failed to restore LFS file in LakeFS: {path} "
                    f"(oid: {oid[:8]}, repo: {lakefs_repo}, branch: {revision})",
                    e,
                )
                raise HTTPException(
                    500,
                    detail={
                        "error": f"Failed to restore LFS file {path} in LakeFS: {str(e)}"
                    },
                )

            # Update database to mark as not deleted
            File.update(is_deleted=False, updated_at=datetime.now(timezone.utc)).where(
                File.id == existing.id
            ).execute()
            logger.success(f"Restored deleted file in DB: {path} (unmarked is_deleted)")

            # Return tracking info for new commit (reusing existing LFS object)
            return True, {
                "path": path,
                "sha256": oid,
                "size": size,
                "old_sha256": None,  # No old version (same content, just restoring)
            }
        else:
            logger.info(
                f"[PROCESS_LFS_FILE] File unchanged: {path} (sha256={oid[:8]}, size={size:,}) "
                f"- WILL TRACK in LFSObjectHistory"
            )
            # File exists and is active - normal case
            # Still return tracking info for new commit
            return False, {
                "path": path,
                "sha256": oid,
                "size": size,
                "old_sha256": None,  # No old version (file unchanged)
            }

    # File changed or new
    logger.info(f"Linking LFS file: {path}")

    # Construct S3 physical address
    lfs_key = f"lfs/{oid[:2]}/{oid[2:4]}/{oid}"
    physical_address = f"s3://{cfg.s3.bucket}/{lfs_key}"

    # Verify object exists in S3
    try:
        exists = await object_exists(cfg.s3.bucket, lfs_key)
        if not exists:
            logger.error(
                f"LFS object not found in S3: {oid[:8]} "
                f"(path: {path}, bucket: {cfg.s3.bucket}, key: {lfs_key})"
            )
            raise HTTPException(
                400,
                detail={
                    "error": f"LFS object {oid} not found in storage. "
                    f"Upload to S3 may have failed. Path: {lfs_key}"
                },
            )
        await _claim_lfs_object(oid, lfs_key, exists=True)
    except HTTPException:
        raise  # Re-raise HTTPException as-is
    except Exception as e:
        logger.exception(
            f"Failed to check S3 existence for LFS object {oid[:8]} "
            f"(path: {path}, bucket: {cfg.s3.bucket}, key: {lfs_key})",
            e,
        )
        raise HTTPException(
            500, detail={"error": f"Failed to verify LFS object in S3: {str(e)}"}
        )

    # Get actual size from S3 to verify
    try:
        s3_metadata = await get_object_metadata(cfg.s3.bucket, lfs_key)
        actual_size = s3_metadata["size"]

        if actual_size != size:
            logger.warning(
                f"Size mismatch for {path}. Expected: {size}, Got: {actual_size} "
                f"(oid: {oid[:8]}, key: {lfs_key})"
            )
            size = actual_size
    except Exception as e:
        logger.exception(
            f"Failed to get S3 metadata for LFS object {oid[:8]} "
            f"(path: {path}, bucket: {cfg.s3.bucket}, key: {lfs_key})",
            e,
        )
        logger.warning(
            f"Could not verify S3 object metadata, continuing without size check"
        )

    # Link the physical S3 object to LakeFS
    try:
        staging_metadata = {
            "staging": {
                "physical_address": physical_address,
            },
            "checksum": f"{algo}:{oid}",
            "size_bytes": size,
        }

        client = get_lakefs_client()
        await client.link_physical_address(
            repository=lakefs_repo,
            branch=revision,
            path=path,
            staging_metadata=staging_metadata,
        )

        logger.success(
            f"Successfully linked LFS file in LakeFS: {path} "
            f"(oid: {oid[:8]}, size: {size}, physical: {physical_address})"
        )

    except Exception as e:
        logger.exception(
            f"Failed to link LFS file in LakeFS: {path} "
            f"(oid: {oid[:8]}, repo: {lakefs_repo}, branch: {revision}, "
            f"physical_address: {physical_address})",
            e,
        )
        raise HTTPException(
            500,
            detail={"error": f"Failed to link LFS file {path} in LakeFS: {str(e)}"},
        )

    # Update database
    File.insert(
        repository=repo,
        branch=revision,
        path_in_repo=path,
        size=size,
        sha256=oid,
        lfs=True,
        is_deleted=False,
        owner=repo.owner,
    ).on_conflict(
        conflict_target=(File.repository, File.branch, File.path_in_repo),
        update={
            File.sha256: oid,
            File.size: size,
            File.lfs: True,
            File.is_deleted: False,  # File is active (un-delete if previously deleted)
            File.updated_at: datetime.now(timezone.utc),
        },
    ).execute()

    logger.success(f"Updated database record for LFS file: {path}")

    # Return tracking info for GC
    tracking_info = {
        "path": path,
        "sha256": oid,
        "size": size,
        "old_sha256": old_lfs_oid,
    }

    logger.info(
        f"[PROCESS_LFS_FILE] File changed/new: {path} (sha256={oid[:8]}, size={size:,}) "
        f"- WILL TRACK in LFSObjectHistory"
    )

    return True, tracking_info


async def process_deleted_file(
    path: str, repo: Repository, lakefs_repo: str, revision: str
) -> bool:
    """Process file deletion.

    Marks file as deleted (soft delete) instead of removing from database.
    This preserves LFSObjectHistory FK references for quota tracking.

    Args:
        path: File path to delete
        repo: Repository object
        lakefs_repo: LakeFS repository name
        revision: Branch name

    Returns:
        True (always changes repository)
    """
    logger.info(f"Deleting file: {path}")

    try:
        client = get_lakefs_client()
        await client.delete_object(repository=lakefs_repo, branch=revision, path=path)
        logger.success(f"Successfully deleted file from LakeFS: {path}")
    except Exception as e:
        # File might not exist, log warning but continue
        logger.warning(f"Failed to delete {path} from LakeFS: {e}")

    # Mark as deleted in database (soft delete) on this branch (#11)
    updated_count = (
        File.update(is_deleted=True, updated_at=datetime.now(timezone.utc))
        .where(
            (File.repository == repo) & (File.branch == revision) & (File.path_in_repo == path)
        )
        .execute()
    )

    if updated_count > 0:
        logger.success(f"Marked {path} as deleted in database (soft delete)")
    else:
        logger.info(f"File {path} was not in database")

    return True


async def process_deleted_folder(
    path: str, repo: Repository, lakefs_repo: str, revision: str
) -> bool:
    """Process folder deletion.

    Args:
        path: Folder path to delete
        repo: Repository object
        lakefs_repo: LakeFS repository name
        revision: Branch name

    Returns:
        True (always changes repository)
    """
    # Normalize folder path
    folder_path = path if path.endswith("/") else f"{path}/"
    logger.info(f"Deleting folder: {folder_path}")

    try:
        client = get_lakefs_client()

        # List all objects in the folder with pagination
        all_folder_objects = []
        after = ""
        has_more = True

        while has_more:
            objects = await client.list_objects(
                repository=lakefs_repo,
                ref=revision,
                prefix=folder_path,
                delimiter="",
                amount=1000,
                after=after,
            )

            all_folder_objects.extend(objects["results"])

            if objects.get("pagination") and objects["pagination"].get("has_more"):
                after = objects["pagination"]["next_offset"]
                has_more = True
            else:
                has_more = False

        # Delete each file concurrently
        file_objects = [
            obj for obj in all_folder_objects if obj["path_type"] == "object"
        ]

        async def delete_file_obj(obj):
            try:
                await client.delete_object(
                    repository=lakefs_repo, branch=revision, path=obj["path"]
                )
                logger.info(f"  Deleted: {obj['path']}")
                return obj["path"]
            except Exception as e:
                logger.warning(f"  Failed to delete {obj['path']}: {e}")
                return None

        results = await asyncio.gather(*[delete_file_obj(obj) for obj in file_objects])
        deleted_files = [path for path in results if path is not None]

        logger.success(f"Deleted {len(deleted_files)} files from folder {folder_path}")

        # Mark as deleted in database (soft delete) on this branch (#11). startswith
        # is ILIKE, so narrow in SQL and match the prefix case-sensitively like
        # LakeFS did: deleting data/ must not mark Data/ deleted.
        if deleted_files:
            ids = [
                row.id
                for row in File.select(File.id, File.path_in_repo).where(
                    (File.repository == repo)
                    & (File.branch == revision)
                    & (File.path_in_repo.startswith(folder_path))
                )
                if row.path_in_repo.startswith(folder_path)
            ]
            updated_count = (
                File.update(is_deleted=True, updated_at=datetime.now(timezone.utc))
                .where(File.id.in_(ids))
                .execute()
            )
            logger.success(
                f"Marked {updated_count} file(s) as deleted in database (soft delete)"
            )

    except Exception as e:
        logger.warning(f"Error deleting folder {folder_path}: {e}")

    return True


async def process_copy_file(
    dest_path: str,
    src_path: str,
    src_revision: str,
    repo: Repository,
    lakefs_repo: str,
    revision: str,
) -> tuple[bool, dict | None]:
    """Process file copy operation.

    Args:
        dest_path: Destination file path
        src_path: Source file path
        src_revision: Source revision
        repo: Repository object
        lakefs_repo: LakeFS repository name
        revision: Branch name

    Returns:
        Tuple of (True, lfs_tracking_info or None): copying always changes the
        repository; a global LFS object is tracked like a linked upload

    Raises:
        HTTPException: If copy fails
    """
    if not src_path:
        raise HTTPException(
            400, detail={"error": f"Missing srcPath for copyFile operation"}
        )
    # Nothing is copied out of history a squash removed
    await ensure_revision_in_history(get_lakefs_client(), repo, lakefs_repo, src_revision)

    logger.info(
        f"Copying file: {src_path} -> {dest_path} (from revision: {src_revision})"
    )

    try:
        # Get source file metadata from LakeFS
        client = get_lakefs_client()
        src_obj = await client.stat_object(
            repository=lakefs_repo, ref=src_revision, path=src_path
        )
        # The address says which content is linked; the source path's file
        # row describes its current version, not necessarily src_revision's.
        oid = lfs_oid(src_obj["physical_address"])
        if oid:
            await _claim_lfs_object(oid, f"lfs/{oid[:2]}/{oid[2:4]}/{oid}")
        previous = File.get_or_none(
            (File.repository == repo)
            & (File.branch == revision)
            & (File.path_in_repo == dest_path)
        )

        # Use LakeFS staging API to link the physical address
        staging_metadata = {
            "staging": {
                "physical_address": src_obj["physical_address"],
            },
            "checksum": src_obj["checksum"],
            "size_bytes": src_obj["size_bytes"],
        }

        await client.link_physical_address(
            repository=lakefs_repo,
            branch=revision,
            path=dest_path,
            staging_metadata=staging_metadata,
        )

        logger.success(
            f"Successfully linked {dest_path} to same physical address as {src_path}"
        )

        # Update database - copy file metadata on this branch. The source's row is
        # the source revision's own row (#11)
        src_file = get_file(repo, src_path, branch=src_revision)
        if oid:
            File.insert(
                repository=repo,
                branch=revision,
                path_in_repo=dest_path,
                size=src_obj["size_bytes"],
                sha256=oid,
                lfs=True,
                is_deleted=False,
                owner=repo.owner,
            ).on_conflict(
                conflict_target=(File.repository, File.branch, File.path_in_repo),
                update={
                    File.sha256: oid,
                    File.size: src_obj["size_bytes"],
                    File.lfs: True,
                    File.is_deleted: False,
                    File.updated_at: datetime.now(timezone.utc),
                },
            ).execute()
        elif src_file:
            File.insert(
                repository=repo,
                branch=revision,
                path_in_repo=dest_path,
                size=src_file.size,
                sha256=src_file.sha256,
                lfs=src_file.lfs,
                is_deleted=False,
                owner=repo.owner,
            ).on_conflict(
                conflict_target=(File.repository, File.branch, File.path_in_repo),
                update={
                    File.sha256: src_file.sha256,
                    File.size: src_file.size,
                    File.lfs: src_file.lfs,
                    File.is_deleted: False,  # File is active
                    File.updated_at: datetime.now(timezone.utc),
                },
            ).execute()
        else:
            # No row for the source (a commit id, or a path it has no row for):
            # the identity is read from the content, as commits record it
            is_lfs = should_use_lfs(repo, dest_path, src_obj["size_bytes"])
            checksum = src_obj["checksum"]
            if not is_lfs:
                content = await client.get_object(
                    repository=lakefs_repo, ref=src_revision, path=src_path
                )
                checksum = calculate_git_blob_sha1(content)
            File.insert(
                repository=repo,
                branch=revision,
                path_in_repo=dest_path,
                size=src_obj["size_bytes"],
                sha256=checksum,
                lfs=is_lfs,
                is_deleted=False,
                owner=repo.owner,
            ).on_conflict(
                conflict_target=(File.repository, File.branch, File.path_in_repo),
                update={
                    File.sha256: checksum,
                    File.size: src_obj["size_bytes"],
                    File.lfs: is_lfs,
                    File.is_deleted: False,  # File is active
                    File.updated_at: datetime.now(timezone.utc),
                },
            ).execute()

        logger.success(f"Successfully copied {src_path} to {dest_path}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            500,
            detail={
                "error": f"Failed to copy file {src_path} to {dest_path}: {str(e)}"
            },
        )

    if not oid:
        return True, None
    replaced = previous and previous.lfs and previous.sha256 != oid
    return True, {
        "path": dest_path,
        "sha256": oid,
        "size": src_obj["size_bytes"],
        "old_sha256": previous.sha256 if replaced else None,
    }


# Operations that put something at their path
CREATING = ("file", "lfsFile", "copyFile")


def _path_too_long(path: str) -> HTTPException:
    message = f"A path is longer than {MAX_PATH_BYTES} bytes (UTF-8), the limit for a repository path"
    return HTTPException(
        400,
        detail={"error": f"{message}: {path[:100]}..."},
        # A header holds Latin-1 only: the path stays in the body
        headers={"X-Error-Code": HFErrorCode.BAD_REQUEST, "X-Error-Message": message},
    )


def _touched(operations: list[dict]) -> tuple[list[str], list[str]]:
    """The paths the operations change, and the folders they delete."""
    paths, folders = [], []
    for op in operations:
        path = op["value"].get("path")
        if not path:
            continue
        if op["key"] == "deletedFolder":
            folders.append(path.strip("/") + "/")
        else:
            paths.append(path)
    return paths, folders


def _file_rows(
    repo: Repository, touched: tuple[list[str], list[str]], branch: str
) -> dict[int, dict]:
    """The File rows of the touched paths on ``branch``, by id (#11)."""
    paths, folders = touched
    rows = {}
    for start in range(0, len(paths), 500):
        query = File.select().where(
            (File.repository == repo)
            & (File.branch == branch)
            & File.path_in_repo.in_(paths[start : start + 500])
        )
        rows.update((row["id"], row) for row in query.dicts())
    for folder in folders:
        query = File.select().where(
            (File.repository == repo)
            & (File.branch == branch)
            & File.path_in_repo.startswith(folder)
        )
        rows.update((row["id"], row) for row in query.dicts())
    return rows


async def _undo(
    client,
    lakefs_repo: str,
    branch: str,
    repo: Repository,
    touched: tuple[list[str], list[str]],
    before: dict[int, dict],
) -> None:
    """Leave the branch and its File rows as they were before a commit that
    failed: what it staged would otherwise go into the next commit on the
    branch, whoever makes it. Best effort: a failure here is logged and the
    first failure is what the client sees. ``before`` is the branch's rows
    (#11) taken before the commit staged anything."""
    try:
        with db.atomic():  # no await inside: no other request's statements
            for row_id, row in _file_rows(repo, touched, branch).items():
                if row_id not in before:
                    File.delete().where(File.id == row_id).execute()
                elif row != before[row_id]:
                    File.update(**before[row_id]).where(File.id == row_id).execute()
    except Exception as e:
        logger.warning(f"Could not restore the File rows of {repo.full_id}: {e}")
    paths, folders = touched
    for path, prefix in [(p, False) for p in paths] + [(f, True) for f in folders]:
        try:
            await client.reset_uncommitted(
                repository=lakefs_repo, branch=branch, path=path, prefix=prefix
            )
        except Exception as e:
            logger.warning(f"Could not drop what a failed commit staged at {path!r}: {e}")


async def _stage_operations(
    operations: list[dict], repo_row: Repository, lakefs_repo: str, revision: str
) -> tuple[bool, list[dict]]:
    """Stage each operation on the branch; whether any changed something,
    and the LFS files to track once committed."""
    files_changed = False
    pending_lfs_tracking = []

    for op in operations:
        key = op["key"]
        value = op["value"]
        path = value.get("path")
        logger.info(f"Processing {key}: {path}")

        match key:
            case "file":
                # Regular file with inline content
                changed = await process_regular_file(
                    path=path,
                    content_b64=value.get("content"),
                    encoding=(value.get("encoding") or "").lower(),
                    repo=repo_row,
                    lakefs_repo=lakefs_repo,
                    revision=revision,
                )
                files_changed = files_changed or changed

            case "lfsFile":
                # LFS file already in S3
                changed, lfs_info = await process_lfs_file(
                    path=path,
                    oid=value.get("oid"),
                    size=value.get("size"),
                    algo=value.get("algo", "sha256"),
                    repo=repo_row,
                    lakefs_repo=lakefs_repo,
                    revision=revision,
                )
                files_changed = files_changed or changed
                if lfs_info:
                    logger.debug(
                        f"[COMMIT_OP] Adding LFS file to tracking queue: {path} "
                        f"(sha256={lfs_info['sha256'][:8]}, size={lfs_info['size']:,})"
                    )
                    pending_lfs_tracking.append(lfs_info)
                else:
                    logger.warning(
                        f"[COMMIT_OP] process_lfs_file returned NO tracking info for: {path} "
                        f"(oid={value.get('oid', 'MISSING')[:8]})"
                    )

            case "deletedFile":
                # Delete single file
                changed = await process_deleted_file(
                    path=path,
                    repo=repo_row,
                    lakefs_repo=lakefs_repo,
                    revision=revision,
                )
                files_changed = files_changed or changed

            case "deletedFolder":
                # Delete folder recursively
                changed = await process_deleted_folder(
                    path=path,
                    repo=repo_row,
                    lakefs_repo=lakefs_repo,
                    revision=revision,
                )
                files_changed = files_changed or changed

            case "copyFile":
                # Copy file
                changed, lfs_info = await process_copy_file(
                    dest_path=path,
                    src_path=value.get("srcPath"),
                    src_revision=value.get("srcRevision", revision),
                    repo=repo_row,
                    lakefs_repo=lakefs_repo,
                    revision=revision,
                )
                files_changed = files_changed or changed
                if lfs_info:
                    pending_lfs_tracking.append(lfs_info)

    return files_changed, pending_lfs_tracking


@router.post("/{repo_type}s/{namespace}/{name}/commit/{revision}")
async def commit(
    repo_type: RepoType,
    namespace: str,
    name: str,
    revision: str,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Create atomic commit with multiple file operations.

    Accepts NDJSON payload with header and file operations.
    Supports inline base64 content for small files and LFS references for large files.

    Args:
        repo_type: Type of repository
        namespace: Repository namespace
        name: Repository name
        revision: Branch name
        request: FastAPI request with NDJSON payload
        user: Current authenticated user

    Returns:
        Commit result with OID and URL

    Raises:
        HTTPException: If commit fails
    """
    repo_id = f"{namespace}/{name}"

    # `create_pr=True` sends ``?create_pr=1`` on the commit endpoint. KohakuHub
    # does not implement the discussions / pull-request workflow, and silently
    # dropping the flag (as the old handler did) means the commit lands on the
    # target branch instead of an isolated ``refs/pr/<N>`` — a compat-breaking
    # surprise. Reject it up front with a HuggingFace-compatible 501 so the
    # client surfaces a clear HfHubHTTPError carrying our X-Error-Message.
    if request.query_params.get("create_pr") in ("1", "true", "True"):
        raise HTTPException(
            status_code=501,
            detail={
                "error": (
                    "create_commit(create_pr=True) is not supported by KohakuHub. "
                    "Commit to a branch directly instead; the discussions / "
                    "pull-request workflow is not implemented."
                )
            },
            headers={
                "X-Error-Code": HFErrorCode.NOT_IMPLEMENTED,
                "X-Error-Message": (
                    "create_commit(create_pr=True) is not supported by KohakuHub. "
                    "Commit to a branch directly instead; the discussions / "
                    "pull-request workflow is not implemented."
                ),
            },
        )

    # Check repository exists and write permission
    repo_row = Repository.get_or_none(
        (Repository.full_id == repo_id) & (Repository.repo_type == repo_type.value)
    )
    if not repo_row:
        raise HTTPException(404, detail={"error": "Repository not found"})

    check_repo_write_permission(repo_row, user)
    operation_lock.ensure_free(repo_row)

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()

    # Parse NDJSON payload
    raw = await request.body()
    lines = raw.decode("utf-8").splitlines()

    if cfg.app.debug_log_payloads:
        logger.debug("==== Commit Payload ====")
        for line in lines:
            logger.debug(line)

    # Parse header and operations
    header = None
    operations = []

    for line in lines:
        if not line.strip():
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise HTTPException(400, detail={"error": f"Invalid JSON line: {e}"})

        key = obj.get("key")
        value = obj.get("value", {})

        if key == "header":
            header = value
        elif key in ("file", "lfsFile", "deletedFile", "deletedFolder", "copyFile"):
            operations.append({"key": key, "value": value})

    if header is None:
        raise HTTPException(400, detail={"error": "Missing commit header"})

    # Refused before anything is staged: nothing is left behind
    for op in operations:
        if op["key"] in CREATING and too_long(op["value"].get("path") or ""):
            raise _path_too_long(op["value"]["path"])

    touched = _touched(operations)
    before = _file_rows(repo_row, touched, revision)
    try:
        files_changed, pending_lfs_tracking = await _stage_operations(
            operations, repo_row, lakefs_repo, revision
        )
    except Exception as error:
        await _undo(client, lakefs_repo, revision, repo_row, touched, before)
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(500, detail={"error": f"Commit failed: {error}"}) from error

    # If no files changed, return early
    if not files_changed:
        try:
            branch = await client.get_branch(repository=lakefs_repo, branch=revision)
            commit_id = branch["commit_id"]
        except Exception:
            commit_id = "no-changes"

        commit_url = f"{build_public_repo_path(repo_type, repo_id)}/commit/{commit_id}"

        return {
            "commitUrl": commit_url,
            "commitOid": commit_id,
            "pullRequestUrl": None,
        }

    # Create commit in LakeFS
    commit_msg = header.get("summary", "Commit via API")
    commit_desc = header.get("description", "")
    logger.info(f"Commit message: {commit_msg}")

    try:
        # A history operation holding the repository goes first; the staged
        # changes then land on top of what it left (operation_lock)
        async with operation_lock.writing(repo_row):
            commit_result = await client.commit(
                repository=lakefs_repo,
                branch=revision,
                message=commit_msg,
                metadata={"description": commit_desc} if commit_desc else None,
            )
    except (HTTPException, httpx.HTTPStatusError) as error:
        # Refused (an operation holds the repository, or LakeFS said no):
        # nothing was committed
        await _undo(client, lakefs_repo, revision, repo_row, touched, before)
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(500, detail={"error": f"Commit failed: {error}"}) from error
    except Exception as e:
        # No answer: the commit may have landed, so the branch stays as it is
        raise HTTPException(500, detail={"error": f"Commit failed: {str(e)}"})

    # Poll to verify commit is accessible (LakeFS needs time to process large commits)
    commit_id = commit_result["id"]
    logger.info(f"Verifying commit {commit_id[:8]} is accessible...")
    max_attempts = 120
    for attempt in range(max_attempts):
        try:
            await client.get_commit(repository=lakefs_repo, commit_id=commit_id)
            logger.debug(
                f"Commit {commit_id[:8]} verified after {attempt + 1} attempts"
            )
            break
        except Exception as e:
            if attempt < max_attempts - 1:
                logger.debug(
                    f"Commit not ready yet (attempt {attempt + 1}/{max_attempts}), waiting..."
                )
                await asyncio.sleep(0.5)  # Wait 500ms before retry
            else:
                logger.warning(
                    f"Commit {commit_id[:8]} not accessible after {max_attempts} attempts, but continuing..."
                )
                break

    # Record commit in our database (track the actual user)
    try:
        create_commit(
            commit_id=commit_result["id"],
            repository=repo_row,
            repo_type=repo_type.value,
            branch=revision,
            author=user,
            username=user.username,
            message=commit_msg,
            description=commit_desc,
        )
        logger.info(f"Recorded commit {commit_result['id'][:8]} by {user.username}")
    except Exception as e:
        logger.warning(f"Failed to record commit in database: {e}")
        # Don't fail the commit if DB recording fails

    # The last commit of each path it changed (a listing without it asks LakeFS)
    try:
        await path_commits.record(
            repo_row,
            revision,
            commit_result,
            [
                op["value"]["path"]
                for op in operations
                if op["key"] in ("file", "lfsFile", "deletedFile", "deletedFolder", "copyFile")
                and op["value"].get("path")
            ],
        )
    except Exception as e:
        logger.warning(f"Failed to record the last commits of {commit_id[:8]}: {e}")

    # Generate commit URL
    commit_url = (
        f"{build_public_repo_path(repo_type, repo_id)}/commit/{commit_result['id']}"
    )
    logger.success(f"Commit URL: {commit_url}")

    # Track LFS objects; schedule the collection of evicted versions
    if pending_lfs_tracking:
        logger.info(
            f"[COMMIT_LFS_TRACKING] Processing {len(pending_lfs_tracking)} LFS file(s) "
            f"for commit {commit_result['id'][:8]}"
        )
        for lfs_info in pending_lfs_tracking:
            logger.debug(
                f"  - {lfs_info['path']}: sha256={lfs_info['sha256'][:8]}, size={lfs_info['size']:,}"
            )

            track_lfs_object(
                repo_type=repo_type.value,
                namespace=namespace,
                name=name,
                path_in_repo=lfs_info["path"],
                sha256=lfs_info["sha256"],
                size=lfs_info["size"],
                commit_id=commit_result["id"],
                branch=revision,
            )

        # Versions this commit pushed out of a path's keep window become
        # collection candidates; the background collection decides (#114).
        replaced = [info["path"] for info in pending_lfs_tracking if info.get("old_sha256")]
        if record_evicted_versions(repo_row, replaced):
            enqueue_lfs_collection()
    else:
        logger.warning(
            f"[COMMIT_LFS_TRACKING] No LFS files to track for commit {commit_result['id'][:8]}"
        )

    # What the branch head links now; garbage collection never deletes it (#114)
    head_paths = {
        op["value"].get("path"): None
        for op in operations
        if op["key"] in ("file", "lfsFile", "deletedFile", "copyFile")
    }
    head_paths.update({info["path"]: info["sha256"] for info in pending_lfs_tracking})
    folders = [
        path if path.endswith("/") else f"{path}/"
        for path in (op["value"].get("path") for op in operations if op["key"] == "deletedFolder")
    ]
    record_head_change(repo_row, revision, head_paths, folders)

    if revision == usage.MAIN:
        from kohakuhub.api.commit.records import count_main_move  # imports this module

        await count_main_move(client, lakefs_repo, repo_row, commit_result["id"])

    return {
        "commitUrl": commit_url,
        "commitOid": commit_result["id"],
        "pullRequestUrl": None,
    }
