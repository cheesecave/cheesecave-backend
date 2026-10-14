"""A repository's LFS objects: list them and delete them for good (#66).

``HfApi.list_lfs_files`` reads ``GET /api/{type}s/{repo}/lfs-files``; ``permanently_delete_lfs_files`` posts
``{"deletions": {"sha": [...], "rewriteHistory": bool}}`` to ``.../lfs-files/batch``. The list is what the repository's
history says it stored (an object is listed once, with the path and ref of its latest version). The delete hands the
decision to the garbage collection's (``kohakuhub.lfs_gc``), with the repository's own history left out of it: the
object goes when no branch head, active file, recent upload or other repository relies on it, and the history rows stay,
so a commit that linked it is told its content is gone (the tombstone) and the quota drops by its size.
"""

from datetime import timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from peewee import fn
from pydantic import BaseModel, Field, StringConstraints

from kohakuhub import lfs_gc
from kohakuhub.api.repo.utils.hf import (
    HFErrorCode,
    hf_bad_request,
    hf_error_response,
    hf_not_implemented,
    hf_repo_not_found,
    hf_server_error,
)
from kohakuhub.api.utils.paging import link_header
from kohakuhub.async_utils import run_in_s3_executor
from kohakuhub.auth.dependencies import get_current_user
from kohakuhub.auth.permissions import check_repo_delete_permission, check_repo_write_permission
from kohakuhub.config import cfg
from kohakuhub.constants import DATETIME_FORMAT_ISO
from kohakuhub.db import Commit, File, LFSObjectHistory, LfsGrant, LfsObjectTombstone, User
from kohakuhub.db_operations import get_repository
from kohakuhub.lfs_gc import claim_key, lfs_key
from kohakuhub.logger import get_logger
from kohakuhub.storage_cleanup import _delete_keys

logger = get_logger("LFS_FILES")

router = APIRouter()

PAGE_SIZE = 1000  # the client deletes in batches of 1000, and so does this
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]

_WHY = {
    "head": "a branch head links it",
    "file": "an active file has this content",
    "recent": "it was uploaded or committed within the last 24 hours",
    "history": "another repository's history keeps it",
}


def _pushed_at(value) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).strftime(DATETIME_FORMAT_ISO)


@router.get("/{repo_type}s/{namespace}/{name}/lfs-files")
async def list_lfs_files(
    repo_type: str,
    namespace: str,
    name: str,
    request: Request,
    limit: int = Query(PAGE_SIZE, ge=1, le=PAGE_SIZE),
    cursor: str | None = None,
    user: User = Depends(get_current_user),
):
    """The LFS objects the repository stores, newest first, in the Hub's shape (``LFSFileInfo``)."""
    repo_row = get_repository(repo_type, namespace, name)
    if not repo_row:
        return hf_repo_not_found(f"{namespace}/{name}", repo_type)
    check_repo_write_permission(repo_row, user)

    H, T = LFSObjectHistory, LfsObjectTombstone
    newest = fn.MAX(H.id)
    query = (
        H.select(H.sha256, newest.alias("newest"))
        .where(
            (H.repository == repo_row.id)
            & (fn.LENGTH(H.sha256) == 64)  # an LFS object, not a git blob
            & H.sha256.not_in(T.select(T.sha256))  # collected: no longer there
        )
        .group_by(H.sha256)
        .order_by(newest.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        if not cursor.isdigit():
            return hf_bad_request("Invalid cursor")
        query = query.having(newest < int(cursor))
    page = list(query.tuples())
    more = len(page) > limit
    page = page[:limit]
    shas = [sha for sha, _ in page]

    first, last = {}, {}
    for row in (
        H.select(H.id, H.sha256, H.path_in_repo, H.size, H.created_at, H.file, H.commit_id)
        .where((H.repository == repo_row.id) & H.sha256.in_(shas))
        .order_by(H.id)
    ):
        first.setdefault(row.sha256, row.created_at)
        last[row.sha256] = row
    branch_of_file = {
        file.id: file.branch
        for file in File.select(File.id, File.branch).where(
            File.id.in_({row.file_id for row in last.values() if row.file_id})
        )
    }
    branch_of_commit = {
        commit.commit_id: commit.branch
        for commit in Commit.select(Commit.commit_id, Commit.branch).where(
            (Commit.repository == repo_row.id) & Commit.commit_id.in_({row.commit_id for row in last.values()})
        )
    }

    entries = []
    for sha in shas:
        row = last[sha]
        branch = branch_of_file.get(row.file_id) or branch_of_commit.get(row.commit_id)
        entries.append(
            {
                "filename": row.path_in_repo,
                "oid": sha,
                "fileOid": sha,  # what the client sends back to delete
                "pushedAt": _pushed_at(first[sha]),
                "ref": f"refs/heads/{branch}" if branch else None,
                "size": row.size,
            }
        )
    next_cursor = str(page[-1][1]) if more else None
    return JSONResponse(content=entries, headers=link_header(request, next_cursor))


class Deletions(BaseModel):
    sha: list[Sha256] = Field(max_length=PAGE_SIZE)
    rewriteHistory: bool = False


class DeleteLfsFiles(BaseModel):
    deletions: Deletions


def _refused(status: int, code: str, message: str, content: dict) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=content,
        headers={"X-Error-Code": code, "X-Error-Message": message},
    )


@router.post("/{repo_type}s/{namespace}/{name}/lfs-files/batch")
async def delete_lfs_files(
    repo_type: str,
    namespace: str,
    name: str,
    payload: DeleteLfsFiles,
    user: User = Depends(get_current_user),
):
    """Delete LFS objects of the repository for good, all or none.

    Refused (nothing deleted) when an object is not the repository's (404) or something still relies on it (409).
    Rewriting history is not supported: the commits that linked a deleted object stay, and are told its content is gone.
    """
    repo_row = get_repository(repo_type, namespace, name)
    if not repo_row:
        return hf_repo_not_found(f"{namespace}/{name}", repo_type)
    check_repo_delete_permission(repo_row, user)
    if payload.deletions.rewriteHistory:
        return hf_not_implemented(
            "Rewriting history",
            "The commits that linked a deleted object stay (their content is reported as gone); "
            "call permanently_delete_lfs_files(..., rewrite_history=False)",
        )

    shas = sorted({sha.lower() for sha in payload.deletions.sha})
    H = LFSObjectHistory
    known = {
        sha
        for (sha,) in H.select(H.sha256)
        .where((H.repository == repo_row.id) & H.sha256.in_(shas))
        .distinct()
        .tuples()
    }
    unknown = [sha for sha in shas if sha not in known]
    if unknown:
        return _refused(
            404,
            HFErrorCode.ENTRY_NOT_FOUND,
            f"Not an LFS file of {namespace}/{name}: {', '.join(unknown)}. Nothing was deleted.",
            {"unknown": unknown},
        )

    gone = lfs_gc.deleted_shas(shas)
    todo = [sha for sha in shas if sha not in gone]
    in_use = {}
    for sha in todo:
        if reason := lfs_gc.retention_reason(sha, ignore_history_of=repo_row.id):
            in_use[sha] = reason
    if in_use:
        detail = "; ".join(f"{sha[:12]}: {_WHY[reason]}" for sha, reason in in_use.items())
        return _refused(
            409,
            HFErrorCode.BAD_REQUEST,
            f"Nothing was deleted, {len(in_use)} object(s) are still in use ({detail})",
            {"inUse": in_use},
        )

    # Decided again under each object's lock: a request relying on one since the check keeps it
    doomed, kept = [], {}
    for sha in todo:
        if lfs_gc.begin_delete(sha, ignore_history_of=repo_row.id):
            doomed.append(sha)
        else:
            kept[sha] = "changed"
    if doomed:
        keys = [lfs_key(sha) for sha in doomed] + [claim_key(repo_row.id, sha) for sha in doomed]
        try:
            await run_in_s3_executor(_delete_keys, cfg.s3.bucket, keys)
        except Exception as e:
            lfs_gc.abort_delete(doomed)  # still stored: not tombstoned
            logger.exception(f"Could not delete {len(doomed)} LFS object(s) of {namespace}/{name}")
            return hf_server_error(f"Failed to delete from storage: {e}")
        lfs_gc.finish_delete(doomed)
        LfsGrant.delete().where(
            (LfsGrant.repository == repo_row.id) & LfsGrant.sha256.in_(doomed) & (LfsGrant.kind == "claim")
        ).execute()
        logger.info(f"Deleted {len(doomed)} LFS object(s) of {namespace}/{name} on {user.username}'s request")
    return {"deleted": doomed, "alreadyDeleted": [sha for sha in shas if sha in gone], "kept": kept}
