"""Garbage collection utilities for LFS objects."""

import asyncio
from datetime import datetime, timezone

from kohakuhub.config import cfg
from kohakuhub.db import File, LFSObjectHistory, Repository
from kohakuhub.db_operations import (
    create_lfs_history,
    get_repository,
    should_use_lfs,
)
from kohakuhub.lfs_gc import deleted_shas, lfs_oid
from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import get_lakefs_client
from kohakuhub.utils.s3 import delete_objects_with_prefix, object_exists

logger = get_logger("GC")


def track_lfs_object(
    repo_type: str,
    namespace: str,
    name: str,
    path_in_repo: str,
    sha256: str,
    size: int,
    commit_id: str,
):
    """Track LFS object usage in a commit.

    Always creates a new LFSObjectHistory entry for full commit tracking.
    GC will count unique oids to determine what to delete.

    Args:
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name
        path_in_repo: File path in repository
        sha256: LFS object SHA256 hash
        size: Object size in bytes
        commit_id: LakeFS commit ID
    """
    logger.info(
        f"[TRACK_LFS_OBJECT_CALLED] repo={repo_type}/{namespace}/{name}, "
        f"path={path_in_repo}, sha256={sha256[:8]}, size={size:,}, commit={commit_id[:8]}"
    )

    # Get repository FK object
    repo = get_repository(repo_type, namespace, name)
    if not repo:
        logger.error(f"Repository not found: {repo_type}/{namespace}/{name}")
        return

    # Try to find corresponding File record for FK link
    file_fk = File.get_or_none(
        (File.repository == repo) & (File.path_in_repo == path_in_repo)
    )

    # Always create new LFS history entry with FK objects
    create_lfs_history(
        repository=repo,
        path_in_repo=path_in_repo,
        sha256=sha256,
        size=size,
        commit_id=commit_id,
        file=file_fk,  # Optional FK for faster lookups
    )
    logger.success(
        f"[TRACK_LFS_OBJECT_DONE] Created LFS history for {path_in_repo} "
        f"(sha256={sha256[:8]}, commit={commit_id[:8]})"
    )


async def check_lfs_recoverability(
    repo: Repository, commit_id: str
) -> tuple[bool, list[str]]:
    """Check if all LFS files in a commit are still available in S3.

    Args:
        repo: Repository FK object
        commit_id: Commit ID to check

    Returns:
        Tuple of (all_recoverable: bool, missing_files: list[str])
    """
    # Get all LFS objects from this commit using repository FK and backref
    lfs_objects = list(
        repo.lfs_history.select().where(LFSObjectHistory.commit_id == commit_id)
    )

    if not lfs_objects:
        # No LFS files in this commit, always recoverable
        return True, []

    missing_files = []
    # History rows outlive a collected object; its tombstone says it is gone.
    collected = deleted_shas(lfs_obj.sha256 for lfs_obj in lfs_objects)

    # Check S3 existence concurrently
    async def check_lfs_object(lfs_obj):
        lfs_key = f"lfs/{lfs_obj.sha256[:2]}/{lfs_obj.sha256[2:4]}/{lfs_obj.sha256}"
        exists = lfs_obj.sha256 not in collected and await object_exists(cfg.s3.bucket, lfs_key)

        if not exists:
            logger.warning(
                f"LFS object missing for {lfs_obj.path_in_repo}: {lfs_obj.sha256[:8]}"
            )
            return lfs_obj.path_in_repo
        return None

    results = await asyncio.gather(
        *[check_lfs_object(lfs_obj) for lfs_obj in lfs_objects]
    )
    missing_files = [path for path in results if path is not None]

    all_recoverable = len(missing_files) == 0

    if all_recoverable:
        logger.info(
            f"All {len(lfs_objects)} LFS files in commit {commit_id[:8]} are recoverable"
        )
    else:
        logger.warning(
            f"{len(missing_files)} LFS files in commit {commit_id[:8]} are NOT recoverable"
        )

    return all_recoverable, missing_files


async def check_commit_range_recoverability(
    lakefs_repo: str,
    repo_type: str,
    namespace: str,
    name: str,
    target_commit: str,
    current_branch: str,
) -> tuple[bool, list[str], list[str]]:
    """Check if all LFS files in commit range (target to HEAD) are recoverable.

    For reset operations, we need to ensure all commits from target to current HEAD
    have their LFS files available, because the operation traverses this range.

    Args:
        lakefs_repo: LakeFS repository name
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name
        target_commit: Target commit to reset to
        current_branch: Current branch name

    Returns:
        Tuple of (all_recoverable: bool, missing_files: list[str], affected_commits: list[str])
    """
    client = get_lakefs_client()

    # Get repository FK object
    repo = get_repository(repo_type, namespace, name)
    if not repo:
        logger.error(f"Repository not found: {repo_type}/{namespace}/{name}")
        return False, [], []

    # Get all commits from target to HEAD
    try:
        # Paginate through commits to find target
        commit_list = []
        after = ""
        has_more = True
        target_index = None

        while has_more and target_index is None:
            commits_result = await client.log_commits(
                repository=lakefs_repo,
                ref=current_branch,
                amount=1000,  # LakeFS maximum
                after=after,
            )

            results = commits_result.get("results", [])
            current_batch_start = len(commit_list)
            commit_list.extend(results)

            # Find target commit in current batch
            for i, commit in enumerate(results):
                if commit["id"] == target_commit:
                    target_index = current_batch_start + i
                    break

            # Check pagination
            pagination = commits_result.get("pagination", {})
            has_more = pagination.get("has_more", False)
            if has_more:
                after = pagination.get("next_offset", "")

        if target_index is None:
            logger.warning(
                f"Target commit {target_commit[:8]} not found in branch history (checked {len(commit_list)} commits)"
            )
            return False, [], []

        # Check all commits from index 0 (HEAD) to target_index (inclusive)
        commits_to_check = commit_list[: target_index + 1]

        logger.info(
            f"Checking LFS recoverability for {len(commits_to_check)} commit(s) "
            f"from HEAD to {target_commit[:8]}"
        )

        all_missing_files = []
        affected_commits = []

        # Check all commits concurrently
        async def check_commit(commit):
            commit_id = commit["id"]
            recoverable, missing = await check_lfs_recoverability(repo, commit_id)
            return recoverable, missing, commit_id

        results = await asyncio.gather(
            *[check_commit(commit) for commit in commits_to_check]
        )

        for recoverable, missing, commit_id in results:
            if not recoverable:
                all_missing_files.extend(missing)
                affected_commits.append(commit_id)

        all_recoverable = len(all_missing_files) == 0

        if all_recoverable:
            logger.success(
                f"All LFS files in {len(commits_to_check)} commit(s) are recoverable"
            )
        else:
            logger.warning(
                f"Found {len(all_missing_files)} missing LFS file(s) across "
                f"{len(affected_commits)} commit(s)"
            )

        return all_recoverable, all_missing_files, affected_commits

    except Exception as e:
        logger.exception(f"Failed to check commit range recoverability", e)
        return False, [], []


async def sync_file_table_with_commit(
    lakefs_repo: str,
    ref: str,
    repo_type: str,
    namespace: str,
    name: str,
) -> int:
    """Sync File table with actual commit state (for reset operations).

    After a reset, the branch points to an old commit, but the File table
    may still have entries from the newer state. This function syncs the
    File table to match the actual commit state.

    Args:
        lakefs_repo: LakeFS repository name
        ref: Branch name or commit ID to sync to
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name

    Returns:
        Number of files synced
    """
    client = get_lakefs_client()

    # Get repository FK object
    repo = get_repository(repo_type, namespace, name)
    if not repo:
        logger.error(f"Repository not found: {repo_type}/{namespace}/{name}")
        return 0

    try:
        # Get commit ID from ref
        # For reset operations, ref is the branch name, but we should list using commit ID
        # to avoid issues with uncommitted changes
        branch_info = await client.get_branch(repository=lakefs_repo, branch=ref)
        commit_id = branch_info["commit_id"]

        # Get ALL objects at the commit (use commit ID to avoid staging issues)
        # LakeFS has max amount=1000, so we need to paginate
        all_objects = []
        after = ""
        has_more = True

        while has_more:
            list_result = await client.list_objects(
                repository=lakefs_repo,
                ref=commit_id,  # Use commit ID, not branch name!
                amount=1000,  # LakeFS maximum
                after=after,
            )

            results = list_result.get("results", [])
            all_objects.extend(results)

            # Check pagination
            pagination = list_result.get("pagination", {})
            has_more = pagination.get("has_more", False)
            if has_more:
                after = pagination.get("next_offset", "")

        logger.info(f"Paginated through LakeFS, got {len(all_objects)} total objects")
        logger.info(
            f"Syncing {len(all_objects)} file(s) from ref {ref} (commit {commit_id[:8]})"
        )

        synced_count = 0
        file_paths = []

        for obj in all_objects:
            if obj.get("path_type") != "object":
                continue

            path = obj["path"]
            file_paths.append(path)
            size_bytes = obj.get("size_bytes", 0)
            checksum = obj.get("checksum", "")

            # Extract SHA256 from checksum
            if ":" in checksum:
                sha256 = checksum.split(":", 1)[1]
            else:
                sha256 = checksum

            # A global LFS address is the identity; otherwise repo-specific
            # LFS rules (size + suffix)
            oid = lfs_oid(obj.get("physical_address"))
            sha256 = oid or sha256
            is_lfs = oid is not None or should_use_lfs(repo, path, size_bytes)

            logger.debug(
                f"Syncing file: {path} (size={size_bytes}, lfs={is_lfs}, sha256={sha256[:8]})"
            )

            # Get File FK if exists for LFS history tracking
            file_fk = File.get_or_none(
                (File.repository == repo) & (File.path_in_repo == path)
            )

            # Update File table using repository FK
            File.insert(
                repository=repo,
                path_in_repo=path,
                size=size_bytes,
                sha256=sha256,
                lfs=is_lfs,
                is_deleted=False,
                owner=repo.owner,  # Denormalized owner
            ).on_conflict(
                conflict_target=(File.repository, File.path_in_repo),
                update={
                    File.sha256: sha256,
                    File.size: size_bytes,
                    File.lfs: is_lfs,
                    File.is_deleted: False,  # File is active
                    File.updated_at: datetime.now(timezone.utc),
                },
            ).execute()

            # Reload file_fk after insert/update
            if not file_fk:
                file_fk = File.get_or_none(
                    (File.repository == repo) & (File.path_in_repo == path)
                )

            # Track LFS objects in history
            if is_lfs:
                create_lfs_history(
                    repository=repo,
                    path_in_repo=path,
                    sha256=sha256,
                    size=size_bytes,
                    commit_id=commit_id,
                    file=file_fk,
                )

            synced_count += 1

        # Remove files from File table that no longer exist in commit using repository FK
        if file_paths:
            removed_count = (
                File.delete()
                .where(
                    (File.repository == repo) & (File.path_in_repo.not_in(file_paths))
                )
                .execute()
            )
        else:
            removed_count = 0

        if removed_count > 0:
            logger.info(f"Removed {removed_count} stale file(s) from File table")

        logger.success(
            f"Synced {synced_count} file(s) to ref {ref} (commit {commit_id[:8]})"
        )
        return synced_count

    except Exception as e:
        logger.exception(f"Failed to sync file table", e)
        return 0


async def cleanup_repository_storage(
    repo_type: str,
    namespace: str,
    name: str,
    lakefs_repo: str,
) -> dict[str, int]:
    """Delete a deleted or moved repository's folder in S3 (its LakeFS data).

    LFS objects are shared, so they are not deleted here: the caller records
    the repository's LFS objects as collection candidates in the transaction
    that deletes the row (``storage_cleanup.record_repository_lfs``), and the
    background collection deletes the ones nothing else relies on.

    Args:
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name
        lakefs_repo: LakeFS repository name (for S3 prefix)

    Returns:
        Dict with the 'repo_objects_deleted' count
    """
    repo_prefix = f"{lakefs_repo}/"
    repo_objects_deleted = await delete_objects_with_prefix(cfg.s3.bucket, repo_prefix)
    logger.info(
        f"Deleted {repo_objects_deleted} repository object(s) from S3 prefix: {repo_prefix} "
        f"({repo_type}/{namespace}/{name})"
    )
    return {"repo_objects_deleted": repo_objects_deleted}


async def track_commit_lfs_objects(
    lakefs_repo: str,
    commit_id: str,
    repo_type: str,
    namespace: str,
    name: str,
) -> int:
    """Track all LFS objects in a commit (after revert/merge).

    This function is called after a revert or merge operation to ensure
    all LFS objects in the new commit are tracked in history and won't
    be accidentally garbage collected.

    Args:
        lakefs_repo: LakeFS repository name
        commit_id: The new commit ID (from revert/merge)
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name

    Returns:
        Number of LFS objects tracked
    """
    client = get_lakefs_client()

    # Get repository FK object
    repo = get_repository(repo_type, namespace, name)
    if not repo:
        logger.error(f"Repository not found: {repo_type}/{namespace}/{name}")
        return 0

    # Get commit details to find parent
    try:
        commit = await client.get_commit(repository=lakefs_repo, commit_id=commit_id)
        parents = commit.get("parents", [])

        if not parents:
            logger.warning(f"Commit {commit_id[:8]} has no parents, cannot track diff")
            return 0

        parent_commit = parents[0]
        logger.info(
            f"Tracking LFS objects in commit {commit_id[:8]} (diff from {parent_commit[:8]})"
        )

        # Get diff between parent and new commit
        diff_result = await client.diff_refs(
            repository=lakefs_repo,
            left_ref=parent_commit,
            right_ref=commit_id,
        )

        tracked_count = 0
        files_to_remove = []

        for result in diff_result.get("results", []):
            path = result.get("path")
            path_type = result.get("path_type")
            diff_type = result.get("type")  # "added", "removed", "changed"

            # Skip if not an object
            if path_type != "object":
                continue

            # Handle removed files
            if diff_type == "removed":
                files_to_remove.append(path)
                logger.debug(f"File removed: {path}")
                continue

            # Get object metadata to check if it's LFS
            try:
                obj_stat = await client.stat_object(
                    repository=lakefs_repo,
                    ref=commit_id,
                    path=path,
                )

                size_bytes = obj_stat.get("size_bytes", 0)

                # Extract SHA256 from checksum (format: "sha256:hash")
                checksum = obj_stat.get("checksum", "")
                if ":" in checksum:
                    sha256 = checksum.split(":", 1)[1]
                else:
                    sha256 = checksum

                # A global LFS address is the identity; otherwise
                # repo-specific LFS rules (size + suffix)
                oid = lfs_oid(obj_stat.get("physical_address"))
                sha256 = oid or sha256
                is_lfs = oid is not None or should_use_lfs(repo, path, size_bytes)

                # Get File FK if exists for LFS history tracking
                file_fk = File.get_or_none(
                    (File.repository == repo) & (File.path_in_repo == path)
                )

                # Track LFS objects in history
                if is_lfs:
                    create_lfs_history(
                        repository=repo,
                        path_in_repo=path,
                        sha256=sha256,
                        size=size_bytes,
                        commit_id=commit_id,
                        file=file_fk,
                    )
                    tracked_count += 1
                    logger.debug(f"Tracked LFS object: {path} ({sha256[:8]})")

                # Update File table for BOTH LFS and regular files using repository FK
                File.insert(
                    repository=repo,
                    path_in_repo=path,
                    size=size_bytes,
                    sha256=sha256,
                    lfs=is_lfs,
                    is_deleted=False,
                    owner=repo.owner,  # Denormalized owner
                ).on_conflict(
                    conflict_target=(File.repository, File.path_in_repo),
                    update={
                        File.sha256: sha256,
                        File.size: size_bytes,
                        File.lfs: is_lfs,
                        File.is_deleted: False,  # File is active
                        File.updated_at: datetime.now(timezone.utc),
                    },
                ).execute()

            except Exception as e:
                logger.warning(f"Failed to stat object {path}: {e}")
                continue

        # Remove deleted files from File table using repository FK
        if files_to_remove:
            removed_count = (
                File.delete()
                .where(
                    (File.repository == repo) & (File.path_in_repo.in_(files_to_remove))
                )
                .execute()
            )
            logger.info(f"Removed {removed_count} deleted file(s) from File table")

        if tracked_count > 0:
            logger.success(
                f"Tracked {tracked_count} LFS object(s) in commit {commit_id[:8]}"
            )

        return tracked_count

    except Exception as e:
        logger.exception(f"Failed to track commit LFS objects", e)
        return 0
