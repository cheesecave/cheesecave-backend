"""Branch and tag management API endpoints."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from kohakuhub.db import Repository, User
from kohakuhub.db_operations import get_repository
from kohakuhub.logger import get_logger
from kohakuhub.storage_cleanup import (
    SCRATCH_BRANCH_PREFIX,
    enqueue_branch_links,
    enqueue_lfs_reconciliation,
    forget_branch,
)
from kohakuhub.auth.dependencies import get_current_user, get_optional_user
from kohakuhub.auth.permissions import (
    check_repo_delete_permission,
    check_repo_read_permission,
    check_repo_write_permission,
)
from kohakuhub.utils.lakefs import (
    get_lakefs_client,
    resolve_lakefs_repo,
    resolve_revision,
)
from kohakuhub.api.commit import records, reset, revert
from kohakuhub.api.repo.utils.hf import (
    HFErrorCode,
    hf_error_response,
    hf_repo_not_found,
    hf_server_error,
)
from kohakuhub.api.operation_capabilities import (
    ensure_repository_operation_enabled,
    require_repository_reset_enabled,
    require_repository_revert_enabled,
)

logger = get_logger("BRANCHES")

router = APIRouter()


class CreateBranchPayload(BaseModel):
    """Payload for branch creation."""

    branch: str
    revision: Optional[str] = None  # Source revision (defaults to main)


class CreateBranchCompatPayload(BaseModel):
    """Hugging Face compatible payload for branch creation."""

    startingPoint: Optional[str] = None


@router.post("/{repo_type}s/{namespace}/{name}/branch")
async def create_branch(
    repo_type: str,
    namespace: str,
    name: str,
    payload: CreateBranchPayload,
    user: User = Depends(get_current_user),
):
    """Create a new branch.

    Args:
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name
        payload: Branch creation parameters
        user: Current authenticated user

    Returns:
        Success message
    """
    repo_id = f"{namespace}/{name}"

    # Check if repository exists
    repo_row = get_repository(repo_type, namespace, name)

    if not repo_row:
        return hf_repo_not_found(repo_id, repo_type)

    # Check if user has permission
    check_repo_delete_permission(repo_row, user)
    if payload.branch.startswith(SCRATCH_BRANCH_PREFIX):  # a reset's working branch
        return hf_error_response(
            400,
            HFErrorCode.BAD_REQUEST,
            f"Branch names starting with '{SCRATCH_BRANCH_PREFIX}' are reserved",
        )

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()

    try:
        # Resolve source revision — accept branch name, tag, or commit sha
        # (huggingface_hub.create_branch(revision=…) passes any of the three).
        source_ref = payload.revision or "main"
        source_commit, _ = await resolve_revision(client, lakefs_repo, source_ref)

        # Create new branch
        await client.create_branch(
            repository=lakefs_repo,
            name=payload.branch,
            source=source_commit,
        )
    except ValueError as e:
        # resolve_revision raises ValueError when the ref is neither branch
        # nor commit — surface it as a 404 RevisionNotFound to the client.
        return hf_error_response(
            404,
            HFErrorCode.REVISION_NOT_FOUND,
            str(e),
        )
    except Exception as e:
        logger.exception("Failed to create branch", e)
        error_msg = str(e).replace("\n", " ").replace("\r", " ")

        # Check if branch already exists (409)
        if "409" in error_msg or "conflict" in error_msg.lower():
            return hf_error_response(
                409,
                HFErrorCode.BAD_REQUEST,
                f"Branch '{payload.branch}' already exists",
            )

        return hf_server_error(f"Failed to create branch: {error_msg}")

    enqueue_branch_links(repo_row, payload.branch)
    return {"success": True, "message": f"Branch '{payload.branch}' created"}


@router.post("/{repo_type}s/{namespace}/{name}/branch/{branch}")
async def create_branch_compat(
    repo_type: str,
    namespace: str,
    name: str,
    branch: str,
    payload: CreateBranchCompatPayload,
    user: User = Depends(get_current_user),
):
    """Create a branch using the Hugging Face Hub compatible route shape."""
    return await create_branch(
        repo_type=repo_type,
        namespace=namespace,
        name=name,
        payload=CreateBranchPayload(
            branch=branch,
            revision=payload.startingPoint,
        ),
        user=user,
    )


@router.delete("/{repo_type}s/{namespace}/{name}/branch/{branch}")
async def delete_branch(
    repo_type: str,
    namespace: str,
    name: str,
    branch: str,
    user: User = Depends(get_current_user),
):
    """Delete a branch.

    Args:
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name
        branch: Branch name to delete
        user: Current authenticated user

    Returns:
        Success message
    """
    repo_id = f"{namespace}/{name}"

    # Check if repository exists
    repo_row = get_repository(repo_type, namespace, name)

    if not repo_row:
        return hf_repo_not_found(repo_id, repo_type)

    # Check if user has permission
    check_repo_delete_permission(repo_row, user)

    # Prevent deletion of main branch
    if branch == "main":
        return hf_error_response(
            400,
            HFErrorCode.BAD_REQUEST,
            "Cannot delete main branch",
        )

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()

    try:
        await client.delete_branch(repository=lakefs_repo, branch=branch)
    except Exception as e:
        return hf_server_error(f"Failed to delete branch: {str(e)}")

    forget_branch(repo_row, branch)
    return {"success": True, "message": f"Branch '{branch}' deleted"}


class RevertPayload(BaseModel):
    """Payload for reverting a commit."""

    ref: str  # Commit ID or ref to revert
    parent_number: int = 1  # For merge commits
    message: Optional[str] = None
    metadata: Optional[dict[str, str]] = None
    force: bool = False  # accepted and ignored: LakeFS refuses conflicts regardless
    allow_empty: bool = False


class MergePayload(BaseModel):
    """Payload for merging branches."""

    message: Optional[str] = None
    metadata: Optional[dict[str, str]] = None
    strategy: Optional[str] = None  # 'dest-wins' or 'source-wins'
    force: bool = False
    allow_empty: bool = False
    squash_merge: bool = False


class ResetPayload(BaseModel):
    """Payload for resetting a branch."""

    ref: str  # Commit ID or ref to reset to
    message: Optional[str] = None  # Optional custom commit message
    force: bool = False


class CreateTagPayload(BaseModel):
    """Payload for tag creation."""

    tag: str
    revision: Optional[str] = None  # Source revision (defaults to main)
    message: Optional[str] = None


class CreateTagCompatPayload(BaseModel):
    """Hugging Face compatible payload for tag creation."""

    tag: str
    message: Optional[str] = None


@router.post("/{repo_type}s/{namespace}/{name}/tag")
async def create_tag(
    repo_type: str,
    namespace: str,
    name: str,
    payload: CreateTagPayload,
    user: User = Depends(get_current_user),
):
    """Create a new tag.

    Args:
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name
        payload: Tag creation parameters
        user: Current authenticated user

    Returns:
        Success message
    """
    repo_id = f"{namespace}/{name}"

    # Check if repository exists
    repo_row = get_repository(repo_type, namespace, name)

    if not repo_row:
        return hf_repo_not_found(repo_id, repo_type)

    # Check if user has permission
    check_repo_delete_permission(repo_row, user)

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()

    try:
        # Resolve source revision — accept branch, tag, or commit sha.
        source_ref = payload.revision or "main"
        source_commit, _ = await resolve_revision(client, lakefs_repo, source_ref)

        # Create new tag
        await client.create_tag(
            repository=lakefs_repo,
            id=payload.tag,
            ref=source_commit,
        )
    except ValueError as e:
        return hf_error_response(
            404,
            HFErrorCode.REVISION_NOT_FOUND,
            str(e),
        )
    except Exception as e:
        return hf_server_error(f"Failed to create tag: {str(e)}")

    return {"success": True, "message": f"Tag '{payload.tag}' created"}


@router.post("/{repo_type}s/{namespace}/{name}/tag/{revision}")
async def create_tag_compat(
    repo_type: str,
    namespace: str,
    name: str,
    revision: str,
    payload: CreateTagCompatPayload,
    user: User = Depends(get_current_user),
):
    """Create a tag using the Hugging Face Hub compatible route shape."""
    return await create_tag(
        repo_type=repo_type,
        namespace=namespace,
        name=name,
        payload=CreateTagPayload(
            tag=payload.tag,
            revision=revision,
            message=payload.message,
        ),
        user=user,
    )


@router.delete("/{repo_type}s/{namespace}/{name}/tag/{tag}")
async def delete_tag(
    repo_type: str,
    namespace: str,
    name: str,
    tag: str,
    user: User = Depends(get_current_user),
):
    """Delete a tag.

    Args:
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name
        tag: Tag name to delete
        user: Current authenticated user

    Returns:
        Success message
    """
    repo_id = f"{namespace}/{name}"

    # Check if repository exists
    repo_row = get_repository(repo_type, namespace, name)

    if not repo_row:
        return hf_repo_not_found(repo_id, repo_type)

    # Check if user has permission
    check_repo_delete_permission(repo_row, user)

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()

    try:
        await client.delete_tag(repository=lakefs_repo, tag=tag)
    except Exception as e:
        return hf_server_error(f"Failed to delete tag: {str(e)}")

    return {"success": True, "message": f"Tag '{tag}' deleted"}


def _resolve_ref_name(item: dict[str, Any]) -> str | None:
    """Extract a branch/tag name from a LakeFS reference payload."""
    return item.get("id") or item.get("name")


def _resolve_target_commit(item: dict[str, Any]) -> str | None:
    """Extract the commit ID from a LakeFS reference payload."""
    commit = item.get("commit")
    if isinstance(commit, dict):
        return commit.get("id") or commit.get("commit_id") or commit.get("commitId")

    return item.get("commit_id") or item.get("commitId") or item.get("hash")


async def _collect_reference_page(
    list_method,
    repository: str,
) -> list[dict[str, Any]]:
    """Collect a complete paginated list of LakeFS references."""
    results: list[dict[str, Any]] = []
    after: str | None = None

    while True:
        payload = await list_method(repository=repository, after=after, amount=1000)
        if isinstance(payload, list):
            results.extend(payload)
            return results

        results.extend(payload.get("results", []))
        pagination = payload.get("pagination", {})
        if not pagination.get("has_more"):
            return results

        after = pagination.get("next_offset")
        if not after:
            return results


@router.get("/{repo_type}s/{namespace}/{name}/refs")
async def list_repo_refs(
    repo_type: str,
    namespace: str,
    name: str,
    include_prs: bool = False,
    user: User | None = Depends(get_optional_user),
):
    """List branches and tags using the Hugging Face Hub compatible schema."""
    repo_id = f"{namespace}/{name}"
    repo_row = get_repository(repo_type, namespace, name)

    if not repo_row:
        return hf_repo_not_found(repo_id, repo_type)

    check_repo_read_permission(repo_row, user)

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()
    branches: list[dict[str, str]] = []
    tags: list[dict[str, str]] = []

    try:
        branch_items = await _collect_reference_page(client.list_branches, lakefs_repo)
    except Exception as e:
        logger.warning(f"Failed to list branches for {repo_id}: {e}")
        branch_items = []

    if not branch_items:
        try:
            branch_items = [await client.get_branch(repository=lakefs_repo, branch="main")]
        except Exception as e:
            logger.warning(f"Failed to fetch main branch for {repo_id}: {e}")

    for item in branch_items:
        branch_name = _resolve_ref_name(item)
        target_commit = _resolve_target_commit(item)
        if not branch_name or not target_commit:
            continue
        branches.append(
            {
                "name": branch_name,
                "ref": f"refs/heads/{branch_name}",
                "targetCommit": target_commit,
            }
        )

    try:
        tag_items = await _collect_reference_page(client.list_tags, lakefs_repo)
    except Exception as e:
        logger.warning(f"Failed to list tags for {repo_id}: {e}")
        tag_items = []

    for item in tag_items:
        tag_name = _resolve_ref_name(item)
        target_commit = _resolve_target_commit(item)
        if not tag_name or not target_commit:
            continue
        tags.append(
            {
                "name": tag_name,
                "ref": f"refs/tags/{tag_name}",
                "targetCommit": target_commit,
            }
        )

    response = {
        "branches": sorted(branches, key=lambda item: item["name"]),
        "converts": [],
        "tags": sorted(tags, key=lambda item: item["name"]),
    }
    if include_prs:
        response["pullRequests"] = []
    return response


@router.post(
    "/{repo_type}s/{namespace}/{name}/branch/{branch}/revert",
    dependencies=[Depends(require_repository_revert_enabled)],
)
async def revert_branch(
    repo_type: str,
    namespace: str,
    name: str,
    branch: str,
    payload: RevertPayload,
    user: User = Depends(get_current_user),
):
    """Revert a commit on a branch.

    This endpoint reverts the changes from a specific commit, creating a new
    commit that undoes those changes. It checks if all LFS files from the
    target commit are still available before reverting.

    Args:
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name
        branch: Branch name to revert on
        payload: Revert parameters (ref, force, etc.)
        user: Current authenticated user

    Returns:
        Success message

    Raises:
        HTTPException: If revert fails or LFS files are not recoverable
    """
    ensure_repository_operation_enabled("revert")

    repo_id = f"{namespace}/{name}"

    # Check if repository exists
    repo_row = get_repository(repo_type, namespace, name)

    if not repo_row:
        return hf_repo_not_found(repo_id, repo_type)

    # Check if user has write permission
    check_repo_write_permission(repo_row, user)

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()

    # Resolve the ref to a commit ID (for logging/validation)
    try:
        commit = await client.get_commit(repository=lakefs_repo, commit_id=payload.ref)
        commit_id = commit["id"]
        logger.info(f"Reverting commit {commit_id[:8]} on branch {branch}")
    except Exception as e:
        logger.error(f"Failed to resolve ref {payload.ref}: {e}")
        raise HTTPException(
            status_code=404,
            detail={"error": f"Commit not found: {payload.ref}"},
        )

    # LakeFS reverts natively and atomically; what it restores is checked
    # and claimed first, and the commit it makes recorded like a commit's
    # (#99). ``force`` is accepted and ignored: LakeFS refuses a conflict or
    # uncommitted changes with or without it.
    message = payload.message or f"Revert commit {commit_id[:8]}"
    try:
        new_commit_id, rounds = await revert.revert_commit(
            client,
            lakefs_repo,
            branch,
            commit,
            payload.parent_number,
            message,
            payload.metadata,
            payload.allow_empty,
        )
    except records.OperationRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    except Exception as e:
        logger.exception(f"Failed to revert commit: {e}", e)
        # Whether the revert landed is unknown here: have the reconciliation
        # record what the branch links (collection waits for it)
        enqueue_lfs_reconciliation()
        raise HTTPException(status_code=500, detail={"error": f"Revert failed: {e}"})
    await records.record_commits(
        client, lakefs_repo, repo_row, branch, rounds, user, message, f"Reverted {commit_id}"
    )
    logger.success(f"Reverted {commit_id[:8]} on {repo_id}@{branch}: {new_commit_id[:8]}")

    return {
        "success": True,
        "message": f"Successfully reverted commit {commit_id[:8]} on branch '{branch}'",
        "new_commit_id": new_commit_id,
    }


@router.post(
    "/{repo_type}s/{namespace}/{name}/merge/{source_ref}/into/{destination_branch}"
)
async def merge_branches(
    repo_type: str,
    namespace: str,
    name: str,
    source_ref: str,
    destination_branch: str,
    payload: MergePayload,
    user: User = Depends(get_current_user),
):
    """Merge source reference into destination branch.

    Args:
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name
        source_ref: Source reference (branch/commit to merge from)
        destination_branch: Destination branch name
        payload: Merge parameters (message, strategy, etc.)
        user: Current authenticated user

    Returns:
        Merge result with reference and summary

    Raises:
        HTTPException: If merge fails
    """
    repo_id = f"{namespace}/{name}"

    # Check if repository exists
    repo_row = get_repository(repo_type, namespace, name)

    if not repo_row:
        return hf_repo_not_found(repo_id, repo_type)

    # Check if user has write permission
    check_repo_write_permission(repo_row, user)

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()

    # Perform the merge
    try:
        # What the merge commit changed is measured from here
        base = (await client.get_branch(repository=lakefs_repo, branch=destination_branch))[
            "commit_id"
        ]
        merge_result = await client.merge_into_branch(
            repository=lakefs_repo,
            source_ref=source_ref,
            destination_branch=destination_branch,
            message=payload.message,
            metadata=payload.metadata,
            strategy=payload.strategy,
            force=payload.force,
            allow_empty=payload.allow_empty,
            squash_merge=payload.squash_merge,
        )
        logger.success(
            f"Successfully merged {source_ref} into {destination_branch} in {repo_id}"
        )
    except Exception as e:
        logger.exception(f"Failed to merge {source_ref} into {destination_branch}", e)
        error_msg = str(e)
        logger.error(f"Failed to merge branches: {error_msg}")

        # Check if it's a conflict error
        if "conflict" in error_msg.lower():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": f"Merge conflict: {error_msg}. "
                    f"Use strategy='source-wins' or 'dest-wins' to resolve automatically.",
                },
            )

        raise HTTPException(
            status_code=500,
            detail={"error": f"Merge failed: {error_msg}"},
        )

    # Record the merge commit, and what it changed, like a commit's
    merge_commit_id = merge_result.get("reference")
    if merge_commit_id:
        merge_msg = payload.message or f"Merge {source_ref} into {destination_branch}"
        try:
            rounds = [await records.commit_changes(client, lakefs_repo, merge_commit_id, base)]
        except Exception as e:
            # The merge happened: record its commit; the reconciliation
            # records what the branch links
            logger.warning(f"Could not read what merge {merge_commit_id[:8]} changed: {e}")
            enqueue_lfs_reconciliation()
            rounds = [(merge_commit_id, {})]
        await records.record_commits(
            client,
            lakefs_repo,
            repo_row,
            destination_branch,
            rounds,
            user,
            merge_msg,
            f"Merged {source_ref}",
        )
    else:
        logger.warning("Merge result did not contain commit reference")

    return {
        "success": True,
        "message": f"Successfully merged {source_ref} into {destination_branch}",
        "result": merge_result,
    }


@router.post(
    "/{repo_type}s/{namespace}/{name}/branch/{branch}/reset",
    dependencies=[Depends(require_repository_reset_enabled)],
)
async def reset_branch(
    repo_type: str,
    namespace: str,
    name: str,
    branch: str,
    payload: ResetPayload,
    user: User = Depends(get_current_user),
):
    """Reset a branch to a specific commit (like git reset --hard).

    This endpoint resets the branch HEAD to point to a specific commit,
    effectively going back in time. It checks if all LFS files from the
    target commit are still available before resetting.

    Args:
        repo_type: Repository type (model/dataset/space)
        namespace: Repository namespace
        name: Repository name
        branch: Branch name to reset
        payload: Reset parameters (ref, force)
        user: Current authenticated user

    Returns:
        Success message

    Raises:
        HTTPException: If reset fails or LFS files are not recoverable
    """
    ensure_repository_operation_enabled("reset")

    repo_id = f"{namespace}/{name}"

    # Check if repository exists
    repo_row = get_repository(repo_type, namespace, name)

    if not repo_row:
        return hf_repo_not_found(repo_id, repo_type)

    # Check if user has write permission
    check_repo_write_permission(repo_row, user)

    # Prevent resetting main branch without force (safety measure)
    if branch == "main" and not payload.force:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Cannot reset main branch without force=true. "
                "This is a safety measure to prevent accidental data loss."
            },
        )

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()

    # Resolve the ref to a commit ID
    try:
        # Get the commit to reset to
        commit = await client.get_commit(repository=lakefs_repo, commit_id=payload.ref)
        commit_id = commit["id"]
    except Exception as e:
        logger.exception(f"Failed to resolve ref {payload.ref}", e)
        logger.error(f"Failed to resolve ref {payload.ref}: {e}")
        raise HTTPException(
            status_code=404,
            detail={"error": f"Commit not found: {payload.ref}"},
        )

    # A new commit whose tree equals the target's: history is kept, and no
    # file content passes through this service (#99). LFS objects are always
    # checked, ``force`` only allows resetting main (#107).
    message = payload.message or f"Reset to commit {commit_id[:8]}"
    try:
        head, rounds = await reset.reset_branch(
            client, repo_row, lakefs_repo, branch, commit_id, message
        )
    except records.OperationRefused as e:
        # Merged before giving up or failing: those commits are on the branch
        await records.record_commits(
            client, lakefs_repo, repo_row, branch, e.rounds, user, message, f"Reset to {commit_id}"
        )
        raise HTTPException(status_code=e.status, detail=e.detail)
    except Exception as e:
        logger.exception(f"Failed to reset branch: {e}", e)
        # Whether a merge landed before the failure is unknown here: have the
        # reconciliation record what the branch links (collection waits for it)
        enqueue_lfs_reconciliation()
        raise HTTPException(status_code=500, detail={"error": f"Reset failed: {e}"})
    await records.record_commits(
        client, lakefs_repo, repo_row, branch, rounds, user, message, f"Reset to {commit_id}"
    )
    logger.success(f"Reset {repo_id}@{branch} to {commit_id[:8]} in {len(rounds)} merge(s)")

    return {
        "success": True,
        "message": f"Successfully reset branch '{branch}' to commit {commit_id[:8]} (new commit created)",
        "commit_id": head,
    }
