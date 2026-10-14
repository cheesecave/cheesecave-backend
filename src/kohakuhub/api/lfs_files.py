"""A repository's LFS objects (#66).

``HfApi.list_lfs_files`` reads ``GET /api/{type}s/{repo}/lfs-files``: what the repository's history says it stored (an
object is listed once, with the path and ref of its latest version).

``permanently_delete_lfs_files`` posts to ``.../lfs-files/batch`` and is answered ``501`` whatever it asks. LFS objects are
stored once for every repository (``kohakuhub.lfs_gc``), so one repository cannot free one on its own: that would need
a release that is the repository's alone while the bytes stay for the others, which needs its own design. What frees
storage today is garbage collection, by the repository's ``lfs_keep_versions``.
"""

from datetime import timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from peewee import fn

from kohakuhub.api.repo.utils.hf import hf_bad_request, hf_not_implemented, hf_repo_not_found
from kohakuhub.api.utils.paging import link_header
from kohakuhub.auth.dependencies import get_current_user
from kohakuhub.auth.permissions import check_repo_delete_permission, check_repo_write_permission
from kohakuhub.constants import DATETIME_FORMAT_ISO
from kohakuhub.db import Commit, File, LFSObjectHistory, LfsObjectTombstone, User
from kohakuhub.db_operations import get_repository

router = APIRouter()

PAGE_SIZE = 1000


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


@router.post("/{repo_type}s/{namespace}/{name}/lfs-files/batch")
async def delete_lfs_files(
    repo_type: str,
    namespace: str,
    name: str,
    user: User = Depends(get_current_user),
):
    """``permanently_delete_lfs_files``: not supported, whatever the request says (nothing is read, nothing is deleted)."""
    repo_row = get_repository(repo_type, namespace, name)
    if not repo_row:
        return hf_repo_not_found(f"{namespace}/{name}", repo_type)
    check_repo_delete_permission(repo_row, user)
    return hf_not_implemented(
        "permanently_delete_lfs_files",
        "LFS objects are stored once for every repository, so one repository cannot delete one on its own. "
        "Garbage collection frees the old versions of a file beyond the repository's LFS keep versions "
        "(repository settings)",
    )
