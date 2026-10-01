"""Whether commits can be reverted, or a branch reset to them (KohakuHub-only).

Kept apart from the Hugging Face compatible commit list on purpose: that
payload stays exactly what ``huggingface_hub`` expects, and clients paging
through a whole history never pay for these checks.
"""

import asyncio
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from kohakuhub.api.commit import availability
from kohakuhub.api.repo.utils.hf import ensure_revision_in_history, hf_repo_not_found
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.auth.permissions import check_repo_read_permission, check_repo_write_permission
from kohakuhub.db import User
from kohakuhub.db_operations import get_repository
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo

router = APIRouter()
LOOKUP_CONCURRENCY = 16


class CommitIds(BaseModel):
    # One commit list page of LakeFS commit ids
    commit_ids: list[Annotated[str, Field(max_length=64)]] = Field(max_length=100)


def _can_write(repo, user) -> bool:
    try:
        return check_repo_write_permission(repo, user)
    except HTTPException:
        return False


async def _branch_head(client, lakefs_repo: str, branch: str) -> str:
    try:
        return (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        raise HTTPException(404, detail={"error": f"Branch not found: {branch}"})


async def _commit(client, lakefs_repo: str, commit_id: str) -> dict | None:
    try:
        return await client.get_commit(repository=lakefs_repo, commit_id=commit_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in (400, 404):  # 400: not a commit id at all
            raise
        return None


def _gate(caps: dict, can_write: bool) -> dict[str, dict]:
    """Verdicts that need no evaluation: disabled on the site, or no write access."""
    gated = {}
    for op in ("revert", "reset"):
        if not caps[op]:
            gated[op] = availability.verdict("disabled", op)
        elif not can_write:
            gated[op] = availability.verdict("forbidden", op)
    return gated


@router.get("/{repo_type}s/{namespace}/{name}/commit/{commit_id}/operations")
async def commit_operations(
    repo_type: str,
    namespace: str,
    name: str,
    commit_id: str,
    branch: str = "main",
    user: User | None = Depends(get_optional_user),
):
    """Whether ``commit_id`` can be reverted on ``branch``, or ``branch`` reset to it.

    Exact: diffs, entries and the bucket are checked (see
    ``kohakuhub.api.commit.availability``).
    """
    repo = get_repository(repo_type, namespace, name)
    if not repo:
        return hf_repo_not_found(f"{namespace}/{name}", repo_type)
    check_repo_read_permission(repo, user)
    lakefs_repo = resolve_lakefs_repo(repo)
    client = get_lakefs_client()
    await ensure_revision_in_history(client, repo, lakefs_repo, commit_id)
    head = await _branch_head(client, lakefs_repo, branch)
    commit = await _commit(client, lakefs_repo, commit_id)
    if commit is None:
        raise HTTPException(404, detail={"error": f"Commit not found: {commit_id}"})

    caps, can_write = availability.capabilities(), _can_write(repo, user)
    result = _gate(caps, can_write)
    if "revert" not in result:
        result["revert"] = await availability.revert_verdict(client, lakefs_repo, commit, head)
    if "reset" not in result:
        result["reset"] = await availability.reset_verdict(
            client, lakefs_repo, commit, head, branch
        )
    return {
        "commit": commit["id"],
        "branch": branch,
        "head": head,
        "can_write": can_write,
        "operations": caps,
        "parents": commit.get("parents") or [],
        **result,
    }


@router.post("/{repo_type}s/{namespace}/{name}/commits/{branch}/operations")
async def commits_operations(
    repo_type: str,
    namespace: str,
    name: str,
    branch: str,
    payload: CommitIds,
    user: User | None = Depends(get_optional_user),
):
    """What a commit list page can mark: only what is proven unavailable.

    Commits the user could not act on anyway (no write access, or both
    operations disabled) are not evaluated.
    """
    repo = get_repository(repo_type, namespace, name)
    if not repo:
        return hf_repo_not_found(f"{namespace}/{name}", repo_type)
    check_repo_read_permission(repo, user)
    lakefs_repo = resolve_lakefs_repo(repo)
    client = get_lakefs_client()
    await ensure_revision_in_history(client, repo, lakefs_repo, branch)
    head = await _branch_head(client, lakefs_repo, branch)
    caps, can_write = availability.capabilities(), _can_write(repo, user)
    response = {"branch": branch, "head": head, "can_write": can_write, "operations": caps}
    if not can_write or not any(caps.values()):
        return {**response, "commits": {}}

    limit = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def lookup(commit_id):
        async with limit:
            return await _commit(client, lakefs_repo, commit_id)

    found = await asyncio.gather(*(lookup(c) for c in dict.fromkeys(payload.commit_ids)))
    commits = [commit for commit in found if commit is not None]
    verdicts = await availability.quick_verdicts(client, lakefs_repo, repo, commits, head)
    for commit_verdicts in verdicts.values():
        for op in ("revert", "reset"):
            if not caps[op]:
                commit_verdicts[op] = availability.verdict("disabled", op)
    return {**response, "commits": verdicts}


@router.get("/{repo_type}s/{namespace}/{name}/commit/{commit_id}/unavailable-files")
async def commit_unavailable_files(
    repo_type: str,
    namespace: str,
    name: str,
    commit_id: str,
    branch: str = "main",
    user: User | None = Depends(get_optional_user),
):
    """Every LFS file of the commit's tree that garbage collection removed.

    For every reader: tombstones only, no bucket requests. ``files`` is
    ``None`` past a bounded number of LakeFS calls.
    """
    repo = get_repository(repo_type, namespace, name)
    if not repo:
        return hf_repo_not_found(f"{namespace}/{name}", repo_type)
    check_repo_read_permission(repo, user)
    lakefs_repo = resolve_lakefs_repo(repo)
    client = get_lakefs_client()
    await ensure_revision_in_history(client, repo, lakefs_repo, commit_id)
    head = await _branch_head(client, lakefs_repo, branch)
    commit = await _commit(client, lakefs_repo, commit_id)
    if commit is None:
        raise HTTPException(404, detail={"error": f"Commit not found: {commit_id}"})
    files = await availability.unavailable_files(
        client, lakefs_repo, repo, commit["id"], branch, head
    )
    response = {"commit": commit["id"], "branch": branch, "files": files}
    if files is None:
        response["reason"] = "too_large"
    return response


@router.post("/{repo_type}s/{namespace}/{name}/commits/unavailable-files")
async def commits_unavailable_files(
    repo_type: str,
    namespace: str,
    name: str,
    payload: CommitIds,
    user: User | None = Depends(get_optional_user),
):
    """For a commit list page: the LFS files each commit committed whose
    object garbage collection removed. One database query, for every reader."""
    repo = get_repository(repo_type, namespace, name)
    if not repo:
        return hf_repo_not_found(f"{namespace}/{name}", repo_type)
    check_repo_read_permission(repo, user)
    return {"commits": availability.introduced_unavailable(repo, payload.commit_ids)}
